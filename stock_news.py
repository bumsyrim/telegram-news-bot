"""
종목 뉴스 수집 모듈
- 네이버 뉴스 API (NAVER_CLIENT_ID, NAVER_CLIENT_SECRET)
- 네이버 증권 토론방 크롤링
- stock_seen.json으로 중복 방지
"""
import hashlib
import json
import logging
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

SEEN_FILE = Path("stock_seen.json")
NAVER_NEWS_URL = "https://openapi.naver.com/v1/search/news.json"
NAVER_BOARD_URL = "https://finance.naver.com/item/board.naver"


def _load_seen() -> set:
    if SEEN_FILE.exists():
        try:
            return set(json.loads(SEEN_FILE.read_text(encoding="utf-8")))
        except Exception:
            pass
    return set()


def _save_seen(seen: set):
    entries = list(seen)[-10000:]  # 최대 10000개 유지
    SEEN_FILE.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")


def _url_hash(url: str) -> str:
    return hashlib.md5(url.encode()).hexdigest()[:12]


def _call_naver_news_api(query: str, display: int) -> list:
    """단일 키워드로 네이버 뉴스 API 호출. 내부 헬퍼."""
    client_id = os.getenv("NAVER_CLIENT_ID", "")
    client_secret = os.getenv("NAVER_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        return []
    resp = requests.get(
        NAVER_NEWS_URL,
        headers={
            "X-Naver-Client-Id": client_id,
            "X-Naver-Client-Secret": client_secret,
        },
        params={"query": query, "display": display, "sort": "date"},
        timeout=10,
    )
    resp.raise_for_status()
    results = []
    for item in resp.json().get("items", []):
        title = BeautifulSoup(item.get("title", ""), "lxml").get_text()
        dt = None
        try:
            dt = datetime.strptime(item.get("pubDate", ""), "%a, %d %b %Y %H:%M:%S %z")
            time_str = dt.strftime("%Y-%m-%d %H:%M")
        except Exception:
            time_str = item.get("pubDate", "")
        results.append({"title": title, "link": item.get("link", ""), "time": time_str, "dt": dt})
    return results


def fetch_naver_news(name: str, code: str = "", display: int = 5, days: int = 7) -> list:
    """
    종목명 정확 일치 → 코드 → 앞 7자 순으로 뉴스 검색.
    최근 days일 이내, 최신순, 최대 display건.
    결과: [{"title", "link", "time"}, ...]
    """
    seen_links: dict = {}
    try:
        # 1단계: 종목명 전체 따옴표 정확 일치 검색
        for item in _call_naver_news_api(f'"{name}"', display):
            if item["link"] not in seen_links:
                seen_links[item["link"]] = item

        # 2단계: 결과 없으면 종목코드로 검색
        if not seen_links and code:
            for item in _call_naver_news_api(code, display):
                if item["link"] not in seen_links:
                    seen_links[item["link"]] = item

        # 3단계: 그래도 없으면 앞 7자로 검색
        if not seen_links:
            short = name[:7]
            for item in _call_naver_news_api(short, display):
                if item["link"] not in seen_links:
                    seen_links[item["link"]] = item
    except Exception as e:
        log.error("네이버 뉴스 조회 실패 (%s): %s", name, e)

    # 최근 N일 필터 + 최신순 정렬 + 최대 display건
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    items = list(seen_links.values())
    items = [i for i in items if i["dt"] is None or i["dt"] >= cutoff]
    items.sort(key=lambda i: i["dt"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return [{"title": i["title"], "link": i["link"], "time": i["time"]} for i in items[:display]]


def fetch_naver_board(code: str) -> list:
    """네이버 증권 토론방 크롤링. 결과: [{"title", "link", "time"}, ...]"""
    try:
        resp = requests.get(
            NAVER_BOARD_URL,
            params={"code": code},
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
            timeout=10,
        )
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")
        results = []
        for row in soup.select("table.type2 tr"):
            tds = row.find_all("td")
            if len(tds) < 2:
                continue
            time_str = tds[0].get_text(strip=True)   # td[0]: 날짜
            title_tag = tds[1].find("a")              # td[1]: 제목
            if not title_tag or not time_str:
                continue
            title = title_tag.get_text(strip=True)
            href = title_tag.get("href", "")
            link = f"https://finance.naver.com{href}" if href.startswith("/") else href
            if title:
                results.append({"title": title, "link": link, "time": time_str})
        return results
    except Exception as e:
        log.error("네이버 토론방 조회 실패 (%s): %s", code, e)
        return []


def collect_and_send(send_fn, news_count: int = 5, board_count: int = 3, days: int = 7):
    """
    구독 종목별 뉴스/토론방 수집 후 새 항목을 send_fn(chat_id, text)으로 발송.
    사용자별 저장 설정을 우선 적용하고, 없으면 파라미터 기본값 사용.
    30분 간격 스케줄러에서 호출.
    """
    from stock_subscription import get_all_subscriptions

    all_subs = get_all_subscriptions()
    if not all_subs:
        return

    seen = _load_seen()
    new_seen = set(seen)

    for chat_id, stocks in all_subs.items():
        for code, info in stocks.items():
            name = info.get("name", code)
            u_news = info.get("news_count", news_count)
            u_board = info.get("board_count", board_count)
            u_days = info.get("days", days)

            news_items = fetch_naver_news(name, code=code, display=u_news, days=u_days)
            board_items = fetch_naver_board(code)[:u_board]

            messages = []
            for item in news_items:
                h = _url_hash(item["link"])
                if h in seen:
                    continue
                new_seen.add(h)
                messages.append(f"[뉴스] {item['title']}\n{item['time']}\n{item['link']}")

            for item in board_items:
                h = _url_hash(item["link"])
                if h in seen:
                    continue
                new_seen.add(h)
                messages.append(f"[토론방] {item['title']}\n{item['time']}\n{item['link']}")

            if messages:
                text = f"<b>{name}</b> [{code}]\n\n" + "\n\n".join(messages)
                try:
                    send_fn(int(chat_id), text)
                except Exception as e:
                    log.error("종목 뉴스 발송 실패 chat_id=%s: %s", chat_id, e)
                log.info("종목 뉴스 발송: %s (%s) → chat_id=%s, %d건", name, code, chat_id, len(messages))

    _save_seen(new_seen)


def fetch_news_for_report(code: str, name: str, market: str,
                          news_count: int = 5, board_count: int = 3, days: int = 7) -> str:
    """
    즉시 조회 버튼용: seen 필터 없이 최신 뉴스/토론방 수집 후 메시지 문자열 반환.
    새 글 없으면 None 반환.
    """
    news_items = fetch_naver_news(name, code=code, display=news_count, days=days)
    board_items = fetch_naver_board(code)

    messages = []
    for item in news_items:
        messages.append(f"[뉴스] {item['title']}\n{item['time']}\n{item['link']}")
    for item in board_items[:board_count]:
        messages.append(f"[토론방] {item['title']}\n{item['time']}\n{item['link']}")

    if not messages:
        return None
    return f"<b>{name}</b> [{code}]\n\n" + "\n\n".join(messages)
