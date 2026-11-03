from __future__ import annotations

import json
import sqlite3
from urllib.parse import urlparse
from typing import Dict, List, Optional, Tuple

from product_features import init_product_features


def init_db(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA busy_timeout = 5000;")
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS games (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                game_date TEXT,
                description TEXT,
                restaurant_name TEXT,
                restaurant_address TEXT,
                restaurant_lat REAL,
                restaurant_lon REAL,
                game_number INTEGER,
                lottery_id INTEGER,
                external_id TEXT,
                registration_external_id TEXT,
                organizer_id INTEGER,
                is_archived INTEGER DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (organizer_id) REFERENCES organizers(id)
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS organizers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                site_url TEXT,
                game_url_template TEXT,
                api_view_template TEXT,
                registration_api_template TEXT,
                registration_payload TEXT,
                api_parser TEXT,
                poll_max_people INTEGER,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS registrations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                game_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                username TEXT,
                full_name TEXT,
                is_cancelled INTEGER DEFAULT 0,
                cancelled_at TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (game_id, user_id),
                FOREIGN KEY (game_id) REFERENCES games(id) ON DELETE CASCADE
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS game_waitlist (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                game_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                username TEXT,
                full_name TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (game_id, user_id),
                FOREIGN KEY (game_id) REFERENCES games(id) ON DELETE CASCADE
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id INTEGER,
                chat_id INTEGER,
                user_id INTEGER,
                username TEXT,
                full_name TEXT,
                content_type TEXT,
                text TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS admins (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                approved_by TEXT,
                approved_at TEXT DEFAULT CURRENT_TIMESTAMP,
                added_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS admin_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL UNIQUE,
                username TEXT,
                full_name TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS polls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                poll_id TEXT NOT NULL UNIQUE,
                game_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                created_by INTEGER,
                yes_notified INTEGER DEFAULT 0,
                full_notified INTEGER DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (game_id) REFERENCES games(id) ON DELETE CASCADE
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS poll_answers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                poll_id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                username TEXT,
                full_name TEXT,
                option_id INTEGER NOT NULL,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (poll_id, user_id)
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS reminder_deliveries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                game_id INTEGER NOT NULL,
                reminder_type TEXT NOT NULL,
                scheduled_date TEXT NOT NULL,
                sent_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (game_id, reminder_type, scheduled_date),
                FOREIGN KEY (game_id) REFERENCES games(id) ON DELETE CASCADE
            );
            """
        )
        conn.commit()
        _ensure_games_columns(conn)
        _ensure_registrations_columns(conn)
        _ensure_admin_columns(conn)
        _ensure_polls_columns(conn)
        _ensure_poll_answers_columns(conn)
        _ensure_organizers_columns(conn)
        init_product_features(conn)
    finally:
        conn.close()


def record_poll(
    db_path: str,
    poll_id: str,
    game_id: int,
    chat_id: int,
    message_id: int,
    created_by: int | None,
) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT OR IGNORE INTO polls (poll_id, game_id, chat_id, message_id, created_by)
            VALUES (?, ?, ?, ?, ?);
            """,
            (poll_id, game_id, chat_id, message_id, created_by),
        )
        conn.commit()
    finally:
        conn.close()


def record_poll_answer(
    db_path: str,
    poll_id: str,
    user_id: int,
    option_id: int,
    username: Optional[str],
    full_name: str,
) -> bool:
    conn = sqlite3.connect(db_path)
    try:
        if conn.execute(
            "SELECT 1 FROM polls WHERE poll_id = ?;", (poll_id,)
        ).fetchone() is None:
            return False
        conn.execute(
            """
            INSERT INTO poll_answers (poll_id, user_id, option_id, username, full_name)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(poll_id, user_id)
            DO UPDATE SET
                option_id = excluded.option_id,
                username = excluded.username,
                full_name = excluded.full_name,
                updated_at = CURRENT_TIMESTAMP;
            """,
            (poll_id, user_id, option_id, username, full_name),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def delete_poll_answer(db_path: str, poll_id: str, user_id: int) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "DELETE FROM poll_answers WHERE poll_id = ? AND user_id = ?;",
            (poll_id, user_id),
        )
        conn.commit()
    finally:
        conn.close()


def get_poll_by_id(db_path: str, poll_id: str) -> Optional[Dict[str, int]]:
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            """
            SELECT poll_id, game_id, chat_id, message_id, yes_notified, full_notified
            FROM polls
            WHERE poll_id = ?;
            """,
            (poll_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return {
            "poll_id": row[0],
            "game_id": row[1],
            "chat_id": row[2],
            "message_id": row[3],
            "yes_notified": bool(row[4]),
            "full_notified": bool(row[5]),
        }
    finally:
        conn.close()


def mark_poll_notified(db_path: str, poll_id: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE polls SET yes_notified = 1 WHERE poll_id = ?;",
            (poll_id,),
        )
        conn.commit()
    finally:
        conn.close()


def mark_poll_full_notified(db_path: str, poll_id: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE polls SET full_notified = 1 WHERE poll_id = ?;",
            (poll_id,),
        )
        conn.commit()
    finally:
        conn.close()


def clear_poll_full_notified(db_path: str, poll_id: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE polls SET full_notified = 0 WHERE poll_id = ?;",
            (poll_id,),
        )
        conn.commit()
    finally:
        conn.close()


def get_poll_yes_count(db_path: str, poll_id: str, yes_option_id: int = 0) -> int:
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            """
            SELECT COUNT(*)
            FROM poll_answers
            WHERE poll_id = ? AND option_id = ?;
            """,
            (poll_id, yes_option_id),
        )
        row = cur.fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


def get_poll_participants(db_path: str, poll_id: str) -> List[Dict[str, Optional[str]]]:
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            """
            SELECT username, full_name, user_id
            FROM poll_answers
            WHERE poll_id = ?
            ORDER BY updated_at ASC, id ASC;
            """,
            (poll_id,),
        )
        return [
            {
                "username": row[0],
                "full_name": row[1],
                "user_id": row[2],
            }
            for row in cur.fetchall()
        ]
    finally:
        conn.close()

def get_game_id_by_external_id(db_path: str, external_id: str) -> Optional[int]:
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            """
            SELECT id
            FROM games
            WHERE external_id = ?
              AND is_archived = 0
            ORDER BY DATETIME(created_at) DESC, id DESC
            LIMIT 1;
            """,
            (external_id,),
        )
        row = cur.fetchone()
        return int(row[0]) if row else None
    finally:
        conn.close()


