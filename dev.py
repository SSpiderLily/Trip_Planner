#!/usr/bin/env python3
"""从项目根目录同时启动本地后端和前端开发服务器。"""

import os
from pathlib import Path
import platform
import shutil
import signal
import subprocess
import sys
import time
from typing import List, Optional


ROOT = Path(__file__).resolve().parent
BACKEND = ROOT / "backend"
FRONTEND = ROOT / "frontend"
PYTHON = BACKEND / "venv" / "bin" / "python"
VITE = FRONTEND / "node_modules" / "vite" / "bin" / "vite.js"


def check_prerequisites() -> str:
    """只检查依赖是否存在，不读取或输出环境变量内容。"""
    if not PYTHON.is_file():
        raise RuntimeError("未找到 backend/venv/bin/python；请先在 backend 创建虚拟环境并安装依赖。")
    if not VITE.is_file():
        raise RuntimeError("未找到前端 Vite；请先在 frontend 执行 npm install。")
    node = shutil.which("node")
    if node is None:
        raise RuntimeError("未找到 node；请先安装 Node.js。")
    return node


def backend_command() -> List[str]:
    """在 Apple Silicon 上为 arm64 虚拟环境避开 Rosetta 解释器。"""
    command = [str(PYTHON)]
    if platform.system() == "Darwin" and shutil.which("arch"):
        probe = [str(PYTHON), "-c", "import pydantic_core"]
        native = subprocess.run(probe, cwd=BACKEND, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if native.returncode != 0:
            arm_command = ["/usr/bin/arch", "-arm64", str(PYTHON)]
            arm = subprocess.run(
                arm_command + ["-c", "import pydantic_core"],
                cwd=BACKEND,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if arm.returncode == 0:
                command = arm_command
                print("检测到 arm64 后端依赖，使用 arm64 Python 启动。", flush=True)
    return command + ["-m", "uvicorn", "app.api.main:app", "--host", "127.0.0.1", "--port", "8000"]


def stop_processes(processes: List[subprocess.Popen]) -> None:
    """结束各服务的整个进程组。"""
    for process in processes:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass

    for process in processes:
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()


def main() -> int:
    try:
        node = check_prerequisites()
    except RuntimeError as error:
        print(f"启动失败：{error}", file=sys.stderr)
        return 1

    if not (BACKEND / ".env").exists():
        print("提示：未找到 backend/.env，后端可能因缺少配置而无法启动。", flush=True)
    if not (FRONTEND / ".env").exists():
        print("提示：未找到 frontend/.env，地图可能无法加载。", flush=True)
    if shutil.which("uvx") is None:
        print("提示：未找到 uvx，旅行规划的高德 MCP 工具可能无法使用。", flush=True)

    print("后端：http://127.0.0.1:8000/docs", flush=True)
    print("前端：http://127.0.0.1:5173", flush=True)
    print("按 Ctrl+C 同时停止两个服务。\n", flush=True)

    commands = [
        (
            "后端",
            backend_command(),
            BACKEND,
        ),
        ("前端", [node, str(VITE), "--host", "127.0.0.1", "--strictPort"], FRONTEND),
    ]
    processes: List[subprocess.Popen] = []
    stop_signal: Optional[int] = None

    def request_stop(signum: int, _frame: object) -> None:
        nonlocal stop_signal
        stop_signal = signum

    previous_int = signal.signal(signal.SIGINT, request_stop)
    previous_term = signal.signal(signal.SIGTERM, request_stop)
    try:
        for name, command, directory in commands:
            process = subprocess.Popen(command, cwd=directory, start_new_session=True)
            processes.append(process)
            print(f"已启动{name}，PID {process.pid}", flush=True)

        while stop_signal is None:
            for (name, _, _), process in zip(commands, processes):
                exit_code = process.poll()
                if exit_code is not None:
                    print(f"{name}已退出（退出码 {exit_code}），正在停止另一服务。", file=sys.stderr)
                    return exit_code if exit_code != 0 else 1
            time.sleep(0.25)
        return 128 + stop_signal
    except OSError as error:
        print(f"启动服务失败：{error}", file=sys.stderr)
        return 1
    finally:
        stop_processes(processes)
        signal.signal(signal.SIGINT, previous_int)
        signal.signal(signal.SIGTERM, previous_term)
        print("前后端已停止。", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
