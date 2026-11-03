import logging
import os
import time

from dotenv import load_dotenv
import telebot
from telebot import types

from game_registration import seed_admins
from handlers import register_handlers
from reminders import start_daily_reminder
from setup_db import ensure_db
from product_features import backfill_private_profiles, reconcile_poll_registrations


def configure_bot_commands(bot: telebot.TeleBot) -> None:
    try:
        bot.set_my_commands(
            [types.BotCommand("help", "как пользоваться ботом")],
            scope=types.BotCommandScopeDefault(),
        )
        bot.set_my_commands(
            [
                types.BotCommand("start", "главное меню"),
                types.BotCommand("schedule", "ближайшие игры"),
                types.BotCommand("mygames", "игры, на которые я записан"),
                types.BotCommand("mystats", "моя статистика"),
                types.BotCommand("help", "как пользоваться ботом"),
            ],
            scope=types.BotCommandScopeAllPrivateChats(),
        )
        bot.set_my_commands(
            [
                types.BotCommand("schedule", "ближайшие игры"),
                types.BotCommand("dmme", "прислать меню в личку"),
                types.BotCommand("help", "как пользоваться ботом"),
            ],
            scope=types.BotCommandScopeAllGroupChats(),
        )
    except Exception:
        logging.exception("Failed to configure Telegram command scopes")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    load_dotenv()
    if os.getenv("BOT_DISABLED", "").strip().lower() in {"1", "true", "yes"}:
        logging.warning("Bot is disabled by BOT_DISABLED; staying idle.")
        while True:
            time.sleep(3600)

    token = os.getenv("BOT_TOKEN")
    db_path = os.getenv("DB_PATH", "quiz_bot.sqlite3")
    admin_usernames_raw = os.getenv("ADMIN_USERNAMES", "podlol")
    admin_usernames = {
        item.strip().lstrip("@").lower()
        for item in admin_usernames_raw.split(",")
        if item.strip()
    }

    if not token:
        raise SystemExit("BOT_TOKEN is not set. Put it into .env or environment variables.")

    bot = telebot.TeleBot(token)
    configure_bot_commands(bot)
    share_app_url = os.getenv("SHARE_APP_URL", "").strip()
    if share_app_url:
        try:
            bot.set_chat_menu_button(
                menu_button=types.MenuButtonWebApp(
                    "web_app",
                    "📱 Квизы",
                    types.WebAppInfo(f"{share_app_url.rstrip('/')}/app"),
                )
            )
        except Exception:
            logging.exception("Failed to configure Mini App menu button")
    if ensure_db(db_path):
        logging.info("Database created at %s", db_path)
    else:
        logging.info("Database found at %s", db_path)
    backfill_private_profiles(db_path)
    reconciliation = reconcile_poll_registrations(db_path, apply_changes=True)
    actionable_reconciliation = {
        key: value
        for key, value in reconciliation.items()
        if key != "orphan_answers" and value
    }
    if actionable_reconciliation:
        logging.warning("Poll/database reconciliation applied: %s", reconciliation)
    elif reconciliation.get("orphan_answers"):
        logging.info(
            "Historical orphan poll answers preserved: %s",
            reconciliation["orphan_answers"],
        )
    seed_admins(db_path, admin_usernames)

    register_handlers(bot, db_path)
    start_daily_reminder(bot, db_path)

    logging.info("Bot started (polling).")
    bot.infinity_polling(skip_pending=False, timeout=20)


if __name__ == "__main__":
    main()