def get_poll_by_external_id(db_path: str, external_id: str) -> Optional[Dict[str, int]]:
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            """
            SELECT p.poll_id, p.game_id, p.chat_id, p.message_id, p.yes_notified
            FROM polls p
            JOIN games g ON g.id = p.game_id
            WHERE g.external_id = ?
              AND g.is_archived = 0
            ORDER BY DATETIME(p.created_at) DESC, p.id DESC
            LIMIT 1;
            """,
            (external_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return {
            "poll_id": row[0],
            "game_id": row[1],
            "chat_id": row[2],
            "message_id": row[3],
            "yes_notified": bool(row[4]),
        }
    finally:
        conn.close()


def get_latest_poll_chat_id_for_game(db_path: str, game_id: int) -> Optional[int]:
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            """
            SELECT chat_id
            FROM polls
            WHERE game_id = ?
            ORDER BY id DESC
            LIMIT 1;
            """,
            (game_id,),
        )
        row = cur.fetchone()
        return int(row[0]) if row else None
    finally:
        conn.close()


def get_latest_poll_for_game(db_path: str, game_id: int) -> Optional[Dict[str, int]]:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            """
            SELECT poll_id, chat_id, message_id
            FROM polls
            WHERE game_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (game_id,),
        ).fetchone()
        if row is None:
            return None
        return {"poll_id": row[0], "chat_id": int(row[1]), "message_id": int(row[2])}
    finally:
        conn.close()


def add_game(
    db_path: str,
    title: str,
    game_date: Optional[str],
    description: Optional[str] = None,
    restaurant_name: Optional[str] = None,
    restaurant_address: Optional[str] = None,
    restaurant_lat: Optional[float] = None,
    restaurant_lon: Optional[float] = None,
    game_number: Optional[int] = None,
    lottery_id: Optional[int] = None,
    external_id: Optional[str] = None,
    registration_external_id: Optional[str] = None,
    organizer_id: Optional[int] = None,
) -> int:
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            """
            INSERT INTO games (
                title,
                game_date,
                description,
                restaurant_name,
                restaurant_address,
                restaurant_lat,
                restaurant_lon,
                game_number,
                lottery_id,
                external_id,
                registration_external_id,
                organizer_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                title,
                game_date,
                description,
                restaurant_name,
                restaurant_address,
                restaurant_lat,
                restaurant_lon,
                game_number,
                lottery_id,
                external_id,
                registration_external_id,
                organizer_id,
            ),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def delete_game(db_path: str, game_id: int) -> bool:
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute("DELETE FROM games WHERE id = ?;", (game_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def list_games(db_path: str) -> List[Dict[str, Optional[str]]]:
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            """
            SELECT g.id, g.title, g.game_date, COUNT(r.id) AS registrations
            FROM games g
            LEFT JOIN registrations r ON r.game_id = g.id AND r.is_cancelled = 0
            GROUP BY g.id, g.title, g.game_date
            ORDER BY g.id DESC;
            """
        )
        return [
            {
                "id": row[0],
                "title": row[1],
                "game_date": row[2],
                "registrations": row[3],
            }
            for row in cur.fetchall()
        ]
    finally:
        conn.close()


def list_upcoming_games(
    db_path: str, limit: int, offset: int = 0
) -> List[Dict[str, Optional[str]]]:
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            """
            SELECT id, title, game_date
            FROM games
            WHERE game_date IS NOT NULL
              AND DATE(game_date) >= DATE('now', '+3 hours')
              AND is_archived = 0
            ORDER BY DATETIME(game_date) ASC, id ASC
            LIMIT ? OFFSET ?;
            """,
            (limit, offset),
        )
        return [
            {"id": row[0], "title": row[1], "game_date": row[2]}
            for row in cur.fetchall()
        ]
    finally:
        conn.close()


