from __future__ import annotations

import re
from html import unescape
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo

from telebot import types

from helpers import escape_markdown, format_game_datetime, get_participation_status


def build_main_menu(
    is_admin_user: bool, app_url: str | None = None
) -> types.InlineKeyboardMarkup:
    keyboard = types.InlineKeyboardMarkup(row_width=2)
    if app_url:
        keyboard.row(
            types.InlineKeyboardButton(
                "📱 Открыть приложение",
                web_app=types.WebAppInfo(f"{app_url.rstrip('/')}/app"),
            )
        )
    if is_admin_user:
        keyboard.add(
            types.InlineKeyboardButton("🎮 Про игры", callback_data="menu:gamelist"),
            types.InlineKeyboardButton("✅ Мои игры", callback_data="menu:mygames"),
        )
        keyboard.add(
            types.InlineKeyboardButton("📊 Моя статистика", callback_data="stats:current"),
            types.InlineKeyboardButton("⚙️ Управление", callback_data="menu:manage"),
        )
    else:
        keyboard.add(
            types.InlineKeyboardButton("🎮 Про игры", callback_data="menu:gamelist"),
            types.InlineKeyboardButton("✅ Мои игры", callback_data="menu:mygames"),
        )
        keyboard.add(
            types.InlineKeyboardButton("📊 Моя статистика", callback_data="stats:current")
        )
        keyboard.add(
            types.InlineKeyboardButton("🛡 Стать админом", callback_data="menu:wantadmin")
        )
    return keyboard


def build_reply_keyboard() -> types.ReplyKeyboardMarkup:
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=1)
    keyboard.add("Главное меню")
    return keyboard


def build_game_block(
    game: dict,
    show_description: bool,
    page_index: int | None = None,
    total_games: int | None = None,
    bot_username: str = "QPHelperBot",
    share_app_url: str | None = None,
) -> tuple[str, types.InlineKeyboardMarkup]:
    if game.get("is_registered"):
        status = "✅ Ты записан"
    elif game.get("is_waitlisted"):
        status = "⏳ Ты в резерве"
    else:
        status = "✖️ Ты не записан"

    date_text = escape_markdown(format_game_datetime(game.get("game_date")))
    title = escape_markdown(game.get("title") or "—")

    restaurant_name_raw = game.get("restaurant_name")
    restaurant_name = escape_markdown(restaurant_name_raw) if restaurant_name_raw else None
    restaurant_address_raw = game.get("restaurant_address")
    restaurant_address = (
        escape_markdown(restaurant_address_raw) if restaurant_address_raw else None
    )
    description_raw = game.get("description")
    description = escape_markdown(description_raw) if description_raw else None

    restaurant_line = None
    lat = game.get("restaurant_lat")
    lon = game.get("restaurant_lon")
    if restaurant_name and restaurant_address:
        map_query = ", ".join(
            part for part in (restaurant_name_raw, restaurant_address_raw) if part
        )
        map_url = "https://yandex.ru/maps/?" + urlencode({"text": map_query})
        restaurant_line = f"{restaurant_name}, [{restaurant_address}]({map_url})"
    elif restaurant_name:
        restaurant_line = restaurant_name
    elif restaurant_address:
        restaurant_line = restaurant_address

    lines = [
        f"*{title}*",
        "",
        status,
        date_text,
    ]
    registrations = int(game.get("registrations") or 0)
    capacity = int(game.get("capacity") or 9)
    free_spots = max(capacity - registrations, 0)
    waitlist_count = int(game.get("waitlist_count") or 0)
    occupancy = f"Места: {registrations}/{capacity}, свободно {free_spots}"
    if waitlist_count:
        occupancy += f", резерв {waitlist_count}"
    lines.append(escape_markdown(occupancy))
    if restaurant_line:
        lines.append(restaurant_line)
    if show_description and description:
        quote_lines = [f"> {line}" for line in description.splitlines() if line]
        lines.append("")
        lines.extend(quote_lines)
    text = "\n".join(lines)

    keyboard = types.InlineKeyboardMarkup(row_width=2)
    external_id = game.get("external_id") or "none"
    keyboard.row(
        types.InlineKeyboardButton("🗳 Опрос", callback_data=f"game_action:poll:{external_id}"),
        types.InlineKeyboardButton("👥 Кто идёт", callback_data=f"game_action:who:{game['id']}"),
    )
    if external_id != "none" and bot_username:
        if share_app_url:
            invite_button = types.InlineKeyboardButton(
                "💌 Позвать друга",
                web_app=types.WebAppInfo(
                    f"{share_app_url.rstrip('/')}/share?game_id={game['id']}"
                ),
            )
        else:
            poll_url = f"https://t.me/{bot_username.lstrip('@')}?start=forward_{external_id}"
            invite_button = types.InlineKeyboardButton(
                "💌 Позвать друга", url=build_invite_share_url(game, poll_url)
            )
        keyboard.row(invite_button)
    if game.get("game_date"):
        keyboard.row(
            types.InlineKeyboardButton(
                "📅 Добавить в календарь", callback_data=f"game_action:calendar:{game['id']}"
            )
        )
    if game.get("restaurant_name") or game.get("restaurant_address"):
        keyboard.row(
            types.InlineKeyboardButton(
                "📍 Место и маршрут", callback_data=f"game_action:map:{game['id']}"
            )
        )
    if description:
        label = "🙈 Скрыть описание" if show_description else "ℹ️ Подробнее"
        keyboard.row(
            types.InlineKeyboardButton(
                label,
                callback_data=f"game_desc:{'hide' if show_description else 'show'}:{game['id']}",
            )
        )
    if page_index is not None and total_games:
        navigation = []
        if page_index > 0:
            navigation.append(
                types.InlineKeyboardButton("←", callback_data=f"gamelist_page:{page_index - 1}")
            )
        navigation.append(
            types.InlineKeyboardButton(
                f"{page_index + 1} из {total_games}", callback_data="gamelist_noop"
            )
        )
        if page_index + 1 < total_games:
            navigation.append(
                types.InlineKeyboardButton("→", callback_data=f"gamelist_page:{page_index + 1}")
            )
        keyboard.row(*navigation)
        keyboard.row(types.InlineKeyboardButton("🏠 В главное меню", callback_data="menu:manage_back"))
    return text, keyboard


