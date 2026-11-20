"""Router validation, failover, redaction."""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

import httpx

from rpc_service import rpc_engine
from tests import make_cfg


def run(coro):
    return asyncio.run(coro)


class RouterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def _engine(self, handler, **over):
        cfg = make_cfg(Path(self.tmp.name), SH_RPC_ETHEREUM="https://a.example/v2/K", **over)
        return rpc_engine.RpcEngine(cfg, transport=httpx.MockTransport(handler))

    def test_unsupported_network(self):
        engine = self._engine(lambda r: httpx.Response(200, json={"result": "0x1"}))
        with self.assertRaises(rpc_engine.RpcError) as ctx:
            run(engine.forward("dogecoin", {"jsonrpc": "2.0", "method": "eth_chainId", "id": 1}))
        self.assertEqual(ctx.exception.code, "network_unsupported")

    def test_method_blocked(self):
        engine = self._engine(lambda r: httpx.Response(200, json={"result": "0x1"}))
        with self.assertRaises(rpc_engine.RpcError) as ctx:
            run(engine.forward("ethereum", {"jsonrpc": "2.0", "method": "eth_mining", "id": 1}))
        self.assertEqual(ctx.exception.code, "method_unsupported")

    def test_bad_version(self):
        engine = self._engine(lambda r: httpx.Response(200, json={"result": "0x1"}))
        with self.assertRaises(rpc_engine.RpcError) as ctx:
            run(engine.forward("ethereum", {"jsonrpc": "1.0", "method": "eth_chainId", "id": 1}))
        self.assertEqual(ctx.exception.code, "bad_payload")

    def test_batch_limit(self):
        engine = self._engine(lambda r: httpx.Response(200, json=[]), SH_RPC_MAX_BATCH=2)
        payload = [{"jsonrpc": "2.0", "method": "eth_chainId", "id": i} for i in range(3)]
        with self.assertRaises(rpc_engine.RpcError) as ctx:
            run(engine.forward("ethereum", payload))
        self.assertEqual(ctx.exception.code, "batch_too_large")

    def test_timeout_fails_over(self):
        cfg = make_cfg(Path(self.tmp.name), SH_RPC_ETHEREUM="https://down.example/v2/K",
                       SH_RPC_BASE="https://up.example/v2/K")

        def handler(request):
            if "down.example" in str(request.url):
                raise httpx.ConnectTimeout("boom")
            return httpx.Response(200, json={"result": "0x1"})

        engine