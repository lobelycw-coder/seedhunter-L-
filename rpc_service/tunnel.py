"""Ephemeral public HTTPS for the Mini App via cloudflared quick tunnel."""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import sys
import time

import httpx

URL_RE = re.compile(r"https://[a-z0-9][a-z0-9-]*\.trycloudflare\.com(?=[\s|'\"]|$)")
BIN = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64"


def extract_url(line: str) -> str | None:
    match = URL_RE.search(line)
    return match.group(0) if match else None


async def wait_local(port: int, timeout: float = 90.0) -> bool:
    deadline = time.time() + timeout
    async with httpx.AsyncClient(timeout=2) as client:
        while time.time() < deadline:
            try:
                if (await client.get(f"http://127.0.0.1:{port}/healthz")).status_code == 200:
                    return True
            except httpx.HTTPError:
                pass
            await asyncio.sleep(1.5)
    return False


def ensure_binary() -> str:
    found = shutil.which("cloudflared")
    if found:
        return found
    target = os.path.join(os.getcwd(), "cloudflared")
    if not os.path.exists(target):
        subprocess.run(["curl", "-fsSL", "-o", target, BIN], check=True)
        os.chmod(target, 0o755)
    return target


async def open_tunnel(port: int, timeout: float = 45.0) -> tuple[subprocess.Popen, str]:
    binary = ensure_binary()
    proc = subprocess.Popen([binary, "tunnel", "--no-autoupdate", "--url",
                             f"http://127.0.0.1:{port}"],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("cloudflared exited before publishing a URL")
        line = proc.stdout.readline() if proc.stdout else ""
        url = extract_url(line)
        if url:
            return proc, url
        await asyncio.sleep(0.2)
    proc.terminate()
    raise RuntimeError("no tunnel URL within timeout")


async def point_menu_button(token: str, url: str, chat_id: str | None) -> bool:
    if not token or not url:
        return False
    api = f"https://api.telegram.org/bot{token}/setChatMenuButton"
    payload = {"menu_button": {"type": "web_app", "text": "Dashboard",
                               "web_app": {"url": url}}}
    if chat_id:
        payload["chat_id"] = chat_id
    async with httpx.AsyncClient(timeout=10) as client:
        for _ in range(5):
            try:
                response = await client.post(api, json=payload)
                if response.status_code == 200 and response.json().get("ok"):
                    return True
            except httpx.HTTPError:
                pass
            await asyncio.sleep(2)
    return False


async def serve(port: int, token: str, chat_id: str | None) -> None:
    api = subprocess.Popen([sys.executable, "-m", "uvicorn", "rpc_service.app:app",
                            "--host", "127.0.0.1", "--port", str(port)])
    tunnel = None
    try:
        if not await wait_local(port):
            raise SystemExit("API did not become healthy")
        tunnel, url = await open_tunnel(port)
        print(f"public URL: {url}", flush=True)
        if await point_menu_button(token, url, chat_id):
            print("menu button pointed at the tunnel", flush=True)
        else:
            print("could not set menu button — open the URL manually", flush=True)
        while True:
            await asyncio.sleep(5)
    finally:
        for proc in (tunnel, api):
            if proc and proc.poll() is None:
                proc.terminate()