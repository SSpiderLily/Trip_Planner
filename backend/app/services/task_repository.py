"""SQLite 存储：每次调用独立连接，业务事务与观测写入分开。"""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path


def now():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


def dumps(value):
    return json.dumps(value, ensure_ascii=False, default=str)


SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
 task_id TEXT PRIMARY KEY NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('accepted','running','succeeded','failed','interrupted')),
 request_data TEXT NOT NULL, created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT,
 interruption_detected_at TEXT, error_code TEXT, error_message TEXT, error_step TEXT,
 observation_incomplete INTEGER NOT NULL DEFAULT 0 CHECK(observation_incomplete IN (0,1))
);
CREATE TABLE IF NOT EXISTS task_results (
 task_id TEXT PRIMARY KEY NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
 result_data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS spans (
 span_id TEXT PRIMARY KEY NOT NULL,
 task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
 parent_span_id TEXT, name TEXT NOT NULL,
 operation_type TEXT NOT NULL CHECK(operation_type IN ('planning','agent','llm','tool','validation')),
 status TEXT NOT NULL CHECK(status IN ('running','succeeded','failed','interrupted')),
 started_at TEXT NOT NULL, finished_at TEXT, input_data TEXT, output_data TEXT, error TEXT,
 input_truncated INTEGER NOT NULL DEFAULT 0 CHECK(input_truncated IN (0,1)),
 output_truncated INTEGER NOT NULL DEFAULT 0 CHECK(output_truncated IN (0,1)),
 UNIQUE(task_id,span_id), CHECK(parent_span_id IS NULL OR parent_span_id <> span_id),
 FOREIGN KEY(task_id,parent_span_id) REFERENCES spans(task_id,span_id) DEFERRABLE INITIALLY DEFERRED
);
CREATE INDEX IF NOT EXISTS spans_by_parent ON spans(task_id,parent_span_id);
CREATE INDEX IF NOT EXISTS tasks_by_created ON tasks(created_at);
"""
SUMMARY = 'task_id,status,created_at,started_at,finished_at,interruption_detected_at,error_code,error_message,error_step,observation_incomplete'
SPAN_SUMMARY = 'span_id,task_id,parent_span_id,name,operation_type,status,started_at,finished_at,input_truncated,output_truncated'


class TaskRepository:
    def __init__(self, path, retention_days=7, max_bytes=200*1024*1024):
        self.path = Path(path)
        self.retention_days, self.max_bytes = retention_days, max_bytes

    @contextmanager
    def connection(self):
        conn = sqlite3.connect(self.path, timeout=1)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as conn:
            conn.execute('PRAGMA journal_mode=WAL')
            conn.executescript(SCHEMA)
        self.recover()
        self.cleanup()

    def recover(self):
        with self.connection() as conn:
            conn.execute("UPDATE spans SET status='interrupted' WHERE status='running'")
            conn.execute("""UPDATE tasks SET status='interrupted', interruption_detected_at=?,
                error_code='PROCESS_INTERRUPTED', error_message='后端重启，任务未完成',
                observation_incomplete=1 WHERE status IN ('accepted','running')""", (now(),))

    def create(self, task_id, data):
        with self.connection() as conn:
            conn.execute("INSERT INTO tasks(task_id,status,request_data,created_at) VALUES (?,'accepted',?,?)",
                         (task_id, dumps(data), now()))

    def start(self, task_id):
        with self.connection() as conn:
            row = conn.execute("UPDATE tasks SET status='running',started_at=? WHERE task_id=? AND status='accepted'", (now(), task_id))
            if row.rowcount != 1:
                raise RuntimeError('任务状态转换冲突')

    def succeed(self, task_id, result, incomplete=False):
        with self.connection() as conn:
            row = conn.execute("UPDATE tasks SET status='succeeded',finished_at=?,observation_incomplete=MAX(observation_incomplete,?) WHERE task_id=? AND status='running'", (now(), int(incomplete), task_id))
            if row.rowcount != 1:
                raise RuntimeError('任务状态转换冲突')
            conn.execute('INSERT INTO task_results VALUES (?,?)', (task_id, dumps(result)))

    def fail(self, task_id, code, message, step=None, incomplete=False):
        with self.connection() as conn:
            conn.execute("""UPDATE tasks SET status='failed',finished_at=?,error_code=?,error_message=?,error_step=?,
                observation_incomplete=MAX(observation_incomplete,?) WHERE task_id=? AND status IN ('accepted','running')""",
                (now(), code, message, step, int(incomplete), task_id))

    def mark_incomplete(self, task_id):
        with self.connection() as conn:
            conn.execute('UPDATE tasks SET observation_incomplete=1 WHERE task_id=?', (task_id,))

    def status(self, task_id):
        with self.connection() as conn:
            row = conn.execute(f'SELECT {SUMMARY} FROM tasks WHERE task_id=?', (task_id,)).fetchone()
            if row is None:
                return None
            result = dict(row)
            current = None
            if result['status'] == 'running':
                current = conn.execute("""SELECT s.span_id,s.name,s.started_at FROM spans s
                    WHERE s.task_id=? AND s.status='running' AND NOT EXISTS
                    (SELECT 1 FROM spans c WHERE c.parent_span_id=s.span_id AND c.task_id=s.task_id AND c.status='running')
                    ORDER BY s.started_at DESC LIMIT 1""", (task_id,)).fetchone()
            result['current_step'] = dict(current) if current else None
            return result

    def result(self, task_id):
        with self.connection() as conn:
            row = conn.execute('SELECT result_data FROM task_results WHERE task_id=?', (task_id,)).fetchone()
            return json.loads(row[0]) if row else None

    def list_tasks(self, status=None, limit=50, offset=0, failed_ids=()):
        with self.connection() as conn:
            where, params = ('WHERE status=?', [status]) if status else ('', [])
            if status and failed_ids:
                placeholders = ','.join('?' for _ in failed_ids)
                if status == 'failed':
                    where = f'WHERE (status=? OR task_id IN ({placeholders}))'
                else:
                    where = f'WHERE status=? AND task_id NOT IN ({placeholders})'
                params.extend(failed_ids)
            return [dict(row) for row in conn.execute(f'SELECT {SUMMARY} FROM tasks {where} ORDER BY created_at DESC LIMIT ? OFFSET ?', (*params, limit, offset))]

    def begin_span(self, record):
        with self.connection() as conn:
            conn.execute('''INSERT INTO spans(span_id,task_id,parent_span_id,name,operation_type,status,started_at,input_data,input_truncated)
                VALUES (?,?,?,?,?,'running',?,?,?)''', record)

    def end_span(self, span_id, status, output, truncated, error):
        with self.connection() as conn:
            conn.execute("UPDATE spans SET status=?,finished_at=?,output_data=?,output_truncated=?,error=? WHERE span_id=? AND status='running'",
                         (status, now(), output, int(truncated), dumps(error) if error else None, span_id))

    def spans(self, task_id):
        with self.connection() as conn:
            return [dict(row) for row in conn.execute(f'SELECT {SPAN_SUMMARY} FROM spans WHERE task_id=? ORDER BY started_at', (task_id,))]

    def span(self, task_id, span_id):
        with self.connection() as conn:
            row = conn.execute('SELECT * FROM spans WHERE task_id=? AND span_id=?', (task_id, span_id)).fetchone()
            if not row:
                return None
            value = dict(row)
            for key in ('input_data', 'output_data', 'error'):
                value[key] = json.loads(value[key]) if value[key] is not None else None
            return value

    def size(self):
        return sum(p.stat().st_size for p in [self.path, Path(str(self.path)+'-wal'), Path(str(self.path)+'-shm')] if p.exists())

    def cleanup(self):
        cutoff = (datetime.now(timezone.utc)-timedelta(days=self.retention_days)).isoformat(timespec='microseconds')
        with self.connection() as conn:
            conn.execute("DELETE FROM tasks WHERE status NOT IN ('accepted','running') AND COALESCE(finished_at,interruption_detected_at)<?", (cutoff,))
        # 检查点/空间回收只在任务开始或结束清理时做，不在每个 Span 上做。
        self.compact()
        while self.size() > self.max_bytes:
            with self.connection() as conn:
                row = conn.execute("SELECT task_id FROM tasks WHERE status NOT IN ('accepted','running') ORDER BY COALESCE(finished_at,interruption_detected_at) LIMIT 1").fetchone()
                if not row:
                    break
                conn.execute('DELETE FROM tasks WHERE task_id=?', (row[0],))
            self.compact()

    def compact(self):
        with self.connection() as conn:
            conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            if conn.execute('PRAGMA freelist_count').fetchone()[0]:
                conn.execute('VACUUM')
