"""HTTP surface: API, JSON-RPC proxy, website and Mini App.

One process, one backend, two front doors. Imports no hunter worker, so no
ProcessPoolExecutor and no cursor file is touched.
"""
from __future__ import annotations

import asyncio
import base64
import collections
import logging
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from . import (auth, bridge_api, db, payment_monitor, rpc_engine, subscriptions,
               wallet_accounts, withdrawals)
from .settings import Settings, get_settings

BASE_DIR = Path(__file__).resolve().parent
log = logging.getLogger("rpc_service")

CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline' https://telegram.org; "
       "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
       "connect-src 'self'; frame-ancestors https://*.telegram.org https://web.telegram.org; "
       "base-uri 'none'; form-action 'self'")


class HTTPProblem(Exception):
    def __init__(self, code: str, message: str, status: int):
        self.code, self.message, self.status = code, message, status


class RateLimiter:
    def __init__(self) -> None:
        self.hits: dict[str, collections.deque] = collections.defaultdict(collections.deque)

    def allow(self, key: str, per_minute: int) -> bool:
        now = time.time()
        window = self.hits[key]
        while window and now - window[0] > 60:
            window.popleft()
        if len(window) >= per_minute:
            return False
        window.append(now)
        return True


def client_ip(request: Request, cfg: Settings) -> str:
    if cfg.trust_proxy:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def error(code: str, message: str, status: int, **extra) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message, **extra}}, status_code=status)


class TelegramBody(BaseModel):
    init_data: str = Field(min_length=8, max_length=8192)


class RegisterBody(BaseModel):
    label: str = Field(default="", max_length=64)


class LoginBody(BaseModel):
    project_id: str = Field(max_length=64)
    auth_secret: str = Field(min_length=16, max_length=200)


class PinBody(BaseModel):
    pin: str = Field(min_length=6, max_length=6)
    current_pin: str | None = Field(default=None, max_length=6)


class WalletBody(BaseModel):
    network: str


class InvoiceBody(BaseModel):
    currency: str


class VerifyBody(BaseModel):
    invoice_ref: str
    txid: str = Field(min_length=32, max_length=128)


class WithdrawBody(BaseModel):
    network: str
    destination: str = Field(max_length=128)
    amount_raw: int = Field(gt=0)
    pin: str = Field(min_length=6, max_length=6)
    idempotency_key: str | None = Field(default=None, max_length=64)


class PublicBody(BaseModel):
    is_public: bool


class SuspendBody(BaseModel):
    suspended: bool


