"""Add-on configuration.

Precedence (highest first): process environment -> rpc_service/.env -> repo root .env.

The repo root .env is only read as a *fallback* so existing RPC_* names can be
reused without duplicating secrets. Nothing here writes to any existing file.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # repository root
SERVICE_DIR = Path(__file__).resolve().parent          # rpc_service/


def _load_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    try:
        from dotenv import dotenv_values
    except ImportError:                                 # pragma: no cover
        out: dict[str, str] = {}
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
        return out
    return {k: v for k, v in dotenv_values(path).items() if v is not None}


def layered_env() -> dict[str, str]:
    values: dict[str, str] = {}
    values.update(_load_env_file(ROOT / ".env"))        # lowest priority
    values.update(_load_env_file(SERVICE_DIR / ".env"))
    values.update(os.environ)                           # highest priority
    return values


def _int(env: dict[str, str], key: str, default: int) -> int:
    raw = env.get(key, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _bool(env: dict[str, str], key: str, default: bool) -> bool:
    raw = env.get(key, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    db_path: Path
    public_base_url: str
    cors_origins: list[str]
    trust_proxy: bool

    tg_bot_token: str
    tg_webapp_url: str
    initdata_ttl: int
    session_ttl: int

    premium_price_usd: int
    price_source: str
    price_static: dict[str, int]
    invoice_ttl: int
    deposit_addresses: dict[str, str]
    confirmations: dict[str, int]
    underpay_tolerance_bps: int
    btc_api: str

    rpc_endpoints: dict[str, list[str]]
    provider_keys: dict[str, str]
    rpc_timeout: float
    rpc_max_batch: int
    rpc_rate_free: int
    rpc_rate_premium: int
    rpc_daily_free: int
    rpc_daily_premium: int

    withdraw_min: dict[str, int]
    withdraw_max: dict[str, int]
    withdraw_daily_count: int
    pin_max_fails: int
    pin_lock_minutes: int
    withdrawals_enabled: bool

    kms_region: str
    kms_key_evm: str
    kms_key_sol: str

    monitor_enabled: bool
    monitor_interval: int
    admin_token: str
    log_level: str

    networks_enabled: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, env: dict[str, str] | None = None) -> "Settings":
        e = layered_env() if env is None else env
        net_csv = e.get("SH_NETWORKS", "").strip()
        return cls(
            host=e.get("SH_HOST", "127.0.0.1").strip() or "127.0.0.1",
            port=_int(e, "SH_PORT", 8080),
            db_path=Path(e.get("SH_DB_PATH", "").strip()
                         or SERVICE_DIR / "data" / "service.db"),
            public_base_url=e.get("SH_PUBLIC_BASE_URL", "").strip().rstrip("/"),
            cors_origins=[o.strip() for o in e.get("SH_CORS_ORIGINS", "").split(",") if o.strip()],
            trust_proxy=_bool(e, "SH_TRUST_PROXY", False),
            tg_bot_token=e.get("SH_TG_DASH_BOT_TOKEN", "").strip(),
            tg_webapp_url=(e.get("SH_TG_WEBAPP_URL", "").strip().rstrip("/")
                           or e.get("SH_PUBLIC_BASE_URL", "").strip().rstrip("/")),
            initdata_ttl=_int(e, "SH_INITDATA_TTL", 3600),
            session_ttl=_int(e, "SH_SESSION_TTL", 30 * 86400),
            premium_price_usd=_int(e, "SH_PREMIUM_PRICE_USD", 20),
            price_source=e.get("SH_PRICE_SOURCE", "static").strip().lower() or "static",
            price_static={
                "BTC": _int(e, "SH_PRICE_STATIC_BTC", 95000),
                "ETH": _int(e, "SH_PRICE_STATIC_ETH", 3000),
                "SOL": _int(e, "SH_PRICE_STATIC_SOL", 150),
                "BNB": _int(e, "SH_PRICE_STATIC_BNB", 600),
            },
            invoice_ttl=_int(e, "SH_INVOICE_TTL", 1800),
            deposit_addresses={
                "BTC": e.get("SH_DEPOSIT_BTC", "").strip(),
                "ETH": e.get("SH_DEPOSIT_ETH", "").strip(),
                "SOL": e.get("SH_DEPOSIT_SOL", "").strip(),
                "BNB": e.get("SH_DEPOSIT_BNB", "").strip(),
            },
            confirmations={
                "BTC": _int(e, "SH_CONFIRMATIONS_BTC", 3),
                "ETH": _int(e, "SH_CONFIRMATIONS_ETH", 12),
                "SOL": _int(e, "SH_CONFIRMATIONS_SOL", 32),
                "BNB": _int(e, "SH_CONFIRMATIONS_BNB", 15),
            },
            underpay_tolerance_bps=_int(e, "SH_UNDERPAY_TOLERANCE_BPS", 50),
            btc_api=e.get("SH_BTC_API", "https://mempool.space/api").strip().rstrip("/"),
            rpc_endpoints={n: [u.strip() for u in e.get(f"SH_RPC_{n.upper()}", "").split(",") if u.strip()]
                           for n in ("ethereum", "bsc", "polygon", "base", "arbitrum",
                                     "optimism", "avalanche", "solana")},
            provider_keys={
                "alchemy": e.get("SH_ALCHEMY_KEY", "").strip(),
                "helius": e.get("SH_HELIUS_KEY", "").strip(),
                "ankr": e.get("SH_ANKR_KEY", "").strip(),
            },
            rpc_timeout=float(_int(e, "SH_RPC_TIMEOUT_MS", 8000)) / 1000.0,
            rpc_max_batch=_int(e, "SH_RPC_MAX_BATCH", 20),
            rpc_rate_free=_int(e, "SH_RPC_RATE_FREE", 60),
            rpc_rate_premium=_int(e, "SH_RPC_RATE_PREMIUM", 600),
            rpc_daily_free=_int(e, "SH_RPC_DAILY_FREE", 2000),
            rpc_daily_premium=_int(e, "SH_RPC_DAILY_PREMIUM", 100000),
            withdraw_min={
                "ethereum": _int(e, "SH_WITHDRAW_MIN_ETH", 0),
                "bsc": _int(e, "SH_WITHDRAW_MIN_BNB", 0),
                "solana": _int(e, "SH_WITHDRAW_MIN_SOL", 0),
            },
            withdraw_max={
                "ethereum": _int(e, "SH_WITHDRAW_MAX_ETH", 10**18),
                "bsc": _int(e, "SH_WITHDRAW_MAX_BNB", 10**19),
                "solana": _int(e, "SH_WITHDRAW_MAX_SOL", 10**10),
            },
            withdraw_daily_count=_int(e, "SH_WITHDRAW_DAILY_COUNT", 5),
            pin_max_fails=_int(e, "SH_PIN_MAX_FAILS", 5),
            pin_lock_minutes=_int(e, "SH_PIN_LOCK_MINUTES", 15),
            withdrawals_enabled=_bool(e, "SH_WITHDRAWALS_ENABLED", False),
            kms_region=e.get("SH_KMS_REGION", "eu-central-1").strip(),
            kms_key_evm=e.get("SH_KMS_KEY_EVM", "").strip(),
            kms_key_sol=e.get("SH_KMS_KEY_SOL", "").strip(),
            monitor_enabled=_bool(e, "SH_MONITOR_ENABLED", False),
            monitor_interval=_int(e, "SH_MONITOR_INTERVAL", 60),
            admin_token=e.get("SH_ADMIN_TOKEN", "").strip(),
            log_level=e.get("SH_LOG_LEVEL", "INFO").strip().upper() or "INFO",
            networks_enabled=[n.strip() for n in net_csv.split(",") if n.strip()],
        )

    # -- derived helpers -------------------------------------------------
    @property
    def is_production(self) -> bool:
        return self.public_base_url.startswith("https://")

    def missing_payment_config(self) -> list[str]:
        return [f"SH_DEPOSIT_{c}" for c, a in self.deposit_addresses.items() if not a]


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings.load()
    return _settings


def reset_settings() -> None:
    global _settings
    _settings = None