def list_upcoming_games_with_counts(
    db_path: str, user_id: int
) -> List[Dict[str, Optional[str]]]:
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            """
            SELECT
                g.id,
                g.title,
                g.game_date,
                g.description,
                g.restaurant_name,
                g.restaurant_address,
                g.restaurant_lat,
                g.restaurant_lon,
                g.external_id,
                o.game_url_template,
                (
                    SELECT COUNT(*)
                    FROM registrations r
                    WHERE r.game_id = g.id AND r.is_cancelled = 0
                ) AS registrations,
                COALESCE(o.poll_max_people, 9) AS capacity,
                (
                    SELECT COUNT(*) FROM game_waitlist w_count
                    WHERE w_count.game_id = g.id
                ) AS waitlist_count,
                EXISTS(
                    SELECT 1
                    FROM registrations r2
                    WHERE r2.game_id = g.id AND r2.user_id = ? AND r2.is_cancelled = 0
                ) AS is_registered,
                EXISTS(
                    SELECT 1
                    FROM game_waitlist w
                    WHERE w.game_id = g.id AND w.user_id = ?
                ) AS is_waitlisted,
                EXISTS(
                    SELECT 1
                    FROM poll_answers pa
                    JOIN polls p ON p.poll_id = pa.poll_id
                    WHERE p.game_id = g.id
                      AND pa.user_id = ?
                      AND pa.option_id = 2
                      AND p.id = (SELECT MAX(latest.id) FROM polls latest WHERE latest.game_id = g.id)
                ) AS is_maybe
            FROM games g
            LEFT JOIN organizers o ON o.id = g.organizer_id
            WHERE g.game_date IS NOT NULL
              AND DATE(g.game_date) >= DATE('now', '+3 hours')
              AND g.is_archived = 0
            ORDER BY DATETIME(g.game_date) ASC, g.id ASC;
            """,
            (user_id, user_id, user_id),
        )
        return [
            {
                "id": row[0],
                "title": row[1],
                "game_date": row[2],
                "description": row[3],
                "restaurant_name": row[4],
                "restaurant_address": row[5],
                "restaurant_lat": row[6],
                "restaurant_lon": row[7],
                "external_id": row[8],
                "game_url_template": row[9],
                "registrations": row[10],
                "capacity": row[11],
                "waitlist_count": row[12],
                "is_registered": bool(row[13]),
                "is_waitlisted": bool(row[14]),
                "is_maybe": bool(row[15]),
            }
            for row in cur.fetchall()
        ]
    finally:
        conn.close()


def list_upcoming_games_with_counts_limit(
    db_path: str, user_id: int, limit: int, offset: int
) -> List[Dict[str, Optional[str]]]:
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            """
            SELECT
                g.id,
                g.title,
                g.game_date,
                g.description,
                g.restaurant_name,
                g.restaurant_address,
                g.restaurant_lat,
                g.restaurant_lon,
                g.external_id,
                o.game_url_template,
                (
                    SELECT COUNT(*)
                    FROM registrations r
                    WHERE r.game_id = g.id AND r.is_cancelled = 0
                ) AS registrations,
                COALESCE(o.poll_max_people, 9) AS capacity,
                (
                    SELECT COUNT(*) FROM game_waitlist w_count
                    WHERE w_count.game_id = g.id
                ) AS waitlist_count,
                EXISTS(
                    SELECT 1
                    FROM registrations r2
                    WHERE r2.game_id = g.id AND r2.user_id = ? AND r2.is_cancelled = 0
                ) AS is_registered,
                EXISTS(
                    SELECT 1
                    FROM game_waitlist w
                    WHERE w.game_id = g.id AND w.user_id = ?
                ) AS is_waitlisted,
                EXISTS(
                    SELECT 1
                    FROM poll_answers pa
                    JOIN polls p ON p.poll_id = pa.poll_id
                    WHERE p.game_id = g.id
                      AND pa.user_id = ?
                      AND pa.option_id = 2
                      AND p.id = (SELECT MAX(latest.id) FROM polls latest WHERE latest.game_id = g.id)
                ) AS is_maybe
            FROM games g
            LEFT JOIN organizers o ON o.id = g.organizer_id
            WHERE g.game_date IS NOT NULL
              AND DATE(g.game_date) >= DATE('now', '+3 hours')
              AND g.is_archived = 0
            ORDER BY DATETIME(g.game_date) ASC, g.id ASC
            LIMIT ? OFFSET ?;
            """,
            (user_id, user_id, user_id, limit, offset),
        )
        return [
            {
                "id": row[0],
                "title": row[1],
                "game_date": row[2],
                "description": row[3],
                "restaurant_name": row[4],
                "restaurant_address": row[5],
                "restaurant_lat": row[6],
                "restaurant_lon": row[7],
                "external_id": row[8],
                "game_url_template": row[9],
                "registrations": row[10],
                "capacity": row[11],
                "waitlist_count": row[12],
                "is_registered": bool(row[13]),
                "is_waitlisted": bool(row[14]),
                "is_maybe": bool(row[15]),
            }
            for row in cur.fetchall()
        ]
    finally:
        conn.close()


def count_upcoming_games(db_path: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            """
            SELECT COUNT(*)
            FROM games
            WHERE game_date IS NOT NULL
              AND DATE(game_date) >= DATE('now', '+3 hours')
              AND is_archived = 0;
            """
        )
        row = cur.fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


