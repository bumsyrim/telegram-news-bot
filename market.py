"""
미국 시장 지표 조회 -> 텔레그램 발송
야후 파이낸스 API (yfinance) + Investing.com Selenium 크롤링
"""
import argparse
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import yfinance as yf
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager

log = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))
USERS_FILE = Path("users.json")

FUTURES_URL = "https://kr.investing.com/indices/korea-200-futures"

# (야후 파이낸스 티커, 표시 이름, 종류)
TICKERS = [
    ("^IXIC",    "나스닥",   "index_us"),
    ("^GSPC",    "S&P500",   "index_us"),
    ("^DJI",     "다우존스", "index_us"),
    ("USDKRW=X", "원/달러",  "forex"),
]


def _make_driver():
    options = Options()
    options.add_argument("--headless")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1280,800")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
    service = Service(ChromeDriverManager().install())
    return webdriver.Chrome(service=service, options=options)


def _parse_number(text: str) -> float | None:
    """쉼표·공백·기호 제거 후 float 변환. 실패 시 None."""
    try:
        cleaned = text.strip().replace(",", "").replace("+", "").replace("%", "")
        return float(cleaned)
    except (ValueError, AttributeError):
        return None


def fetch_kospi200_futures() -> dict:
    """Investing.com에서 코스피200 야간선물 데이터 크롤링."""
    driver = None
    try:
        driver = _make_driver()
        driver.get(FUTURES_URL)

        # 가격 요소 대기 (data-test 속성 또는 공통 가격 클래스)
        WebDriverWait(driver, 20).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "[data-test='instrument-price-last']"))
        )
        time.sleep(2)

        soup = BeautifulSoup(driver.page_source, "lxml")

        # ── 현재가 ──
        price_el = soup.select_one("[data-test='instrument-price-last']")
        price = _parse_number(price_el.get_text()) if price_el else None
        if price is None:
            raise ValueError("현재가 파싱 실패")

        # ── 등락률 ──
        pct_el = soup.select_one("[data-test='instrument-price-change-percent']")
        pct_text = pct_el.get_text().strip().strip("()") if pct_el else ""
        change_pct = _parse_number(pct_text)

        # ── 시가·고가·저가: dl>dt+dd 쌍 탐색 ──
        KEY_MAP = {
            "시가": "open", "오픈": "open",
            "고가": "high", "최고": "high",
            "저가": "low",  "최저": "low",
        }
        extras: dict = {}
        for dt in soup.find_all("dt"):
            label = dt.get_text(strip=True)
            dd = dt.find_next_sibling("dd")
            if not dd:
                continue
            for kor, key in KEY_MAP.items():
                if kor in label and key not in extras:
                    val = _parse_number(dd.get_text())
                    if val is not None:
                        extras[key] = val

        # fallback: span 인접 쌍으로도 시도
        if len(extras) < 3:
            spans = soup.find_all("span")
            for i, span in enumerate(spans[:-1]):
                label = span.get_text(strip=True)
                for kor, key in KEY_MAP.items():
                    if kor in label and key not in extras:
                        val = _parse_number(spans[i + 1].get_text())
                        if val is not None:
                            extras[key] = val

        log.info("코스피200 야간선물: price=%.2f pct=%s extras=%s", price, change_pct, extras)
        return {
            "price": price,
            "change_pct": change_pct or 0.0,
            "open": extras.get("open"),
            "high": extras.get("high"),
            "low":  extras.get("low"),
        }

    finally:
        if driver:
            driver.quit()


def fetch_market_data() -> tuple[list, Exception | None]:
    """yfinance 지표 조회. (results, futures_error) 반환."""
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

    # 코스피200 야간선물 (Investing.com 크롤링)
    futures_error = None
    try:
        futures = fetch_kospi200_futures()
        results.append({
            "label": "코스피200 야간선물",
            "kind": "futures_kr",
            **futures,
        })
    except Exception as e:
        log.error("코스피200 야간선물 조회 실패: %s", e, exc_info=True)
        futures_error = e

    return results, futures_error


def format_market_message(data: list) -> str:
    date_str = datetime.now(KST).strftime("%Y년 %m월 %d일")
    lines = [
        "<b>[금융]</b>",
        f"전일 시장현황 · {date_str}",
    ]

    for item in data:
        label = item["label"]
        price = item["price"]
        pct = item["change_pct"]
        arrow = "▲" if pct >= 0 else "▼"
        sign = "+" if pct >= 0 else ""

        if item["kind"] == "forex":
            change = item["change"]
            lines.append(
                f"- {label}: {price:,.0f}원 {arrow} {sign}{change:.0f}원 ({sign}{pct:.2f}%)"
            )
        elif item["kind"] == "futures_kr":
            o = item.get("open")
            h = item.get("high")
            lo = item.get("low")
            detail = ""
            if o is not None and h is not None and lo is not None:
                detail = f" (시:{o:,.2f} 고:{h:,.2f} 저:{lo:,.2f})"
            lines.append(
                f"- {label}: {price:,.2f} {arrow} {sign}{pct:.1f}%{detail}"
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


def _broadcast(api_url: str, subscribers: list, text: str):
    sent = failed = 0
    for chat_id in subscribers:
        try:
            resp = requests.post(
                api_url,
                json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
                timeout=10,
            )
            resp.raise_for_status()
            sent += 1
        except Exception as e:
            log.warning("발송 실패 (chat_id=%s): %s", chat_id, e)
            failed += 1
    return sent, failed


def send_market_report():
    from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

    data, futures_error = fetch_market_data()
    subscribers = _load_subscribers(TELEGRAM_CHAT_ID)
    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    # 야간선물 오류 시 별도 알림 발송 (나머지는 계속 진행)
    if futures_error:
        _broadcast(api_url, subscribers, "⚠️ [금융] 야간선물 데이터 오류\n코스피200 야간선물 정보를 가져올 수 없습니다.")

    if not data:
        log.error("시장 데이터 전체 조회 실패, 발송 중단")
        return

    msg = format_market_message(data)
    sent, failed = _broadcast(api_url, subscribers, msg)
    log.info("금융 알림 완료: 성공 %d명 / 실패 %d명", sent, failed)


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
