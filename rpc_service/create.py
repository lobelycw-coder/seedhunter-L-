"""High-intensity funded-address detector. Own cursor, stats, queues, hit store."""
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


def fmt(raw: int, dec: int) -> str:
    return f"{Decimal(raw) / Decimal(10) ** dec:.8f}".rstrip("0").rstrip(".")


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
            self.con.execute(
                "INSERT OR IGNORE INTO brutal_hits(ts,chain,address,seed,path,asset,amount,raw,url)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (int(time.time()), k["chain"], k["address"], k["seed"], k.get("path"),
                 k["asset"], k["amount"], k["raw"], k.get("url")))
            self.con.commit()
        except Exception:
            return False
        if self.mirror is not None:
            try:
                self.mirror.add(**{x: k[x] for x in
                                   ("chain", "address", "seed", "path", "asset", "amount",
                                    "raw", "url")})
            except Exception:
                pass
        return True

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


class Pool:
    def __init__(self, cfg: dict, profile, chains: list[str]):
        self.profile = profile
        self.client = httpx.AsyncClient(
            timeout=profile.rpc_timeout,
            limits=httpx.Limits(max_connections=profile.workers * 2,
                                max_keepalive_connections=profile.workers),
            headers={"content-type": "application/json",
                     "user-agent": "seedhunter-brutal/0.1"})
        self.eps: dict[str, list[Endpoint]] = {}
        self.batch = {c: profile.batch for c in chains}
        envmap = {"ethereum": "RPC_ETH", "bsc": "RPC_BSC", "polygon": "RPC_POLYGON",
                  "base": "RPC_BASE", "arbitrum": "RPC_ARBITRUM",
                  "optimism": "RPC_OPTIMISM", "avalanche": "RPC_AVAX", "solana": "RPC_SOLANA"}
        for chain in chains:
            urls = [e for e in (((cfg.get("chains") or {}).get(chain) or {})
                                .get("endpoints") or []) if e]
            env = os.getenv(envmap.get(chain, ""), "")
            if env:
                urls.insert(0, env)
            self.eps[chain] = [Endpoint(u) for u in dict.fromkeys(urls)]
        self.sem = {c: asyncio.Semaphore(max(1, len(self.eps.get(c, [])) * 3)) for c in chains}

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
        return pool[0]

    async def post(self, chain: str, payload: list[dict]) -> list[dict]:
        async with self.sem[chain]:
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
                    ep.down_until = time.time() + 10
                    continue
                if r.status_code == 200:
                    ep.fails = 0
                    ep.latency = time.perf_counter() - started
                    if self.batch[chain] < self.profile.batch:
                        self.batch[chain] = min(self.profile.batch, self.batch[chain] + 1)
                    try:
                        body = r.json()
                    except ValueError:
                        return []
                    return body if isinstance(body, list) else [body]
                if r.status_code in (429, 502, 503, 504):
                    ep.fails += 1
                    ep.down_until = time.time() + 5
                    self.batch[chain] = max(1, int(self.batch[chain] * 0.7))
                    await asyncio.sleep(0.25 + random.random() * 0.5)
                    continue
                return []
            return []

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


