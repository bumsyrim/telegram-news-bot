"""
사용자별 종목 구독 관리 모듈
- stock_subscriptions.json에 저장
"""
import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

SUBS_FILE = Path("stock_subscriptions.json")


def _load() -> dict:
    if SUBS_FILE.exists():
        try:
            return json.loads(SUBS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save(data: dict):
    SUBS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def subscribe(chat_id: int, code: str, name: str, market: str) -> bool:
    """구독 등록. 이미 등록돼 있으면 False 반환."""
    data = _load()
    key = str(chat_id)
    if key not in data:
        data[key] = {}
    if code in data[key]:
        return False
    data[key][code] = {"name": name, "market": market}
    _save(data)
    log.info("종목 구독 등록: chat_id=%s code=%s name=%s", chat_id, code, name)
    return True


def unsubscribe(chat_id: int, code: str) -> bool:
    """구독 해제. 등록돼 있지 않으면 False 반환."""
    data = _load()
    key = str(chat_id)
    if key not in data or code not in data[key]:
        return False
    del data[key][code]
    if not data[key]:
        del data[key]
    _save(data)
    log.info("종목 구독 해제: chat_id=%s code=%s", chat_id, code)
    return True


def get_subscriptions(chat_id: int) -> dict:
    """사용자 구독 종목. {"code": {"name": ..., "market": ...}, ...}"""
    return _load().get(str(chat_id), {})


def get_all_subscriptions() -> dict:
    """전체 구독 현황. {"chat_id": {"code": {...}, ...}, ...}"""
    return _load()


def is_subscribed(chat_id: int, code: str) -> bool:
    return code in _load().get(str(chat_id), {})
