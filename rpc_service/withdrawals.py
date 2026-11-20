"""Withdrawal requests: validation, limits, idempotency, audit, signing hook.

State machine:
  requested -> approved -> signed -> broadcast -> confirmed
                     \\-> rejected
  requested -> failed (validation/signing error)

Authorization is *layered*: valid session + correct 6-digit PIN + (optionally)
a re-typed destination. A public project id or an address prefix alone never
authorizes anything.
"""
from __future__ import annotations

import secrets
import time

from . import auth, db, signer, wallet_accounts
from .rpc_engine import RpcError

OPEN_STATES = ("requested", "approved", "signed", "broadcast")


class WithdrawalError(Exception):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def _open_balance_check(path, account_id: int, network: str, amount: int, fee: int) -> None:
    rows = db.fetch_all(path, "SELECT amount_raw,fee_raw FROM withdrawals WHERE account_id=? "
                              "AND network=? AND status IN ('requested','approved','signed','broadcast')",
                        (account_id, network))
    pending = sum(int(r["amount_raw"]) + int(r["fee_raw"]) for r in rows)
    return pending


async def create(cfg, engine, path, account: dict, *, network: str, destination: str,
                 amount_raw: int, pin: str, idem_key: str | None, ip: str | None) -> dict:
    if not cfg.withdrawals_enabled:
        raise WithdrawalError("withdrawals_disabled",
                              "real withdrawals are switched off (SH_WITHDRAWALS_ENABLED)", 503)
    if account["withdrawal_suspended"]:
        raise WithdrawalError("suspended", "withdrawals suspended for this account", 423)
    auth.check_pin(path, account["id"], pin, cfg.pin_max_fails, cfg.pin_lock_minutes)

    if idem_key:
        existing = db.fetch_one(path, "SELECT * FROM withdrawals WHERE account_id=? AND idem_key=?",
                                (account["id"], idem_key))
        if existing:
            return dict(existing)

    destination = wallet_accounts.validate_address(network, destination)
    wallet = db.fetch_one(path, "SELECT * FROM wallets WHERE account_id=? AND network=? "
                                "AND is_active=1", (account["id"], network))
    if not wallet:
        raise WithdrawalError("wallet_missing", f"no active {network} wallet", 404)
    if wallet["address"].lower() == destination.lower():
        raise WithdrawalError("destination_same", "destination equals source wallet", 400)

    if amount_raw <= 0:
        raise WithdrawalError("amount_invalid", "amount must be positive", 400)
    if amount_raw < cfg.withdraw_min.get(network, 0):
        raise WithdrawalError("amount_below_min", "below minimum", 400)
    if amount_raw > cfg.withdraw_max.get(network, 1 << 62):
        raise WithdrawalError("amount_above_max", "above maximum", 400)

    day_start = int(time.time()) - 86400
    used = db.fetch_one(path, "SELECT COUNT(*) AS n FROM withdrawals WHERE account_id=? "
                              "AND created_at>?", (account["id"], day_start))["n"]
    if used >= cfg.withdraw_daily_count:
        raise WithdrawalError("daily_limit", "daily withdrawal count reached", 429)

    # duplicate guard: same destination + amount still open
    twin = db.fetch_one(path, "SELECT wd_ref FROM withdrawals WHERE account_id=? AND network=? "
                              "AND lower(destination)=lower(?) AND amount_raw=? "
                              "AND status IN ('requested','approved','signed','broadcast')",
                        (account["id"], network, destination, str(amount_raw)))
    if twin:
        raise WithdrawalError("duplicate_request",
                              f"identical request {twin['wd_ref']} still open", 409)

    try:
        balance = await wallet_accounts.balance(engine, dict(wallet))
    except RpcError as exc:
        raise WithdrawalError("balance_unavailable", exc.message, 503) from None
    pending = _open_balance_check(path, account["id"], network, amount_raw, 0)
    available = int(balance["raw"]) - pending
    fee = 0
    if amount_raw + fee > available:
        raise WithdrawalError("insufficient_balance",
                              f"available {available} raw, requested {amount_raw}", 409)

    ref = "WD-" + secrets.token_hex(6).upper()
    now = int(time.time())
    wd_id = db.execute(path,
                       "INSERT INTO withdrawals(wd_ref,account_id,wallet_id,network,destination,"
                       "amount_raw,fee_raw,status,idem_key,created_ip,created_at,updated_at) "
                       "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                       (ref, account["id"], wallet["id"], network, destination,
                        str(amount_raw), str(fee), "requested", idem_key, ip, now, now))
    db.audit(path, account["id"], "withdrawal.requested",
             {"ref": ref, "network": network, "destination": destination,
              "amount_raw": str(amount_raw)}, ip)
    return dict(db.fetch_one(path, "SELECT * FROM withdrawals WHERE id=?", (wd_id,)))


