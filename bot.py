"""
텔레그램 뉴스 봇 - 메인 실행 파일
새 글 감지 → Claude API 요약 → 텔레그램 발송
"""
import sys
import json
import time
import logging
import schedule
from pathlib import Path
from datetime import datetime

import anthropic
import requests

from sources.brunch import BrunchSource
# 나중에 추가할 소스들:
# from sources.rss import RssSource
# from sources.naver_blog import NaverBlogSource

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("bot.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

# ── 설정 ─────────────────────────────────────────────
from config import (
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    ANTHROPIC_API_KEY,
    CHECK_INTERVAL_MINUTES,
)

SEEN_FILE = Path("seen.json")


# ── 상태 저장 ─────────────────────────────────────────
def load_seen() -> set:
    if SEEN_FILE.exists():
        return set(json.loads(SEEN_FILE.read_text(encoding="utf-8")))
    return set()


def save_seen(seen: set):
    SEEN_FILE.write_text(json.dumps(list(seen), ensure_ascii=False, indent=2), encoding="utf-8")


# ── Claude 요약 ───────────────────────────────────────
def summarize(title: str, content: str, url: str) -> str:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    prompt = f"""다음 글을 한국어로 간결하게 요약해 주세요.

제목: {title}
내용:
{content[:3000]}

요약 형식:
- 핵심 내용을 3~5개 bullet point로 정리
- 각 bullet은 1~2문장
- 전문 용어는 쉽게 풀어서 설명"""

    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=800,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text


# ── 텔레그램 발송 ──────────────────────────────────────
def send_telegram(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = requests.post(
        url,
        json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        },
        timeout=10,
    )
    resp.raise_for_status()
    log.info("텔레그램 발송 완료")


def format_message(title: str, summary: str, url: str, source_name: str) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    return (
        f"📰 <b>{title}</b>\n"
        f"🔗 <a href='{url}'>원문 보기</a>\n"
        f"<i>출처: {source_name} | {now}</i>"
    )

# ── 메인 체크 루프 ─────────────────────────────────────
SOURCES = [
    BrunchSource(
        url="https://brunch.co.kr/@sungdairi",
        name="브런치 AI Weekly",
    ),
]
def check_and_send():
    seen = load_seen()
    log.info(f"새 글 확인 중... (이미 발송된 글: {len(seen)}개)")

    for source in SOURCES:
        try:
            articles = source.fetch()
            log.info(f"[{source.name}] 글 {len(articles)}개 발견")

            for article in articles:
                if article["id"] in seen:
                    continue

                log.info(f"새 글 발견: {article['title']}")

                message = format_message(
                     article["title"], "", article["url"], source.name
                )
                send_telegram(message)

                seen.add(article["id"])
                save_seen(seen)
                time.sleep(2)  # API 호출 간격

        except Exception as e:
            log.error(f"[{source.name}] 오류: {e}", exc_info=True)


# ── 실행 ──────────────────────────────────────────────
if __name__ == "__main__":
    log.info(f"봇 시작! {CHECK_INTERVAL_MINUTES}분마다 확인합니다.")
    check_and_send()

    if "--once" not in sys.argv:
        schedule.every(CHECK_INTERVAL_MINUTES).minutes.do(check_and_send)
        while True:
            schedule.run_pending()
            time.sleep(30)