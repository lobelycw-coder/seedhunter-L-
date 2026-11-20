"""SPL enumeration and transfer building."""
from __future__ import annotations

import base64
import struct

import httpx

from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.message import Message
from solders.pubkey import Pubkey
from solders.system_program import transfer
from solders.transaction import Transaction

from .chains import JITO, JITO_TIPS

TOKEN_PROGRAM = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
ATA_PROGRAM = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")


async def token_accounts(pool, chain, owner):
    rows = await pool.post(chain, [{
        "jsonrpc": "2.0", "id": 0, "method": "getTokenAccountsByOwner",
        "params": [owner, {"programId": str(TOKEN_PROGRAM)},
                   {"encoding": "jsonParsed"}]}])
    out = []
    try:
        for item in rows[0]["result"]["value"]:
            info = item["account"]["data"]["parsed"]["info"]
            amt = int(info["tokenAmount"]["amount"])
            if amt:
                out.append({"mint": info["mint"], "raw": amt, "symbol": "SPL",
                            "decimals": int(info["tokenAmount"]["decimals"]),
                            "kind": "spl"})
    except (KeyError, IndexError, TypeError, ValueError):
        pass
    return out


def _ata(owner: Pubkey, mint: Pubkey) -> Pubkey:
    return Pubkey.find_program_address(
        [bytes(owner), bytes(TOKEN_PROGRAM), bytes(mint)], ATA_PROGRAM)[0]


def build(mint: str, amount: int, dest: str, kp) -> Instruction | None:
    try:
        m = Pubkey.from_string(mint)
        d = Pubkey.from_string(dest)
        keys = [AccountMeta(_ata(kp.pubkey(), m), False, True),
                AccountMeta(_ata(d, m), False, True),
                AccountMeta(kp.pubkey(), True, False)]
        return Instruction(TOKEN_PROGRAM, bytes([3]) + struct.pack("<Q", amount), keys)
    except Exception:
        return None


async def _blockhash(pool, chain):
    rows = await pool.post(chain, [{"jsonrpc": "2.0", "id": 0,
                                    "method": "getLatestBlockhash",
                                    "params": [{"commitment": "confirmed"}]}])
    try:
        return Hash.from_string(rows[0]["result"]["value"]["blockhash"])
    except (KeyError, IndexError, TypeError):
        return None


async def send(pool, chain, kp, ixs, jito: bool = False) -> str | None:
    bh = await _blockhash(pool, chain)
    if bh is None:
        return None
    tx = Transaction([kp], Message.new_with_blockhash(ixs, kp.pubkey(), bh), bh)
    raw = base64.b64encode(bytes(tx)).decode()
    if jito:
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.post(JITO, json={"jsonrpc": "2.0", "id": 1,
                                             "method": "sendBundle",
                                             "params": [[raw]]})
                if r.status_code == 200:
                    res = r.json().get("result")
                    if res:
                        return res[0]
        except (httpx.HTTPError, ValueError):
            pass
    rows = await pool.post(chain, [{
        "jsonrpc": "2.0", "id": 0, "method": "sendTransaction",
        "params": [raw, {"encoding": "base64", "skipPreflight": True,
                         "maxRetries": 5}]}])
    try:
        return rows[0]["result"]
    except (KeyError, IndexError, TypeError):
        return None


def tip_ix(kp) -> Instruction | None:
    try:
        return transfer({"from_pubkey": kp.pubkey(),
                         "to_pubkey": Pubkey.from_string(JITO_TIPS[0]),
                         "lamports": 100_000})
    except Exception:
        return None