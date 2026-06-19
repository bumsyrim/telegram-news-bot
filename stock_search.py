"""
KRX 코스피/코스닥/ETF 종목 검색 모듈
- stock_list.json을 읽기만 함 (외부 다운로드 없음)
- 메모리 내 _code_map, _name_map으로 빠른 검색
"""
import json
import logging
import re
import unicodedata
from pathlib import Path

log = logging.getLogger(__name__)

CACHE_FILE = Path("stock_list.json")

_code_map: dict = {}   # "005930" → {"name": "삼성전자", "market": "KOSPI"}
_name_map: dict = {}   # "삼성전자" → {"code": "005930", "market": "KOSPI"}
# 공백 제거+소문자 정규화 키 → 원본 name (부분 일치용)
_norm_map: dict = {}   # "kodex200" → "KODEX 200"


def _normalize(s: str) -> str:
    return unicodedata.normalize("NFC", s).strip()


def _search_key(s: str) -> str:
    """공백 제거 + 소문자 변환 (KODEX 200 → kodex200)"""
    return re.sub(r"\s+", "", unicodedata.normalize("NFC", s)).lower()


def _build_maps(stocks: list):
    global _code_map, _name_map, _norm_map
    _code_map = {}
    _name_map = {}
    _norm_map = {}
    for s in stocks:
        code = s["code"]
        name = _normalize(s["name"])
        _code_map[code] = {"name": name, "market": s["market"]}
        _name_map[name] = {"code": code, "market": s["market"]}
        _norm_map[_search_key(name)] = name


def load_stocks():
    """봇 시작 시 1회 호출. stock_list.json이 있으면 읽고, 없으면 빈 상태 유지."""
    if not CACHE_FILE.exists():
        log.warning("종목 데이터 없음: %s 파일이 없습니다.", CACHE_FILE)
        return
    try:
        data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        stocks = data.get("stocks", [])
        _build_maps(stocks)
        log.info("종목 목록 로드 완료: %d개", len(stocks))
    except Exception as e:
        log.error("종목 데이터 로드 실패: %s", e, exc_info=True)


def search_stocks(query: str, limit: int = 5) -> list:
    """종목명/코드 양방향 검색. 공백 제거·대소문자 무시 부분 일치 포함."""
    q = _normalize(query)
    q_key = _search_key(q)

    # 1) 코드 검색 (숫자 입력 시)
    if q.isdigit():
        # 6자리 완전 일치
        code = q.zfill(6)
        if code in _code_map:
            m = _code_map[code]
            return [{"code": code, "name": m["name"], "market": m["market"]}]
        # 6자리 미만 → 앞자리 일치 검색
        results = []
        for code, m in _code_map.items():
            if code.startswith(q):
                results.append({"code": code, "name": m["name"], "market": m["market"]})
                if len(results) >= limit:
                    break
        if results:
            return results

    # 2) 이름 완전 일치
    if q in _name_map:
        m = _name_map[q]
        return [{"code": m["code"], "name": q, "market": m["market"]}]

    # 3) 정규화 키 완전 일치 (공백·대소문자 무시)
    if q_key in _norm_map:
        name = _norm_map[q_key]
        m = _name_map[name]
        return [{"code": m["code"], "name": name, "market": m["market"]}]

    # 4) 부분 일치 (원본 이름, 공백 유지)
    results = []
    seen = set()
    for name, m in _name_map.items():
        if q in name:
            if m["code"] not in seen:
                results.append({"code": m["code"], "name": name, "market": m["market"]})
                seen.add(m["code"])
            if len(results) >= limit:
                return results

    # 5) 정규화 키 부분 일치 (공백·대소문자 무시)
    if len(results) < limit:
        for norm_name, orig_name in _norm_map.items():
            if q_key in norm_name:
                m = _name_map[orig_name]
                if m["code"] not in seen:
                    results.append({"code": m["code"], "name": orig_name, "market": m["market"]})
                    seen.add(m["code"])
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