def get_upcoming_game_with_counts(
    db_path: str, user_id: int, game_id: int
) -> Optional[Dict[str, Optional[str]]]:
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            """
            SELECT
                g.id,
                g.title,
                g.game_date,
                g.description,
                g.restaurant_name,
                g.restaurant_address,
                g.restaurant_lat,
                g.restaurant_lon,
                g.external_id,
                o.game_url_template,
                (
                    SELECT COUNT(*)
                    FROM registrations r
                    WHERE r.game_id = g.id AND r.is_cancelled = 0
                ) AS registrations,
                COALESCE(o.poll_max_people, 9) AS capacity,
                (
                    SELECT COUNT(*) FROM game_waitlist w_count
                    WHERE w_count.game_id = g.id
                ) AS waitlist_count,
                EXISTS(
                    SELECT 1
                    FROM registrations r2
                    WHERE r2.game_id = g.id AND r2.user_id = ? AND r2.is_cancelled = 0
                ) AS is_registered,
                EXISTS(
                    SELECT 1
                    FROM game_waitlist w
                    WHERE w.game_id = g.id AND w.user_id = ?
                ) AS is_waitlisted,
                EXISTS(
                    SELECT 1
                    FROM poll_answers pa
                    JOIN polls p ON p.poll_id = pa.poll_id
                    WHERE p.game_id = g.id
                      AND pa.user_id = ?
                      AND pa.option_id = 2
                      AND p.id = (SELECT MAX(latest.id) FROM polls latest WHERE latest.game_id = g.id)
                ) AS is_maybe
            FROM games g
            LEFT JOIN organizers o ON o.id = g.organizer_id
            WHERE g.id = ?
              AND g.game_date IS NOT NULL
              AND DATE(g.game_date) >= DATE('now', '+3 hours')
              AND g.is_archived = 0
            """,
            (user_id, user_id, user_id, game_id),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return {
            "id": row[0],
            "title": row[1],
            "game_date": row[2],
            "description": row[3],
            "restaurant_name": row[4],
            "restaurant_address": row[5],
            "restaurant_lat": row[6],
            "restaurant_lon": row[7],
            "external_id": row[8],
            "game_url_template": row[9],
            "registrations": row[10],
            "capacity": row[11],
            "waitlist_count": row[12],
            "is_registered": bool(row[13]),
            "is_waitlisted": bool(row[14]),
            "is_maybe": bool(row[15]),
        }
    finally:
        conn.close()


def register_player(
    db_path: str,
    game_id: int,
    user_id: int,
    username: Optional[str],
    full_name: str,
) -> Tuple[bool, str]:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON;")
        cur = conn.execute(
            "SELECT title, game_date, is_archived FROM games WHERE id = ?;",
            (game_id,),
        )
        row = cur.fetchone()
        if row is None:
            return False, "Игра с таким номером не найдена."
        title, game_date, is_archived = row
        if is_archived:
            return False, "Игра отменена."

        try:
            cur = conn.execute(
                """
                SELECT id, is_cancelled
                FROM registrations
                WHERE game_id = ? AND user_id = ?;
                """,
                (game_id, user_id),
            )
            existing = cur.fetchone()
            if existing is not None:
                if existing[1]:
                    conn.execute(
                        """
                        UPDATE registrations
                        SET is_cancelled = 0, cancelled_at = NULL
                        WHERE id = ?;
                        """,
                        (existing[0],),
                    )
                else:
                    return False, "Вы уже записаны на эту игру."
            else:
                conn.execute(
                    """
                    INSERT INTO registrations (game_id, user_id, username, full_name)
                    VALUES (?, ?, ?, ?);
                    """,
                    (game_id, user_id, username, full_name),
                )
        except sqlite3.IntegrityError:
            return False, "Вы уже записаны на эту игру."

        conn.commit()
        game_date_text = f" ({game_date})" if game_date else ""
        conn.execute(
            "DELETE FROM game_waitlist WHERE game_id = ? AND user_id = ?;",
            (game_id, user_id),
        )
        conn.commit()
        return True, ""
    finally:
        conn.close()


def unregister_player(db_path: str, game_id: int, user_id: int) -> Tuple[bool, str]:
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            """
            UPDATE registrations
            SET is_cancelled = 1, cancelled_at = CURRENT_TIMESTAMP
            WHERE game_id = ? AND user_id = ? AND is_cancelled = 0;
            """,
            (game_id, user_id),
        )
        conn.commit()
        if cur.rowcount == 0:
            return False, "Запись не найдена."
        return True, ""
    finally:
        conn.close()


def add_waitlist(
    db_path: str,
    game_id: int,
    user_id: int,
    username: Optional[str],
    full_name: str,
) -> Tuple[bool, str]:
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute("SELECT id, is_archived FROM games WHERE id = ?;", (game_id,))
        row = cur.fetchone()
        if row is None:
            return False, "Игра с таким номером не найдена."
        if row[1]:
            return False, "Игра отменена."
        cur = conn.execute(
            """
            SELECT 1
            FROM registrations
            WHERE game_id = ? AND user_id = ? AND is_cancelled = 0
            LIMIT 1;
            """,
            (game_id, user_id),
        )
        if cur.fetchone() is not None:
            return False, "Вы уже записаны на эту игру."
        try:
            conn.execute(
                """
                INSERT INTO game_waitlist (game_id, user_id, username, full_name)
                VALUES (?, ?, ?, ?);
                """,
                (game_id, user_id, username, full_name),
            )
        except sqlite3.IntegrityError:
            return False, "Вы уже в списке ожидания."
        conn.commit()
        return True, "Добавил в список ожидания."
    finally:
        conn.close()


def get_waitlist_first(db_path: str, game_id: int) -> Optional[Dict[str, Optional[str]]]:
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            """
            SELECT user_id, username, full_name
            FROM game_waitlist
            WHERE game_id = ?
            ORDER BY DATETIME(created_at) ASC, id ASC
            LIMIT 1;
            """,
            (game_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return {"user_id": row[0], "username": row[1], "full_name": row[2]}
    finally:
        conn.close()


def remove_waitlist_entry(db_path: str, game_id: int, user_id: int) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "DELETE FROM game_waitlist WHERE game_id = ? AND user_id = ?;",
            (game_id, user_id),
        )
        conn.commit()
    finally:
        conn.close()


