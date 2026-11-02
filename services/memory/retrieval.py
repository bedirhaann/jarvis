from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

from services.memory.database import MemoryDatabase
from services.memory.repositories import MemoryRecord, MemoryRepository, utc_now_iso
from services.memory.text import build_fts_query, normalize_tr


@dataclass(frozen=True)
class ScoredMemory:
    record: MemoryRecord
    score: float
    bm25_norm: float = 0.0


def _age_days(created_at_iso: str, now_dt: datetime | None = None) -> float:
    if now_dt is None:
        now_dt = datetime.now(timezone.utc)
    try:
        dt = datetime.fromisoformat(created_at_iso.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = (now_dt - dt).total_seconds() / 86400.0
        return max(0.0, delta)
    except Exception:
        return 0.0


class MemoryRetriever:
    """Retrieves and ranks active memories strictly within the allowed scope boundary."""

    def __init__(self, db: MemoryDatabase):
        self.db = db
        self.repo = MemoryRepository(db)

    def _scope_sql_and_params(
        self,
        project_id: int | None,
        conversation_id: int | None,
        table_alias: str = "m",
    ) -> tuple[str, list[object]]:
        prefix = f"{table_alias}." if table_alias else ""
        parts = [f"{prefix}scope = 'global'"]
        params: list[object] = []
        if project_id is not None:
            parts.append(f"({prefix}scope = 'project' AND {prefix}project_id = ?)")
            params.append(int(project_id))
        if conversation_id is not None:
            parts.append(
                f"({prefix}scope = 'conversation' AND {prefix}conversation_id = ?)"
            )
            params.append(int(conversation_id))
        return "(" + " OR ".join(parts) + ")", params

    @staticmethod
    def _compute_score(
        record: MemoryRecord,
        bm25_norm: float,
        active_project_id: int | None,
        current_conversation_id: int | None,
        now_dt: datetime,
    ) -> float:
        imp_norm = max(0.0, min(1.0, record.importance / 5.0))
        age = _age_days(record.created_at, now_dt)
        recency = math.exp(-age / 30.0)

        if (
            record.scope == "project"
            and active_project_id is not None
            and record.project_id == active_project_id
        ):
            scope_bonus = 1.0
        elif (
            record.scope == "conversation"
            and current_conversation_id is not None
            and record.conversation_id == current_conversation_id
        ):
            scope_bonus = 0.95
        else:
            scope_bonus = 0.70

        pin_boost = 0.25 if record.pinned else 0.0
        score = (
            0.55 * bm25_norm
            + 0.20 * imp_norm
            + 0.15 * recency
            + 0.10 * scope_bonus
            + pin_boost
        )
        return round(score, 6)

    def retrieve(
        self,
        query: str,
        project_id: int | None = None,
        conversation_id: int | None = None,
        exclude_ids: Iterable[int] | None = None,
        include_pinned: bool = True,
        max_items: int = 8,
        max_chars: int = 1500,
    ) -> list[ScoredMemory]:
        """Search and rank relevant active memories for a user query or command."""
        excluded = set(int(x) for x in (exclude_ids or ()))
        now_iso = utc_now_iso()
        now_dt = datetime.now(timezone.utc)

        scope_sql, scope_params = self._scope_sql_and_params(
            project_id=project_id,
            conversation_id=conversation_id,
            table_alias="m",
        )

        candidates: dict[int, tuple[MemoryRecord, float]] = {}

        # 1. Include pinned memories in scope if requested
        if include_pinned:
            pin_rows = self.db.connection.execute(
                f"SELECT m.* FROM memories m "
                f"WHERE m.deleted_at IS NULL AND m.status = 'active' "
                f"AND (m.expires_at IS NULL OR m.expires_at > ?) "
                f"AND m.pinned = 1 AND {scope_sql} "
                f"ORDER BY m.importance DESC, m.id DESC LIMIT ?;",
                [now_iso, *scope_params, max(8, max_items)],
            ).fetchall()
            for r in pin_rows:
                rec = self.repo._from_row(r)
                if rec.id not in excluded:
                    candidates[rec.id] = (rec, 0.75)

        # 2. FTS5 search if query produces valid tokens
        fts_query = build_fts_query(query)
        if fts_query:
            fts_rows = self.db.connection.execute(
                f"SELECT m.*, bm25(memories_fts, 2.0, 1.0) AS raw_bm25 "
                f"FROM memories_fts "
                f"JOIN memories m ON m.id = memories_fts.rowid "
                f"WHERE memories_fts MATCH ? "
                f"AND m.deleted_at IS NULL AND m.status = 'active' "
                f"AND (m.expires_at IS NULL OR m.expires_at > ?) "
                f"AND {scope_sql} "
                f"ORDER BY raw_bm25 ASC LIMIT ?;",
                [fts_query, now_iso, *scope_params, max(24, max_items * 3)],
            ).fetchall()

            if fts_rows:
                # SQLite FTS5 bm25() returns negative numbers (more negative = better match)
                raw_scores = [abs(float(r["raw_bm25"])) for r in fts_rows]
                max_raw = max(raw_scores) if raw_scores else 1.0
                if max_raw <= 0.0:
                    max_raw = 1.0
                for r, raw_val in zip(fts_rows, raw_scores):
                    rec = self.repo._from_row(r)
                    if rec.id in excluded:
                        continue
                    bm25_norm = min(1.0, max(0.15, raw_val / max_raw))
                    prev = candidates.get(rec.id)
                    if prev is None or bm25_norm > prev[1]:
                        candidates[rec.id] = (rec, bm25_norm)

        # 3. Fallback if query has short tokens not caught by FTS
        if not candidates and query.strip():
            norm_q = normalize_tr(query)
            if norm_q:
                like_rows = self.db.connection.execute(
                    f"SELECT m.* FROM memories m "
                    f"WHERE m.deleted_at IS NULL AND m.status = 'active' "
                    f"AND (m.expires_at IS NULL OR m.expires_at > ?) "
                    f"AND m.search_text LIKE ? AND {scope_sql} "
                    f"ORDER BY m.pinned DESC, m.importance DESC, m.id DESC LIMIT ?;",
                    [now_iso, f"%{norm_q}%", *scope_params, max_items],
                ).fetchall()
                for r in like_rows:
                    rec = self.repo._from_row(r)
                    if rec.id not in excluded:
                        candidates[rec.id] = (rec, 0.60)

        scored: list[ScoredMemory] = []
        for rec, bm25_norm in candidates.values():
            final_score = self._compute_score(
                record=rec,
                bm25_norm=bm25_norm,
                active_project_id=project_id,
                current_conversation_id=conversation_id,
                now_dt=now_dt,
            )
            scored.append(ScoredMemory(record=rec, score=final_score, bm25_norm=bm25_norm))

        scored.sort(key=lambda item: (item.score, item.record.importance, item.record.id), reverse=True)
        return self._apply_budget(scored, max_items=max_items, max_chars=max_chars)

    def retrieve_for_bootstrap(
        self,
        query: str = "",
        project_id: int | None = None,
        conversation_id: int | None = None,
        max_items: int = 8,
        max_chars: int = 1500,
    ) -> list[ScoredMemory]:
        """Retrieve high-priority global + active project + conversation memories for a new session."""
        now_iso = utc_now_iso()
        now_dt = datetime.now(timezone.utc)
        scope_sql, scope_params = self._scope_sql_and_params(
            project_id=project_id,
            conversation_id=conversation_id,
            table_alias="m",
        )

        # 1. Start with query-matched + pinned memories
        matched = {
            sm.record.id: sm
            for sm in self.retrieve(
                query=query,
                project_id=project_id,
                conversation_id=conversation_id,
                include_pinned=True,
                max_items=max_items,
                max_chars=max_chars,
            )
        }

        # 2. Fill remaining slots with top importance/recent active memories in scope
        rows = self.db.connection.execute(
            f"SELECT m.* FROM memories m "
            f"WHERE m.deleted_at IS NULL AND m.status = 'active' "
            f"AND (m.expires_at IS NULL OR m.expires_at > ?) "
            f"AND {scope_sql} "
            f"ORDER BY m.pinned DESC, m.importance DESC, m.id DESC LIMIT ?;",
            [now_iso, *scope_params, max(16, max_items * 2)],
        ).fetchall()

        for r in rows:
            rec = self.repo._from_row(r)
            if rec.id not in matched:
                score = self._compute_score(
                    record=rec,
                    bm25_norm=0.40,
                    active_project_id=project_id,
                    current_conversation_id=conversation_id,
                    now_dt=now_dt,
                )
                matched[rec.id] = ScoredMemory(record=rec, score=score, bm25_norm=0.40)

        all_items = sorted(
            matched.values(),
            key=lambda item: (item.record.pinned, item.score, item.record.importance, item.record.id),
            reverse=True,
        )
        return self._apply_budget(all_items, max_items=max_items, max_chars=max_chars)

    @staticmethod
    def _apply_budget(
        items: list[ScoredMemory],
        max_items: int,
        max_chars: int,
    ) -> list[ScoredMemory]:
        selected: list[ScoredMemory] = []
        used_chars = 0
        for item in items:
            if len(selected) >= max_items:
                break
            content_len = len(item.record.content) + 16
            if selected and (used_chars + content_len > max_chars):
                continue
            selected.append(item)
            used_chars += content_len
        return selected
