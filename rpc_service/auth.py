"""Identity, session and step-up authorization.

Threat model notes:
  * project_id is a *public* label, never a credential.
  * An address (or its prefix) is only a lookup hint.
  * Session tokens are opaque, 256-bit, stored as SHA-256 digests.
  * Telegram initData is verified with the documented HMAC scheme; the client
    supplied user id is never trusted on its own.
  * Withdrawals require session + PIN (step-up), see withdrawals.py.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import string
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl

from . import db

B64URL = re_alphabet = string.ascii_letters + string.digits + "-_"


class AuthError(Exception):
    def __init__(self, code: str, message: str, status: int = 401):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


@dataclass(frozen=True)
class Principal:
    account_id: int
    project_id: str
    tier: str
    premium_until: int
    is_public: bool


# ---------------------------------------------------------------- hashing
def hash_secret(secret: str, salt: bytes | None = None) -> tuple[str, str]:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(secret.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return salt.hex(), digest.hex()


def verify_secret(secret: str, salt_hex: str, expected_hex: str) -> bool:
    if not salt_hex or not expected_hex:
        return False
    salt = bytes.fromhex(salt_hex)
    digest = hashlib.scrypt(secret.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return hmac.compare_digest(digest.hex(), expected_hex)


def new_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_project_id(kind: str, tg_user_id: str | None = None) -> str:
    if kind == "telegram" and tg_user_id:
        return f"SH-{tg_user_id}"
    return "SH-" + "".join(secrets.choice(re_alphabet) for _ in range(10))


# ------------------------------------------------------- telegram initData
def verify_init_data(init_data: str, bot_token: str, ttl: int,
                     now: int | None = None) -> dict:
    """Validate Mini App initData. Returns parsed fields on success."""
    if not init_data:
        raise AuthError("init_data_missing", "initData is empty", 401)
    if not bot_token:
        raise AuthError("bot_not_configured", "dashboard bot token not configured", 503)
    pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=False)
    seen: dict[str, str] = {}
    for key, value in pairs:
        if key in seen:
            raise AuthError("init_data_duplicate", f"duplicate field {key}", 400)
        seen[key] = value
    received = seen.pop("hash", None)
    if not received:
        raise AuthError("init_data_unsigned", "hash field missing", 401)
    seen.pop("signature", None)          # ed25519 field, not part of the HMAC
    check_string = "\n".join(f"{k}={seen[k]}" for k in sorted(seen))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret_key, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        raise AuthError("init_data_bad_signature", "initData signature invalid", 401)
    try:
        auth_date = int(seen.get("auth_date", "0"))
    except ValueError:
        raise AuthError("init_data_bad_date", "auth_date malformed", 400) from None
    current = int(now if now is not None else time.time())
    if auth_date <= 0 or auth_date > current + 300:
        raise AuthError("init_data_stale", "auth_date out of range", 401)
    if current - auth_date > ttl:
        raise AuthError("init_data_expired", "initData expired", 401)
    try:
        user = json.loads(seen.get("user", "{}"))
    except json.JSONDecodeError:
        raise AuthError("init_data_bad_user", "user payload malformed", 400) from None
    if not isinstance(user, dict) or not user.get("id"):
        raise AuthError("init_data_no_user", "user missing from initData", 401)
    if user.get("is_bot"):
        raise AuthError("init_data_bot_user", "bot accounts cannot sign in", 403)
    return {"user": user, "auth_date": auth_date, "start_param": seen.get("start_param")}


# --------------------------------------------------------------- accounts
def create_account(path, kind: str, *, tg_user_id: str | None = None,
                   tg_username: str | None = None, label: str | None = None,
                   auth_secret: str | None = None) -> dict:
    now = int(time.time())
    project_id = new_project_id(kind, tg_user_id)
    salt = digest = None
    if auth_secret:
        salt, digest = hash_secret(auth_secret)
    account_id = db.execute(
        path,
        "INSERT INTO accounts(project_id,kind,tg_user_id,tg_username,label,auth_salt,"
        "auth_hash,created_at,last_seen_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (project_id, kind, tg_user_id, tg_username, label, salt, digest, now, now),
    )
    db.audit(path, account_id, "account.created", {"kind": kind})
    return get_account_by_id(path, account_id)


def get_account_by_id(path, account_id: int) -> dict:
    row = db.fetch_one(path, "SELECT * FROM accounts WHERE id=?", (account_id,))
    if not row:
        raise AuthError("account_missing", "account not found", 404)
    return dict(row)


def get_account_by_project(path, project_id: str) -> dict | None:
    row = db.fetch_one(path, "SELECT * FROM accounts WHERE project_id=?", (project_id,))
    return dict(row) if row else None


def get_account_by_tg(path, tg_user_id: str) -> dict | None:
    row = db.fetch_one(path, "SELECT * FROM accounts WHERE tg_user_id=?", (tg_user_id,))
    return dict(row) if row else None


# --------------------------------------------------------------- sessions
def issue_session(path, account_id: int, ttl: int, user_agent: str | None = None) -> dict:
    token = new_token()
    now = int(time.time())
    expires = now + ttl
    db.execute(path, "INSERT INTO sessions(token_hash,account_id,created_at,expires_at,user_agent)"
                     " VALUES(?,?,?,?,?)", (token_digest(token), account_id, now, expires,
                                            (user_agent or "")[:180]))
    return {"session_token": token, "expires_at": expires}


def resolve_session(path, token: str) -> Principal:
    if not token:
        raise AuthError("session_missing", "session token missing", 401)
    row = db.fetch_one(path, "SELECT * FROM sessions WHERE token_hash=?",
                       (token_digest(token),))
    if not row:
        raise AuthError("session_invalid", "session invalid", 401)
    if row["revoked"]:
        raise AuthError("session_revoked", "session revoked", 401)
    if row["expires_at"] < int(time.time()):
        raise AuthError("session_expired", "session expired", 401)
    account = get_account_by_id(path, row["account_id"])
    return Principal(account_id=account["id"], project_id=account["project_id"],
                     tier=account["tier"], premium_until=account["premium_until"],
                     is_public=bool(account["is_public"]))


def revoke_session(path, token: str) -> None:
    db.execute(path, "UPDATE sessions SET revoked=1 WHERE token_hash=?", (token_digest(token),))


def revoke_all_sessions(path, account_id: int) -> None:
    db.execute(path, "UPDATE sessions SET revoked=1 WHERE account_id=?", (account_id,))


def bearer(header_value: str | None) -> str:
    if not header_value:
        return ""
    parts = header_value.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return ""


# -------------------------------------------------------------------- pin
def set_pin(path, account_id: int, pin: str, current_pin: str | None = None) -> None:
    if not (pin.isdigit() and len(pin) == 6):
        raise AuthError("pin_format", "PIN must be exactly 6 digits", 400)
    account = get_account_by_id(path, account_id)
    if account["pin_hash"] and not current_pin:
        raise AuthError("pin_current_required", "current PIN required", 400)
    if account["pin_hash"] and not verify_secret(current_pin or "", account["pin_salt"],
                                                 account["pin_hash"]):
        raise AuthError("pin_current_wrong", "current PIN wrong", 403)
    salt, digest = hash_secret(pin)
    db.execute(path, "UPDATE accounts SET pin_salt=?,pin_hash=?,pin_fails=0,"
                     "pin_locked_until=0 WHERE id=?", (salt, digest, account_id))
    db.audit(path, account_id, "pin.set")


def check_pin(path, account_id: int, pin: str, max_fails: int, lock_minutes: int) -> None:
    account = get_account_by_id(path, account_id)
    if not account["pin_hash"]:
        raise AuthError("pin_not_set", "set a withdrawal PIN first", 409)
    now = int(time.time())
    if account["pin_locked_until"] and account["pin_locked_until"] > now:
        raise AuthError("pin_locked", "PIN locked, try later", 429)
    if not verify_secret(pin or "", account["pin_salt"], account["pin_hash"]):
        fails = int(account["pin_fails"]) + 1
        if fails >= max_fails:
            db.execute(path, "UPDATE accounts SET pin_fails=0,pin_locked_until=? WHERE id=?",
                       (now + lock_minutes * 60, account_id))
            db.audit(path, account_id, "pin.locked", {"fails": fails})
            raise AuthError("pin_locked", "too many attempts, PIN locked", 429)
        db.execute(path, "UPDATE accounts SET pin_fails=? WHERE id=?", (fails, account_id))
        raise AuthError("pin_wrong", "PIN wrong", 403)
    db.execute(path, "UPDATE accounts SET pin_fails=0 WHERE id=?", (account_id,))