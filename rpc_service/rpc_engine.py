"""Private RPC router.

Network metadata is read from the *real* repository files, guarded by import
fallbacks, so the add-on never invents a network or a token address:

  * seedhunter/chains.py  -> explorers, native symbols, token lists
  * config.yaml           -> enabled chains + public endpoint seeds
  * RPC_* env vars        -> operator supplied endpoints (documented, read only)

Upstream URLs, provider keys and credentials stay in this process. Only network
names, health and usage are ever serialized towards clients.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import httpx

ROOT = Path(__file__).resolve().parent.parent

EVM_METHODS = {
    "eth_blockNumber", "eth_chainId", "eth_getBalance", "eth_getCode",
    "eth_getStorageAt", "eth_getTransactionByHash", "eth_getTransactionReceipt",
    "eth_getTransactionCount", "eth_getBlockByNumber", "eth_getBlockByHash",
    "eth_getLogs", "eth_call", "eth_estimateGas", "eth_gasPrice",
    "eth_maxPriorityFeePerGas", "eth_feeHistory", "net_version",
}
EVM_WRITE_METHODS = {"eth_sendRawTransaction"}
SOL_METHODS = {
    "getBalance", "getAccountInfo", "getMultipleAccounts", "getTransaction",
    "getSignaturesForAddress", "getLatestBlockhash", "getBlockHeight", "getSlot",
    "getTokenAccountsByOwner", "getTokenAccountBalance", "getFeeForMessage",
    "simulateTransaction", "getHealth", "getVersion", "getRecentBlockhash",
    "getMinimumBalanceForRentExemption", "getRecentPrioritizationFees", "getEpochInfo",
}
SOL_WRITE_METHODS = {"sendTransaction"}

PROVIDER_TEMPLATES = {
    # verified public provider URL shapes; a network is only "up" after a probe
    "alchemy": {
        "ethereum": "https://eth-mainnet.g.alchemy.com/v2/{key}",
        "polygon": "https://polygon-mainnet.g.alchemy.com/v2/{key}",
        "base": "https://base-mainnet.g.alchemy.com/v2/{key}",
        "arbitrum": "https://arb-mainnet.g.alchemy.com/v2/{key}",
        "optimism": "https://opt-mainnet.g.alchemy.com/v2/{key}",
        "bsc": "https://bnb-mainnet.g.alchemy.com/v2/{key}",
    },
    "helius": {"solana": "https://mainnet.helius-rpc.com/?api-key={key}"},
    "ankr": {
        "ethereum": "https://rpc.ankr.com/eth/{key}",
        "polygon": "https://rpc.ankr.com/polygon/{key}",
        "base": "https://rpc.ankr.com/base/{key}",
        "arbitrum": "https://rpc.ankr.com/arbitrum/{key}",
        "optimism": "https://rpc.ankr.com/optimism/{key}",
        "avalanche": "https://rpc.ankr.com/avalanche/{key}",
        "bsc": "https://rpc.ankr.com/bsc/{key}",
    },
}
NETWORK_ALIASES = {"bsc": "bsc", "bnb": "bsc", "avax": "avalanche", "eth": "ethereum",
                   "sol": "solana", "arb": "arbitrum", "op": "optimism", "matic": "polygon"}
PAYMENT_NETWORK = {"BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana", "BNB": "bsc"}


class RpcError(Exception):
    def __init__(self, code: str, message: str, status: int = 502, detail=None):
        super().__init__(message)
        self.code, self.message, self.status, self.detail = code, message, status, detail


@dataclass
class Network:
    name: str
    chain_id: int
    kind: str                    # evm | solana
    explorer: str
    symbol: str
    decimals: int
    tokens: list = field(default_factory=list)

    @property
    def public(self) -> dict:
        return {"name": self.name, "chain_id": self.chain_id, "kind": self.kind,
                "symbol": self.symbol, "decimals": self.decimals}


def _repo_metadata() -> tuple[dict, dict, bool]:
    """Read the real repo. Never raises; reports availability."""
    try:
        chains = importlib.import_module("seedhunter.chains")
        explorers = dict(getattr(chains, "EXPLORER", {}))
        natives = dict(getattr(chains, "NATIVE", {}))
        tokens = {k: list(v) for k, v in getattr(chains, "TOKENS", {}).items()}
        return explorers, {"natives": natives, "tokens": tokens}, True
    except Exception:                                      # pragma: no cover
        return {}, {"natives": {}, "tokens": {}}, False


def _repo_endpoints() -> dict[str, list[str]]:
    try:
        import yaml
        raw = yaml.safe_load((ROOT / "config.yaml").read_text()) or {}
    except Exception:                                      # pragma: no cover
        return {}
    out: dict[str, list[str]] = {}
    for name, cfg in (raw.get("chains") or {}).items():
        if isinstance(cfg, dict) and cfg.get("enabled"):
            out[name] = [e for e in (cfg.get("endpoints") or []) if isinstance(e, str)]
    return out


def load_networks(cfg) -> dict[str, Network]:
    explorers, meta, _ = _repo_metadata()
    natives = meta["natives"]
    tokens = meta["tokens"]
    names = cfg.networks_enabled or list(_repo_endpoints().keys())
    if not names:
        names = ["ethereum", "bsc", "polygon", "base", "arbitrum", "optimism",
                 "avalanche", "solana"]
    ids = {"ethereum": 1, "bsc": 56, "polygon": 137, "base": 8453, "arbitrum": 42161,
           "optimism": 10, "avalanche": 43114, "solana": 0}
    networks: dict[str, Network] = {}
    for name in names:
        if name not in ids:
            continue
        symbol, decimals = natives.get(name, ("SOL" if name == "solana" else "ETH", 9 if name == "solana" else 18))
        networks[name] = Network(name=name, chain_id=ids[name],
                                 kind="solana" if name == "solana" else "evm",
                                 explorer=explorers.get(name, ""), symbol=symbol,
                                 decimals=decimals, tokens=tokens.get(name, []))
    return networks


def redact(url: str) -> str:
    """Strip credentials from an upstream URL. Safe for logs and responses."""
    if not url:
        return ""
    parts = urlparse(url)
    host = parts.hostname or ""
    path = parts.path.rstrip("/")
    tail = path.rsplit("/", 1)[-1] if path else ""
    keep = tail if tail and not _looks_secret(tail) else ""
    suffix = f"/{keep}" if keep else ""
    return f"{parts.scheme}://{host}{suffix}"


def _looks_secret(segment: str) -> bool:
    if len(segment) < 16:
        return False
    return any(c.isdigit() for c in segment) or segment.lower().startswith("v2")


def provider_of(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    for name in ("alchemy", "helius", "ankr", "llamarpc", "publicnode", "binance",
                 "base.org", "arbitrum.io", "optimism.io", "avax.network", "solana.com"):
        if name in host:
            return name
    return "custom"


@dataclass
class Endpoint:
    url: str
    provider: str
    fails: int = 0
    latency: float = 999.0
    down_until: float = 0.0

    def ok(self) -> bool:
        return self.down_until < time.time()

    def label(self) -> str:
        return f"{self.provider}:{redact(self.url).rsplit('/', 1)[-1]}"


class RpcEngine:
    def __init__(self, cfg, transport: httpx.AsyncBaseTransport | None = None):
        self.cfg = cfg
        self.networks = load_networks(cfg)
        self.client = httpx.AsyncClient(timeout=cfg.rpc_timeout, transport=transport,
                                        headers={"content-type": "application/json",
                                                 "user-agent": "seedhunter-rpc/0.1"})
        self.pools: dict[str, list[Endpoint]] = {}
        repo = _repo_endpoints()
        for name in self.networks:
            urls: list[str] = []
            urls += cfg.rpc_endpoints.get(name, [])
            for provider, mapping in PROVIDER_TEMPLATES.items():
                key = cfg.provider_keys.get(provider, "")
                if key and name in mapping:
                    urls.append(mapping[name].format(key=key))
            urls += repo.get(name, [])
            seen, ordered = set(), []
            for u in urls:
                if u not in seen:
                    seen.add(u)
                    ordered.append(u)
            self.pools[name] = [Endpoint(url=u, provider=provider_of(u)) for u in ordered]

    async def close(self) -> None:
        await self.client.aclose()

    # ------------------------------------------------------------ routing
    def resolve(self, name: str) -> Network:
        key = NETWORK_ALIASES.get((name or "").lower(), (name or "").lower())
        if key not in self.networks:
            raise RpcError("network_unsupported", f"network '{name}' not supported", 404,
                           {"supported": sorted(self.networks)})
        return self.networks[key]

    def _pick(self, name: str) -> Endpoint:
        pool = [e for e in self.pools.get(name, []) if e.ok()]
        if not pool:
            pool = self.pools.get(name, [])
            for e in pool:
                e.down_until = 0.0
        if not pool:
            raise RpcError("no_endpoint", f"no endpoint configured for {name}", 503)
        pool.sort(key=lambda e: (e.fails, e.latency))
        return pool[0]

    def _validate(self, net: Network, payload) -> list[dict]:
        if isinstance(payload, dict):
            payload = [payload]
        if not isinstance(payload, list) or not payload:
            raise RpcError("bad_payload", "body must be a JSON-RPC object or array", 400)
        if len(payload) > self.cfg.rpc_max_batch:
            raise RpcError("batch_too_large", f"max {self.cfg.rpc_max_batch} calls", 413)
        allowed = SOL_METHODS | SOL_WRITE_METHODS if net.kind == "solana" \
            else EVM_METHODS | EVM_WRITE_METHODS
        for call in payload:
            if not isinstance(call, dict):
                raise RpcError("bad_payload", "batch entries must be objects", 400)
            if call.get("jsonrpc") != "2.0":
                raise RpcError("bad_payload", "jsonrpc must be '2.0'", 400)
            method = call.get("method")
            if method not in allowed:
                raise RpcError("method_unsupported", f"method '{method}' not allowed", 400,
                               {"allowed": sorted(allowed)})
            if not isinstance(call.get("params", []), (list, dict)):
                raise RpcError("bad_params", "params must be array or object", 400)
            call.setdefault("params", [])
            call.setdefault("id", 0)
        return payload

    async def forward(self, name: str, payload) -> list[dict]:
        net = self.resolve(name)
        calls = self._validate(net, payload)
        last: Exception | None = None
        for attempt in range(2):
            endpoint = self._pick(net.name)
            started = time.perf_counter()
            try:
                response = await self.client.post(endpoint.url, content=json.dumps(calls))
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                endpoint.fails += 1
                endpoint.down_until = time.time() + 15
                last = exc
                continue
            if response.status_code == 429 or response.status_code >= 500:
                endpoint.fails += 1
                endpoint.down_until = time.time() + 15
                last = RpcError("upstream_busy", f"upstream {response.status_code}", 502)
                continue
            try:
                body = response.json()
            except ValueError:
                raise RpcError("upstream_malformed", "upstream returned non-JSON", 502) from None
            endpoint.fails = 0
            endpoint.latency = time.perf_counter() - started
            return body if isinstance(body, list) else [body]
        raise RpcError("upstream_unavailable", "all endpoints failed",
                       503, {"reason": type(last).__name__ if last else "unknown"})

    async def probe(self, name: str) -> dict:
        net = self.resolve(name)
        endpoint = self._pick(net.name)
        method = "getHealth" if net.kind == "solana" else "eth_chainId"
        started = time.perf_counter()
        try:
            response = await self.client.post(endpoint.url, json={
                "jsonrpc": "2.0", "id": 1, "method": method, "params": []})
            ms = int((time.perf_counter() - started) * 1000)
            healthy = response.status_code == 200 and "result" in response.json()
            endpoint.fails = 0 if healthy else endpoint.fails + 1
            endpoint.latency = ms / 1000
            return {"network": net.name, "ok": healthy, "latency_ms": ms,
                    "endpoint": endpoint.label(), "checked_at": int(time.time())}
        except Exception as exc:
            return {"network": net.name, "ok": False, "latency_ms": None,
                    "endpoint": endpoint.label(), "error": type(exc).__name__,
                    "checked_at": int(time.time())}

    async def probe_all(self) -> list[dict]:
        return list(await asyncio.gather(*(self.probe(n) for n in self.networks)))

    def catalogue(self, health: dict[str, dict] | None = None) -> list[dict]:
        health = health or {}
        out = []
        for net in self.networks.values():
            entry = net.public
            state = health.get(net.name, {})
            entry["status"] = "up" if state.get("ok") else ("down" if state else "unknown")
            entry["providers"] = sorted({e.provider for e in self.pools.get(net.name, [])})
            out.append(entry)
        return out