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

import FinanceDataReader as fdr
import pandas as pd

log = logging.getLogger(__name__)

CACHE_FILE = Path("stock_list.json")
CACHE_TTL = 86400  # 24시간

_code_map: dict = {}   # "005930" → {"name": "삼성전자", "market": "KOSPI"}
_name_map: dict = {}   # "삼성전자" → {"code": "005930", "market": "KOSPI"}


def _normalize(s: str) -> str:
    return unicodedata.normalize("NFC", s).strip()


def _fetch_all_stocks() -> list:
    """FinanceDataReader로 코스피+코스닥 전체 종목 다운로드."""
    kospi = fdr.StockListing('KOSPI')[['Code', 'Name']].copy()
    kospi['market'] = 'KOSPI'
    kosdaq = fdr.StockListing('KOSDAQ')[['Code', 'Name']].copy()
    kosdaq['market'] = 'KOSDAQ'
    df = pd.concat([kospi, kosdaq], ignore_index=True)
    stocks = [
        {"code": str(row.Code).zfill(6), "name": row.Name, "market": row.market}
        for row in df.itertuples()
        if row.Code and row.Name
    ]
    log.info("FinanceDataReader 전체 종목 수집: %d개", len(stocks))
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
    """봇 시작 시 1회 호출. 캐시 유효하면 캐시 사용, 없으면 FDR에서 다운로드."""
    stocks = _load_cache()
    if stocks is None:
        log.info("KRX 종목 목록 다운로드 중 (FinanceDataReader)...")
        try:
            stocks = _fetch_all_stocks()
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
