from __future__ import annotations

import calendar
import re
import sqlite3
from collections import Counter
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from config import BOT_TIMEZONE


RESULT_FIELD_ALIASES = {
    "место": "position",
    "из скольки команд": "total_teams",
    "команд": "total_teams",
    "всего команд": "total_teams",
    "лотерея": "lottery_text",
    "подлутки": "podlutki_text",
}


def init_product_features(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS team_registration_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            game_id INTEGER NOT NULL,
            requested_by INTEGER,
            success INTEGER NOT NULL,
            response_text TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (game_id) REFERENCES games(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS postgame_prompts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            game_id INTEGER NOT NULL UNIQUE,
            chat_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            sent_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (game_id) REFERENCES games(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS game_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            game_id INTEGER NOT NULL UNIQUE,
            position INTEGER NOT NULL,
            total_teams INTEGER NOT NULL,
            lottery_text TEXT NOT NULL DEFAULT 'ничего',
            podlutki_text TEXT NOT NULL DEFAULT 'ничего',
            submitted_by INTEGER NOT NULL,
            submitted_username TEXT,
            submitted_name TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (game_id) REFERENCES games(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS game_result_revisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            game_id INTEGER NOT NULL,
            position INTEGER NOT NULL,
            total_teams INTEGER NOT NULL,
            lottery_text TEXT NOT NULL,
            podlutki_text TEXT NOT NULL,
            submitted_by INTEGER NOT NULL,
            submitted_username TEXT,
            submitted_name TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (game_id) REFERENCES games(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS user_profiles (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            full_name TEXT,
            private_chat_id INTEGER,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS user_preferences (
            user_id INTEGER PRIMARY KEY,
            reactivation_enabled INTEGER NOT NULL DEFAULT 1,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS reactivation_deliveries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            delivered_month TEXT NOT NULL,
            sent_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (user_id, delivered_month)
        );

        CREATE TABLE IF NOT EXISTS monthly_card_deliveries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            period_month TEXT NOT NULL,
            sent_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (chat_id, period_month)
        );

        CREATE TABLE IF NOT EXISTS bot_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_by INTEGER,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_results_game ON game_results(game_id);
        CREATE INDEX IF NOT EXISTS idx_attempts_game ON team_registration_attempts(game_id, id DESC);
        CREATE INDEX IF NOT EXISTS idx_profiles_private_chat ON user_profiles(private_chat_id);
        """
    )
    conn.commit()


def record_registration_attempt(
    db_path: str,
    game_id: int,
    requested_by: int | None,
    success: bool,
    response_text: str,
) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO team_registration_attempts
                (game_id, requested_by, success, response_text)
            VALUES (?, ?, ?, ?)
            """,
            (game_id, requested_by, int(success), response_text[:2000]),
        )


def get_registration_status(db_path: str, game_id: int) -> dict[str, Any] | None:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT success, response_text, created_at, requested_by
            FROM team_registration_attempts
            WHERE game_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (game_id,),
        ).fetchone()
        return dict(row) if row else None


def list_admin_snapshots(db_path: str, current_date: date | None = None) -> list[dict[str, Any]]:
    today = (current_date or date.today()).isoformat()
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT g.id, g.title, g.game_date,
                   COALESCE(o.poll_max_people, 9) AS capacity,
                   (SELECT COUNT(*) FROM registrations r
                    WHERE r.game_id = g.id AND r.is_cancelled = 0) AS registrations,
                   (SELECT COUNT(*) FROM game_waitlist w WHERE w.game_id = g.id) AS waitlist,
                   (SELECT COUNT(*) FROM poll_answers pa
                    JOIN polls p ON p.poll_id = pa.poll_id
                    WHERE p.game_id = g.id AND pa.option_id = 0) AS poll_yes
            FROM games g
            LEFT JOIN organizers o ON o.id = g.organizer_id
            WHERE g.is_archived = 0 AND DATE(g.game_date) >= DATE(?)
            ORDER BY DATETIME(g.game_date), g.id
            """,
            (today,),
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            attempt = conn.execute(
                """
                SELECT success, response_text, created_at
                FROM team_registration_attempts
                WHERE game_id = ? ORDER BY id DESC LIMIT 1
                """,
                (item["id"],),
            ).fetchone()
            item["last_attempt"] = dict(attempt) if attempt else None
            result.append(item)
        return result


def upsert_user_profile(
    db_path: str,
    user_id: int,
    username: str | None,
    full_name: str | None,
    private_chat_id: int | None = None,
) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO user_profiles (user_id, username, full_name, private_chat_id)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username = COALESCE(excluded.username, user_profiles.username),
                full_name = COALESCE(excluded.full_name, user_profiles.full_name),
                private_chat_id = COALESCE(excluded.private_chat_id, user_profiles.private_chat_id),
                updated_at = CURRENT_TIMESTAMP
            """,
            (user_id, username, full_name, private_chat_id),
        )


def backfill_private_profiles(db_path: str) -> int:
    with sqlite3.connect(db_path) as conn:
        before = conn.total_changes
        conn.execute(
            """
            INSERT INTO user_profiles (user_id, username, full_name, private_chat_id)
            SELECT m.user_id, m.username, m.full_name, m.chat_id
            FROM messages m
            JOIN (
                SELECT user_id, MAX(id) AS max_id
                FROM messages
                WHERE user_id IS NOT NULL AND chat_id = user_id
                GROUP BY user_id
            ) latest ON latest.max_id = m.id
            ON CONFLICT(user_id) DO UPDATE SET
                username = COALESCE(excluded.username, user_profiles.username),
                full_name = COALESCE(excluded.full_name, user_profiles.full_name),
                private_chat_id = excluded.private_chat_id,
                updated_at = CURRENT_TIMESTAMP
            """
        )
        return conn.total_changes - before


def get_private_chat_id(db_path: str, user_id: int) -> int | None:
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT private_chat_id FROM user_profiles WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        return int(row[0]) if row and row[0] is not None else None


def record_postgame_prompt(
    db_path: str, game_id: int, chat_id: int, message_id: int
) -> bool:
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO postgame_prompts (game_id, chat_id, message_id)
            VALUES (?, ?, ?)
            """,
            (game_id, chat_id, message_id),
        )
        return cur.rowcount > 0


def postgame_prompt_exists(db_path: str, game_id: int) -> bool:
    with sqlite3.connect(db_path) as conn:
        return conn.execute(
            "SELECT 1 FROM postgame_prompts WHERE game_id = ?", (game_id,)
        ).fetchone() is not None


def get_prompt_game(
    db_path: str, chat_id: int, replied_message_id: int
) -> dict[str, Any] | None:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT p.game_id, g.title, g.game_date
            FROM postgame_prompts p
            JOIN games g ON g.id = p.game_id
            WHERE p.chat_id = ? AND p.message_id = ?
            """,
            (chat_id, replied_message_id),
        ).fetchone()
        return dict(row) if row else None


def parse_result_reply(text: str) -> tuple[dict[str, Any] | None, str | None]:
    parsed: dict[str, Any] = {}
    for raw_line in (text or "").splitlines():
        if ":" not in raw_line:
            continue
        raw_key, raw_value = raw_line.split(":", 1)
        key = RESULT_FIELD_ALIASES.get(raw_key.strip().casefold())
        if key:
            parsed[key] = raw_value.strip()

    missing = [
        label
        for field, label in (
            ("position", "Место"),
            ("total_teams", "Из скольки команд"),
            ("lottery_text", "Лотерея"),
            ("podlutki_text", "Подлутки"),
        )
        if field not in parsed or not str(parsed[field]).strip()
    ]
    if missing:
        return None, "Не хватает полей: " + ", ".join(missing) + "."

    try:
        position = int(str(parsed["position"]).strip())
        total_teams = int(str(parsed["total_teams"]).strip())
    except ValueError:
        return None, "Место и число команд нужно указать целыми числами."
    if position < 1 or total_teams < 1:
        return None, "Место и число команд должны быть больше нуля."
    if position > total_teams:
        return None, "Место не может быть больше общего числа команд."

    for field in ("lottery_text", "podlutki_text"):
        value = str(parsed[field]).strip()
        if value.casefold() in {"0", "нет", "ничего", "-", "не было"}:
            value = "ничего"
        parsed[field] = value[:1000]
    parsed["position"] = position
    parsed["total_teams"] = total_teams
    return parsed, None


def save_game_result(
    db_path: str,
    game_id: int,
    result: dict[str, Any],
    user_id: int,
    username: str | None,
    full_name: str | None,
) -> bool:
    with sqlite3.connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        existed = conn.execute(
            "SELECT 1 FROM game_results WHERE game_id = ?", (game_id,)
        ).fetchone() is not None
        values = (
            game_id,
            result["position"],
            result["total_teams"],
            result["lottery_text"],
            result["podlutki_text"],
            user_id,
            username,
            full_name,
        )
        conn.execute(
            """
            INSERT INTO game_result_revisions (
                game_id, position, total_teams, lottery_text, podlutki_text,
                submitted_by, submitted_username, submitted_name
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            values,
        )
        conn.execute(
            """
            INSERT INTO game_results (
                game_id, position, total_teams, lottery_text, podlutki_text,
                submitted_by, submitted_username, submitted_name
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(game_id) DO UPDATE SET
                position = excluded.position,
                total_teams = excluded.total_teams,
                lottery_text = excluded.lottery_text,
                podlutki_text = excluded.podlutki_text,
                submitted_by = excluded.submitted_by,
                submitted_username = excluded.submitted_username,
                submitted_name = excluded.submitted_name,
                updated_at = CURRENT_TIMESTAMP
            """,
            values,
        )
        return existed


def get_result(db_path: str, game_id: int) -> dict[str, Any] | None:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM game_results WHERE game_id = ?", (game_id,)
        ).fetchone()
        return dict(row) if row else None


def list_recent_results(db_path: str, limit: int = 10) -> list[dict[str, Any]]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT g.id AS game_id, g.title, g.game_date,
                   gr.position, gr.total_teams, gr.lottery_text,
                   gr.podlutki_text, gr.updated_at,
                   (SELECT COUNT(*) FROM game_result_revisions rev
                    WHERE rev.game_id = g.id) AS revisions
            FROM game_results gr
            JOIN games g ON g.id = gr.game_id
            ORDER BY DATETIME(g.game_date) DESC, g.id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]


def month_bounds(month_key: str | None = None) -> tuple[str, str, str]:
    if month_key:
        try:
            start_date = datetime.strptime(month_key, "%Y-%m").date().replace(day=1)
        except ValueError as exc:
            raise ValueError("Месяц нужен в формате YYYY-MM.") from exc
    else:
        start_date = datetime.now(ZoneInfo(BOT_TIMEZONE)).date().replace(day=1)
    days = calendar.monthrange(start_date.year, start_date.month)[1]
    end_date = start_date.replace(day=days) + timedelta(days=1)
    return start_date.isoformat(), end_date.isoformat(), start_date.strftime("%Y-%m")


def previous_month_key(current: date) -> str:
    previous_day = current.replace(day=1) - timedelta(days=1)
    return previous_day.strftime("%Y-%m")


def build_monthly_stats(db_path: str, month_key: str | None = None) -> dict[str, Any]:
    start, end, normalized_key = month_bounds(month_key)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT g.id, g.title, gr.position, gr.total_teams,
                   gr.lottery_text, gr.podlutki_text,
                   (SELECT COUNT(*) FROM registrations r
                    WHERE r.game_id = g.id AND r.is_cancelled = 0) AS players
            FROM game_results gr
            JOIN games g ON g.id = gr.game_id
            WHERE DATE(g.game_date) >= DATE(?) AND DATE(g.game_date) < DATE(?)
            ORDER BY DATETIME(g.game_date), g.id
            """,
            (start, end),
        ).fetchall()
        unique_players = conn.execute(
            """
            SELECT COUNT(DISTINCT r.user_id)
            FROM registrations r
            JOIN games g ON g.id = r.game_id
            JOIN game_results gr ON gr.game_id = g.id
            WHERE r.is_cancelled = 0
              AND DATE(g.game_date) >= DATE(?) AND DATE(g.game_date) < DATE(?)
            """,
            (start, end),
        ).fetchone()[0]
    games = [dict(row) for row in rows]
    total_player_marks = sum(int(game["players"]) for game in games)
    average_players = (
        round(total_player_marks / len(games), 1) if games else 0
    )
    podiums = sum(1 for game in games if int(game["position"]) <= 3)
    lottery_wins = sum(_count_prize_entries(str(game["lottery_text"]), mentions=True) for game in games)
    podlutki_prizes = sum(
        _count_prize_entries(str(game["podlutki_text"]), mentions=False) for game in games
    )
    best = min(games, key=lambda game: int(game["position"])) if games else None
    topic_counts = Counter(_normalize_game_topic(str(game["title"])) for game in games)
    popular_topic = topic_counts.most_common(1)[0][0] if topic_counts else None
    return {
        "month": normalized_key,
        "games": games,
        "games_count": len(games),
        "average_players": average_players,
        "total_player_marks": total_player_marks,
        "unique_players": int(unique_players),
        "podiums": podiums,
        "lottery_wins": lottery_wins,
        "podlutki_prizes": podlutki_prizes,
        "best": best,
        "popular_topic": popular_topic,
    }


def format_monthly_card(stats: dict[str, Any]) -> str:
    month_text = datetime.strptime(stats["month"], "%Y-%m").strftime("%m.%Y")
    lines = [
        f"Итоги команды за {month_text}",
        f"Сыграно игр: {stats['games_count']}",
        f"Среднее количество игроков: {stats['average_players']}",
        f"Всего отметок участников: {stats['total_player_marks']}",
        f"Уникальных игроков: {stats['unique_players']}",
        f"Мест в топ-3: {stats['podiums']}",
        f"Побед в лотерее: {stats['lottery_wins']}",
        f"Призов «Подлутки»: {stats['podlutki_prizes']}",
    ]
    if stats.get("popular_topic"):
        lines.append(f"Чаще всего играли: {stats['popular_topic']}")
    best = stats.get("best")
    if best:
        lines.append(
            f"Лучший результат: {best['position']} место из {best['total_teams']} — {best['title']}"
        )
    if not stats["games_count"]:
        lines.append("Пока нет внесённых результатов за этот месяц.")
    return "\n".join(lines)


def build_personal_stats(
    db_path: str,
    user_id: int,
    username: str | None,
    period: str,
) -> str:
    if period == "all":
        where = "1 = 1"
        params: tuple[Any, ...] = (user_id, user_id)
        title = "Личная статистика за всё время"
    else:
        start, end, month_key = month_bounds(period if period != "current" else None)
        where = "DATE(g.game_date) >= DATE(?) AND DATE(g.game_date) < DATE(?)"
        params = (user_id, start, end, user_id)
        title = f"Личная статистика за {month_key}"

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        if period == "all":
            games = conn.execute(
                f"""
                SELECT g.id, g.title, gr.lottery_text
                FROM registrations mine
                JOIN games g ON g.id = mine.game_id
                LEFT JOIN game_results gr ON gr.game_id = g.id
                WHERE mine.user_id = ? AND mine.is_cancelled = 0 AND {where}
                ORDER BY DATETIME(g.game_date)
                """,
                (user_id,),
            ).fetchall()
            teammates = conn.execute(
                """
                SELECT COALESCE(NULLIF(r.username, ''), r.full_name) AS name, COUNT(*) AS games
                FROM registrations mine
                JOIN registrations r ON r.game_id = mine.game_id
                JOIN game_results gr ON gr.game_id = mine.game_id
                WHERE mine.user_id = ? AND mine.is_cancelled = 0
                  AND r.user_id != ? AND r.is_cancelled = 0
                GROUP BY r.user_id
                ORDER BY games DESC, name
                LIMIT 3
                """,
                params,
            ).fetchall()
        else:
            games = conn.execute(
                f"""
                SELECT g.id, g.title, gr.lottery_text
                FROM registrations mine
                JOIN games g ON g.id = mine.game_id
                LEFT JOIN game_results gr ON gr.game_id = g.id
                WHERE mine.user_id = ? AND mine.is_cancelled = 0 AND {where}
                ORDER BY DATETIME(g.game_date)
                """,
                (user_id, start, end),
            ).fetchall()
            teammates = conn.execute(
                f"""
                SELECT COALESCE(NULLIF(r.username, ''), r.full_name) AS name, COUNT(*) AS games
                FROM registrations mine
                JOIN registrations r ON r.game_id = mine.game_id
                JOIN games g ON g.id = mine.game_id
                JOIN game_results gr ON gr.game_id = mine.game_id
                WHERE mine.user_id = ? AND mine.is_cancelled = 0 AND {where}
                  AND r.user_id != ? AND r.is_cancelled = 0
                GROUP BY r.user_id
                ORDER BY games DESC, name
                LIMIT 3
                """,
                params,
            ).fetchall()

    title_counts = Counter(_normalize_game_topic(str(game["title"])) for game in games)
    favorite_topic = title_counts.most_common(1)[0][0] if title_counts else "пока нет данных"
    username_token = (username or "").lstrip("@").casefold()
    lottery_wins = 0
    if username_token:
        mention_pattern = re.compile(rf"(?<![\w])@?{re.escape(username_token)}(?![\w])", re.I)
        lottery_wins = sum(
            1
            for game in games
            if game["lottery_text"] and mention_pattern.search(str(game["lottery_text"]))
        )
    teammate_text = ", ".join(
        f"@{row['name']} ({row['games']})" if row["name"] and " " not in row["name"] else f"{row['name']} ({row['games']})"
        for row in teammates
        if row["name"]
    ) or "пока нет данных"
    return (
        f"{title}\n"
        f"Отмечался на игры: {len(games)}\n"
        f"Чаще выбирал: {favorite_topic}\n"
        f"Победы в лотерее: {lottery_wins}\n"
        f"Чаще всего играл вместе: {teammate_text}"
    )


def _normalize_game_topic(title: str) -> str:
    value = re.split(r"\s*[\[#(]", title, maxsplit=1)[0]
    value = re.sub(r"\b(?:spb|санкт-петербург)\b", "", value, flags=re.I)
    value = re.sub(r"(^|\s)[№#]?\d+(?=\s|$)", " ", value)
    value = re.sub(r"\s+", " ", value).strip(" -—")
    return value or title.strip() or "Игра"


def _count_prize_entries(value: str, mentions: bool) -> int:
    if value.strip().casefold() in {"", "ничего", "нет", "0", "-"}:
        return 0
    if mentions:
        mentioned_users = set(re.findall(r"@[A-Za-z0-9_]+", value))
        if mentioned_users:
            return len(mentioned_users)
    return len([part for part in value.split(";") if part.strip()])


def monthly_card_was_sent(db_path: str, chat_id: int, month_key: str) -> bool:
    with sqlite3.connect(db_path) as conn:
        return conn.execute(
            "SELECT 1 FROM monthly_card_deliveries WHERE chat_id = ? AND period_month = ?",
            (chat_id, month_key),
        ).fetchone() is not None


def mark_monthly_card_sent(db_path: str, chat_id: int, month_key: str) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO monthly_card_deliveries (chat_id, period_month) VALUES (?, ?)",
            (chat_id, month_key),
        )


def list_reactivation_candidates(
    db_path: str, current_date: date, inactivity_days: int = 45, limit: int = 3
) -> list[dict[str, Any]]:
    cutoff = (current_date - timedelta(days=inactivity_days)).isoformat()
    month_key = current_date.strftime("%Y-%m")
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT p.user_id, p.username, p.full_name, p.private_chat_id,
                   MAX(DATE(g.game_date)) AS last_game
            FROM user_profiles p
            JOIN registrations r ON r.user_id = p.user_id AND r.is_cancelled = 0
            JOIN games g ON g.id = r.game_id
            LEFT JOIN user_preferences pref ON pref.user_id = p.user_id
            WHERE p.private_chat_id IS NOT NULL
              AND COALESCE(pref.reactivation_enabled, 1) = 1
              AND NOT EXISTS (
                  SELECT 1 FROM registrations future_r
                  JOIN games future_g ON future_g.id = future_r.game_id
                  WHERE future_r.user_id = p.user_id AND future_r.is_cancelled = 0
                    AND DATE(future_g.game_date) >= DATE(?)
                    AND future_g.is_archived = 0
              )
              AND NOT EXISTS (
                  SELECT 1 FROM reactivation_deliveries d
                  WHERE d.user_id = p.user_id AND d.delivered_month = ?
              )
            GROUP BY p.user_id
            HAVING MAX(DATE(g.game_date)) <= DATE(?)
            ORDER BY MAX(DATE(g.game_date)) ASC
            LIMIT ?
            """,
            (current_date.isoformat(), month_key, cutoff, limit),
        ).fetchall()
        return [dict(row) for row in rows]


def mark_reactivation_sent(db_path: str, user_id: int, month_key: str) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO reactivation_deliveries (user_id, delivered_month) VALUES (?, ?)",
            (user_id, month_key),
        )


def get_latest_group_chat_id(db_path: str) -> int | None:
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT chat_id FROM polls ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return int(row[0]) if row else None


def set_reactivation_preference(db_path: str, user_id: int, enabled: bool) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO user_preferences (user_id, reactivation_enabled)
            VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                reactivation_enabled = excluded.reactivation_enabled,
                updated_at = CURRENT_TIMESTAMP
            """,
            (user_id, int(enabled)),
        )