def build_location_keyboard(game: dict) -> types.InlineKeyboardMarkup | None:
    name = str(game.get("restaurant_name") or "").strip()
    address = str(game.get("restaurant_address") or "").strip()
    query = ", ".join(part for part in (name, address) if part)
    if not query:
        return None

    keyboard = types.InlineKeyboardMarkup(row_width=1)
    keyboard.add(
        types.InlineKeyboardButton(
            "Яндекс Карты", url="https://yandex.ru/maps/?" + urlencode({"text": query})
        ),
        types.InlineKeyboardButton(
            "2ГИС", url="https://2gis.ru/spb/search/" + quote(query, safe="")
        ),
    )
    apple_params = {"q": name or query, "address": address or query}
    lat = game.get("restaurant_lat")
    lon = game.get("restaurant_lon")
    if lat is not None and lon is not None:
        apple_params["ll"] = f"{lat},{lon}"
    keyboard.add(
        types.InlineKeyboardButton(
            "Apple Карты", url="https://maps.apple.com/?" + urlencode(apple_params)
        )
    )
    return keyboard


def build_calendar_url(game: dict) -> str | None:
    raw_date = game.get("game_date")
    if not raw_date:
        return None
    parsed = None
    for date_format in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(str(raw_date), date_format)
            break
        except ValueError:
            continue
    if parsed is None:
        return None
    local_timezone = ZoneInfo("Europe/Moscow")
    start_utc = parsed.replace(tzinfo=local_timezone).astimezone(timezone.utc)
    end_utc = (parsed + timedelta(hours=3)).replace(tzinfo=local_timezone).astimezone(
        timezone.utc
    )
    location = ", ".join(
        value
        for value in (
            game.get("restaurant_name"),
            game.get("restaurant_address"),
        )
        if value
    )
    params = {
        "action": "TEMPLATE",
        "text": game.get("title") or "Квиз",
        "dates": f"{start_utc.strftime('%Y%m%dT%H%M%SZ')}/{end_utc.strftime('%Y%m%dT%H%M%SZ')}",
        "stz": "Europe/Moscow",
        "etz": "Europe/Moscow",
    }
    if location:
        params["location"] = location
    return "https://calendar.google.com/calendar/r/eventedit?" + urlencode(params)


