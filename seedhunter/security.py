from __future__ import annotations

import time

from eth_utils import keccak

from .chains import EIP1967_IMPL

CHECKS = [
    ("isBlackListed(address)", 30, "blacklist"),
    ("setBlackList(address,bool)", 25, "blacklist-setter"),
    ("setBots(address,bool)", 20, "bot-block"),
    ("paused()", 20, "pausable"),
    ("mint(address,uint256)", 25, "mintable"),
    ("upgradeTo(address)", 20, "upgradeable"),
    ("setMaxTxAmount(uint256)", 10, "tx-limit"),
    ("setTaxFee(uint256)", 8, "mutable-tax"),
]
BUY = keccak(text="swapExactETHForTokens(uint256,address[],address,uint256)")[:4].hex()


def _word(v: int) -> str:
    return f"{v:064x}"


async def inspect(pool, chain, token, holder, router, weth) -> dict:
    score, reasons = 50, []
    rows = await pool.post(chain, [{"jsonrpc": "2.0", "id": 0,
                                    "method": "eth_getCode",
                                    "params": [token, "latest"]}])
    code = (rows[0].get("result") if rows else "") or "0x"
    if code in ("0x", "0x0", ""):
        return {"score": 0, "verdict": "dead", "reasons": ["no bytecode"]}
    for sig, pen, label in CHECKS:
        if keccak(text=sig)[:4].hex() in code:
            score -= pen
            reasons.append(f"-{pen} {label}")
    st = await pool.post(chain, [{"jsonrpc": "2.0", "id": 0,
                                  "method": "eth_getStorageAt",
                                  "params": [token, EIP1967_IMPL, "latest"]}])
    try:
        if int(st[0]["result"], 16):
            score -= 20
            reasons.append("-20 proxy")
    except (KeyError, IndexError, TypeError, ValueError):
        pass
    owner = await pool.call(chain, token, "0x" + keccak(text="owner()")[:4].hex())
    if owner and owner not in ("0x", "0x" + "0" * 64):
        score -= 15
        reasons.append("-15 live owner")
    else:
        score += 15
        reasons.append("+15 renounced")
    if router and weth:
        data = ("0x" + BUY + _word(10**15) + _word(160) + _word(int(holder, 16))
                + _word(int(time.time()) + 900) + _word(2)
                + _word(int(weth, 16)) + _word(int(token, 16)))
        res = await pool.call(chain, router, data, frm=holder,
                              override={holder: {"balance": hex(10**18)}})
        if res and res != "0x":
            score += 20
            reasons.append("+20 buy ok")
        else:
            score -= 35
            reasons.append("-35 buy reverted")
    score = max(0, min(100, score))
    return {"score": score,
            "verdict": "clean" if score >= 70 else "risky" if score >= 40 else "honeypot",
            "reasons": reasons}