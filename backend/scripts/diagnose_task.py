"""只读导出单次任务诊断；不会发起规划、模型或地图调用。"""
import argparse
import json
from pathlib import Path
import sqlite3
import sys

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from app.services.task_diagnostics import build_task_report, render_task_report  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task_id")
    parser.add_argument("--db", type=Path, default=BACKEND_ROOT / "data/tasks.sqlite3")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args()
    try:
        report = build_task_report(args.db, args.task_id)
    except (OSError, sqlite3.Error, LookupError):
        print("无法读取诊断：请检查数据库路径、任务 ID 和数据库结构。", file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2) if args.format == "json" else render_task_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
