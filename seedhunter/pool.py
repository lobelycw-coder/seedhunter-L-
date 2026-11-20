"""Endpoint pool: rotation, breakers, blast broadcast, gas oracle."""
from __future__ import annotations

import asyncio
import json
import random
import time

import httpx

from .chains import TRANSFER_TOPIC
from .config import Settings
from .gov import Breaker, Governor
from .stealth import headers, jitter


class Endpoint:
    __slots__ = ("url", "breaker")

    def __init__(self, url: str):
        self.url = url
        self.breaker = Breaker()


class Pool:
    def __init__(self, s: Settings):
        self.s = s
        self.client = httpx.AsyncClient(
            timeout=s.rpc_timeout,
            limits=httpx.Limits(max_connections=256, max_keepalive_connections=128),
            http2=True,
        )
        self.endpoints = {c.name: [Endpoint(u) for u in c.endpoints] for c in s.chains}
        self.gov = {c.name: Governor() for c in s.chains}

    async def close(self) -> None:
        await self.client.aclose()

    def _pick(self, chain: str) -> Endpoint | None:
        pool = self.endpoints.get(chain, [])
        if not pool:
            return None
        live = [e for e in pool if e.breaker.ok()]
        if not live:
            best = min(pool, key=lambda e: e.breaker.until)
            best.breaker.until = 0.0
            return best
        live.sort(key=lambda e: e.breaker.latency)
        return live[0] if random.random() > 0.2 else random.choice(live)

    async def post(self, chain: str, payload: list[dict]) -> list[dict]:
        g = self.gov[chain]
        await g.acquire()
        ok = False
        try:
            delay = 0.5
            for _ in range(self.s.max_retries):
                ep = self._pick(chain)
                if ep is None:
                    return []
                if self.s.jitter_ms:
                    await asyncio.sleep(jitter(self.s.jitter_ms))
                t0 = time.perf_counter()
                try:
                    r = await self.client.post(ep.url, json=payload,
                                               headers=headers())
                except (httpx.HTTPError, json.JSONDecodeError):
                    ep.breaker.fail()
                    await asyncio.sleep(delay + random.random() * 0.3)
                    delay *= 1.8
                    continue
                if r.status_code == 200:
                    ep.breaker.success(time.perf_counter() - t0)
                    ok = True
                    body = r.json()
                    return body if isinstance(body, list) else [body]
                if r.status_code in (429, 502, 503, 504):
                    ep.breaker.fail()
                    g.limit = max(g.floor, int(g.limit * 0.6))
                    g.cuts += 1
                    await asyncio.sleep(delay + random.random() * 0.3)
                    delay *= 1.8
                    continue
                ok = True
                return []
            return []
        finally:
            await g.release(ok)

    async def blast(self, chain: str, raw: str) -> str | None:
        """Broadcast one signed tx to every endpoint simultaneously."""
        if not self.s.blast_all:
            rows = await self.post(chain, [{"jsonrpc": "2.0", "id": 0,
                                            "method": "eth_sendRawTransaction",
                                            "params": [raw]}])
            return (rows[0].get("result") if rows else None)
        req = [{"jsonrpc": "2.0", "id": 0, "method": "eth_sendRawTransaction",
                "params": [raw]}]
        tasks = [
            self.client.post(e.url, json=req, headers=headers())
            for e in self.endpoints.get(chain, [])
        ]
        out = None
        for coro in asyncio.as_completed(tasks):
            try:
                r = await coro
                if r.status_code == 200:
                    res = r.json()
                    h = (res[0] if isinstance(res, list) else res).get("result")
                    if h and not out:
                        out = h
            except (httpx.HTTPError, json.JSONDecodeError):
                continue
        return out

    async def gas_oracle(self, chain: str) -> tuple[int, int]:
        """Lowest base fee seen, highest priority fee seen."""
        payload = [
            {"jsonrpc": "2.0", "id": 0, "method": "eth_getBlockByNumber",
             "params": ["latest", False]},
            {"jsonrpc": "2.0", "id": 1, "method": "eth_maxPriorityFeePerGas",
             "params": []},
        ]
        rows = await self.post(chain, payload)
        base = tip = 0
        for r in rows:
            try:
                if r["id"] == 0:
                    base = int(r["result"].get("baseFeePerGas", "0x0"), 16)
                else:
                    tip = int(r["result"], 16)
            except (KeyError, TypeError, ValueError, AttributeError):
                pass
        return base, tip

    async def native(self, chain, addrs):
        payload = [{"jsonrpc": "2.0", "id": i, "method": "eth_getBalance",
                    "params": [a, "latest"]} for i, a in enumerate(addrs)]
        out = {}
        for row in await self.post(chain, payload):
            try:
                out[int(row["id"])] = int(row["result"], 16)
            except (KeyError, TypeError, ValueError):
                pass
        return out

    async def tokens(self, chain, addrs, token):
        payload = []
        for i, a in enumerate(addrs):
            data = TRANSFER_TOPIC + a.lower().replace("0x", "").rjust(64, "0")
            payload.append({"jsonrpc": "2.0", "id": i, "method": "eth_call",
                            "params": [{"to": token, "data": data}, "latest"]})
        out = {}
        for row in await self.post(chain, payload):
            try:
                res = row.get("result")
                if res and res != "0x":
                    out[int(row["id"])] = int(res, 16)
            except (KeyError, TypeError, ValueError):
                pass
        return out

    async def call(self, chain, to, data, frm=None, override=None, block="latest"):
        tx = {"to": to, "data": data}
        if frm:
            tx["from"] = frm
        params = [tx, block] + ([override] if override else [])
        rows = await self.post(chain, [{"jsonrpc": "2.0", "id": 0,
                                        "method": "eth_call", "params": params}])
        return rows[0].get("result") if rows else None

    async def sol(self, chain, addrs):
        payload = [{"jsonrpc": "2.0", "id": i, "method": "getBalance",
                    "params": [a]} for i, a in enumerate(addrs)]
        out = {}
        for row in await self.post(chain, payload):
            try:
                out[int(row["id"])] = int(row["result"]["value"])
            except (KeyError, TypeError, ValueError):
                pass
        return out