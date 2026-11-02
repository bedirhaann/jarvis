from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING

from core.paths import ensure_secure_file
from services.memory.text import MemorySecretError

if TYPE_CHECKING:
    from services.memory.service import MemoryService


_COMMAND_PREFIX_RE = re.compile(
    r"^\s*/(hatırla|hatirla|unut|hafıza|hafiza|proje|geçmiş|gecmis)(?:\s+(.*))?$",
    re.IGNORECASE | re.DOTALL,
)


def is_local_memory_command(message: str) -> bool:
    if not message:
        return False
    return _COMMAND_PREFIX_RE.match(message.strip()) is not None


def execute_local_memory_command(
    service: "MemoryService",
    message: str,
) -> tuple[bool, str]:
    """Parse and execute a local memory command without touching the LLM.
    Returns (handled, markdown_response)."""
    m = _COMMAND_PREFIX_RE.match((message or "").strip())
    if not m:
        return False, ""

    cmd = m.group(1).lower()
    arg_str = (m.group(2) or "").strip()

    if cmd in ("hatırla", "hatirla"):
        return True, _handle_hatirla(service, arg_str)
    if cmd == "unut":
        return True, _handle_unut(service, arg_str)
    if cmd in ("hafıza", "hafiza"):
        return True, _handle_hafiza(service, arg_str)
    if cmd == "proje":
        return True, _handle_proje(service, arg_str)
    if cmd in ("geçmiş", "gecmis"):
        return True, _handle_gecmis(service, arg_str)

    return False, ""


