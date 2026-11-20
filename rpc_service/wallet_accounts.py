"""Wallet account records, address validation, balances, history.

Custody model: addresses are created by the custody layer (AWS KMS today) and
only the *reference* is stored. No seed phrase and no private key ever lands in
a table, a log line, an API response or the dashboard HTML.
"""
from __future__ import annotations

import re
import time

from . import db

EVM_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
BTC_RE = re.compile(r"^(bc1[a-z0-9]{25,62}|[13][a-km-zA-HJ-NP-Z1-9]{25,34})$")
SOL_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
SUPPORTED = {"ethereum": "evm", "bsc": "evm", "polygon": "evm", "base": "evm",
             "arbitrum": "evm", "optimism": "evm", "avalanche": "evm",
             "solana": "sol", "bitcoin": "btc"}


class WalletError(Exception):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def validate_address(network: str, address: str) -> str:
    address = (address or "").strip()
    family = SUPPORTED.get(network)
    if family is None:
        raise WalletError("network_unsupported", f"{network} not supported", 400)
    pattern = {"evm": EVM_RE, "sol": SOL_RE, "btc": BTC_RE}[family]
    if not pattern.match(address):
        raise WalletError("address_invalid", f"not a valid {network} address", 400)
    return address if family == "evm" else address


def create_wallet(path, account_id: int, network: str, address: str,
                  key_ref: str, provider: str = "kms") -> dict:
    if network not in SUPPORTED:
        raise WalletError("network_unsupported", f"{network} not supported", 400)
    validate_address(network, address)
    if db.fetch_one(path, "SELECT id FROM wallets WHERE account_id=? AND network=?",
                    (account_id, network)):
        raise WalletError("wallet_exists", "wallet already exists for this network", 409)
    wallet_id = db.execute(path,
                           "INSERT INTO wallets(account_id,network,address,provider,key_ref,"
                           "created_at) VALUES(?,?,?,?,?,?)",
                           (account_id, network, address, provider, key_ref, int(time.time())))
    db.audit(path, account_id, "wallet.created", {"network": network, "address": address})
    return dict(db.fetch_one(path, "SELECT * FROM wallets WHERE id=?", (wallet_id,)))


def list_wallets(path, account_id: int) -> list[dict]:
    rows = db.fetch_all(path, "SELECT * FROM wallets WHERE account_id=? AND is_active=1 "
                              "ORDER BY id", (account_id,))
    return [dict(r) for r in rows]


def get_wallet(path, account_id: int, wallet_id: int) -> dict:
    row = db.fetch_one(path, "SELECT * FROM wallets WHERE id=? AND account_id=?",
                       (wallet_id, account_id))
    if not row:
        raise WalletError("wallet_missing", "wallet not found", 404)
    return dict(row)


def public_wallet(wallet: dict) -> dict:
    return {"id": wallet["id"], "network": wallet["network"], "address": wallet["address"],
            "provider": wallet["provider"], "created_at": wallet["created_at"]}


async def balance(engine, wallet: dict) -> dict:
    net = engine.resolve(wallet["network"])
    if net.kind == "solana":
        result = (await engine.forward(net.name, {"jsonrpc": "2.0", "id": 1,
                                                  "method": "getBalance",
                                                  "params": [wallet["address"]]}))[0]
        raw = int((result.get("result") or {}).get("value", 0))
    else:
        result = (await engine.forward(net.name, {"jsonrpc": "2.0", "id": 1,
                                                  "method": "eth_getBalance",
                                                  "params": [wallet["address"], "latest"]}))[0]
        raw = int(result.get("result") or "0x0", 16)
    return {"network": net.name, "symbol": net.symbol, "decimals": net.decimals,
            "raw": str(raw), "address": wallet["address"]}


def history(path, account_id: int, limit: int = 50) -> list[dict]:
    rows = db.fetch_all(path,
                        "SELECT wd_ref,network,destination,amount_raw,status,txid,created_at "
                        "FROM withdrawals WHERE account_id=? ORDER BY id DESC LIMIT ?",
                        (account_id, limit))
    return [dict(r) for r in rows]


def set_public(path, account_id: int, is_public: bool) -> None:
    db.execute(path, "UPDATE accounts SET is_public=? WHERE id=?",
               (1 if is_public else 0, account_id))
    db.audit(path, account_id, "account.publicity", {"is_public": bool(is_public)})


def public_profile(path, project_id: str) -> dict | None:
    """Deliberately-public view. No Telegram id, no auth data, no key refs."""
    account = db.fetch_one(path, "SELECT * FROM accounts WHERE project_id=?", (project_id,))
    if not account or not account["is_public"]:
        return None
    wallets = db.fetch_all(path, "SELECT network,address FROM wallets WHERE account_id=? "
                                 "AND is_active=1 ORDER BY id", (account["id"],))
    return {"project_id": account["project_id"], "label": account["label"],
            "tier": account["tier"],
            "premium_until": account["premium_until"],
            "wallets": [{"network": w["network"], "address": w["address"]} for w in wallets],
            "member_since": account["created_at"]}