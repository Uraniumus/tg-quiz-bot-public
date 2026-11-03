import io
import hashlib
import hmac
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlencode

from api_client import register_team_in_api
from game_registration import init_db, list_upcoming_games_with_counts, record_poll_answer
from product_features import (
    build_monthly_stats,
    build_personal_stats,
    get_prompt_game,
    parse_result_reply,
    reconcile_poll_registrations,
    save_game_result,
)
from reminders import run_due_reminders
from share_app import validate_webapp_init_data
from ui import (
    build_calendar_file,
    build_calendar_url,
    build_game_block,
    build_group_game_keyboard,
    build_invite_text,
    build_invite_share_url,
    build_location_keyboard,
    build_share_keyboard,
    clean_game_title,
)


class DatabaseTestCase(unittest.TestCase):
    def setUp(self) -> None:
        fd, self.db_path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(fd)
        init_db(self.db_path)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO organizers (id, name, poll_max_people) VALUES (1, 'Test', 2)"
            )

    def tearDown(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            path = self.db_path + suffix
            if os.path.exists(path):
                os.unlink(path)


class ResultTests(DatabaseTestCase):
    def test_result_form_requires_place_and_team_count(self) -> None:
        result, error = parse_result_reply(
            "Место: 4\nИз скольки команд: 31\nЛотерея: @alice — сертификат\nПодлутки: вино"
        )
        self.assertIsNone(error)
        self.assertEqual(result["position"], 4)
        self.assertEqual(result["total_teams"], 31)

        result, error = parse_result_reply("Лотерея: ничего\nПодлутки: ничего")
        self.assertIsNone(result)
        self.assertIn("Место", error)

    def test_result_updates_keep_revision_history(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO games (id, title, game_date, organizer_id) VALUES (1, 'Игра', '2026-08-10 19:30', 1)"
            )
        first = {
            "position": 5,
            "total_teams": 20,
            "lottery_text": "ничего",
            "podlutki_text": "ничего",
        }
        second = {**first, "position": 4, "podlutki_text": "сертификат"}
        self.assertFalse(save_game_result(self.db_path, 1, first, 10, "alice", "Alice"))
        self.assertTrue(save_game_result(self.db_path, 1, second, 11, "bob", "Bob"))
        with sqlite3.connect(self.db_path) as conn:
            current = conn.execute(
                "SELECT position, podlutki_text FROM game_results WHERE game_id = 1"
            ).fetchone()
            revisions = conn.execute(
                "SELECT COUNT(*) FROM game_result_revisions WHERE game_id = 1"
            ).fetchone()[0]
        self.assertEqual(current, (4, "сертификат"))
        self.assertEqual(revisions, 2)


class ReconciliationTests(DatabaseTestCase):
    def test_unknown_poll_answer_is_not_stored(self) -> None:
        stored = record_poll_answer(self.db_path, "unknown", 1, 0, "alice", "Alice")
        self.assertFalse(stored)
        with sqlite3.connect(self.db_path) as conn:
            count = conn.execute("SELECT COUNT(*) FROM poll_answers").fetchone()[0]
        self.assertEqual(count, 0)

    def test_reconcile_respects_capacity_and_cancels_no_vote(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO games (id, title, game_date, organizer_id) VALUES (1, 'Игра', '2026-08-28 19:30', 1)"
            )
            conn.execute(
                "INSERT INTO polls (poll_id, game_id, chat_id, message_id) VALUES ('p1', 1, -100, 7)"
            )
            conn.executemany(
                "INSERT INTO poll_answers (poll_id, user_id, username, full_name, option_id) VALUES ('p1', ?, ?, ?, ?)",
                [
                    (1, "one", "One", 0),
                    (2, "two", "Two", 0),
                    (3, "three", "Three", 0),
                    (4, "four", "Four", 1),
                ],
            )
            conn.execute(
                "INSERT INTO registrations (game_id, user_id, username, full_name) VALUES (1, 1, 'one', 'One')"
            )
            conn.execute(
                "INSERT INTO registrations (game_id, user_id, username, full_name) VALUES (1, 4, 'four', 'Four')"
            )

        result = reconcile_poll_registrations(
            self.db_path, True, current_date=date(2026, 8, 26)
        )
        self.assertEqual(result["restored_registrations"], 1)
        self.assertEqual(result["restored_waitlist"], 1)
        self.assertEqual(result["cancelled_stale_registrations"], 1)
        with sqlite3.connect(self.db_path) as conn:
            active = conn.execute(
                "SELECT user_id FROM registrations WHERE game_id = 1 AND is_cancelled = 0 ORDER BY user_id"
            ).fetchall()
            waiting = conn.execute(
                "SELECT user_id FROM game_waitlist WHERE game_id = 1"
            ).fetchall()
        self.assertEqual(active, [(1,), (2,)])
        self.assertEqual(waiting, [(3,)])

    def test_maybe_status_comes_from_latest_poll_only(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO games (id, title, game_date, organizer_id) VALUES (1, 'Игра', '2099-08-28 19:30', 1)"
            )
            conn.execute(
                "INSERT INTO polls (poll_id, game_id, chat_id, message_id) VALUES ('old', 1, -100, 7)"
            )
            conn.execute(
                "INSERT INTO poll_answers (poll_id, user_id, option_id) VALUES ('old', 42, 2)"
            )
            conn.execute(
                "INSERT INTO polls (poll_id, game_id, chat_id, message_id) VALUES ('new', 1, -100, 8)"
            )
            conn.execute(
                "INSERT INTO poll_answers (poll_id, user_id, option_id) VALUES ('new', 42, 1)"
            )
        game = list_upcoming_games_with_counts(self.db_path, 42)[0]
        self.assertFalse(game["is_maybe"])


class StatisticsTests(DatabaseTestCase):
    def test_monthly_card_uses_average_player_count(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.executemany(
                "INSERT INTO games (id, title, game_date, organizer_id) VALUES (?, ?, ?, 1)",
                [(1, "Первая", "2026-08-05 19:00"), (2, "Вторая", "2026-08-20 19:00")],
            )
            conn.executemany(
                "INSERT INTO registrations (game_id, user_id, username, full_name) VALUES (?, ?, ?, ?)",
                [
                    (1, 1, "one", "One"),
                    (1, 2, "two", "Two"),
                    (2, 1, "one", "One"),
                    (2, 2, "two", "Two"),
                    (2, 3, "three", "Three"),
                    (2, 4, "four", "Four"),
                ],
            )
        base = {
            "position": 3,
            "total_teams": 25,
            "lottery_text": "ничего",
            "podlutki_text": "ничего",
        }
        save_game_result(self.db_path, 1, base, 1, "one", "One")
        save_game_result(self.db_path, 2, {**base, "position": 5}, 1, "one", "One")

        stats = build_monthly_stats(self.db_path, "2026-08")
        self.assertEqual(stats["games_count"], 2)
        self.assertEqual(stats["average_players"], 3.0)
        personal = build_personal_stats(self.db_path, 1, "one", "2026-08")
        self.assertIn("Отмечался на игры: 2", personal)


class SchedulerTests(DatabaseTestCase):
    class Bot:
        def __init__(self) -> None:
            self.messages = []

        def send_message(self, chat_id, text, **kwargs):
            message = SimpleNamespace(message_id=len(self.messages) + 100)
            self.messages.append((chat_id, text, kwargs, message))
            return message

    def test_postgame_prompt_is_sent_once_at_2300(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO games (id, title, game_date, organizer_id) VALUES (1, 'Игра', '2026-08-28 19:30', 1)"
            )
            conn.execute(
                "INSERT INTO polls (poll_id, game_id, chat_id, message_id) VALUES ('p1', 1, -100, 7)"
            )
        bot = self.Bot()
        run_due_reminders(
            bot, self.db_path, datetime(2026, 8, 28, 20, 0, tzinfo=timezone.utc)
        )
        run_due_reminders(
            bot, self.db_path, datetime(2026, 8, 28, 20, 1, tzinfo=timezone.utc)
        )
        prompts = [message for message in bot.messages if "Как сыграли" in message[1]]
        self.assertEqual(len(prompts), 1)
        self.assertIn("Из скольки команд", prompts[0][1])
        self.assertIsNotNone(
            get_prompt_game(self.db_path, -100, prompts[0][3].message_id)
        )


class ApiErrorTests(unittest.TestCase):
    def test_site_validation_message_is_preserved(self) -> None:
        response = HTTPError(
            "https://example.test",
            422,
            "Unprocessable Entity",
            {},
            io.BytesIO(
                '{"status":"error","message":"Ваша команда уже играла в эту игру"}'.encode()
            ),
        )
        with patch("api_client.request.urlopen", side_effect=response):
            ok, text = register_team_in_api("1", "https://example.test/{external_id}", {"x": 1})
        self.assertFalse(ok)
        self.assertIn("уже играла", text)


class UiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.game = {
            "id": 14,
            "title": "Меломания",
            "game_date": "2026-08-28 19:30",
            "restaurant_name": "AMRA",
            "restaurant_address": "Фонтанка, 1",
            "restaurant_lat": 59.9,
            "restaurant_lon": 30.3,
            "external_id": "game-14",
            "registrations": 8,
            "capacity": 9,
            "waitlist_count": 0,
            "is_registered": True,
            "is_waitlisted": False,
        }

    def test_private_game_card_shows_one_game_actions_and_navigation(self) -> None:
        text, keyboard = build_game_block(
            self.game, False, page_index=1, total_games=3
        )
        self.assertIn("Места: 8/9", text.replace("\\", ""))
        labels = [button.text for row in keyboard.keyboard for button in row]
        self.assertIn("📅 Добавить в календарь", labels)
        self.assertIn("📍 Место и маршрут", labels)
        invite_button = next(button for row in keyboard.keyboard for button in row if button.text == "💌 Позвать друга")
        self.assertTrue(invite_button.url.startswith("tg://msg_url?"))
        self.assertIn("2 из 3", labels)
        self.assertIn("←", labels)
        self.assertIn("→", labels)

    def test_private_game_card_can_use_prepared_share_web_app(self) -> None:
        _, keyboard = build_game_block(
            self.game,
            False,
            page_index=0,
            total_games=1,
            share_app_url="https://share.example:8443",
        )
        invite_button = next(
            button
            for row in keyboard.keyboard
            for button in row
            if button.text == "💌 Позвать друга"
        )
        self.assertIsNone(invite_button.url)
        self.assertEqual(
            invite_button.web_app.url,
            "https://share.example:8443/share?game_id=14",
        )

    def test_group_keyboard_contains_only_safe_links(self) -> None:
        keyboard = build_group_game_keyboard(
            self.game, -100123456, 777, "QPHelperBot"
        )
        buttons = [button for row in keyboard.keyboard for button in row]
        labels = [button.text for button in buttons]
        self.assertIn("🗳 Открыть опрос", labels)
        poll_button = next(button for button in buttons if button.text == "🗳 Открыть опрос")
        self.assertEqual(poll_button.url, "https://t.me/c/123456/777")
        app_button = next(button for button in buttons if button.text == "📱 Открыть игру")
        self.assertEqual(
            app_button.url,
            "https://t.me/QPHelperBot?startapp=game_14",
        )
        self.assertTrue(all(button.callback_data is None for button in buttons))

    def test_calendar_supports_google_and_apple_ics(self) -> None:
        google_url = build_calendar_url(self.game)
        self.assertIn("calendar.google.com/calendar/r/eventedit", google_url)
        self.assertIn("20260828T163000Z%2F20260828T193000Z", google_url)
        calendar_file, filename = build_calendar_file(self.game)
        decoded = calendar_file.decode("utf-8")
        self.assertEqual(filename, "quiz-14.ics")
        self.assertIn("DTSTART;TZID=Europe/Moscow:20260828T193000", decoded)
        self.assertIn("LOCATION:AMRA\\, Фонтанка\\, 1", decoded)

    def test_location_buttons_search_by_venue_and_address(self) -> None:
        keyboard = build_location_keyboard(self.game)
        buttons = [button for row in keyboard.keyboard for button in row]
        self.assertEqual([button.text for button in buttons], ["Яндекс Карты", "2ГИС", "Apple Карты"])
        self.assertIn("AMRA", buttons[0].url)
        self.assertNotIn("pt=", buttons[0].url)

    def test_invite_is_clean_and_has_hidden_poll_link(self) -> None:
        game = {
            **self.game,
            "title": "IV Чемпионат мира по Квиз, плиз! [отборочная] SPB",
        }
        text = build_invite_text(game, "https://t.me/QPHelperBot?start=forward_game-14")
        self.assertTrue(text.startswith("Привет! Го с нами на квиз?"))
        self.assertIn("IV Чемпионат мира по Квиз, плиз! — отборочная", text)
        self.assertNotIn("forward_game-14\n", text)
        self.assertIn(">Открыть опрос и отметиться</a>", text)
        self.assertEqual(
            clean_game_title(game["title"]),
            "IV Чемпионат мира по Квиз, плиз! — отборочная",
        )

    def test_invite_can_link_directly_to_mini_app(self) -> None:
        text = build_invite_text(
            self.game,
            "https://t.me/QPHelperBot?startapp=game_14",
            "Открыть игру и отметиться",
        )
        self.assertIn("startapp=game_14", text)
        self.assertIn("Открыть игру и отметиться", text)
        self.assertNotIn("Открыть опрос", text)

    def test_invite_share_url_uses_real_space_encoding_and_clean_short_title(self) -> None:
        game = {**self.game, "title": "[меломания] SPB"}
        share_url = build_invite_share_url(
            game, "https://t.me/QPHelperBot?start=forward_game-14"
        )
        self.assertNotIn("+", share_url)
        self.assertIn("%20", share_url)
        self.assertIn("%D0%9C%D0%B5%D0%BB%D0%BE%D0%BC%D0%B0%D0%BD%D0%B8%D1%8F", share_url)
        self.assertEqual(clean_game_title(game["title"]), "Меломания")

    def test_share_button_uses_native_telegram_deep_link(self) -> None:
        keyboard = build_share_keyboard(
            "Моя статистика", "https://t.me/QPHelperBot", "Поделиться"
        )
        self.assertTrue(keyboard.keyboard[0][0].url.startswith("tg://msg_url?"))

    def test_webapp_init_data_signature_is_verified(self) -> None:
        token = "123456:test-token"
        auth_date = 1_800_000_000
        values = {
            "auth_date": str(auth_date),
            "query_id": "query-1",
            "user": json.dumps({"id": 42, "first_name": "Test"}, separators=(",", ":")),
        }
        data_check_string = "\n".join(
            f"{key}={values[key]}" for key in sorted(values)
        )
        secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
        values["hash"] = hmac.new(
            secret, data_check_string.encode(), hashlib.sha256
        ).hexdigest()
        verified = validate_webapp_init_data(
            urlencode(values), token, now=auth_date + 10
        )
        self.assertEqual(verified["user_id"], 42)
        with self.assertRaises(ValueError):
            validate_webapp_init_data(
                urlencode({**values, "hash": "0" * 64}),
                token,
                now=auth_date + 10,
            )


if __name__ == "__main__":
    unittest.main()