def set_status(path, wd_id: int, status: str, *, txid: str | None = None,
               reason: str | None = None, account_id: int | None = None) -> None:
    db.execute(path, "UPDATE withdrawals SET status=?,txid=COALESCE(?,txid),"
                     "failure_reason=COALESCE(?,failure_reason),updated_at=? WHERE id=?",
               (status, txid, reason, int(time.time()), wd_id))
    db.audit(path, account_id, f"withdrawal.{status}", {"id": wd_id, "txid": txid,
                                                       "reason": reason})


async def approve_and_sign(cfg, engine, path, wd: dict) -> dict:
    """Move a request to signed. Requires a configured KMS signer."""
    network = wd["network"]
    ok, why = signer.available(cfg, network)
    if not ok:
        set_status(path, wd["id"], "requested", reason=f"signer unavailable: {why}")
        return {"status": "requested", "signer": why}
    set_status(path, wd["id"], "approved")
    try:
        if network == "solana":
            raw = await _sign_sol(cfg, engine, path, wd)
        else:
            raw = await _sign_evm(cfg, engine, path, wd)
    except Exception as exc:
        set_status(path, wd["id"], "failed", reason=f"{type(exc).__name__}: {exc}")
        return {"status": "failed", "reason": str(exc)}
    set_status(path, wd["id"], "signed", txid=raw if network == "solana" else None)
    return {"status": "signed", "raw": raw}


async def _sign_evm(cfg, engine, path, wd: dict) -> str:
    from eth_account._utils.legacy_transactions import (
        encode_transaction, serializable_unsigned_transaction_from_dict)
    net = engine.resolve(wd["network"])
    wallet = db.fetch_one(path, "SELECT * FROM wallets WHERE id=?", (wd["wallet_id"],))
    nonce = int((await engine.forward(net.name, {"jsonrpc": "2.0", "id": 1,
                                                 "method": "eth_getTransactionCount",
                                                 "params": [wallet["address"], "pending"]}))[0]
                .get("result", "0x0"), 16)
    gas_price = int((await engine.forward(net.name, {"jsonrpc": "2.0", "id": 2,
                                                     "method": "eth_gasPrice",
                                                     "params": []}))[0]
                    .get("result", "0x0"), 16)
    tx = {"nonce": nonce, "gasPrice": gas_price, "gas": 21000,
          "to": wd["destination"], "value": int(wd["amount_raw"]), "data": b"",
          "chainId": net.chain_id}
    unsigned = serializable_unsigned_transaction_from_dict(tx)
    v, r, s, _addr = signer.sign_evm_digest(cfg, cfg.kms_key_evm, unsigned.hash())
    return encode_transaction(unsigned, (v, r, s)).hex()


async def _sign_sol(cfg, engine, path, wd: dict) -> str:
    from solders.hash import Hash
    from solders.instruction import Instruction
    from solders.keypair import Keypair
    from solders.message import Message
    from solders.pubkey import Pubkey
    from solders.transaction import Transaction
    from base58 import b58decode
    wallet = db.fetch_one(path, "SELECT * FROM wallets WHERE id=?", (wd["wallet_id"],))
    blockhash = (await engine.forward("solana", {"jsonrpc": "2.0", "id": 1,
                                                 "method": "getLatestBlockhash",
                                                 "params": []}))[0]["result"]["value"]["blockhash"]
    payer = Pubkey.from_bytes(b58decode(wallet["address"]))
    dest = Pubkey.from_bytes(b58decode(wd["destination"]))
    ix = Instruction(Pubkey.from_string("11111111111111111111111111111111"),
                     b"", [payer, dest, payer], 2, b"")
    from solders.system_program import transfer
    ix = transfer({"from_pubkey": payer, "to_pubkey": dest},
                  int(wd["amount_raw"]))
    message = Message.new_with_blockhash([ix], payer, Hash.from_string(blockhash))
    signature = signer.sign_solana(cfg, cfg.kms_key_sol, bytes(message))
    return bytes(Transaction.populate(message, [signature])).hex()


def suspend(path, account_id: int, suspended: bool) -> None:
    db.execute(path, "UPDATE accounts SET withdrawal_suspended=? WHERE id=?",
               (1 if suspended else 0, account_id))
    db.audit(path, account_id, "withdrawal.suspend", {"suspended": suspended})


def reconcile(path, account_id: int) -> dict:
    """Book-keeping check: open withdrawals per network vs. recorded amounts."""
    rows = db.fetch_all(path, "SELECT network,COUNT(*) AS n,SUM(CAST(amount_raw AS INTEGER)) AS total "
                              "FROM withdrawals WHERE account_id=? AND status IN "
                              "('requested','approved','signed','broadcast') GROUP BY network",
                        (account_id,))
    return {"open": {r["network"]: {"count": r["n"], "total_raw": str(r["total"] or 0)}
                     for r in rows}}