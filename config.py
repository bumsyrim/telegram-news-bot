"""
설정 파일 - .env 파일에서 읽어옵니다.
"""

import os
from dotenv import load_dotenv

load_dotenv()


def _require(key: str) -> str:
    val = os.getenv(key)
    if not val:
        raise EnvironmentError(
            f"필수 환경변수 '{key}'가 설정되지 않았습니다.\n"
            f".env 파일을 확인해 주세요."
        )
    return val


TELEGRAM_BOT_TOKEN = _require("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = _require("TELEGRAM_CHAT_ID")
ANTHROPIC_API_KEY = _require("ANTHROPIC_API_KEY")
CHECK_INTERVAL_MINUTES = int(os.getenv("CHECK_INTERVAL_MINUTES", "60"))
WEATHER_API_KEY = os.getenv("WEATHER_API_KEY", "")
