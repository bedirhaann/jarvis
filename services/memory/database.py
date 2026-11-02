"""JARVIS Stage 8 — Thread-Confined SQLite Connection & Migration Manager."""

from __future__ import annotations

import os
import shutil
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from core.paths import (
    enforce_db_file_permissions,
    is_inside_project_repo,
    resolve_db_path,
)
from services.memory.schema import MIGRATIONS


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class MemoryDatabase:
    """Thread-confined SQLite database manager with automatic WAL configuration,
    chmod 700/600 permission enforcement, and forward-only schema migrations."""

    def __init__(
        self,
        db_path: str | Path | None = None,
        check_same_thread: bool = True,
    ):
        self.db_path: Path = resolve_db_path(db_path)
        self.is_memory_db: bool = str(self.db_path) == ":memory:"
        self.check_same_thread = check_same_thread
        self.owner_thread_ident: int | None = None
        self._conn: sqlite3.Connection | None = None

    @property
    def connection(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("MemoryDatabase connection is not open.")
        if self.check_same_thread and self.owner_thread_ident != threading.get_ident():
            raise RuntimeError(
                f"SQLite connection accessed from thread {threading.get_ident()}, "
                f"but owned by thread {self.owner_thread_ident}."
            )
        return self._conn

    def is_open(self) -> bool:
        return self._conn is not None

    def connect(self) -> sqlite3.Connection:
        """Open the SQLite connection on the calling thread, enforce 0700/0600
        permissions, configure PRAGMAs, and apply pending migrations."""
        if self._conn is not None:
            return self.connection

        if not self.is_memory_db:
            if is_inside_project_repo(self.db_path):
                raise PermissionError(
                    f"Veritabanı proje dizini içinde oluşturulamaz: {self.db_path}"
                )
            parent = self.db_path.parent
            old_umask = os.umask(0o077)
            try:
                parent.mkdir(parents=True, exist_ok=True)
            finally:
                os.umask(old_umask)
            os.chmod(parent, 0o700)

        old_umask = os.umask(0o077)
        try:
            conn = sqlite3.connect(
                ":memory:" if self.is_memory_db else str(self.db_path),
                timeout=3.0,
                check_same_thread=self.check_same_thread,
            )
        finally:
            os.umask(old_umask)

        conn.row_factory = sqlite3.Row
        self.owner_thread_ident = threading.get_ident()
        self._conn = conn

        try:
            conn.execute("PRAGMA foreign_keys = ON;")
            conn.execute("PRAGMA busy_timeout = 3000;")
            if not self.is_memory_db:
                conn.execute("PRAGMA journal_mode = WAL;")
                conn.execute("PRAGMA synchronous = NORMAL;")
                enforce_db_file_permissions(self.db_path)

            self._apply_migrations(conn)

            if not self.is_memory_db:
                enforce_db_file_permissions(self.db_path)
        except Exception:
            self.close()
            raise

        return conn

    def _apply_migrations(self, conn: sqlite3.Connection) -> None:
        current_version = int(conn.execute("PRAGMA user_version;").fetchone()[0])
        pending = [m for m in MIGRATIONS if m[0] > current_version]
        if not pending:
            return

        # Take a safe .bak copy before upgrading an existing non-empty file DB
        if (
            not self.is_memory_db
            and current_version > 0
            and self.db_path.is_file()
            and self.db_path.stat().st_size > 0
        ):
            bak_path = self.db_path.with_suffix(f".v{current_version}.bak")
            shutil.copy2(self.db_path, bak_path)
            os.chmod(bak_path, 0o600)

        for version, description, sql_script in pending:
            conn.executescript(sql_script)
            with conn:
                conn.execute(
                    "INSERT OR REPLACE INTO schema_migrations(version, applied_at, description) "
                    "VALUES (?, ?, ?);",
                    (version, utc_now_iso(), description),
                )
                conn.execute(f"PRAGMA user_version = {int(version)};")

    def user_version(self) -> int:
        row = self.connection.execute("PRAGMA user_version;").fetchone()
        return int(row[0]) if row else 0

    def schema_version(self) -> int:
        return self.user_version()

    def close(self) -> None:
        conn = self._conn
        self._conn = None
        if conn is None:
            return
        try:
            if not self.is_memory_db:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        except sqlite3.Error:
            pass
        try:
            conn.close()
        except sqlite3.Error:
            pass
        if not self.is_memory_db and self.db_path.exists():
            try:
                enforce_db_file_permissions(self.db_path)
            except OSError:
                pass
