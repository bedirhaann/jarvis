from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.paths import ensure_secure_file, get_default_db_path
from services.memory.commands import (
    execute_local_memory_command,
    is_local_memory_command,
)
from services.memory.context_builder import ContextBuilder
from services.memory.database import MemoryDatabase
from services.memory.repositories import (
    ConversationRecord,
    ConversationRepository,
    MemoryRecord,
    MemoryRepository,
    MessageRecord,
    MessageRepository,
    ProjectRecord,
    ProjectRepository,
    SessionRepository,
    SettingsRepository,
)
from services.memory.retrieval import MemoryRetriever, ScoredMemory
from services.memory.text import MemorySecretError, extract_memory_suggestions


class MemoryService:
    """Thread-confined facade coordinating SQLite persistence, retrieval,
    user-confirmed suggestions, local slash commands, and session bootstrap."""

    def __init__(self, db_path: Path | None = None, enabled: bool = True):
        self.db_path = Path(db_path) if db_path is not None else get_default_db_path()
        self.enabled = enabled
        self.db = MemoryDatabase(self.db_path)

        self.settings: SettingsRepository | None = None
        self.projects: ProjectRepository | None = None
        self.conversations: ConversationRepository | None = None
        self.sessions: SessionRepository | None = None
        self.messages: MessageRepository | None = None
        self.memories: MemoryRepository | None = None
        self.retriever: MemoryRetriever | None = None
        self.context_builder = ContextBuilder()

        self.active_project_id: int | None = None
        self.current_conversation_id: int | None = None
        self.current_session_id: int | None = None

        self._injected_memory_ids: set[int] = set()
        self._needs_session_bootstrap: bool = True
        self._initialized: bool = False

    @property
    def is_initialized(self) -> bool:
        return self._initialized

    def initialize(self) -> None:
        """Open the SQLite connection on the owning worker thread and prepare state."""
        if self._initialized:
            return
        self.db.connect()
        self.settings = SettingsRepository(self.db)
        self.projects = ProjectRepository(self.db)
        self.conversations = ConversationRepository(self.db)
        self.sessions = SessionRepository(self.db)
        self.messages = MessageRepository(self.db)
        self.memories = MemoryRepository(self.db)
        self.retriever = MemoryRetriever(self.db)

        saved_proj_id = self.settings.get_int("active_project_id", default=None)
        if saved_proj_id is not None and self.projects.get_by_id(saved_proj_id) is not None:
            self.active_project_id = saved_proj_id
        else:
            self.active_project_id = None

        conv = self.conversations.create_conversation(project_id=self.active_project_id)
        self.current_conversation_id = conv.id
        self.current_session_id = None
        self._injected_memory_ids.clear()
        self._needs_session_bootstrap = True
        self._initialized = True

    def close(self) -> None:
        if not self._initialized:
            return
        try:
            if self.current_session_id is not None and self.sessions is not None:
                self.sessions.end_session(self.current_session_id, end_reason="shutdown")
                self.current_session_id = None
            if self.current_conversation_id is not None and self.conversations is not None:
                self.conversations.end_conversation(self.current_conversation_id)
        except Exception:
            pass
        finally:
            self.db.close()
            self._initialized = False

    def _require_init(self) -> None:
        if not self._initialized:
            self.initialize()

    # --- Session & Conversation Lifecycle ---

    def on_session_started(
        self,
        backend_conv_id: str | None = None,
        model: str | None = None,
        mode: str | None = None,
    ) -> int:
        self._require_init()
        assert self.sessions is not None
        assert self.current_conversation_id is not None

        if self.current_session_id is not None:
            self.sessions.end_session(self.current_session_id, end_reason="restarted")

        sess = self.sessions.start_session(
            conversation_id=self.current_conversation_id,
            backend_conv_id=backend_conv_id,
            model=model,
            mode=mode,
        )
        self.current_session_id = sess.id
        self._injected_memory_ids.clear()
        self._needs_session_bootstrap = True
        return sess.id

    def update_session_backend_id(self, backend_conv_id: str | None) -> None:
        if not self._initialized or not backend_conv_id or self.current_session_id is None:
            return
        assert self.sessions is not None
        self.sessions.update_backend_conv_id(self.current_session_id, backend_conv_id)

    def on_session_ended(self, end_reason: str = "closed") -> None:
        if not self._initialized:
            return
        if self.current_session_id is not None and self.sessions is not None:
            self.sessions.end_session(self.current_session_id, end_reason=end_reason)
            self.current_session_id = None
        self._injected_memory_ids.clear()
        self._needs_session_bootstrap = True

    def start_new_conversation(self) -> ConversationRecord:
        """Called when the user clicks 'Temizle'.
        1. Ends the current agent_session with end_reason='reset'.
        2. Ends the previous conversation record.
        3. Creates a fresh conversation record.
        4. Clears injected memory tracking so global & project memories bootstrap into
           the new session while previous conversation messages are NOT carried over."""
        self._require_init()
        assert self.conversations is not None
        assert self.sessions is not None

        if self.current_session_id is not None:
            self.sessions.end_session(self.current_session_id, end_reason="reset")
            self.current_session_id = None

        if self.current_conversation_id is not None:
            self.conversations.end_conversation(self.current_conversation_id)

        new_conv = self.conversations.create_conversation(
            project_id=self.active_project_id
        )
        self.current_conversation_id = new_conv.id
        self._injected_memory_ids.clear()
        self._needs_session_bootstrap = True
        return new_conv

    # --- Local Command Handling ---

    @staticmethod
    def is_memory_command(message: str) -> bool:
        return is_local_memory_command(message)

    def handle_command(self, message: str) -> tuple[bool, str]:
        self._require_init()
        assert self.messages is not None
        assert self.current_conversation_id is not None

        handled, response = execute_local_memory_command(self, message)
        if not handled:
            return False, ""

        self.messages.add_message(
            conversation_id=self.current_conversation_id,
            role="user",
            content=message,
            source="local:command",
            session_id=self.current_session_id,
            status="complete",
        )
        self.messages.add_message(
            conversation_id=self.current_conversation_id,
            role="assistant",
            content=response,
            source="local:command",
            session_id=self.current_session_id,
            status="complete",
        )
        return True, response

    # --- Turn Recording & Prompt Augmentation ---

    def record_local_turn(
        self,
        user_message: str,
        assistant_response: str,
        source: str,
        latency_ms: int | None = None,
    ) -> None:
        """Persist a local (weather/system) user + assistant turn."""
        self._require_init()
        assert self.messages is not None
        assert self.conversations is not None
        assert self.current_conversation_id is not None

        self.conversations.update_title_if_empty(self.current_conversation_id, user_message)
        self.messages.add_message(
            conversation_id=self.current_conversation_id,
            role="user",
            content=user_message,
            source=source,
            session_id=self.current_session_id,
            status="complete",
        )
        self.messages.add_message(
            conversation_id=self.current_conversation_id,
            role="assistant",
            content=assistant_response,
            source=source,
            session_id=self.current_session_id,
            status="complete",
            latency_ms=latency_ms,
        )

    def prepare_user_turn(
        self,
        user_message: str,
    ) -> tuple[MessageRecord, str, list[MemoryRecord]]:
        """Persist the user message, extract candidate pending memory suggestions,
        and build the memory-augmented prompt for AntigravityClient."""
        self._require_init()
        assert self.messages is not None
        assert self.conversations is not None
        assert self.memories is not None
        assert self.retriever is not None
        assert self.current_conversation_id is not None

        # 1. Fetch existing messages in the current conversation BEFORE adding the new message
        #    (Used only if bootstrapping after a mid-conversation session restart/crash)
        prior_messages: list[MessageRecord] = []
        if self._needs_session_bootstrap:
            prior_messages = self.messages.list_for_conversation(
                self.current_conversation_id, limit=6
            )

        # 2. Persist user message
        self.conversations.update_title_if_empty(self.current_conversation_id, user_message)
        user_msg = self.messages.add_message(
            conversation_id=self.current_conversation_id,
            role="user",
            content=user_message,
            source="llm",
            session_id=self.current_session_id,
            status="complete",
        )

        # 3. Retrieve active memories for prompt injection (before extracting new pending ones,
        #    though pending ones are never retrieved anyway)
        active_project = self.get_active_project()
        if self._needs_session_bootstrap:
            scored_memories = self.retriever.retrieve_for_bootstrap(
                query=user_message,
                project_id=self.active_project_id,
                conversation_id=self.current_conversation_id,
            )
            augmented_prompt = self.context_builder.build_prompt(
                user_message=user_message,
                memories=scored_memories,
                active_project=active_project,
                recent_messages=prior_messages,
                is_bootstrap=True,
            )
            self._needs_session_bootstrap = False
        else:
            scored_memories = self.retriever.retrieve(
                query=user_message,
                project_id=self.active_project_id,
                conversation_id=self.current_conversation_id,
                exclude_ids=self._injected_memory_ids,
                include_pinned=False,
            )
            augmented_prompt = self.context_builder.build_prompt(
                user_message=user_message,
                memories=scored_memories,
                active_project=active_project,
                recent_messages=None,
                is_bootstrap=False,
            )

        if scored_memories:
            self.memories.record_usage(
                [(sm.record.id, sm.score) for sm in scored_memories],
                message_id=user_msg.id,
            )
            for sm in scored_memories:
                self._injected_memory_ids.add(sm.record.id)

        # 4. Extract user-confirmed memory suggestions (always saved as status='pending')
        pending_suggestions: list[MemoryRecord] = []
        candidates = extract_memory_suggestions(
            user_message,
            has_active_project=(self.active_project_id is not None),
        )
        for target_scope, kind, cand_text, importance in candidates:
            try:
                rec, created = self.memories.add_memory(
                    content=cand_text,
                    scope=target_scope,
                    kind=kind,
                    project_id=self.active_project_id if target_scope == "project" else None,
                    importance=importance,
                    status="pending",
                    origin="extracted",
                    source_message_id=user_msg.id,
                )
                if created and rec.status == "pending":
                    pending_suggestions.append(rec)
            except (MemorySecretError, ValueError):
                continue

        return user_msg, augmented_prompt, pending_suggestions

    def record_assistant_turn(
        self,
        content: str,
        source: str = "llm",
        status: str = "complete",
        latency_ms: int | None = None,
    ) -> MessageRecord:
        self._require_init()
        assert self.messages is not None
        assert self.current_conversation_id is not None

        return self.messages.add_message(
            conversation_id=self.current_conversation_id,
            role="assistant",
            content=content,
            source=source,
            session_id=self.current_session_id,
            status=status,
            latency_ms=latency_ms,
        )

    @staticmethod
    def format_pending_suggestions_notice(suggestions: list[MemoryRecord]) -> str:
        if not suggestions:
            return ""
        lines = ["\n\n---", "💡 **Hafıza Önerisi *(Onayınız Bekleniyor)*:**"]
        for rec in suggestions:
            lines.append(
                f"- **`#{rec.id}`** [{rec.scope}] *{rec.content}*\n"
                f"  Onaylamak için: `/hafıza onayla {rec.id}` · "
                f"Düzenlemek için: `/hafıza düzenle {rec.id} <metin>` · "
                f"Reddetmek için: `/hafıza reddet {rec.id}`"
            )
        return "\n".join(lines)

    # --- Memory & Project Operations Used by Commands ---

    def add_explicit_memory(
        self,
        content: str,
        scope: str = "global",
        kind: str = "fact",
        pinned: bool = False,
        importance: int = 4,
    ) -> tuple[MemoryRecord, bool]:
        self._require_init()
        assert self.memories is not None
        proj_id = self.active_project_id if scope == "project" else None
        conv_id = self.current_conversation_id if scope == "conversation" else None
        return self.memories.add_memory(
            content=content,
            scope=scope,
            kind=kind,
            project_id=proj_id,
            conversation_id=conv_id,
            importance=importance,
            status="active",
            origin="explicit",
            pinned=pinned,
        )

    def delete_memory(self, memory_id: int, hard: bool = False) -> bool:
        self._require_init()
        assert self.memories is not None
        ok = self.memories.delete_memory(memory_id, hard=hard)
        if ok:
            self._injected_memory_ids.discard(memory_id)
        return ok

    def search_memories(self, query: str) -> list[ScoredMemory]:
        self._require_init()
        assert self.retriever is not None
        return self.retriever.retrieve(
            query=query,
            project_id=self.active_project_id,
            conversation_id=self.current_conversation_id,
            include_pinned=False,
        )

    def list_active_memories(self, limit: int = 50) -> list[MemoryRecord]:
        self._require_init()
        assert self.memories is not None
        assert self.retriever is not None
        scope_sql, scope_params = self.retriever._scope_sql_and_params(
            project_id=self.active_project_id,
            conversation_id=self.current_conversation_id,
            table_alias="m",
        )
        rows = self.db.connection.execute(
            f"SELECT m.* FROM memories m "
            f"WHERE m.deleted_at IS NULL AND m.status = 'active' AND {scope_sql} "
            f"ORDER BY m.pinned DESC, m.importance DESC, m.id DESC LIMIT ?;",
            [*scope_params, max(1, int(limit))],
        ).fetchall()
        return [self.memories._from_row(r) for r in rows]

    def list_pending_memories(self, limit: int = 30) -> list[MemoryRecord]:
        self._require_init()
        assert self.memories is not None
        return self.memories.list_pending(
            project_id=self.active_project_id,
            conversation_id=self.current_conversation_id,
            limit=limit,
        )

    def confirm_pending_memory(
        self,
        memory_id: int,
        edited_content: str | None = None,
    ) -> MemoryRecord | None:
        self._require_init()
        assert self.memories is not None
        return self.memories.confirm_pending(memory_id, edited_content=edited_content)

    def edit_memory(self, memory_id: int, new_content: str) -> MemoryRecord | None:
        self._require_init()
        assert self.memories is not None
        rec = self.memories.update_content(memory_id, new_content, new_status="active")
        if rec is not None:
            # Allow updated content to be re-injected if relevant
            self._injected_memory_ids.discard(memory_id)
        return rec

    def reject_pending_memory(self, memory_id: int) -> bool:
        self._require_init()
        assert self.memories is not None
        return self.memories.reject_pending(memory_id)

    def export_memories_to_file(self, target_path: Path | None = None) -> Path:
        self._require_init()
        assert self.memories is not None
        if target_path is None:
            target_path = self.db_path.parent / "memory_export.json"
        data = self.memories.export_all_active()
        target_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        ensure_secure_file(target_path)
        return target_path

    def get_active_project(self) -> ProjectRecord | None:
        self._require_init()
        assert self.projects is not None
        if self.active_project_id is None:
            return None
        return self.projects.get_by_id(self.active_project_id)

    def set_active_project(self, name: str | None) -> ProjectRecord | None:
        self._require_init()
        assert self.projects is not None
        assert self.settings is not None
        assert self.conversations is not None

        if name is None or not name.strip():
            self.active_project_id = None
            self.settings.set("active_project_id", "")
            if self.current_conversation_id is not None:
                self.conversations.set_project(self.current_conversation_id, None)
            self._needs_session_bootstrap = True
            return None

        proj = self.projects.create_or_get(name.strip())
        self.active_project_id = proj.id
        self.settings.set("active_project_id", str(proj.id))
        if self.current_conversation_id is not None:
            self.conversations.set_project(self.current_conversation_id, proj.id)
        self._needs_session_bootstrap = True
        return proj

    def list_projects(self) -> list[ProjectRecord]:
        self._require_init()
        assert self.projects is not None
        return self.projects.list_projects()

    def list_recent_conversations(self, limit: int = 15) -> list[dict[str, Any]]:
        self._require_init()
        assert self.conversations is not None
        return self.conversations.list_recent_with_stats(limit=limit)

    def list_conversation_messages(
        self,
        conversation_id: int,
        limit: int = 20,
    ) -> list[MessageRecord]:
        self._require_init()
        assert self.messages is not None
        return self.messages.list_for_conversation(conversation_id, limit=limit)

    def status_badge_text(self) -> str:
        if not self._initialized or self.memories is None:
            return ""
        count = self.memories.count_active(project_id=self.active_project_id)
        proj = self.get_active_project()
        if proj is not None:
            return f"🧠 {proj.name} ({count})"
        return f"🧠 {count}"
