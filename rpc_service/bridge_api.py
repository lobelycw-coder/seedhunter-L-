"""Bridge routes + hunter supervision. Never edits a project file."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from fastapi import Depends, FastAPI

from . import bridge
from .settings import Settings

ROOT = Path(__file__).resolve().parent.parent
_proc: dict = {"p": None}


def register(app: FastAPI, cfg: Settings, guard) -> None:
    @app.get("/api/bridge/status")
    async def bridge_status():
        return bridge.status()

    @app.get("/api/bridge/hits")
    async def bridge_hits(limit: int = 25):
        return {"hits": bridge.recent_hits(limit), "total": bridge.hit_count()}

    @app.get("/api/bridge/sweeps")
    async def bridge_sweeps():
        return {"sweeps": bridge.sweeps()}

    @app.get("/api/engine/stats")
    async def engine_stats():
        try:
            return json.loads((Path(__file__).resolve().parent / "data"
                               / "brutal_stats.json").read_text())
        except Exception:
            return {"running": False, "reason": "no heartbeat"}

    @app.get("/api/hunter")
    async def hunter_state():
        p = _proc["p"]
        return {"running": bool(p and p.poll() is None),
                "pid": p.pid if p and p.poll() is None else None}

    @app.post("/api/hunter/start", dependencies=[Depends(guard)])
    async def hunter_start():
        p = _proc["p"]
        if p and p.poll() is None:
            return {"running": True, "pid": p.pid}
        _proc["p"] = subprocess.Popen([sys.executable, "-m", "seedhunter.cli", "hunt"],
                                      cwd=str(ROOT))
        return {"running": True, "pid": _proc["p"].pid}

    @app.post("/api/hunter/stop", dependencies=[Depends(guard)])
    async def hunter_stop():
        p = _proc["p"]
        if not p or p.poll() is not None:
            return {"running": False}
        p.terminate()
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            p.kill()
        return {"running": False}