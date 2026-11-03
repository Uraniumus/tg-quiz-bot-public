from __future__ import annotations

import io
import logging
import os
from datetime import datetime
from html import escape as escape_html
from urllib.parse import quote
from zoneinfo import ZoneInfo

from telebot import types
from telebot.apihelper import ApiTelegramException

from config import (
    AMRA_TEAM_NAME,
    TEAM_NAME,
    BOT_TIMEZONE,
    POLL_MAX_PEOPLE,
    POLL_YES_THRESHOLD,
    QUIZPLEASE_PROMO_END_DATE,
    QUIZPLEASE_PROMO_ID,
    TEAM_REGISTRATION_COMMENT,
)
from api_client import build_registration_payload, fetch_game_from_api, register_team_in_api
from helpers import (
    build_game_payload_from_api,
    build_poll_full_message,
    build_poll_question_from_payload,
    build_poll_registered_message,
    build_poll_threshold_message,
    build_upcoming_schedule_text,
    escape_markdown,
    extract_external_id_from_link,
    extract_game_id,
    format_game_datetime,
    get_full_name,
    get_participation_status,
    is_skip_value,
    normalize_game_datetime,
    parse_poll_labels,
)
from ui import (
    build_admin_game_block,
    build_admin_cancel_list_message,
    build_calendar_file,
    build_calendar_url,
    build_group_game_keyboard,
    build_invite_text,
    build_location_keyboard,
    build_main_menu,
    build_manage_menu,
    build_share_keyboard,
    build_game_block,
)
from game_registration import (
    add_game,
    deny_admin_request,
    approve_admin_request,
    is_admin,
    list_games,
    list_admin_requests,
    list_upcoming_games_admin,
    get_game_admin,
    list_upcoming_games_with_counts,
    get_upcoming_game_with_counts,
    list_user_games,
    log_message,
    register_player,
    request_admin,
    add_waitlist,
    unregister_player,
    archive_game,
    restore_game,
    list_game_registrants,
    get_user_last_chat_id,
    get_game_basic,
    get_waitlist_first,
    remove_waitlist_entry,
    record_poll,
    record_poll_answer,
    get_poll_by_id,
    get_poll_yes_count,
    mark_poll_notified,
    mark_poll_full_notified,
    clear_poll_full_notified,
    delete_poll_answer,
    get_game_id_by_external_id,
    get_poll_by_external_id,
    get_default_organizer,
    get_organizer_for_external_id,
    get_organizer_by_link,
    get_game_registration_info_by_external_id,
    get_organizer_by_id,
    get_poll_max_people_for_game_id,
    count_upcoming_games,
    list_upcoming_games_with_counts_limit,
)
from product_features import (
    build_monthly_stats,
    build_personal_stats,
    format_monthly_card,
    get_prompt_game,
    get_setting,
    get_settings,
    list_admin_snapshots,
    list_recent_results,
    parse_result_reply,
    previous_month_key,
    reconcile_poll_registrations,
    record_registration_attempt,
    save_game_result,
    set_reactivation_preference,
    set_setting,
    upsert_user_profile,
)