def build_calendar_file(game: dict) -> tuple[bytes, str] | None:
    raw_date = game.get("game_date")
    if not raw_date:
        return None
    parsed = None
    for date_format in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(str(raw_date), date_format)
            break
        except ValueError:
            continue
    if parsed is None:
        return None

    def ics_escape(value: object) -> str:
        return (
            str(value or "")
            .replace("\\", "\\\\")
            .replace("\n", "\\n")
            .replace(",", "\\,")
            .replace(";", "\\;")
        )

    end = parsed + timedelta(hours=3)
    location = ", ".join(
        str(value) for value in (game.get("restaurant_name"), game.get("restaurant_address")) if value
    )
    game_id = game.get("id") or game.get("external_id") or "quiz"
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//QPHelperBot//Quiz Game//RU",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "BEGIN:VEVENT",
        f"UID:quiz-game-{game_id}@qphelperbot",
        f"DTSTAMP:{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
        f"DTSTART;TZID=Europe/Moscow:{parsed.strftime('%Y%m%dT%H%M%S')}",
        f"DTEND;TZID=Europe/Moscow:{end.strftime('%Y%m%dT%H%M%S')}",
        f"SUMMARY:{ics_escape(game.get('title') or 'Квиз')}",
        f"LOCATION:{ics_escape(location)}",
        "DESCRIPTION:Квиз с командой",
        "END:VEVENT",
        "END:VCALENDAR",
        "",
    ]
    return "\r\n".join(lines).encode("utf-8"), f"quiz-{game_id}.ics"


def clean_game_title(title: str | None) -> str:
    value = re.sub(r"\s+SPB\s*$", "", str(title or "Игра"), flags=re.IGNORECASE).strip()
    bracketed = re.search(r"\s*\[([^]]+)]\s*$", value)
    if bracketed:
        prefix = value[: bracketed.start()].strip()
        suffix = bracketed.group(1).strip()
        value = f"{prefix} — {suffix}" if prefix else suffix
    if value:
        value = value[0].upper() + value[1:]
    return re.sub(r"\s+", " ", value).strip()


def build_invite_text(
    game: dict,
    poll_url: str,
    link_label: str = "Открыть опрос и отметиться",
) -> str:
    from html import escape

    date_text = format_game_datetime(game.get("game_date")) or "время уточняется"
    location = ", ".join(
        str(value) for value in (game.get("restaurant_name"), game.get("restaurant_address")) if value
    ) or "место уточняется"
    registrations = int(game.get("registrations") or 0)
    capacity = int(game.get("capacity") or 9)
    free_spots = max(capacity - registrations, 0)
    if free_spots == 1:
        occupancy = f"Сейчас нас {registrations} из {capacity} — осталось одно место"
    elif free_spots > 1:
        occupancy = f"Сейчас нас {registrations} из {capacity} — свободно мест: {free_spots}"
    else:
        occupancy = f"Сейчас нас {registrations} из {capacity} — можно отметиться в резерв"
    return (
        "Привет! Го с нами на квиз?\n\n"
        f"<b>{escape(clean_game_title(game.get('title')))}</b>\n\n"
        f"🗓 {escape(date_text)}\n"
        f"📍 {escape(location)}\n"
        f"👥 {escape(occupancy)}\n\n"
        f'👉 <a href="{escape(poll_url, quote=True)}">{escape(link_label)}</a>'
    )


def build_invite_share_url(game: dict, poll_url: str) -> str:
    plain_text = unescape(re.sub(r"<[^>]+>", "", build_invite_text(game, poll_url)))
    return (
        "tg://msg_url?url="
        + quote(poll_url, safe="")
        + "&text="
        + quote(plain_text, safe="")
    )


