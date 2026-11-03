from __future__ import annotations

import logging
import threading
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from config import (
    BOT_TIMEZONE,
    DAILY_REMINDER_TIME,
    DAY_BEFORE_REMINDER_TIME,
    MONTHLY_CARD_TIME,
    POSTGAME_PROMPT_TIME,
    REACTIVATION_TIME,
)
from game_registration import (
    get_latest_poll_for_game,
    list_game_registrants,
    list_games_on_date,
    mark_reminder_sent,
    reminder_was_sent,
)
from helpers import escape_markdown, format_game_datetime
from ui import build_group_game_keyboard, build_share_keyboard
from product_features import (
    build_monthly_stats,
    format_monthly_card,
    get_latest_group_chat_id,
    get_setting,
    list_reactivation_candidates,
    mark_monthly_card_sent,
    mark_reactivation_sent,
    monthly_card_was_sent,
    postgame_prompt_exists,
    previous_month_key,
    reconcile_poll_registrations,
    record_postgame_prompt,
    set_setting,
)


MOSCOW_TIMEZONE = ZoneInfo(BOT_TIMEZONE)
DAY_OF_REMINDER = "day_of"
DAY_BEFORE_AVAILABILITY_REMINDER = "day_before_availability"


def start_daily_reminder(bot, db_path: str) -> None:
    def run_loop() -> None:
        while True:
            try:
                run_due_reminders(bot, db_path)
            except Exception:
                logging.exception("Reminder scheduler iteration failed")
            time.sleep(20)

    thread = threading.Thread(target=run_loop, daemon=True)
    thread.start()


def run_due_reminders(bot, db_path: str, now: datetime | None = None) -> None:
    current = now or datetime.now(MOSCOW_TIMEZONE)
    if current.tzinfo is None:
        current = current.replace(tzinfo=MOSCOW_TIMEZONE)
    else:
        current = current.astimezone(MOSCOW_TIMEZONE)

    current_time = current.strftime("%H:%M")
    today = current.date()
    if current_time >= "10:00" and get_setting(
        db_path, "last_poll_reconciliation_date"
    ) != today.isoformat():
        reconciliation = reconcile_poll_registrations(
            db_path, apply_changes=True, current_date=today
        )
        set_setting(db_path, "last_poll_reconciliation_date", today.isoformat(), None)
        if any(
            value
            for key, value in reconciliation.items()
            if key != "orphan_answers"
        ):
            logging.warning("Scheduled poll/database reconciliation: %s", reconciliation)
    if current_time >= DAILY_REMINDER_TIME:
        send_today_reminders(bot, db_path, today)
    if current_time >= DAY_BEFORE_REMINDER_TIME:
        send_day_before_availability_reminders(
            bot,
            db_path,
            today + timedelta(days=1),
        )
    if current_time >= POSTGAME_PROMPT_TIME:
        send_postgame_prompts(bot, db_path, today)
    if current_time >= REACTIVATION_TIME:
        send_reactivation_messages(bot, db_path, today)
    if current.day == 1 and current_time >= MONTHLY_CARD_TIME:
        send_monthly_card(bot, db_path, today)


def send_today_reminders(bot, db_path: str, target_date: date | None = None) -> None:
    game_date = target_date or datetime.now(MOSCOW_TIMEZONE).date()
    date_key = game_date.isoformat()
    for game in list_games_on_date(db_path, date_key):
        if reminder_was_sent(db_path, game["id"], DAY_OF_REMINDER, date_key):
            continue
        poll = get_latest_poll_for_game(db_path, game["id"])
        if poll is None:
            continue
        chat_id = poll["chat_id"]
        participants = list_game_registrants(db_path, game["id"], include_cancelled=False)
        names = []
        for person in participants:
            username = person.get("username")
            full_name = person.get("full_name") or ""
            if username:
                names.append(f"@{username.lstrip('@')}")
            elif full_name:
                names.append(full_name)
            else:
                names.append(f"id:{person.get('user_id')}")

        title = game.get("title") or "Игра"
        date_text = format_game_datetime(game.get("game_date"))
        time_text = date_text.split()[-1] if date_text else "—"
        place_text = _format_place(game)
        if names:
            participants_text = ", ".join(escape_markdown(name) for name in names)
        else:
            participants_text = "пока никто не отметился"
        message = (
            f"Сегодня игра: {escape_markdown(title)}\n"
            f"Время: {escape_markdown(time_text)}\n"
            f"Место: {place_text}\n"
            f"Желать удачи этим героям: {participants_text}"
        )
        try:
            bot.send_message(
                chat_id,
                message,
                parse_mode="MarkdownV2",
                disable_web_page_preview=True,
                reply_to_message_id=poll["message_id"],
                allow_sending_without_reply=True,
                reply_markup=build_group_game_keyboard(
                    game,
                    chat_id,
                    poll["message_id"],
                ),
            )
            mark_reminder_sent(db_path, game["id"], DAY_OF_REMINDER, date_key)
        except Exception:
            logging.exception("Failed to send day-of reminder for game %s", game.get("id"))


