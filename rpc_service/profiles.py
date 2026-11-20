"""Risk presets. Thresholds are divisors on config.yaml's own min_native."""
from __future__ import annotations

from dataclasses import asdict, dataclass


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
    note: str


PRESETS = {
    "safe": Profile("safe", ("ethereum", "bsc", "solana"), True, 1, 8, 2, 25, 18.0, 4, 40,
                    False, 10, "low load, repo thresholds, native only"),
    "aggressive": Profile("aggressive", ("ethereum", "bsc", "polygon", "base", "solana"),
                          False, 2, 32, 4, 60, 14.0, 4, 25, False, 10,
                          "5 chains, tokens on funded, half thresholds"),
    "brutal": Profile("brutal", ("ethereum", "bsc", "polygon", "base", "arbitrum",
                                 "optimism", "avalanche", "solana"),
                      False, 4, 96, 8, 120, 10.0, 3, 10, True, 12,
                      "all 8 chains, deep funded scan, quarter thresholds"),
    "insane": Profile("insane", ("ethereum", "bsc", "polygon", "base", "arbitrum",
                                 "optimism", "avalanche", "solana"),
                      False, 10, 256, 16, 250, 8.0, 2, 0, True, 16,
                      "max throughput, tenth thresholds, expects 429s"),
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