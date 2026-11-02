"""JARVIS Stage 8 — SQLite Repositories & Data Records (Pure SQL, No Qt)."""

from __future__ import annotations

import re
import sqlite3
import uuid
from dataclasses import asdict, dataclass
from typing import Any

from services.memory.database import MemoryDatabase, utc_now_iso
from services.memory.text import (
    MemorySecretError,
    compute_content_hash,
    contains_secret,
    normalize_tr,
    sanitize_memory_content,
)


@dataclass
class ProjectRecord:
    id: int
    slug: str
    name: str
    root_path: str | None
    description: str | None
    status: str
    created_at: str
    updated_at: str


@dataclass
class ConversationRecord:
    id: int
    uuid: str
    project_id: int | None
    title: str | None
    summary: str | None
    summary_upto_message_id: int | None
    started_at: str
    last_activity_at: str
    archived: int


@dataclass
class AgentSessionRecord:
    id: int
    conversation_id: int
    backend: str
    backend_conversation_id: str | None
    model: str
    mode: str | None
    started_at: str
    ended_at: str | None
    end_reason: str | None


@dataclass
class MessageRecord:
    id: int
    conversation_id: int
    session_id: int | None
    role: str
    source: str
    content: str
    search_text: str
    status: str
    latency_ms: int | None
    created_at: str


@dataclass
class MemoryRecord:
    id: int
    scope: str
    project_id: int | None
    conversation_id: int | None
    kind: str
    content: str
    search_text: str
    content_hash: str
    importance: int
    status: str
    origin: str
    source_message_id: int | None
    pinned: int
    superseded_by: int | None
    access_count: int
    last_accessed_at: str | None
    expires_at: str | None
    deleted_at: str | None
    created_at: str
    updated_at: str


def slugify_project_name(name: str) -> str:
    norm = normalize_tr(name)
    slug = re.sub(r"[^a-z0-9]+", "-", norm).strip("-")
    return slug or "proje"


class SettingsRepository:
    def __init__(self, db: MemoryDatabase):
        self.db = db

    def get(self, key: str, default: str | None = None) -> str | None:
        row = self.db.connection.execute(
            "SELECT value FROM settings WHERE key = ?;", (key,)
        ).fetchone()
        return str(row["value"]) if row is not None else default

    def get_int(self, key: str, default: int | None = None) -> int | None:
        val = self.get(key, default=None)
        if val is None or not str(val).strip():
            return default
        try:
            return int(str(val).strip())
        except ValueError:
            return default

    def set(self, key: str, value: str) -> None:
        now = utc_now_iso()
        with self.db.connection:
            self.db.connection.execute(
                "INSERT INTO settings(key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at;",
                (key, str(value), now),
            )

    def delete(self, key: str) -> None:
        with self.db.connection:
            self.db.connection.execute("DELETE FROM settings WHERE key = ?;", (key,))


