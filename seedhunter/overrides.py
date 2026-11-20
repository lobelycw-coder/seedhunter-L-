"""Runtime overrides written by the Telegram bot."""
from __future__ import annotations

import json
from pathlib import Path

PATH = Path(__file__).resolve().parent.parent / "data" / "overrides.json"
ALLOWED = {"dry_run", "destination_evm", "destination_sol", "family", "shard",
           "min_gain_ratio", "workers", "sweep_enabled"}


def load() -> dict:
    try:
        return json.loads(PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save(key: str, value: str) -> str:
    if key not in ALLOWED:
        return f"unknown key. allowed: {', '.join(sorted(ALLOWED))}"
    data = load()
    if key in ("dry_run", "sweep_enabled"):
        data[key] = value.lower() in ("1", "on", "true", "yes")
    elif key in ("shard", "workers"):
        data[key] = int(value)
    elif key == "min_gain_ratio":
        data[key] = float(value)
    else:
        data[key] = value
    PATH.parent.mkdir(parents=True, exist_ok=True)
    PATH.write_text(json.dumps(data, indent=2))
    return f"{key} = {data[key]} (restart to apply)"


def apply(s) -> None:
    d = load()
    for k, v in d.items():
        if k == "dry_run":
            s.sweep.dry_run = v
        elif k == "sweep_enabled":
            s.sweep.enabled = v
        elif hasattr(s, k):
            setattr(s, k, v)