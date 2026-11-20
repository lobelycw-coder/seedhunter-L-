"""On-chain invoice verification and background polling.

Rules enforced here:
  * destination address must match the invoice exactly,
  * currency must be the invoice currency on the invoice network,
  * amount must be >= expected minus tolerance, overpayment is accepted,
  * confirmations must reach the configured threshold before crediting,
  * a txid can only ever credit one invoice (UNIQUE constraint + guard),
  * wrong-network / expired / underpaid payments are recorded, not credited.
"""
from __future__ import annotations

import asyncio
import logging
import time

import httpx

from . import db, subscriptions
from .rpc_engine import RpcError

log = logging.getLogger("payments")
TERMINAL = {"paid", "expired", "cancelled", "wrong_network", "underpaid"}


def _int(value, default=0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


async def _evm_payment(cfg, engine, invoice: dict, txid: str) -> dict:
    network = invoice["network"]
    tx = (await engine.forward(network, {"jsonrpc": "2.0", "id": 1,
                                         "method": "eth_getTransactionByHash",
                                         "params": [txid]}))[0]
    if not tx.get("result"):
        return {"state": "pending", "reason": "transaction not found on network"}
    result = tx["result"]
    receipt = (await engine.forward(network, {"jsonrpc": "2.0", "id": 2,
                                             "method": "eth_getTransactionReceipt",
                                             "params": [txid]}))[0].get("result")
    if not result.get("to") or result["to"].lower() != invoice["address"].lower():
        return {"state": "wrong_network", "reason": "destination mismatch"}
    value = _int(result.get("value"), -1)
    if value < 0:
        return {"state": "pending", "reason": "value unreadable"}
    if receipt is None:
        return {"state": "pending", "reason": "unconfirmed", "value": value}
    if _int(receipt.get("status")) != 1:
        return {"state": "failed", "reason": "transaction reverted", "value": value}
    head = _int((await engine.forward(network, {"jsonrpc": "2.0", "id": 3,
                                                "method": "eth_blockNumber",
                                                "params": []}))[0].get("result"))
    confirmations = max(0, head - _int(receipt.get("blockNumber")) + 1)
    return {"state": "confirmed" if confirmations >= cfg.confirmations[invoice["currency"]]
            else "pending", "value": value, "confirmations": confirmations,
            "reason": f"{confirmations} confirmations"}


async def _sol_payment(cfg, engine, invoice: dict, txid: str) -> dict:
    rows = await engine.forward("solana", {"jsonrpc": "2.0", "id": 1,
                                           "method": "getTransaction",
                                           "params": [txid, {"encoding": "jsonParsed",
                                                             "maxSupportedTransactionVersion": 0}]})
    result = rows[0].get("result")
    if not result:
        return {"state": "pending", "reason": "transaction not found"}
    meta = result.get("meta") or {}
    if meta.get("err"):
        return {"state": "failed", "reason": "transaction failed"}
    keys = [k.get("pubkey") if isinstance(k, dict) else k
            for k in (result.get("transaction", {}).get("message", {}).get("accountKeys") or [])]
    if invoice["address"] not in keys:
        return {"state": "wrong_network", "reason": "destination not in transaction"}
    index = keys.index(invoice["address"])
    pre = (meta.get("preBalances") or [])
    post = (meta.get("postBalances") or [])
    if index >= len(pre) or index >= len(post):
        return {"state": "pending", "reason": "balances unreadable"}
    value = _int(post[index]) - _int(pre[index])
    confirmations = cfg.confirmations["SOL"] if result.get("slot") else 0
    state = "confirmed" if confirmations >= cfg.confirmations["SOL"] else "pending"
    return {"state": state, "value": value, "confirmations": confirmations}


async def _btc_payment(cfg, invoice: dict, txid: str) -> dict:
    async with httpx.AsyncClient(timeout=10, base_url=cfg.btc_api) as client:
        response = await client.get(f"/tx/{txid}")
        if response.status_code == 404:
            return {"state": "pending", "reason": "transaction not found"}
        response.raise_for_status()
        tx = response.json()
        status = tx.get("status") or {}
        if status.get("confirmed"):
            confirmations = _int((await client.get(f"/blocks/tip/height")).json()) \
                - _int(status.get("block_height")) + 1
        else:
            confirmations = 0
    value = 0
    for out in tx.get("vout", []):
        if out.get("scriptpubkey_address") == invoice["address"]:
            value += _int(out.get("value"))
    if value == 0:
        return {"state": "wrong_network", "reason": "destination mismatch"}
    state = "confirmed" if confirmations >= cfg.confirmations["BTC"] else "pending"
    return {"state": state, "value": value, "confirmations": confirmations}


async def verify(cfg, engine, path, invoice_ref: str, txid: str) -> dict:
    invoice = subscriptions.get_invoice(path, invoice_ref)
    if invoice["status"] == "paid":
        return {"state": "paid", "already_credited": True,
                "invoice": subscriptions.public_invoice(invoice, cfg)}
    if invoice["status"] in ("cancelled",):
        return {"state": invoice["status"], "invoice": subscriptions.public_invoice(invoice, cfg)}

    duplicate = db.fetch_one(path, "SELECT invoice_ref FROM invoices WHERE txid=? "
                                   "AND invoice_ref<>?", (txid, invoice_ref))
    if duplicate:
        return {"state": "duplicate", "credited_to": duplicate["invoice_ref"],
                "invoice": subscriptions.public_invoice(invoice, cfg)}

    if invoice["currency"] == "BTC":
        outcome = await _btc_payment(cfg, invoice, txid)
    elif invoice["currency"] == "SOL":
        outcome = await _sol_payment(cfg, engine, invoice, txid)
    else:
        outcome = await _evm_payment(cfg, engine, invoice, txid)

    state = outcome["state"]
    value = _int(outcome.get("value"))
    confirmations = _int(outcome.get("confirmations"))
    expected = _int(invoice["amount_raw"])
    floor = expected - expected * cfg.underpay_tolerance_bps // 10_000

    if state in ("pending", "failed") and value and value < floor:
        state = "underpaid" if invoice["expires_at"] < int(time.time()) else state
    if state == "confirmed" and value < floor:
        state = "underpaid"

    db.execute(path, "UPDATE invoices SET confirmations=? WHERE id=? AND status='pending'",
               (confirmations, invoice["id"]))
    if state == "confirmed" and invoice["status"] == "pending":
        db.execute(path, "UPDATE invoices SET txid=? WHERE id=? AND txid IS NULL",
                   (txid, invoice["id"]))
        fresh = dict(db.fetch_one(path, "SELECT * FROM invoices WHERE id=?", (invoice["id"],)))
        if fresh["status"] == "pending":
            subscriptions.credit(path, invoice["id"], cfg)
        db.audit(path, invoice["account_id"], "payment.confirmed",
                 {"ref": invoice_ref, "txid": txid, "value": value})
    elif state in ("wrong_network", "underpaid", "failed"):
        db.execute(path, "UPDATE invoices SET status=?,note=? WHERE id=? AND status='pending'",
                   (state, f"{outcome.get('reason', '')} txid={txid}", invoice["id"]))
        db.audit(path, invoice["account_id"], f"payment.{state}",
                 {"ref": invoice_ref, "txid": txid, "value": value})

    invoice = subscriptions.get_invoice(path, invoice_ref)
    return {"state": subscriptions.effective_status(invoice, cfg),
            "value": value, "confirmations": confirmations,
            "reason": outcome.get("reason"),
            "invoice": subscriptions.public_invoice(invoice, cfg)}


async def run_loop(cfg, engine, path, stop: asyncio.Event) -> None:
    """Poll pending invoices. Only started when SH_MONITOR_ENABLED=1."""
    while not stop.is_set():
        try:
            subscriptions.expire_stale(path)
            rows = db.fetch_all(path, "SELECT invoice_ref FROM invoices "
                                      "WHERE status='pending' AND txid IS NOT NULL")
            for row in rows:
                invoice = subscriptions.get_invoice(path, row["invoice_ref"])
                try:
                    await verify(cfg, engine, path, invoice["invoice_ref"], invoice["txid"])
                except (RpcError, httpx.HTTPError) as exc:
                    log.warning("recheck %s failed: %s", invoice["invoice_ref"], exc)
        except Exception:                                   # pragma: no cover
            log.exception("monitor tick failed")
        try:
            await asyncio.wait_for(stop.wait(), cfg.monitor_interval)
        except asyncio.TimeoutError:
            pass