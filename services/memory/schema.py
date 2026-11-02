"""JARVIS Stage 8 — SQLite Schema & Forward Migrations (v1)."""

from __future__ import annotations

SCHEMA_V1_DESCRIPTION = (
    "Stage 8 Persistent Memory v1: projects, conversations, agent_sessions, "
    "messages, memories, memory_tags, memory_usage, settings, and FTS5 indexes."
)

SCHEMA_V1_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT NOT NULL,
    description TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    id          INTEGER PRIMARY KEY,
    slug        TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    root_path   TEXT UNIQUE,
    description TEXT,
    status      TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'archived')),
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conversations (
    id                      INTEGER PRIMARY KEY,
    uuid                    TEXT NOT NULL UNIQUE,
    project_id              INTEGER REFERENCES projects(id) ON DELETE SET NULL,
    title                   TEXT,
    summary                 TEXT,
    summary_upto_message_id INTEGER,
    started_at              TEXT NOT NULL,
    last_activity_at        TEXT NOT NULL,
    archived                INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS agent_sessions (
    id                      INTEGER PRIMARY KEY,
    conversation_id         INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    backend                 TEXT NOT NULL DEFAULT 'antigravity',
    backend_conversation_id TEXT,
    model                   TEXT NOT NULL,
    mode                    TEXT,
    started_at              TEXT NOT NULL,
    ended_at                TEXT,
    end_reason              TEXT CHECK (end_reason IN ('normal', 'timeout', 'crash', 'shutdown', 'reset'))
);

CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    session_id      INTEGER REFERENCES agent_sessions(id) ON DELETE SET NULL,
    role            TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system', 'error')),
    source          TEXT NOT NULL DEFAULT 'llm',
    content         TEXT NOT NULL,
    search_text     TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'complete'
                    CHECK (status IN ('complete', 'partial', 'cancelled', 'error')),
    latency_ms      INTEGER,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id, id);

CREATE TABLE IF NOT EXISTS memories (
    id                INTEGER PRIMARY KEY,
    scope             TEXT NOT NULL CHECK (scope IN ('global', 'project', 'conversation')),
    project_id        INTEGER REFERENCES projects(id) ON DELETE CASCADE,
    conversation_id   INTEGER REFERENCES conversations(id) ON DELETE CASCADE,
    kind              TEXT NOT NULL
                      CHECK (kind IN ('fact', 'preference', 'decision', 'instruction', 'task', 'note')),
    content           TEXT NOT NULL,
    search_text       TEXT NOT NULL,
    content_hash      TEXT NOT NULL,
    importance        INTEGER NOT NULL DEFAULT 3 CHECK (importance BETWEEN 1 AND 5),
    status            TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'pending', 'rejected')),
    origin            TEXT NOT NULL CHECK (origin IN ('explicit', 'extracted', 'imported', 'system')),
    source_message_id INTEGER REFERENCES messages(id) ON DELETE SET NULL,
    pinned            INTEGER NOT NULL DEFAULT 0,
    superseded_by     INTEGER REFERENCES memories(id) ON DELETE SET NULL,
    access_count      INTEGER NOT NULL DEFAULT 0,
    last_accessed_at  TEXT,
    expires_at        TEXT,
    deleted_at        TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    CHECK (
        (scope = 'global'       AND project_id IS NULL AND conversation_id IS NULL)
        OR (scope = 'project'   AND project_id IS NOT NULL)
        OR (scope = 'conversation' AND conversation_id IS NOT NULL)
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_memory_dedupe
    ON memories(scope, IFNULL(project_id, 0), IFNULL(conversation_id, 0), content_hash)
    WHERE deleted_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_memories_scope
    ON memories(scope, project_id, status)
    WHERE deleted_at IS NULL;

CREATE TABLE IF NOT EXISTS memory_tags (
    memory_id INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    tag       TEXT NOT NULL,
    PRIMARY KEY (memory_id, tag)
);

CREATE TABLE IF NOT EXISTS memory_usage (
    id         INTEGER PRIMARY KEY,
    memory_id  INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    score      REAL NOT NULL,
    used_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- External-content FTS5 virtual tables over Turkish-normalized search_text
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    search_text,
    content='memories',
    content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);

CREATE TRIGGER IF NOT EXISTS trg_memories_fts_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, search_text) VALUES (new.id, new.search_text);
END;

CREATE TRIGGER IF NOT EXISTS trg_memories_fts_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, search_text)
    VALUES ('delete', old.id, old.search_text);
END;

CREATE TRIGGER IF NOT EXISTS trg_memories_fts_au AFTER UPDATE OF search_text ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, search_text)
    VALUES ('delete', old.id, old.search_text);
    INSERT INTO memories_fts(rowid, search_text) VALUES (new.id, new.search_text);
END;

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    search_text,
    content='messages',
    content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);

CREATE TRIGGER IF NOT EXISTS trg_messages_fts_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, search_text) VALUES (new.id, new.search_text);
END;

CREATE TRIGGER IF NOT EXISTS trg_messages_fts_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, search_text)
    VALUES ('delete', old.id, old.search_text);
END;

CREATE TRIGGER IF NOT EXISTS trg_messages_fts_au AFTER UPDATE OF search_text ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, search_text)
    VALUES ('delete', old.id, old.search_text);
    INSERT INTO messages_fts(rowid, search_text) VALUES (new.id, new.search_text);
END;
"""

LATEST_SCHEMA_VERSION = 1

MIGRATIONS: list[tuple[int, str, str]] = [
    (1, SCHEMA_V1_DESCRIPTION, SCHEMA_V1_SQL),
]