def register_handlers(bot, db_path: str) -> None:
    try:
        bot_username = (bot.get_me().username or "").lstrip("@")
    except Exception:
        logging.exception("Failed to resolve bot username")
        bot_username = ""
    share_app_url = os.getenv("SHARE_APP_URL", "").strip()
    def store_message(message) -> None:
        try:
            user = message.from_user
            chat = message.chat
            log_message(
                db_path=db_path,
                message_id=getattr(message, "message_id", None),
                chat_id=getattr(chat, "id", None) if chat else None,
                user_id=getattr(user, "id", None) if user else None,
                username=getattr(user, "username", None) if user else None,
                full_name=get_full_name(user),
                content_type=getattr(message, "content_type", None),
                text=getattr(message, "text", None),
            )
            if user:
                private_chat_id = (
                    getattr(chat, "id", None)
                    if chat and getattr(chat, "type", None) == "private"
                    else None
                )
                upsert_user_profile(
                    db_path,
                    user.id,
                    getattr(user, "username", None),
                    get_full_name(user),
                    private_chat_id,
                )
        except Exception:
            logging.exception("Failed to log incoming message")

    def store_callback(call) -> None:
        try:
            user = call.from_user
            chat = call.message.chat if call.message else None
            log_message(
                db_path=db_path,
                message_id=getattr(call.message, "message_id", None) if call.message else None,
                chat_id=getattr(chat, "id", None) if chat else None,
                user_id=getattr(user, "id", None) if user else None,
                username=getattr(user, "username", None) if user else None,
                full_name=get_full_name(user),
                content_type="callback",
                text=getattr(call, "data", None),
            )
        except Exception:
            logging.exception("Failed to log callback")

    menu_labels = {
        "Про игры",
        "Отписаться от игры",
        "Хочу стать админом",
        "Управление",
        "Главное меню",
    }
    pending_actions: dict[int, str] = {}
    pending_addgame: dict[int, dict] = {}
    pending_prompt_messages: dict[int, tuple[int, int]] = {}
    schedule_message_refs: dict[int, tuple[int, int, int]] = {}
    help_message_refs: dict[int, tuple[int, int, int]] = {}
    poll_message_refs: dict[int, tuple[int, int, int]] = {}
    list_message_refs: dict[int, tuple[int, int]] = {}

    def clear_pending_for_user(user_id: int) -> None:
        pending_actions.pop(user_id, None)
        pending_addgame.pop(user_id, None)
        pending_prompt_messages.pop(user_id, None)
        help_message_refs.pop(user_id, None)

    def clear_pending(message) -> None:
        if message.from_user:
            clear_pending_for_user(message.from_user.id)

    def _format_reconciliation(result: dict[str, int]) -> str:
        return (
            "Сверка завершена.\n"
            f"Восстановлено записей: {result['restored_registrations']}\n"
            f"Восстановлено в резерве: {result.get('restored_waitlist', 0)}\n"
            f"Отменено устаревших записей: {result['cancelled_stale_registrations']}\n"
            f"Удалено устаревших записей из резерва: {result.get('removed_stale_waitlist', 0)}\n"
            f"Архивных ответов без опроса (не изменены): {result['orphan_answers']}"
        )

    def _format_admin_status() -> str:
        games = list_admin_snapshots(db_path)
        if not games:
            return "Предстоящих игр нет."
        lines = ["Состояние предстоящих игр:"]
        for game in games:
            lines.append(
                f"\n#{game['id']} {game['title']} — "
                f"{game['registrations']}/{game['capacity']}, "
                f"резерв {game['waitlist']}, в опросе «Да» {game['poll_yes']}"
            )
            attempt = game.get("last_attempt")
            if attempt:
                icon = "успешно" if attempt["success"] else "ошибка"
                lines.append(
                    f"Регистрация на сайте: {icon}, {attempt['created_at']} — "
                    f"{attempt['response_text']}"
                )
            else:
                lines.append("Регистрация на сайте: попыток пока нет")
        return "\n".join(lines)

    def _build_site_registration_payload(
        organizer: dict, registration_id: str | None, restaurant_name: str | None
    ) -> dict | None:
        runtime_settings = get_settings(
            db_path,
            (
                "registration_comment",
                "default_team_name",
                "amra_team_name",
                "quizplease_promo_id",
                "quizplease_promo_end_date",
            ),
        )
        return build_registration_payload(
            organizer.get("registration_payload"),
            registration_id,
            restaurant_name,
            runtime_settings=runtime_settings,
        )

    def _register_game_on_site(game_id: int, requested_by: int) -> tuple[bool, str]:
        game_info = get_game_basic(db_path, game_id)
        if not game_info or not game_info.get("external_id"):
            return False, "Игра или её внешний id не найдены."
        reg_info = get_game_registration_info_by_external_id(
            db_path, game_info["external_id"]
        )
        organizer = (
            get_organizer_by_id(db_path, reg_info["organizer_id"])
            if reg_info and reg_info.get("organizer_id")
            else None
        ) or get_organizer_for_external_id(
            db_path, game_info["external_id"]
        ) or get_default_organizer(db_path)
        if not organizer:
            return False, "Организатор не настроен."
        payload = _build_site_registration_payload(
            organizer,
            reg_info.get("registration_external_id") if reg_info else None,
            reg_info.get("restaurant_name") if reg_info else None,
        )
        ok, text = register_team_in_api(
            game_info["external_id"],
            organizer.get("registration_api_template") or "",
            payload,
        )
        record_registration_attempt(db_path, game_id, requested_by, ok, text)
        return ok, text

    def _build_registration_status_keyboard() -> types.InlineKeyboardMarkup:
        keyboard = types.InlineKeyboardMarkup(row_width=1)
        for game in list_admin_snapshots(db_path):
            keyboard.add(
                types.InlineKeyboardButton(
                    f"Повторить регистрацию #{game['id']}",
                    callback_data=f"admin_retry_confirm:{game['id']}",
                )
            )
        keyboard.add(types.InlineKeyboardButton("Назад", callback_data="menu:manage"))
        return keyboard

    def _build_stats_keyboard(stats_text: str) -> types.InlineKeyboardMarkup:
        keyboard = types.InlineKeyboardMarkup(row_width=2)
        keyboard.add(
            types.InlineKeyboardButton("Этот месяц", callback_data="stats:current"),
            types.InlineKeyboardButton("Прошлый месяц", callback_data="stats:previous"),
            types.InlineKeyboardButton("Всё время", callback_data="stats:all"),
        )
        share_target = f"https://t.me/{bot_username}" if bot_username else "https://t.me"
        share_url = (
            f"tg://msg_url?url={quote(share_target)}&text={quote(stats_text)}"
        )
        keyboard.add(types.InlineKeyboardButton("Поделиться статистикой", url=share_url))
        return keyboard

    def set_pending(message, action: str, prompt: str, user_id: int | None = None):
        target_id = user_id
        if target_id is None and message.from_user:
            target_id = message.from_user.id
        if target_id is not None:
            pending_actions[target_id] = action
        return bot.send_message(message.chat.id, prompt)

    def safe_delete_message(chat_id: int, message_id: int | None) -> None:
        if message_id is None:
            return
        try:
            bot.delete_message(chat_id, message_id)
        except Exception as exc:
            error_text = str(exc).lower()
            if isinstance(exc, ApiTelegramException):
                result_json = getattr(exc, "result_json", None)
                if isinstance(result_json, dict):
                    error_text = str(result_json.get("description") or error_text).lower()
            handled_errors = (
                "message to delete not found",
                "message can't be deleted",
                "message cant be deleted",
                "message identifier is not specified",
                "chat not found",
                "not enough rights to delete message",
            )
            if any(item in error_text for item in handled_errors):
                logging.warning(
                    "Delete message skipped for chat_id=%s message_id=%s: %s",
                    chat_id,
                    message_id,
                    error_text,
                )
                return
            logging.exception("Failed to delete message %s in chat %s", message_id, chat_id)

    def forward_poll_to_user(
        external_id: str,
        target_chat_id: int,
        prompt_text: str | None = None,
        prompt_keyboard: types.InlineKeyboardMarkup | None = None,
        track_for_cleanup: bool = False,
    ) -> bool:
        poll_info = get_poll_by_external_id(db_path, external_id)
        if not poll_info:
            return False
        prompt_id: int | None = None
        try:
            if prompt_text:
                prompt = bot.send_message(
                    target_chat_id,
                    prompt_text,
                    reply_markup=prompt_keyboard,
                )
                prompt_id = prompt.message_id
            forwarded = bot.forward_message(
                target_chat_id,
                poll_info["chat_id"],
                poll_info["message_id"],
            )
            if getattr(forwarded.chat, "type", None) in {"group", "supergroup"}:
                game = get_game_basic(db_path, poll_info["game_id"])
                if game:
                    try:
                        bot.edit_message_reply_markup(
                            target_chat_id,
                            forwarded.message_id,
                            reply_markup=build_group_game_keyboard(
                                game,
                                target_chat_id,
                                forwarded.message_id,
                                bot_username or "QPHelperBot",
                            ),
                        )
                    except Exception:
                        logging.exception("Failed to attach safe links to forwarded poll")
            if track_for_cleanup:
                poll_message_refs[target_chat_id] = (
                    target_chat_id,
                    prompt_id,
                    forwarded.message_id,
                )
            return True
        except Exception:
            logging.exception("Failed to forward poll to user %s", target_chat_id)
            if prompt_id is not None:
                try:
                    safe_delete_message(target_chat_id, prompt_id)
                except Exception:
                    pass
            return False

    def store_prompt_message(user_id: int, prompt_message) -> None:
        if prompt_message is None:
            return
        pending_prompt_messages[user_id] = (prompt_message.chat.id, prompt_message.message_id)

    def clear_prompt_message(user_id: int) -> None:
        prompt = pending_prompt_messages.pop(user_id, None)
        if prompt:
            chat_id, message_id = prompt
            safe_delete_message(chat_id, message_id)

    addgame_steps = [
        "title",
        "game_datetime",
        "description",
        "restaurant_name",
        "restaurant_address",
        "restaurant_lat",
        "restaurant_lon",
        "game_number",
        "lottery_id",
    ]

    def prompt_for_addgame_step(chat_id: int, step: str) -> None:
        prompts = {
            "title": "Введите название игры или ссылку из API.",
            "game_datetime": "Введите дату и время (YYYY-MM-DD HH:MM) или только дату.",
            "description": "Введите описание или '-' чтобы пропустить.",
            "restaurant_name": "Введите название ресторана или '-' чтобы пропустить.",
            "restaurant_address": "Введите адрес или '-' чтобы пропустить.",
            "restaurant_lat": "Введите широту (lat) или '-' чтобы пропустить.",
            "restaurant_lon": "Введите долготу (lon) или '-' чтобы пропустить.",
            "game_number": "Введите номер игры или '-' чтобы пропустить.",
            "lottery_id": "Введите id для лотереи или '-' чтобы пропустить.",
        }
        reply_markup = None
        if step == "title":
            keyboard = types.InlineKeyboardMarkup()
            keyboard.add(types.InlineKeyboardButton("Назад", callback_data="menu:manage"))
            reply_markup = keyboard
        bot.send_message(chat_id, prompts.get(step, "Введите значение."), reply_markup=reply_markup)

    def start_addgame_flow(message, user_id: int) -> None:
        pending_actions[user_id] = "addgame"
        pending_addgame[user_id] = {"step": "title", "data": {}}
        prompt_for_addgame_step(message.chat.id, "title")

    def send_game_poll(message, game_link: str, labels_text: str | None) -> None:
        organizer = get_organizer_by_link(db_path, game_link) or get_default_organizer(db_path)
        if not organizer:
            bot.send_message(message.chat.id, "Организатор не найден.")
            return
        resolved_id = extract_external_id_from_link(
            game_link, organizer.get("game_url_template")
        )
        if not resolved_id:
            bot.send_message(message.chat.id, "Не удалось извлечь id игры из ссылки.")
            return
        if forward_poll_to_user(resolved_id, message.chat.id):
            bot.send_message(message.chat.id, "Так вот же он")
            return
        ok, result = fetch_game_from_api(
            resolved_id,
            organizer.get("api_view_template") or "",
            organizer.get("game_url_template") or "",
        )
        if not ok:
            bot.send_message(message.chat.id, str(result))
            return
        payload, error_text = build_game_payload_from_api(result, organizer.get("api_parser"))
        if error_text:
            bot.send_message(message.chat.id, error_text)
            return
        assert payload is not None
        payload_external_id = payload.get("external_id")
        if payload_external_id and payload_external_id != resolved_id:
            if forward_poll_to_user(payload_external_id, message.chat.id):
                bot.send_message(message.chat.id, "Так вот же он")
                return
        question = build_poll_question_from_payload(payload)
        if not question:
            bot.send_message(message.chat.id, "В API нет обязательных полей title/date.")
            return
        external_id_for_game = payload_external_id or resolved_id
        game_db_id = get_game_id_by_external_id(db_path, external_id_for_game)
        if game_db_id is None:
            registration_external_id = payload.get("registration_external_id") or external_id_for_game
            game_db_id = add_game(
                db_path,
                payload["title"],
                payload["game_datetime"],
                description=payload["description"],
                restaurant_name=payload["restaurant_name"],
                restaurant_address=payload["restaurant_address"],
                restaurant_lat=payload["restaurant_lat"],
                restaurant_lon=payload["restaurant_lon"],
                game_number=payload["game_number"],
                lottery_id=payload["lottery_id"],
                external_id=external_id_for_game,
                registration_external_id=registration_external_id,
                organizer_id=organizer.get("id"),
            )
        labels = parse_poll_labels(labels_text)
        reply = bot.send_poll(
            message.chat.id,
            question,
            labels,
            is_anonymous=False,
            allows_multiple_answers=False,
        )
        if reply and reply.poll:
            record_poll(
                db_path,
                reply.poll.id,
                game_db_id,
                reply.chat.id,
                reply.message_id,
                message.from_user.id if message.from_user else None,
            )
            if message.chat.type in {"group", "supergroup"}:
                game_for_actions = {
                    "id": game_db_id,
                    "title": payload.get("title"),
                    "game_date": payload.get("game_datetime"),
                    "restaurant_name": payload.get("restaurant_name"),
                    "restaurant_address": payload.get("restaurant_address"),
                    "restaurant_lat": payload.get("restaurant_lat"),
                    "restaurant_lon": payload.get("restaurant_lon"),
                }
                try:
                    bot.edit_message_reply_markup(
                        reply.chat.id,
                        reply.message_id,
                        reply_markup=build_group_game_keyboard(
                            game_for_actions,
                            reply.chat.id,
                            reply.message_id,
                            bot_username or "QPHelperBot",
                        ),
                    )
                except Exception:
                    logging.exception("Failed to attach safe links to poll")

    def import_game_from_api(message, game_id: str) -> None:
        organizer = get_organizer_by_link(db_path, game_id) or get_default_organizer(db_path)
        if not organizer:
            bot.send_message(message.chat.id, "Организатор не найден.")
            return
        resolved_id = extract_external_id_from_link(
            game_id, organizer.get("game_url_template")
        )
        if not resolved_id:
            bot.send_message(message.chat.id, "Не удалось извлечь id игры из ссылки.")
            return

        ok, result = fetch_game_from_api(
            resolved_id,
            organizer.get("api_view_template") or "",
            organizer.get("game_url_template") or "",
        )
        if not ok:
            bot.send_message(message.chat.id, str(result))
            return

        payload, error_text = build_game_payload_from_api(result, organizer.get("api_parser"))
        if error_text:
            bot.send_message(message.chat.id, error_text)
            return
        assert payload is not None
        external_id_for_game = payload.get("external_id") or resolved_id
        registration_external_id = payload.get("registration_external_id") or external_id_for_game
        game_db_id = add_game(
            db_path,
            payload["title"],
            payload["game_datetime"],
            description=payload["description"],
            restaurant_name=payload["restaurant_name"],
            restaurant_address=payload["restaurant_address"],
            restaurant_lat=payload["restaurant_lat"],
            restaurant_lon=payload["restaurant_lon"],
            game_number=payload["game_number"],
            lottery_id=payload["lottery_id"],
            external_id=external_id_for_game,
            registration_external_id=registration_external_id,
            organizer_id=organizer.get("id"),
        )
        bot.send_message(
            message.chat.id,
            f"Игра добавлена из API. ID: {game_db_id}.",
        )

    def advance_addgame_step(user_id: int) -> str | None:
        flow = pending_addgame.get(user_id)
        if not flow:
            return None
        current = flow.get("step")
        if current not in addgame_steps:
            return None
        index = addgame_steps.index(current)
        if index + 1 >= len(addgame_steps):
            return None
        next_step = addgame_steps[index + 1]
        flow["step"] = next_step
        return next_step

    def finalize_addgame_flow(message, data: dict) -> None:
        organizer = get_default_organizer(db_path)
        if not organizer:
            bot.send_message(message.chat.id, "Организатор не настроен.")
            clear_pending_for_user(message.from_user.id)
            return
        game_id = add_game(
            db_path,
            data.get("title"),
            data.get("game_date"),
            data.get("description"),
            data.get("restaurant_name"),
            data.get("restaurant_address"),
            data.get("restaurant_lat"),
            data.get("restaurant_lon"),
            data.get("game_number"),
            data.get("lottery_id"),
            organizer_id=organizer.get("id"),
        )
        clear_pending_for_user(message.from_user.id)
        bot.send_message(message.chat.id, f"Игра добавлена. ID: {game_id}")

    def handle_addgame_input(message) -> bool:
        user_id = message.from_user.id
        flow = pending_addgame.get(user_id)
        if not flow:
            start_addgame_flow(message, user_id)
            return True

        step = flow.get("step")
        data = flow.get("data", {})
        text = message.text.strip()

        if step == "title":
            if not text or is_skip_value(text):
                bot.send_message(message.chat.id, "Название игры не может быть пустым.")
                return True
            data["title"] = text
            flow["data"] = data
            next_step = advance_addgame_step(user_id)
            if next_step:
                prompt_for_addgame_step(message.chat.id, next_step)
            return True

        if step == "game_datetime":
            if is_skip_value(text):
                bot.send_message(
                    message.chat.id,
                    "Дата обязательна. Формат: YYYY-MM-DD [HH:MM] или DD.MM.YYYY [HH:MM].",
                )
                return True
            normalized = normalize_game_datetime(text)
            if normalized is None:
                bot.send_message(
                    message.chat.id,
                    "Неверный формат даты. Пример: 2026-01-15 19:30 или 15.01.2026.",
                )
                return True
            data["game_date"] = normalized
            flow["data"] = data
            next_step = advance_addgame_step(user_id)
            if next_step:
                prompt_for_addgame_step(message.chat.id, next_step)
            return True

        if step in {
            "description",
            "restaurant_name",
            "restaurant_address",
        }:
            if is_skip_value(text):
                data[step] = None
            else:
                data[step] = text
            flow["data"] = data
            next_step = advance_addgame_step(user_id)
            if next_step:
                prompt_for_addgame_step(message.chat.id, next_step)
            return True

        if step in {"restaurant_lat", "restaurant_lon"}:
            if is_skip_value(text):
                data[step] = None
            else:
                try:
                    data[step] = float(text.replace(",", "."))
                except ValueError:
                    bot.send_message(message.chat.id, "Нужен числовой формат, например 55.7558.")
                    return True
            flow["data"] = data
            next_step = advance_addgame_step(user_id)
            if next_step:
                prompt_for_addgame_step(message.chat.id, next_step)
            return True

        if step in {"game_number", "lottery_id"}:
            if is_skip_value(text):
                data[step] = None
            else:
                if not text.isdigit():
                    bot.send_message(message.chat.id, "Нужно целое число или '-' для пропуска.")
                    return True
                data[step] = int(text)
            flow["data"] = data
            next_step = advance_addgame_step(user_id)
            if next_step:
                prompt_for_addgame_step(message.chat.id, next_step)
                return True
            finalize_addgame_flow(message, data)
            return True

        return False

    def send_games_list(message) -> None:
        games = list_games(db_path)
        if not games:
            bot.send_message(message.chat.id, "Пока нет добавленных игр.")
            return

        lines = ["Доступные игры:"]
        for game in games:
            game_date = f" ({game['game_date']})" if game["game_date"] else ""
            lines.append(
                f"{game['id']}. {game['title']}{game_date} "
                f"— записано: {game['registrations']}"
            )
        bot.send_message(message.chat.id, "\n".join(lines))

    def send_upcoming_schedule(message) -> None:
        bot.send_message(message.chat.id, build_upcoming_schedule_text(db_path))

    def _build_main_menu_text(user_id: int | None) -> str:
        games = (
            list_upcoming_games_with_counts(db_path, user_id)
            if user_id is not None
            else []
        )
        lines = ["Привет! Я бот для нашей квиз-команды 🙂", "", "Предстоящие игры:"]
        if not games:
            lines.append("Ближайших игр пока нет.")
            return "\n".join(lines)
        username = bot_username or "QPHelperBot"
        for game in games:
            status_icon, _ = get_participation_status(game)
            title = escape_html(str(game.get("title") or "Игра"))
            game_url = f"https://t.me/{username}?start=game_{game['id']}"
            date_text = format_game_datetime(game.get("game_date"))
            date_suffix = f" ({escape_html(date_text)})" if date_text else ""
            lines.append(f'{status_icon} <a href="{game_url}">{title}</a>{date_suffix}')
        lines.extend(["", "Нажмите на название, чтобы сразу открыть нужную игру."])
        return "\n".join(lines)

    def send_main_menu_to_chat(chat_id: int, is_admin_user: bool, user_id: int | None = None) -> None:
        bot.send_message(
            chat_id,
            _build_main_menu_text(user_id),
            reply_markup=build_main_menu(is_admin_user, share_app_url or None),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )

    def edit_main_menu(message, user_id: int, is_admin_user: bool) -> None:
        bot.edit_message_text(
            _build_main_menu_text(user_id),
            chat_id=message.chat.id,
            message_id=message.message_id,
            reply_markup=build_main_menu(is_admin_user, share_app_url or None),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )

    def send_main_menu(
        message, is_admin_user: bool, show_reply_keyboard: bool = True, user_id: int | None = None
    ) -> None:
        if user_id is None:
            user_id = message.from_user.id if message.from_user else None
        send_main_menu_to_chat(message.chat.id, is_admin_user, user_id=user_id)

    def send_user_games(message, user_id: int | None = None) -> None:
        resolved_user_id = user_id
        if resolved_user_id is None and message.from_user:
            resolved_user_id = message.from_user.id
        if resolved_user_id is None:
            bot.send_message(message.chat.id, "Не удалось определить пользователя.")
            return

        games = list_user_games(db_path, resolved_user_id)
        if not games:
            bot.send_message(message.chat.id, "Вы пока не записаны ни на одну игру.")
            return

        lines = ["Вы записаны на игры:"]
        username = bot_username or "QPHelperBot"
        for game in games:
            game_date = (
                f" ({escape_html(format_game_datetime(game['game_date']))})"
                if game["game_date"]
                else ""
            )
            game_url = f"https://t.me/{username}?start=game_{game['id']}"
            lines.append(
                f'• <a href="{game_url}">{escape_html(str(game["title"]))}</a>{game_date}'
            )
        keyboard = types.InlineKeyboardMarkup()
        keyboard.add(types.InlineKeyboardButton("🏠 В главное меню", callback_data="menu:manage_back"))
        bot.send_message(
            message.chat.id,
            "\n".join(lines),
            reply_markup=keyboard,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )

    def send_admin_requests(message) -> None:
        requests = list_admin_requests(db_path)
        if not requests:
            keyboard = types.InlineKeyboardMarkup()
            keyboard.add(types.InlineKeyboardButton("Назад", callback_data="menu:manage"))
            bot.send_message(message.chat.id, "Заявок нет.", reply_markup=keyboard)
            return

        send_admin_request_item(message.chat.id, requests[0], len(requests))

    def _build_games_page(user_id: int, offset: int = 0):
        total = count_upcoming_games(db_path)
        if total <= 0:
            return None, None, 0, 0
        safe_offset = min(max(offset, 0), total - 1)
        games = list_upcoming_games_with_counts_limit(
            db_path, user_id, limit=1, offset=safe_offset
        )
        if not games:
            return None, None, 0, total
        text, keyboard = build_game_block(
            games[0],
            False,
            page_index=safe_offset,
            total_games=total,
            bot_username=bot_username or "QPHelperBot",
            share_app_url=share_app_url or None,
        )
        return text, keyboard, safe_offset, total

    def _game_page_index(user_id: int, game_id: int) -> tuple[int | None, int]:
        games = list_upcoming_games_with_counts(db_path, user_id)
        for index, game in enumerate(games):
            if int(game["id"]) == game_id:
                return index, len(games)
        return None, len(games)

    def send_games_list_blocks(message, user_id: int | None = None) -> None:
        resolved_user_id = user_id
        if resolved_user_id is None and message.from_user:
            resolved_user_id = message.from_user.id
        if resolved_user_id is None:
            bot.send_message(message.chat.id, "Не удалось определить пользователя.")
            return
        text, keyboard, _, _ = _build_games_page(resolved_user_id, 0)
        if text is None:
            keyboard = types.InlineKeyboardMarkup(row_width=1)
            keyboard.add(types.InlineKeyboardButton("В главное меню", callback_data="menu:manage_back"))
            bot.send_message(
                message.chat.id,
                "Ближайших игр пока нет.",
                reply_markup=keyboard,
            )
            return

        reply = bot.send_message(
            message.chat.id,
            text,
            reply_markup=keyboard,
            parse_mode="MarkdownV2",
            disable_web_page_preview=True,
        )
        if resolved_user_id is not None and reply:
            list_message_refs[resolved_user_id] = (reply.chat.id, reply.message_id)

    def send_admin_games_list(message) -> None:
        games = [game for game in list_upcoming_games_admin(db_path) if not game.get("is_archived")]
        if not games:
            keyboard = types.InlineKeyboardMarkup(row_width=1)
            keyboard.add(types.InlineKeyboardButton("В главное меню", callback_data="menu:manage_back"))
            bot.send_message(
                message.chat.id,
                "Ближайших игр пока нет.",
                reply_markup=keyboard,
            )
            return
        text, keyboard = build_admin_cancel_list_message(games, bot_username, start_index=1)
        bot.send_message(
            message.chat.id,
            text,
            reply_markup=keyboard,
            parse_mode="MarkdownV2",
            disable_web_page_preview=True,
        )

    def notify_game_users(
        game_id: int,
        text: str,
        include_cancelled: bool,
        exclude_user_id: int | None = None,
        recipients: list[dict] | None = None,
    ) -> None:
        sent_chats: set[int] = set()
        if recipients is None:
            recipients = list_game_registrants(db_path, game_id, include_cancelled)
        for user in recipients:
            if exclude_user_id is not None and user.get("user_id") == exclude_user_id:
                continue
            chat_id = get_user_last_chat_id(db_path, user["user_id"])
            if chat_id is None:
                continue
            if chat_id in sent_chats:
                continue
            sent_chats.add(chat_id)
            try:
                bot.send_message(chat_id, text)
            except Exception:
                logging.exception("Failed to notify user %s", user["user_id"])

    def cancel_game_and_notify(game_id: int, actor_user_id: int | None) -> bool:
        recipients = list_game_registrants(db_path, game_id, include_cancelled=False)
        if not archive_game(db_path, game_id):
            return False
        game_info = get_game_basic(db_path, game_id)
        title = game_info["title"] if game_info else "Игра"
        date_text = format_game_datetime(game_info["game_date"]) if game_info else "—"
        notify_game_users(
            game_id,
            f"Игра отменена: {title} ({date_text}).",
            include_cancelled=False,
            exclude_user_id=actor_user_id,
            recipients=recipients,
        )
        return True

    def send_admin_request_item(chat_id: int, item: dict, total: int) -> None:
        username = f"@{item['username']}" if item["username"] else "без username"
        full_name = item["full_name"] or ""
        text = (
            f"Заявка {item['id']} (в очереди: {total}):\n"
            f"{username} {full_name}\n"
            f"user_id={item['user_id']}"
        )
        keyboard = types.InlineKeyboardMarkup(row_width=2)
        keyboard.add(
            types.InlineKeyboardButton(
                "Одобрить", callback_data=f"adminreq:approve:{item['id']}"
            ),
            types.InlineKeyboardButton(
                "Отклонить", callback_data=f"adminreq:deny:{item['id']}"
            ),
        )
        keyboard.add(types.InlineKeyboardButton("Назад", callback_data="menu:manage"))
        bot.send_message(chat_id, text, reply_markup=keyboard)

    def handle_private_start_like(message) -> None:
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) > 1 and parts[1].startswith("adminreg_"):
            raw_game_id = parts[1].split("adminreg_", 1)[1]
            if not is_admin(db_path, message.from_user.username):
                bot.send_message(message.chat.id, "Эта регистрация доступна только админам.")
            elif raw_game_id.isdigit():
                game_id = int(raw_game_id)
                ok, result_text = _register_game_on_site(game_id, message.from_user.id)
                if ok:
                    result_text = build_poll_registered_message(db_path, game_id) or result_text
                bot.send_message(message.chat.id, result_text, parse_mode="MarkdownV2" if ok else None)
            else:
                bot.send_message(message.chat.id, "Не удалось прочитать игру.")
            safe_delete_message(message.chat.id, message.message_id)
            return
        if len(parts) > 1 and parts[1].startswith("adminregskip_"):
            raw_game_id = parts[1].split("adminregskip_", 1)[1]
            if not is_admin(db_path, message.from_user.username):
                bot.send_message(message.chat.id, "Это действие доступно только админам.")
            elif raw_game_id.isdigit():
                result_text = build_poll_registered_message(db_path, int(raw_game_id))
                bot.send_message(message.chat.id, result_text or "Игра не найдена.", parse_mode="MarkdownV2" if result_text else None)
            else:
                bot.send_message(message.chat.id, "Не удалось прочитать игру.")
            safe_delete_message(message.chat.id, message.message_id)
            return
        if len(parts) > 1 and parts[1].startswith("game_"):
            raw_game_id = parts[1].split("game_", 1)[1]
            if raw_game_id.isdigit():
                game_id = int(raw_game_id)
                page_index, total_games = _game_page_index(message.from_user.id, game_id)
                game = get_upcoming_game_with_counts(
                    db_path, message.from_user.id, game_id
                )
                if game and page_index is not None:
                    text, keyboard = build_game_block(
                        game,
                        False,
                        page_index=page_index,
                        total_games=total_games,
                        bot_username=bot_username or "QPHelperBot",
                        share_app_url=share_app_url or None,
                    )
                    reply = bot.send_message(
                        message.chat.id,
                        text,
                        reply_markup=keyboard,
                        parse_mode="MarkdownV2",
                        disable_web_page_preview=True,
                    )
                    if reply:
                        list_message_refs[message.from_user.id] = (
                            reply.chat.id,
                            reply.message_id,
                        )
                    safe_delete_message(message.chat.id, message.message_id)
                    return
            bot.send_message(message.chat.id, "Эта игра уже прошла или была отменена.")
            safe_delete_message(message.chat.id, message.message_id)
            return
        if len(parts) > 1 and parts[1].startswith("poll_"):
            external_id = parts[1].split("poll_", 1)[1]
            if external_id:
                send_game_poll(message, external_id, None)
                safe_delete_message(message.chat.id, message.message_id)
                return
        if len(parts) > 1 and parts[1].startswith("forward_"):
            external_id = parts[1].split("forward_", 1)[1]
            if external_id:
                back_keyboard = types.InlineKeyboardMarkup()
                back_keyboard.add(
                    types.InlineKeyboardButton("В список игр", callback_data="menu:gamelist")
                )
                ok = forward_poll_to_user(
                    external_id,
                    message.chat.id,
                    prompt_text="Голосуй, если осмелишься",
                    prompt_keyboard=back_keyboard,
                    track_for_cleanup=True,
                )
                if not ok:
                    bot.send_message(message.chat.id, "Опрос для этой игры не найден.")
                refs = list_message_refs.pop(message.from_user.id, None)
                if refs:
                    chat_id, list_message_id = refs
                    safe_delete_message(chat_id, list_message_id)
                safe_delete_message(message.chat.id, message.message_id)
                return
        if len(parts) > 1 and parts[1].startswith("cancel_"):
            if not is_admin(db_path, message.from_user.username):
                bot.send_message(message.chat.id, "Команда доступна только админам.")
                return
            raw_id = parts[1].split("cancel_", 1)[1]
            if raw_id.isdigit():
                game_id = int(raw_id)
                ok = cancel_game_and_notify(game_id, message.from_user.id)
                if ok:
                    bot.send_message(message.chat.id, "Игра отменена.")
                else:
                    bot.send_message(message.chat.id, "Игра не найдена или уже отменена.")
            else:
                bot.send_message(message.chat.id, "Нужен числовой id игры.")
            safe_delete_message(message.chat.id, message.message_id)
            return
        if is_admin(db_path, message.from_user.username):
            send_main_menu(message, True, show_reply_keyboard=True)
            safe_delete_message(message.chat.id, message.message_id)
            return

        send_main_menu(message, False, show_reply_keyboard=True)
        safe_delete_message(message.chat.id, message.message_id)

    @bot.message_handler(commands=["start"])
    def start_handler(message):
        store_message(message)
        clear_pending(message)
        if message.chat.type != "private":
            bot.send_message(message.chat.id, "Команда доступна только в личке.")
            return
        handle_private_start_like(message)

    @bot.message_handler(commands=["dmme"])
    def dmme_handler(message):
        store_message(message)
        clear_pending(message)
        if message.chat.type not in {"group", "supergroup"}:
            bot.send_message(message.chat.id, "Команда доступна только в общем чате.")
            return
        if not message.from_user:
            bot.send_message(message.chat.id, "Не удалось определить пользователя.")
            return
        is_admin_user = is_admin(db_path, message.from_user.username)
        try:
            send_main_menu_to_chat(message.from_user.id, is_admin_user, user_id=message.from_user.id)
            safe_delete_message(message.chat.id, message.message_id)
        except Exception as exc:
            error_text = str(exc).lower()
            if isinstance(exc, ApiTelegramException):
                result_json = getattr(exc, "result_json", None)
                if isinstance(result_json, dict):
                    error_text = str(result_json.get("description") or error_text).lower()
            logging.warning(
                "Failed to send DM menu to user_id=%s from chat_id=%s: %s",
                message.from_user.id,
                message.chat.id,
                error_text,
            )
            bot.send_message(
                message.chat.id,
                "Не получилось написать тебе в личку 😢 Сначала открой диалог с ботом и нажми /start.",
            )

    @bot.message_handler(commands=["games"])
    def games_handler(message):
        store_message(message)
        clear_pending(message)
        send_games_list(message)

    @bot.message_handler(commands=["help"])
    def help_handler(message):
        store_message(message)
        clear_pending(message)
        text = (
            "Я помогаю беседе собираться на квизы 🤖\n\n"
            "• Голосуйте в опросах в общей беседе — ответ «Да» сразу отражается в записи.\n"
            "• /schedule — ближайшие игры, /mygames — игры, на которые ты записан.\n"
            "• /mystats — личная статистика за месяц и всё время.\n"
            "• Чтобы создать или восстановить опрос, напишите именно в общей беседе: "
            "@QPHelperBot сделай опрос <ссылка на игру> Да, Нет, Не знаю.\n"
            "• Команда /dmme нужна только в общей беседе: бот пришлёт личное меню.\n"
            "• За день напомню о свободных местах, в день игры — время, место и состав.\n"
            "• После игры попрошу беседу внести место и полученные призы.\n\n"
            "Если что-то сломалось — пишите @totodeep."
        )
        keyboard = types.InlineKeyboardMarkup()
        keyboard.add(types.InlineKeyboardButton("Всё понятно", callback_data="help:ack"))
        reply = bot.send_message(message.chat.id, text, reply_markup=keyboard)
        if message.from_user and reply:
            help_message_refs[message.from_user.id] = (
                message.chat.id,
                message.message_id,
                reply.message_id,
            )

    @bot.message_handler(commands=["mygames"])
    def mygames_handler(message):
        store_message(message)
        clear_pending(message)
        send_user_games(message, message.from_user.id)

    @bot.message_handler(commands=["mystats"])
    def mystats_handler(message):
        store_message(message)
        clear_pending(message)
        stats_text = build_personal_stats(
            db_path, message.from_user.id, message.from_user.username, "current"
        )
        bot.send_message(
            message.chat.id,
            stats_text,
            reply_markup=_build_stats_keyboard(stats_text),
        )

    @bot.message_handler(commands=["monthstats"])
    def monthstats_handler(message):
        store_message(message)
        clear_pending(message)
        parts = (message.text or "").split(maxsplit=1)
        month_key = parts[1].strip() if len(parts) > 1 else None
        try:
            text = format_monthly_card(build_monthly_stats(db_path, month_key))
        except ValueError as exc:
            text = str(exc)
        bot.send_message(
            message.chat.id,
            text,
            reply_markup=build_share_keyboard(
                text,
                f"https://t.me/{bot_username}" if bot_username else "https://t.me",
                "Поделиться итогами",
            ),
        )

    @bot.message_handler(commands=["reconcile"])
    def reconcile_handler(message):
        store_message(message)
        clear_pending(message)
        if not is_admin(db_path, message.from_user.username):
            bot.send_message(message.chat.id, "Команда доступна только админам.")
            return
        result = reconcile_poll_registrations(db_path, apply_changes=True)
        bot.send_message(message.chat.id, _format_reconciliation(result))

    @bot.message_handler(commands=["adminstatus"])
    def adminstatus_handler(message):
        store_message(message)
        clear_pending(message)
        if not is_admin(db_path, message.from_user.username):
            bot.send_message(message.chat.id, "Команда доступна только админам.")
            return
        bot.send_message(
            message.chat.id,
            _format_admin_status(),
            reply_markup=_build_registration_status_keyboard(),
        )

    @bot.message_handler(commands=["retryreg"])
    def retry_registration_handler(message):
        store_message(message)
        clear_pending(message)
        if not is_admin(db_path, message.from_user.username):
            bot.send_message(message.chat.id, "Команда доступна только админам.")
            return
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) != 2 or not parts[1].isdigit():
            bot.send_message(message.chat.id, "Формат: /retryreg <id игры>")
            return
        _, text = _register_game_on_site(int(parts[1]), message.from_user.id)
        bot.send_message(message.chat.id, text)

    @bot.message_handler(commands=["setting"])
    def setting_handler(message):
        store_message(message)
        clear_pending(message)
        if not is_admin(db_path, message.from_user.username):
            bot.send_message(message.chat.id, "Команда доступна только админам.")
            return
        parts = (message.text or "").split(maxsplit=2)
        allowed = {
            "reactivation_enabled",
            "registration_comment",
            "default_team_name",
            "amra_team_name",
            "quizplease_promo_id",
            "quizplease_promo_end_date",
        }
        if len(parts) != 3 or parts[1] not in allowed:
            bot.send_message(
                message.chat.id,
                "Формат: /setting <ключ> <значение>\nКлючи: " + ", ".join(sorted(allowed)),
            )
            return
        key, value = parts[1], parts[2].strip()
        if not value or len(value) > 500:
            bot.send_message(message.chat.id, "Значение должно содержать от 1 до 500 символов.")
            return
        if key == "reactivation_enabled" and value not in {"0", "1"}:
            bot.send_message(message.chat.id, "reactivation_enabled принимает только 0 или 1.")
            return
        if key == "quizplease_promo_id" and (not value.isdigit() or int(value) < 1):
            bot.send_message(message.chat.id, "quizplease_promo_id должен быть положительным числом.")
            return
        if key == "quizplease_promo_end_date":
            try:
                datetime.strptime(value, "%Y-%m-%d")
            except ValueError:
                bot.send_message(message.chat.id, "Дата нужна в формате YYYY-MM-DD.")
                return
        set_setting(db_path, key, value, message.from_user.id)
        bot.send_message(message.chat.id, "Настройка сохранена.")

    def send_schedule_with_ack(message) -> None:
        if message.chat.type not in {"group", "supergroup"}:
            return
        text = build_upcoming_schedule_text(db_path, user_id=None)
        keyboard = types.InlineKeyboardMarkup()
        keyboard.add(types.InlineKeyboardButton("Всё понятно", callback_data="schedule:ack"))
        reply = bot.send_message(
            message.chat.id,
            text,
            reply_markup=keyboard,
            disable_notification=True,
        )
        schedule_message_refs[message.from_user.id] = (
            message.chat.id,
            message.message_id,
            reply.message_id,
        )

    @bot.message_handler(commands=["schedule"])
    def schedule_handler(message):
        store_message(message)
        clear_pending(message)
        if message.chat.type == "private":
            handle_private_start_like(message)
            return
        send_schedule_with_ack(message)

    def is_postgame_result_reply(message) -> bool:
        reply = getattr(message, "reply_to_message", None)
        chat = getattr(message, "chat", None)
        if not reply or not chat or getattr(chat, "type", None) not in {"group", "supergroup"}:
            return False
        return get_prompt_game(db_path, chat.id, reply.message_id) is not None

    @bot.message_handler(func=is_postgame_result_reply, content_types=["text"])
    def postgame_result_handler(message):
        store_message(message)
        prompt_game = get_prompt_game(
            db_path, message.chat.id, message.reply_to_message.message_id
        )
        if not prompt_game:
            return
        result, error = parse_result_reply(message.text or "")
        if error:
            bot.reply_to(
                message,
                f"{error}\n\nОтветьте реплаем по форме:\n"
                "Место: 1\n"
                "Из скольки команд: 30\n"
                "Лотерея: ничего\n"
                "Подлутки: ничего",
            )
            return
        updated = save_game_result(
            db_path,
            prompt_game["game_id"],
            result,
            message.from_user.id,
            message.from_user.username,
            get_full_name(message.from_user),
        )
        verb = "обновлён" if updated else "сохранён"
        bot.reply_to(
            message,
            f"Результат {verb}: {result['position']} место из {result['total_teams']}.\n"
            f"Лотерея: {result['lottery_text']}\n"
            f"Подлутки: {result['podlutki_text']}",
        )

    @bot.message_handler(
        func=lambda msg: (
            bool(msg.text)
            and msg.chat.type in {"group", "supergroup"}
            and msg.text.strip().lower() == "@qphelperbot расписание"
        )
    )
    def schedule_mention_handler(message):
        store_message(message)
        clear_pending(message)
        send_schedule_with_ack(message)

    @bot.message_handler(
        func=lambda msg: (
            bool(msg.text)
            and msg.chat.type in {"group", "supergroup"}
            and msg.text.strip().lower().startswith("@qphelperbot сделай опрос")
        )
    )
    def schedule_poll_handler(message):
        store_message(message)
        clear_pending(message)
        text = message.text.strip()
        prefix = "@qphelperbot сделай опрос"
        lowered = text.lower()
        if not lowered.startswith(prefix):
            bot.send_message(
                message.chat.id,
                'Формат: "@qphelperbot сделай опрос <ссылка> Да, Нет, Не знаю".',
            )
            return
        remainder = text[len(prefix) :].strip()
        if not remainder:
            bot.send_message(
                message.chat.id,
                'Формат: "@qphelperbot сделай опрос <ссылка> Да, Нет, Не знаю".',
            )
            return
        parts = remainder.split(maxsplit=1)
        game_link = parts[0].strip()
        labels_text = parts[1].strip() if len(parts) > 1 else None
        if labels_text and "[" in labels_text and "]" in labels_text:
            start = labels_text.rfind("[")
            end = labels_text.rfind("]")
            if end > start:
                labels_text = labels_text[start + 1 : end]
        send_game_poll(message, game_link, labels_text)

    @bot.message_handler(
        func=lambda msg: (
            bool(msg.text)
            and msg.chat.type in {"group", "supergroup"}
            and "@qphelperbot" in msg.text.lower()
            and "сделай опрос" not in msg.text.lower()
        )
    )
    def poll_link_mention_handler(message):
        store_message(message)
        clear_pending(message)
        text = message.text.strip()
        parts = text.split(maxsplit=1)
        game_link = parts[1].strip() if len(parts) > 1 else ""
        labels_text = None
        if game_link:
            remainder = parts[1].strip()
            remainder_parts = remainder.split(maxsplit=1)
            game_link = remainder_parts[0].strip()
            labels_text = remainder_parts[1].strip() if len(remainder_parts) > 1 else None
        if labels_text and "[" in labels_text and "]" in labels_text:
            start = labels_text.rfind("[")
            end = labels_text.rfind("]")
            if end > start:
                labels_text = labels_text[start + 1 : end]
        if not game_link:
            resolved_id = None
        else:
            organizer = get_organizer_by_link(db_path, game_link) or get_default_organizer(db_path)
            if not organizer:
                bot.send_message(message.chat.id, "Организатор не найден.")
                return
            resolved_id = extract_external_id_from_link(
                game_link, organizer.get("game_url_template")
            )
        if not resolved_id:
            bot.send_message(
                message.chat.id,
                'Нужна ссылка на игру. Пример: "@qphelperbot <ссылка> Да, Нет, Не знаю".',
            )
            return
        send_game_poll(message, game_link, labels_text)

    def handle_poll_departure(poll_id: str, poll: dict, user_id: int) -> None:
        ok, _ = unregister_player(db_path, poll["game_id"], user_id)
        remove_waitlist_entry(db_path, poll["game_id"], user_id)
        if not (ok and poll.get("full_notified")):
            return

        game = get_game_basic(db_path, poll["game_id"])
        title = game["title"] if game else "Игра"
        waitlisted = get_waitlist_first(db_path, poll["game_id"])
        if waitlisted:
            wait_name = (
                f"@{waitlisted['username'].lstrip('@')}"
                if waitlisted.get("username")
                else waitlisted.get("full_name") or f"id:{waitlisted.get('user_id')}"
            )
            ok_wait, _ = register_player(
                db_path,
                poll["game_id"],
                waitlisted["user_id"],
                waitlisted.get("username"),
                waitlisted.get("full_name") or "",
            )
            if not ok_wait:
                remove_waitlist_entry(db_path, poll["game_id"], waitlisted["user_id"])
            if ok_wait:
                if game and game.get("game_date"):
                    date_text = format_game_datetime(game.get("game_date"))
                    text = (
                        f"на игру освободилось место: {title} — {date_text}. "
                        f"Взял из резерва {wait_name}"
                    )
                else:
                    text = f"на игру освободилось место. Взял из резерва {wait_name}"
            else:
                waitlisted = None
        else:
            if game and game.get("game_date"):
                date_text = format_game_datetime(game.get("game_date"))
                text = f"освободилось место: {title} — {date_text}"
            else:
                text = f"освободилось место: {title}"
        bot.send_message(poll["chat_id"], text)
        if not waitlisted:
            clear_poll_full_notified(db_path, poll_id)

    @bot.poll_answer_handler()
    def poll_answer_handler(poll_answer):
        poll_id = poll_answer.poll_id
        option_ids = poll_answer.option_ids or []
        if not option_ids:
            poll = get_poll_by_id(db_path, poll_id)
            delete_poll_answer(db_path, poll_id, poll_answer.user.id)
            if poll:
                handle_poll_departure(poll_id, poll, poll_answer.user.id)
            return
        option_id = option_ids[0]
        username = poll_answer.user.username
        full_name = get_full_name(poll_answer.user)
        upsert_user_profile(db_path, poll_answer.user.id, username, full_name)
        if not record_poll_answer(
            db_path, poll_id, poll_answer.user.id, option_id, username, full_name
        ):
            logging.warning("Ignored answer for unknown poll_id=%s", poll_id)
            return
        poll = get_poll_by_id(db_path, poll_id)
        if not poll:
            return
        yes_count = get_poll_yes_count(db_path, poll_id, yes_option_id=0)
        poll_limit = get_poll_max_people_for_game_id(db_path, poll["game_id"]) or POLL_MAX_PEOPLE
        if option_id == 0:
            if yes_count <= poll_limit:
                register_player(db_path, poll["game_id"], poll_answer.user.id, username, full_name)
            else:
                ok_wait, _ = add_waitlist(
                    db_path, poll["game_id"], poll_answer.user.id, username, full_name
                )
                if ok_wait:
                    game = get_game_basic(db_path, poll["game_id"])
                    title = escape_markdown(game.get("title") or "Игра") if game else "Игра"
                    date_text = (
                        escape_markdown(format_game_datetime(game.get("game_date")))
                        if game and game.get("game_date")
                        else ""
                    )
                    details = f"{title} \\({date_text}\\)" if date_text else title
                    try:
                        bot.send_message(
                            poll_answer.user.id,
                            "Вижу твою регистрацию на игру "
                            f"*{details}*\\. "
                            "Пока все места заняты и я записал тебя в резерв. Отмечу тебя в общем диалоге, если место освободится",
                            parse_mode="MarkdownV2",
                        )
                    except Exception:
                        logging.exception(
                            "Failed to send waitlist DM to user %s", poll_answer.user.id
                        )
        else:
            handle_poll_departure(poll_id, poll, poll_answer.user.id)
        if yes_count >= POLL_YES_THRESHOLD and not poll.get("yes_notified"):
            text = build_poll_threshold_message(db_path, poll["game_id"], POLL_YES_THRESHOLD)
            if not text:
                return
            keyboard = types.InlineKeyboardMarkup()
            keyboard.row(
                types.InlineKeyboardButton(
                    "Зарегистрируй нас",
                    url=f"https://t.me/{bot_username or 'QPHelperBot'}?start=adminreg_{poll['game_id']}",
                ),
                types.InlineKeyboardButton(
                    "Справились без тебя",
                    url=f"https://t.me/{bot_username or 'QPHelperBot'}?start=adminregskip_{poll['game_id']}",
                ),
            )
            bot.send_message(
                poll["chat_id"],
                text,
                reply_markup=keyboard,
                parse_mode="MarkdownV2",
            )
            mark_poll_notified(db_path, poll_id)
        if yes_count >= poll_limit and not poll.get("full_notified"):
            text = build_poll_full_message(db_path, poll_id, poll["game_id"])
            if text:
                bot.send_message(
                    poll["chat_id"],
                    text,
                    disable_notification=True,
                    disable_web_page_preview=True,
                    parse_mode="HTML",
                )
            mark_poll_full_notified(db_path, poll_id)

    @bot.message_handler(commands=["signup"])
    def signup_handler(message):
        store_message(message)
        clear_pending(message)
        parts = message.text.split(maxsplit=1)
        if len(parts) < 2:
            send_games_list_blocks(message)
            return
        if not parts[1].isdigit():
            bot.send_message(message.chat.id, "Нужен числовой id игры.")
            return

        game_id = int(parts[1])
        username = message.from_user.username
        full_name = (message.from_user.full_name or "").strip()
        ok, text = register_player(db_path, game_id, message.from_user.id, username, full_name)
        if text:
            bot.send_message(message.chat.id, text)

    @bot.message_handler(commands=["unsubscribe"])
    def unsubscribe_handler(message):
        store_message(message)
        clear_pending(message)
        parts = message.text.split(maxsplit=1)
        if len(parts) < 2:
            set_pending(message, "unsubscribe", "Введите id игры для отписки.")
            return
        if not parts[1].isdigit():
            bot.send_message(message.chat.id, "Нужен числовой id игры.")
            return

        game_id = int(parts[1])
        ok, text = unregister_player(db_path, game_id, message.from_user.id)
        if text:
            bot.send_message(message.chat.id, text)

    @bot.message_handler(commands=["addgame"])
    def addgame_handler(message):
        store_message(message)
        clear_pending(message)
        bot.send_message(
            message.chat.id,
            'Добавление игр доступно через команду "@qphelperbot сделай опрос <ссылка>" в общем чате.',
        )

    @bot.message_handler(commands=["importgame"])
    def importgame_handler(message):
        store_message(message)
        clear_pending(message)
        if not is_admin(db_path, message.from_user.username):
            bot.send_message(message.chat.id, "Команда доступна только админам.")
            return

        parts = message.text.split(maxsplit=1)
        if len(parts) < 2:
            prompt_message = set_pending(message, "importgame", "Введите id игры из API.")
            store_prompt_message(message.from_user.id, prompt_message)
            return

        import_game_from_api(message, parts[1].strip())

    @bot.message_handler(commands=["delgame"])
    def delgame_handler(message):
        store_message(message)
        clear_pending(message)
        if not is_admin(db_path, message.from_user.username):
            bot.send_message(message.chat.id, "Команда доступна только админам.")
            return

        parts = message.text.split(maxsplit=1)
        if len(parts) < 2:
            send_admin_games_list(message)
            return
        if not parts[1].isdigit():
            bot.send_message(message.chat.id, "Нужен числовой id игры.")
            return

        game_id = int(parts[1])
        if cancel_game_and_notify(game_id, message.from_user.id):
            bot.send_message(message.chat.id, "Игра отменена.")
        else:
            bot.send_message(message.chat.id, "Игра не найдена или уже отменена.")

    @bot.message_handler(commands=["wantadmin"])
    def wantadmin_handler(message):
        store_message(message)
        clear_pending(message)
        username = message.from_user.username
        full_name = (message.from_user.full_name or "").strip()
        ok, text = request_admin(db_path, message.from_user.id, username, full_name)
        bot.send_message(message.chat.id, text)

    @bot.message_handler(func=lambda msg: bool(msg.text) and msg.text.strip().lower() == "хочу стать админом")
    def wantadmin_text_handler(message):
        store_message(message)
        clear_pending(message)
        username = message.from_user.username
        full_name = (message.from_user.full_name or "").strip()
        ok, text = request_admin(db_path, message.from_user.id, username, full_name)
        bot.send_message(message.chat.id, text)

    @bot.message_handler(commands=["adminrequests"])
    def adminrequests_handler(message):
        store_message(message)
        clear_pending(message)
        if not is_admin(db_path, message.from_user.username):
            bot.send_message(message.chat.id, "Команда доступна только админам.")
            return

        send_admin_requests(message)

    def send_manage_menu(message) -> None:
        bot.send_message(
            message.chat.id,
            "Выберите действие:",
            reply_markup=build_manage_menu(),
        )

    @bot.message_handler(commands=["approveadmin"])
    def approveadmin_handler(message):
        store_message(message)
        clear_pending(message)
        if not is_admin(db_path, message.from_user.username):
            bot.send_message(message.chat.id, "Команда доступна только админам.")
            return

        parts = message.text.split(maxsplit=1)
        if len(parts) < 2:
            set_pending(message, "approveadmin", "Введите id заявки для одобрения.")
            return
        if not parts[1].isdigit():
            bot.send_message(message.chat.id, "Нужен числовой id заявки.")
            return

        request_id = int(parts[1])
        approved_by = message.from_user.username or get_full_name(message.from_user) or "unknown"
        ok, text = approve_admin_request(db_path, request_id, approved_by)
        bot.send_message(message.chat.id, text)

    @bot.message_handler(commands=["denyadmin"])
    def denyadmin_handler(message):
        store_message(message)
        clear_pending(message)
        if not is_admin(db_path, message.from_user.username):
            bot.send_message(message.chat.id, "Команда доступна только админам.")
            return

        parts = message.text.split(maxsplit=1)
        if len(parts) < 2:
            set_pending(message, "denyadmin", "Введите id заявки для отклонения.")
            return
        if not parts[1].isdigit():
            bot.send_message(message.chat.id, "Нужен числовой id заявки.")
            return

        request_id = int(parts[1])
        ok, text = deny_admin_request(db_path, request_id)
        bot.send_message(message.chat.id, text)

    @bot.message_handler(func=lambda msg: msg.text == "Про игры")
    def gamelist_text_handler(message):
        store_message(message)
        clear_pending(message)
        send_games_list_blocks(message, message.from_user.id)

    @bot.message_handler(func=lambda msg: msg.text == "Управление")
    def manage_text_handler(message):
        store_message(message)
        clear_pending(message)
        if not is_admin(db_path, message.from_user.username):
            bot.send_message(message.chat.id, "Команда доступна только админам.")
            return
        send_manage_menu(message)

    @bot.message_handler(func=lambda msg: msg.text == "Главное меню")
    def main_menu_text_handler(message):
        store_message(message)
        clear_pending(message)
        send_main_menu(
            message,
            is_admin(db_path, message.from_user.username),
            show_reply_keyboard=False,
        )
        safe_delete_message(message.chat.id, message.message_id)

    @bot.message_handler(func=lambda msg: msg.text == "Отписаться от игры")
    def unsubscribe_prompt_handler(message):
        store_message(message)
        clear_pending(message)
        set_pending(message, "unsubscribe", "Введите id игры для отписки.")

    @bot.message_handler(func=lambda msg: msg.text == "Хочу стать админом")
    def wantadmin_button_handler(message):
        store_message(message)
        clear_pending(message)
        username = message.from_user.username
        full_name = (message.from_user.full_name or "").strip()
        ok, text = request_admin(db_path, message.from_user.id, username, full_name)
        bot.send_message(message.chat.id, text)

    @bot.callback_query_handler(func=lambda call: bool(call.data) and call.data.startswith("adminreq:"))
    def admin_request_action_handler(call):
        store_callback(call)
        message = call.message
        if message is None:
            bot.answer_callback_query(call.id)
            return

        if not is_admin(db_path, call.from_user.username):
            bot.answer_callback_query(call.id, "Команда доступна только админам.")
            return

        bot.answer_callback_query(call.id)
        try:
            _, action, raw_id = call.data.split(":", 2)
            request_id = int(raw_id)
        except (ValueError, IndexError):
            bot.send_message(message.chat.id, "Не удалось прочитать заявку.")
            return

        if action == "approve":
            approved_by = call.from_user.username or get_full_name(call.from_user) or "unknown"
            ok, text = approve_admin_request(db_path, request_id, approved_by)
        elif action == "deny":
            ok, text = deny_admin_request(db_path, request_id)
        else:
            bot.send_message(message.chat.id, "Неизвестное действие.")
            return

        try:
            bot.edit_message_reply_markup(
                chat_id=message.chat.id,
                message_id=message.message_id,
                reply_markup=None,
            )
        except Exception:
            logging.exception("Failed to update admin request message")

        bot.send_message(message.chat.id, text)
        requests = list_admin_requests(db_path)
        if requests:
            send_admin_request_item(message.chat.id, requests[0], len(requests))
        else:
            bot.send_message(message.chat.id, "Заявок больше нет.")

    @bot.callback_query_handler(func=lambda call: bool(call.data) and call.data.startswith("game_action:"))
    def game_action_handler(call):
        store_callback(call)
        message = call.message
        if message is None:
            bot.answer_callback_query(call.id)
            return

        if getattr(message.chat, "type", None) in {"group", "supergroup"}:
            bot.answer_callback_query(
                call.id,
                "Откройте игру через Mini App",
                show_alert=True,
            )
            try:
                bot.edit_message_reply_markup(
                    message.chat.id,
                    message.message_id,
                    reply_markup=None,
                )
            except Exception:
                logging.exception("Failed to disable legacy group game buttons")
            return

        bot.answer_callback_query(call.id)
        try:
            _, action, raw_id = call.data.split(":", 2)
        except (ValueError, IndexError):
            bot.send_message(message.chat.id, "Не удалось прочитать игру.")
            return

        if action == "poll":
            if raw_id == "none":
                bot.send_message(message.chat.id, "Опрос для этой игры не найден.")
                safe_delete_message(message.chat.id, message.message_id)
                return
            poll_info = get_poll_by_external_id(db_path, raw_id)
            if not poll_info:
                bot.send_message(message.chat.id, "Опрос для этой игры не найден.")
                safe_delete_message(message.chat.id, message.message_id)
                return
            try:
                forwarded = bot.forward_message(
                    call.from_user.id,
                    poll_info["chat_id"],
                    poll_info["message_id"],
                )
                poll_message_refs[call.from_user.id] = (
                    call.from_user.id,
                    forwarded.message_id,
                )
                back_keyboard = types.InlineKeyboardMarkup()
                back_keyboard.add(
                    types.InlineKeyboardButton("Вернуться в список игр", callback_data="menu:gamelist")
                )
                bot.send_message(
                    call.from_user.id,
                    "Готово. Вернуться к списку игр?",
                    reply_markup=back_keyboard,
                )
                safe_delete_message(message.chat.id, message.message_id)
            except Exception:
                logging.exception("Failed to forward poll to user %s", call.from_user.id)
                bot.send_message(
                    message.chat.id,
                    "Не смог отправить опрос в личку. Напишите мне /start в личке.",
                )
                safe_delete_message(message.chat.id, message.message_id)
            return
        if action == "team_register":
            if not is_admin(db_path, call.from_user.username):
                bot.send_message(
                    message.chat.id,
                    "ноу ноу ноу, мистер фиш, это может сделать только админ",
                )
                safe_delete_message(message.chat.id, message.message_id)
                return
            if raw_id == "none":
                bot.send_message(message.chat.id, "Не удалось найти id игры для регистрации.")
                safe_delete_message(message.chat.id, message.message_id)
                return
            organizer = get_organizer_for_external_id(db_path, raw_id) or get_default_organizer(db_path)
            if not organizer:
                bot.send_message(message.chat.id, "Организатор не настроен.")
                safe_delete_message(message.chat.id, message.message_id)
                return
            reg_info = get_game_registration_info_by_external_id(db_path, raw_id)
            registration_id = reg_info.get("registration_external_id") if reg_info else None
            payload = _build_site_registration_payload(
                organizer,
                registration_id,
                reg_info.get("restaurant_name") if reg_info else None,
            )
            ok, text = register_team_in_api(
                raw_id,
                organizer.get("registration_api_template") or "",
                payload,
            )
            if reg_info:
                record_registration_attempt(
                    db_path, reg_info["game_id"], call.from_user.id, ok, text
                )
            bot.send_message(message.chat.id, text)
            safe_delete_message(message.chat.id, message.message_id)
            return

        try:
            game_id = int(raw_id)
        except ValueError:
            bot.send_message(message.chat.id, "Не удалось прочитать игру.")
            return

        username = call.from_user.username
        full_name = (call.from_user.full_name or "").strip()

        if action == "who":
            game = get_game_basic(db_path, game_id)
            participants = list_game_registrants(db_path, game_id, include_cancelled=False)
            names = [
                f"@{person['username'].lstrip('@')}"
                if person.get("username")
                else person.get("full_name") or f"id:{person['user_id']}"
                for person in participants
            ]
            title = game.get("title") if game else "Игра"
            text = f"На «{title}» идут ({len(names)}):\n" + (
                ", ".join(names) if names else "пока никто не отметился"
            )
            bot.send_message(message.chat.id, text)
            return

        if action == "invite":
            game = get_upcoming_game_with_counts(db_path, call.from_user.id, game_id)
            if not game:
                game = get_game_basic(db_path, game_id)
            if not game:
                bot.send_message(message.chat.id, "Игра не найдена.")
                return
            poll_info = (
                get_poll_by_external_id(db_path, game.get("external_id"))
                if game.get("external_id")
                else None
            )
            if not poll_info or not bot_username:
                bot.send_message(message.chat.id, "Не нашёл ссылку на опрос для этой игры.")
                return
            poll_url = f"https://t.me/{bot_username}?start=forward_{game['external_id']}"
            try:
                if share_app_url:
                    keyboard = types.InlineKeyboardMarkup()
                    keyboard.add(
                        types.InlineKeyboardButton(
                            "💌 Выбрать получателя",
                            web_app=types.WebAppInfo(
                                f"{share_app_url.rstrip('/')}/share?game_id={game_id}"
                            ),
                        )
                    )
                    bot.send_message(
                        call.from_user.id,
                        "Приглашение готово. Нажмите кнопку и выберите, кому отправить:",
                        reply_markup=keyboard,
                    )
                else:
                    bot.send_message(
                        call.from_user.id,
                        build_invite_text(game, poll_url),
                        parse_mode="HTML",
                        disable_web_page_preview=True,
                    )
                if message.chat.id != call.from_user.id:
                    bot.send_message(message.chat.id, "Приглашение отправил вам в личку.")
            except Exception:
                logging.exception("Failed to send invite to user %s", call.from_user.id)
                bot.send_message(
                    message.chat.id,
                    "Не смог отправить приглашение в личку. Сначала напишите мне /start.",
                )
            return

        if action == "calendar":
            game = get_game_basic(db_path, game_id)
            calendar_file = build_calendar_file(game) if game else None
            google_url = build_calendar_url(game) if game else None
            if not game or not calendar_file or not google_url:
                bot.send_message(message.chat.id, "Для этой игры не удалось собрать событие.")
                return
            file_content, file_name = calendar_file
            document = io.BytesIO(file_content)
            document.name = file_name
            keyboard = types.InlineKeyboardMarkup()
            keyboard.add(types.InlineKeyboardButton("Google Календарь", url=google_url))
            try:
                bot.send_document(
                    call.from_user.id,
                    document,
                    caption=(
                        "Выберите календарь:\n"
                        "• Google — кнопка ниже\n"
                        "• Apple / Outlook — откройте приложенный файл .ics"
                    ),
                    reply_markup=keyboard,
                )
                if message.chat.id != call.from_user.id:
                    bot.send_message(message.chat.id, "Календарь отправил вам в личку.")
            except Exception:
                logging.exception("Failed to send calendar to user %s", call.from_user.id)
                bot.send_message(
                    message.chat.id,
                    "Не смог отправить календарь в личку. Сначала напишите мне /start.",
                )
            return

        if action == "map":
            game = get_game_basic(db_path, game_id)
            keyboard = build_location_keyboard(game) if game else None
            if not game or not keyboard:
                bot.send_message(message.chat.id, "У этой игры пока не указано место.")
                return
            location = ", ".join(
                str(value)
                for value in (game.get("restaurant_name"), game.get("restaurant_address"))
                if value
            )
            try:
                bot.send_message(
                    call.from_user.id,
                    f"Куда открыть маршрут?\n{location}",
                    reply_markup=keyboard,
                )
                if message.chat.id != call.from_user.id:
                    bot.send_message(message.chat.id, "Маршрут отправил вам в личку.")
            except Exception:
                logging.exception("Failed to send location to user %s", call.from_user.id)
                bot.send_message(
                    message.chat.id,
                    "Не смог отправить маршрут в личку. Сначала напишите мне /start.",
                )
            return

        if action == "signup":
            ok, text = register_player(db_path, game_id, call.from_user.id, username, full_name)
            if text:
                bot.send_message(message.chat.id, text)
            refreshed = get_upcoming_game_with_counts(db_path, call.from_user.id, game_id)
            if refreshed:
                new_text, new_keyboard = build_game_block(
                    refreshed,
                    show_description=False,
                    bot_username=bot_username or "QPHelperBot",
                    share_app_url=share_app_url or None,
                )
                bot.edit_message_text(
                    new_text,
                    chat_id=message.chat.id,
                    message_id=message.message_id,
                    reply_markup=new_keyboard,
                    parse_mode="MarkdownV2",
                    disable_web_page_preview=True,
                )
            return
        if action == "cancel":
            ok, text = unregister_player(db_path, game_id, call.from_user.id)
            if text:
                bot.send_message(message.chat.id, text)
            refreshed = get_upcoming_game_with_counts(db_path, call.from_user.id, game_id)
            if refreshed:
                new_text, new_keyboard = build_game_block(
                    refreshed,
                    show_description=False,
                    bot_username=bot_username or "QPHelperBot",
                    share_app_url=share_app_url or None,
                )
                bot.edit_message_text(
                    new_text,
                    chat_id=message.chat.id,
                    message_id=message.message_id,
                    reply_markup=new_keyboard,
                    parse_mode="MarkdownV2",
                    disable_web_page_preview=True,
                )
            return
        if action == "waitlist":
            ok, text = add_waitlist(db_path, game_id, call.from_user.id, username, full_name)
            bot.send_message(message.chat.id, text)
            refreshed = get_upcoming_game_with_counts(db_path, call.from_user.id, game_id)
            if refreshed:
                new_text, new_keyboard = build_game_block(
                    refreshed,
                    show_description=False,
                    bot_username=bot_username or "QPHelperBot",
                    share_app_url=share_app_url or None,
                )
                bot.edit_message_text(
                    new_text,
                    chat_id=message.chat.id,
                    message_id=message.message_id,
                    reply_markup=new_keyboard,
                    parse_mode="MarkdownV2",
                    disable_web_page_preview=True,
                )
            return

        bot.send_message(message.chat.id, "Неизвестное действие.")

    @bot.callback_query_handler(func=lambda call: bool(call.data) and call.data.startswith("game_desc:"))
    def game_desc_handler(call):
        store_callback(call)
        message = call.message
        if message is None:
            bot.answer_callback_query(call.id)
            return

        bot.answer_callback_query(call.id)
        try:
            _, action, raw_id = call.data.split(":", 2)
            game_id = int(raw_id)
        except (ValueError, IndexError):
            bot.send_message(message.chat.id, "Не удалось прочитать игру.")
            return

        show_description = action == "show"
        refreshed = get_upcoming_game_with_counts(db_path, call.from_user.id, game_id)
        if not refreshed:
            bot.send_message(message.chat.id, "Игра не найдена.")
            return

        page_index, total_games = _game_page_index(call.from_user.id, game_id)
        new_text, new_keyboard = build_game_block(
            refreshed,
            show_description=show_description,
            page_index=page_index,
            total_games=total_games,
            bot_username=bot_username or "QPHelperBot",
            share_app_url=share_app_url or None,
        )
        bot.edit_message_text(
            new_text,
            chat_id=message.chat.id,
            message_id=message.message_id,
            reply_markup=new_keyboard,
            parse_mode="MarkdownV2",
            disable_web_page_preview=True,
        )

    @bot.callback_query_handler(func=lambda call: bool(call.data) and call.data.startswith("gamelist_page:"))
    def gamelist_page_handler(call):
        store_callback(call)
        message = call.message
        if message is None:
            bot.answer_callback_query(call.id)
            return

        bot.answer_callback_query(call.id)
        try:
            offset = int(call.data.split(":", 1)[1])
        except (ValueError, IndexError):
            bot.send_message(message.chat.id, "Не удалось прочитать страницу.")
            return

        text, keyboard, _, _ = _build_games_page(call.from_user.id, offset)
        if text is None:
            bot.send_message(message.chat.id, "Ближайших игр пока нет.")
            return
        bot.edit_message_text(
            text,
            chat_id=message.chat.id,
            message_id=message.message_id,
            reply_markup=keyboard,
            parse_mode="MarkdownV2",
            disable_web_page_preview=True,
        )

    @bot.callback_query_handler(func=lambda call: call.data == "gamelist_noop")
    def gamelist_noop_handler(call):
        bot.answer_callback_query(call.id, "Листайте игры стрелками")

    @bot.callback_query_handler(func=lambda call: call.data == "schedule:ack")
    def schedule_ack_handler(call):
        store_callback(call)
        message = call.message
        if message is None:
            bot.answer_callback_query(call.id)
            return

        bot.answer_callback_query(call.id)
        refs = schedule_message_refs.get(call.from_user.id)
        if not refs:
            return
        chat_id, user_message_id, bot_message_id = refs
        if message.chat.id != chat_id:
            return
        safe_delete_message(chat_id, bot_message_id)
        safe_delete_message(chat_id, user_message_id)
        schedule_message_refs.pop(call.from_user.id, None)

    @bot.callback_query_handler(func=lambda call: call.data == "help:ack")
    def help_ack_handler(call):
        store_callback(call)
        message = call.message
        if message is None:
            bot.answer_callback_query(call.id)
            return

        bot.answer_callback_query(call.id)
        refs = help_message_refs.get(call.from_user.id)
        if not refs:
            return
        chat_id, user_message_id, bot_message_id = refs
        if message.chat.id != chat_id:
            return
        safe_delete_message(chat_id, bot_message_id)
        safe_delete_message(chat_id, user_message_id)
        help_message_refs.pop(call.from_user.id, None)

    @bot.callback_query_handler(func=lambda call: bool(call.data) and call.data.startswith("poll_register:"))
    def poll_register_handler(call):
        store_callback(call)
        message = call.message
        if message is None:
            bot.answer_callback_query(call.id)
            return

        if getattr(message.chat, "type", None) in {"group", "supergroup"}:
            bot.answer_callback_query(
                call.id,
                "Старая кнопка отключена. Используйте новое уведомление.",
                show_alert=True,
            )
            try:
                bot.edit_message_reply_markup(
                    message.chat.id, message.message_id, reply_markup=None
                )
            except Exception:
                logging.exception("Failed to disable legacy registration buttons")
            return

        if not is_admin(db_path, call.from_user.username):
            bot.answer_callback_query(
                call.id,
                "ноу ноу мистер фиш, только админ может это сделать",
                show_alert=True,
            )
            return
        bot.answer_callback_query(call.id)
        try:
            game_id = int(call.data.split(":", 1)[1])
        except (ValueError, IndexError):
            bot.send_message(message.chat.id, "Не удалось прочитать игру.")
            return

        game_info = get_game_basic(db_path, game_id)
        if not game_info or not game_info.get("external_id"):
            bot.send_message(message.chat.id, "Не удалось найти id игры для регистрации.")
            return
        reg_info = get_game_registration_info_by_external_id(
            db_path, game_info.get("external_id")
        )
        organizer = None
        if reg_info and reg_info.get("organizer_id"):
            organizer = get_organizer_by_id(db_path, reg_info["organizer_id"])
        if not organizer:
            organizer = get_organizer_for_external_id(
                db_path, game_info.get("external_id")
            ) or get_default_organizer(db_path)
        if not organizer:
            bot.send_message(message.chat.id, "Организатор не настроен.")
            return
        registration_id = reg_info.get("registration_external_id") if reg_info else None
        payload = _build_site_registration_payload(
            organizer,
            registration_id,
            reg_info.get("restaurant_name") if reg_info else None,
        )
        ok, text = register_team_in_api(
            game_info.get("external_id"),
            organizer.get("registration_api_template") or "",
            payload,
        )
        record_registration_attempt(db_path, game_id, call.from_user.id, ok, text)
        if not ok:
            bot.send_message(message.chat.id, text)
            return
        text = build_poll_registered_message(db_path, game_id)
        if not text:
            bot.send_message(message.chat.id, "Игра не найдена.")
            return
        bot.edit_message_text(
            text,
            chat_id=message.chat.id,
            message_id=message.message_id,
            reply_markup=None,
            parse_mode="MarkdownV2",
        )

    @bot.callback_query_handler(
        func=lambda call: bool(call.data) and call.data.startswith("poll_register_skip:")
    )
    def poll_register_skip_handler(call):
        store_callback(call)
        message = call.message
        if message is None:
            bot.answer_callback_query(call.id)
            return

        if getattr(message.chat, "type", None) in {"group", "supergroup"}:
            bot.answer_callback_query(
                call.id,
                "Старая кнопка отключена. Используйте новое уведомление.",
                show_alert=True,
            )
            try:
                bot.edit_message_reply_markup(
                    message.chat.id, message.message_id, reply_markup=None
                )
            except Exception:
                logging.exception("Failed to disable legacy registration buttons")
            return

        if not is_admin(db_path, call.from_user.username):
            bot.answer_callback_query(
                call.id,
                "ноу ноу мистер фиш, только админ может это сделать",
                show_alert=True,
            )
            return
        bot.answer_callback_query(call.id)
        try:
            game_id = int(call.data.split(":", 1)[1])
        except (ValueError, IndexError):
            bot.send_message(message.chat.id, "Не удалось прочитать игру.")
            return

        text = build_poll_registered_message(db_path, game_id)
        if not text:
            bot.send_message(message.chat.id, "Игра не найдена.")
            return
        bot.edit_message_text(
            text,
            chat_id=message.chat.id,
            message_id=message.message_id,
            reply_markup=None,
            parse_mode="MarkdownV2",
        )

    @bot.callback_query_handler(func=lambda call: bool(call.data) and call.data.startswith("admin_game:"))
    def admin_game_action_handler(call):
        store_callback(call)
        message = call.message
        if message is None:
            bot.answer_callback_query(call.id)
            return

        if not is_admin(db_path, call.from_user.username):
            bot.answer_callback_query(call.id, "Команда доступна только админам.")
            return

        bot.answer_callback_query(call.id)
        try:
            _, action, raw_id = call.data.split(":", 2)
            game_id = int(raw_id)
        except (ValueError, IndexError):
            bot.send_message(message.chat.id, "Не удалось прочитать игру.")
            return

        if action == "cancel":
            if cancel_game_and_notify(game_id, call.from_user.id):
                updated = get_game_admin(db_path, game_id)
                if updated:
                    new_text, new_keyboard = build_admin_game_block(updated)
                    bot.edit_message_text(
                        new_text,
                        chat_id=message.chat.id,
                        message_id=message.message_id,
                        reply_markup=new_keyboard,
                        parse_mode="MarkdownV2",
                        disable_web_page_preview=True,
                    )
            else:
                bot.send_message(message.chat.id, "Игра не найдена или уже отменена.")
        elif action == "activate":
            if restore_game(db_path, game_id):
                game_info = get_game_basic(db_path, game_id)
                title = game_info["title"] if game_info else "Игра"
                date_text = format_game_datetime(game_info["game_date"]) if game_info else "—"
                notify_game_users(
                    game_id,
                    f"Игра снова доступна для записи: {title} ({date_text}).",
                    include_cancelled=True,
                    exclude_user_id=call.from_user.id,
                )
                bot.send_message(message.chat.id, "Игра снова активна.")
                updated = get_game_admin(db_path, game_id)
                if updated:
                    new_text, new_keyboard = build_admin_game_block(updated)
                    bot.edit_message_text(
                        new_text,
                        chat_id=message.chat.id,
                        message_id=message.message_id,
                        reply_markup=new_keyboard,
                        parse_mode="MarkdownV2",
                        disable_web_page_preview=True,
                    )
            else:
                bot.send_message(message.chat.id, "Игра не найдена или уже активна.")
        else:
            bot.send_message(message.chat.id, "Неизвестное действие.")

    @bot.callback_query_handler(func=lambda call: bool(call.data) and call.data.startswith("stats:"))
    def stats_callback_handler(call):
        store_callback(call)
        bot.answer_callback_query(call.id)
        period = call.data.split(":", 1)[1]
        if period not in {"current", "previous", "all"}:
            return
        stats_period = (
            previous_month_key(datetime.now(ZoneInfo(BOT_TIMEZONE)).date())
            if period == "previous"
            else period
        )
        stats_text = build_personal_stats(
            db_path, call.from_user.id, call.from_user.username, stats_period
        )
        bot.send_message(
            call.message.chat.id,
            stats_text,
            reply_markup=_build_stats_keyboard(stats_text),
        )

    @bot.callback_query_handler(
        func=lambda call: bool(call.data) and call.data.startswith("reactivation:")
    )
    def reactivation_callback_handler(call):
        store_callback(call)
        bot.answer_callback_query(call.id)
        action = call.data.split(":", 1)[1]
        if action == "games":
            send_games_list_blocks(call.message, call.from_user.id)
        elif action == "disable":
            set_reactivation_preference(db_path, call.from_user.id, False)
            bot.send_message(call.message.chat.id, "Хорошо, больше не буду присылать такие напоминания.")
        elif action == "later":
            bot.send_message(call.message.chat.id, "Окей, увидимся в беседе 🙂")
        try:
            bot.edit_message_reply_markup(
                call.message.chat.id, call.message.message_id, reply_markup=None
            )
        except Exception:
            pass

    @bot.callback_query_handler(
        func=lambda call: bool(call.data) and call.data.startswith("admin_retry_")
    )
    def admin_retry_callback_handler(call):
        store_callback(call)
        if not is_admin(db_path, call.from_user.username):
            bot.answer_callback_query(call.id, "Только для админов", show_alert=True)
            return
        try:
            action, raw_game_id = call.data.split(":", 1)
            game_id = int(raw_game_id)
        except (ValueError, IndexError):
            bot.answer_callback_query(call.id, "Не удалось прочитать игру", show_alert=True)
            return
        bot.answer_callback_query(call.id)
        if action == "admin_retry_cancel":
            bot.edit_message_reply_markup(
                call.message.chat.id, call.message.message_id, reply_markup=None
            )
            return
        if action == "admin_retry_confirm":
            game = get_game_basic(db_path, game_id)
            keyboard = types.InlineKeyboardMarkup(row_width=2)
            keyboard.add(
                types.InlineKeyboardButton(
                    "Да, отправить на сайт",
                    callback_data=f"admin_retry_execute:{game_id}",
                ),
                types.InlineKeyboardButton(
                    "Отмена", callback_data=f"admin_retry_cancel:{game_id}"
                ),
            )
            bot.send_message(
                call.message.chat.id,
                f"Отправить настоящую регистрацию на сайт для «{game['title'] if game else game_id}»?",
                reply_markup=keyboard,
            )
            return
        if action == "admin_retry_execute":
            _, text = _register_game_on_site(game_id, call.from_user.id)
            bot.send_message(call.message.chat.id, text)
            bot.edit_message_reply_markup(
                call.message.chat.id, call.message.message_id, reply_markup=None
            )

    @bot.callback_query_handler(func=lambda call: bool(call.data) and call.data.startswith("admin:"))
    def admin_tools_callback_handler(call):
        store_callback(call)
        if not is_admin(db_path, call.from_user.username):
            bot.answer_callback_query(call.id, "Только для админов", show_alert=True)
            return
        bot.answer_callback_query(call.id)
        action = call.data.split(":", 1)[1]
        if action == "reconcile":
            bot.send_message(
                call.message.chat.id,
                _format_reconciliation(reconcile_poll_registrations(db_path, True)),
            )
        elif action == "monthstats":
            stats_text = format_monthly_card(build_monthly_stats(db_path))
            bot.send_message(
                call.message.chat.id,
                stats_text,
                reply_markup=build_share_keyboard(
                    stats_text,
                    f"https://t.me/{bot_username}" if bot_username else "https://t.me",
                    "Поделиться итогами",
                ),
            )
        elif action == "registration_status":
            bot.send_message(
                call.message.chat.id,
                _format_admin_status(),
                reply_markup=_build_registration_status_keyboard(),
            )
        elif action == "reactivation":
            enabled = get_setting(db_path, "reactivation_enabled", "1") == "1"
            set_setting(
                db_path,
                "reactivation_enabled",
                "0" if enabled else "1",
                call.from_user.id,
            )
            bot.send_message(
                call.message.chat.id,
                "Личные мягкие напоминания теперь " + ("выключены." if enabled else "включены."),
            )
        elif action == "results":
            results = list_recent_results(db_path)
            if not results:
                text = (
                    "Результатов пока нет. После игры бот спросит их в беседе в 23:00. "
                    "Любой участник может ответить реплаем, повторный ответ исправит запись."
                )
            else:
                lines = ["Последние результаты:"]
                for result in results:
                    lines.append(
                        f"#{result['game_id']} {result['title']}: "
                        f"{result['position']} из {result['total_teams']}; "
                        f"лотерея — {result['lottery_text']}; "
                        f"Подлутки — {result['podlutki_text']} "
                        f"(версий: {result['revisions']})"
                    )
                lines.append(
                    "Исправление: ответьте заново реплаем на исходный запрос результатов в беседе."
                )
                text = "\n".join(lines)
            bot.send_message(call.message.chat.id, text)
        elif action == "settings":
            settings = {
                "registration_comment": get_setting(
                    db_path, "registration_comment", TEAM_REGISTRATION_COMMENT
                ),
                "default_team_name": get_setting(
                    db_path, "default_team_name", TEAM_NAME
                ),
                "amra_team_name": get_setting(
                    db_path, "amra_team_name", AMRA_TEAM_NAME
                ),
                "quizplease_promo_id": get_setting(
                    db_path, "quizplease_promo_id", str(QUIZPLEASE_PROMO_ID)
                ),
                "quizplease_promo_end_date": get_setting(
                    db_path, "quizplease_promo_end_date", QUIZPLEASE_PROMO_END_DATE
                ),
            }
            text = (
                "Текущие настройки регистрации:\n"
                f"Команда: {settings['default_team_name']}\n"
                f"AMRA: {settings['amra_team_name']}\n"
                f"Комментарий: {settings['registration_comment']}\n"
                f"Промо id: {settings['quizplease_promo_id']}\n"
                f"Промо до: {settings['quizplease_promo_end_date']}\n\n"
                "Изменить значение: /setting <ключ> <новое значение>"
            )
            bot.send_message(call.message.chat.id, text)

    @bot.callback_query_handler(func=lambda call: bool(call.data) and call.data.startswith("menu:"))
    def menu_callback_handler(call):
        action = call.data.split(":", 1)[1]
        message = call.message
        if message is None:
            bot.answer_callback_query(call.id)
            return

        store_callback(call)
        bot.answer_callback_query(call.id)

        if action in {"addgame", "delgame", "adminrequests", "manage"}:
            if not is_admin(db_path, call.from_user.username):
                clear_pending_for_user(call.from_user.id)
                bot.send_message(message.chat.id, "Команда доступна только админам.")
                return

        if action == "mygames":
            clear_pending_for_user(call.from_user.id)
            send_user_games(message, call.from_user.id)
            return
        if action == "gamelist":
            clear_pending_for_user(call.from_user.id)
            text, keyboard, _, _ = _build_games_page(call.from_user.id, 0)
            if text is None:
                bot.edit_message_text(
                    "Ближайших игр пока нет.",
                    chat_id=message.chat.id,
                    message_id=message.message_id,
                    reply_markup=build_main_menu(
                        is_admin(db_path, call.from_user.username),
                        share_app_url or None,
                    ),
                )
            else:
                bot.edit_message_text(
                    text,
                    chat_id=message.chat.id,
                    message_id=message.message_id,
                    reply_markup=keyboard,
                    parse_mode="MarkdownV2",
                    disable_web_page_preview=True,
                )
                list_message_refs[call.from_user.id] = (
                    message.chat.id,
                    message.message_id,
                )
            refs = poll_message_refs.pop(call.from_user.id, None)
            if refs:
                chat_id, prompt_id, poll_id = refs
                safe_delete_message(chat_id, prompt_id)
                safe_delete_message(chat_id, poll_id)
            return
        if action == "manage":
            clear_pending_for_user(call.from_user.id)
            send_manage_menu(message)
            safe_delete_message(message.chat.id, message.message_id)
            return
        if action == "manage_back":
            clear_pending_for_user(call.from_user.id)
            edit_main_menu(
                message,
                call.from_user.id,
                is_admin(db_path, call.from_user.username),
            )
            list_message_refs.pop(call.from_user.id, None)
            return
        if action == "unsubscribe":
            clear_pending_for_user(call.from_user.id)
            set_pending(message, "unsubscribe", "Введите id игры для отписки.", user_id=call.from_user.id)
            return
        if action == "wantadmin":
            clear_pending_for_user(call.from_user.id)
            username = call.from_user.username
            full_name = (call.from_user.full_name or "").strip()
            ok, text = request_admin(db_path, call.from_user.id, username, full_name)
            bot.send_message(message.chat.id, text)
            return
        if action == "adminrequests":
            clear_pending_for_user(call.from_user.id)
            send_admin_requests(message)
            safe_delete_message(message.chat.id, message.message_id)
            return
        if action == "addgame":
            clear_pending_for_user(call.from_user.id)
            bot.send_message(
                message.chat.id,
                'Добавление игр доступно через команду "@qphelperbot сделай опрос <ссылка>" в общем чате.',
            )
            safe_delete_message(message.chat.id, message.message_id)
            return
        if action == "importgame":
            clear_pending_for_user(call.from_user.id)
            prompt_message = set_pending(
                message,
                "importgame",
                "Введите id игры из API.",
                user_id=call.from_user.id,
            )
            store_prompt_message(call.from_user.id, prompt_message)
            safe_delete_message(message.chat.id, message.message_id)
            return
        if action == "delgame":
            clear_pending_for_user(call.from_user.id)
            send_admin_games_list(message)
            safe_delete_message(message.chat.id, message.message_id)
            return

    @bot.message_handler(
        func=lambda msg: (
            bool(msg.text)
            and not msg.text.startswith("/")
            and msg.from_user
            and msg.from_user.id in pending_actions
            and msg.text not in menu_labels
        )
    )
    def pending_input_handler(message):
        store_message(message)
        action = pending_actions.get(message.from_user.id)
        if not action:
            return

        if action in {"addgame", "delgame", "approveadmin", "denyadmin", "importgame"}:
            if not is_admin(db_path, message.from_user.username):
                clear_pending(message)
                bot.send_message(message.chat.id, "Команда доступна только админам.")
                return

        text = message.text.strip()
        if action == "addgame":
            clear_pending(message)
            bot.send_message(
                message.chat.id,
                'Добавление игр доступно через команду "@qphelperbot сделай опрос <ссылка>" в общем чате.',
            )
            return
        if action == "importgame":
            import_game_from_api(message, text)
            clear_prompt_message(message.from_user.id)
            clear_pending(message)
            return

        if not text.isdigit():
            bot.send_message(message.chat.id, "Нужен числовой id.")
            return

        value = int(text)

        if action == "unsubscribe":
            ok, reply_text = unregister_player(db_path, value, message.from_user.id)
            if ok:
                clear_pending(message)
            if reply_text:
                bot.send_message(message.chat.id, reply_text)
            return

        if action == "delgame":
            recipients = list_game_registrants(db_path, value, include_cancelled=False)
            ok = archive_game(db_path, value)
            if ok:
                clear_pending(message)
                game_info = get_game_basic(db_path, value)
                title = game_info["title"] if game_info else "Игра"
                date_text = format_game_datetime(game_info["game_date"]) if game_info else "—"
                notify_game_users(
                    value,
                    f"Игра отменена: {title} ({date_text}).",
                    include_cancelled=False,
                    exclude_user_id=message.from_user.id,
                    recipients=recipients,
                )
            else:
                bot.send_message(message.chat.id, "Игра не найдена или уже отменена.")
            return

        if action == "approveadmin":
            approved_by = message.from_user.username or get_full_name(message.from_user) or "unknown"
            ok, reply_text = approve_admin_request(db_path, value, approved_by)
            if ok:
                clear_pending(message)
            bot.send_message(message.chat.id, reply_text)
            return

        if action == "denyadmin":
            ok, reply_text = deny_admin_request(db_path, value)
            if ok:
                clear_pending(message)
            bot.send_message(message.chat.id, reply_text)
            return

    @bot.message_handler(content_types=["text"])
    def text_logger(message):
        if message.text and message.text.startswith("/"):
            return
        store_message(message)

    @bot.message_handler(
        content_types=[
            "audio",
            "document",
            "photo",
            "sticker",
            "video",
            "video_note",
            "voice",
            "location",
            "contact",
        ]
    )
    def media_logger(message):
        store_message(message)
