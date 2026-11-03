from __future__ import annotations

import html
import logging
import re
from html.parser import HTMLParser
from datetime import datetime

from poll_answers import pick_default_answers
from game_registration import (
    get_game_basic,
    list_game_registrants,
    list_upcoming_games,
    list_upcoming_games_with_counts,
    list_upcoming_games_with_counts_limit,
    count_upcoming_games,
)


def get_full_name(user) -> str:
    if user is None:
        return ""
    full_name = getattr(user, "full_name", None)
    if full_name:
        return full_name
    parts = [getattr(user, "first_name", None), getattr(user, "last_name", None)]
    return " ".join(part for part in parts if part).strip()


def normalize_game_datetime(raw_value: str | None) -> str | None:
    if not raw_value:
        return None
    value = raw_value.strip()
    if not value:
        return None
    parts = value.split()
    date_part = parts[0]
    time_part = parts[1] if len(parts) > 1 else None

    parsed_date = None
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            parsed_date = datetime.strptime(date_part, fmt).strftime("%Y-%m-%d")
            break
        except ValueError:
            continue
    if not parsed_date:
        return None

    if time_part:
        try:
            parsed_time = datetime.strptime(time_part, "%H:%M").strftime("%H:%M")
        except ValueError:
            return None
        return f"{parsed_date} {parsed_time}"
    return parsed_date


def parse_api_date(raw_value: str | None) -> str | None:
    if not raw_value:
        return None
    value = raw_value.strip()
    for fmt in ("%d.%m.%Y %H:%M", "%d.%m.%Y"):
        try:
            parsed = datetime.strptime(value, fmt)
            if fmt == "%d.%m.%Y":
                return parsed.strftime("%Y-%m-%d")
            return parsed.strftime("%Y-%m-%d %H:%M")
        except ValueError:
            continue
    return normalize_game_datetime(value)


def format_game_datetime(value: str | None) -> str:
    if not value:
        return "—"
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(value, fmt)
            if fmt == "%Y-%m-%d":
                return parsed.strftime("%d.%m.%Y")
            return parsed.strftime("%d.%m.%Y %H:%M")
        except ValueError:
            continue
    return value


def escape_markdown(text: str) -> str:
    if not text:
        return ""
    escaped = html.escape(text)
    for symbol in ("_", "*", "[", "]", "(", ")", "~", "`", ">", "#", "+", "-", "=", "|", "{", "}", ".", "!"):
        escaped = escaped.replace(symbol, f"\\{symbol}")
    return escaped


def is_skip_value(text: str) -> bool:
    return text.strip().lower() in {"-", "пропустить", "skip"}


def parse_addgame_payload(payload: str) -> tuple[str | None, str | None, str | None]:
    title_part = payload.strip()
    if not title_part:
        return None, None, "Название игры не может быть пустым."

    if "|" in title_part:
        title, game_datetime = [item.strip() for item in title_part.split("|", 1)]
        game_datetime = normalize_game_datetime(game_datetime or None)
        if game_datetime is None:
            return (
                None,
                None,
                "Дата должна быть в формате YYYY-MM-DD [HH:MM] или DD.MM.YYYY [HH:MM].",
            )
    else:
        title = title_part
        game_datetime = None

    if not title:
        return None, None, "Название игры не может быть пустым."

    return title, game_datetime, None


def extract_game_id(raw_value: str) -> str | None:
    value = raw_value.strip()
    if not value:
        return None

    uuid_pattern = (
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
        r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
    )

    parsed = parse_url(raw_value)
    if parsed is not None:
        match = re.search(uuid_pattern, parsed)
        if match:
            return match.group(0)
        return None

    match = re.search(uuid_pattern, value)
    if match:
        return match.group(0)
    return None


def extract_external_id_from_link(link: str, game_url_template: str | None) -> str | None:
    if not link:
        return None
    cleaned = link.strip().strip(" ,.;:)")
    if game_url_template:
        prefix = game_url_template.split("{", 1)[0].rstrip("/")
        if prefix:
            target = cleaned
            if not target.startswith(prefix) and prefix in target:
                target = target[target.index(prefix) :]
            if not target.startswith(prefix):
                return extract_game_id(cleaned)
            remainder = target[len(prefix) :].lstrip("/")
            remainder = remainder.split("?", 1)[0].split("#", 1)[0]
            remainder = remainder.strip().strip(" ,.;:)")
            return remainder or None
    return extract_game_id(cleaned)


