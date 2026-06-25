"""
미국 시장 지표 조회 -> 텔레그램 발송
야후 파이낸스 API (yfinance) + Investing.com Selenium 크롤링
"""
import argparse
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import yfinance as yf
from bs4 import BeautifulSoup
from curl_cffi import requests as cffi_req

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


def fetch_kospi() -> dict:
    """KOSPI 지수 + 원/달러 환율 실시간 조회 (yfinance ^KS11, USDKRW=X)."""
    ticker = yf.Ticker("^KS11")
    fi = ticker.fast_info

    price = fi.last_price
    prev = fi.previous_close
    if price is None or prev is None or prev == 0:
        raise ValueError("KOSPI 데이터를 가져올 수 없습니다.")

    # 최근 1분봉에서 마지막 체결 시각 추출
    hist = ticker.history(period="1d", interval="1m")
    if not hist.empty:
        last_time = hist.index[-1].tz_convert(KST)
    else:
        last_time = datetime.now(KST)

    change = price - prev
    change_pct = change / prev * 100

    result = {
        "price": price,
        "change": change,
        "change_pct": change_pct,
        "open": fi.open,
        "high": fi.day_high,
        "low": fi.day_low,
        "time": last_time,
    }

    # 원/달러 환율 추가 조회
    try:
        fx = yf.Ticker("USDKRW=X").fast_info
        fx_price = fx.last_price
        fx_prev = fx.previous_close
        if fx_price and fx_prev and fx_prev != 0:
            result["usdkrw"] = fx_price
            result["usdkrw_change"] = fx_price - fx_prev
            result["usdkrw_change_pct"] = (fx_price - fx_prev) / fx_prev * 100
    except Exception as e:
        log.warning("원/달러 조회 실패: %s", e)

    return result


def format_kospi_message(data: dict) -> str:
    price = data["price"]
    change = data["change"]
    pct = data["change_pct"]
    arrow = "▲" if change >= 0 else "▼"
    sign = "+" if change >= 0 else ""
    time_str = data["time"].strftime("%Y년 %m월 %d일 %H:%M")

    lines = [
        "<b>[코스피]</b>",
        f"{time_str} 기준",
        f"- 현재: {price:,.0f} {arrow} {sign}{change:,.0f} ({sign}{pct:.2f}%)",
    ]
    if data.get("open") is not None:
        lines.append(f"- 시가: {data['open']:,.0f}")
    if data.get("high") is not None:
        lines.append(f"- 고가: {data['high']:,.0f}")
    if data.get("low") is not None:
        lines.append(f"- 저가: {data['low']:,.0f}")
    if data.get("usdkrw") is not None:
        fx = data["usdkrw"]
        fx_chg = data["usdkrw_change"]
        fx_pct = data["usdkrw_change_pct"]
        fx_arrow = "▲" if fx_chg >= 0 else "▼"
        fx_sign = "+" if fx_chg >= 0 else ""
        lines.append(
            f"- 원/달러: {fx:,.0f}원 {fx_arrow} {fx_sign}{fx_chg:.0f}원 ({fx_sign}{fx_pct:.2f}%)"
        )
    return "\n".join(lines)


def _parse_number(text: str) -> float | None:
    """쉼표·공백·기호 제거 후 float 변환. 실패 시 None."""
    try:
        cleaned = text.strip().replace(",", "").replace("+", "").replace("%", "")
        return float(cleaned)
    except (ValueError, AttributeError):
        return None


