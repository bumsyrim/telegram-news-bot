"""
미국 시장 지표 조회 -> 텔레그램 발송
야후 파이낸스 API (yfinance) + Investing.com Selenium 크롤링
"""
import argparse
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import FinanceDataReader as fdr
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
    캐시 없음 — 호출마다 매번 새로 요청.
    """
    _start_time = datetime.now(KST)
    log.info("[야간선물] 크롤링 시작: %s", _start_time.strftime("%Y-%m-%d %H:%M:%S KST"))

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

    _elapsed = (datetime.now(KST) - _start_time).total_seconds()
    soup = BeautifulSoup(resp.text, "lxml")
    log.info("[야간선물] Investing.com 응답 수신 (상태: %s, 소요: %.2fs)", resp.status_code, _elapsed)

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

    log.info("[야간선물] 파싱 완료: price=%.2f pct=%.2f extras=%s", price, change_pct, extras)
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


def _fetch_naver_index(code: str) -> dict | None:
    """네이버 금융 sise_index 페이지에서 현재가·등락 데이터 크롤링.

    code: KOSPI / KOSDAQ / KPI200 / KOSPI200F
    반환: {"price", "change", "change_pct"} 또는 실패 시 None
    """
    url = f"https://finance.naver.com/sise/sise_index.naver?code={code}"
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"},
            timeout=10,
        )
        resp.encoding = "euc-kr"
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")

        price = change = change_pct = None

        for row in soup.select("table.type_1 tr"):
            th = row.select_one("th")
            td = row.select_one("td")
            if not th or not td:
                continue
            label = th.get_text(strip=True)
            raw = td.get_text(strip=True)
            is_down = "▼" in raw or "하락" in raw
            val = _parse_number(raw.replace("▲", "").replace("▼", ""))
            if val is None:
                continue
            if "현재가" in label:
                price = val
            elif "전일대비" in label:
                change = -val if is_down else val
            elif "등락률" in label:
                change_pct = -val if is_down else val

        if price is None:
            log.warning("네이버 지수 파싱 실패 (code=%s)", code)
            return None

        if change_pct is None and change is not None:
            prev = price - change
            change_pct = (change / prev * 100) if prev else 0.0

        log.info("네이버 지수 조회 성공 (code=%s): %.2f, %.2f%%", code, price, change_pct or 0)
        return {"price": price, "change": change or 0.0, "change_pct": change_pct or 0.0}

    except Exception as e:
        log.warning("네이버 지수 조회 실패 (code=%s): %s", code, e)
        return None


# ── 주요 종목 티커 (코스피 시총 상위 5개) ────────────────────
_MAJOR_STOCKS = [
    ("005930.KS", "삼성전자"),
    ("000660.KS", "SK하이닉스"),
    ("005935.KS", "삼성전자우"),
    ("373220.KS", "LG에너지솔루션"),
    ("005380.KS", "현대차"),
]


def fetch_market_brief(brief_type: str) -> dict:
    """brief_type: morning / midday / closing. FinanceDataReader + yfinance 기반 브리핑 데이터 수집."""
    data: dict = {"brief_type": brief_type, "time": datetime.now(KST)}

    # 한국 지수 (FinanceDataReader — 실시간에 가까운 정확한 값)
    _start = (datetime.now(KST) - timedelta(days=5)).strftime("%Y-%m-%d")
    for fdr_code, key in [("KS11", "kospi"), ("KQ11", "kosdaq"), ("KS200", "kospi200")]:
        try:
            df = fdr.DataReader(fdr_code, _start)
            if len(df) >= 2:
                price = float(df["Close"].iloc[-1])
                prev  = float(df["Close"].iloc[-2])
                data[key] = {
                    "price": price,
                    "change": price - prev,
                    "change_pct": (price - prev) / prev * 100,
                }
            elif len(df) == 1:
                price = float(df["Close"].iloc[-1])
                data[key] = {"price": price, "change": 0.0, "change_pct": 0.0}
        except Exception as e:
            log.warning("FDR %s 조회 실패: %s", fdr_code, e)

    # 미국 지수 (yfinance)
    for symbol, key in [("^IXIC", "nasdaq"), ("^GSPC", "sp500"), ("^DJI", "dow")]:
        try:
            fi = yf.Ticker(symbol).fast_info
            price, prev = fi.last_price, fi.previous_close
            if price and prev and prev != 0:
                data[key] = {"price": price, "change": price - prev,
                             "change_pct": (price - prev) / prev * 100}
        except Exception as e:
            log.warning("%s 조회 실패: %s", symbol, e)

    # 코스피200 야간선물 (morning, Investing.com curl_cffi)
    if brief_type == "morning":
        try:
            data["futures"] = fetch_kospi200_futures()
        except Exception as e:
            log.warning("야간선물 조회 실패: %s", e)

    # 주요 종목 현황 (midday + closing에서 사용)
    if brief_type in ("midday", "closing"):
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

    # 수급 데이터 (closing 전용, pykrx)
    if brief_type == "closing":
        _today = datetime.now(KST).strftime("%Y%m%d")
        try:
            from pykrx import stock as _pykrx
            adv_df = _pykrx.get_market_advancing_count(_today, "KOSPI")
            if adv_df is not None and not adv_df.empty:
                row = adv_df.iloc[-1]
                data["advancing"] = int(row.get("상승", 0))
                data["declining"] = int(row.get("하락", 0))
        except Exception as e:
            log.warning("pykrx 상승/하락 조회 실패: %s", e)

        try:
            from pykrx import stock as _pykrx
            net_df = _pykrx.get_market_net_purchases_of_equities(_today, _today, "KOSPI")
            if net_df is not None and not net_df.empty:
                val_col = next(
                    (c for c in ["순매수거래대금", "순매수", net_df.columns[-1]] if c in net_df.columns),
                    None,
                )
                if val_col:
                    supply = {}
                    for label, idx_key in [("외국인", "외국인합계"), ("기관", "기관합계"), ("개인", "개인")]:
                        if idx_key in net_df.index:
                            supply[label] = int(net_df.loc[idx_key, val_col])
                    data["supply"] = supply
        except Exception as e:
            log.warning("pykrx 수급 조회 실패: %s", e)

    return data


def _fetch_market_news() -> list:
    """네이버 검색 API로 오늘 시황 뉴스 헤드라인 3개 수집."""
    import os
    from bs4 import BeautifulSoup as _BS

    client_id = os.getenv("NAVER_CLIENT_ID", "")
    client_secret = os.getenv("NAVER_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        log.warning("NAVER_CLIENT_ID / NAVER_CLIENT_SECRET 미설정")
        return []

    today = datetime.now(KST).strftime("%Y%m%d")
    try:
        resp = requests.get(
            "https://openapi.naver.com/v1/search/news.json",
            headers={
                "X-Naver-Client-Id": client_id,
                "X-Naver-Client-Secret": client_secret,
            },
            params={"query": "코스피 시황", "display": 10, "sort": "date"},
            timeout=10,
        )
        resp.raise_for_status()

        items = resp.json().get("items", [])
        headlines = []       # 오늘 기사
        fallback_headlines = []  # 최신 기사 (오늘 이외) — 부족 시 보완용

        from datetime import datetime as _dt
        for item in items:
            title = _BS(item.get("title", ""), "lxml").get_text()
            if not title:
                continue

            pub = item.get("pubDate", "")
            is_today = True  # 날짜 파싱 실패 시 오늘 기사로 간주
            try:
                pub_dt = _dt.strptime(pub, "%a, %d %b %Y %H:%M:%S %z")
                is_today = pub_dt.strftime("%Y%m%d") == today
            except Exception:
                pass

            if is_today:
                if len(headlines) < 3:
                    headlines.append(title)
            elif len(fallback_headlines) < 3:
                fallback_headlines.append(title)

        today_count = len(headlines)

        # 오늘 기사가 3건 미만이면 최신 기사로 보완
        for t in fallback_headlines:
            if len(headlines) >= 3:
                break
            if t not in headlines:
                headlines.append(t)

        log.info("시황 뉴스 API: 오늘 %d건, 보완 %d건 (총 %d건)",
                 today_count, len(headlines) - today_count, len(headlines))
        return headlines
    except Exception as e:
        log.warning("시황 뉴스 API 실패: %s", e)
        return []


def format_market_brief(data: dict, brief_type: str, news_headlines: list = None, now_mode: bool = False) -> str:
    """브리핑 데이터를 텔레그램 메시지 문자열로 포맷."""
    if news_headlines is None:
        news_headlines = []
    SEP = "──────────────────"
    now: datetime = data.get("time", datetime.now(KST))
    date_str = now.strftime("%Y-%m-%d")
    now_str = now.strftime("%Y-%m-%d %H:%M")

    news_title = "최신 시황 뉴스" if now_mode else "오늘 시황 뉴스"

    def _news_section():
        lines = ["", SEP, news_title, SEP]
        if news_headlines:
            lines += [f"- {h}" for h in news_headlines]
        else:
            lines.append("(뉴스를 가져올 수 없습니다)")
        return lines

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
        header = f"📊 실시간 시장 현황 [{now_str} KST]" if now_mode else f"📊 장 시작 전 브리핑 [{date_str} 08:30 KST]"
        kospi_label = "코스피 현황" if now_mode else "전일 코스피"
        us_label = "미국 시장" if now_mode else "간밤 미국 시장"
        lines += [
            header,
            "",
            SEP,
            kospi_label,
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
            us_label,
            SEP,
            _idx("nasdaq", "나스닥  ", 0),
            _idx("sp500",  "S&P500  ", 0),
            _idx("dow",    "다우존스", 0),
        ]
        lines += _news_section()
        if not now_mode:
            lines += [
                "",
                "⏰ 장 시작: 09:00 KST",
                "다음 리포트: 12:00 (장 중간)",
            ]

    elif brief_type == "midday":
        header = f"📊 실시간 시장 현황 [{now_str} KST]" if now_mode else f"📊 장 중간 현황 [{date_str} 12:00 KST]"
        idx_label = "현재 지수 (조회 시점)" if now_mode else "현재 지수"
        major_label = "주요 종목 (조회 시점)" if now_mode else "주요 종목"
        lines += [
            header,
            "",
            SEP,
            idx_label,
            SEP,
            _idx("kospi",   "코스피  "),
            _idx("kosdaq",  "코스닥  "),
            _idx("kospi200","코스피200"),
        ]
        major = data.get("major_stocks", [])
        if major:
            lines += ["", SEP, major_label, SEP]
            for s in major:
                pct = s["change_pct"]
                sign = "+" if pct >= 0 else ""
                arrow = "▲" if pct >= 0 else "▼"
                lines.append(f"{s['name']}: {s['price']:,.0f}원 {arrow} {sign}{pct:.2f}%")
        lines += _news_section()
        if not now_mode:
            lines += [
                "",
                "⏰ 다음 리포트: 15:30 (장 마감)",
            ]

    else:  # closing
        header = f"📊 실시간 시장 현황 [{now_str} KST]" if now_mode else f"📊 장 마감 리포트 [{date_str} 15:30 KST]"
        idx_label = "현재 지수 (조회 시점)" if now_mode else "최종 지수"
        major_label = "주요 종목 (조회 시점)" if now_mode else "시총 상위 5개 종목"
        feature_label = "오늘 특징 (조회 시점)" if now_mode else "오늘 특징"
        lines += [
            header,
            "",
            SEP,
            idx_label,
            SEP,
            _idx("kospi",    "코스피   "),
            _idx("kosdaq",   "코스닥   "),
            _idx("kospi200", "코스피200"),
        ]

        major = data.get("major_stocks", [])
        if major:
            lines += ["", SEP, major_label, SEP]
            for s in major:
                pct = s["change_pct"]
                sign = "+" if pct >= 0 else ""
                arrow = "▲" if pct >= 0 else "▼"
                lines.append(f"{s['name']}: {s['price']:,.0f}원 {arrow} {sign}{pct:.2f}%")

        adv = data.get("advancing")
        dec = data.get("declining")
        supply = data.get("supply", {})
        if adv is not None or supply:
            lines += ["", SEP, feature_label, SEP]
            if adv is not None and dec is not None:
                lines.append(f"상승 종목: {adv:,}개 / 하락 종목: {dec:,}개")
            for label, key in [("외국인", "외국인"), ("기관  ", "기관"), ("개인  ", "개인")]:
                val = supply.get(key)
                if val is not None:
                    val_억 = val / 1e8
                    sign = "+" if val_억 >= 0 else ""
                    direction = "순매수" if val_억 >= 0 else "순매도"
                    lines.append(f"{label}: {sign}{val_억:,.0f}억 ({direction})")

        lines += _news_section()
        if not now_mode:
            lines += ["", "⏰ 다음 리포트: 내일 08:30 (장 시작 전)"]

    return "\n".join(lines)


def send_market_brief_report(brief_type: str):
    """brief_type에 맞는 시장 브리핑을 전체 구독자에게 발송."""
    from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

    data = fetch_market_brief(brief_type)
    news = _fetch_market_news()
    msg = format_market_brief(data, brief_type, news)

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
