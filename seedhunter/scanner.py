"""Ad-hoc address scan."""
from __future__ import annotations

from .chains import EXPLORER, NATIVE, TOKENS
from .config import Settings
from .discover import assets
from .pool import Pool
from .security import inspect
from .worker import fmt


async def scan(s: Settings, address: str) -> list[str]:
    pool = Pool(s)
    out: list[str] = []
    try:
        for chain in s.chains:
            if chain.is_evm:
                if not address.startswith("0x"):
                    continue
                bal = await pool.native(chain.name, [address])
                if bal.get(0):
                    out.append(f"{chain.name}: {fmt(bal[0], 18)} {NATIVE[chain.name][0]}")
                for sym, token, dec in TOKENS.get(chain.name, []):
                    tb = await pool.tokens(chain.name, [address], token)
                    if tb.get(0):
                        out.append(f"{chain.name}: {fmt(tb[0], dec)} {sym}")
                        if chain.router:
                            r = await inspect(pool, chain.name, token, address,
                                              chain.router, chain.weth)
                            out.append(f"  score {r['score']} {r['verdict']}")
                if s.discover:
                    extra = await assets(pool, chain.name, address)
                    for a in extra:
                        if a["kind"] != "erc20":
                            out.append(f"{chain.name}: {a['symbol']} id {a['raw']} ({a['kind']})")
            else:
                bal = await pool.sol(chain.name, [address])
                if bal.get(0):
                    out.append(f"solana: {fmt(bal[0], 9)} SOL")
        if out:
            out.append("")
            out.append("explorer: " + EXPLORER[s.chains[0].name].format(a=address))
    finally:
        await pool.close()
    return out or ["no balance"]