def list_user_games(db_path: str, user_id: int) -> List[Dict[str, Optional[str]]]:
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            """
            SELECT g.id, g.title, g.game_date
            FROM registrations r
            JOIN games g ON g.id = r.game_id
            WHERE r.user_id = ?
              AND g.game_date IS NOT NULL
              AND DATE(g.game_date) >= DATE('now', '+3 hours')
              AND g.is_archived = 0
              AND r.is_cancelled = 0
            ORDER BY DATETIME(g.game_date) ASC, g.id ASC;
            """,
            (user_id,),
        )
        return [
            {"id": row[0], "title": row[1], "game_date": row[2]}
            for row in cur.fetchall()
        ]
    finally:
        conn.close()


def _ensure_games_columns(conn: sqlite3.Connection) -> None:
    cur = conn.execute("PRAGMA table_info(games);")
    columns = {row[1] for row in cur.fetchall()}
    if "description" not in columns:
        conn.execute("ALTER TABLE games ADD COLUMN description TEXT;")
    if "restaurant_name" not in columns:
        conn.execute("ALTER TABLE games ADD COLUMN restaurant_name TEXT;")
    if "restaurant_address" not in columns:
        conn.execute("ALTER TABLE games ADD COLUMN restaurant_address TEXT;")
    if "restaurant_lat" not in columns:
        conn.execute("ALTER TABLE games ADD COLUMN restaurant_lat REAL;")
    if "restaurant_lon" not in columns:
        conn.execute("ALTER TABLE games ADD COLUMN restaurant_lon REAL;")
    if "game_number" not in columns:
        conn.execute("ALTER TABLE games ADD COLUMN game_number INTEGER;")
    if "lottery_id" not in columns:
        conn.execute("ALTER TABLE games ADD COLUMN lottery_id INTEGER;")
    if "external_id" not in columns:
        conn.execute("ALTER TABLE games ADD COLUMN external_id TEXT;")
    if "registration_external_id" not in columns:
        conn.execute("ALTER TABLE games ADD COLUMN registration_external_id TEXT;")
    if "organizer_id" not in columns:
        conn.execute("ALTER TABLE games ADD COLUMN organizer_id INTEGER;")
    if "is_archived" not in columns:
        conn.execute("ALTER TABLE games ADD COLUMN is_archived INTEGER DEFAULT 0;")


def _ensure_registrations_columns(conn: sqlite3.Connection) -> None:
    cur = conn.execute("PRAGMA table_info(registrations);")
    columns = {row[1] for row in cur.fetchall()}
    if "is_cancelled" not in columns:
        conn.execute("ALTER TABLE registrations ADD COLUMN is_cancelled INTEGER DEFAULT 0;")
    if "cancelled_at" not in columns:
        conn.execute("ALTER TABLE registrations ADD COLUMN cancelled_at TEXT;")


def _ensure_admin_columns(conn: sqlite3.Connection) -> None:
    cur = conn.execute("PRAGMA table_info(admins);")
    columns = {row[1] for row in cur.fetchall()}
    if "approved_by" not in columns:
        conn.execute("ALTER TABLE admins ADD COLUMN approved_by TEXT;")
    if "approved_at" not in columns:
        conn.execute("ALTER TABLE admins ADD COLUMN approved_at TEXT;")


def _ensure_polls_columns(conn: sqlite3.Connection) -> None:
    cur = conn.execute("PRAGMA table_info(polls);")
    columns = {row[1] for row in cur.fetchall()}
    if "full_notified" not in columns:
        conn.execute("ALTER TABLE polls ADD COLUMN full_notified INTEGER DEFAULT 0;")


def _ensure_poll_answers_columns(conn: sqlite3.Connection) -> None:
    cur = conn.execute("PRAGMA table_info(poll_answers);")
    columns = {row[1] for row in cur.fetchall()}
    if "username" not in columns:
        conn.execute("ALTER TABLE poll_answers ADD COLUMN username TEXT;")
    if "full_name" not in columns:
        conn.execute("ALTER TABLE poll_answers ADD COLUMN full_name TEXT;")


def _ensure_organizers_columns(conn: sqlite3.Connection) -> None:
    cur = conn.execute("PRAGMA table_info(organizers);")
    columns = {row[1] for row in cur.fetchall()}
    if "site_url" not in columns:
        conn.execute("ALTER TABLE organizers ADD COLUMN site_url TEXT;")
    if "game_url_template" not in columns:
        conn.execute("ALTER TABLE organizers ADD COLUMN game_url_template TEXT;")
    if "api_view_template" not in columns:
        conn.execute("ALTER TABLE organizers ADD COLUMN api_view_template TEXT;")
    if "registration_api_template" not in columns:
        conn.execute("ALTER TABLE organizers ADD COLUMN registration_api_template TEXT;")
    if "registration_payload" not in columns:
        conn.execute("ALTER TABLE organizers ADD COLUMN registration_payload TEXT;")
    if "api_parser" not in columns:
        conn.execute("ALTER TABLE organizers ADD COLUMN api_parser TEXT;")
    if "poll_max_people" not in columns:
        conn.execute("ALTER TABLE organizers ADD COLUMN poll_max_people INTEGER;")


