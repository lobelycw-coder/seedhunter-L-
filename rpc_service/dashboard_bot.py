"""Second Telegram bot: dashboard entry only.

Deliberately does not import seedhunter.bot, so the existing command set and the
existing polling process stay untouched. Run it as its own process with its own
token (SH_TG_DASH_BOT_TOKEN).

Telegram requires an HTTPS URL for Web App buttons, so SH_PUBLIC_BASE_URL (or
SH_TG_WEBAPP_URL) must be a public https:// origin before this is usable.
"""
from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.ext import Application, CommandHandler, ContextTypes

from . import auth, db, subscriptions
from .settings import Settings

log = logging.getLogger("dashbot")


def build(cfg: Settings) -> Application:
    if not cfg.tg_bot_token:
        raise SystemExit("SH_TG_DASH_BOT_TOKEN missing")
    if not cfg.tg_webapp_url.startswith("https://"):
        log.warning("SH_TG_WEBAPP_URL is not https; Telegram will reject the Web App button")

    async def start(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        button = InlineKeyboardButton("Open dashboard",
                                      web_app=WebAppInfo(url=cfg.tg_webapp_url or "https://localhost"))
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

    application = Application.builder().token(cfg.tg_bot_token).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("dashboard", start))
    application.add_handler(CommandHandler("status", status))
    return application


def main() -> None:
    from .settings import get_settings
    cfg = get_settings()
    db.init(cfg.db_path)
    build(cfg).run_polling()


if __name__ == "__main__":
    main()