import os

from dotenv import load_dotenv

from config import (
    TEAM_CAPTAIN_EMAIL,
    TEAM_CAPTAIN_NAME,
    TEAM_CAPTAIN_PHONE,
    TEAM_NAME,
    TEAM_REGISTRATION_COMMENT,
    TEAM_REGISTRATION_PAYLOAD,
)
from game_registration import init_db, seed_organizers, get_default_organizer, backfill_games_organizer


def ensure_db(db_path: str) -> bool:
    existed = os.path.exists(db_path)
    init_db(db_path)
    seed_organizers(
        db_path,
        [
            {
                "name": "Квиз Плиз!",
                "site_url": "https://spb.quizplease.ru",
                "game_url_template": "https://spb.quizplease.ru/game/{external_id}",
                "api_view_template": "https://api.quizplease.ru/api/games/view/{game_id}",
                "registration_api_template": "https://api.quizplease.ru/api/games/{external_id}/registration",
                "registration_payload": TEAM_REGISTRATION_PAYLOAD,
                "api_parser": "quizplease_v1",
                "poll_max_people": 9,
            }
            ,
            {
                "name": "Шейкер Квиз",
                "site_url": "https://spb.shakerquiz.ru/",
                "game_url_template": "https://spb.shakerquiz.ru/{external_id}",
                "registration_api_template": "https://games-beta.backend.beta.shakerquiz.ru/registration",
                "registration_payload": {
                    "ads_from": "",
                    "comment": TEAM_REGISTRATION_COMMENT,
                    "channel": "email",
                    "confirmed_channel": "email",
                    "email": TEAM_CAPTAIN_EMAIL,
                    "vkontakte": "",
                    "telegramBot": "",
                    "event_id": "",
                    "human_name": TEAM_CAPTAIN_NAME,
                    "is_alone": False,
                    "is_birthday": False,
                    "is_extensible": False,
                    "is_first": False,
                    "people_count": 9,
                    "phone": TEAM_CAPTAIN_PHONE,
                    "promocode": "",
                    "team_name": TEAM_NAME,
                },
                "api_parser": "shaker_v1",
                "poll_max_people": 10,
            },
        ],
    )
    organizer = get_default_organizer(db_path)
    if organizer:
        backfill_games_organizer(db_path, organizer["id"])
    return not existed


def main() -> None:
    load_dotenv()
    db_path = os.getenv("DB_PATH", "quiz_bot.sqlite3")
    created = ensure_db(db_path)
    if created:
        print(f"Database initialized at {db_path}")
    else:
        print(f"Database already exists at {db_path}")


if __name__ == "__main__":
    main()