def seed_organizers(db_path: str, organizers: list[dict]) -> None:
    if not organizers:
        return
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON;")
        for organizer in organizers:
            payload = organizer.get("registration_payload")
            payload_json = (
                json.dumps(payload, ensure_ascii=False) if isinstance(payload, dict) else payload
            )
            conn.execute(
                """
                INSERT INTO organizers (
                    name,
                    site_url,
                    game_url_template,
                    api_view_template,
                    registration_api_template,
                    registration_payload,
                    api_parser,
                    poll_max_people
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(name) DO NOTHING;
                """,
                (
                    organizer.get("name"),
                    organizer.get("site_url"),
                    organizer.get("game_url_template"),
                    organizer.get("api_view_template"),
                    organizer.get("registration_api_template"),
                    payload_json,
                    organizer.get("api_parser"),
                    organizer.get("poll_max_people"),
                ),
            )
            if organizer.get("poll_max_people") is not None:
                conn.execute(
                    """
                    UPDATE organizers
                    SET poll_max_people = ?
                    WHERE name = ? AND (poll_max_people IS NULL OR poll_max_people = 0);
                    """,
                    (organizer.get("poll_max_people"), organizer.get("name")),
                )
        conn.commit()
    finally:
        conn.close()


def get_default_organizer(db_path: str) -> Optional[Dict[str, Optional[str]]]:
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            """
            SELECT id, name, site_url, game_url_template, api_view_template,
                   registration_api_template, registration_payload, api_parser, poll_max_people
            FROM organizers
            ORDER BY id ASC
            LIMIT 1;
            """
        )
        row = cur.fetchone()
        if row is None:
            return None
        payload = row[6]
        parsed_payload = None
        if payload:
            try:
                parsed_payload = json.loads(payload)
            except json.JSONDecodeError:
                parsed_payload = None
        return {
            "id": row[0],
            "name": row[1],
            "site_url": row[2],
            "game_url_template": row[3],
            "api_view_template": row[4],
            "registration_api_template": row[5],
            "registration_payload": parsed_payload,
            "api_parser": row[7],
            "poll_max_people": row[8],
        }
    finally:
        conn.close()


def get_organizer_for_external_id(
    db_path: str, external_id: str
) -> Optional[Dict[str, Optional[str]]]:
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            """
            SELECT o.id, o.name, o.site_url, o.game_url_template, o.api_view_template,
                   o.registration_api_template, o.registration_payload, o.api_parser, o.poll_max_people
            FROM games g
            JOIN organizers o ON o.id = g.organizer_id
            WHERE g.external_id = ?
              AND g.is_archived = 0
            ORDER BY DATETIME(g.created_at) DESC, g.id DESC
            LIMIT 1;
            """,
            (external_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        payload = row[6]
        parsed_payload = None
        if payload:
            try:
                parsed_payload = json.loads(payload)
            except json.JSONDecodeError:
                parsed_payload = None
        return {
            "id": row[0],
            "name": row[1],
            "site_url": row[2],
            "game_url_template": row[3],
            "api_view_template": row[4],
            "registration_api_template": row[5],
            "registration_payload": parsed_payload,
            "api_parser": row[7],
            "poll_max_people": row[8],
        }
    finally:
        conn.close()


def get_organizer_by_id(
    db_path: str, organizer_id: int
) -> Optional[Dict[str, Optional[str]]]:
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            """
            SELECT id, name, site_url, game_url_template, api_view_template,
                   registration_api_template, registration_payload, api_parser, poll_max_people
            FROM organizers
            WHERE id = ?
            LIMIT 1;
            """,
            (organizer_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        payload = row[6]
        parsed_payload = None
        if payload:
            try:
                parsed_payload = json.loads(payload)
            except json.JSONDecodeError:
                parsed_payload = None
        return {
            "id": row[0],
            "name": row[1],
            "site_url": row[2],
            "game_url_template": row[3],
            "api_view_template": row[4],
            "registration_api_template": row[5],
            "registration_payload": parsed_payload,
            "api_parser": row[7],
            "poll_max_people": row[8],
        }
    finally:
        conn.close()


def get_game_registration_info_by_external_id(
    db_path: str, external_id: str
) -> Optional[Dict[str, Optional[str]]]:
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            """
            SELECT g.id, g.registration_external_id, g.organizer_id, g.restaurant_name
            FROM games g
            WHERE g.external_id = ?
              AND g.is_archived = 0
            ORDER BY DATETIME(g.created_at) DESC, g.id DESC
            LIMIT 1;
            """,
            (external_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return {
            "game_id": row[0],
            "registration_external_id": row[1],
            "organizer_id": row[2],
            "restaurant_name": row[3],
        }
    finally:
        conn.close()


def get_poll_max_people_for_game_id(db_path: str, game_id: int) -> Optional[int]:
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            """
            SELECT o.poll_max_people
            FROM games g
            JOIN organizers o ON o.id = g.organizer_id
            WHERE g.id = ?
            LIMIT 1;
            """,
            (game_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return row[0]
    finally:
        conn.close()


def _normalize_url(value: str | None) -> str:
    if not value:
        return ""
    return value.strip().rstrip("/")


def _template_prefix(template: str | None) -> str:
    if not template:
        return ""
    prefix = template.split("{", 1)[0].rstrip("/")
    return prefix


def get_organizer_by_link(
    db_path: str, link: str
) -> Optional[Dict[str, Optional[str]]]:
    parsed = urlparse(link)
    if not parsed.scheme or not parsed.netloc:
        return None
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            """
            SELECT id, name, site_url, game_url_template, api_view_template,
                   registration_api_template, registration_payload, api_parser, poll_max_people
            FROM organizers
            ORDER BY id ASC;
            """
        )
        for row in cur.fetchall():
            site_url = _normalize_url(row[2])
            game_template = row[3]
            if site_url and _normalize_url(f"{parsed.scheme}://{parsed.netloc}") == site_url:
                payload = row[6]
                parsed_payload = None
                if payload:
                    try:
                        parsed_payload = json.loads(payload)
                    except json.JSONDecodeError:
                        parsed_payload = None
                return {
                    "id": row[0],
                    "name": row[1],
                    "site_url": row[2],
                    "game_url_template": row[3],
                    "api_view_template": row[4],
                    "registration_api_template": row[5],
                    "registration_payload": parsed_payload,
                    "api_parser": row[7],
                    "poll_max_people": row[8],
                }
            prefix = _template_prefix(game_template)
            normalized_link = _normalize_url(link)
            if prefix and (normalized_link.startswith(prefix) or prefix in normalized_link):
                payload = row[6]
                parsed_payload = None
                if payload:
                    try:
                        parsed_payload = json.loads(payload)
                    except json.JSONDecodeError:
                        parsed_payload = None
                return {
                    "id": row[0],
                    "name": row[1],
                    "site_url": row[2],
                    "game_url_template": row[3],
                    "api_view_template": row[4],
                    "registration_api_template": row[5],
                    "registration_payload": parsed_payload,
                    "api_parser": row[7],
                    "poll_max_people": row[8],
                }
        return None
    finally:
        conn.close()


def backfill_games_organizer(db_path: str, organizer_id: int) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE games SET organizer_id = ? WHERE organizer_id IS NULL;",
            (organizer_id,),
        )
        conn.commit()
    finally:
        conn.close()


def seed_admins(db_path: str, usernames: set[str]) -> None:
    if not usernames:
        return
    conn = sqlite3.connect(db_path)
    try:
        conn.executemany(
            """
            INSERT OR IGNORE INTO admins (username, approved_by, approved_at)
            VALUES (?, ?, CURRENT_TIMESTAMP);
            """,
            [(username, "seed") for username in sorted(usernames)],
        )
        conn.commit()
    finally:
        conn.close()


def is_admin(db_path: str, username: Optional[str]) -> bool:
    if not username:
        return False
    normalized = username.lstrip("@").lower()
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            "SELECT 1 FROM admins WHERE username = ? LIMIT 1;",
            (normalized,),
        )
        return cur.fetchone() is not None
    finally:
        conn.close()


def request_admin(
    db_path: str,
    user_id: int,
    username: Optional[str],
    full_name: str,
) -> Tuple[bool, str]:
    if not username:
        return False, "Нужен username в Telegram, чтобы стать админом."
    if username and is_admin(db_path, username):
        return False, "Вы уже администратор."
    conn = sqlite3.connect(db_path)
    try:
        try:
            conn.execute(
                """
                INSERT INTO admin_requests (user_id, username, full_name)
                VALUES (?, ?, ?);
                """,
                (user_id, username, full_name),
            )
        except sqlite3.IntegrityError:
            return False, "Заявка уже отправлена."
        conn.commit()
        return True, "Заявка отправлена. Админ рассмотрит ее."
    finally:
        conn.close()


def list_admin_requests(db_path: str) -> List[Dict[str, Optional[str]]]:
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            """
            SELECT id, user_id, username, full_name, created_at
            FROM admin_requests
            ORDER BY id ASC;
            """
        )
        return [
            {
                "id": row[0],
                "user_id": row[1],
                "username": row[2],
                "full_name": row[3],
                "created_at": row[4],
            }
            for row in cur.fetchall()
        ]
    finally:
        conn.close()


