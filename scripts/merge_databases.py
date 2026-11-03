#!/usr/bin/env python3
"""Safely merge a secondary quiz-bot SQLite database into a primary copy.

The input databases are opened read-only and are never modified. The output must
not exist. It is first created through SQLite's backup API, then all secondary
rows are imported in a single transaction with foreign-key remapping.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable


TABLE_COLUMNS = {
    "organizers": (
        "name", "site_url", "game_url_template", "api_view_template",
        "registration_api_template", "registration_payload", "api_parser",
        "poll_max_people", "created_at",
    ),
    "games": (
        "title", "game_date", "description", "restaurant_name",
        "restaurant_address", "restaurant_lat", "restaurant_lon",
        "game_number", "lottery_id", "external_id",
        "registration_external_id", "organizer_id", "is_archived", "created_at",
    ),
    "registrations": (
        "game_id", "user_id", "username", "full_name", "is_cancelled",
        "cancelled_at", "created_at",
    ),
    "game_waitlist": (
        "game_id", "user_id", "username", "full_name", "created_at",
    ),
    "messages": (
        "message_id", "chat_id", "user_id", "username", "full_name",
        "content_type", "text", "created_at",
    ),
    "admins": (
        "username", "approved_by", "approved_at", "added_at",
    ),
    "admin_requests": (
        "user_id", "username", "full_name", "created_at",
    ),
    "polls": (
        "poll_id", "game_id", "chat_id", "message_id", "created_by",
        "yes_notified", "full_notified", "created_at",
    ),
    "poll_answers": (
        "poll_id", "user_id", "username", "full_name", "option_id", "updated_at",
    ),
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--primary", required=True, type=Path,
                        help="authoritative database whose IDs and conflicts win")
    parser.add_argument("--secondary", required=True, type=Path,
                        help="database to import")
    parser.add_argument("--output", required=True, type=Path,
                        help="new merged database; must not already exist")
    parser.add_argument("--report", type=Path,
                        help="optional JSON report path; must not already exist")
    return parser.parse_args()


def quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def readonly_connection(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in connection.execute(f"PRAGMA table_info({quote_identifier(table)})")}


def validate_schema(connection: sqlite3.Connection, label: str) -> None:
    for table, required in TABLE_COLUMNS.items():
        actual = table_columns(connection, table)
        missing = set(required) - actual
        if missing:
            raise RuntimeError(f"{label}: {table} is missing columns: {sorted(missing)}")


def fetch_rows(connection: sqlite3.Connection, table: str) -> Iterable[sqlite3.Row]:
    columns = ("id",) + TABLE_COLUMNS[table]
    sql = f"SELECT {', '.join(quote_identifier(c) for c in columns)} FROM {quote_identifier(table)} ORDER BY id"
    return connection.execute(sql)


def insert_row(
    connection: sqlite3.Connection,
    table: str,
    values: dict[str, Any],
    preferred_id: int | None,
) -> int:
    row_id = preferred_id
    if row_id is not None:
        occupied = connection.execute(
            f"SELECT 1 FROM {quote_identifier(table)} WHERE id = ?", (row_id,)
        ).fetchone()
        if occupied:
            row_id = None

    insert_values = dict(values)
    if row_id is not None:
        insert_values = {"id": row_id, **insert_values}
    columns = tuple(insert_values)
    placeholders = ", ".join("?" for _ in columns)
    sql = (
        f"INSERT INTO {quote_identifier(table)} "
        f"({', '.join(quote_identifier(c) for c in columns)}) VALUES ({placeholders})"
    )
    cursor = connection.execute(sql, tuple(insert_values[c] for c in columns))
    return int(row_id if row_id is not None else cursor.lastrowid)


def fill_missing(
    connection: sqlite3.Connection,
    table: str,
    row_id: int,
    source: sqlite3.Row,
    columns: Iterable[str],
) -> int:
    current = connection.execute(
        f"SELECT * FROM {quote_identifier(table)} WHERE id = ?", (row_id,)
    ).fetchone()
    updates = {
        column: source[column]
        for column in columns
        if current[column] is None and source[column] is not None
    }
    if not updates:
        return 0
    assignment = ", ".join(f"{quote_identifier(c)} = ?" for c in updates)
    connection.execute(
        f"UPDATE {quote_identifier(table)} SET {assignment} WHERE id = ?",
        (*updates.values(), row_id),
    )
    return len(updates)


def normalized_username(value: str | None) -> str | None:
    if not value:
        return None
    return value.lstrip("@").lower()


def merge(primary: Path, secondary: Path, output: Path) -> dict[str, Any]:
    paths = [p.resolve() for p in (primary, secondary, output)]
    if len(set(paths)) != 3:
        raise RuntimeError("primary, secondary, and output must be different paths")
    if not primary.is_file() or not secondary.is_file():
        raise RuntimeError("both input databases must exist")
    if output.exists():
        raise RuntimeError(f"refusing to overwrite existing output: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)

    source_primary = readonly_connection(primary)
    source_secondary = readonly_connection(secondary)
    validate_schema(source_primary, "primary")
    validate_schema(source_secondary, "secondary")

    target = sqlite3.connect(output)
    target.row_factory = sqlite3.Row
    try:
        source_primary.backup(target)
        target.execute("PRAGMA foreign_keys = ON")
        validate_schema(target, "output")

        stats: Counter[str] = Counter()
        conflicts: Counter[str] = Counter()
        organizer_map: dict[int, int] = {}
        game_map: dict[int, int] = {}

        target.execute("BEGIN IMMEDIATE")

        # Organizers are configuration. Primary values win; missing fields may be filled.
        for row in fetch_rows(source_secondary, "organizers"):
            existing = target.execute(
                "SELECT id FROM organizers WHERE name = ?", (row["name"],)
            ).fetchone()
            if existing:
                organizer_map[row["id"]] = int(existing["id"])
                stats["organizers_matched"] += 1
                stats["organizer_fields_filled"] += fill_missing(
                    target, "organizers", int(existing["id"]), row, TABLE_COLUMNS["organizers"]
                )
            else:
                values = {column: row[column] for column in TABLE_COLUMNS["organizers"]}
                organizer_map[row["id"]] = insert_row(
                    target, "organizers", values, preferred_id=row["id"]
                )
                stats["organizers_imported"] += 1

        # Build match indexes from the untouched primary only. Do not deduplicate
        # rows that were already duplicated inside the secondary: their numeric IDs
        # may still exist in historical Telegram callback payloads.
        primary_games_by_external: dict[tuple[Any, Any], list[int]] = defaultdict(list)
        primary_games_by_title_date: dict[tuple[Any, Any, Any], list[int]] = defaultdict(list)
        for row in target.execute(
            "SELECT id, organizer_id, external_id, title, game_date FROM games"
        ):
            if row["external_id"]:
                primary_games_by_external[(row["organizer_id"], row["external_id"])].append(row["id"])
            if row["game_date"]:
                primary_games_by_title_date[
                    (row["organizer_id"], row["title"], row["game_date"])
                ].append(row["id"])

        # Games are the root entity. Preserve a secondary ID whenever it is free so
        # historical Telegram callback payloads continue to resolve.
        for row in fetch_rows(source_secondary, "games"):
            mapped_organizer = organizer_map.get(row["organizer_id"], row["organizer_id"])
            existing_id = None
            if row["external_id"]:
                candidates = primary_games_by_external.get(
                    (mapped_organizer, row["external_id"]), []
                )
                if len(candidates) == 1:
                    existing_id = int(candidates[0])
                elif len(candidates) > 1:
                    conflicts["ambiguous_primary_game_external_id"] += 1
            if existing_id is None and row["game_date"]:
                candidates = primary_games_by_title_date.get(
                    (mapped_organizer, row["title"], row["game_date"]), []
                )
                if len(candidates) == 1:
                    existing_id = int(candidates[0])
                elif len(candidates) > 1:
                    conflicts["ambiguous_primary_game_title_date"] += 1
            if existing_id is not None:
                game_map[row["id"]] = existing_id
                stats["games_matched"] += 1
                stats["game_fields_filled"] += fill_missing(
                    target, "games", existing_id, row,
                    (c for c in TABLE_COLUMNS["games"] if c != "organizer_id"),
                )
            else:
                values = {column: row[column] for column in TABLE_COLUMNS["games"]}
                values["organizer_id"] = mapped_organizer
                new_id = insert_row(target, "games", values, preferred_id=row["id"])
                game_map[row["id"]] = new_id
                stats["games_imported"] += 1
                if new_id == row["id"]:
                    stats["game_ids_preserved"] += 1
                else:
                    stats["game_ids_remapped"] += 1

        # Child rows keyed by (game, user). Primary state wins on a true conflict.
        for table in ("registrations", "game_waitlist"):
            for row in fetch_rows(source_secondary, table):
                mapped_game = game_map[row["game_id"]]
                existing = target.execute(
                    f"SELECT id FROM {quote_identifier(table)} WHERE game_id = ? AND user_id = ?",
                    (mapped_game, row["user_id"]),
                ).fetchone()
                if existing:
                    conflicts[f"{table}_primary_won"] += 1
                    continue
                values = {column: row[column] for column in TABLE_COLUMNS[table]}
                values["game_id"] = mapped_game
                insert_row(target, table, values, preferred_id=row["id"])
                stats[f"{table}_imported"] += 1

        # Poll IDs come from Telegram and are globally stable.
        for row in fetch_rows(source_secondary, "polls"):
            mapped_game = game_map[row["game_id"]]
            existing = target.execute(
                "SELECT id, game_id, yes_notified, full_notified FROM polls WHERE poll_id = ?",
                (row["poll_id"],),
            ).fetchone()
            if existing:
                stats["polls_matched"] += 1
                if existing["game_id"] != mapped_game:
                    conflicts["poll_game_mismatch_primary_won"] += 1
                target.execute(
                    "UPDATE polls SET yes_notified = MAX(yes_notified, ?), "
                    "full_notified = MAX(full_notified, ?) WHERE id = ?",
                    (row["yes_notified"], row["full_notified"], existing["id"]),
                )
            else:
                values = {column: row[column] for column in TABLE_COLUMNS["polls"]}
                values["game_id"] = mapped_game
                insert_row(target, "polls", values, preferred_id=row["id"])
                stats["polls_imported"] += 1

        # Poll answers were split between servers. Union them; latest timestamp wins.
        for row in fetch_rows(source_secondary, "poll_answers"):
            existing = target.execute(
                "SELECT * FROM poll_answers WHERE poll_id = ? AND user_id = ?",
                (row["poll_id"], row["user_id"]),
            ).fetchone()
            if existing is None:
                values = {column: row[column] for column in TABLE_COLUMNS["poll_answers"]}
                insert_row(target, "poll_answers", values, preferred_id=row["id"])
                stats["poll_answers_imported"] += 1
            elif (row["updated_at"] or "") > (existing["updated_at"] or ""):
                target.execute(
                    "UPDATE poll_answers SET username = ?, full_name = ?, option_id = ?, updated_at = ? "
                    "WHERE id = ?",
                    (row["username"], row["full_name"], row["option_id"],
                     row["updated_at"], existing["id"]),
                )
                stats["poll_answers_updated_from_secondary"] += 1
            else:
                stats["poll_answers_primary_won"] += 1

        # Admins are a normalized union. Primary metadata wins.
        admin_by_name = {
            normalized_username(row["username"]): int(row["id"])
            for row in target.execute("SELECT id, username FROM admins")
        }
        for row in fetch_rows(source_secondary, "admins"):
            username = normalized_username(row["username"])
            if username in admin_by_name:
                stats["admins_matched"] += 1
                continue
            values = {column: row[column] for column in TABLE_COLUMNS["admins"]}
            values["username"] = username
            new_id = insert_row(target, "admins", values, preferred_id=row["id"])
            admin_by_name[username] = new_id
            stats["admins_imported"] += 1

        for row in fetch_rows(source_secondary, "admin_requests"):
            if normalized_username(row["username"]) in admin_by_name:
                stats["admin_requests_skipped_existing_admin"] += 1
                continue
            existing = target.execute(
                "SELECT id FROM admin_requests WHERE user_id = ?", (row["user_id"],)
            ).fetchone()
            if existing:
                stats["admin_requests_matched"] += 1
                continue
            values = {column: row[column] for column in TABLE_COLUMNS["admin_requests"]}
            insert_row(target, "admin_requests", values, preferred_id=row["id"])
            stats["admin_requests_imported"] += 1

        # Incoming Telegram messages are naturally identified by (chat_id, message_id).
        # The index intentionally contains only primary rows, preserving any repeated
        # log rows that already existed inside the secondary.
        primary_messages: dict[tuple[int, int], list[sqlite3.Row]] = defaultdict(list)
        for row in target.execute(
            "SELECT * FROM messages WHERE chat_id IS NOT NULL AND message_id IS NOT NULL"
        ):
            primary_messages[(row["chat_id"], row["message_id"])].append(row)
        for row in fetch_rows(source_secondary, "messages"):
            existing_rows: list[sqlite3.Row] = []
            if row["chat_id"] is not None and row["message_id"] is not None:
                existing_rows = primary_messages.get((row["chat_id"], row["message_id"]), [])
            if existing_rows:
                comparable = ("user_id", "username", "full_name", "content_type", "text")
                if any(all(existing[c] == row[c] for c in comparable) for existing in existing_rows):
                    stats["messages_deduplicated"] += 1
                else:
                    conflicts["message_key_conflict_primary_won"] += 1
                continue
            values = {column: row[column] for column in TABLE_COLUMNS["messages"]}
            insert_row(target, "messages", values, preferred_id=row["id"])
            stats["messages_imported"] += 1

        target.commit()

        foreign_key_violations = [tuple(row) for row in target.execute("PRAGMA foreign_key_check")]
        integrity = target.execute("PRAGMA integrity_check").fetchone()[0]
        if foreign_key_violations or integrity != "ok":
            raise RuntimeError(
                f"merged database validation failed: integrity={integrity!r}, "
                f"foreign_key_violations={foreign_key_violations!r}"
            )

        counts = {
            table: target.execute(f"SELECT COUNT(*) FROM {quote_identifier(table)}").fetchone()[0]
            for table in TABLE_COLUMNS
        }
        return {
            "primary": str(primary.resolve()),
            "secondary": str(secondary.resolve()),
            "output": str(output.resolve()),
            "stats": dict(sorted(stats.items())),
            "conflicts": dict(sorted(conflicts.items())),
            "row_counts": counts,
            "integrity_check": integrity,
            "foreign_key_violations": len(foreign_key_violations),
        }
    except Exception:
        target.rollback()
        target.close()
        output.unlink(missing_ok=True)
        raise
    finally:
        source_primary.close()
        source_secondary.close()
        if target:
            target.close()


def main() -> None:
    args = parse_args()
    if args.report and args.report.exists():
        raise SystemExit(f"refusing to overwrite existing report: {args.report}")
    report = merge(args.primary, args.secondary, args.output)
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()