def reconcile_poll_registrations(
    db_path: str,
    apply_changes: bool = True,
    current_date: date | None = None,
) -> dict[str, int]:
    today_key = (
        current_date or datetime.now(ZoneInfo(BOT_TIMEZONE)).date()
    ).isoformat()
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("BEGIN IMMEDIATE")
        orphan_answers = conn.execute(
            """
            SELECT COUNT(*) FROM poll_answers pa
            WHERE NOT EXISTS (SELECT 1 FROM polls p WHERE p.poll_id = pa.poll_id)
            """
        ).fetchone()[0]
        yes_answers = conn.execute(
            """
            SELECT p.game_id, pa.user_id, pa.username, pa.full_name,
                   COALESCE(o.poll_max_people, 9) AS capacity
            FROM poll_answers pa
            JOIN polls p ON p.poll_id = pa.poll_id
            JOIN games g ON g.id = p.game_id
            LEFT JOIN organizers o ON o.id = g.organizer_id
            WHERE pa.option_id = 0
              AND p.id = (SELECT MAX(latest.id) FROM polls latest WHERE latest.game_id = p.game_id)
              AND DATE(g.game_date) >= DATE(?)
            ORDER BY p.game_id, pa.id
            """,
            (today_key,),
        ).fetchall()
        missing: list[tuple[Any, ...]] = []
        missing_waitlist: list[tuple[Any, ...]] = []
        positions: dict[int, int] = {}
        for game_id, user_id, username, full_name, capacity in yes_answers:
            position = positions.get(game_id, 0)
            positions[game_id] = position + 1
            active = conn.execute(
                """
                SELECT 1 FROM registrations
                WHERE game_id = ? AND user_id = ? AND is_cancelled = 0
                """,
                (game_id, user_id),
            ).fetchone()
            waiting = conn.execute(
                "SELECT 1 FROM game_waitlist WHERE game_id = ? AND user_id = ?",
                (game_id, user_id),
            ).fetchone()
            if position < int(capacity):
                if active is None:
                    missing.append((game_id, user_id, username, full_name))
            elif active is None and waiting is None:
                missing_waitlist.append((game_id, user_id, username, full_name))
        stale = conn.execute(
            """
            SELECT DISTINCT r.game_id, r.user_id
            FROM registrations r
            JOIN polls p ON p.game_id = r.game_id
            JOIN games g ON g.id = r.game_id
            WHERE r.is_cancelled = 0
              AND p.id = (SELECT MAX(latest.id) FROM polls latest WHERE latest.game_id = p.game_id)
              AND DATE(g.game_date) >= DATE(?)
              AND EXISTS (
                  SELECT 1 FROM poll_answers any_answer
                  WHERE any_answer.poll_id = p.poll_id AND any_answer.user_id = r.user_id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM poll_answers yes_answer
                  WHERE yes_answer.poll_id = p.poll_id AND yes_answer.user_id = r.user_id
                    AND yes_answer.option_id = 0
              )
            """,
            (today_key,),
        ).fetchall()
        stale_waitlist = conn.execute(
            """
            SELECT DISTINCT w.game_id, w.user_id
            FROM game_waitlist w
            JOIN polls p ON p.game_id = w.game_id
            JOIN games g ON g.id = w.game_id
            WHERE p.id = (SELECT MAX(latest.id) FROM polls latest WHERE latest.game_id = p.game_id)
              AND DATE(g.game_date) >= DATE(?)
              AND EXISTS (
                  SELECT 1 FROM poll_answers pa
                  WHERE pa.poll_id = p.poll_id AND pa.user_id = w.user_id
                    AND pa.option_id != 0
              )
            """,
            (today_key,),
        ).fetchall()
        if apply_changes:
            for game_id, user_id, username, full_name in missing:
                conn.execute(
                    """
                    INSERT INTO registrations (game_id, user_id, username, full_name)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(game_id, user_id) DO UPDATE SET
                        username = COALESCE(excluded.username, registrations.username),
                        full_name = COALESCE(excluded.full_name, registrations.full_name),
                        is_cancelled = 0,
                        cancelled_at = NULL
                    """,
                    (game_id, user_id, username, full_name),
                )
                conn.execute(
                    "DELETE FROM game_waitlist WHERE game_id = ? AND user_id = ?",
                    (game_id, user_id),
                )
            for game_id, user_id, username, full_name in missing_waitlist:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO game_waitlist
                        (game_id, user_id, username, full_name)
                    VALUES (?, ?, ?, ?)
                    """,
                    (game_id, user_id, username, full_name),
                )
            for game_id, user_id in stale:
                conn.execute(
                    """
                    UPDATE registrations SET is_cancelled = 1, cancelled_at = CURRENT_TIMESTAMP
                    WHERE game_id = ? AND user_id = ? AND is_cancelled = 0
                    """,
                    (game_id, user_id),
                )
            for game_id, user_id in stale_waitlist:
                conn.execute(
                    "DELETE FROM game_waitlist WHERE game_id = ? AND user_id = ?",
                    (game_id, user_id),
                )
        return {
            "orphan_answers": int(orphan_answers),
            "restored_registrations": len(missing),
            "restored_waitlist": len(missing_waitlist),
            "cancelled_stale_registrations": len(stale),
            "removed_stale_waitlist": len(stale_waitlist),
        }


def get_setting(db_path: str, key: str, default: str | None = None) -> str | None:
    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT value FROM bot_settings WHERE key = ?", (key,)).fetchone()
        return str(row[0]) if row else default


def get_settings(db_path: str, keys: tuple[str, ...]) -> dict[str, str]:
    if not keys:
        return {}
    placeholders = ",".join("?" for _ in keys)
    with sqlite3.connect(db_path) as conn:
        return {
            str(key): str(value)
            for key, value in conn.execute(
                f"SELECT key, value FROM bot_settings WHERE key IN ({placeholders})",
                keys,
            ).fetchall()
        }


def set_setting(db_path: str, key: str, value: str, updated_by: int | None) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO bot_settings (key, value, updated_by)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_by = excluded.updated_by,
                updated_at = CURRENT_TIMESTAMP
            """,
            (key, value, updated_by),
        )