def build_group_game_keyboard(
    game: dict,
    chat_id: int,
    poll_message_id: int,
    bot_username: str = "QPHelperBot",
) -> types.InlineKeyboardMarkup:
    keyboard = types.InlineKeyboardMarkup(row_width=2)
    buttons = []
    chat_text = str(chat_id)
    if chat_text.startswith("-100"):
        poll_url = f"https://t.me/c/{chat_text[4:]}/{poll_message_id}"
        buttons.append(types.InlineKeyboardButton("🗳 Открыть опрос", url=poll_url))
    app_url = (
        f"https://t.me/{bot_username.lstrip('@')}?startapp=game_{int(game['id'])}"
    )
    buttons.append(types.InlineKeyboardButton("📱 Открыть игру", url=app_url))
    keyboard.row(*buttons)
    return keyboard


def build_share_keyboard(
    text: str,
    share_url: str,
    label: str = "Поделиться",
) -> types.InlineKeyboardMarkup:
    keyboard = types.InlineKeyboardMarkup()
    keyboard.add(
        types.InlineKeyboardButton(
            label,
            url="tg://msg_url?" + urlencode({"url": share_url, "text": text}),
        )
    )
    return keyboard


def build_admin_game_block(game: dict) -> tuple[str, types.InlineKeyboardMarkup]:
    title = escape_markdown(game.get("title") or "—")
    date_text = escape_markdown(format_game_datetime(game.get("game_date")))
    restaurant_name = escape_markdown(game.get("restaurant_name") or "")
    restaurant_address = escape_markdown(game.get("restaurant_address") or "")
    restaurant_line = ", ".join(part for part in [restaurant_name, restaurant_address] if part)
    if not restaurant_line:
        restaurant_line = "—"

    status = "Отменена" if game.get("is_archived") else "Активна"
    text = (
        f"*{title}*\n"
        f"{date_text}\n"
        f"{restaurant_line}\n"
        f"Статус: {escape_markdown(status)}"
    )

    keyboard = types.InlineKeyboardMarkup(row_width=2)
    buttons = []
    if game.get("is_archived"):
        buttons.append(
            types.InlineKeyboardButton("Возобновить", callback_data=f"admin_game:activate:{game['id']}")
        )
    else:
        buttons.append(
            types.InlineKeyboardButton("Отменить", callback_data=f"admin_game:cancel:{game['id']}")
        )
    keyboard.row(*buttons)
    return text, keyboard


def build_manage_menu() -> types.InlineKeyboardMarkup:
    keyboard = types.InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        types.InlineKeyboardButton("Удалить игру", callback_data="menu:delgame"),
        types.InlineKeyboardButton("Заявки на админство", callback_data="menu:adminrequests"),
    )
    keyboard.add(
        types.InlineKeyboardButton("Сверить опрос и базу", callback_data="admin:reconcile"),
        types.InlineKeyboardButton("Итоги месяца", callback_data="admin:monthstats"),
    )
    keyboard.add(
        types.InlineKeyboardButton("Статус регистрации", callback_data="admin:registration_status"),
        types.InlineKeyboardButton("Реактивация вкл/выкл", callback_data="admin:reactivation"),
    )
    keyboard.add(
        types.InlineKeyboardButton("Результаты игр", callback_data="admin:results"),
        types.InlineKeyboardButton("Настройки", callback_data="admin:settings"),
    )
    keyboard.row(
        types.InlineKeyboardButton("Назад", callback_data="menu:manage_back"),
    )
    return keyboard


