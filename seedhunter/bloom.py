"""In-memory bloom filter to skip already-seen hits."""
from __future__ import annotations

import hashlib

BITS = 1 << 25
MASK = BITS - 1


class Bloom:
    def __init__(self):
        self.arr = bytearray(BITS >> 3)
        self.n = 0

    def _idx(self, value: str):
        h = hashlib.blake2b(value.encode(), digest_size=8).digest()
        a = int.from_bytes(h[:4], "big") & MASK
        b = int.from_bytes(h[4:], "big") & MASK
        yield a
        yield (a + b) & MASK
        yield (a + (b << 1)) & MASK

    def add(self, value: str) -> bool:
        """Return True if already present."""
        seen = True
        for i in self._idx(value):
            if not self.arr[i >> 3] & (1 << (i & 7)):
                seen = False
                self.arr[i >> 3] |= 1 << (i & 7)
        if not seen:
            self.n += 1
        return seen

    def __len__(self) -> int:
        return self.n