def fetch_kospi200_futures() -> dict:
    """Investing.com에서 코스피200 야간선물 데이터 크롤링.

    curl_cffi로 Chrome TLS 지문 흉내 → Cloudflare 우회.
    Selenium headless는 Cloudflare JS 챌린지에서 Chrome 크래시 발생.
    """
    resp = cffi_req.get(
        FUTURES_URL,
        impersonate="chrome124",
        headers={
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "accept-language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
            "referer": "https://kr.investing.com/",
        },
        timeout=30,
    )
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "lxml")
    log.info("Investing.com 응답 수신 (상태: %s)", resp.status_code)

    # ── 현재가: 여러 셀렉터 순서대로 시도 ──
    PRICE_SELECTORS = [
        "[data-test='instrument-price-last']",
        "[class*='last-price']",
        "[class*='priceText']",
        "[class*='text-5xl']",
    ]
    price = None
    for sel in PRICE_SELECTORS:
        el = soup.select_one(sel)
        if el:
            val = _parse_number(el.get_text())
            if val and val > 10:
                price = val
                log.info("현재가 파싱 성공 (셀렉터: %s): %.2f", sel, price)
                break

    if price is None:
        page_title = soup.title.get_text() if soup.title else "unknown"
        raise ValueError(f"현재가 파싱 실패 (페이지: {page_title})")

    # ── 등락률 ──
    pct_el = soup.select_one("[data-test='instrument-price-change-percent']")
    pct_text = pct_el.get_text().strip().strip("()") if pct_el else ""
    change_pct = _parse_number(pct_text) or 0.0

    # ── 시가·고가·저가 ──
    # 1순위: data-test 속성
    DATA_TEST_MAP = {
        "instrument-price-open":  "open",
        "instrument-price-high":  "high",
        "instrument-price-low":   "low",
    }
    extras: dict = {}
    for attr, key in DATA_TEST_MAP.items():
        el = soup.select_one(f"[data-test='{attr}']")
        if el:
            val = _parse_number(el.get_text())
            if val is not None:
                extras[key] = val

    # 2순위: dl > dt + dd 텍스트 매칭
    KEY_MAP = {
        "시가": "open", "오픈": "open",
        "고가": "high", "최고": "high",
        "저가": "low",  "최저": "low",
    }
    if len(extras) < 3:
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

    # 3순위: span 인접 쌍
    if len(extras) < 3:
        spans = soup.find_all("span")
        for i, span in enumerate(spans[:-1]):
            label = span.get_text(strip=True)
            for kor, key in KEY_MAP.items():
                if kor in label and key not in extras:
                    val = _parse_number(spans[i + 1].get_text())
                    if val is not None:
                        extras[key] = val

    log.info("코스피200 야간선물: price=%.2f pct=%.2f extras=%s", price, change_pct, extras)
    return {
        "price": price,
        "change_pct": change_pct,
        "open": extras.get("open"),
        "high": extras.get("high"),
        "low":  extras.get("low"),
    }


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
            parts = []
            if item.get("open") is not None:
                parts.append(f"시:{item['open']:,.2f}")
            if item.get("high") is not None:
                parts.append(f"고:{item['high']:,.2f}")
            if item.get("low") is not None:
                parts.append(f"저:{item['low']:,.2f}")
            detail = f" ({' '.join(parts)})" if parts else ""
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


# ── 주요 종목 티커 (midday 조회용) ──────────────────────────
_MAJOR_STOCKS = [
    ("005930.KS", "삼성전자"),
    ("000660.KS", "SK하이닉스"),
    ("035720.KS", "카카오"),
]


def fetch_market_brief(brief_type: str) -> dict:
    """brief_type: morning / midday / closing. yfinance 기반 시장 브리핑 데이터 수집."""
    data: dict = {"brief_type": brief_type, "time": datetime.now(KST)}

    # 한국 지수
    for symbol, key in [("^KS11", "kospi"), ("^KQ11", "kosdaq"), ("^KS200", "kospi200")]:
        try:
            fi = yf.Ticker(symbol).fast_info
            price, prev = fi.last_price, fi.previous_close
            if price and prev and prev != 0:
                data[key] = {"price": price, "change": price - prev,
                             "change_pct": (price - prev) / prev * 100}
        except Exception as e:
            log.warning("%s 조회 실패: %s", symbol, e)

    # 미국 지수 (morning/closing에서 사용)
    for symbol, key in [("^IXIC", "nasdaq"), ("^GSPC", "sp500"), ("^DJI", "dow")]:
        try:
            fi = yf.Ticker(symbol).fast_info
            price, prev = fi.last_price, fi.previous_close
            if price and prev and prev != 0:
                data[key] = {"price": price, "change": price - prev,
                             "change_pct": (price - prev) / prev * 100}
        except Exception as e:
            log.warning("%s 조회 실패: %s", symbol, e)

    # 코스피200 야간선물 (morning에서 사용)
    if brief_type == "morning":
        try:
            data["futures"] = fetch_kospi200_futures()
        except Exception as e:
            log.warning("야간선물 조회 실패: %s", e)

    # 주요 종목 현황 (midday에서 사용)
    if brief_type == "midday":
        major = []
        for ticker_sym, name in _MAJOR_STOCKS:
            try:
                fi = yf.Ticker(ticker_sym).fast_info
                price, prev = fi.last_price, fi.previous_close
                if price and prev and prev != 0:
                    pct = (price - prev) / prev * 100
                    major.append({"name": name, "price": price, "change_pct": pct})
            except Exception as e:
                log.warning("%s 조회 실패: %s", ticker_sym, e)
        data["major_stocks"] = major

    return data


