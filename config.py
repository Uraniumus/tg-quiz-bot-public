import os

from dotenv import load_dotenv

load_dotenv()

POLL_YES_THRESHOLD = 5
POLL_MAX_PEOPLE = 9

TEAM_REGISTRATION_COMMENT = os.getenv(
    "TEAM_REGISTRATION_COMMENT",
    "Посадите, пожалуйста, за стол, где всем участникам будет виден экран.",
)

# Team / captain contact details used to register for games.
# They are personal data: set them in .env, never commit real values.
TEAM_NAME = os.getenv("TEAM_NAME", "Моя команда")
AMRA_TEAM_NAME = os.getenv("AMRA_TEAM_NAME", TEAM_NAME)
TEAM_CAPTAIN_NAME = os.getenv("TEAM_CAPTAIN_NAME", "")
TEAM_CAPTAIN_PHONE = os.getenv("TEAM_CAPTAIN_PHONE", "")
TEAM_CAPTAIN_EMAIL = os.getenv("TEAM_CAPTAIN_EMAIL", "")

# The QuizPlease form validates the visible code first, then submits the
# returned numeric promo id with the registration payload. 0 = no promo.
QUIZPLEASE_PROMO_CODE = os.getenv("QUIZPLEASE_PROMO_CODE", "")
QUIZPLEASE_PROMO_ID = int(os.getenv("QUIZPLEASE_PROMO_ID", "0") or 0)
QUIZPLEASE_PROMO_END_DATE = os.getenv("QUIZPLEASE_PROMO_END_DATE", "1970-01-01")

TEAM_REGISTRATION_PAYLOAD = {
    "title": TEAM_NAME,
    "captain": TEAM_CAPTAIN_NAME,
    "phone": TEAM_CAPTAIN_PHONE,
    "email": TEAM_CAPTAIN_EMAIL,
    "comment": TEAM_REGISTRATION_COMMENT,
    "people_count": 9,
    "count_paid": 5,
    "is_first": False,
    "is_personal_data_consent": True,
    "is_marketing_consent": False,
    "promo_ids": [],
    "certificate_ids": [],
    "table_size": None,
    "payment_method": "cash",
}

DAILY_REMINDER_TIME = "11:00"
DAY_BEFORE_REMINDER_TIME = "14:00"
POSTGAME_PROMPT_TIME = "23:00"
REACTIVATION_TIME = "16:00"
MONTHLY_CARD_TIME = "11:30"
BOT_TIMEZONE = "Europe/Moscow"
