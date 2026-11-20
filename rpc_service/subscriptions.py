"""Plans, quotas and crypto invoices.

Pricing policy (documented in README): the premium price is a fixed USD amount
(SH_PREMIUM_PRICE_USD, default 20). The crypto amount is quoted once at invoice
creation and frozen, so a later price move never changes what a customer owes.
Rate source is either the static table (SH_PRICE_STATIC_*) or a live oracle
(SH_PRICE_SOURCE=coingecko). Invoices expire after SH_INVOICE_TTL seconds.
"""
from __future__ import annotations

import math
import secrets
import time

import httpx

from . import db
from .rpc_engine import PAYMENT_NETWORK

CURRENCIES = ("BTC", "ETH", "SOL", "BNB")
DECIMALS = {"BTC": 8, "ETH": 18, "SOL": 9, "BNB": 18}
PLANS = {
    "free": {"label": "Free", "rpc_rate": "rpc_rate_free", "rpc_daily": "rpc_daily_free"},
    "premium": {"label": "Premium", "rpc_rate": "rpc_rate_premium", "rpc_daily": "rpc_daily_premium"},
}
GRACE_SECONDS = 3600


class SubscriptionError(Exception):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


async def price_usd(cfg, currency: str) -> float:
    if cfg.price_source == "static":
        value = cfg.price_static.get(currency)
        if not value:
            raise SubscriptionError("price_missing", f"no price for {currency}", 503)
        return float(value)
    if cfg.price_source == "coingecko":
        ids = {"BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana", "BNB": "binancecoin"}
        async with httpx.AsyncClient(timeout=8) as client:
            response = await client.get("https://api.coingecko.com/api/v3/simple/price",
                                        params={"ids": ids[currency], "vs_currencies": "usd"})
            response.raise_for_status()
            return float(response.json()[ids[currency]]["usd"])
    raise SubscriptionError("price_source_unknown", cfg.price_source, 503)


def crypto_amount(usd: int, price: float, decimals: int) -> int:
    """Ceil to the asset's smallest unit so a rounding error never underpays."""
    if price <= 0:
        raise SubscriptionError("price_invalid", "non-positive price", 503)
    return max(1, math.ceil(usd / price * (10 ** decimals)))


async def create_invoice(cfg, path, account_id: int, currency: str) -> dict:
    currency = currency.upper()
    if currency not in CURRENCIES:
        raise SubscriptionError("currency_unsupported", f"use one of {', '.join(CURRENCIES)}", 400)
    address = cfg.deposit_addresses.get(currency, "")
    if not address:
        raise SubscriptionError("deposit_address_missing",
                                f"SH_DEPOSIT_{currency} not configured", 503)
    price = await price_usd(cfg, currency)
    now = int(time.time())
    ref = "INV-" + secrets.token_hex(8).upper()
    amount = crypto_amount(cfg.premium_price_usd, price, DECIMALS[currency])
    db.execute(path,
               "INSERT INTO invoices(invoice_ref,account_id,currency,network,address,"
               "amount_raw,amount_usd,price_usd,created_at,expires_at) "
               "VALUES(?,?,?,?,?,?,?,?,?,?)",
               (ref, account_id, currency, PAYMENT_NETWORK[currency], address,
                str(amount), cfg.premium_price_usd, price, now, now + cfg.invoice_ttl))
    db.audit(path, account_id, "invoice.created",
             {"ref": ref, "currency": currency, "amount_raw": str(amount)})
    return get_invoice(path, ref)


def get_invoice(path, ref: str) -> dict:
    row = db.fetch_one(path, "SELECT * FROM invoices WHERE invoice_ref=?", (ref,))
    if not row:
        raise SubscriptionError("invoice_missing", "invoice not found", 404)
    return dict(row)


def public_invoice(invoice: dict, cfg) -> dict:
    return {"invoice_ref": invoice["invoice_ref"], "currency": invoice["currency"],
            "network": invoice["network"], "address": invoice["address"],
            "amount_raw": invoice["amount_raw"], "amount_usd": invoice["amount_usd"],
            "status": effective_status(invoice, cfg), "txid": invoice["txid"],
            "confirmations": invoice["confirmations"],
            "expires_at": invoice["expires_at"],
            "required_confirmations": cfg.confirmations.get(invoice["currency"], 0)}


def effective_status(invoice: dict, cfg) -> str:
    if invoice["status"] == "pending" and invoice["expires_at"] < int(time.time()):
        return "expired"
    return invoice["status"]


def expire_stale(path) -> int:
    return db.execute(path, "UPDATE invoices SET status='expired' "
                            "WHERE status='pending' AND expires_at<?", (int(time.time()),))


def credit(path, invoice_id: int, cfg, months: int = 1) -> None:
    """Idempotent credit. Caller guarantees the invoice row is still 'pending'."""
    invoice = dict(db.fetch_one(path, "SELECT * FROM invoices WHERE id=?", (invoice_id,)))
    now = int(time.time())
    account = dict(db.fetch_one(path, "SELECT * FROM accounts WHERE id=?",
                                (invoice["account_id"],)))
    base = max(account["premium_until"], now)
    until = base + months * 30 * 86400
    db.execute(path, "UPDATE invoices SET status='paid',credited_at=?,credited_until=? "
                     "WHERE id=?", (now, until, invoice_id))
    db.execute(path, "UPDATE accounts SET tier='premium',premium_until=? WHERE id=?",
               (until, invoice["account_id"]))
    db.audit(path, invoice["account_id"], "subscription.credited",
             {"ref": invoice["invoice_ref"], "until": until})


def tier_of(account: dict) -> str:
    if account["tier"] == "premium" and account["premium_until"] + GRACE_SECONDS > time.time():
        return "premium"
    return "free"


def limits(cfg, tier: str) -> tuple[int, int]:
    if tier == "premium":
        return cfg.rpc_rate_premium, cfg.rpc_daily_premium
    return cfg.rpc_rate_free, cfg.rpc_daily_free