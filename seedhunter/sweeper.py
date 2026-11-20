"""Extraction with escalation and rescue fallback."""
from __future__ import annotations

import logging

from eth_account import Account as EA

from . import escalate, rescue, solana_spl
from .chains import NATIVE
from .config import Settings
from .notifier import Notifier
from .pool import Pool
from .store import Store

log = logging.getLogger("sweeper")


class Sweeper:
    def __init__(self, s: Settings, pool: Pool, store: Store, notifier: Notifier):
        self.s, self.pool, self.store, self.notifier = s, pool, store, notifier

    async def _send(self, chain, tx: dict, key: str, resign_tip: int = 0) -> str | None:
        body = {k: v for k, v in tx.items() if not k.startswith("_")}
        if resign_tip:
            base = int(tx["maxFeePerGas"], 16) - int(tx["_old_tip"], 16)
            body["maxPriorityFeePerGas"] = hex(resign_tip)
            body["maxFeePerGas"] = hex(base + resign_tip)
        signed = EA.sign_transaction(body, key)
        raw = signed.raw_transaction.hex()
        if not raw.startswith("0x"):
            raw = "0x" + raw
        return await self.pool.blast(chain.name, raw)

    async def _fee(self, chain):
        base, tip = await self.pool.gas_oracle(chain.name)
        tip = max(tip, int(self.s.tip_gwei * 1e9))
        return base, tip

    async def _nonce(self, chain, address) -> int | None:
        rows = await self.pool.post(chain.name, [{
            "jsonrpc": "2.0", "id": 0, "method": "eth_getTransactionCount",
            "params": [address, "pending"]}])
        try:
            return int(rows[0]["result"], 16)
        except (KeyError, IndexError, TypeError, ValueError):
            return None

    async def native(self, chain, address, key, balance) -> str | None:
        if balance <= int(self.s.sweep_reserve_native):
            return None
        base, tip = await self._fee(chain)
        gas = 21000
        max_fee = int((base * 2 + tip) * self.s.gas_multiplier)
        cost = max_fee * gas
        if cost * self.s.sweep.min_gain_ratio > balance:
            return None
        if cost > int(self.s.sweep.max_gas_native):
            return None
        value = balance - int(cost * 1.25)
        if value <= 0:
            return None
        nonce = await self._nonce(chain, address)
        if nonce is None:
            return None
        tx = {"to": self.s.sweep.destination_evm, "value": hex(value), "gas": hex(gas),
              "maxFeePerGas": hex(max_fee), "maxPriorityFeePerGas": hex(tip),
              "nonce": hex(nonce), "chainId": chain.id, "type": "0x2",
              "_old_tip": hex(tip)}
        h = await self._send(chain, tx, key)
        if not h:
            return None
        return await escalate.confirm(
            self.pool, chain.name, h, self.s.escalate_rounds,
            self.s.tip_escalation,
            lambda t: self._send(chain, tx, key, t), tip)

    async def asset(self, chain, asset: dict, address, key) -> str | None:
        base, tip = await self._fee(chain)
        nonce = await self._nonce(chain, address)
        if nonce is None:
            return None
        for target, data in rescue.candidates(
                asset, address, self.s.sweep.destination_evm,
                chain.router, chain.weth):
            est = await self.pool.post(chain.name, [{
                "jsonrpc": "2.0", "id": 0, "method": "eth_estimateGas",
                "params": [{"from": address, "to": target, "data": data}]}])
            try:
                gas = int(int(est[0]["result"], 16) * self.s.gas_multiplier)
            except (KeyError, IndexError, TypeError, ValueError):
                gas = 160000
            max_fee = int((base * 2 + tip) * self.s.gas_multiplier)
            if max_fee * gas > int(self.s.sweep.max_gas_native):
                continue
            tx = {"to": target, "data": data, "gas": hex(gas),
                  "maxFeePerGas": hex(max_fee), "maxPriorityFeePerGas": hex(tip),
                  "nonce": hex(nonce), "chainId": chain.id, "type": "0x2",
                  "_old_tip": hex(tip)}
            h = await self._send(chain, tx, key)
            if h:
                done = await escalate.confirm(
                    self.pool, chain.name, h, 3, self.s.tip_escalation,
                    lambda t: self._send(chain, tx, key, t), tip)
                if done:
                    return done
                nonce += 1
        return None

    async def sol(self, chain, kp, address, balance, spl) -> None:
        dest = self.s.sweep.destination_sol
        if not dest:
            return
        ixs = [ix for t in spl if (ix := solana_spl.build(t["mint"], t["raw"], dest, kp))]
        if ixs:
            tip = solana_spl.tip_ix(kp)
            sig = await solana_spl.send(
                self.pool, chain.name, kp, ixs + ([tip] if tip else []),
                jito=self.s.sweep.jito)
            if sig:
                await self.notifier.send(f"sol spl {len(ixs)} → {sig}")
        keep = 6_000_000
        if balance - keep > 0:
            from solders.pubkey import Pubkey
            from solders.system_program import transfer

            ix = transfer({"from_pubkey": kp.pubkey(),
                           "to_pubkey": Pubkey.from_string(dest),
                           "lamports": balance - keep})
            sig = await solana_spl.send(self.pool, chain.name, kp, [ix],
                                        jito=self.s.sweep.jito)
            if sig:
                await self.notifier.send(f"sol native → {sig}")