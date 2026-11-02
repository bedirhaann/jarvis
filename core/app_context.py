"""JARVIS Stage 8 — Optional Application Context & Service Container."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from core.paths import resolve_db_path

if TYPE_CHECKING:
    from services.memory.service import MemoryService


@dataclass
class AppContext:
    """Lightweight dependency injection container passed optionally to JarvisOrb,
    ChatWindow, and AntigravityWorker."""

    db_path: Path | None = None
    memory_enabled: bool = True
    memory_service_factory: Callable[[], MemoryService] | None = None

    @classmethod
    def create_default(
        cls,
        db_path: str | Path | None = None,
        environ: dict[str, str] | None = None,
    ) -> AppContext:
        env = os.environ if environ is None else environ
        enabled_flag = env.get("JARVIS_MEMORY", "1").strip().lower()
        memory_enabled = enabled_flag not in ("0", "false", "no", "off")
        resolved_db = resolve_db_path(db_path=db_path, environ=env)

        def _factory() -> MemoryService:
            from services.memory.service import MemoryService

            return MemoryService(db_path=resolved_db, enabled=memory_enabled)

        return cls(
            db_path=resolved_db,
            memory_enabled=memory_enabled,
            memory_service_factory=_factory if memory_enabled else None,
        )
