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

import anthropic
import requests

from sources.brunch import BrunchSource
from sources.gpters import GptersSource

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
SOURCES_FILE = Path("sources.json")
USERS_FILE = Path("users.json")


# ── sources.json 로드 ──────────────────────────────────
def _auto_tag(name: str) -> str:
    """소스 이름에서 태그 자동 생성: 짧은 대문자 단어 우선, 없으면 첫 단어"""
    for word in name.split():
        if word.isupper() and 1 <= len(word) <= 5:
            return word
    return name.split()[0] if name else name


def _build_sources() -> list:
    if not SOURCES_FILE.exists():
        return [BrunchSource(url="https://brunch.co.kr/@sungdairi", name="브런치 AI Weekly", tag="AI")]
    data = json.loads(SOURCES_FILE.read_text(encoding="utf-8"))
    sources = []
    for s in data.get("sources", []):
        tag = s.get("tag") or _auto_tag(s["name"])
        if s.get("type") == "brunch":
            sources.append(BrunchSource(url=s["url"], name=s["name"], tag=tag))
        elif s.get("type") == "gpters":
            sources.append(GptersSource(url=s["url"], name=s["name"], tag=tag))
    return sources or [BrunchSource(url="https://brunch.co.kr/@sungdairi", name="브런치 AI Weekly", tag="AI")]


def _get_interval() -> int:
    if not SOURCES_FILE.exists():
        return CHECK_INTERVAL_MINUTES
    data = json.loads(SOURCES_FILE.read_text(encoding="utf-8"))
    return data.get("interval_minutes", CHECK_INTERVAL_MINUTES)


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
def _load_subscribers() -> list:
    if USERS_FILE.exists():
        data = json.loads(USERS_FILE.read_text(encoding="utf-8"))
        subs = data.get("subscribers", {})
        if isinstance(subs, dict):
            return [int(uid) for uid in subs.keys()] if subs else [TELEGRAM_CHAT_ID]
        if isinstance(subs, list) and subs:
            return subs
    return [TELEGRAM_CHAT_ID]


def send_telegram(text: str):
    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    subscribers = _load_subscribers()
    failed = 0
    for chat_id in subscribers:
        try:
            resp = requests.post(
                api_url,
                json={
                    "chat_id": chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": False,
                },
                timeout=10,
            )
            resp.raise_for_status()
        except Exception as e:
            log.warning("발송 실패 (chat_id=%s): %s", chat_id, e)
            failed += 1
    log.info("텔레그램 발송 완료 (%d명 중 %d명 성공)", len(subscribers), len(subscribers) - failed)


def format_message(title: str, content: str, url: str, tag: str = "") -> str:
    lines = []
    if tag:
        lines.append(f"<b>[{tag}]</b>")
    lines.append(title)
    if content:
        preview = content[:200].strip()
        if len(content) > 200:
            preview += "..."
        lines += ["", f"- {preview}"]
    lines += ["", f"- 🔗 <a href='{url}'>원문 보기</a>"]
    return "\n".join(lines)

# ── 메인 체크 루프 ─────────────────────────────────────
def check_and_send():
    seen = load_seen()
    sources = _build_sources()
    log.info(f"새 글 확인 중... (등록 사이트: {len(sources)}개 / 이미 발송된 글: {len(seen)}개)")

    for source in sources:
        try:
            articles = source.fetch()
            log.info(f"[{source.name}] 글 {len(articles)}개 발견")

            for article in articles:
                if article["id"] in seen:
                    continue

                log.info(f"새 글 발견: {article['title']}")

                message = format_message(
                    article["title"], article.get("content", ""), article["url"], source.tag
                )
                send_telegram(message)

                seen.add(article["id"])
                save_seen(seen)
                time.sleep(2)  # API 호출 간격

        except Exception as e:
            log.error(f"[{source.name}] 오류: {e}", exc_info=True)


# ── 실행 ──────────────────────────────────────────────
if __name__ == "__main__":
    interval = _get_interval()
    log.info(f"봇 시작! {interval}분마다 확인합니다.")
    check_and_send()

    if "--once" not in sys.argv:
        schedule.every(interval).minutes.do(check_and_send)
        while True:
            schedule.run_pending()
            time.sleep(30)