def approve_admin_request(
    db_path: str,
    request_id: int,
    approved_by: Optional[str],
) -> Tuple[bool, str]:
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            """
            SELECT username, full_name
            FROM admin_requests
            WHERE id = ?;
            """,
            (request_id,),
        )
        row = cur.fetchone()
        if row is None:
            return False, "Заявка не найдена."
        username = row[0]
        if not username:
            return False, "У пользователя нет username, добавить в админы нельзя."
        conn.execute(
            """
            INSERT OR IGNORE INTO admins (username, approved_by, approved_at)
            VALUES (?, ?, CURRENT_TIMESTAMP);
            """,
            (username.lstrip("@").lower(), approved_by),
        )
        conn.execute("DELETE FROM admin_requests WHERE id = ?;", (request_id,))
        conn.commit()
        return True, "Админ добавлен."
    finally:
        conn.close()


def deny_admin_request(db_path: str, request_id: int) -> Tuple[bool, str]:
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute("DELETE FROM admin_requests WHERE id = ?;", (request_id,))
        conn.commit()
        if cur.rowcount == 0:
            return False, "Заявка не найдена."
        return True, "Заявка отклонена."
    finally:
        conn.close()


def list_upcoming_games_admin(db_path: str) -> List[Dict[str, Optional[str]]]:
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            """
            SELECT
                id,
                title,
                game_date,
                restaurant_name,
                restaurant_address,
                restaurant_lat,
                restaurant_lon,
                is_archived
            FROM games
            WHERE game_date IS NOT NULL
              AND DATE(game_date) >= DATE('now', '+3 hours')
            ORDER BY DATETIME(game_date) ASC, id ASC;
            """
        )
        return [
            {
                "id": row[0],
                "title": row[1],
                "game_date": row[2],
                "restaurant_name": row[3],
                "restaurant_address": row[4],
                "restaurant_lat": row[5],
                "restaurant_lon": row[6],
                "is_archived": bool(row[7]),
            }
            for row in cur.fetchall()
        ]
    finally:
        conn.close()


