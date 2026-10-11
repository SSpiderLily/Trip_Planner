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
from typing import Callable, Iterator
import hmac

from app.models.planning_v4.plan import ItineraryPreview
from app.models.planning_v4.cost import ReferenceCostSummary
from app.models.planning_v4_preview import PreviewSnapshot
from app.services.planning_v4_timing import PlanTimingResult
from app.models.preview_storage import (
    CreatedPreview,
    ClaimedPreviewRouteJob,
    DraftWriteResult,
    FinalSaveResult,
    IdempotencyConflict,
    PreviewAccessError,
    PreviewConflict,
    PreviewEditWriteResult,
    PreviewFinalChanged,
    PreviewFinalUnavailable,
    PreviewIdentityConflict,
    PreviewRestoreResult,
    PreviewRouteJob,
    SavedPreview,
    SavedPreviewMetadata,
    StoredPreview,
)


SCHEMA = """
CREATE TABLE IF NOT EXISTS preview_drafts (
    plan_id TEXT PRIMARY KEY NOT NULL,
    access_token_hash TEXT NOT NULL UNIQUE,
    preview_json TEXT NOT NULL,
    timing_json TEXT,
    cost_json TEXT,
    plan_revision INTEGER NOT NULL CHECK (plan_revision > 0),
    store_version INTEGER NOT NULL CHECK (store_version > 0),
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS preview_finals (
    plan_id TEXT PRIMARY KEY NOT NULL,
    preview_json TEXT NOT NULL,
    timing_json TEXT,
    cost_json TEXT,
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
CREATE TABLE IF NOT EXISTS preview_restore_receipts (
    restore_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id TEXT NOT NULL,
    restore_id TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    result_store_version INTEGER NOT NULL CHECK (result_store_version > 0),
    source_save_id TEXT NOT NULL,
    source_saved_at TEXT NOT NULL,
    source_store_version INTEGER NOT NULL CHECK (source_store_version > 0),
    source_plan_revision INTEGER NOT NULL CHECK (source_plan_revision > 0),
    created_at TEXT NOT NULL,
    UNIQUE (plan_id, restore_id)
);
CREATE INDEX IF NOT EXISTS preview_restore_receipts_by_plan
    ON preview_restore_receipts(plan_id, restore_seq DESC);
CREATE TABLE IF NOT EXISTS preview_edit_receipts (
    edit_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id TEXT NOT NULL,
    command_id TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    result_store_version INTEGER NOT NULL CHECK (result_store_version > 0),
    result_plan_revision INTEGER NOT NULL CHECK (result_plan_revision > 0),
    calculation_id TEXT NOT NULL,
    changed_activity_ids_json TEXT NOT NULL,
    changed_leg_ids_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (plan_id, command_id)
);
CREATE INDEX IF NOT EXISTS preview_edit_receipts_by_plan
    ON preview_edit_receipts(plan_id, edit_seq DESC);
CREATE TABLE IF NOT EXISTS preview_route_jobs (
    calculation_id TEXT PRIMARY KEY NOT NULL,
    task_id TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    source_store_version INTEGER NOT NULL CHECK (source_store_version > 0),
    source_plan_revision INTEGER NOT NULL CHECK (source_plan_revision > 0),
    affected_day_ids_json TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('pending','running','completed','failed','superseded')),
    result_store_version INTEGER CHECK (result_store_version IS NULL OR result_store_version > 0),
    error_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS preview_route_jobs_by_plan_state
    ON preview_route_jobs(plan_id, state, created_at);
CREATE TABLE IF NOT EXISTS preview_jobs (
    task_id TEXT PRIMARY KEY NOT NULL,
    plan_id TEXT NOT NULL UNIQUE,
    access_token_hash TEXT NOT NULL UNIQUE,
    generation_state TEXT NOT NULL CHECK (generation_state IN ('accepted','running','ready','failed','interrupted')),
    generation_finished_at TEXT,
    generation_stage TEXT NOT NULL,
    generation_completed INTEGER NOT NULL DEFAULT 0 CHECK (generation_completed >= 0),
    generation_total INTEGER NOT NULL DEFAULT 0 CHECK (generation_total >= 0),
    generation_message TEXT,
    backfill_state TEXT NOT NULL CHECK (backfill_state IN ('not_started','running','completed','failed','interrupted')),
    backfill_completed INTEGER NOT NULL DEFAULT 0 CHECK (backfill_completed >= 0),
    backfill_total INTEGER NOT NULL DEFAULT 0 CHECK (backfill_total >= 0),
    preview_plan_id TEXT,
    error_code TEXT,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS preview_jobs_by_state ON preview_jobs(generation_state, updated_at);
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
            for table in ("preview_drafts", "preview_finals"):
                existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
                for field in ("timing_json", "cost_json"):
                    if field not in existing:
                        conn.execute(f"ALTER TABLE {table} ADD COLUMN {field} TEXT")
            job_fields = {row["name"] for row in conn.execute("PRAGMA table_info(preview_jobs)")}
            if "generation_finished_at" not in job_fields:
                # 老任务无法可靠还原首屏完成时间，保留 NULL；不使用会被补齐/重启更新的 updated_at 猜测。
                conn.execute("ALTER TABLE preview_jobs ADD COLUMN generation_finished_at TEXT")
        finally:
            conn.close()

    def create(
        self,
        preview: ItineraryPreview | PreviewSnapshot,
        *,
        access_token: str | None = None,
    ) -> CreatedPreview:
        """保存首个预览，并返回只在本次结果中出现的随机访问标识。

        ``access_token`` 仅供受信任的异步任务服务传入预先签发的标识；旧调用仍由仓库生成。
        """
        validated, timing_json, cost_json = self._validated_snapshot(preview)
        preview_json = self._dump_preview(validated)
        token = self.issue_access_token() if access_token is None else access_token
        token_hash = self._token_hash(token)
        timestamp = self._now()
        with self._write_transaction() as conn:
            if conn.execute("SELECT 1 FROM preview_drafts WHERE plan_id=?", (validated.draft.plan_id,)).fetchone():
                raise PreviewIdentityConflict("plan_id 已存在")
            conn.execute(
                """INSERT INTO preview_drafts
                   (plan_id,access_token_hash,preview_json,timing_json,cost_json,plan_revision,store_version,updated_at)
                   VALUES (?,?,?,?,?,?,1,?)""",
                (validated.draft.plan_id, token_hash, preview_json, timing_json, cost_json,
                 validated.draft.revision, timestamp),
            )
        record = StoredPreview(
            validated, store_version=1, plan_revision=validated.draft.revision,
            updated_at=timestamp,
            timing=self._timing_from_json(timing_json), cost=self._cost_from_json(cost_json),
        )
        return CreatedPreview(access_token=token, record=record)

    @staticmethod
    def issue_access_token() -> str:
        """为异步任务响应预先签发高熵 bearer 标识。"""
        return secrets.token_urlsafe(32)

    def create_job(self, task_id: str, plan_id: str, access_token: str) -> None:
        """先保存异步任务的 token hash；明文只由调用方保留并返回一次。"""
        timestamp = self._now()
        with self._write_transaction() as conn:
            conn.execute(
                """INSERT INTO preview_jobs
                   (task_id,plan_id,access_token_hash,generation_state,generation_stage,
                    backfill_state,updated_at) VALUES (?,?,?,'accepted','accepted','not_started',?)""",
                (self._required_id(task_id, "task_id"), self._required_id(plan_id, "plan_id"),
                 self._token_hash(access_token), timestamp),
            )

    def job_status(self, task_id: str, access_token: str) -> dict | None:
        token_hash = self._token_hash(access_token)
        with self._read_connection() as conn:
            row = conn.execute(
                """SELECT j.*,p.store_version AS actual_store_version
                   FROM preview_jobs j LEFT JOIN preview_drafts p ON p.plan_id=j.preview_plan_id
                   WHERE j.task_id=?""",
                (task_id,),
            ).fetchone()
        if row is None or not hmac.compare_digest(row["access_token_hash"], token_hash):
            return None
        return self._job_from_row(row)

    def ready_task_completion(self, task_id: str) -> dict | None:
        """供任务状态恢复核实：仅返回持久化首屏已就绪的观测任务证据。"""
        with self._read_connection() as conn:
            row = conn.execute(
                """SELECT task_id,preview_plan_id,generation_finished_at FROM preview_jobs
                   WHERE task_id=? AND generation_state='ready' AND preview_plan_id IS NOT NULL""",
                (task_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def ready_task_completions(self, task_ids: tuple[str, ...] | list[str]) -> dict[str, dict]:
        """批量读取已就绪任务摘要，不读取预览正文，也不为每个任务单独打开连接。"""
        ids = tuple(dict.fromkeys(task_id for task_id in task_ids if task_id))
        if not ids:
            return {}
        result: dict[str, dict] = {}
        # SQLite 的 bind 参数数目有限；用同一个连接分块查询，避免任务列表触发 N+1 连接。
        with self._read_connection() as conn:
            for start in range(0, len(ids), 500):
                batch = ids[start:start + 500]
                placeholders = ",".join("?" for _ in batch)
                rows = conn.execute(
                    f"""SELECT task_id,preview_plan_id,generation_finished_at FROM preview_jobs
                        WHERE generation_state='ready' AND preview_plan_id IS NOT NULL
                          AND task_id IN ({placeholders})""",
                    batch,
                ).fetchall()
                result.update((row["task_id"], dict(row)) for row in rows)
        return result

    def update_job(
        self,
        task_id: str,
        *,
        generation_state: str | None = None,
        generation_stage: str | None = None,
        generation_completed: int | None = None,
        generation_total: int | None = None,
        generation_message: str | None = None,
        backfill_state: str | None = None,
        backfill_completed: int | None = None,
        backfill_total: int | None = None,
        error_code: str | None = None,
    ) -> None:
        allowed = {
            "generation_state": ("accepted", "running", "ready", "failed", "interrupted"),
            "backfill_state": ("not_started", "running", "completed", "failed", "interrupted"),
        }
        updates = {
            "generation_state": generation_state,
            "generation_stage": generation_stage,
            "generation_completed": generation_completed,
            "generation_total": generation_total,
            "generation_message": generation_message,
            "backfill_state": backfill_state,
            "backfill_completed": backfill_completed,
            "backfill_total": backfill_total,
            "error_code": error_code,
        }
        fields = [(key, value) for key, value in updates.items() if value is not None]
        for key, value in fields:
            if key in allowed and value not in allowed[key]:
                raise ValueError(f"{key} 状态无效")
            if key.endswith("_completed") or key.endswith("_total"):
                if type(value) is not int or value < 0:
                    raise ValueError(f"{key} 必须是非负整数")
        if not fields:
            return
        assignments = ",".join(f"{key}=?" for key, _ in fields)
        with self._write_transaction() as conn:
            row = conn.execute("SELECT generation_state FROM preview_jobs WHERE task_id=?", (task_id,)).fetchone()
            if row is None:
                raise KeyError("预览任务不存在")
            if row["generation_state"] == "ready" and any(
                key in {"generation_state", "generation_stage", "generation_completed", "generation_total", "generation_message"}
                for key, _ in fields
            ):
                raise ValueError("预览已就绪，不能覆盖生成状态")
            conn.execute(
                f"UPDATE preview_jobs SET {assignments},updated_at=? WHERE task_id=?",
                (*[value for _, value in fields], self._now(), task_id),
            )

    def complete_job(self, task_id: str, snapshot: PreviewSnapshot) -> StoredPreview:
        """在一个事务中写入草稿快照并把任务指针切到 ready。"""
        validated, timing_json, cost_json = self._validated_snapshot(snapshot)
        preview_json = self._dump_preview(validated)
        timestamp = self._now()
        with self._write_transaction() as conn:
            job = conn.execute("SELECT * FROM preview_jobs WHERE task_id=?", (task_id,)).fetchone()
            if job is None or job["generation_state"] not in ("accepted", "running"):
                raise ValueError("任务状态不允许保存预览")
            if job["plan_id"] != validated.draft.plan_id:
                raise PreviewIdentityConflict("任务计划 ID 与预览不一致")
            conn.execute(
                """INSERT INTO preview_drafts
                   (plan_id,access_token_hash,preview_json,timing_json,cost_json,plan_revision,store_version,updated_at)
                   VALUES (?,?,?,?,?,?,1,?)""",
                (validated.draft.plan_id, job["access_token_hash"], preview_json, timing_json, cost_json,
                 validated.draft.revision, timestamp),
            )
            conn.execute(
                """UPDATE preview_jobs SET generation_state='ready',generation_stage='ready',
                   generation_finished_at=?,generation_completed=generation_total,generation_message=NULL,
                   preview_plan_id=?,updated_at=?
                   WHERE task_id=?""",
                (timestamp, validated.draft.plan_id, timestamp, task_id),
            )
        return StoredPreview(
            validated, store_version=1, plan_revision=validated.draft.revision,
            updated_at=timestamp, timing=self._timing_from_json(timing_json),
            cost=self._cost_from_json(cost_json),
        )

    def recover_jobs(self) -> None:
        """进程重启后标记未就绪的生成与后台任务为中断；就绪预览保留。"""
        with self._write_transaction() as conn:
            conn.execute(
                """UPDATE preview_jobs SET generation_state='interrupted',generation_stage='interrupted',
                   error_code='PROCESS_INTERRUPTED',updated_at=?
                   WHERE generation_state IN ('accepted','running')""",
                (self._now(),),
            )
            conn.execute(
                "UPDATE preview_jobs SET backfill_state='interrupted',updated_at=? WHERE backfill_state='running'",
                (self._now(),),
            )
            route_rows = conn.execute(
                "SELECT * FROM preview_route_jobs WHERE state IN ('pending','running')"
            ).fetchall()
            now = self._now()
            for route_row in route_rows:
                draft_row = conn.execute(
                    "SELECT * FROM preview_drafts WHERE plan_id=?", (route_row["plan_id"],)
                ).fetchone()
                current = self._stored_from_row(draft_row) if draft_row is not None else None
                if current is not None and self._job_matches_record(route_row, current):
                    conn.execute(
                        "UPDATE preview_route_jobs SET state='pending',updated_at=? WHERE calculation_id=?",
                        (now, route_row["calculation_id"]),
                    )
                else:
                    conn.execute(
                        "UPDATE preview_route_jobs SET state='superseded',updated_at=? WHERE calculation_id=?",
                        (now, route_row["calculation_id"]),
                    )

    def delete_job(self, task_id: str) -> None:
        """回滚尚未接受成功的任务登记。"""
        with self._write_transaction() as conn:
            conn.execute("DELETE FROM preview_jobs WHERE task_id=?", (task_id,))

    def get_preview(self, access_token: str) -> StoredPreview | None:
        """按不透明访问标识读取草稿；无效标识与不存在记录均返回 None。"""
        token_hash = self._token_hash(access_token)
        with self._read_connection() as conn:
            row = conn.execute(
                "SELECT * FROM preview_drafts WHERE access_token_hash=?", (token_hash,)
            ).fetchone()
        return self._stored_from_row(row) if row else None

    def current_plan_revision(self, plan_id: str) -> int | None:
        """读取计划当前版本，供后台路线任务在查询前检查是否仍有效。"""
        plan_id = self._required_id(plan_id, "plan_id")
        with self._read_connection() as conn:
            row = conn.execute(
                "SELECT plan_revision FROM preview_drafts WHERE plan_id=?", (plan_id,)
            ).fetchone()
        return int(row["plan_revision"]) if row is not None else None

    def apply_edit(
        self,
        access_token: str,
        task_id: str,
        command,
        *,
        expected_store_version: int,
        transform: Callable[[PreviewSnapshot, tuple[str, ...]], tuple[
            PreviewSnapshot, tuple[str, ...], tuple[str, ...]
        ]],
    ) -> PreviewEditWriteResult:
        """在一个 SQLite 写事务内检查收据、CAS、改稿、收据和路线任务。"""
        command_id = self._required_id(command.command_id, "command_id")
        expected_store_version = self._positive_version(expected_store_version, "expected_store_version")
        request_hash = self._fingerprint({
            "expected_store_version": expected_store_version,
            "command": command.model_dump(mode="json"),
        })
        token_hash = self._token_hash(access_token)

        with self._write_transaction() as conn:
            row = conn.execute(
                """SELECT p.*,j.task_id,j.generation_state,j.preview_plan_id
                   FROM preview_drafts p JOIN preview_jobs j ON j.plan_id=p.plan_id
                   WHERE p.access_token_hash=? AND j.task_id=?""",
                (token_hash, self._required_id(task_id, "task_id")),
            ).fetchone()
            if row is None:
                raise PreviewAccessError("预览访问标识与任务不匹配")
            if row["generation_state"] != "ready" or row["preview_plan_id"] != row["plan_id"]:
                raise PreviewConflict(
                    "预览尚未就绪",
                    current_store_version=row["store_version"],
                    current_plan_revision=row["plan_revision"],
                )

            receipt = conn.execute(
                """SELECT * FROM preview_edit_receipts WHERE plan_id=? AND command_id=?""",
                (row["plan_id"], command_id),
            ).fetchone()
            current = self._stored_from_row(row)
            if receipt is not None:
                if receipt["request_hash"] != request_hash:
                    raise IdempotencyConflict("command_id 已用于不同编辑请求")
                route_job_row = conn.execute(
                    "SELECT * FROM preview_route_jobs WHERE plan_id=? AND calculation_id=?",
                    (row["plan_id"], receipt["calculation_id"]),
                ).fetchone()
                return PreviewEditWriteResult(
                    record=current,
                    receipt_store_version=receipt["result_store_version"],
                    replayed=True,
                    calculation_id=receipt["calculation_id"],
                    changed_activity_ids=tuple(json.loads(receipt["changed_activity_ids_json"])),
                    changed_leg_ids=tuple(json.loads(receipt["changed_leg_ids_json"])),
                    route_job=(self._route_job_from_row(route_job_row) if route_job_row else None),
                )

            if command.plan_id != current.preview.draft.plan_id:
                raise PreviewAccessError("编辑命令与访问计划不匹配")
            if (
                current.store_version != expected_store_version
                or current.plan_revision != command.base_revision
            ):
                raise PreviewConflict(
                    "草稿版本已变化",
                    current_store_version=current.store_version,
                    current_plan_revision=current.plan_revision,
                )
            snapshot = current.snapshot
            if snapshot is None:
                raise PreviewConflict(
                    "预览计算快照不完整",
                    current_store_version=current.store_version,
                    current_plan_revision=current.plan_revision,
                )

            inherited_days = set(self._pending_days(snapshot.preview))
            current_job = conn.execute(
                """SELECT affected_day_ids_json FROM preview_route_jobs
                   WHERE plan_id=? AND calculation_id=? AND state IN ('pending','running')
                     AND source_store_version=? AND source_plan_revision=?""",
                (
                    current.preview.draft.plan_id,
                    snapshot.preview.calculation.calculation_id,
                    current.store_version,
                    current.plan_revision,
                ),
            ).fetchone()
            if current_job is not None:
                inherited_days.update(json.loads(current_job["affected_day_ids_json"]))

            updated_snapshot, changed_activity_ids, changed_leg_ids = transform(
                snapshot, tuple(sorted(inherited_days)),
            )
            updated, timing_json, cost_json = self._validated_snapshot(updated_snapshot)
            preview_json = self._dump_preview(updated)
            self._check_immutable_identity(current.preview, updated)
            if updated.draft.revision != current.plan_revision + 1:
                raise PreviewIdentityConflict("编辑必须且只能递增一个 plan_revision")
            if updated.draft.base_revision != current.plan_revision:
                raise PreviewIdentityConflict("编辑 draft.base_revision 必须引用当前 revision")
            pending_ids = set(updated.calculation.pending_leg_ids)
            leg_by_id = {leg.leg_id: leg for leg in updated.draft.legs}
            if updated.calculation.current_routes == "pending":
                if not pending_ids or pending_ids - set(leg_by_id):
                    raise PreviewIdentityConflict("pending 路线必须引用当前草稿路段")
                pending_days = {leg_by_id[item].day_id for item in pending_ids}
                affected_days = tuple(
                    day.day_id for day in updated.planning_input.days
                    if day.day_id in pending_days
                )
                if set(affected_days) != pending_days:
                    raise PreviewIdentityConflict("pending 路线引用了当前行程之外的日期")
            else:
                if pending_ids:
                    raise PreviewIdentityConflict("非 pending 路线状态不能保留待计算路段")
                affected_days = ()

            timestamp = self._now()
            new_store_version = current.store_version + 1
            changed = conn.execute(
                """UPDATE preview_drafts
                   SET preview_json=?,timing_json=?,cost_json=?,plan_revision=?,store_version=?,updated_at=?
                   WHERE plan_id=? AND access_token_hash=? AND store_version=? AND plan_revision=?""",
                (
                    preview_json, timing_json, cost_json, updated.draft.revision,
                    new_store_version, timestamp, current.preview.draft.plan_id,
                    token_hash, expected_store_version, command.base_revision,
                ),
            )
            if changed.rowcount != 1:
                latest = conn.execute(
                    "SELECT store_version,plan_revision FROM preview_drafts WHERE plan_id=?",
                    (current.preview.draft.plan_id,),
                ).fetchone()
                raise PreviewConflict(
                    "草稿版本已变化",
                    current_store_version=latest["store_version"],
                    current_plan_revision=latest["plan_revision"],
                )

            conn.execute(
                """UPDATE preview_route_jobs SET state='superseded',updated_at=?
                   WHERE plan_id=? AND state IN ('pending','running')""",
                (timestamp, current.preview.draft.plan_id),
            )
            calculation_id = updated.calculation.calculation_id
            if affected_days:
                self._insert_route_job(
                    conn,
                    task_id=task_id,
                    plan_id=current.preview.draft.plan_id,
                    calculation_id=calculation_id,
                    source_store_version=new_store_version,
                    source_plan_revision=updated.draft.revision,
                    affected_day_ids=affected_days,
                    created_at=timestamp,
                )
            conn.execute(
                """INSERT INTO preview_edit_receipts
                   (plan_id,command_id,request_hash,result_store_version,result_plan_revision,
                    calculation_id,changed_activity_ids_json,changed_leg_ids_json,created_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    current.preview.draft.plan_id,
                    command_id,
                    request_hash,
                    new_store_version,
                    updated.draft.revision,
                    calculation_id,
                    json.dumps(changed_activity_ids, ensure_ascii=False, separators=(",", ":")),
                    json.dumps(changed_leg_ids, ensure_ascii=False, separators=(",", ":")),
                    timestamp,
                ),
            )
            self._prune_edit_receipts(conn, current.preview.draft.plan_id)
            stored_row = conn.execute(
                "SELECT * FROM preview_drafts WHERE plan_id=?", (current.preview.draft.plan_id,)
            ).fetchone()
            route_job_row = conn.execute(
                "SELECT * FROM preview_route_jobs WHERE plan_id=? AND calculation_id=?",
                (current.preview.draft.plan_id, calculation_id),
            ).fetchone()
            return PreviewEditWriteResult(
                record=self._stored_from_row(stored_row),
                receipt_store_version=new_store_version,
                replayed=False,
                calculation_id=calculation_id,
                changed_activity_ids=tuple(changed_activity_ids),
                changed_leg_ids=tuple(changed_leg_ids),
                route_job=(self._route_job_from_row(route_job_row) if route_job_row else None),
            )

    def get_route_job(
        self, access_token: str, task_id: str, calculation_id: str,
    ) -> PreviewRouteJob | None:
        token_hash = self._token_hash(access_token)
        with self._read_connection() as conn:
            row = conn.execute(
                """SELECT r.* FROM preview_route_jobs r
                   JOIN preview_jobs j ON j.plan_id=r.plan_id
                   WHERE j.task_id=? AND j.access_token_hash=? AND r.calculation_id=?""",
                (task_id, token_hash, calculation_id),
            ).fetchone()
        return self._route_job_from_row(row) if row is not None else None

    def requeue_stale_running_route_job(
        self, access_token: str, task_id: str, calculation_id: str,
    ) -> PreviewRouteJob | None:
        """Recover a same-process worker that exited while its durable job stayed running."""
        token_hash = self._token_hash(access_token)
        with self._write_transaction() as conn:
            job_row = conn.execute(
                """SELECT r.* FROM preview_route_jobs r
                   JOIN preview_jobs j ON j.plan_id=r.plan_id
                   WHERE j.task_id=? AND j.access_token_hash=? AND r.calculation_id=?""",
                (task_id, token_hash, calculation_id),
            ).fetchone()
            if job_row is None or job_row["state"] != "running":
                return self._route_job_from_row(job_row) if job_row is not None else None

            draft_row = conn.execute(
                "SELECT * FROM preview_drafts WHERE plan_id=?", (job_row["plan_id"],),
            ).fetchone()
            current = self._stored_from_row(draft_row) if draft_row is not None else None
            state = (
                "pending"
                if current is not None and self._job_matches_record(job_row, current)
                else "superseded"
            )
            conn.execute(
                "UPDATE preview_route_jobs SET state=?,updated_at=? WHERE calculation_id=? AND state='running'",
                (state, self._now(), calculation_id),
            )
            refreshed = conn.execute(
                "SELECT * FROM preview_route_jobs WHERE calculation_id=?", (calculation_id,),
            ).fetchone()
            return self._route_job_from_row(refreshed)

    def get_route_job_by_plan(self, plan_id: str, calculation_id: str) -> PreviewRouteJob | None:
        with self._read_connection() as conn:
            row = conn.execute(
                "SELECT * FROM preview_route_jobs WHERE plan_id=? AND calculation_id=?",
                (plan_id, calculation_id),
            ).fetchone()
        return self._route_job_from_row(row) if row is not None else None

    def list_pending_route_plans(self) -> tuple[str, ...]:
        with self._read_connection() as conn:
            rows = conn.execute(
                "SELECT DISTINCT plan_id FROM preview_route_jobs WHERE state='pending' ORDER BY plan_id"
            ).fetchall()
        return tuple(row["plan_id"] for row in rows)

    def has_pending_route_job(self, plan_id: str) -> bool:
        with self._read_connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM preview_route_jobs WHERE plan_id=? AND state='pending' LIMIT 1",
                (plan_id,),
            ).fetchone()
        return row is not None

    def requeue_route_jobs(self) -> None:
        """优雅关停后，仅把仍绑定当前 pending 快照的 worker 重新排队。"""
        with self._write_transaction() as conn:
            now = self._now()
            rows = conn.execute(
                "SELECT * FROM preview_route_jobs WHERE state='running'"
            ).fetchall()
            for job_row in rows:
                draft_row = conn.execute(
                    "SELECT * FROM preview_drafts WHERE plan_id=?", (job_row["plan_id"],)
                ).fetchone()
                current = self._stored_from_row(draft_row) if draft_row is not None else None
                state = "pending" if current is not None and self._job_matches_record(job_row, current) else "superseded"
                conn.execute(
                    "UPDATE preview_route_jobs SET state=?,updated_at=? WHERE calculation_id=?",
                    (state, now, job_row["calculation_id"]),
                )

    def claim_route_job(self, plan_id: str) -> ClaimedPreviewRouteJob | None:
        with self._write_transaction() as conn:
            job_row = conn.execute(
                """SELECT * FROM preview_route_jobs WHERE plan_id=? AND state='pending'
                   ORDER BY created_at DESC LIMIT 1""",
                (plan_id,),
            ).fetchone()
            if job_row is None:
                return None
            draft_row = conn.execute(
                "SELECT * FROM preview_drafts WHERE plan_id=?", (plan_id,),
            ).fetchone()
            if draft_row is None:
                conn.execute(
                    "UPDATE preview_route_jobs SET state='superseded',updated_at=? WHERE calculation_id=?",
                    (self._now(), job_row["calculation_id"]),
                )
                return None
            record = self._stored_from_row(draft_row)
            if not self._job_matches_record(job_row, record):
                conn.execute(
                    "UPDATE preview_route_jobs SET state='superseded',updated_at=? WHERE calculation_id=?",
                    (self._now(), job_row["calculation_id"]),
                )
                return None
            now = self._now()
            conn.execute(
                "UPDATE preview_route_jobs SET state='running',updated_at=? WHERE calculation_id=? AND state='pending'",
                (now, job_row["calculation_id"]),
            )
            claimed_row = conn.execute(
                "SELECT * FROM preview_route_jobs WHERE calculation_id=?",
                (job_row["calculation_id"],),
            ).fetchone()
            return ClaimedPreviewRouteJob(self._route_job_from_row(claimed_row), record)

    def publish_route_job(
        self,
        plan_id: str,
        calculation_id: str,
        snapshot: PreviewSnapshot,
        *,
        status: str,
        error_code: str | None = None,
    ) -> StoredPreview | None:
        if status not in {"completed", "failed"}:
            raise ValueError("路线任务终态无效")
        updated, timing_json, cost_json = self._validated_snapshot(snapshot)
        preview_json = self._dump_preview(updated)
        with self._write_transaction() as conn:
            job = conn.execute(
                "SELECT * FROM preview_route_jobs WHERE plan_id=? AND calculation_id=?",
                (plan_id, calculation_id),
            ).fetchone()
            row = conn.execute(
                "SELECT * FROM preview_drafts WHERE plan_id=?", (plan_id,),
            ).fetchone()
            if job is None or row is None:
                return None
            current = self._stored_from_row(row)
            if job["state"] != "running":
                return None
            if not self._job_matches_record(job, current):
                conn.execute(
                    "UPDATE preview_route_jobs SET state='superseded',updated_at=? WHERE calculation_id=?",
                    (self._now(), calculation_id),
                )
                return None
            self._check_immutable_identity(current.preview, updated)
            if (
                updated.draft.revision != current.plan_revision
                or updated.calculation.calculation_id != calculation_id
            ):
                raise PreviewIdentityConflict("路线结果必须引用当前 calculation_id 和 plan_revision")
            new_store_version = current.store_version + 1
            timestamp = self._now()
            changed = conn.execute(
                """UPDATE preview_drafts
                   SET preview_json=?,timing_json=?,cost_json=?,store_version=?,updated_at=?
                   WHERE plan_id=? AND store_version=? AND plan_revision=?""",
                (
                    preview_json, timing_json, cost_json, new_store_version,
                    timestamp, plan_id, current.store_version, current.plan_revision,
                ),
            )
            if changed.rowcount != 1:
                return None
            conn.execute(
                """UPDATE preview_route_jobs SET state=?,result_store_version=?,error_code=?,updated_at=?
                   WHERE calculation_id=? AND state='running'""",
                (status, new_store_version, error_code, timestamp, calculation_id),
            )
            stored_row = conn.execute(
                "SELECT * FROM preview_drafts WHERE plan_id=?", (plan_id,),
            ).fetchone()
            return self._stored_from_row(stored_row)

    def replace_preview(
        self,
        access_token: str,
        preview: ItineraryPreview | PreviewSnapshot,
        *,
        expected_store_version: int,
        expected_plan_revision: int,
        command_id: str,
    ) -> DraftWriteResult:
        """通过双版本 CAS 原子替换整份草稿，并按 command_id 幂等重放。"""
        validated, timing_json, cost_json = self._validated_snapshot(preview)
        preview_json = self._dump_preview(validated)
        command_id = self._required_id(command_id, "command_id")
        expected_store_version = self._positive_version(expected_store_version, "expected_store_version")
        expected_plan_revision = self._positive_version(expected_plan_revision, "expected_plan_revision")
        request_hash = self._fingerprint({
            "expected_store_version": expected_store_version,
            "expected_plan_revision": expected_plan_revision,
            "preview_json": preview_json,
            "timing_json": timing_json,
            "cost_json": cost_json,
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
                   SET preview_json=?,timing_json=?,cost_json=?,plan_revision=?,store_version=?,updated_at=?
                   WHERE plan_id=? AND store_version=? AND plan_revision=?""",
                (
                    preview_json,
                    timing_json,
                    cost_json,
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
                       (plan_id,preview_json,timing_json,cost_json,plan_revision,source_store_version,save_id,saved_at)
                       VALUES (?,?,?,?,?,?,?,?)
                       ON CONFLICT(plan_id) DO UPDATE SET
                         preview_json=excluded.preview_json,
                         timing_json=excluded.timing_json,
                         cost_json=excluded.cost_json,
                         plan_revision=excluded.plan_revision,
                         source_store_version=excluded.source_store_version,
                         save_id=excluded.save_id,
                         saved_at=excluded.saved_at""",
                    (
                        plan_id,
                        row["preview_json"],
                        row["timing_json"],
                        row["cost_json"],
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

    def restore_final(
        self,
        access_token: str,
        *,
        restore_id: str,
        expected_store_version: int,
        expected_plan_revision: int,
        expected_save_id: str,
    ) -> PreviewRestoreResult:
        """原子地把最近一次完整成功保存复制为新的草稿版本。"""
        restore_id = self._required_id(restore_id, "restore_id")
        expected_store_version = self._positive_version(expected_store_version, "expected_store_version")
        expected_plan_revision = self._positive_version(expected_plan_revision, "expected_plan_revision")
        expected_save_id = self._required_id(expected_save_id, "expected_save_id")
        request_hash = self._fingerprint({
            "expected_store_version": expected_store_version,
            "expected_plan_revision": expected_plan_revision,
            "expected_save_id": expected_save_id,
        })
        token_hash = self._token_hash(access_token)

        with self._write_transaction() as conn:
            row = conn.execute(
                "SELECT * FROM preview_drafts WHERE access_token_hash=?", (token_hash,)
            ).fetchone()
            if row is None:
                raise PreviewAccessError("预览访问标识无效")
            plan_id = row["plan_id"]
            receipt = conn.execute(
                """SELECT * FROM preview_restore_receipts WHERE plan_id=? AND restore_id=?""",
                (plan_id, restore_id),
            ).fetchone()
            if receipt is not None:
                if receipt["request_hash"] != request_hash:
                    raise IdempotencyConflict("restore_id 已用于不同请求")
                return PreviewRestoreResult(
                    record=self._stored_from_row(row),
                    restored_from=SavedPreviewMetadata(
                        plan_revision=receipt["source_plan_revision"],
                        source_store_version=receipt["source_store_version"],
                        save_id=receipt["source_save_id"],
                        saved_at=receipt["source_saved_at"],
                    ),
                    receipt_store_version=receipt["result_store_version"],
                    replayed=True,
                )

            current = self._stored_from_row(row)
            if (
                current.store_version != expected_store_version
                or current.plan_revision != expected_plan_revision
            ):
                raise PreviewConflict(
                    "草稿版本已变化",
                    current_store_version=current.store_version,
                    current_plan_revision=current.plan_revision,
                )

            final_row = conn.execute(
                "SELECT * FROM preview_finals WHERE plan_id=?", (plan_id,)
            ).fetchone()
            if final_row is None:
                raise PreviewFinalUnavailable("not_saved")
            final_metadata = self._final_metadata_from_row(final_row)
            if final_metadata.save_id != expected_save_id:
                raise PreviewFinalChanged(final_metadata)
            final = self._final_from_row(final_row)
            try:
                source_snapshot = final.snapshot
            except (TypeError, ValueError):
                source_snapshot = None
            if source_snapshot is None:
                raise PreviewFinalUnavailable("snapshot_incomplete")

            restored_snapshot = self._rebind_snapshot_for_restore(
                source_snapshot, base_revision=current.plan_revision,
                calculation_id=(
                    f"restore:{plan_id}:{restore_id}"
                    if source_snapshot.preview.calculation.current_routes == "pending"
                    else source_snapshot.preview.calculation.calculation_id
                ),
            )
            validated, timing_json, cost_json = self._validated_snapshot(restored_snapshot)
            preview_json = self._dump_preview(validated)
            new_store_version = current.store_version + 1
            timestamp = self._now()
            changed = conn.execute(
                """UPDATE preview_drafts
                   SET preview_json=?,timing_json=?,cost_json=?,plan_revision=?,store_version=?,updated_at=?
                   WHERE plan_id=? AND store_version=? AND plan_revision=?""",
                (
                    preview_json,
                    timing_json,
                    cost_json,
                    validated.draft.revision,
                    new_store_version,
                    timestamp,
                    plan_id,
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
            conn.execute(
                """UPDATE preview_route_jobs SET state='superseded',updated_at=?
                   WHERE plan_id=? AND state IN ('pending','running')""",
                (timestamp, plan_id),
            )
            if validated.calculation.current_routes == "pending":
                job_row = conn.execute(
                    "SELECT task_id FROM preview_jobs WHERE plan_id=? AND preview_plan_id=?",
                    (plan_id, plan_id),
                ).fetchone()
                if job_row is None:
                    raise PreviewIdentityConflict("pending 预览还原需要有效任务绑定")
                pending_ids = set(validated.calculation.pending_leg_ids)
                legs_by_id = {leg.leg_id: leg for leg in validated.draft.legs}
                affected_days = tuple(sorted({
                    legs_by_id[leg_id].day_id for leg_id in pending_ids if leg_id in legs_by_id
                }))
                if not affected_days or pending_ids - set(legs_by_id):
                    raise PreviewIdentityConflict("pending 预览还原缺少可恢复的目标日期")
                self._insert_route_job(
                    conn,
                    task_id=job_row["task_id"],
                    plan_id=plan_id,
                    calculation_id=validated.calculation.calculation_id,
                    source_store_version=new_store_version,
                    source_plan_revision=validated.draft.revision,
                    affected_day_ids=affected_days,
                    created_at=timestamp,
                )
            conn.execute(
                """INSERT INTO preview_restore_receipts
                   (plan_id,restore_id,request_hash,result_store_version,source_save_id,source_saved_at,
                    source_store_version,source_plan_revision,created_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    plan_id,
                    restore_id,
                    request_hash,
                    new_store_version,
                    final_metadata.save_id,
                    final_metadata.saved_at,
                    final_metadata.source_store_version,
                    final_metadata.plan_revision,
                    timestamp,
                ),
            )
            self._prune_restore_receipts(conn, plan_id)
            updated_row = conn.execute(
                "SELECT * FROM preview_drafts WHERE plan_id=?", (plan_id,)
            ).fetchone()
            return PreviewRestoreResult(
                record=self._stored_from_row(updated_row),
                restored_from=final_metadata,
                receipt_store_version=new_store_version,
                replayed=False,
            )

    @staticmethod
    def _rebind_snapshot_for_restore(
        snapshot: PreviewSnapshot,
        *,
        base_revision: int,
        calculation_id: str,
    ) -> PreviewSnapshot:
        new_revision = base_revision + 1
        draft = snapshot.preview.draft.model_copy(update={
            "revision": new_revision,
            "base_revision": base_revision,
            "legs": tuple(leg.model_copy(update={"plan_revision": new_revision}) for leg in snapshot.preview.draft.legs),
        })
        validation = snapshot.preview.validation.model_copy(update={
            "based_on_revision": new_revision,
            "issues": tuple(
                issue.model_copy(update={"based_on_revision": new_revision})
                for issue in snapshot.preview.validation.issues
            ),
        })
        preview = snapshot.preview.model_copy(update={
            "draft": draft,
            "validation": validation,
            "calculation": snapshot.preview.calculation.model_copy(update={
                "based_on_revision": new_revision,
                "calculation_id": calculation_id,
            }),
        })
        return PreviewSnapshot.model_validate({
            "preview": preview.model_dump(mode="json"),
            "timing": snapshot.timing.model_copy(update={"based_on_revision": new_revision}).model_dump(mode="json"),
            "cost": snapshot.cost.model_copy(update={"based_on_revision": new_revision}).model_dump(mode="json"),
        })

    @staticmethod
    def _final_metadata_from_row(row: sqlite3.Row) -> SavedPreviewMetadata:
        return SavedPreviewMetadata(
            plan_revision=row["plan_revision"],
            source_store_version=row["source_store_version"],
            save_id=row["save_id"],
            saved_at=row["saved_at"],
        )

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

    def _prune_restore_receipts(self, conn: sqlite3.Connection, plan_id: str) -> None:
        expired = conn.execute(
            """SELECT restore_seq FROM preview_restore_receipts WHERE plan_id=?
               ORDER BY restore_seq DESC LIMIT -1 OFFSET ?""",
            (plan_id, self.receipt_limit),
        ).fetchall()
        if expired:
            conn.executemany(
                "DELETE FROM preview_restore_receipts WHERE restore_seq=?",
                ((row["restore_seq"],) for row in expired),
            )

    def _prune_edit_receipts(self, conn: sqlite3.Connection, plan_id: str) -> None:
        expired = conn.execute(
            """SELECT edit_seq FROM preview_edit_receipts WHERE plan_id=?
               ORDER BY edit_seq DESC LIMIT -1 OFFSET ?""",
            (plan_id, self.receipt_limit),
        ).fetchall()
        if expired:
            conn.executemany(
                "DELETE FROM preview_edit_receipts WHERE edit_seq=?",
                ((row["edit_seq"],) for row in expired),
            )

    @staticmethod
    def _pending_days(preview: ItineraryPreview) -> tuple[str, ...]:
        if preview.calculation.current_routes != "pending":
            return ()
        legs_by_id = {leg.leg_id: leg for leg in preview.draft.legs}
        return tuple(sorted({
            legs_by_id[leg_id].day_id
            for leg_id in preview.calculation.pending_leg_ids
            if leg_id in legs_by_id
        }))

    @staticmethod
    def _job_matches_record(job_row: sqlite3.Row, record: StoredPreview) -> bool:
        preview = record.preview
        if (
            job_row["source_store_version"] != record.store_version
            or job_row["source_plan_revision"] != record.plan_revision
            or preview.calculation.calculation_id != job_row["calculation_id"]
            or preview.calculation.current_routes != "pending"
        ):
            return False
        try:
            job_days = set(json.loads(job_row["affected_day_ids_json"]))
        except (TypeError, ValueError):
            return False
        return bool(job_days) and job_days.issubset(set(PreviewRepository._pending_days(preview)))

    @staticmethod
    def _insert_route_job(
        conn: sqlite3.Connection,
        *,
        task_id: str,
        plan_id: str,
        calculation_id: str,
        source_store_version: int,
        source_plan_revision: int,
        affected_day_ids: tuple[str, ...],
        created_at: str,
    ) -> None:
        conn.execute(
            """INSERT INTO preview_route_jobs
               (calculation_id,task_id,plan_id,source_store_version,source_plan_revision,
                affected_day_ids_json,state,created_at,updated_at)
               VALUES (?,?,?,?,?,?,'pending',?,?)""",
            (
                calculation_id,
                task_id,
                plan_id,
                source_store_version,
                source_plan_revision,
                json.dumps(affected_day_ids, ensure_ascii=False, separators=(",", ":")),
                created_at,
                created_at,
            ),
        )

    @staticmethod
    def _route_job_from_row(row: sqlite3.Row) -> PreviewRouteJob:
        return PreviewRouteJob(
            task_id=row["task_id"],
            plan_id=row["plan_id"],
            calculation_id=row["calculation_id"],
            status=row["state"],
            source_store_version=row["source_store_version"],
            source_plan_revision=row["source_plan_revision"],
            affected_day_ids=tuple(json.loads(row["affected_day_ids_json"])),
            result_store_version=row["result_store_version"],
            error_code=row["error_code"],
            updated_at=row["updated_at"],
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

    @classmethod
    def _validated_snapshot(
        cls, value: ItineraryPreview | PreviewSnapshot
    ) -> tuple[ItineraryPreview, str | None, str | None]:
        if isinstance(value, PreviewSnapshot):
            snapshot = value
        else:
            snapshot = None
        preview = cls._validated_preview(snapshot.preview if snapshot else value)
        if snapshot is None:
            return preview, None, None
        validated = PreviewSnapshot.model_validate(snapshot.model_dump(mode="json"))
        return (
            preview,
            cls._dump_model(validated.timing),
            cls._dump_model(validated.cost),
        )

    @staticmethod
    def _dump_model(value) -> str:
        return json.dumps(value.model_dump(mode="json"), ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False)

    @staticmethod
    def _timing_from_json(value: str | None) -> PlanTimingResult | None:
        return PlanTimingResult.model_validate_json(value) if value else None

    @staticmethod
    def _cost_from_json(value: str | None) -> ReferenceCostSummary | None:
        return ReferenceCostSummary.model_validate_json(value) if value else None

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
            timing=self._timing_from_json(row["timing_json"]),
            cost=self._cost_from_json(row["cost_json"]),
        )

    def _final_from_row(self, row: sqlite3.Row) -> SavedPreview:
        preview = self._preview_from_json(row["preview_json"])
        return SavedPreview(
            preview=preview,
            plan_revision=row["plan_revision"],
            source_store_version=row["source_store_version"],
            save_id=row["save_id"],
            saved_at=row["saved_at"],
            timing=self._timing_from_json(row["timing_json"]),
            cost=self._cost_from_json(row["cost_json"]),
        )

    @staticmethod
    def _job_from_row(row: sqlite3.Row) -> dict:
        return {
            "task_id": row["task_id"],
            "plan_id": row["plan_id"],
            "status": row["generation_state"],
            "stage": row["generation_stage"],
            "completed": row["generation_completed"],
            "total": row["generation_total"],
            "message": row["generation_message"],
            "backfill": {
                "status": row["backfill_state"],
                "completed": row["backfill_completed"],
                "total": row["backfill_total"],
            },
            "preview_ready": row["preview_plan_id"] is not None,
            "store_version": row["actual_store_version"],
            "error_code": row["error_code"],
            "updated_at": row["updated_at"],
        }

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
