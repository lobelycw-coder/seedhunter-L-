"""Replacement / speed-up loop."""
from __future__ import annotations

import asyncio
import logging

log = logging.getLogger("escalate")


async def confirm(pool, chain, tx_hash: str, rounds: int, factor: float,
                  resign) -> str | None:
    """Poll for receipt; on stall, re-sign with a higher tip and re-blast."""
    tip = 0
    for rnd in range(rounds):
        await asyncio.sleep(2 + rnd)
        rows = await pool.post(chain, [{"jsonrpc": "2.0", "id": 0,
                                        "method": "eth_getTransactionReceipt",
                                        "params": [tx_hash]}])
        try:
            rec = rows[0]["result"]
        except (KeyError, IndexError, TypeError):
            rec = None
        if rec:
            if int(rec.get("status", "0x0"), 16) == 1:
                return tx_hash
            log.warning("reverted %s", tx_hash)
            return None
        if rnd < rounds - 1:
            try:
                tip = max(int(tip * factor) + 1, 1)
                new_hash = await resign(tip)
                if new_hash:
                    tx_hash = new_hash
            except Exception as exc:
                log.debug("resign failed: %s", exc)
    return tx_hash