def send_day_before_availability_reminders(
    bot,
    db_path: str,
    target_date: date | None = None,
) -> None:
    game_date = target_date or (datetime.now(MOSCOW_TIMEZONE).date() + timedelta(days=1))
    date_key = game_date.isoformat()
    for game in list_games_on_date(db_path, date_key):
        free_spots = max(int(game["capacity"]) - int(game["registrations"]), 0)
        if free_spots <= 0:
            continue
        if reminder_was_sent(
            db_path,
            game["id"],
            DAY_BEFORE_AVAILABILITY_REMINDER,
            date_key,
        ):
            continue
        poll = get_latest_poll_for_game(db_path, game["id"])
        if poll is None:
            continue
        chat_id = poll["chat_id"]
        title = escape_markdown(game.get("title") or "Игра")
        date_text = format_game_datetime(game.get("game_date"))
        time_text = date_text.split()[-1] if date_text else "—"
        message = (
            f"Завтра игра: {title}\n"
            f"Время: {escape_markdown(time_text)}\n"
            f"Свободных мест: *{free_spots}*\n"
            "Отмечайтесь в опросе и приходите играть\\!"
        )
        try:
            bot.send_message(
                chat_id,
                message,
                parse_mode="MarkdownV2",
                disable_web_page_preview=True,
                reply_to_message_id=poll["message_id"],
                allow_sending_without_reply=True,
                reply_markup=build_group_game_keyboard(
                    game, chat_id, poll["message_id"]
                ),
            )
            mark_reminder_sent(
                db_path,
                game["id"],
                DAY_BEFORE_AVAILABILITY_REMINDER,
                date_key,
            )
        except Exception:
            logging.exception(
                "Failed to send day-before availability reminder for game %s",
                game.get("id"),
            )


def _format_place(game: dict) -> str:
    restaurant_name_raw = game.get("restaurant_name") or ""
    restaurant_address_raw = game.get("restaurant_address") or ""
    restaurant_name = escape_markdown(restaurant_name_raw) if restaurant_name_raw else ""
    restaurant_address = (
        escape_markdown(restaurant_address_raw) if restaurant_address_raw else ""
    )
    lat = game.get("restaurant_lat")
    lon = game.get("restaurant_lon")
    if restaurant_name and restaurant_address:
        if lat is not None and lon is not None:
            map_url = f"https://yandex.ru/maps/?pt={lon},{lat}&z=18&l=map"
            return f"{restaurant_name}, [{restaurant_address}]({map_url})"
        return f"{restaurant_name}, {restaurant_address}"
    if restaurant_name:
        return restaurant_name
    if restaurant_address:
        return restaurant_address
    return "—"


def send_postgame_prompts(bot, db_path: str, target_date: date) -> None:
    for game in list_games_on_date(db_path, target_date.isoformat()):
        if postgame_prompt_exists(db_path, game["id"]):
            continue
        poll = get_latest_poll_for_game(db_path, game["id"])
        if poll is None:
            continue
        chat_id = poll["chat_id"]
        title = game.get("title") or "Игра"
        message = (
            f"Как сыграли «{title}»? Ответьте реплаем на это сообщение по форме:\n\n"
            "Место: 1\n"
            "Из скольки команд: 30\n"
            "Лотерея: ничего\n"
            "Подлутки: ничего\n\n"
            "Если были призы, вместо «ничего» перечислите их и победителей; "
            "несколько записей разделяйте точкой с запятой. "
            "Результат может внести любой участник беседы."
        )
        try:
            sent = bot.send_message(chat_id, message)
            record_postgame_prompt(db_path, game["id"], chat_id, sent.message_id)
        except Exception:
            logging.exception("Failed to send postgame prompt for game %s", game.get("id"))


def send_monthly_card(bot, db_path: str, current_date: date) -> None:
    chat_id = get_latest_group_chat_id(db_path)
    if chat_id is None:
        return
    month_key = previous_month_key(current_date)
    if monthly_card_was_sent(db_path, chat_id, month_key):
        return
    try:
        text = format_monthly_card(build_monthly_stats(db_path, month_key))
        try:
            bot_username = (bot.get_me().username or "QPHelperBot").lstrip("@")
        except Exception:
            bot_username = "QPHelperBot"
        bot.send_message(
            chat_id,
            text,
            reply_markup=build_share_keyboard(
                text,
                f"https://t.me/{bot_username}",
                "Поделиться итогами",
            ),
        )
        mark_monthly_card_sent(db_path, chat_id, month_key)
    except Exception:
        logging.exception("Failed to send monthly card for %s", month_key)


def send_reactivation_messages(bot, db_path: str, current_date: date) -> None:
    if get_setting(db_path, "reactivation_enabled", "1") != "1":
        return
    group_chat_id = get_latest_group_chat_id(db_path)
    if group_chat_id is None:
        return
    month_key = current_date.strftime("%Y-%m")
    try:
        from telebot import types
    except ModuleNotFoundError:
        return
    for candidate in list_reactivation_candidates(db_path, current_date):
        try:
            member = bot.get_chat_member(group_chat_id, candidate["user_id"])
            if getattr(member, "status", None) not in {
                "creator",
                "administrator",
                "member",
                "restricted",
            }:
                continue
            keyboard = types.InlineKeyboardMarkup(row_width=1)
            keyboard.add(
                types.InlineKeyboardButton("Посмотреть игры", callback_data="reactivation:games"),
                types.InlineKeyboardButton("Не сейчас", callback_data="reactivation:later"),
                types.InlineKeyboardButton(
                    "Больше не напоминать", callback_data="reactivation:disable"
                ),
            )
            bot.send_message(
                candidate["private_chat_id"],
                "Давно не играли вместе 🙂 В беседе уже есть новые опросы — "
                "загляни, вдруг найдётся игра по настроению.",
                reply_markup=keyboard,
            )
            mark_reactivation_sent(db_path, candidate["user_id"], month_key)
        except Exception:
            logging.exception("Failed to reactivate user %s", candidate.get("user_id"))