def build_games_list_message(
    games: list[dict], bot_username: str | None, start_index: int = 1
) -> tuple[str, types.InlineKeyboardMarkup]:
    lines = ["Предстоящие игры:"]
    keyboard = types.InlineKeyboardMarkup(row_width=2)
    bot_username = (bot_username or "").lstrip("@")
    for index, game in enumerate(games, start=start_index):
        title = escape_markdown(game.get("title") or "—")
        date_value = format_game_datetime(game.get("game_date"))
        date_text = f" \\({escape_markdown(date_value)}\\)" if date_value else ""
        status_icon, status_text = get_participation_status(game)
        lines.append(f"{index}\\. *{title}*{date_text}")
        lines.append(f"{status_icon} {status_text}")
        registrations = int(game.get("registrations") or 0)
        capacity = int(game.get("capacity") or 9)
        free_spots = max(capacity - registrations, 0)
        lines.append(
            escape_markdown(f"Места: {registrations}/{capacity}, свободно {free_spots}")
        )

        restaurant_name_raw = game.get("restaurant_name")
        restaurant_name = escape_markdown(restaurant_name_raw) if restaurant_name_raw else None
        restaurant_address_raw = game.get("restaurant_address")
        restaurant_address = (
            escape_markdown(restaurant_address_raw) if restaurant_address_raw else None
        )
        lat = game.get("restaurant_lat")
        lon = game.get("restaurant_lon")
        if restaurant_name or restaurant_address:
            if restaurant_name and restaurant_address:
                if lat is not None and lon is not None:
                    map_url = f"https://yandex.ru/maps/?pt={lon},{lat}&z=18&l=map"
                    address_part = f"[{restaurant_address}]({map_url})"
                else:
                    address_part = restaurant_address
                lines.append(f"{restaurant_name}, {address_part}")
            elif restaurant_name:
                lines.append(restaurant_name)
            else:
                lines.append(restaurant_address or "—")
        else:
            lines.append("—")

        external_id = game.get("external_id")
        actions = []
        if bot_username and external_id:
            poll_url = f"https://t.me/{bot_username}?start=forward_{external_id}"
            actions.append(f"[Опрос]({poll_url})")
        else:
            actions.append("Опрос недоступен")
        game_url_template = game.get("game_url_template")
        if external_id and game_url_template:
            details_url = game_url_template.format(external_id=external_id, game_id=external_id)
            actions.append(f"[Подробнее]({details_url})")
        description_raw = game.get("description")
        description = escape_markdown(description_raw) if description_raw else None
        if description:
            quote_lines = [f"> {line}" for line in description.splitlines() if line]
            lines.extend(quote_lines)

        lines.append(" • ".join(actions))
        lines.append("")
        lines.append("")

        external_id = game.get("external_id") or "none"
        keyboard.row(
            types.InlineKeyboardButton(
                f"{index}. Опрос",
                callback_data=f"game_action:poll:{external_id}",
            ),
            types.InlineKeyboardButton(
                "Кто идёт", callback_data=f"game_action:who:{game['id']}"
            ),
        )
        keyboard.row(
            types.InlineKeyboardButton(
                "Позвать друга", callback_data=f"game_action:invite:{game['id']}"
            )
        )
        if game.get("game_date"):
            keyboard.row(
                types.InlineKeyboardButton(
                    "Добавить в календарь",
                    callback_data=f"game_action:calendar:{game['id']}",
                )
            )
        if game.get("restaurant_name") or game.get("restaurant_address"):
            keyboard.row(
                types.InlineKeyboardButton(
                    "Место и маршрут", callback_data=f"game_action:map:{game['id']}"
                )
            )

    keyboard.add(types.InlineKeyboardButton("В главное меню", callback_data="menu:manage_back"))
    return "\n".join(lines).strip(), keyboard


def build_admin_cancel_list_message(
    games: list[dict], bot_username: str | None, start_index: int = 1
) -> tuple[str, types.InlineKeyboardMarkup]:
    lines = ["Игры для отмены:"]
    bot_username = (bot_username or "").lstrip("@")
    for index, game in enumerate(games, start=start_index):
        title = escape_markdown(game.get("title") or "—")
        date_value = format_game_datetime(game.get("game_date"))
        date_text = f" \\({escape_markdown(date_value)}\\)" if date_value else ""
        lines.append(f"{index}\\. *{title}*{date_text}")

        cancel_text = "отменить"
        if bot_username:
            cancel_url = f"https://t.me/{bot_username}?start=cancel_{game['id']}"
            lines.append(f"[{cancel_text}]({cancel_url})")
        else:
            lines.append(cancel_text)
        lines.append("")

    keyboard = types.InlineKeyboardMarkup(row_width=1)
    keyboard.add(types.InlineKeyboardButton("Назад", callback_data="menu:manage"))
    return "\n".join(lines).strip(), keyboard