class ProjectRepository:
    def __init__(self, db: MemoryDatabase):
        self.db = db

    @staticmethod
    def _from_row(row: sqlite3.Row) -> ProjectRecord:
        return ProjectRecord(
            id=int(row["id"]),
            slug=str(row["slug"]),
            name=str(row["name"]),
            root_path=row["root_path"],
            description=row["description"],
            status=str(row["status"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def create_or_get(
        self,
        name: str,
        root_path: str | None = None,
        description: str | None = None,
    ) -> ProjectRecord:
        cleaned_name = name.strip()
        slug = slugify_project_name(cleaned_name)
        existing = self.get_by_slug(slug)
        if existing is not None:
            return existing

        now = utc_now_iso()
        with self.db.connection:
            cur = self.db.connection.execute(
                "INSERT INTO projects(slug, name, root_path, description, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 'active', ?, ?);",
                (slug, cleaned_name, root_path, description, now, now),
            )
            proj_id = int(cur.lastrowid)
        proj = self.get_by_id(proj_id)
        assert proj is not None
        return proj

    def get_by_id(self, project_id: int) -> ProjectRecord | None:
        row = self.db.connection.execute(
            "SELECT * FROM projects WHERE id = ?;", (project_id,)
        ).fetchone()
        return self._from_row(row) if row is not None else None

    def get_by_slug(self, slug_or_name: str) -> ProjectRecord | None:
        slug = slugify_project_name(slug_or_name)
        row = self.db.connection.execute(
            "SELECT * FROM projects WHERE slug = ?;", (slug,)
        ).fetchone()
        return self._from_row(row) if row is not None else None

    def list_projects(self, status: str = "active") -> list[ProjectRecord]:
        rows = self.db.connection.execute(
            "SELECT * FROM projects WHERE status = ? ORDER BY updated_at DESC, id DESC;",
            (status,),
        ).fetchall()
        return [self._from_row(r) for r in rows]


class ConversationRepository:
    def __init__(self, db: MemoryDatabase):
        self.db = db

    @staticmethod
    def _from_row(row: sqlite3.Row) -> ConversationRecord:
        return ConversationRecord(
            id=int(row["id"]),
            uuid=str(row["uuid"]),
            project_id=int(row["project_id"]) if row["project_id"] is not None else None,
            title=row["title"],
            summary=row["summary"],
            summary_upto_message_id=(
                int(row["summary_upto_message_id"])
                if row["summary_upto_message_id"] is not None
                else None
            ),
            started_at=str(row["started_at"]),
            last_activity_at=str(row["last_activity_at"]),
            archived=int(row["archived"]),
        )

    def create(
        self,
        project_id: int | None = None,
        title: str | None = None,
    ) -> ConversationRecord:
        now = utc_now_iso()
        conv_uuid = str(uuid.uuid4())
        with self.db.connection:
            cur = self.db.connection.execute(
                "INSERT INTO conversations(uuid, project_id, title, started_at, last_activity_at, archived) "
                "VALUES (?, ?, ?, ?, ?, 0);",
                (conv_uuid, project_id, title, now, now),
            )
            conv_id = int(cur.lastrowid)
        conv = self.get_by_id(conv_id)
        assert conv is not None
        return conv

    def get_by_id(self, conv_id: int) -> ConversationRecord | None:
        row = self.db.connection.execute(
            "SELECT * FROM conversations WHERE id = ?;", (conv_id,)
        ).fetchone()
        return self._from_row(row) if row is not None else None

    def touch(self, conv_id: int, title_if_empty: str | None = None) -> None:
        now = utc_now_iso()
        with self.db.connection:
            if title_if_empty:
                trimmed = title_if_empty.strip()[:80]
                self.db.connection.execute(
                    "UPDATE conversations SET last_activity_at = ?, "
                    "title = COALESCE(title, ?) WHERE id = ?;",
                    (now, trimmed, conv_id),
                )
            else:
                self.db.connection.execute(
                    "UPDATE conversations SET last_activity_at = ? WHERE id = ?;",
                    (now, conv_id),
                )

    def update_project(self, conv_id: int, project_id: int | None) -> None:
        with self.db.connection:
            self.db.connection.execute(
                "UPDATE conversations SET project_id = ? WHERE id = ?;",
                (project_id, conv_id),
            )

    def archive(self, conv_id: int) -> None:
        with self.db.connection:
            self.db.connection.execute(
                "UPDATE conversations SET archived = 1, last_activity_at = ? WHERE id = ?;",
                (utc_now_iso(), conv_id),
            )

    def create_conversation(
        self,
        project_id: int | None = None,
        title: str | None = None,
    ) -> ConversationRecord:
        return self.create(project_id=project_id, title=title)

    def update_title_if_empty(self, conv_id: int, text: str) -> None:
        self.touch(conv_id, title_if_empty=text)

    def set_project(self, conv_id: int, project_id: int | None) -> None:
        self.update_project(conv_id, project_id)

    def end_conversation(self, conv_id: int) -> None:
        self.touch(conv_id)

    def list_recent(self, limit: int = 10) -> list[ConversationRecord]:
        rows = self.db.connection.execute(
            "SELECT * FROM conversations ORDER BY id DESC LIMIT ?;",
            (max(1, int(limit)),),
        ).fetchall()
        return [self._from_row(r) for r in rows]

    def list_recent_with_stats(self, limit: int = 15) -> list[dict[str, Any]]:
        rows = self.db.connection.execute(
            "SELECT c.*, p.name AS project_name, "
            "(SELECT COUNT(*) FROM messages m WHERE m.conversation_id = c.id) AS message_count "
            "FROM conversations c "
            "LEFT JOIN projects p ON p.id = c.project_id "
            "ORDER BY c.id DESC LIMIT ?;",
            (max(1, int(limit)),),
        ).fetchall()
        return [
            {
                "id": int(r["id"]),
                "uuid": str(r["uuid"]),
                "project_id": int(r["project_id"]) if r["project_id"] is not None else None,
                "project_name": r["project_name"],
                "title": r["title"],
                "started_at": str(r["started_at"]),
                "last_activity_at": str(r["last_activity_at"]),
                "message_count": int(r["message_count"]),
            }
            for r in rows
        ]


class SessionRepository:
    def __init__(self, db: MemoryDatabase):
        self.db = db

    @staticmethod
    def _from_row(row: sqlite3.Row) -> AgentSessionRecord:
        return AgentSessionRecord(
            id=int(row["id"]),
            conversation_id=int(row["conversation_id"]),
            backend=str(row["backend"]),
            backend_conversation_id=row["backend_conversation_id"],
            model=str(row["model"]),
            mode=row["mode"],
            started_at=str(row["started_at"]),
            ended_at=row["ended_at"],
            end_reason=row["end_reason"],
        )

    def start_session(
        self,
        conversation_id: int,
        model: str | None = "gemini-4-argon-medium",
        mode: str | None = "plan",
        backend_conversation_id: str | None = None,
        backend_conv_id: str | None = None,
        backend: str = "antigravity",
    ) -> AgentSessionRecord:
        now = utc_now_iso()
        b_id = backend_conversation_id if backend_conversation_id is not None else backend_conv_id
        model_name = model or "gemini-4-argon-medium"
        with self.db.connection:
            cur = self.db.connection.execute(
                "INSERT INTO agent_sessions(conversation_id, backend, backend_conversation_id, model, mode, started_at) "
                "VALUES (?, ?, ?, ?, ?, ?);",
                (conversation_id, backend, b_id, model_name, mode, now),
            )
            sess_id = int(cur.lastrowid)
        sess = self.get_by_id(sess_id)
        assert sess is not None
        return sess

    def update_backend_conv_id(self, session_id: int, backend_conv_id: str) -> None:
        with self.db.connection:
            self.db.connection.execute(
                "UPDATE agent_sessions SET backend_conversation_id = ? WHERE id = ?;",
                (backend_conv_id, session_id),
            )

    def end_session(self, session_id: int, end_reason: str = "normal") -> None:
        valid_reasons = {"normal", "timeout", "crash", "shutdown", "reset"}
        reason = end_reason if end_reason in valid_reasons else "normal"
        with self.db.connection:
            self.db.connection.execute(
                "UPDATE agent_sessions SET ended_at = ?, end_reason = ? WHERE id = ? AND ended_at IS NULL;",
                (utc_now_iso(), reason, session_id),
            )

    def get_by_id(self, session_id: int) -> AgentSessionRecord | None:
        row = self.db.connection.execute(
            "SELECT * FROM agent_sessions WHERE id = ?;", (session_id,)
        ).fetchone()
        return self._from_row(row) if row is not None else None

    def list_for_conversation(self, conversation_id: int) -> list[AgentSessionRecord]:
        rows = self.db.connection.execute(
            "SELECT * FROM agent_sessions WHERE conversation_id = ? ORDER BY id ASC;",
            (conversation_id,),
        ).fetchall()
        return [self._from_row(r) for r in rows]


class MessageRepository:
    def __init__(self, db: MemoryDatabase):
        self.db = db

    @staticmethod
    def _from_row(row: sqlite3.Row) -> MessageRecord:
        return MessageRecord(
            id=int(row["id"]),
            conversation_id=int(row["conversation_id"]),
            session_id=int(row["session_id"]) if row["session_id"] is not None else None,
            role=str(row["role"]),
            source=str(row["source"]),
            content=str(row["content"]),
            search_text=str(row["search_text"]),
            status=str(row["status"]),
            latency_ms=int(row["latency_ms"]) if row["latency_ms"] is not None else None,
            created_at=str(row["created_at"]),
        )

    def add_message(
        self,
        conversation_id: int,
        role: str,
        content: str,
        source: str = "llm",
        session_id: int | None = None,
        status: str = "complete",
        latency_ms: int | None = None,
    ) -> MessageRecord:
        now = utc_now_iso()
        s_text = normalize_tr(content)
        with self.db.connection:
            cur = self.db.connection.execute(
                "INSERT INTO messages(conversation_id, session_id, role, source, content, search_text, status, latency_ms, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);",
                (
                    conversation_id,
                    session_id,
                    role,
                    source,
                    content,
                    s_text,
                    status,
                    latency_ms,
                    now,
                ),
            )
            msg_id = int(cur.lastrowid)
        msg = self.get_by_id(msg_id)
        assert msg is not None
        return msg

    def get_by_id(self, message_id: int) -> MessageRecord | None:
        row = self.db.connection.execute(
            "SELECT * FROM messages WHERE id = ?;", (message_id,)
        ).fetchone()
        return self._from_row(row) if row is not None else None

    def list_for_conversation(
        self,
        conversation_id: int,
        limit: int = 20,
    ) -> list[MessageRecord]:
        rows = self.db.connection.execute(
            "SELECT * FROM ("
            "  SELECT * FROM messages WHERE conversation_id = ? ORDER BY id DESC LIMIT ?"
            ") ORDER BY id ASC;",
            (conversation_id, max(1, int(limit))),
        ).fetchall()
        return [self._from_row(r) for r in rows]


class MemoryRepository:
    def __init__(self, db: MemoryDatabase):
        self.db = db

    @staticmethod
    def _from_row(row: sqlite3.Row) -> MemoryRecord:
        return MemoryRecord(
            id=int(row["id"]),
            scope=str(row["scope"]),
            project_id=int(row["project_id"]) if row["project_id"] is not None else None,
            conversation_id=(
                int(row["conversation_id"]) if row["conversation_id"] is not None else None
            ),
            kind=str(row["kind"]),
            content=str(row["content"]),
            search_text=str(row["search_text"]),
            content_hash=str(row["content_hash"]),
            importance=int(row["importance"]),
            status=str(row["status"]),
            origin=str(row["origin"]),
            source_message_id=(
                int(row["source_message_id"]) if row["source_message_id"] is not None else None
            ),
            pinned=int(row["pinned"]),
            superseded_by=(
                int(row["superseded_by"]) if row["superseded_by"] is not None else None
            ),
            access_count=int(row["access_count"]),
            last_accessed_at=row["last_accessed_at"],
            expires_at=row["expires_at"],
            deleted_at=row["deleted_at"],
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def add_memory(
        self,
        content: str,
        scope: str = "global",
        kind: str = "fact",
        project_id: int | None = None,
        conversation_id: int | None = None,
        importance: int = 3,
        status: str = "active",
        origin: str = "explicit",
        source_message_id: int | None = None,
        pinned: bool = False,
        expires_at: str | None = None,
        tags: list[str] | None = None,
    ) -> tuple[MemoryRecord, bool]:
        """Store or deduplicate a memory record.
        Returns (record, created_new).
        Raises MemorySecretError if content contains a secret/credential."""
        has_secret, secret_label = contains_secret(content)
        if has_secret:
            raise MemorySecretError(
                f"Güvenlik nedeniyle API anahtarı, parola veya gizli bilgi hafızaya kaydedilemez ({secret_label})."
            )

        sanitized = sanitize_memory_content(content)
        if not sanitized:
            raise ValueError("Hafıza içeriği boş olamaz.")

        if scope == "global":
            project_id = None
            conversation_id = None
        elif scope == "project":
            if project_id is None:
                raise ValueError("Proje kapsamlı hafıza için aktif bir proje (project_id) gereklidir.")
        elif scope == "conversation":
            if conversation_id is None:
                raise ValueError("Konuşma kapsamlı hafıza için conversation_id gereklidir.")
        else:
            raise ValueError(f"Geçersiz hafıza kapsamı: {scope}")

        s_text = normalize_tr(sanitized)
        c_hash = compute_content_hash(sanitized)
        imp = max(1, min(5, int(importance)))
        now = utc_now_iso()

        # Check deduplication in same (scope, project_id, conversation_id, content_hash)
        existing_row = self.db.connection.execute(
            "SELECT * FROM memories WHERE scope = ? "
            "AND IFNULL(project_id, 0) = IFNULL(?, 0) "
            "AND IFNULL(conversation_id, 0) = IFNULL(?, 0) "
            "AND content_hash = ? AND deleted_at IS NULL;",
            (scope, project_id, conversation_id, c_hash),
        ).fetchone()

        if existing_row is not None:
            existing = self._from_row(existing_row)
            new_status = existing.status
            if existing.status == "pending" and status == "active":
                new_status = "active"
            new_imp = max(existing.importance, imp)
            new_pin = 1 if (existing.pinned or pinned) else 0
            with self.db.connection:
                self.db.connection.execute(
                    "UPDATE memories SET status = ?, importance = ?, pinned = ?, updated_at = ? WHERE id = ?;",
                    (new_status, new_imp, new_pin, now, existing.id),
                )
                if tags:
                    for tag in tags:
                        clean_tag = normalize_tr(tag)
                        if clean_tag:
                            self.db.connection.execute(
                                "INSERT OR IGNORE INTO memory_tags(memory_id, tag) VALUES (?, ?);",
                                (existing.id, clean_tag),
                            )
            updated = self.get_by_id(existing.id)
            assert updated is not None
            return updated, False

        with self.db.connection:
            cur = self.db.connection.execute(
                "INSERT INTO memories("
                "  scope, project_id, conversation_id, kind, content, search_text, content_hash, "
                "  importance, status, origin, source_message_id, pinned, expires_at, created_at, updated_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);",
                (
                    scope,
                    project_id,
                    conversation_id,
                    kind,
                    sanitized,
                    s_text,
                    c_hash,
                    imp,
                    status,
                    origin,
                    source_message_id,
                    1 if pinned else 0,
                    expires_at,
                    now,
                    now,
                ),
            )
            mem_id = int(cur.lastrowid)
            if tags:
                for tag in tags:
                    clean_tag = normalize_tr(tag)
                    if clean_tag:
                        self.db.connection.execute(
                            "INSERT OR IGNORE INTO memory_tags(memory_id, tag) VALUES (?, ?);",
                            (mem_id, clean_tag),
                        )

        rec = self.get_by_id(mem_id)
        assert rec is not None
        return rec, True

    def get_by_id(
        self,
        memory_id: int,
        include_deleted: bool = False,
    ) -> MemoryRecord | None:
        if include_deleted:
            row = self.db.connection.execute(
                "SELECT * FROM memories WHERE id = ?;", (memory_id,)
            ).fetchone()
        else:
            row = self.db.connection.execute(
                "SELECT * FROM memories WHERE id = ? AND deleted_at IS NULL;",
                (memory_id,),
            ).fetchone()
        return self._from_row(row) if row is not None else None

    def confirm_pending(
        self,
        memory_id: int,
        edited_content: str | None = None,
    ) -> MemoryRecord | None:
        """Confirm a pending memory suggestion, optionally editing its text first."""
        rec = self.get_by_id(memory_id)
        if rec is None:
            return None
        if edited_content is not None and edited_content.strip():
            return self.update_content(memory_id, edited_content, new_status="active")

        now = utc_now_iso()
        with self.db.connection:
            self.db.connection.execute(
                "UPDATE memories SET status = 'active', updated_at = ? WHERE id = ?;",
                (now, memory_id),
            )
        return self.get_by_id(memory_id)

    def update_content(
        self,
        memory_id: int,
        new_content: str,
        new_status: str | None = None,
    ) -> MemoryRecord | None:
        """Update a memory's text, search_text, and content_hash (syncing FTS5 triggers)."""
        rec = self.get_by_id(memory_id)
        if rec is None:
            return None

        has_secret, secret_label = contains_secret(new_content)
        if has_secret:
            raise MemorySecretError(
                f"Güvenlik nedeniyle gizli bilgi hafızaya kaydedilemez ({secret_label})."
            )

        sanitized = sanitize_memory_content(new_content)
        if not sanitized:
            raise ValueError("Hafıza içeriği boş olamaz.")

        s_text = normalize_tr(sanitized)
        c_hash = compute_content_hash(sanitized)
        status_val = new_status if new_status is not None else rec.status
        now = utc_now_iso()

        with self.db.connection:
            self.db.connection.execute(
                "UPDATE memories SET content = ?, search_text = ?, content_hash = ?, "
                "status = ?, updated_at = ? WHERE id = ?;",
                (sanitized, s_text, c_hash, status_val, now, memory_id),
            )
        return self.get_by_id(memory_id)

    def reject_pending(self, memory_id: int) -> bool:
        """Reject a pending memory suggestion so it is never injected."""
        rec = self.get_by_id(memory_id)
        if rec is None:
            return False
        now = utc_now_iso()
        with self.db.connection:
            self.db.connection.execute(
                "UPDATE memories SET status = 'rejected', deleted_at = ?, updated_at = ? WHERE id = ?;",
                (now, now, memory_id),
            )
        return True

    def delete_memory(self, memory_id: int, hard: bool = False) -> bool:
        """Soft-delete by default (sets deleted_at), or hard-delete when hard=True."""
        rec = self.get_by_id(memory_id, include_deleted=hard)
        if rec is None:
            return False
        with self.db.connection:
            if hard:
                self.db.connection.execute(
                    "DELETE FROM memories WHERE id = ?;", (memory_id,)
                )
            else:
                now = utc_now_iso()
                self.db.connection.execute(
                    "UPDATE memories SET deleted_at = ?, updated_at = ? WHERE id = ?;",
                    (now, now, memory_id),
                )
        return True

    def list_memories(
        self,
        scope: str | None = None,
        project_id: int | None = None,
        conversation_id: int | None = None,
        status: str = "active",
        limit: int = 50,
    ) -> list[MemoryRecord]:
        clauses = ["deleted_at IS NULL", "status = ?"]
        params: list[Any] = [status]

        if scope is not None:
            clauses.append("scope = ?")
            params.append(scope)
        if project_id is not None:
            clauses.append("(scope = 'global' OR project_id = ?)")
            params.append(project_id)
        if conversation_id is not None:
            clauses.append("conversation_id = ?")
            params.append(conversation_id)

        where_sql = " AND ".join(clauses)
        params.append(max(1, int(limit)))
        rows = self.db.connection.execute(
            f"SELECT * FROM memories WHERE {where_sql} "
            "ORDER BY pinned DESC, importance DESC, id DESC LIMIT ?;",
            params,
        ).fetchall()
        return [self._from_row(r) for r in rows]

    def list_pending(
        self,
        project_id: int | None = None,
        conversation_id: int | None = None,
        limit: int = 30,
    ) -> list[MemoryRecord]:
        clauses = ["deleted_at IS NULL", "status = 'pending'"]
        params: list[Any] = []
        if project_id is not None or conversation_id is not None:
            scope_Parts = ["scope = 'global'"]
            if project_id is not None:
                scope_Parts.append("(scope = 'project' AND project_id = ?)")
                params.append(project_id)
            if conversation_id is not None:
                scope_Parts.append("(scope = 'conversation' AND conversation_id = ?)")
                params.append(conversation_id)
            clauses.append("(" + " OR ".join(scope_Parts) + ")")

        where_sql = " AND ".join(clauses)
        params.append(max(1, int(limit)))
        rows = self.db.connection.execute(
            f"SELECT * FROM memories WHERE {where_sql} ORDER BY id DESC LIMIT ?;",
            params,
        ).fetchall()
        return [self._from_row(r) for r in rows]

    def count_active(self, project_id: int | None = None) -> int:
        if project_id is not None:
            row = self.db.connection.execute(
                "SELECT COUNT(*) FROM memories WHERE deleted_at IS NULL AND status = 'active' "
                "AND (scope = 'global' OR (scope = 'project' AND project_id = ?));",
                (project_id,),
            ).fetchone()
        else:
            row = self.db.connection.execute(
                "SELECT COUNT(*) FROM memories WHERE deleted_at IS NULL AND status = 'active';"
            ).fetchone()
        return int(row[0]) if row else 0

    def record_usage(
        self,
        scored_memories: list[tuple[int, float]],
        message_id: int | None = None,
    ) -> None:
        if not scored_memories:
            return
        now = utc_now_iso()
        with self.db.connection:
            for mem_id, score in scored_memories:
                self.db.connection.execute(
                    "UPDATE memories SET access_count = access_count + 1, "
                    "last_accessed_at = ? WHERE id = ?;",
                    (now, mem_id),
                )
                if message_id is not None:
                    self.db.connection.execute(
                        "INSERT INTO memory_usage(memory_id, message_id, score, used_at) "
                        "VALUES (?, ?, ?, ?);",
                        (mem_id, message_id, round(float(score), 4), now),
                    )

    def export_all_active(self) -> list[dict[str, Any]]:
        rows = self.db.connection.execute(
            "SELECT * FROM memories WHERE deleted_at IS NULL AND status = 'active' ORDER BY id ASC;"
        ).fetchall()
        return [asdict(self._from_row(r)) for r in rows]