def build_app(cfg: Settings | None = None) -> FastAPI:
    cfg = cfg or get_settings()
    limiter = RateLimiter()
    stop = asyncio.Event()
    engine_holder: dict[str, rpc_engine.RpcEngine] = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db.init(cfg.db_path)
        engine = rpc_engine.RpcEngine(cfg)
        engine_holder["engine"] = engine
        task = None
        if cfg.monitor_enabled:
            task = asyncio.create_task(
                payment_monitor.run_loop(cfg, engine, cfg.db_path, stop))
            log.info("payment monitor started (interval %ss)", cfg.monitor_interval)
        log.info("rpc_service ready: %d networks, db=%s", len(engine.networks), cfg.db_path)
        try:
            yield
        finally:
            stop.set()
            if task:
                task.cancel()
            await engine.close()

    app = FastAPI(title="SeedHunter add-on", version="0.2.0", lifespan=lifespan,
                  docs_url="/api/docs", openapi_url="/api/openapi.json")
    app.state.cfg = cfg

    @app.exception_handler(HTTPProblem)
    async def _problem(_request: Request, exc: HTTPProblem):
        return error(exc.code, exc.message, exc.status)

    @app.exception_handler(auth.AuthError)
    async def _auth(_request: Request, exc: auth.AuthError):
        return error(exc.code, exc.message, exc.status)

    if cfg.cors_origins:
        app.add_middleware(CORSMiddleware, allow_origins=cfg.cors_origins,
                           allow_credentials=False, allow_methods=["GET", "POST", "OPTIONS"],
                           allow_headers=["Authorization", "Content-Type", "X-Admin-Token"],
                           max_age=600)

    @app.middleware("http")
    async def hardening(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "ALLOW-FROM https://telegram.org"
        response.headers["Content-Security-Policy"] = CSP
        response.headers["Cache-Control"] = "no-store"
        return response

    def engine() -> rpc_engine.RpcEngine:
        return engine_holder["engine"]

    def principal(request: Request, authorization: str | None = Header(default=None)):
        try:
            token = auth.bearer(authorization)
            found = auth.resolve_session(cfg.db_path, token)
        except auth.AuthError as exc:
            raise HTTPProblem(exc.code, exc.message, exc.status) from None
        db.execute(cfg.db_path, "UPDATE accounts SET last_seen_at=? WHERE id=?",
                   (int(time.time()), found.account_id))
        return found

    def admin_token(x_admin_token: str | None = Header(default=None)) -> None:
        if not cfg.admin_token:
            raise HTTPProblem("admin_not_configured", "SH_ADMIN_TOKEN unset", 503)
        if not x_admin_token or not secrets.compare_digest(x_admin_token, cfg.admin_token):
            raise HTTPProblem("admin_forbidden", "bad admin token", 403)

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(BASE_DIR / "miniapp" / "index.html")

    @app.get("/w/{project_id}", include_in_schema=False)
    async def public_page(project_id: str):
        return FileResponse(BASE_DIR / "miniapp" / "index.html")

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok", "networks": len(engine().networks),
                "monitor": cfg.monitor_enabled, "time": int(time.time())}

    @app.get("/api/meta")
    async def meta():
        return {"plans": [{"id": "free", "label": "Free", "price_usd": 0},
                          {"id": "premium", "label": "Premium",
                           "price_usd": cfg.premium_price_usd}],
                "currencies": list(subscriptions.CURRENCIES),
                "withdrawals_enabled": cfg.withdrawals_enabled,
                "telegram_enabled": bool(cfg.tg_bot_token),
                "networks": engine().catalogue()}

    @app.get("/api/networks")
    async def networks():
        return {"networks": engine().catalogue()}

    @app.get("/api/networks/health")
    async def networks_health():
        return {"probes": await engine().probe_all()}

    @app.post("/api/auth/telegram")
    async def auth_telegram(body: TelegramBody, request: Request):
        try:
            data = auth.verify_init_data(body.init_data, cfg.tg_bot_token, cfg.initdata_ttl)
        except auth.AuthError as exc:
            return error(exc.code, exc.message, exc.status)
        user = data["user"]
        tg_id = str(user["id"])
        account = auth.get_account_by_tg(cfg.db_path, tg_id)
        if account is None:
            account = auth.create_account(cfg.db_path, "telegram", tg_user_id=tg_id,
                                          tg_username=user.get("username"),
                                          label=user.get("first_name"))
        session = auth.issue_session(cfg.db_path, account["id"], cfg.session_ttl,
                                     request.headers.get("user-agent"))
        db.audit(cfg.db_path, account["id"], "auth.telegram", {"tg_id": tg_id},
                 client_ip(request, cfg))
        return {"project_id": account["project_id"], "tier": account["tier"], **session}

    @app.post("/api/auth/register")
    async def auth_register(body: RegisterBody, request: Request):
        secret = auth.new_token(24)
        account = auth.create_account(cfg.db_path, "web", label=body.label or None,
                                      auth_secret=secret)
        session = auth.issue_session(cfg.db_path, account["id"], cfg.session_ttl,
                                     request.headers.get("user-agent"))
        db.audit(cfg.db_path, account["id"], "auth.register", None, client_ip(request, cfg))
        return {"project_id": account["project_id"], "auth_secret": secret,
                "tier": account["tier"], **session,
                "note": "store auth_secret now, it is shown once"}

    @app.post("/api/auth/login")
    async def auth_login(body: LoginBody, request: Request):
        account = auth.get_account_by_project(cfg.db_path, body.project_id.strip())
        if not account or not account["auth_hash"]:
            return error("login_failed", "project id or secret wrong", 401)
        if not auth.verify_secret(body.auth_secret, account["auth_salt"], account["auth_hash"]):
            db.audit(cfg.db_path, account["id"], "auth.login_failed", None,
                     client_ip(request, cfg))
            return error("login_failed", "project id or secret wrong", 401)
        session = auth.issue_session(cfg.db_path, account["id"], cfg.session_ttl,
                                     request.headers.get("user-agent"))
        return {"project_id": account["project_id"], "tier": account["tier"], **session}

    @app.post("/api/auth/logout")
    async def auth_logout(request: Request, authorization: str | None = Header(default=None)):
        auth.revoke_session(cfg.db_path, auth.bearer(authorization))
        return {"ok": True}

    @app.post("/api/auth/pin")
    async def auth_pin(body: PinBody, who=Depends(principal)):
        try:
            auth.set_pin(cfg.db_path, who.account_id, body.pin, body.current_pin)
        except auth.AuthError as exc:
            return error(exc.code, exc.message, exc.status)
        return {"ok": True}

    @app.get("/api/me")
    async def me(who=Depends(principal)):
        account = auth.get_account_by_id(cfg.db_path, who.account_id)
        tier = subscriptions.tier_of(account)
        rate, daily = subscriptions.limits(cfg, tier)
        used = db.usage_today(cfg.db_path, who.account_id,
                              time.strftime("%Y-%m-%d", time.gmtime()))
        return {"project_id": account["project_id"], "tier": tier,
                "premium_until": account["premium_until"],
                "is_public": bool(account["is_public"]),
                "pin_set": bool(account["pin_hash"]),
                "withdrawal_suspended": bool(account["withdrawal_suspended"]),
                "withdrawals_enabled": cfg.withdrawals_enabled,
                "limits": {"rpc_per_minute": rate, "rpc_daily": daily,
                           "rpc_used_today": used}}

    @app.post("/api/me/public")
    async def set_public(body: PublicBody, who=Depends(principal)):
        wallet_accounts.set_public(cfg.db_path, who.account_id, body.is_public)
        return {"is_public": body.is_public}

    @app.get("/api/public/{project_id}")
    async def public_profile(project_id: str):
        profile = wallet_accounts.public_profile(cfg.db_path, project_id.strip())
        if not profile:
            return error("not_public", "no public profile for that project id", 404)
        return profile

    @app.get("/api/wallets")
    async def list_wallets(who=Depends(principal)):
        rows = wallet_accounts.list_wallets(cfg.db_path, who.account_id)
        return {"wallets": [wallet_accounts.public_wallet(w) for w in rows]}

    @app.post("/api/wallets")
    async def create_wallet(body: WalletBody, who=Depends(principal)):
        from . import signer
        network = body.network.strip().lower()
        ok, why = signer.available(cfg, network)
        if not ok:
            return error("signer_unavailable", f"cannot derive an address yet: {why}", 503)
        try:
            if network == "solana":
                address = signer.solana_address_of(cfg, cfg.kms_key_sol)
                key_ref = cfg.kms_key_sol
            else:
                address = signer.evm_address_of(cfg, cfg.kms_key_evm)
                key_ref = cfg.kms_key_evm
            wallet = wallet_accounts.create_wallet(cfg.db_path, who.account_id, network,
                                                   address, key_ref)
        except (wallet_accounts.WalletError, signer.SignerError) as exc:
            return error(getattr(exc, "code", "wallet_error"), str(exc),
                         getattr(exc, "status", 400))
        except Exception as exc:
            return error("kms_error", f"{type(exc).__name__}: {exc}", 502)
        return wallet_accounts.public_wallet(wallet)

    @app.get("/api/balances")
    async def balances(who=Depends(principal)):
        out = []
        for wallet in wallet_accounts.list_wallets(cfg.db_path, who.account_id):
            try:
                out.append(await wallet_accounts.balance(engine(), wallet))
            except (rpc_engine.RpcError, ValueError) as exc:
                out.append({"network": wallet["network"], "error": str(exc),
                            "address": wallet["address"]})
        return {"balances": out}

    @app.get("/api/transactions")
    async def transactions(who=Depends(principal)):
        return {"transactions": wallet_accounts.history(cfg.db_path, who.account_id)}

    @app.get("/api/subscriptions")
    async def subscription(who=Depends(principal)):
        account = auth.get_account_by_id(cfg.db_path, who.account_id)
        rows = db.fetch_all(cfg.db_path, "SELECT * FROM invoices WHERE account_id=? "
                                         "ORDER BY id DESC LIMIT 20", (who.account_id,))
        return {"tier": subscriptions.tier_of(account),
                "premium_until": account["premium_until"],
                "price_usd": cfg.premium_price_usd,
                "currencies": list(subscriptions.CURRENCIES),
                "invoices": [subscriptions.public_invoice(dict(r), cfg) for r in rows]}

    @app.post("/api/subscriptions/invoice")
    async def new_invoice(body: InvoiceBody, who=Depends(principal)):
        try:
            invoice = await subscriptions.create_invoice(cfg, cfg.db_path, who.account_id,
                                                         body.currency)
        except subscriptions.SubscriptionError as exc:
            return error(exc.code, exc.message, exc.status)
        return subscriptions.public_invoice(invoice, cfg)

    @app.post("/api/payments/verify")
    async def verify_payment(body: VerifyBody, who=Depends(principal)):
        invoice = db.fetch_one(cfg.db_path, "SELECT account_id FROM invoices "
                                            "WHERE invoice_ref=?", (body.invoice_ref,))
        if not invoice or invoice["account_id"] != who.account_id:
            return error("invoice_missing", "invoice not found", 404)
        try:
            return await payment_monitor.verify(cfg, engine(), cfg.db_path,
                                                body.invoice_ref, body.txid.strip())
        except subscriptions.SubscriptionError as exc:
            return error(exc.code, exc.message, exc.status)
        except rpc_engine.RpcError as exc:
            return error(exc.code, exc.message, exc.status, **{"detail": exc.detail})

    @app.get("/api/withdrawals")
    async def list_withdrawals(who=Depends(principal)):
        rows = db.fetch_all(cfg.db_path, "SELECT * FROM withdrawals WHERE account_id=? "
                                         "ORDER BY id DESC LIMIT 50", (who.account_id,))
        return {"withdrawals": [dict(r) for r in rows],
                "reconciliation": withdrawals.reconcile(cfg.db_path, who.account_id)}

    @app.post("/api/withdrawals")
    async def request_withdrawal(body: WithdrawBody, request: Request, who=Depends(principal)):
        account = auth.get_account_by_id(cfg.db_path, who.account_id)
        try:
            record = await withdrawals.create(
                cfg, engine(), cfg.db_path, account, network=body.network.strip().lower(),
                destination=body.destination, amount_raw=body.amount_raw, pin=body.pin,
                idem_key=body.idempotency_key, ip=client_ip(request, cfg))
        except (auth.AuthError, wallet_accounts.WalletError) as exc:
            return error(exc.code, exc.message, exc.status)
        except withdrawals.WithdrawalError as exc:
            return error(exc.code, exc.message, exc.status)
        return record

    @app.post("/api/admin/withdrawals/{wd_ref}/approve", dependencies=[Depends(admin_token)])
    async def approve_withdrawal(wd_ref: str):
        row = db.fetch_one(cfg.db_path, "SELECT * FROM withdrawals WHERE wd_ref=?", (wd_ref,))
        if not row:
            return error("withdrawal_missing", "not found", 404)
        record = dict(row)
        if record["status"] not in ("requested", "failed"):
            return error("bad_state", f"status is {record['status']}", 409)
        return await withdrawals.approve_and_sign(cfg, engine(), cfg.db_path, record)

    @app.post("/api/admin/withdrawals/{wd_ref}/broadcast", dependencies=[Depends(admin_token)])
    async def broadcast(wd_ref: str):
        row = db.fetch_one(cfg.db_path, "SELECT * FROM withdrawals WHERE wd_ref=?", (wd_ref,))
        if not row:
            return error("withdrawal_missing", "not found", 404)
        if row["status"] != "signed" or not row["txid"]:
            return error("bad_state", "needs a signed raw transaction", 409)
        if row["network"] == "solana":
            method = "sendTransaction"
            params = [base64.b64encode(bytes.fromhex(row["txid"])).decode(),
                      {"encoding": "base64"}]
        else:
            method, params = "eth_sendRawTransaction", [row["txid"]]
        try:
            rows = await engine().forward(row["network"],
                                          {"jsonrpc": "2.0", "id": 1, "method": method,
                                           "params": params})
        except rpc_engine.RpcError as exc:
            return error(exc.code, exc.message, exc.status)
        txid = rows[0].get("result")
        if not txid:
            withdrawals.set_status(cfg.db_path, row["id"], "failed",
                                   reason=str(rows[0].get("error"))[:300],
                                   account_id=row["account_id"])
            return error("broadcast_failed", "node rejected the transaction", 502)
        withdrawals.set_status(cfg.db_path, row["id"], "broadcast", txid=str(txid),
                               account_id=row["account_id"])
        return {"txid": txid, "status": "broadcast"}

    @app.post("/api/admin/accounts/{project_id}/suspend", dependencies=[Depends(admin_token)])
    async def suspend(project_id: str, body: SuspendBody):
        account = auth.get_account_by_project(cfg.db_path, project_id)
        if not account:
            return error("account_missing", "not found", 404)
        withdrawals.suspend(cfg.db_path, account["id"], body.suspended)
        return {"project_id": project_id, "suspended": body.suspended}

    @app.post("/api/admin/probe", dependencies=[Depends(admin_token)])
    async def probe():
        return {"probes": await engine().probe_all()}

    @app.post("/rpc/{network}")
    async def rpc(network: str, request: Request, who=Depends(principal)):
        account = auth.get_account_by_id(cfg.db_path, who.account_id)
        tier = subscriptions.tier_of(account)
        rate, daily = subscriptions.limits(cfg, tier)
        if not limiter.allow(f"rpc:{who.account_id}", rate):
            return error("rate_limited", f"{rate} requests/minute", 429)
        today = time.strftime("%Y-%m-%d", time.gmtime())
        if db.usage_today(cfg.db_path, who.account_id, today) >= daily:
            return error("daily_quota", f"{daily} requests/day on {tier}", 429)
        try:
            payload = await request.json()
        except Exception:
            return error("bad_json", "body is not JSON", 400)
        try:
            result = await engine().forward(network, payload)
        except rpc_engine.RpcError as exc:
            return error(exc.code, exc.message, exc.status,
                         **({"detail": exc.detail} if exc.detail else {}))
        db.bump_usage(cfg.db_path, who.account_id, today)
        return result

    bridge_api.register(app, cfg, admin_token)
    return app


logging.basicConfig(level=get_settings().log_level,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
app = build_app()