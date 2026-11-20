"""Deterministic seed enumeration with checkpointing. No sampling, no repeats."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from .derive import WORDLIST

H = 0x80000000
FAMILIES = ("repeat", "pair", "pool", "sequential")


def _checksum(indices: list[int], free7: int) -> int:
    bits = 0
    for i in indices:
        bits = (bits << 11) | i
    bits = (bits << 7) | free7
    return (free7 << 4) | (hashlib.sha256(bits.to_bytes(16, "big")).digest()[0] >> 4)


def _entropy_phrase(counter: int) -> str:
    """Sequential family: counter is the 128-bit entropy, walked in order."""
    entropy = (counter % (1 << 128)).to_bytes(16, "big")
    check = hashlib.sha256(entropy).digest()[0] >> 4
    bits = int.from_bytes(entropy, "big") << 4 | check
    idx = [(bits >> (11 * (11 - i))) & 0x7FF for i in range(12)]
    return " ".join(WORDLIST[i] for i in idx)


class Cursor:
    """Monotonic, shard-safe, resumable."""

    def __init__(self, family: str, pool: int, shard: int, shards: int, state: Path):
        if family not in FAMILIES:
            raise SystemExit(f"family must be one of {FAMILIES}")
        self.family = family
        self.pool = max(16, min(pool, 2048))
        self.shard = shard
        self.shards = max(1, shards)
        self.state = state
        self.span = (1 << 128) // self.shards
        self.n = self._load()
        self.saved = self.n

    def _load(self) -> int:
        base = self.shard * self.span
        if self.state.exists():
            try:
                d = json.loads(self.state.read_text())
                if d.get("family") == self.family and d.get("pool") == self.pool:
                    return max(int(d.get("n", 0)), base)
            except (json.JSONDecodeError, ValueError, KeyError):
                pass
        return base

    def save(self) -> None:
        if self.n == self.saved:
            return
        self.state.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state.with_suffix(".tmp")
        tmp.write_text(json.dumps(
            {"family": self.family, "pool": self.pool, "n": self.n,
             "shard": self.shard, "shards": self.shards}))
        os.replace(tmp, self.state)
        self.saved = self.n

    def next(self) -> str:
        c = self.n
        self.n += 1
        f = self.family
        if f == "sequential":
            return _entropy_phrase(c)
        if f == "repeat":
            w = c % 2048
            idx = [w] * 11
        elif f == "pair":
            a, b = c % 2048, (c // 2048) % 2048
            idx = [a if i % 2 == 0 else b for i in range(11)]
        else:
            idx, r = [], c
            for _ in range(11):
                r, m = divmod(r, self.pool)
                idx.append(m)
        free = (c >> 11) & 0x7F
        idx.append(_checksum(idx, free))
        return " ".join(WORDLIST[i] for i in idx)

    def batch(self, n: int) -> list[str]:
        return [self.next() for _ in range(n)]

    def progress(self) -> float:
        return (self.n - self.shard * self.span) / self.span * 100