def _generate_ai_analysis(data: dict, brief_type: str) -> str:
    """Claude API로 시장 데이터 기반 분석 생성."""
    try:
        import anthropic
        from config import ANTHROPIC_API_KEY
        if not ANTHROPIC_API_KEY or ANTHROPIC_API_KEY == "dummy":
            return "(AI 분석: ANTHROPIC_API_KEY 미설정)"

        def _pct(d, key):
            item = d.get(key)
            if not item:
                return "데이터 없음"
            pct = item["change_pct"]
            return f"{'+' if pct >= 0 else ''}{pct:.2f}%"

        if brief_type == "morning":
            futures_info = ""
            if data.get("futures"):
                fp = data["futures"]["change_pct"]
                futures_info = f"코스피200 야간선물: {'+' if fp >= 0 else ''}{fp:.2f}%"
            context = (
                f"전일 코스피: {_pct(data, 'kospi')}, 코스닥: {_pct(data, 'kosdaq')}\n"
                f"{futures_info}\n"
                f"간밤 미국: 나스닥 {_pct(data, 'nasdaq')}, S&P500 {_pct(data, 'sp500')}, 다우 {_pct(data, 'dow')}"
            )
            prompt = f"다음 시장 데이터를 보고 오늘 한국 주식시장 출발 전망을 3문장으로 간결하게 분석해주세요:\n{context}"

        elif brief_type == "midday":
            stocks = ", ".join(
                f"{s['name']} {'+' if s['change_pct'] >= 0 else ''}{s['change_pct']:.2f}%"
                for s in data.get("major_stocks", [])
            )
            context = (
                f"코스피: {_pct(data, 'kospi')}, 코스닥: {_pct(data, 'kosdaq')}, 코스피200: {_pct(data, 'kospi200')}\n"
                f"주요 종목: {stocks}"
            )
            prompt = f"다음 장중 데이터를 보고 현재 한국 주식시장 시황을 3문장으로 간결하게 분석해주세요:\n{context}"

        else:  # closing
            context = (
                f"코스피: {_pct(data, 'kospi')}, 코스닥: {_pct(data, 'kosdaq')}, 코스피200: {_pct(data, 'kospi200')}\n"
                f"미국 시장(전일): 나스닥 {_pct(data, 'nasdaq')}, S&P500 {_pct(data, 'sp500')}, 다우 {_pct(data, 'dow')}"
            )
            prompt = f"다음 장마감 데이터를 보고 오늘 시장 특징과 내일 전망을 3문장으로 간결하게 분석해주세요:\n{context}"

        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text.strip()
    except Exception as e:
        log.warning("AI 분석 생성 실패: %s", e)
        return "(AI 분석을 생성할 수 없습니다)"


