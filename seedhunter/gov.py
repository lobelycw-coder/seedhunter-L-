from __future__ import annotations

import asyncio
import time


class Governor:
    def __init__(self, start: int = 6, ceiling: int = 160, floor: int = 1):
        self.limit, self.ceiling, self.floor = start, ceiling, floor
        self.inflight = 0
        self._c = asyncio.Condition()
        self._s = 0
        self.cuts = 0

    async def acquire(self) -> None:
        async with self._c:
            while self.inflight >= self.limit:
                await self._c.wait()
            self.inflight += 1

    async def release(self, ok: bool) -> None:
        async with self._c:
            self.inflight -= 1
            if ok:
                self._s += 1
                if self._s >= 30 and self.limit < self.ceiling:
                    self.limit += 2
                    self._s = 0
            else:
                self._s = 0
            self._c.notify(2)


class Breaker:
    def __init__(self, threshold: int = 5, cooldown: float = 15.0):
        self.fails, self.threshold, self.cooldown = 0, threshold, cooldown
        self.until = 0.0
        self.latency = 0.0

    def ok(self) -> bool:
        return time.time() >= self.until

    def success(self, dt: float) -> None:
        self.fails = 0
        self.latency = self.latency * 0.7 + dt * 0.3

    def fail(self) -> None:
        self.fails += 1
        if self.fails >= self.threshold:
            self.until = time.time() + self.cooldown
            self.fails = 0