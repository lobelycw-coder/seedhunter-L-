"""Supervisor: API + engine + bots, each as a child process."""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def spawn(args: list[str]) -> subprocess.Popen:
    return subprocess.Popen(args, cwd=ROOT)


def main() -> None:
    ap = argparse.ArgumentParser(prog="launch")
    ap.add_argument("--api", action="store_true")
    ap.add_argument("--engine", action="store_true")
    ap.add_argument("--hunter", action="store_true")
    ap.add_argument("--bot", action="store_true")
    ap.add_argument("--port", default=os.getenv("SH_PORT", "8080"))
    a = ap.parse_args()
    if not any([a.api, a.engine, a.hunter, a.bot]):
        ap.error("pick at least one of --api --engine --hunter --bot")

    children: list[subprocess.Popen] = []
    if a.api:
        children.append(spawn([sys.executable, "-m", "uvicorn", "rpc_service.app:app",
                               "--host", os.getenv("SH_HOST", "127.0.0.1"),
                               "--port", a.port, "--proxy-headers"]))
    if a.engine:
        children.append(spawn([sys.executable, "-m", "rpc_service.brutal", "-p",
                               os.getenv("SH_PROFILE", "brutal"), "--notify"]))
    if a.hunter:
        children.append(spawn([sys.executable, "-m", "seedhunter.cli", "hunt"]))
    if a.bot:
        children.append(spawn([sys.executable, "-m", "rpc_service.dashboard_bot"]))

    def shutdown(*_):
        for c in children:
            if c.poll() is None:
                c.terminate()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    print(f"supervising {len(children)} process(es); ctrl-c to stop")
    try:
        while children:
            for c in list(children):
                if c.poll() is not None:
                    print(f"pid {c.pid} exited {c.returncode}")
                    children.remove(c)
            time.sleep(1)
    except KeyboardInterrupt:
        shutdown()


if __name__ == "__main__":
    main()