def parse_url(raw_value: str) -> str | None:
    try:
        from urllib.parse import urlparse
    except ImportError:
        return None
    parsed = urlparse(raw_value)
    if parsed.scheme and parsed.netloc:
        return parsed.path
    return None


def _parse_quizplease_v1(data: dict | str) -> tuple[dict | None, str | None]:
    if not isinstance(data, dict):
        return None, "Некорректный ответ API."
    title = data.get("title")
    date_raw = data.get("date")
    game_datetime = parse_api_date(date_raw)
    if not title or not game_datetime:
        return None, "В API нет обязательных полей title/date."

    place = data.get("place") or {}
    description = data.get("description")
    restaurant_name = place.get("title")
    restaurant_address = place.get("address") or place.get("address_ru")
    restaurant_lat = place.get("lat")
    restaurant_lon = place.get("lon")

    game_number = data.get("game_number")
    try:
        game_number = int(game_number) if game_number is not None else None
    except (TypeError, ValueError):
        game_number = None

    lottery_id = data.get("lottery_id")
    try:
        lottery_id = int(lottery_id) if lottery_id is not None else None
    except (TypeError, ValueError):
        lottery_id = None

    return (
        {
            "title": title,
            "game_datetime": game_datetime,
            "description": description,
            "restaurant_name": restaurant_name,
            "restaurant_address": restaurant_address,
            "restaurant_lat": restaurant_lat,
            "restaurant_lon": restaurant_lon,
            "game_number": game_number,
            "lottery_id": lottery_id,
        },
        None,
    )


class _HtmlNode:
    def __init__(self, tag: str, attrs: dict[str, str | None]) -> None:
        self.tag = tag
        self.attrs = attrs
        self.children: list[_HtmlNode] = []
        self.text_parts: list[str] = []

    def append_text(self, data: str) -> None:
        if data:
            self.text_parts.append(data)

    def text(self) -> str:
        text = "".join(self.text_parts).strip()
        if not self.children:
            return text
        child_text = " ".join(child.text() for child in self.children if child.text())
        if text and child_text:
            return f"{text} {child_text}".strip()
        return text or child_text


