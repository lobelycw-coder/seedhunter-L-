"""Pre-flight briefing and gate."""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import httpx

from . import bridge, profiles

DATA = Path(__file__).resolve().parent / "data"
ACTIVE = DATA / "active_profile.json"


async def probe(chain: str, url: str, timeout: float) -> dict:
    method = "getHealth" if chain == "solana" else "eth_chainId"
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(url, json={"jsonrpc": "2.0", "id": 1,
                                             "method": method, "params": []})
        ms = int((time.perf_counter() - started) * 1000)
        return {"chain": chain, "ok": r.status_code == 200 and "result" in r.json(),
                "ms": ms, "status": r.status_code}
    except Exception as exc:
        return {"chain": chain, "ok": False, "ms": None, "error": type(exc).__name__}


def _seed_space(cfg: dict) -> int | None:
    if cfg.get("family") in ("repeat", "sequential") and cfg.get("word_pool"):
        return int(cfg["word_pool"])
    return None


def brief(cfg: dict, profile, chains: list[str], per_seed: int) -> dict:
    cost = float(os.getenv("SH_COST_PER_MILLION", "0") or 0)
    return {"profile": profile.name, "note": profile.note,
            "chains": [{"chain": c,
                        "min_native": max(1, int(((cfg.get("chains") or {}).get(c) or {})
                                                 .get("min_native", 1)) // profile.threshold_div),
                        "endpoints": len([e for e in (((cfg.get("chains") or {}).get(c) or {})
                                                      .get("endpoints") or []) if e])}
                       for c in chains],
            "addrs_per_seed": per_seed * len(chains), "workers": profile.workers,
            "farm_processes": profile.farm_processes, "batch": profile.batch,
            "seed_space": _seed_space(cfg), "cost_per_million": cost,
            "tokens_on_funded": not profile.native_only, "deep_funded": profile.deep_funded}


def render(cfg: dict, profile, chains: list[str], probes: list[dict]) -> None:
    b = brief(cfg, profile, chains, profile.per_seed)
    print("\n" + "=" * 66)
    print(f" PRE-FLIGHT  profile={b['profile']}  ({b['note']})")
    print("=" * 66)
    print(f" addresses/seed : {b['addrs_per_seed']} across {len(chains)} chain(s)")
    print(f" concurrency    : {b['workers']} checkers, {b['farm_processes']} derive procs,"
          f" batch {b['batch']}")
    print(f" token scan     : {'funded only' if b['tokens_on_funded'] else 'off'}")
    print(f" deep scan      : {'on' if b['deep_funded'] else 'off'}")
    print(f"\n thresholds (min_native ÷{profile.threshold_div}):")
    for line in b["chains"]:
        print(f"   {line['chain']:<11} {line['min_native']:<24} {line['endpoints']} endpoint(s)")
    print("\n probes:")
    dead = 0
    for p in probes:
        if p["ok"]:
            print(f"   OK   {p['chain']:<11} {p['ms']} ms")
        else:
            dead += 1
            print(f"   FAIL {p['chain']:<11} {p.get('error') or p.get('status')}")
    print(f"\n keyspace: {('%d seeds' % b['seed_space']) if b['seed_space'] else 'unbounded'}")
    print(" cost    : " + (("%.2f per 1M" % b["cost_per_million"])
                           if b["cost_per_million"] else "unset (SH_COST_PER_MILLION)"))
    print(f" dead endpoints: {dead}")
    print("=" * 66 + "\n")


async def run(args) -> int:
    cfg = bridge.repo_config()
    if not cfg:
        print("config.yaml not found — run from inside the repository")
        return 2
    profile = profiles.get(args.profile)
    chains = [c for c in (args.chains.split(",") if args.chains else profile.chains)
              if c in (cfg.get("chains") or {})]
    pairs = [(c, e) for c in chains
             for e in (((cfg.get("chains") or {}).get(c) or {}).get("endpoints") or []) if e]
    probes = list(await asyncio.gather(*(probe(c, u, 6.0) for c, u in pairs[:24])))
    render(cfg, profile, chains, probes)
    alive = {p["chain"] for p in probes if p["ok"]}
    usable = [c for c in chains if c in alive]
    if not usable:
        print("no chain answered — aborting")
        return 3
    if not args.yes:
        if input(f"start {profile.name} on {len(usable)} chain(s)? type START: ").strip() != "START":
            print("aborted")
            return 1
    DATA.mkdir(parents=True, exist_ok=True)
    ACTIVE.write_text(json.dumps({"profile": profile.name, "chains": ",".join(usable),
                                  "shard": args.shard, "shards": args.shards,
                                  "duration": args.duration, "ts": int(time.time())}))
    print(f"wrote {ACTIVE}")
    return 0


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(prog="preflight")
    ap.add_argument("-p", "--profile", default="brutal", choices=list(profiles.PRESETS))
    ap.add_argument("-c", "--chains", default="")
    ap.add_argument("-s", "--shard", type=int, default=0)
    ap.add_argument("--shards", type=int, default=1)
    ap.add_argument("-d", "--duration", type=int, default=0)
    ap.add_argument("-y", "--yes", action="store_true")
    raise SystemExit(asyncio.run(run(ap.parse_args())))


if __name__ == "__main__":
    main()