def format_market_brief(data: dict, brief_type: str, ai_analysis: str = "") -> str:
    """브리핑 데이터를 텔레그램 메시지 문자열로 포맷."""
    SEP = "──────────────────"
    now: datetime = data.get("time", datetime.now(KST))
    date_str = now.strftime("%Y-%m-%d")

    def _idx(key, name, decimals=2):
        item = data.get(key)
        if not item:
            return f"{name}: 데이터 없음"
        p, pct = item["price"], item["change_pct"]
        sign = "+" if pct >= 0 else ""
        arrow = "▲" if pct >= 0 else "▼"
        return f"{name}: {p:,.{decimals}f} {arrow} {sign}{pct:.2f}%"

    lines = []

    if brief_type == "morning":
        lines += [
            f"📊 장 시작 전 브리핑 [{date_str} 08:30 KST]",
            "",
            SEP,
            "전일 코스피",
            SEP,
            _idx("kospi",  "코스피 "),
            _idx("kosdaq", "코스닥 "),
        ]
        if data.get("futures"):
            fp = data["futures"]["change_pct"]
            fv = data["futures"]["price"]
            sign = "+" if fp >= 0 else ""
            arrow = "▲" if fp >= 0 else "▼"
            lines.append(f"야간선물: {fv:,.2f} {arrow} {sign}{fp:.2f}%")
        # 출발 전망 힌트 (야간선물 또는 미국 시장 기반)
        ref_pct = data.get("futures", {}).get("change_pct") or data.get("nasdaq", {}).get("change_pct")
        if ref_pct is not None:
            if ref_pct >= 0.5:
                hint = "→ 강보합 이상 출발 예상"
            elif ref_pct >= 0:
                hint = "→ 강보합 출발 예상"
            elif ref_pct >= -0.5:
                hint = "→ 약보합 출발 예상"
            else:
                hint = "→ 약세 출발 예상"
            lines.append(hint)
        lines += [
            "",
            SEP,
            "간밤 미국 시장",
            SEP,
            _idx("nasdaq", "나스닥  ", 0),
            _idx("sp500",  "S&P500  ", 0),
            _idx("dow",    "다우존스", 0),
        ]
        lines += [
            "",
            SEP,
            "AI 투자 전략",
            SEP,
            ai_analysis or "(분석 없음)",
            "",
            "⏰ 장 시작: 09:00 KST",
            "다음 리포트: 12:00 (장 중간)",
        ]

    elif brief_type == "midday":
        lines += [
            f"📊 장 중간 현황 [{date_str} 12:00 KST]",
            "",
            SEP,
            "현재 지수",
            SEP,
            _idx("kospi",   "코스피  "),
            _idx("kosdaq",  "코스닥  "),
            _idx("kospi200","코스피200"),
        ]
        major = data.get("major_stocks", [])
        if major:
            lines += ["", SEP, "주요 종목", SEP]
            for s in major:
                pct = s["change_pct"]
                sign = "+" if pct >= 0 else ""
                arrow = "▲" if pct >= 0 else "▼"
                lines.append(f"{s['name']}: {s['price']:,.0f}원 {arrow} {sign}{pct:.2f}%")
        lines += [
            "",
            SEP,
            "AI 시황 분석",
            SEP,
            ai_analysis or "(분석 없음)",
            "",
            "⏰ 다음 리포트: 15:30 (장 마감)",
        ]

    else:  # closing
        lines += [
            f"📊 장 마감 리포트 [{date_str} 15:30 KST]",
            "",
            SEP,
            "최종 지수",
            SEP,
            _idx("kospi",    "코스피  "),
            _idx("kosdaq",   "코스닥  "),
            _idx("kospi200", "코스피200"),
            "",
            SEP,
            "간밤 미국 시장 (참고)",
            SEP,
            _idx("nasdaq", "나스닥  ", 0),
            _idx("sp500",  "S&P500  ", 0),
            _idx("dow",    "다우존스", 0),
            "",
            SEP,
            "AI 내일 전망",
            SEP,
            ai_analysis or "(분석 없음)",
            "",
            "⏰ 다음 리포트: 내일 08:30 (장 시작 전)",
        ]

    return "\n".join(lines)


def send_market_brief_report(brief_type: str):
    """brief_type에 맞는 시장 브리핑을 전체 구독자에게 발송."""
    from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

    data = fetch_market_brief(brief_type)
    ai_text = _generate_ai_analysis(data, brief_type)
    msg = format_market_brief(data, brief_type, ai_text)

    subscribers = _load_subscribers(TELEGRAM_CHAT_ID)
    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    sent, failed = _broadcast(api_url, subscribers, msg)
    log.info("시장 브리핑(%s) 발송 완료: 성공 %d명 / 실패 %d명", brief_type, sent, failed)


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
