"""Shared test helpers."""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from pathlib import Path
from urllib.parse import urlencode

from rpc_service.settings import Settings

BOT_TOKEN = "123456:TEST-TOKEN"


def make_cfg(tmp: Path, **over) -> Settings:
    env = {"SH_DB_PATH": str(tmp / "service.db"), "SH_ADMIN_TOKEN": "admin-test",
           "SH_TG_DASH_BOT_TOKEN": BOT_TOKEN, "SH_DEPOSIT_ETH": "0x" + "11" * 20,
           "SH_DEPOSIT_BNB": "0x" + "22" * 20,
           "SH_DEPOSIT_SOL": "9xQeWvG816bUx9EPf2RQPp3yVxRsxVYpRfKMRwSbXk4m",
           "SH_DEPOSIT_BTC": "bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq"}
    env.update({k: str(v) for k, v in over.items()})
    return Settings.load(env)


def sign_init_data(user: dict, auth_date: int | None = None,
                   token: str = BOT_TOKEN, tamper: bool = False) -> str:
    fields = {"auth_date": str(auth_date if auth_date is not None else int(time.time())),
              "query_id": "AAF", "user": json.dumps(user, separators=(",", ":"))}
    check = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    digest = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    fields["hash"] = "0" * 64 if tamper else digest
    return urlencode(fields)


class FakeEngine:
    def __init__(self, script=None, networks=("ethereum", "solana")):
        self.script = script or {}
        self.networks = {n: None for n in networks}
        self.calls = []

    def resolve(self, name):
        if name not in self.networks:
            from rpc_service.rpc_engine import RpcError
            raise RpcError("network_unsupported", name, 404)
        return type("N", (), {"name": name, "kind": "solana" if name == "solana" else "evm",
                              "symbol": "SOL" if name == "solana" else "ETH",
                              "decimals": 9 if name == "solana" else 18})()

    async def forward(self, name, payload):
        call = payload[0] if isinstance(payload, list) else payload
        self.calls.append((name, call.get("method")))
        queue = self.script.get(call.get("method"), [])
        if not queue:
            return [{"jsonrpc": "2.0", "id": 1, "result": None}]
        return [{"jsonrpc": "2.0", "id": 1, "result": queue.pop(0)}]