def _parse_hatirla_args(raw: str) -> tuple[str, bool, str]:
    """Return (scope, pinned, clean_content)."""
    text = raw.strip()
    pinned = False

    # Check pin flags
    pin_patterns = [r"--sabit\b", r"\[sabit\]", r"--pin\b"]
    for pat in pin_patterns:
        if re.search(pat, text, flags=re.IGNORECASE):
            pinned = True
            text = re.sub(pat, "", text, flags=re.IGNORECASE).strip()

    scope = "global"
    scope_match = re.match(
        r"^(?:\[(proje|project|konuşma|konusma|conversation|global)\]|(proje|project|konuşma|konusma|conversation|global)\s*:)\s*(.*)$",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if scope_match:
        raw_scope = (scope_match.group(1) or scope_match.group(2) or "").lower()
        text = (scope_match.group(3) or "").strip()
        if raw_scope in ("proje", "project"):
            scope = "project"
        elif raw_scope in ("konuşma", "konusma", "conversation"):
            scope = "conversation"
        else:
            scope = "global"

    return scope, pinned, text.strip()


def _handle_hatirla(service: "MemoryService", arg_str: str) -> str:
    if not arg_str:
        return (
            "### 🧠 `/hatırla` Kullanımı\n"
            "- `/hatırla <bilgi>` — Genel (global) hafızaya kaydeder.\n"
            "- `/hatırla proje: <bilgi>` — Aktif projeye özel kaydeder.\n"
            "- `/hatırla konuşma: <bilgi>` — Yalnızca mevcut konuşma için kaydeder.\n"
            "- `/hatırla --sabit <bilgi>` — Sabitlenmiş (yüksek öncelikli) olarak kaydeder."
        )

    scope, pinned, content = _parse_hatirla_args(arg_str)
    if not content:
        return "⚠️ Kaydedilecek hafıza metni boş olamaz."

    if scope == "project" and service.active_project_id is None:
        return (
            "⚠️ Proje kapsamlı hafıza eklemek için önce aktif bir proje seçmelisiniz.\n"
            "Örnek: `/proje jarvis`"
        )

    try:
        rec, created = service.add_explicit_memory(
            content=content,
            scope=scope,
            pinned=pinned,
            importance=5 if pinned else 4,
        )
    except MemorySecretError as exc:
        return f"🔒 **Güvenlik Engeli:** {exc}"
    except ValueError as exc:
        return f"⚠️ **Hata:** {exc}"

    scope_label = {
        "global": "Genel",
        "project": "Proje",
        "conversation": "Konuşma",
    }.get(rec.scope, rec.scope)
    pin_badge = " 📌 *(Sabit)*" if rec.pinned else ""
    action_word = "Hafızaya kaydedildi" if created else "Mevcut hafıza kaydı güncellendi"
    return (
        f"✅ **{action_word}** (`#{rec.id}` · {scope_label}{pin_badge})\n"
        f"> {rec.content}"
    )


def _handle_unut(service: "MemoryService", arg_str: str) -> str:
    if not arg_str:
        return (
            "### 🗑️ `/unut` Kullanımı\n"
            "- `/unut <id>` — Belirtilen hafıza kaydını pasife alır (soft-delete).\n"
            "- `/unut <id> --kalıcı` — Veritabanından ve arama indeksinden kalıcı olarak siler."
        )

    hard = False
    cleaned = arg_str
    for flag in ("--kalıcı", "--kalici", "--hard", "kalıcı", "kalici"):
        if re.search(rf"(?:^|\s){re.escape(flag)}(?:\s|$)", cleaned, flags=re.IGNORECASE):
            hard = True
            cleaned = re.sub(
                rf"(?:^|\s){re.escape(flag)}(?:\s|$)", " ", cleaned, flags=re.IGNORECASE
            ).strip()

    id_match = re.match(r"^#?(\d+)$", cleaned.strip())
    if not id_match:
        return "⚠️ Lütfen silinecek hafıza kimliğini sayı olarak belirtin. Örnek: `/unut 3` veya `/unut 3 --kalıcı`"

    mem_id = int(id_match.group(1))
    deleted = service.delete_memory(mem_id, hard=hard)
    if not deleted:
        return f"⚠️ `#{mem_id}` numaralı hafıza kaydı bulunamadı."

    mode_str = "kalıcı olarak (hard-delete)" if hard else "arşivlenerek (soft-delete)"
    return f"🗑️ `#{mem_id}` numaralı hafıza kaydı {mode_str} silindi."


def _handle_hafiza(service: "MemoryService", arg_str: str) -> str:
    raw = arg_str.strip()
    if not raw or raw.lower() in ("liste", "list"):
        return _format_memory_list(service)

    parts = raw.split(maxsplit=1)
    sub = parts[0].lower()
    rest = parts[1].strip() if len(parts) > 1 else ""

    if sub in ("ara", "search", "bul"):
        if not rest:
            return "⚠️ Arama sorgusu belirtin. Örnek: `/hafıza ara python`"
        results = service.search_memories(rest)
        if not results:
            return f"🔍 **`{rest}`** için eşleşen aktif hafıza kaydı bulunamadı."
        lines = [f"### 🔍 Hafıza Arama Sonuçları (`{rest}`)"]
        for sm in results:
            rec = sm.record
            pin = "📌 " if rec.pinned else ""
            lines.append(
                f"- **`#{rec.id}`** [{rec.scope}] {pin}{rec.content} *(skor: {sm.score:.2f})*"
            )
        return "\n".join(lines)

    if sub in ("bekleyen", "öneriler", "oneriler", "pending"):
        pending = service.list_pending_memories()
        if not pending:
            return "✅ Onay bekleyen hafıza önerisi bulunmuyor."
        lines = ["### 💡 Onay Bekleyen Hafıza Önerileri"]
        for rec in pending:
            lines.append(f"- **`#{rec.id}`** [{rec.scope}] {rec.content}")
        lines.append("")
        lines.append(
            "**İşlemler:** `/hafıza onayla <id>` · `/hafıza düzenle <id> <yeni metin>` · `/hafıza reddet <id>`"
        )
        return "\n".join(lines)

    if sub in ("onayla", "confirm", "kabul"):
        m = re.match(r"^#?(\d+)(?:\s+(.*))?$", rest, flags=re.DOTALL)
        if not m:
            return "⚠️ Kullanım: `/hafıza onayla <id>`"
        mem_id = int(m.group(1))
        edited = (m.group(2) or "").strip() or None
        try:
            rec = service.confirm_pending_memory(mem_id, edited_content=edited)
        except MemorySecretError as exc:
            return f"🔒 **Güvenlik Engeli:** {exc}"
        except ValueError as exc:
            return f"⚠️ **Hata:** {exc}"
        if rec is None:
            return f"⚠️ `#{mem_id}` numaralı hafıza kaydı bulunamadı."
        return f"✅ **`#{rec.id}` numaralı hafıza onaylandı ve aktifleştirildi:**\n> {rec.content}"

    if sub in ("düzenle", "duzenle", "edit"):
        m = re.match(r"^#?(\d+)\s+(.+)$", rest, flags=re.DOTALL)
        if not m:
            return "⚠️ Kullanım: `/hafıza düzenle <id> <yeni metin>`"
        mem_id = int(m.group(1))
        new_text = m.group(2).strip()
        try:
            rec = service.edit_memory(mem_id, new_text)
        except MemorySecretError as exc:
            return f"🔒 **Güvenlik Engeli:** {exc}"
        except ValueError as exc:
            return f"⚠️ **Hata:** {exc}"
        if rec is None:
            return f"⚠️ `#{mem_id}` numaralı hafıza kaydı bulunamadı."
        return f"✏️ **`#{rec.id}` numaralı hafıza güncellendi ve aktifleştirildi:**\n> {rec.content}"

    if sub in ("reddet", "reject", "iptal"):
        m = re.match(r"^#?(\d+)$", rest.strip())
        if not m:
            return "⚠️ Kullanım: `/hafıza reddet <id>`"
        mem_id = int(m.group(1))
        ok = service.reject_pending_memory(mem_id)
        if not ok:
            return f"⚠️ `#{mem_id}` numaralı hafıza önerisi bulunamadı."
        return f"🚫 `#{mem_id}` numaralı hafıza önerisi reddedildi."

    if sub in ("dışa-aktar", "disa-aktar", "export"):
        export_path = service.export_memories_to_file()
        return f"📦 Aktif hafıza kayıtları güvenli olarak dışa aktarıldı (`0600`):\n`{export_path}`"

    # Fallback: treat argument as search query
    results = service.search_memories(raw)
    if not results:
        return (
            f"🔍 **`{raw}`** için eşleşen hafıza bulunamadı.\n"
            "Komutlar: `/hafıza`, `/hafıza ara <sorgu>`, `/hafıza bekleyen`, "
            "`/hafıza onayla <id>`, `/hafıza düzenle <id> <metin>`, `/hafıza reddet <id>`, `/hafıza dışa-aktar`"
        )
    lines = [f"### 🔍 Hafıza Arama Sonuçları (`{raw}`)"]
    for sm in results:
        rec = sm.record
        pin = "📌 " if rec.pinned else ""
        lines.append(f"- **`#{rec.id}`** [{rec.scope}] {pin}{rec.content}")
    return "\n".join(lines)


def _format_memory_list(service: "MemoryService") -> str:
    records = service.list_active_memories()
    pending = service.list_pending_memories()
    active_proj = service.get_active_project()

    lines: list[str] = ["### 🧠 Aktif Hafıza Kayıtları"]
    if active_proj is not None:
        lines.append(f"**Aktif Proje:** `{active_proj.name}` (`{active_proj.slug}`)")

    if not records:
        lines.append("Henüz kayıtlı aktif hafıza bulunmuyor. Eklemek için: `/hatırla <bilgi>`")
    else:
        for rec in records:
            pin = "📌 " if rec.pinned else ""
            lines.append(
                f"- **`#{rec.id}`** [{rec.scope} · önem:{rec.importance}] {pin}{rec.content}"
            )

    if pending:
        lines.append("")
        lines.append(
            f"💡 **{len(pending)} adet onay bekleyen hafıza önerisi var.** İncelemek için: `/hafıza bekleyen`"
        )
    return "\n".join(lines)


def _handle_proje(service: "MemoryService", arg_str: str) -> str:
    raw = arg_str.strip()
    if not raw or raw.lower() in ("liste", "list"):
        active = service.get_active_project()
        projects = service.list_projects()
        lines = ["### 📁 Proje Bağlamı"]
        if active:
            lines.append(f"**Aktif Proje:** `{active.name}` (`{active.slug}`)")
        else:
            lines.append("**Aktif Proje:** Yok *(Yalnızca genel hafıza aktif)*")

        if projects:
            lines.append("\n**Kayıtlı Projeler:**")
            for p in projects:
                marker = "✅ " if (active and p.id == active.id) else "• "
                lines.append(f"{marker}**{p.name}** (`{p.slug}`)")
        lines.append("\nDeğiştirmek için: `/proje <ad>` · Kapatmak için: `/proje yok`")
        return "\n".join(lines)

    if raw.lower() in ("yok", "none", "kapat", "temizle", "off"):
        service.set_active_project(None)
        return "📁 Aktif proje bağlamı kapatıldı. Yalnızca genel (global) hafıza kullanılacak."

    proj = service.set_active_project(raw)
    assert proj is not None
    return (
        f"📁 **Aktif proje değiştirildi:** `{proj.name}` (`{proj.slug}`)\n"
        "Bu projeye özel hafıza eklemek için: `/hatırla proje: <bilgi>`"
    )


def _handle_gecmis(service: "MemoryService", arg_str: str) -> str:
    raw = arg_str.strip()
    if raw:
        m = re.match(r"^#?(\d+)$", raw)
        if m:
            conv_id = int(m.group(1))
            msgs = service.list_conversation_messages(conv_id, limit=12)
            if not msgs:
                return f"⚠️ `#{conv_id}` numaralı konuşmada mesaj bulunamadı."
            lines = [f"### 💬 Konuşma `#{conv_id}` Son Mesajları"]
            for msg in msgs:
                role_label = "👤 Kullanıcı" if msg.role == "user" else "🤖 JARVIS"
                snippet = msg.content.replace("\n", " ").strip()
                if len(snippet) > 160:
                    snippet = snippet[:157] + "..."
                lines.append(f"- **{role_label}:** {snippet}")
            return "\n".join(lines)

    convs = service.list_recent_conversations(limit=15)
    if not convs:
        return "💬 Henüz kayıtlı konuşma geçmişi bulunmuyor."

    lines = ["### 🕓 Son Konuşmalar"]
    current_id = service.current_conversation_id
    for c in convs:
        cid = c["id"]
        title = c["title"] or "(Başlıksız konuşma)"
        count = c["message_count"]
        proj_name = c.get("project_name")
        proj_tag = f" · 📁 `{proj_name}`" if proj_name else ""
        active_tag = " *(aktif)*" if cid == current_id else ""
        lines.append(
            f"- **`#{cid}`**{active_tag} {title} — {count} mesaj{proj_tag} (`{c['started_at'][:19]}`)"
        )
    lines.append("\nDetay görmek için: `/geçmiş <id>`")
    return "\n".join(lines)
