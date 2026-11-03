from __future__ import annotations

import json
from datetime import datetime, time
from urllib import parse, request, error
from zoneinfo import ZoneInfo

from config import (
    AMRA_TEAM_NAME,
    BOT_TIMEZONE,
    QUIZPLEASE_PROMO_END_DATE,
    QUIZPLEASE_PROMO_ID,
    TEAM_REGISTRATION_COMMENT,
)


def _extract_api_error(payload: object) -> str | None:
    """Return a useful site validation message without exposing a whole response."""
    if isinstance(payload, str):
        value = payload.strip()
        return value[:1000] if value else None
    if isinstance(payload, list):
        messages = [message for item in payload if (message := _extract_api_error(item))]
        return "; ".join(dict.fromkeys(messages))[:1000] or None
    if not isinstance(payload, dict):
        return None
    messages: list[str] = []
    for key in ("message", "detail", "error", "errors", "description"):
        if key in payload and (message := _extract_api_error(payload[key])):
            messages.append(message)
    if not messages:
        for value in payload.values():
            if isinstance(value, (str, dict, list)) and (message := _extract_api_error(value)):
                messages.append(message)
    return "; ".join(dict.fromkeys(messages))[:1000] or None


def _decode_error_body(raw: str) -> str | None:
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        payload = raw
    return _extract_api_error(payload)


def _quizplease_promo_is_active(
    now: datetime | None = None, end_date_value: str | None = None
) -> bool:
    timezone = ZoneInfo(BOT_TIMEZONE)
    current = now or datetime.now(timezone)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone)
    else:
        current = current.astimezone(timezone)
    end_date = datetime.strptime(
        end_date_value or QUIZPLEASE_PROMO_END_DATE, "%Y-%m-%d"
    ).date()
    expires_at = datetime.combine(end_date, time.max, tzinfo=timezone)
    return current <= expires_at


def build_registration_payload(
    registration_payload: dict | None,
    registration_id: str | None,
    restaurant_name: str | None,
    now: datetime | None = None,
    runtime_settings: dict[str, str] | None = None,
) -> dict | None:
    if not isinstance(registration_payload, dict):
        return None
    settings = runtime_settings or {}
    payload = {
        **registration_payload,
        "comment": settings.get("registration_comment", TEAM_REGISTRATION_COMMENT),
    }
    if registration_id:
        payload["event_id"] = registration_id
    if "title" in payload:
        default_name = settings.get("default_team_name")
        if default_name:
            payload["title"] = default_name
        promo_id = int(settings.get("quizplease_promo_id", QUIZPLEASE_PROMO_ID))
        if promo_id > 0 and _quizplease_promo_is_active(
            now, settings.get("quizplease_promo_end_date")
        ):
            payload["promo_ids"] = [promo_id]
        else:
            payload["promo_ids"] = []
    if "team_name" in payload and settings.get("default_team_name"):
        payload["team_name"] = settings["default_team_name"]
    normalized_restaurant = (restaurant_name or "").casefold()
    if "amra" in normalized_restaurant or "амра" in normalized_restaurant:
        if "title" in payload:
            payload["title"] = settings.get("amra_team_name", AMRA_TEAM_NAME)
        if "team_name" in payload:
            payload["team_name"] = settings.get("amra_team_name", AMRA_TEAM_NAME)
    return payload

def fetch_game_from_api(
    game_id: str,
    api_view_template: str,
    game_url_template: str,
) -> tuple[bool, str | dict]:
    if not api_view_template:
        if not game_url_template:
            return False, "Не настроена ссылка API для организатора."
        url = game_url_template.format(external_id=game_id, game_id=game_id)
        try:
            with request.urlopen(url, timeout=15) as response:
                return True, response.read().decode("utf-8")
        except error.URLError as exc:
            return False, f"Не удалось получить страницу игры: {exc}"
    base = api_view_template.format(game_id=game_id, external_id=game_id)
    params = [
        ("relationships[]", "result"),
        ("relationships[]", "game_tables.game_record:id,status"),
        ("relationships[]", "custom_fields.radios"),
    ]
    url = f"{base}?{parse.urlencode(params)}"
    try:
        with request.urlopen(url, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except error.URLError as exc:
        return False, f"Не удалось получить данные API: {exc}"
    except json.JSONDecodeError:
        return False, "Ответ API не является JSON."

    if payload.get("status") != "ok":
        return False, "API вернуло ошибку."
    data = payload.get("data")
    if not isinstance(data, dict):
        return False, "API не вернуло данные игры."
    return True, data


def register_team_in_api(
    external_id: str, registration_api_template: str, registration_payload: dict | None
) -> tuple[bool, str]:
    if not registration_api_template:
        return False, "Не настроена ссылка регистрации для организатора."
    if not isinstance(registration_payload, dict):
        return False, "Не настроены данные для регистрации команды."
    url = registration_api_template.format(external_id=external_id, game_id=external_id)
    data = json.dumps(registration_payload, ensure_ascii=False).encode("utf-8")
    req = request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=15) as response:
            status_code = response.getcode()
            raw = response.read().decode("utf-8")
    except error.HTTPError as exc:
        try:
            raw_error = exc.read().decode("utf-8", errors="replace")
        except Exception:
            raw_error = ""
        site_message = _decode_error_body(raw_error)
        if site_message:
            return False, f"Сайт не подтвердил регистрацию: {site_message}"
        return False, f"Сайт не подтвердил регистрацию (HTTP {exc.code})."
    except error.URLError as exc:
        return False, f"Не удалось отправить регистрацию: {exc}"

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return False, "Ответ API не является JSON."
    if isinstance(payload, dict) and payload.get("status") == "ok":
        return True, "Команда зарегистрирована."
    if status_code and 200 <= status_code < 300:
        if isinstance(payload, dict):
            if payload.get("status") == "error" or payload.get("error") or payload.get("errors"):
                site_message = _extract_api_error(payload)
                if site_message:
                    return False, f"Сайт не подтвердил регистрацию: {site_message}"
                return False, "Сайт не подтвердил регистрацию."
        return True, "Команда зарегистрирована."
    site_message = _extract_api_error(payload)
    if site_message:
        return False, f"Сайт не подтвердил регистрацию: {site_message}"
    return False, "Сайт не подтвердил регистрацию."
