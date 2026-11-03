import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import mock

import api_client
from api_client import build_registration_payload
from config import TEAM_REGISTRATION_COMMENT

# Deterministic values for tests, independent of any local .env.
AMRA_TEAM_NAME = "Тестовая команда AMRA"
QUIZPLEASE_PROMO_ID = 12345
from game_registration import init_db, list_upcoming_games_admin
from reminders import run_due_reminders

try:
    from handlers import register_handlers
except ModuleNotFoundError as exc:
    if exc.name != "telebot":
        raise
    register_handlers = None


class FakeBot:
    def __init__(self) -> None:
        self.messages = []

    def send_message(self, chat_id, text, **kwargs) -> None:
        self.messages.append({"chat_id": chat_id, "text": text, **kwargs})


class FakeHandlerBot:
    def __init__(self) -> None:
        self.poll_answer_callback = None
        self.messages = []

    def get_me(self):
        return SimpleNamespace(username="qphelperbot")

    def message_handler(self, *args, **kwargs):
        return lambda callback: callback

    def callback_query_handler(self, *args, **kwargs):
        return lambda callback: callback

    def poll_answer_handler(self, *args, **kwargs):
        def decorator(callback):
            self.poll_answer_callback = callback
            return callback

        return decorator

    def send_message(self, chat_id, text, **kwargs) -> None:
        self.messages.append({"chat_id": chat_id, "text": text, **kwargs})


