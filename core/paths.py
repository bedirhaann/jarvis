"""JARVIS Stage 8 — Secure XDG Data & Database Path Resolution.

Enforces:
- Default database path: ~/.local/share/jarvis/jarvis.db
  (or $JARVIS_DATA_DIR / $XDG_DATA_HOME/jarvis)
- Directory permissions: 0700 (rwx------)
- Database & WAL/SHM file permissions: 0600 (rw-------)
- Repository isolation: refuses to place the runtime database inside the
  JARVIS git repository tree.
"""

from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_FILENAME = "jarvis.db"


def is_inside_project_repo(candidate: Path) -> bool:
    """Return True if candidate resolves inside the JARVIS project repository."""
    try:
        resolved = candidate.resolve()
        repo = PROJECT_ROOT.resolve()
        return resolved == repo or repo in resolved.parents
    except OSError:
        return False


def resolve_data_dir(
    override_dir: str | Path | None = None,
    environ: dict[str, str] | None = None,
) -> Path:
    """Resolve the JARVIS user data directory outside the git repository.

    Priority:
    1. Explicit override_dir argument
    2. $JARVIS_DATA_DIR environment variable
    3. $XDG_DATA_HOME/jarvis
    4. ~/.local/share/jarvis
    """
    env = os.environ if environ is None else environ
    raw: str | Path | None = override_dir or env.get("JARVIS_DATA_DIR")
    if raw:
        target = Path(raw).expanduser().resolve()
    else:
        xdg_data = env.get("XDG_DATA_HOME")
        if xdg_data and xdg_data.strip():
            base = Path(xdg_data).expanduser()
        else:
            home_dir = Path(env["HOME"]).expanduser() if env.get("HOME") else Path.home()
            base = home_dir / ".local" / "share"
        target = (base / "jarvis").resolve()

    if is_inside_project_repo(target):
        raise PermissionError(
            f"Güvenlik kuralı ihlali: Veritabanı dizini proje deposu içinde olamaz ({target})."
        )
    return target


def ensure_secure_data_dir(
    override_dir: str | Path | None = None,
    environ: dict[str, str] | None = None,
) -> Path:
    """Create the JARVIS data directory if needed and enforce chmod 700."""
    data_dir = resolve_data_dir(override_dir=override_dir, environ=environ)
    old_umask = os.umask(0o077)
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
    finally:
        os.umask(old_umask)
    os.chmod(data_dir, 0o700)
    return data_dir


def resolve_db_path(
    db_path: str | Path | None = None,
    environ: dict[str, str] | None = None,
) -> Path:
    """Resolve the SQLite database path and verify it is outside the project repo."""
    if db_path is not None:
        raw_str = str(db_path)
        if raw_str == ":memory:":
            return Path(":memory:")
        target = Path(db_path).expanduser().resolve()
        if is_inside_project_repo(target):
            raise PermissionError(
                f"Güvenlik kuralı ihlali: Veritabanı dosyası proje deposu içinde olamaz ({target})."
            )
        return target

    data_dir = resolve_data_dir(environ=environ)
    return data_dir / DEFAULT_DB_FILENAME


def enforce_db_file_permissions(db_path: str | Path) -> None:
    """Enforce chmod 700 on parent directory and chmod 600 on DB, WAL, and SHM files."""
    if str(db_path) == ":memory:":
        return
    path = Path(db_path)
    if is_inside_project_repo(path):
        raise PermissionError(
            f"Güvenlik kuralı ihlali: Veritabanı dosyası proje deposu içinde olamaz ({path})."
        )
    parent = path.parent
    if parent.exists():
        os.chmod(parent, 0o700)

    for suffix in ("", "-wal", "-shm", "-journal", ".bak"):
        candidate = Path(f"{path}{suffix}")
        if candidate.exists() and candidate.is_file():
            os.chmod(candidate, 0o600)


def ensure_secure_file(file_path: str | Path) -> Path:
    """Enforce chmod 700 on parent directory and chmod 600 on the given file."""
    target = Path(file_path).expanduser().resolve()
    if is_inside_project_repo(target):
        raise PermissionError(
            f"Güvenlik kuralı ihlali: Veri dosyası proje deposu içinde olamaz ({target})."
        )
    if target.parent.exists():
        os.chmod(target.parent, 0o700)
    if target.exists() and target.is_file():
        os.chmod(target, 0o600)
    return target


def get_default_db_path() -> Path:
    """Return the default SQLite database path (~/.local/share/jarvis/jarvis.db)."""
    return resolve_db_path()