class Detector:
    def __init__(self, cfg, profile, chains, shard, shards, duration, sink, notify):
        self.cfg, self.profile, self.chains = cfg, profile, chains
        self.shard, self.shards, self.duration = shard, shards, duration
        self.sink, self.notify = sink, notify
        self.pool = Pool(cfg, profile, chains)
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=profile.workers * 3)
        self.stop = asyncio.Event()
        self.seeds = self.addrs = self.hits = 0
        self.started = time.time()
        self.latencies: deque = deque(maxlen=240)
        self._seed = 0
        self.natives, self.tokens = {}, {}
        for chain in chains:
            nc = (cfg.get("chains") or {}).get(chain) or {}
            self.natives[chain] = max(1, int(nc.get("min_native", 1)) // profile.threshold_div)
            self.tokens[chain] = list(self._repo_tokens(chain))[:2]
        self._cursor = self._load_cursor()

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
        CURSOR_FILE.write_text(json.dumps({"seed_index": index, "ts": int(time.time())}))

    def rate(self) -> float:
        return self.addrs / max(time.time() - self.started, 1e-9)

    def heartbeat(self) -> None:
        lat = sorted(self.latencies)
        bridge.write_heartbeat(STATS, {
            "running": not self.stop.is_set(), "seeds": self.seeds, "addrs": self.addrs,
            "hits": self.hits, "rate": round(self.rate(), 1),
            "uptime": int(time.time() - self.started),
            "p50_ms": round(lat[len(lat) // 2] * 1000) if lat else None,
            "batch": dict(self.pool.batch), "chains": self.chains,
            "profile": self.profile.name, "ts": int(time.time())})

    async def _producer(self) -> None:
        from seedhunter.derive import derive_batch
        loop = asyncio.get_running_loop()
        index = self._cursor
        while not self.stop.is_set():
            if index % self.shards != self.shard:
                index += 1
                continue
            try:
                rows = await loop.run_in_executor(self.farm, derive_batch, index,
                                                  self.profile.per_seed)
            except TypeError:
                rows = await loop.run_in_executor(self.farm, derive_batch,
                                                  (index, self.profile.per_seed))
            self.seeds += 1
            await self.queue.put((index, rows))
            index += 1
            if index % 64 == 0:
                self._save_cursor(index)

    async def _hit(self, chain, address, seed, path, asset, raw, dec, minimum):
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

    async def _check_evm(self, chain: str, rows) -> None:
        addrs = [r[2] for r in rows]
        start = time.perf_counter()
        bal = await self.pool.native(chain, addrs)
        self.latencies.append(time.perf_counter() - start)
        funded = []
        for j, raw in bal.items():
            if j >= len(rows):
                continue
            idx, _key, addr = rows[j]
            await self._hit(chain, addr, str(self._seed), f"m/44'/60'/0'/0/{idx}",
                            NATIVE.get(chain, "ETH"), raw, 18, self.natives[chain])
            if raw > 0:
                funded.append(rows[j])
        if not funded:
            return
        for sym, token, dec in self.tokens.get(chain, []):
            tb = await self.pool.tokens(chain, [r[2] for r in funded], token)
            for j, raw in tb.items():
                if j < len(funded) and raw > 0:
                    idx, _key, addr = funded[j]
                    await self._hit(chain, addr, str(self._seed), f"m/44'/60'/0'/0/{idx}",
                                    sym, raw, dec, 1)
        if self.profile.deep_funded:
            await self._deep(chain, funded)

    async def _deep(self, chain: str, rows) -> None:
        try:
            from seedhunter.discover import assets
            for _idx, _key, addr in rows[:8]:
                for a in await assets(self.pool, chain, addr):
                    if a["kind"] == "erc20":
                        await self._hit(chain, addr, str(self._seed), "", a["symbol"],
                                        a["raw"], a["decimals"], 1)
        except Exception:
            pass

    async def _check_sol(self, rows) -> None:
        addrs = [r[2] for r in rows]
        start = time.perf_counter()
        bal = await self.pool.sol(addrs)
        self.latencies.append(time.perf_counter() - start)
        for j, raw in bal.items():
            if j < len(rows):
                idx, _kp, addr = rows[j]
                await self._hit("solana", addr, str(self._seed), f"m/44'/501'/{idx}'/0'",
                                "SOL", raw, 9, self.natives["solana"])

    async def _consumer(self, chain: str) -> None:
        while not self.stop.is_set():
            try:
                seed_index, rows = await asyncio.wait_for(self.queue.get(), 1.0)
            except asyncio.TimeoutError:
                continue
            self._seed = seed_index
            self.addrs += len(rows)
            try:
                if chain == "solana":
                    await self._check_sol(rows)
                else:
                    await self._check_evm(chain, rows)
            except Exception as exc:
                log.debug("%s check failed: %s", chain, exc)
            finally:
                self.queue.task_done()

    async def run(self) -> None:
        self.farm = ProcessPoolExecutor(max_workers=self.profile.farm_processes)
        tasks = [asyncio.create_task(self._producer())]
        per = max(2, self.profile.workers // len(self.chains) // 6)
        for chain in self.chains:
            for _ in range(per):
                tasks.append(asyncio.create_task(self._consumer(chain)))
        beat = asyncio.create_task(self._beat())
        log.info("brutal profile=%s chains=%s shard=%s/%s", self.profile.name,
                 ",".join(self.chains), self.shard, self.shards)
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
            self._save_cursor(self._cursor)
            self.farm.shutdown(wait=False)
            await self.pool.close()
            self.heartbeat()

    async def _beat(self) -> None:
        while not self.stop.is_set():
            self.heartbeat()
            await asyncio.sleep(2)


async def _main(args) -> None:
    profile = profiles.get(args.profile)
    chains = [c for c in (args.chains.split(",") if args.chains else profile.chains) if c]
    cfg = bridge.repo_config()
    if not cfg:
        raise SystemExit("config.yaml unreadable — is the add-on inside the repo?")
    sink = HitSink(mirror=args.mirror_hits)
    notify = bridge.notifier() if args.notify else None
    det = Detector(cfg, profile, chains, args.shard, args.shards, args.duration, sink, notify)
    if args.start_seed is not None:
        det._cursor = args.start_seed
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