class _HtmlTreeBuilder(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.root = _HtmlNode("document", {})
        self._stack = [self.root]

    def handle_starttag(self, tag: str, attrs) -> None:
        attrs_dict = {key: value for key, value in attrs}
        node = _HtmlNode(tag, attrs_dict)
        self._stack[-1].children.append(node)
        self._stack.append(node)

    def handle_endtag(self, tag: str) -> None:
        if len(self._stack) > 1:
            self._stack.pop()

    def handle_data(self, data: str) -> None:
        self._stack[-1].append_text(data)


def _class_has_prefix(attrs: dict[str, str | None], prefix: str) -> bool:
    raw = attrs.get("class") or ""
    return any(part.startswith(prefix) for part in raw.split())


def _find_first(node: _HtmlNode, predicate) -> _HtmlNode | None:
    if predicate(node):
        return node
    for child in node.children:
        found = _find_first(child, predicate)
        if found:
            return found
    return None


def _nth_child(node: _HtmlNode, tag: str, index: int) -> _HtmlNode | None:
    matched = [child for child in node.children if child.tag == tag]
    if index <= 0 or index > len(matched):
        return None
    return matched[index - 1]


def _parse_iso_datetime(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(cleaned)
    except ValueError:
        return normalize_game_datetime(value)
    return parsed.strftime("%Y-%m-%d %H:%M")


def _parse_shaker_v1(raw_html: str | dict) -> tuple[dict | None, str | None]:
    if not isinstance(raw_html, str):
        return None, "Некорректный ответ парсера HTML."
    parser = _HtmlTreeBuilder()
    parser.feed(raw_html)
    root = parser.root

    time_node = _find_first(
        root,
        lambda node: node.tag == "time" and _class_has_prefix(node.attrs, "GameCard_figure-text__"),
    )
    datetime_raw = time_node.attrs.get("datetime") if time_node else None
    game_datetime = _parse_iso_datetime(datetime_raw)
    if not game_datetime:
        return None, "Не удалось извлечь дату игры."

    wrapper = _find_first(
        root,
        lambda node: node.tag == "div" and _class_has_prefix(node.attrs, "GameCard_wrapper__"),
    )
    title_node = None
    description_node = None
    if wrapper:
        third_div = _nth_child(wrapper, "div", 3)
        if third_div:
            inner_div = _nth_child(third_div, "div", 1)
            if inner_div:
                inner_div = _nth_child(inner_div, "div", 1)
                if inner_div:
                    title_node = _nth_child(inner_div, "p", 1)
                    description_node = _nth_child(inner_div, "p", 2)
    title = title_node.text() if title_node else ""
    if not title:
        return None, "Не удалось извлечь название игры."
    title = f"{title} (шейкер квиз)"
    description = description_node.text() if description_node else None

    location_wrapper = _find_first(
        root,
        lambda node: node.tag == "div"
        and _class_has_prefix(node.attrs, "GameCard_wrapper__location__"),
    )
    address = None
    if location_wrapper:
        loc_div = _nth_child(location_wrapper, "div", 1)
        if loc_div:
            loc_div = _nth_child(loc_div, "div", 1)
            if loc_div:
                address_node = _nth_child(loc_div, "p", 1)
                if address_node:
                    address = address_node.text() or None

    reg_wrapper = _find_first(
        root,
        lambda node: node.tag == "div"
        and _class_has_prefix(node.attrs, "_id__contentBlock__"),
    )
    logger.info("shaker reg_wrapper=%s", reg_wrapper)
    logger.info("shaker parser reached reg block")
    registration_external_id = None
    if reg_wrapper:
        logger.info("shaker reg_wrapper confirmed=%s", reg_wrapper)
        first_div = _nth_child(reg_wrapper, "div", 1)
        if first_div:
            second_div = _nth_child(first_div, "div", 1)
            if second_div:
                link_node = _nth_child(second_div, "a", 1)
                if link_node:
                    href = link_node.attrs.get("href") or ""
                    logger.info("shaker reg href=%s", href)

                    if "#" in href:
                        registration_external_id = href.split("#", 1)[1] or None
    if not registration_external_id:
        return None, "Не удалось извлечь id игры."

    return (
        {
            "title": title,
            "game_datetime": game_datetime,
            "description": description,
            "restaurant_name": address,
            "restaurant_address": None,
            "restaurant_lat": None,
            "restaurant_lon": None,
            "game_number": None,
            "lottery_id": None,
            "external_id": registration_external_id,
            "registration_external_id": registration_external_id,
        },
        None,
    )


API_PARSERS = {
    "quizplease_v1": _parse_quizplease_v1,
    "shaker_v1": _parse_shaker_v1,
}


def build_game_payload_from_api(
    data: dict | str, parser_name: str | None
) -> tuple[dict | None, str | None]:
    parser_key = parser_name or "quizplease_v1"
    parser = API_PARSERS.get(parser_key)
    if not parser:
        return None, f"Неизвестный парсер API: {parser_key}."
    return parser(data)

def parse_poll_labels(text: str | None) -> list[str]:
    if not text:
        return pick_default_answers()
    raw = text.strip()
    if not raw:
        return pick_default_answers()
    quotechar = None
    if any(ch in raw for ch in ('"', "“", "”", "«", "»")):
        quotechar = '"'
        raw = (
            raw.replace("“", '"')
            .replace("”", '"')
            .replace("«", '"')
            .replace("»", '"')
        )
    elif "'" in raw:
        quotechar = "'"
    if quotechar:
        try:
            import csv
            import io

            reader = csv.reader(
                io.StringIO(raw),
                delimiter=",",
                quotechar=quotechar,
                skipinitialspace=True,
            )
            row = next(reader, [])
            parts = [item.strip() for item in row if item.strip()]
        except Exception:
            parts = [item.strip() for item in raw.split(",") if item.strip()]
    else:
        parts = [item.strip() for item in raw.split(",") if item.strip()]
    if len(parts) != 3:
        return pick_default_answers()
    return parts


def build_poll_question(data: dict) -> str | None:
    title = data.get("title")
    date_raw = data.get("date")
    game_datetime = parse_api_date(date_raw)
    if not title or not game_datetime:
        return None
    date_text = format_game_datetime(game_datetime)
    place = data.get("place") or {}
    restaurant_name = place.get("title")
    if restaurant_name:
        return f"{title} — {date_text} — {restaurant_name}"
    return f"{title} — {date_text}"


def build_poll_question_from_payload(payload: dict) -> str | None:
    title = payload.get("title")
    game_datetime = payload.get("game_datetime")
    if not title or not game_datetime:
        return None
    date_text = format_game_datetime(game_datetime)
    restaurant_name = payload.get("restaurant_name")
    if restaurant_name:
        return f"{title} — {date_text} — {restaurant_name}"
    return f"{title} — {date_text}"


def build_upcoming_schedule_text(
    db_path: str, limit: int | None = None, user_id: int | None = None
) -> str:
    if user_id is None:
        if limit is None:
            total = count_upcoming_games(db_path)
            games = list_upcoming_games(db_path, limit=total, offset=0)
        else:
            games = list_upcoming_games(db_path, limit=limit, offset=0)
    else:
        if limit is None:
            games = list_upcoming_games_with_counts(db_path, user_id)
        else:
            games = list_upcoming_games_with_counts_limit(db_path, user_id, limit=limit, offset=0)
    if not games:
        return "Ближайших игр пока нет."

    lines = ["Предстоящие игры:"]
    for game in games:
        if game.get("game_date"):
            game_date = f" ({format_game_datetime(game['game_date'])})"
        else:
            game_date = ""
        if user_id is None:
            lines.append(f"{game['title']}{game_date}")
        else:
            status_icon, _ = get_participation_status(game)
            lines.append(f"{status_icon} {game['title']}{game_date}")
    return "\n".join(lines)


def get_participation_status(game: dict) -> tuple[str, str]:
    if game.get("is_registered"):
        return "✅", "Ты записан"
    if game.get("is_waitlisted"):
        return "⏳", "Ты в резерве"
    if game.get("is_maybe"):
        return "🤨", "Пока не уверен"
    return "✖️", "Ты не записан"


def build_poll_threshold_message(db_path: str, game_id: int, threshold: int) -> str | None:
    game = get_game_basic(db_path, game_id)
    if not game:
        return None
    title = escape_markdown(game.get("title") or "Игра")
    date_text = escape_markdown(format_game_datetime(game.get("game_date")))
    return (
        f"На *{title} \\({date_text}\\)* набралось уже {threshold} участников\\."
        " Пора зарегистрироваться\\!"
    )


def build_poll_registered_message(db_path: str, game_id: int) -> str | None:
    game = get_game_basic(db_path, game_id)
    if not game:
        return None
    title = escape_markdown(game.get("title") or "Игра")
    date_text = escape_markdown(format_game_datetime(game.get("game_date")))
    return f"Наша команда зарегана на игру *{title} \\({date_text}\\)*\\."


def build_poll_full_message(db_path: str, poll_id: str, game_id: int) -> str | None:
    game = get_game_basic(db_path, game_id)
    if not game:
        return None
    title = html.escape(game.get("title") or "Игра")
    date_text = html.escape(format_game_datetime(game.get("game_date")))
    participants = list_game_registrants(db_path, game_id, include_cancelled=False)
    if participants:
        names = []
        for participant in participants:
            username = participant.get("username")
            full_name = participant.get("full_name") or ""
            user_id = participant.get("user_id")
            if username:
                clean_username = username.lstrip("@")
                label = html.escape(clean_username)
                href = f"https://t.me/{clean_username}"
                names.append(f'<a href="{href}">{label}</a>')
            elif full_name:
                label = html.escape(full_name)
                href = f"tg://user?id={user_id}"
                names.append(f'<a href="{href}">{label}</a>')
            else:
                label = html.escape(f"id:{user_id}")
                href = f"tg://user?id={user_id}"
                names.append(f'<a href="{href}">{label}</a>')
        roster = "\n".join(f"- {name}" for name in names)
    else:
        roster = "- нет данных"
    return f"у нас фулл!\n{title} — {date_text}\n{roster}"
logger = logging.getLogger(__name__)
