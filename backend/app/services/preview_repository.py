"""独立的 SQLite 预览草稿与最近成功保存仓库。

本模块只验证 ItineraryPreview 的模型结构。业务引用与可行性校验由服务组装器执行。
每条草稿和最终结果均以一个完整 JSON snapshot 保存，避免事实、候选、报告和行程跨版本混写。
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import secrets
import sqlite3
from typing import Iterator

from app.models.planning_v4.plan import ItineraryPreview
from app.models.preview_storage import (
    CreatedPreview,
    DraftWriteResult,
    FinalSaveResult,
    IdempotencyConflict,
    PreviewAccessError,
    PreviewConflict,
    PreviewIdentityConflict,
    SavedPreview,
    StoredPreview,
)


SCHEMA = """
CREATE TABLE IF NOT EXISTS preview_drafts (
    plan_id TEXT PRIMARY KEY NOT NULL,
    access_token_hash TEXT NOT NULL UNIQUE,
    preview_json TEXT NOT NULL,
    plan_revision INTEGER NOT NULL CHECK (plan_revision > 0),
    store_version INTEGER NOT NULL CHECK (store_version > 0),
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS preview_finals (
    plan_id TEXT PRIMARY KEY NOT NULL,
    preview_json TEXT NOT NULL,
    plan_revision INTEGER NOT NULL CHECK (plan_revision > 0),
    source_store_version INTEGER NOT NULL CHECK (source_store_version > 0),
    save_id TEXT NOT NULL,
    saved_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS preview_mutations (
    mutation_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id TEXT NOT NULL,
    mutation_kind TEXT NOT NULL CHECK (mutation_kind IN ('command', 'save')),
    mutation_id TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    result_status TEXT NOT NULL,
    result_store_version INTEGER NOT NULL CHECK (result_store_version > 0),
    created_at TEXT NOT NULL,
    UNIQUE (plan_id, mutation_kind, mutation_id)
);
CREATE INDEX IF NOT EXISTS preview_mutations_by_plan
    ON preview_mutations(plan_id, mutation_seq DESC);
"""


class PreviewRepository:
    """独立于任务观测仓库的单计划草稿与最终保存仓库。"""

    def __init__(self, path: str | Path, *, receipt_limit: int = 128, timeout_seconds: float = 5.0):
        if receipt_limit < 1:
            raise ValueError("receipt_limit 必须大于 0")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds 必须大于 0")
        self.path = Path(path)
        self.receipt_limit = int(receipt_limit)
        self.timeout_seconds = float(timeout_seconds)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=self.timeout_seconds, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(f"PRAGMA busy_timeout={int(self.timeout_seconds * 1000)}")
        return conn

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

    @contextmanager
    def _read_connection(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    def initialize(self) -> None:
        """创建独立表；不恢复或清理任务表，也不依赖 tasks 外键。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
        finally:
            conn.close()

    def create(self, preview: ItineraryPreview) -> CreatedPreview:
        """保存首个预览，并返回只在本次结果中出现的随机访问标识。"""
        validated = self._validated_preview(preview)
        preview_json = self._dump_preview(validated)
        token = secrets.token_urlsafe(32)
        token_hash = self._token_hash(token)
        timestamp = self._now()
        with self._write_transaction() as conn:
            if conn.execute("SELECT 1 FROM preview_drafts WHERE plan_id=?", (validated.draft.plan_id,)).fetchone():
                raise PreviewIdentityConflict("plan_id 已存在")
            conn.execute(
                """INSERT INTO preview_drafts
                   (plan_id,access_token_hash,preview_json,plan_revision,store_version,updated_at)
                   VALUES (?,?,?,?,1,?)""",
                (validated.draft.plan_id, token_hash, preview_json, validated.draft.revision, timestamp),
            )
        record = StoredPreview(validated, store_version=1, plan_revision=validated.draft.revision, updated_at=timestamp)
        return CreatedPreview(access_token=token, record=record)

    def get_preview(self, access_token: str) -> StoredPreview | None:
        """按不透明访问标识读取草稿；无效标识与不存在记录均返回 None。"""
        token_hash = self._token_hash(access_token)
        with self._read_connection() as conn:
            row = conn.execute(
                "SELECT * FROM preview_drafts WHERE access_token_hash=?", (token_hash,)
            ).fetchone()
        return self._stored_from_row(row) if row else None

    def replace_preview(
        self,
        access_token: str,
        preview: ItineraryPreview,
        *,
        expected_store_version: int,
        expected_plan_revision: int,
        command_id: str,
    ) -> DraftWriteResult:
        """通过双版本 CAS 原子替换整份草稿，并按 command_id 幂等重放。"""
        validated = self._validated_preview(preview)
        preview_json = self._dump_preview(validated)
        command_id = self._required_id(command_id, "command_id")
        expected_store_version = self._positive_version(expected_store_version, "expected_store_version")
        expected_plan_revision = self._positive_version(expected_plan_revision, "expected_plan_revision")
        request_hash = self._fingerprint({
            "expected_store_version": expected_store_version,
            "expected_plan_revision": expected_plan_revision,
            "preview_json": preview_json,
        })
        token_hash = self._token_hash(access_token)

        with self._write_transaction() as conn:
            row = conn.execute(
                "SELECT * FROM preview_drafts WHERE access_token_hash=?", (token_hash,)
            ).fetchone()
            if row is None:
                raise PreviewAccessError("预览访问标识无效")
            if validated.draft.plan_id != row["plan_id"]:
                raise PreviewAccessError("预览访问标识无效")

            receipt = conn.execute(
                """SELECT request_hash,result_store_version FROM preview_mutations
                   WHERE plan_id=? AND mutation_kind='command' AND mutation_id=?""",
                (row["plan_id"], command_id),
            ).fetchone()
            if receipt:
                if receipt["request_hash"] != request_hash:
                    raise IdempotencyConflict("command_id 已用于不同请求")
                return DraftWriteResult(
                    record=self._stored_from_row(row),
                    receipt_store_version=receipt["result_store_version"],
                    replayed=True,
                )

            current = self._stored_from_row(row)
            self._check_immutable_identity(current.preview, validated)
            if current.store_version != expected_store_version or current.plan_revision != expected_plan_revision:
                raise PreviewConflict(
                    "草稿版本已变化",
                    current_store_version=current.store_version,
                    current_plan_revision=current.plan_revision,
                )
            if validated.draft.revision < current.plan_revision:
                raise PreviewConflict(
                    "plan_revision 不能倒退",
                    current_store_version=current.store_version,
                    current_plan_revision=current.plan_revision,
                )

            new_store_version = current.store_version + 1
            timestamp = self._now()
            changed = conn.execute(
                """UPDATE preview_drafts
                   SET preview_json=?,plan_revision=?,store_version=?,updated_at=?
                   WHERE plan_id=? AND store_version=? AND plan_revision=?""",
                (
                    preview_json,
                    validated.draft.revision,
                    new_store_version,
                    timestamp,
                    current.preview.draft.plan_id,
                    expected_store_version,
                    expected_plan_revision,
                ),
            )
            if changed.rowcount != 1:
                raise PreviewConflict(
                    "草稿版本已变化",
                    current_store_version=current.store_version,
                    current_plan_revision=current.plan_revision,
                )
            self._insert_receipt(
                conn,
                plan_id=current.preview.draft.plan_id,
                mutation_kind="command",
                mutation_id=command_id,
                request_hash=request_hash,
                result_status="applied",
                result_store_version=new_store_version,
                created_at=timestamp,
            )
            self._prune_receipts(conn, current.preview.draft.plan_id)
            updated_row = conn.execute(
                "SELECT * FROM preview_drafts WHERE plan_id=?", (current.preview.draft.plan_id,)
            ).fetchone()
            return DraftWriteResult(
                record=self._stored_from_row(updated_row),
                receipt_store_version=new_store_version,
                replayed=False,
            )

    def save_final(
        self,
        access_token: str,
        *,
        save_id: str,
        source_store_version: int,
    ) -> FinalSaveResult:
        """只把导出时读取的当前草稿版本复制为最近成功最终结果。"""
        save_id = self._required_id(save_id, "save_id")
        source_store_version = self._positive_version(source_store_version, "source_store_version")
        token_hash = self._token_hash(access_token)
        request_hash = self._fingerprint({"source_store_version": source_store_version})

        with self._write_transaction() as conn:
            row = conn.execute(
                "SELECT * FROM preview_drafts WHERE access_token_hash=?", (token_hash,)
            ).fetchone()
            if row is None:
                raise PreviewAccessError("预览访问标识无效")
            plan_id = row["plan_id"]
            receipt = conn.execute(
                """SELECT request_hash,result_status,result_store_version FROM preview_mutations
                   WHERE plan_id=? AND mutation_kind='save' AND mutation_id=?""",
                (plan_id, save_id),
            ).fetchone()
            if receipt:
                if receipt["request_hash"] != request_hash:
                    raise IdempotencyConflict("save_id 已用于不同请求")
                final_row = conn.execute("SELECT * FROM preview_finals WHERE plan_id=?", (plan_id,)).fetchone()
                return FinalSaveResult(
                    status=receipt["result_status"],
                    final=self._final_from_row(final_row) if final_row else None,
                    current_store_version=row["store_version"],
                    receipt_store_version=receipt["result_store_version"],
                    replayed=True,
                )

            now = self._now()
            current_store_version = row["store_version"]
            current_preview = self._preview_from_json(row["preview_json"])
            final_row = conn.execute("SELECT * FROM preview_finals WHERE plan_id=?", (plan_id,)).fetchone()
            if source_store_version != current_store_version:
                status = "stale"
                receipt_version = current_store_version
            elif final_row and final_row["source_store_version"] > source_store_version:
                status = "stale"
                receipt_version = current_store_version
            elif final_row and final_row["source_store_version"] == source_store_version:
                status = "already_saved"
                receipt_version = current_store_version
            else:
                status = "saved"
                receipt_version = source_store_version
                conn.execute(
                    """INSERT INTO preview_finals
                       (plan_id,preview_json,plan_revision,source_store_version,save_id,saved_at)
                       VALUES (?,?,?,?,?,?)
                       ON CONFLICT(plan_id) DO UPDATE SET
                         preview_json=excluded.preview_json,
                         plan_revision=excluded.plan_revision,
                         source_store_version=excluded.source_store_version,
                         save_id=excluded.save_id,
                         saved_at=excluded.saved_at""",
                    (
                        plan_id,
                        row["preview_json"],
                        current_preview.draft.revision,
                        source_store_version,
                        save_id,
                        now,
                    ),
                )
                final_row = conn.execute("SELECT * FROM preview_finals WHERE plan_id=?", (plan_id,)).fetchone()

            self._insert_receipt(
                conn,
                plan_id=plan_id,
                mutation_kind="save",
                mutation_id=save_id,
                request_hash=request_hash,
                result_status=status,
                result_store_version=receipt_version,
                created_at=now,
            )
            self._prune_receipts(conn, plan_id)
            return FinalSaveResult(
                status=status,
                final=self._final_from_row(final_row) if final_row else None,
                current_store_version=current_store_version,
                receipt_store_version=receipt_version,
                replayed=False,
            )

    def read_final(self, access_token: str) -> SavedPreview | None:
        """只读最近一次成功保存；还原为新草稿版本由上层服务执行。"""
        token_hash = self._token_hash(access_token)
        with self._read_connection() as conn:
            draft = conn.execute(
                "SELECT plan_id FROM preview_drafts WHERE access_token_hash=?", (token_hash,)
            ).fetchone()
            if draft is None:
                return None
            row = conn.execute(
                "SELECT * FROM preview_finals WHERE plan_id=?", (draft["plan_id"],)
            ).fetchone()
        return self._final_from_row(row) if row else None

    def _insert_receipt(
        self,
        conn: sqlite3.Connection,
        *,
        plan_id: str,
        mutation_kind: str,
        mutation_id: str,
        request_hash: str,
        result_status: str,
        result_store_version: int,
        created_at: str,
    ) -> None:
        conn.execute(
            """INSERT INTO preview_mutations
               (plan_id,mutation_kind,mutation_id,request_hash,result_status,result_store_version,created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (plan_id, mutation_kind, mutation_id, request_hash, result_status, result_store_version, created_at),
        )

    def _prune_receipts(self, conn: sqlite3.Connection, plan_id: str) -> None:
        expired = conn.execute(
            """SELECT mutation_seq FROM preview_mutations WHERE plan_id=?
               ORDER BY mutation_seq DESC LIMIT -1 OFFSET ?""",
            (plan_id, self.receipt_limit),
        ).fetchall()
        if expired:
            conn.executemany(
                "DELETE FROM preview_mutations WHERE mutation_seq=?",
                ((row["mutation_seq"],) for row in expired),
            )

    @staticmethod
    def _check_immutable_identity(current: ItineraryPreview, updated: ItineraryPreview) -> None:
        current_identity = (
            current.preview_id,
            current.draft.plan_id,
            current.draft.request_id,
            current.draft.source_run_id,
            current.draft.rule_version,
            current.planning_input.request.request_id,
            current.planning_input.rules.rule_version,
        )
        updated_identity = (
            updated.preview_id,
            updated.draft.plan_id,
            updated.draft.request_id,
            updated.draft.source_run_id,
            updated.draft.rule_version,
            updated.planning_input.request.request_id,
            updated.planning_input.rules.rule_version,
        )
        if updated_identity != current_identity:
            raise PreviewIdentityConflict("更新不能改变预览、计划、需求、运行或规则身份")

    @staticmethod
    def _validated_preview(preview: ItineraryPreview) -> ItineraryPreview:
        value = preview.model_dump(mode="json") if isinstance(preview, ItineraryPreview) else preview
        return ItineraryPreview.model_validate(value)

    @staticmethod
    def _dump_preview(preview: ItineraryPreview) -> str:
        return json.dumps(
            preview.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )

    @staticmethod
    def _preview_from_json(value: str) -> ItineraryPreview:
        return ItineraryPreview.model_validate_json(value)

    def _stored_from_row(self, row: sqlite3.Row) -> StoredPreview:
        preview = self._preview_from_json(row["preview_json"])
        return StoredPreview(
            preview=preview,
            store_version=row["store_version"],
            plan_revision=row["plan_revision"],
            updated_at=row["updated_at"],
        )

    def _final_from_row(self, row: sqlite3.Row) -> SavedPreview:
        preview = self._preview_from_json(row["preview_json"])
        return SavedPreview(
            preview=preview,
            plan_revision=row["plan_revision"],
            source_store_version=row["source_store_version"],
            save_id=row["save_id"],
            saved_at=row["saved_at"],
        )

    @staticmethod
    def _token_hash(access_token: str) -> str:
        if not isinstance(access_token, str) or not access_token or len(access_token) > 2048:
            raise PreviewAccessError("预览访问标识无效")
        return hashlib.sha256(access_token.encode("utf-8")).hexdigest()

    @staticmethod
    def _required_id(value: str, name: str) -> str:
        if not isinstance(value, str) or not value.strip() or len(value) > 256:
            raise ValueError(f"{name} 必须是 1 到 256 个字符")
        return value

    @staticmethod
    def _positive_version(value: int, name: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"{name} 必须是正整数")
        return value

    @staticmethod
    def _fingerprint(value: object) -> str:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="microseconds")
