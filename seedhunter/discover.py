"""Full asset discovery on a hit address."""
from __future__ import annotations

from eth_utils import keccak

from .chains import TRANSFER_EVT, TRANSFER_EVT_1155

SYMBOL = keccak(text="symbol()")[:4].hex()
DECIMALS = keccak(text="decimals()")[:4].hex()
BALANCE = keccak(text="balanceOf(address)")[:4].hex()


async def _logs(pool, chain, address, topic):
    out: set[str] = set()
    head_rows = await pool.post(chain, [{"jsonrpc": "2.0", "id": 0,
                                         "method": "eth_blockNumber", "params": []}])
    try:
        head = int(head_rows[0]["result"], 16)
    except (KeyError, IndexError, TypeError, ValueError):
        return out
    for depth in (20000, 200000):
        start = max(0, head - depth)
        for lo in range(start, head, 8000):
            rows = await pool.post(chain, [{
                "jsonrpc": "2.0", "id": 0, "method": "eth_getLogs",
                "params": [{"fromBlock": hex(lo), "toBlock": hex(min(lo + 7999, head)),
                            "topics": [topic, None, "0x" + address.lower()[2:].rjust(64, "0")]}],
            }])
            try:
                for row in rows[0]["result"]:
                    out.add(row["address"])
            except (KeyError, IndexError, TypeError):
                pass
            if len(out) > 60:
                return out
        if out:
            return out
    return out


async def assets(pool, chain, address: str) -> list[dict]:
    """Return every ERC20/721/1155 the address holds with a balance."""
    found: list[dict] = []
    seen: set[str] = set()
    for topic in (TRANSFER_EVT, TRANSFER_EVT_1155):
        for token in await _logs(pool, chain, address, topic):
            if token.lower() in seen:
                continue
            seen.add(token.lower())
            data = BALANCE + address.lower()[2:].rjust(64, "0")
            bal = await pool.call(chain, token, "0x" + data)
            if not bal or bal == "0x":
                continue
            try:
                raw = int(bal, 16)
            except ValueError:
                continue
            if raw == 0:
                continue
            dec_hex = await pool.call(chain, token, "0x" + DECIMALS)
            try:
                dec = int(dec_hex, 16) if dec_hex and dec_hex != "0x" else 0
            except ValueError:
                dec = 0
            sym_hex = await pool.call(chain, token, "0x" + SYMBOL)
            sym = "?"
            if sym_hex and len(sym_hex) > 66:
                try:
                    sym = bytes.fromhex(sym_hex[130:]).rstrip(b"\x00").decode(errors="ignore")[:12]
                except ValueError:
                    pass
            found.append({"token": token, "raw": raw, "decimals": dec,
                          "symbol": sym or "?", "kind": "erc20" if dec else "erc721",
                          "topic": topic})
    return found