class ReminderTests(unittest.TestCase):
    def setUp(self) -> None:
        fd, self.db_path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(fd)
        init_db(self.db_path)
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO organizers (id, name, poll_max_people) VALUES (1, 'Test', 9)"
        )
        conn.execute(
            """
            INSERT INTO games (
                id, title, game_date, restaurant_name, organizer_id
            ) VALUES (1, 'Сегодняшняя игра', '2026-08-28 19:30', 'AMRA', 1)
            """
        )
        conn.execute(
            """
            INSERT INTO games (
                id, title, game_date, restaurant_name, organizer_id
            ) VALUES (2, 'Завтрашняя игра', '2026-08-29 19:30', 'Руки ВВерх!', 1)
            """
        )
        conn.execute(
            "INSERT INTO polls (poll_id, game_id, chat_id, message_id) VALUES ('today', 1, 100, 10)"
        )
        conn.execute(
            "INSERT INTO polls (poll_id, game_id, chat_id, message_id) VALUES ('tomorrow', 2, 100, 11)"
        )
        conn.execute(
            """
            INSERT INTO registrations (game_id, user_id, username, full_name)
            VALUES (1, 1, 'today_player', 'Today Player')
            """
        )
        conn.executemany(
            """
            INSERT INTO registrations (game_id, user_id, username, full_name)
            VALUES (2, ?, ?, ?)
            """,
            [(user_id, f"player{user_id}", f"Player {user_id}") for user_id in range(2, 9)],
        )
        conn.commit()
        conn.close()

    def tearDown(self) -> None:
        os.unlink(self.db_path)

    def test_moscow_schedule_availability_and_idempotency(self) -> None:
        bot = FakeBot()

        # 07:59 UTC is 10:59 MSK: nothing is due yet.
        run_due_reminders(
            bot,
            self.db_path,
            datetime(2026, 8, 28, 7, 59, tzinfo=timezone.utc),
        )
        self.assertEqual(bot.messages, [])

        # 08:00 UTC is 11:00 MSK: send the day-of reminder once.
        run_due_reminders(
            bot,
            self.db_path,
            datetime(2026, 8, 28, 8, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(len(bot.messages), 1)
        self.assertIn("Сегодня игра", bot.messages[0]["text"])
        self.assertEqual(bot.messages[0]["reply_to_message_id"], 10)
        day_of_labels = [
            button.text
            for row in bot.messages[0]["reply_markup"].keyboard
            for button in row
        ]
        self.assertNotIn("👥 Кто идёт", day_of_labels)
        self.assertEqual(day_of_labels, ["📱 Открыть игру"])

        run_due_reminders(
            bot,
            self.db_path,
            datetime(2026, 8, 28, 8, 1, tzinfo=timezone.utc),
        )
        self.assertEqual(len(bot.messages), 1)

        # 11:00 UTC is 14:00 MSK: tomorrow has 7/9 players, so announce 2 spots.
        run_due_reminders(
            bot,
            self.db_path,
            datetime(2026, 8, 28, 11, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(len(bot.messages), 2)
        self.assertIn("Завтра игра", bot.messages[1]["text"])
        self.assertIn("Свободных мест: *2*", bot.messages[1]["text"])
        self.assertEqual(bot.messages[1]["reply_to_message_id"], 11)

        run_due_reminders(
            bot,
            self.db_path,
            datetime(2026, 8, 28, 11, 1, tzinfo=timezone.utc),
        )
        self.assertEqual(len(bot.messages), 2)

    def test_admin_game_list_still_works(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE games SET game_date = CASE id "
                "WHEN 1 THEN '2099-08-28 19:30' ELSE '2099-08-29 19:30' END"
            )
        games = list_upcoming_games_admin(self.db_path)
        self.assertEqual([game["id"] for game in games], [1, 2])


class RegistrationPayloadTests(unittest.TestCase):
    def setUp(self) -> None:
        for name, value in (
            ("AMRA_TEAM_NAME", AMRA_TEAM_NAME),
            ("QUIZPLEASE_PROMO_ID", QUIZPLEASE_PROMO_ID),
            ("QUIZPLEASE_PROMO_END_DATE", "2026-09-30"),
        ):
            patcher = mock.patch.object(api_client, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_default_comment_is_always_applied(self) -> None:
        result = build_registration_payload(
            {"title": "Тестовая команда", "comment": ""},
            "event-1",
            "Другой ресторан",
            datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(result["title"], "Тестовая команда")
        self.assertEqual(result["comment"], TEAM_REGISTRATION_COMMENT)
        self.assertEqual(result["event_id"], "event-1")
        self.assertEqual(result["promo_ids"], [QUIZPLEASE_PROMO_ID])

    def test_amra_uses_special_team_name_for_both_payload_shapes(self) -> None:
        quizplease = build_registration_payload(
            {"title": "Тестовая команда"},
            None,
            "AMRA (Event hall, 2 этаж)",
        )
        shaker = build_registration_payload(
            {"team_name": "Тестовая команда"},
            None,
            "ресторан Амра",
        )
        self.assertEqual(quizplease["title"], AMRA_TEAM_NAME)
        self.assertEqual(shaker["team_name"], AMRA_TEAM_NAME)
        self.assertEqual(quizplease["comment"], TEAM_REGISTRATION_COMMENT)
        self.assertEqual(shaker["comment"], TEAM_REGISTRATION_COMMENT)

    def test_quizplease_promo_expires_at_end_of_september_moscow_time(self) -> None:
        active = build_registration_payload(
            {"title": "Тестовая команда", "promo_ids": []},
            None,
            "Другой ресторан",
            datetime(2026, 9, 30, 20, 59, tzinfo=timezone.utc),
        )
        expired = build_registration_payload(
            {"title": "Тестовая команда", "promo_ids": [QUIZPLEASE_PROMO_ID]},
            None,
            "Другой ресторан",
            datetime(2026, 9, 30, 21, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(active["promo_ids"], [QUIZPLEASE_PROMO_ID])
        self.assertEqual(expired["promo_ids"], [])

    def test_quizplease_promo_is_not_added_to_shaker_payload(self) -> None:
        result = build_registration_payload(
            {"team_name": "Тестовая команда", "promocode": ""},
            "event-2",
            "Другой ресторан",
            datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc),
        )

        self.assertNotIn("promo_ids", result)
        self.assertEqual(result["promocode"], "")


@unittest.skipIf(register_handlers is None, "pyTelegramBotAPI is not installed")
class PollRegistrationSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        fd, self.db_path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(fd)
        init_db(self.db_path)
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO organizers (id, name, poll_max_people) VALUES (1, 'Test', 9)"
        )
        conn.execute(
            """
            INSERT INTO games (id, title, game_date, organizer_id)
            VALUES (1, 'Меломания', '2026-08-28 19:30', 1)
            """
        )
        conn.execute(
            """
            INSERT INTO polls (poll_id, game_id, chat_id, message_id, yes_notified)
            VALUES ('melomania', 1, 100, 10, 1)
            """
        )
        conn.execute(
            """
            INSERT INTO registrations (game_id, user_id, username, full_name)
            VALUES (1, 42, 'player', 'Player')
            """
        )
        conn.execute(
            """
            INSERT INTO poll_answers (poll_id, user_id, username, full_name, option_id)
            VALUES ('melomania', 42, 'player', 'Player', 0)
            """
        )
        conn.execute(
            """
            INSERT INTO game_waitlist (game_id, user_id, username, full_name)
            VALUES (1, 43, 'waiting', 'Waiting')
            """
        )
        conn.execute(
            """
            INSERT INTO poll_answers (poll_id, user_id, username, full_name, option_id)
            VALUES ('melomania', 43, 'waiting', 'Waiting', 0)
            """
        )
        conn.commit()
        conn.close()

        self.bot = FakeHandlerBot()
        register_handlers(self.bot, self.db_path)

    def tearDown(self) -> None:
        os.unlink(self.db_path)

    def answer(self, user_id: int, username: str, option_id: int) -> None:
        poll_answer = SimpleNamespace(
            poll_id="melomania",
            option_ids=[option_id],
            user=SimpleNamespace(
                id=user_id,
                username=username,
                full_name=username,
            ),
        )
        self.bot.poll_answer_callback(poll_answer)

    def test_switching_from_yes_cancels_registration(self) -> None:
        self.answer(42, "player", 1)

        conn = sqlite3.connect(self.db_path)
        registration = conn.execute(
            "SELECT is_cancelled FROM registrations WHERE game_id = 1 AND user_id = 42"
        ).fetchone()
        poll_answer = conn.execute(
            "SELECT option_id FROM poll_answers WHERE poll_id = 'melomania' AND user_id = 42"
        ).fetchone()
        conn.close()

        self.assertEqual(registration[0], 1)
        self.assertEqual(poll_answer[0], 1)

    def test_switching_from_yes_removes_waitlist_entry(self) -> None:
        self.answer(43, "waiting", 2)

        conn = sqlite3.connect(self.db_path)
        waitlist_count = conn.execute(
            "SELECT COUNT(*) FROM game_waitlist WHERE game_id = 1 AND user_id = 43"
        ).fetchone()[0]
        conn.close()

        self.assertEqual(waitlist_count, 0)


if __name__ == "__main__":
    unittest.main()
