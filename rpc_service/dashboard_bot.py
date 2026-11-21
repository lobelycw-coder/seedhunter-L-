"""Second Telegram bot: dashboard entry only.

Reads the live tunnel URL from data/webapp_url.txt at command time, so the
button always follows the current tunnel instead of a stale environment value.
"""
from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.ext import Application, CommandHandler, ContextTypes

from . import auth, db, subscriptions, tunnel
from .settings import Settings

log = logging.getLogger("dashbot")


def webapp_url(cfg: Settings) -> str:
    return tunnel.current_url() or cfg.tg_webapp_url


def build(cfg: Settings) -> Application:
    if not cfg.tg_bot_token:
        raise SystemExit("SH_TG_DASH_BOT_TOKEN missing")

    async def start(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        url = webapp_url(cfg)
        if not url.startswith("https://"):
            await update.effective_message.reply_text(
                "Dashboard URL not published yet — the tunnel is still coming up. "
                "Try again in a minute.")
            return
        button = InlineKeyboardButton("Open dashboard", web_app=WebAppInfo(url=url))
        await update.effective_message.reply_text(
            "SeedHunter account dashboard.\nBalances, plan and withdrawals stay server-side.",
            reply_markup=InlineKeyboardMarkup([[button]]))

    async def status(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        tg_id = str(update.effective_user.id)
        account = auth.get_account_by_tg(cfg.db_path, tg_id)
        if not account:
            await update.effective_message.reply_text(
                "No account yet. Open the dashboard once to create one.")
            return
        tier = subscriptions.tier_of(account)
        await update.effective_message.reply_text(
            f"project {account['project_id']}\nplan {tier}\n"
            f"premium until {account['premium_until'] or '—'}")

    async def url_cmd(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await update.effective_message.reply_text(webapp_url(cfg) or "not published")

    application = Application.builder().token(cfg.tg_bot_token).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("dashboard", start))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("url", url_cmd))
    return application


def main() -> None:
    from .settings import get_settings
    cfg = get_settings()
    db.init(cfg.db_path)
    build(cfg).run_polling()


if __name__ == "__main__":
    main()