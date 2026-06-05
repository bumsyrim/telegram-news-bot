"""
미국 시장 지표 조회 -> 텔레그램 발송
야후 파이낸스 API (yfinance) 사용
"""
import argparse
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import yfinance as yf

log = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))
USERS_FILE = Path("users.json")

# (야후 파이낸스 티커, 표시 이름, 종류)
# CME 코스피200 선물(KM=F)은 Yahoo Finance 미지원 → KOSPI200 지수(^KS200) 대체
TICKERS = [
    ("^IXIC",    "나스닥",    "index_us"),
    ("^GSPC",    "S&P500",    "index_us"),
    ("^DJI",     "다우존스",  "index_us"),
    ("^KS200",   "코스피200", "index_kr"),
    ("USDKRW=X", "원/달러",   "forex"),
]


def fetch_market_data() -> list:
    results = []
    for symbol, label, kind in TICKERS:
        try:
            ticker = yf.Ticker(symbol)
            fi = ticker.fast_info
            price = fi.last_price
            prev = fi.previous_close

            if price is None or prev is None or prev == 0:
                log.warning("데이터 없음: %s", symbol)
                continue

            change = price - prev
            change_pct = change / prev * 100
            results.append({
                "label": label,
                "kind": kind,
                "price": price,
                "change": change,
                "change_pct": change_pct,
            })
        except Exception as e:
            log.error("조회 실패 (%s): %s", symbol, e)

    return results


def format_market_message(data: list) -> str:
    date_str = datetime.now(KST).strftime("%Y년 %m월 %d일")
    lines = [
        "<b>[금융]</b>",
        f"전일 시장현황 · {date_str}",
    ]

    for item in data:
        label = item["label"]
        price = item["price"]
        change = item["change"]
        pct = item["change_pct"]
        arrow = "▲" if change >= 0 else "▼"
        sign = "+" if change >= 0 else ""

        if item["kind"] == "forex":
            lines.append(
                f"- {label}: {price:,.0f}원 {arrow} {sign}{change:.0f}원 ({sign}{pct:.2f}%)"
            )
        elif item["kind"] == "index_kr":
            lines.append(
                f"- {label}: {price:,.2f} {arrow} {sign}{pct:.1f}%"
            )
        else:  # index_us
            lines.append(
                f"- {label}: {price:,.0f} {arrow} {sign}{pct:.1f}%"
            )

    return "\n".join(lines)


def _load_subscribers(fallback_id) -> list:
    if USERS_FILE.exists():
        data = json.loads(USERS_FILE.read_text(encoding="utf-8"))
        subs = data.get("subscribers", {})
        if isinstance(subs, dict):
            return [int(uid) for uid in subs.keys()] if subs else [int(fallback_id)]
        if isinstance(subs, list) and subs:
            return subs
    return [int(fallback_id)]


def send_market_report():
    from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

    data = fetch_market_data()
    if not data:
        log.error("시장 데이터 조회 실패, 발송 중단")
        return

    msg = format_market_message(data)
    subscribers = _load_subscribers(TELEGRAM_CHAT_ID)
    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    total_sent = total_failed = 0
    for chat_id in subscribers:
        try:
            resp = requests.post(
                api_url,
                json={"chat_id": chat_id, "text": msg, "parse_mode": "HTML"},
                timeout=10,
            )
            resp.raise_for_status()
            total_sent += 1
        except Exception as e:
            log.warning("발송 실패 (chat_id=%s): %s", chat_id, e)
            total_failed += 1

    log.info("금융 알림 완료: 성공 %d명 / 실패 %d명", total_sent, total_failed)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    parser = argparse.ArgumentParser(description="미국 시장 지표 텔레그램 알림")
    parser.add_argument("--send", action="store_true", help="시장 알림 발송")
    args = parser.parse_args()

    if args.send:
        send_market_report()
    else:
        parser.print_help()
