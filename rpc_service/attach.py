"""In-process hook. Enabled only with SH_ATTACH=1."""
from __future__ import annotations

import os
import sys

INSTALLED = False


def install() -> bool:
    global INSTALLED
    if INSTALLED or os.getenv("SH_ATTACH", "") not in ("1", "true", "yes"):
        return False
    try:
        from . import bridge
        bridge.status()
        INSTALLED = True
        return True
    except Exception:
        return False


def attached() -> bool:
    return INSTALLED


def summary() -> dict:
    from . import bridge
    return {"attached": INSTALLED, "python": sys.version.split()[0],
            "bridge": bridge.status()}