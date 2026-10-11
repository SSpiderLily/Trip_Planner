"""短期目的地搜索候选 sidecar；不修改预览快照或 store_version。"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import secrets
import sqlite3
import time
from typing import Iterator

from app.models.planning_v4.destination_search import DestinationCategory
from app.models.planning_v4.facts import FactSource, PlaceFact


_SCHEMA = """
CREATE TABLE IF NOT EXISTS destination_search_candidates (
    candidate_token_hash TEXT PRIMARY KEY NOT NULL,
    task_id TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    access_token_hash TEXT NOT NULL,
    search_id TEXT NOT NULL,
    category TEXT NOT NULL CHECK (category IN ('sightseeing','meal')),
    poi_id TEXT NOT NULL,
    place_json TEXT NOT NULL,
    source_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS destination_search_candidates_expiry
    ON destination_search_candidates(expires_at);
CREATE INDEX IF NOT EXISTS destination_search_candidates_owner
    ON destination_search_candidates(plan_id, task_id, search_id);
"""


class DestinationCandidateNotFound(ValueError):
    """候选不存在、过期或不属于当前计划。"""


@dataclass(frozen=True, slots=True)
class StoredDestinationCandidate:
    task_id: str
    plan_id: str
    search_id: str
    category: DestinationCategory
    place: PlaceFact
    sources: tuple[FactSource, ...]
    expires_at: datetime


class DestinationCandidateRepository:
    """将搜索结果按随机 token 哈希保存，并绑定 task、plan 和访问标识。"""

    def __init__(self, path: str | Path, *, ttl_seconds: int = 1800, timeout_seconds: float = 5.0):
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) or ttl_seconds < 60:
            raise ValueError("ttl_seconds 必须至少为60秒")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds 必须大于0")
        self.path = Path(path)
        self.ttl_seconds = ttl_seconds
        self.timeout_seconds = float(timeout_seconds)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=self.timeout_seconds, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(f"PRAGMA busy_timeout={int(self.timeout_seconds * 1000)}")
        return conn

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect()
        try:
            conn.executescript(_SCHEMA)
        finally:
            conn.close()

    @classmethod
    def validate_owner_binding(cls, *, task_id: str, plan_id: str, access_token: str) -> None:
        """在外部查询前验证并规范 owner 参数，避免无效请求触发 MCP。"""
        cls._validate_identity(task_id, "task_id")
        cls._validate_identity(plan_id, "plan_id")
        _token_hash(access_token)

    def store_search(
        self,
        *,
        task_id: str,
        plan_id: str,
        access_token: str,
        search_id: str,
        category: DestinationCategory,
        places: tuple[PlaceFact, ...],
        source: FactSource,
        now: float | None = None,
    ) -> tuple[str, ...]:
        self._validate_identity(task_id, "task_id")
        self._validate_identity(plan_id, "plan_id")
        self._validate_identity(search_id, "search_id")
        access_hash = _token_hash(access_token)
        timestamp = time.time() if now is None else float(now)
        expires_at = timestamp + self.ttl_seconds
        rows: list[tuple] = []
        tokens: list[str] = []
        for place in places:
            if source.tool_call_id not in place.source_refs and source.tool_call_id not in place.field_sources.values():
                raise ValueError("搜索地点必须引用当前 MCP 来源")
            token = secrets.token_urlsafe(32)
            tokens.append(token)
            rows.append((
                _token_hash(token), task_id, plan_id, access_hash, search_id, category, place.poi_id,
                _dump(place), _dump(source), timestamp, expires_at,
            ))
        with self._write_transaction() as conn:
            conn.execute("DELETE FROM destination_search_candidates WHERE expires_at<=?", (timestamp,))
            conn.executemany(
                """INSERT INTO destination_search_candidates
                   (candidate_token_hash,task_id,plan_id,access_token_hash,search_id,category,poi_id,
                    place_json,source_json,created_at,expires_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                rows,
            )
        return tuple(tokens)

    def get_candidate(
        self,
        candidate_token: str,
        *,
        task_id: str,
        plan_id: str,
        access_token: str,
        now: float | None = None,
    ) -> StoredDestinationCandidate:
        self._validate_identity(task_id, "task_id")
        self._validate_identity(plan_id, "plan_id")
        token_hash = _token_hash(candidate_token, max_length=128)
        access_hash = _token_hash(access_token)
        timestamp = time.time() if now is None else float(now)
        with self._write_transaction() as conn:
            conn.execute("DELETE FROM destination_search_candidates WHERE expires_at<=?", (timestamp,))
            row = conn.execute(
                """SELECT * FROM destination_search_candidates
                   WHERE candidate_token_hash=? AND task_id=? AND plan_id=? AND access_token_hash=?
                     AND expires_at>?""",
                (token_hash, task_id, plan_id, access_hash, timestamp),
            ).fetchone()
        if row is None:
            raise DestinationCandidateNotFound("目的地候选已失效或不存在")
        place = PlaceFact.model_validate_json(row["place_json"])
        source = FactSource.model_validate_json(row["source_json"])
        if source.tool_call_id not in place.source_refs and source.tool_call_id not in place.field_sources.values():
            raise DestinationCandidateNotFound("目的地候选来源无效")
        return StoredDestinationCandidate(
            task_id=row["task_id"],
            plan_id=row["plan_id"],
            search_id=row["search_id"],
            category=row["category"],
            place=place,
            sources=(source,),
            expires_at=datetime.fromtimestamp(row["expires_at"], tz=timezone.utc),
        )

    def cleanup_expired(self, *, now: float | None = None) -> int:
        timestamp = time.time() if now is None else float(now)
        with self._write_transaction() as conn:
            cursor = conn.execute("DELETE FROM destination_search_candidates WHERE expires_at<=?", (timestamp,))
            return cursor.rowcount

    def count(self) -> int:
        conn = self._connect()
        try:
            return int(conn.execute("SELECT COUNT(*) FROM destination_search_candidates").fetchone()[0])
        finally:
            conn.close()

    @staticmethod
    def _validate_identity(value: str, name: str) -> None:
        if not isinstance(value, str) or not value.strip() or len(value) > 256:
            raise ValueError(f"{name} 必须为1到256个字符")

    @contextmanager
    def _write_transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()


def _token_hash(value: str, *, max_length: int = 2048) -> str:
    if not isinstance(value, str) or not value or len(value) > max_length:
        raise ValueError("访问标识无效")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _dump(value) -> str:
    return json.dumps(value.model_dump(mode="json"), ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)

