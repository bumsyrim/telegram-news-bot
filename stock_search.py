"""
KRX 코스피/코스닥 종목 검색 모듈
- stock_list.json에 24시간 캐시
- 메모리 내 _code_map, _name_map으로 빠른 검색
"""
import json
import logging
import time
import unicodedata
from pathlib import Path

import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

CACHE_FILE = Path("stock_list.json")
CACHE_TTL = 86400  # 24시간

_code_map: dict = {}   # "005930" → {"name": "삼성전자", "market": "KOSPI"}
_name_map: dict = {}   # "삼성전자" → {"code": "005930", "market": "KOSPI"}


def _normalize(s: str) -> str:
    return unicodedata.normalize("NFC", s).strip()


def _fetch_market(market_type: str) -> list:
    """KIND에서 시장별 종목 다운로드. market_type: 'kospi' 또는 'kosdaq'"""
    url = "http://kind.krx.co.kr/corpgeneral/corpList.do"
    params = {"method": "download", "searchType": "13"}
    if market_type == "kosdaq":
        params["marketType"] = "kosdaqMkt"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "http://kind.krx.co.kr/",
    }
    resp = requests.get(url, params=params, headers=headers, timeout=30)
    resp.raise_for_status()
    resp.encoding = "euc-kr"
    soup = BeautifulSoup(resp.text, "lxml")
    table = soup.find("table")
    if not table:
        log.warning("KIND 응답에서 table을 찾지 못함 (%s)", market_type)
        return []
    market_name = "KOSDAQ" if market_type == "kosdaq" else "KOSPI"
    stocks = []
    for row in table.find_all("tr")[1:]:
        cols = row.find_all("td")
        if len(cols) < 2:
            continue
        name = cols[0].get_text(strip=True)
        code = cols[1].get_text(strip=True).zfill(6)
        if name and len(code) == 6 and code.isdigit():
            stocks.append({"name": name, "code": code, "market": market_name})
    return stocks


def _load_cache():
    """캐시 파일 읽기. 24시간 초과 또는 없으면 None 반환."""
    if not CACHE_FILE.exists():
        return None
    try:
        data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        if time.time() - data.get("updated_at", 0) > CACHE_TTL:
            return None
        return data["stocks"]
    except Exception:
        return None


def _save_cache(stocks: list):
    CACHE_FILE.write_text(
        json.dumps({"updated_at": time.time(), "stocks": stocks}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _build_maps(stocks: list):
    global _code_map, _name_map
    _code_map = {}
    _name_map = {}
    for s in stocks:
        code = s["code"]
        name = _normalize(s["name"])
        _code_map[code] = {"name": name, "market": s["market"]}
        _name_map[name] = {"code": code, "market": s["market"]}


def load_stocks():
    """봇 시작 시 1회 호출. 캐시 유효하면 캐시 사용, 없으면 KRX에서 다운로드."""
    stocks = _load_cache()
    if stocks is None:
        log.info("KRX 종목 목록 다운로드 중...")
        try:
            stocks = _fetch_market("kospi") + _fetch_market("kosdaq")
            _save_cache(stocks)
            log.info("종목 목록 다운로드 완료: %d개", len(stocks))
        except Exception as e:
            log.error("KRX 종목 다운로드 실패: %s", e, exc_info=True)
            stocks = []
    else:
        log.info("종목 목록 캐시 로드: %d개", len(stocks))
    _build_maps(stocks)


def search_stocks(query: str, limit: int = 5) -> list:
    """종목명 또는 코드 양방향 검색. 결과: [{"code", "name", "market"}, ...]"""
    q = _normalize(query)
    # 코드 완전 일치
    if q.isdigit():
        code = q.zfill(6)
        if code in _code_map:
            m = _code_map[code]
            return [{"code": code, "name": m["name"], "market": m["market"]}]
    # 이름 완전 일치
    if q in _name_map:
        m = _name_map[q]
        return [{"code": m["code"], "name": q, "market": m["market"]}]
    # 이름 부분 일치
    results = []
    for name, m in _name_map.items():
        if q in name:
            results.append({"code": m["code"], "name": name, "market": m["market"]})
            if len(results) >= limit:
                break
    return results


def get_by_code(code: str):
    """종목 코드로 조회. {"code", "name", "market"} 또는 None"""
    code = code.strip().zfill(6)
    m = _code_map.get(code)
    if m:
        return {"code": code, "name": m["name"], "market": m["market"]}
    return None


def get_by_name(name: str):
    """종목명 완전 일치 조회. {"code", "name", "market"} 또는 None"""
    name = _normalize(name)
    m = _name_map.get(name)
    if m:
        return {"code": m["code"], "name": name, "market": m["market"]}
    return None