def list_games_on_date(db_path: str, target_date: str) -> List[Dict[str, Optional[str]]]:
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            """
            SELECT
                g.id,
                g.title,
                g.game_date,
                g.restaurant_name,
                g.restaurant_address,
                g.restaurant_lat,
                g.restaurant_lon,
                COALESCE(o.poll_max_people, 9) AS capacity,
                (
                    SELECT COUNT(*)
                    FROM registrations r
                    WHERE r.game_id = g.id AND r.is_cancelled = 0
                ) AS registrations
            FROM games g
            LEFT JOIN organizers o ON o.id = g.organizer_id
            WHERE g.game_date IS NOT NULL
              AND DATE(g.game_date) = DATE(?)
              AND g.is_archived = 0
            ORDER BY DATETIME(g.game_date) ASC, g.id ASC;
            """,
            (target_date,),
        )
        return [
            {
                "id": row[0],
                "title": row[1],
                "game_date": row[2],
                "restaurant_name": row[3],
                "restaurant_address": row[4],
                "restaurant_lat": row[5],
                "restaurant_lon": row[6],
                "capacity": int(row[7]),
                "registrations": int(row[8]),
            }
            for row in cur.fetchall()
        ]
    finally:
        conn.close()


def list_today_games(db_path: str) -> List[Dict[str, Optional[str]]]:
    conn = sqlite3.connect(db_path)
    try:
        today = conn.execute("SELECT DATE('now', '+3 hours')").fetchone()[0]
    finally:
        conn.close()
    return list_games_on_date(db_path, today)


def reminder_was_sent(
    db_path: str, game_id: int, reminder_type: str, scheduled_date: str
) -> bool:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            """
            SELECT 1
            FROM reminder_deliveries
            WHERE game_id = ? AND reminder_type = ? AND scheduled_date = ?
            LIMIT 1;
            """,
            (game_id, reminder_type, scheduled_date),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def mark_reminder_sent(
    db_path: str, game_id: int, reminder_type: str, scheduled_date: str
) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT OR IGNORE INTO reminder_deliveries (
                game_id, reminder_type, scheduled_date
            ) VALUES (?, ?, ?);
            """,
            (game_id, reminder_type, scheduled_date),
        )
        conn.commit()
    finally:
        conn.close()


def get_game_admin(db_path: str, game_id: int) -> Optional[Dict[str, Optional[str]]]:
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            """
            SELECT
                id,
                title,
                game_date,
                restaurant_name,
                restaurant_address,
                restaurant_lat,
                restaurant_lon,
                is_archived
            FROM games
            WHERE id = ?;
            """,
            (game_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return {
            "id": row[0],
            "title": row[1],
            "game_date": row[2],
            "restaurant_name": row[3],
            "restaurant_address": row[4],
            "restaurant_lat": row[5],
            "restaurant_lon": row[6],
            "is_archived": bool(row[7]),
        }
    finally:
        conn.close()


def get_game_basic(db_path: str, game_id: int) -> Optional[Dict[str, Optional[str]]]:
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            """
            SELECT id, title, game_date, external_id,
                   restaurant_name, restaurant_address, restaurant_lat, restaurant_lon
            FROM games
            WHERE id = ?;
            """,
            (game_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return {
            "id": row[0],
            "title": row[1],
            "game_date": row[2],
            "external_id": row[3],
            "restaurant_name": row[4],
            "restaurant_address": row[5],
            "restaurant_lat": row[6],
            "restaurant_lon": row[7],
        }
    finally:
        conn.close()


def list_game_registrants(
    db_path: str, game_id: int, include_cancelled: bool
) -> List[Dict[str, Optional[str]]]:
    conn = sqlite3.connect(db_path)
    try:
        if include_cancelled:
            cur = conn.execute(
                """
                SELECT user_id, username, full_name
                FROM registrations
                WHERE game_id = ?;
                """,
                (game_id,),
            )
        else:
            cur = conn.execute(
                """
                SELECT user_id, username, full_name
                FROM registrations
                WHERE game_id = ? AND is_cancelled = 0;
                """,
                (game_id,),
            )
        return [
            {"user_id": row[0], "username": row[1], "full_name": row[2]}
            for row in cur.fetchall()
        ]
    finally:
        conn.close()


def get_user_last_chat_id(db_path: str, user_id: int) -> Optional[int]:
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            """
            SELECT chat_id
            FROM messages
            WHERE user_id = ? AND chat_id = user_id
            ORDER BY id DESC
            LIMIT 1;
            """,
            (user_id,),
        )
        row = cur.fetchone()
        return int(row[0]) if row and row[0] is not None else None
    finally:
        conn.close()


def archive_game(db_path: str, game_id: int) -> bool:
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            "UPDATE games SET is_archived = 1 WHERE id = ? AND is_archived = 0;",
            (game_id,),
        )
        conn.execute(
            """
            UPDATE registrations
            SET is_cancelled = 1, cancelled_at = CURRENT_TIMESTAMP
            WHERE game_id = ? AND is_cancelled = 0;
            """,
            (game_id,),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def restore_game(db_path: str, game_id: int) -> bool:
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            "UPDATE games SET is_archived = 0 WHERE id = ? AND is_archived = 1;",
            (game_id,),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def log_message(
    db_path: str,
    message_id: Optional[int],
    chat_id: Optional[int],
    user_id: Optional[int],
    username: Optional[str],
    full_name: Optional[str],
    content_type: Optional[str],
    text: Optional[str],
) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO messages (
                message_id,
                chat_id,
                user_id,
                username,
                full_name,
                content_type,
                text
            )
            VALUES (?, ?, ?, ?, ?, ?, ?);
            """,
            (
                message_id,
                chat_id,
                user_id,
                username,
                full_name,
                content_type,
                text,
            ),
        )
        conn.commit()
    finally:
        conn.close()
