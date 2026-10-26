"""只读导出单次任务诊断；不会发起规划、模型或地图调用。"""
import argparse
import json
import os
from pathlib import Path
import sys

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from app.services.task_diagnostics import build_task_report, render_task_report  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task_id")
    storage = parser.add_mutually_exclusive_group()
    storage.add_argument("--db", type=Path, default=BACKEND_ROOT / "data/tasks.sqlite3", help="只读SQLite备份路径")
    storage.add_argument("--postgres", action="store_true", help="只读DATABASE_URL指定的PostgreSQL库")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args()
    source = args.db
    if args.postgres:
        from dotenv import load_dotenv
        load_dotenv(BACKEND_ROOT / '.env', override=False)
        source = os.environ.get('DATABASE_URL', '')
        if not source.startswith(('postgresql://', 'postgres://')):
            print('无法读取诊断：请配置有效的PostgreSQL DATABASE_URL。', file=sys.stderr)
            return 2
        from app.services.relational_storage import StorageDatabase
        source = StorageDatabase(source, 'observation', schema_prefix=os.environ.get('DATABASE_SCHEMA_PREFIX', 'trip'))
    try:
        report = build_task_report(source, args.task_id)
    except Exception:
        # 诊断命令只返回固定错误，不让驱动错误附带连接信息或完整SQL内容。
        print("无法读取诊断：请检查数据库配置、任务 ID 和数据库结构。", file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2) if args.format == "json" else render_task_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
