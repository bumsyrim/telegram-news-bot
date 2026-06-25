"""
텔레그램 명령어 봇 - long-polling 방식
공개: /start /stop
관리자: /list /add /remove /interval /run
"""
import json
import logging
import re
import subprocess
import sys
import threading
import time
import unicodedata
from pathlib import Path

import requests
import schedule

from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
import stock_search
import stock_subscription

SOURCES_FILE = Path("sources.json")
WORKFLOW_FILE = Path(".github/workflows/run_bot.yml")
USERS_FILE = Path("users.json")
ENV_FILE = Path(".env")

# 누구나 사용 가능한 명령어 (NFC 정규화된 소문자로 저장)
PUBLIC_COMMANDS = {
    unicodedata.normalize("NFC", c)
    for c in {"/start", "/stop", "/날씨", "/weather", "/location", "/코스피", "/kospi", "/금융", "/finance", "/help", "/list", "/종목", "/브리핑"}
}

log = logging.getLogger(__name__)

_STOCK_HELP = (
    "\n[📈 종목 검색/구독]\n"
    "/종목 종목명       - 종목 검색\n"
    "/종목 등록 종목명  - 구독 등록\n"
    "/종목 해제 종목명  - 구독 해제\n"
    "/종목 목록         - 내 구독 종목 확인\n"
    "/종목 설정 종목명  - 알림 설정\n"
    "/종목 조회 종목명  - 즉시 뉴스 조회\n\n"
    "💡 실시간 검색:\n"
    "@ptw_aiwkeekly_bot 종목명 입력\n"
    "→ 탭으로 선택 후 버튼으로 동작 선택"
)

_BRIEF_HELP = (
    "\n[📊 시장 브리핑]\n"
    "/브리핑 now     - 현재 시점 시장 브리핑\n"
    "/브리핑 morning - 장 시작 전 브리핑\n"
    "/브리핑 midday  - 장 중간 브리핑\n"
    "/브리핑 closing - 장 마감 브리핑"
)

_COMMON_HELP = (
    "[📋 공통 명령어]\n"
    "/start - 뉴스 구독 등록\n"
    "/stop - 뉴스 구독 취소\n"
    "/list - 등록된 사이트 목록\n"
    "/날씨 - 내 위치 기준 날씨 조회\n"
    "/location 위치명 - 날씨 위치 변경\n"
    "/코스피 - 코스피 지수 조회\n"
    "/금융 - 미국 시장 지표 조회\n"
    "/브리핑 now - 현재 시점 시장 브리핑\n"
    "/종목 - 종목 검색/구독\n"
    "/help - 도움말"
) + _STOCK_HELP + _BRIEF_HELP

HELP_TEXT = (
    "[📋 사용 가능한 명령어]\n"
    "/start - 뉴스 구독 등록\n"
    "/stop - 뉴스 구독 취소\n"
    "/list - 등록된 사이트 목록\n"
    "/날씨 - 내 위치 기준 날씨 조회\n"
    "/location 위치명 - 날씨 위치 변경\n"
    "/코스피 - 코스피 지수 조회\n"
    "/금융 - 미국 시장 지표 조회\n"
    "/브리핑 now - 현재 시점 시장 브리핑\n"
    "/종목 - 종목 검색/구독\n"
    "/help - 도움말"
) + _STOCK_HELP + _BRIEF_HELP

ADMIN_HELP_TEXT = (
    "[👑 관리자 명령어]\n"
    "/list - 등록된 사이트 목록\n"
    "/add URL 이름 - 사이트 추가\n"
    "/remove 이름 - 사이트 삭제\n"
    "/interval 숫자 - 실행 주기 변경\n"
    "/run - 즉시 뉴스 체크 실행\n"
    "/subscribers - 구독자 목록 조회\n"
    "/admins - 관리자 목록 조회\n"
    "/promote chat_id - 관리자 추가\n"
    "/demote chat_id - 관리자 권한 제거\n"
    "\n"
) + _COMMON_HELP + _BRIEF_HELP


# ── 관리자 목록 읽기/쓰기 (.env ADMIN_IDS) ───────────────

def load_admin_ids() -> set:
    """TELEGRAM_CHAT_ID + .env의 ADMIN_IDS를 합쳐 반환. 매번 파일을 직접 읽어 최신값 반영."""
    ids = {str(TELEGRAM_CHAT_ID)}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("ADMIN_IDS="):
                value = line[len("ADMIN_IDS="):].strip()
                ids.update(v.strip() for v in value.split(",") if v.strip())
    return ids


def save_admin_ids(ids: set):
    """TELEGRAM_CHAT_ID를 제외한 추가 관리자 ID를 .env의 ADMIN_IDS에 저장."""
    to_store = ids - {str(TELEGRAM_CHAT_ID)}
    value = ",".join(sorted(to_store))
    if not ENV_FILE.exists():
        return
    content = ENV_FILE.read_text(encoding="utf-8")
    if re.search(r"^ADMIN_IDS=", content, re.MULTILINE):
        new_content = re.sub(r"^ADMIN_IDS=.*$", f"ADMIN_IDS={value}", content, flags=re.MULTILINE)
    else:
        new_content = content.rstrip() + f"\nADMIN_IDS={value}\n"
    ENV_FILE.write_text(new_content, encoding="utf-8")


def is_admin(chat_id: int) -> bool:
    return str(chat_id) in load_admin_ids()


# ── users.json 읽기/쓰기 ─────────────────────────────────

def load_users() -> dict:
    if USERS_FILE.exists():
        data = json.loads(USERS_FILE.read_text(encoding="utf-8"))
        subs = data.get("subscribers", {})
        # 구형 포맷(리스트) → 신형(딕셔너리) 자동 마이그레이션
        if isinstance(subs, list):
            data["subscribers"] = {str(uid): {} for uid in subs}
            save_users(data)
        return data
    return {"subscribers": {}}


def save_users(data: dict):
    USERS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ── sources.json 읽기/쓰기 ────────────────────────────────

def load_sources() -> dict:
    if SOURCES_FILE.exists():
        return json.loads(SOURCES_FILE.read_text(encoding="utf-8"))
    return {"interval_minutes": 60, "sources": []}


def save_sources(data: dict):
    SOURCES_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ── 텔레그램 메시지 발송 ──────────────────────────────────

def send_message(chat_id: int, text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    requests.post(
        url,
        json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
        timeout=10,
    )


def send_message_with_markup(chat_id: int, text: str, keyboard: list):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    requests.post(
        url,
        json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "reply_markup": {"inline_keyboard": keyboard},
        },
        timeout=10,
    )


# ── GitHub Actions workflow cron 업데이트 ─────────────────

def interval_to_cron(minutes: int) -> str:
    if minutes < 60:
        return f"*/{minutes} * * * *"
    hours = minutes // 60
    if hours == 1:
        return "0 * * * *"
    return f"0 */{hours} * * *"


def update_workflow_cron(minutes: int) -> bool:
    if not WORKFLOW_FILE.exists():
        return False
    content = WORKFLOW_FILE.read_text(encoding="utf-8")
    cron = interval_to_cron(minutes)
    new_content = re.sub(
        r"(cron:\s*')[^']*(')",
        rf"\g<1>{cron}\g<2>",
        content,
    )
    if new_content == content:
        return False
    WORKFLOW_FILE.write_text(new_content, encoding="utf-8")
    log.info("workflow cron 업데이트: %s", cron)
    return True


# ── 명령어 핸들러 ─────────────────────────────────────────

def handle_start(chat_id: int, username: str = ""):
    data = load_users()
    key = str(chat_id)
    if key in data["subscribers"]:
        loc = data["subscribers"][key].get("location", "서울 (기본값)")
        send_message(chat_id, f"이미 구독 중입니다. (날씨 위치: {loc})\n/stop 으로 구독을 취소할 수 있습니다.")
        return
    data["subscribers"][key] = {}
    save_users(data)
    name = f"@{username}" if username else str(chat_id)
    log.info("구독 등록: %s", name)
    send_message(
        chat_id,
        "✅ 구독이 완료되었습니다!\n새 글이 발행되면 알림을 보내드립니다.\n\n"
        "/location 위치명 으로 날씨 위치를 설정할 수 있습니다.\n"
        "/stop 으로 구독을 취소할 수 있습니다.",
    )


def handle_stop(chat_id: int, username: str = ""):
    data = load_users()
    key = str(chat_id)
    if key not in data["subscribers"]:
        send_message(chat_id, "구독 중이 아닙니다.\n/start 로 구독할 수 있습니다.")
        return
    del data["subscribers"][key]
    save_users(data)
    name = f"@{username}" if username else str(chat_id)
    log.info("구독 취소: %s", name)
    send_message(chat_id, "구독이 취소되었습니다.\n언제든지 /start 로 다시 구독할 수 있습니다.")


def handle_weather_query(chat_id: int):
    from weather import get_subscriber_location, fetch_weather, fetch_air_quality, format_weather_message
    from config import WEATHER_API_KEY

    if not WEATHER_API_KEY:
        send_message(chat_id, "❌ 날씨 API 키가 설정되지 않았습니다.")
        return

    send_message(chat_id, "🔍 날씨 조회 중...")
    loc = get_subscriber_location(chat_id)
    weather, air = {}, {}

    try:
        weather = fetch_weather(loc["nx"], loc["ny"], WEATHER_API_KEY)
    except Exception as e:
        log.error("날씨 조회 실패: %s", e, exc_info=True)

    try:
        air = fetch_air_quality(loc["station"], WEATHER_API_KEY)
    except Exception as e:
        log.error("대기질 조회 실패: %s", e, exc_info=True)

    if not weather and not air:
        send_message(chat_id, "❌ 날씨 정보를 가져올 수 없습니다. 잠시 후 다시 시도해주세요.")
        return

    msg = format_weather_message(loc["display"], weather, air)
    send_message(chat_id, msg)


def handle_finance_query(chat_id: int):
    from market import fetch_market_data, format_market_message

    send_message(chat_id, "🔍 시장 데이터 조회 중...")
    try:
        data, futures_error = fetch_market_data()
        if futures_error:
            send_message(chat_id, "⚠️ 야간선물 데이터를 가져올 수 없습니다. 나머지 항목만 표시합니다.")
        if not data:
            send_message(chat_id, "❌ 시장 데이터를 가져올 수 없습니다. 잠시 후 다시 시도해주세요.")
            return
        send_message(chat_id, format_market_message(data))
    except Exception as e:
        log.error("금융 조회 실패: %s", e, exc_info=True)
        send_message(chat_id, "❌ 시장 데이터를 가져올 수 없습니다. 잠시 후 다시 시도해주세요.")


def handle_brief_cmd(chat_id: int, args: str):
    from market import fetch_market_brief, format_market_brief, _fetch_market_news
    import datetime as _dt

    arg = args.strip().lower()
    if not arg:
        send_message(
            chat_id,
            "📊 <b>시장 브리핑</b>\n\n"
            "/브리핑 now     - 현재 시점 자동 판단\n"
            "/브리핑 morning - 장 시작 전 브리핑\n"
            "/브리핑 midday  - 장 중간 브리핑\n"
            "/브리핑 closing - 장 마감 브리핑\n\n"
            "자동 발송: 08:30 / 12:00 / 15:30 KST"
        )
        return

    if arg == "now":
        # 서버 로컬 시간과 무관하게 KST(UTC+9) 기준으로 판단
        _KST = _dt.timezone(_dt.timedelta(hours=9))
        now_kst = _dt.datetime.now(_KST)
        h, m = now_kst.hour, now_kst.minute
        if h < 9:                        # 00:00~08:59 KST → 장 시작 전
            brief_type = "morning"
        elif h < 15 or (h == 15 and m == 0):  # 09:00~15:00 KST → 장 중
            brief_type = "midday"
        else:                            # 15:00+ KST → 장 마감
            brief_type = "closing"
    elif arg in ("morning", "midday", "closing"):
        brief_type = arg
    else:
        send_message(chat_id, "❌ 사용법: /브리핑 now | morning | midday | closing")
        return

    send_message(chat_id, f"🔍 {brief_type} 브리핑 조회 중...")
    try:
        data = fetch_market_brief(brief_type)
        news = _fetch_market_news()
        msg = format_market_brief(data, brief_type, news)
        send_message(chat_id, msg)
    except Exception as e:
        log.error("브리핑 조회 실패: %s", e, exc_info=True)
        send_message(chat_id, "❌ 브리핑을 가져올 수 없습니다. 잠시 후 다시 시도해주세요.")


def handle_kospi_query(chat_id: int):
    from market import fetch_kospi, format_kospi_message

    send_message(chat_id, "🔍 코스피 조회 중...")
    try:
        data = fetch_kospi()
        send_message(chat_id, format_kospi_message(data))
    except Exception as e:
        log.error("코스피 조회 실패: %s", e, exc_info=True)
        send_message(chat_id, "❌ 코스피 정보를 가져올 수 없습니다. 잠시 후 다시 시도해주세요.")


def handle_list(chat_id: int):
    src = load_sources()
    admin = is_admin(chat_id)
    if not src["sources"]:
        msg = "등록된 사이트가 없습니다."
        if admin:
            msg += "\n/add URL 이름 으로 추가하세요."
        send_message(chat_id, msg)
        return
    if admin:
        usr = load_users()
        header = f"📋 <b>등록된 사이트 목록</b> (주기: {src['interval_minutes']}분 / 구독자: {len(usr['subscribers'])}명)\n"
    else:
        header = "📋 <b>등록된 사이트 목록</b>\n"
    lines = [header]
    for i, s in enumerate(src["sources"], 1):
        lines.append(f"{i}. <b>{s['name']}</b>\n   {s['url']}")
    send_message(chat_id, "\n".join(lines))


def handle_add(chat_id: int, args: str):
    parts = args.split(maxsplit=1)
    if len(parts) < 2:
        send_message(chat_id, "사용법: /add URL 이름\n예: /add https://brunch.co.kr/@user 내블로그")
        return
    url, name = parts[0].strip(), parts[1].strip()
    if not url.startswith("http"):
        send_message(chat_id, "올바른 URL을 입력해주세요. (http:// 또는 https://로 시작)")
        return
    data = load_sources()
    if any(s["name"] == name for s in data["sources"]):
        send_message(chat_id, f"이미 <b>{name}</b> 이름의 사이트가 있습니다.")
        return
    source_type = "brunch" if "brunch.co.kr" in url else "generic"
    data["sources"].append({"name": name, "url": url, "type": source_type})
    save_sources(data)
    log.info("사이트 추가: %s (%s)", name, url)
    send_message(chat_id, f"✅ <b>{name}</b> 사이트가 추가되었습니다.\nURL: {url}")


def handle_remove(chat_id: int, args: str):
    name = args.strip()
    if not name:
        send_message(chat_id, "사용법: /remove 이름\n예: /remove 내블로그")
        return
    data = load_sources()
    before = len(data["sources"])
    data["sources"] = [s for s in data["sources"] if s["name"] != name]
    if len(data["sources"]) == before:
        names = [s["name"] for s in data["sources"]]
        send_message(chat_id, f"<b>{name}</b> 이름의 사이트를 찾을 수 없습니다.\n등록된 이름: {', '.join(names) or '없음'}")
    else:
        save_sources(data)
        log.info("사이트 삭제: %s", name)
        send_message(chat_id, f"🗑️ <b>{name}</b> 사이트가 삭제되었습니다.")


def handle_interval(chat_id: int, args: str):
    arg = args.strip()
    if not arg.isdigit():
        send_message(chat_id, "사용법: /interval 숫자\n예: /interval 30  (30분마다 실행)")
        return
    minutes = int(arg)
    if minutes < 5:
        send_message(chat_id, "최소 실행 주기는 5분입니다. (GitHub Actions 제한)")
        return
    data = load_sources()
    data["interval_minutes"] = minutes
    save_sources(data)
    workflow_updated = update_workflow_cron(minutes)

    msg = f"⏱️ 실행 주기가 <b>{minutes}분</b>으로 변경되었습니다."
    if workflow_updated:
        cron = interval_to_cron(minutes)
        msg += f"\n\nGitHub Actions workflow도 업데이트됐습니다.\n cron: <code>{cron}</code>"
        msg += "\n\n⚠️ <b>변경사항을 git push해야 적용됩니다.</b>"
    send_message(chat_id, msg)


def handle_location(chat_id: int, args: str):
    from weather import LOCATION_MAP

    # 구독 여부 확인
    user_data_store = load_users()
    key = str(chat_id)
    if key not in user_data_store["subscribers"]:
        send_message(chat_id, "먼저 /start 로 구독하신 후 위치를 설정할 수 있습니다.")
        return

    query = args.strip()
    if not query:
        current = user_data_store["subscribers"][key].get("location", "서울 (기본값)")
        sample = ["서울", "서울 강남구", "서울 마포구", "부산", "대구", "인천", "대전", "광주"]
        send_message(
            chat_id,
            f"📍 내 현재 위치: <b>{current}</b>\n\n"
            f"사용법: /location 위치명\n"
            f"예시: {' / '.join(sample)}\n\n"
            f"서울 각 구(강남구, 마포구 등) 및 주요 도시 지원",
        )
        return

    # 위치 검색 — 정확히 일치 우선, 부분 일치 fallback
    if query in LOCATION_MAP:
        matched = query
    else:
        candidates = [k for k in LOCATION_MAP if query in k]
        if not candidates:
            send_message(chat_id, f"'{query}' 위치를 찾을 수 없습니다.\n/location 으로 지원 위치 목록을 확인하세요.")
            return
        if len(candidates) > 1:
            send_message(chat_id, f"여러 결과가 있습니다:\n{', '.join(sorted(candidates)[:10])}\n\n더 구체적으로 입력해주세요.")
            return
        matched = candidates[0]

    # 개인 위치 저장
    user_data_store["subscribers"][key]["location"] = matched
    save_users(user_data_store)
    display = LOCATION_MAP[matched]["display"]
    log.info("위치 변경: chat_id=%s → %s", chat_id, display)
    send_message(chat_id, f"📍 내 날씨 위치가 <b>{display}</b>으로 변경되었습니다.\n/날씨 로 바로 확인해보세요.")


def handle_run(chat_id: int):
    send_message(chat_id, "🔄 뉴스 체크를 시작합니다...")
    bot_path = Path(__file__).parent / "bot.py"
    try:
        result = subprocess.run(
            [sys.executable, str(bot_path), "--once"],
            capture_output=True,
            text=True,
            timeout=300,
            cwd=str(bot_path.parent),
        )
        if result.returncode == 0:
            send_message(chat_id, "✅ 뉴스 체크가 완료되었습니다.")
        else:
            err = result.stderr[-500:] if result.stderr else "알 수 없는 오류"
            send_message(chat_id, f"❌ 오류 발생:\n<code>{err}</code>")
    except subprocess.TimeoutExpired:
        send_message(chat_id, "⏰ 시간 초과 (5분). 사이트 응답이 느린 것 같습니다.")
    except Exception as e:
        send_message(chat_id, f"❌ 실행 오류: {e}")


def handle_subscribers(chat_id: int):
    data = load_users()
    subs = data.get("subscribers", {})
    if not subs:
        send_message(chat_id, "현재 구독자가 없습니다.")
        return
    lines = [f"👥 <b>구독자 목록</b> (총 {len(subs)}명)\n"]
    for uid, info in subs.items():
        loc = info.get("location", "기본값")
        lines.append(f"• <code>{uid}</code>  📍{loc}")
    send_message(chat_id, "\n".join(lines))


def handle_admins(chat_id: int):
    ids = load_admin_ids()
    lines = [f"👑 <b>관리자 목록</b> (총 {len(ids)}명)\n"]
    for uid in sorted(ids):
        tag = " (기본 관리자)" if uid == str(TELEGRAM_CHAT_ID) else ""
        lines.append(f"• <code>{uid}</code>{tag}")
    send_message(chat_id, "\n".join(lines))


def handle_promote(chat_id: int, args: str):
    target = args.strip()
    if not target.lstrip("-").isdigit():
        send_message(chat_id, "사용법: /promote chat_id\n예: /promote 123456789")
        return
    if target == str(TELEGRAM_CHAT_ID):
        send_message(chat_id, "기본 관리자는 이미 최고 권한을 가지고 있습니다.")
        return
    ids = load_admin_ids()
    if target in ids:
        send_message(chat_id, f"<code>{target}</code> 은(는) 이미 관리자입니다.")
        return
    ids.add(target)
    save_admin_ids(ids)
    log.info("관리자 추가: %s (by %s)", target, chat_id)
    send_message(chat_id, f"✅ <code>{target}</code> 을(를) 관리자로 추가했습니다.")


def handle_demote(chat_id: int, args: str):
    target = args.strip()
    if not target.lstrip("-").isdigit():
        send_message(chat_id, "사용법: /demote chat_id\n예: /demote 123456789")
        return
    if target == str(TELEGRAM_CHAT_ID):
        send_message(chat_id, "❌ 기본 관리자는 권한을 제거할 수 없습니다.")
        return
    ids = load_admin_ids()
    if target not in ids:
        send_message(chat_id, f"<code>{target}</code> 은(는) 관리자가 아닙니다.")
        return
    ids.discard(target)
    save_admin_ids(ids)
    log.info("관리자 제거: %s (by %s)", target, chat_id)
    send_message(chat_id, f"🗑️ <code>{target}</code> 의 관리자 권한을 제거했습니다.")


def _execute_stock_action(chat_id: int, code: str, action: str):
    """종목 코드와 action으로 직접 실행. send_message로 결과 전송."""
    s = stock_search.get_by_code(code)
    if not s:
        send_message(chat_id, f"❌ 종목코드 <code>{code}</code>를 찾을 수 없습니다.")
        return

    if action == "register":
        ok = stock_subscription.subscribe(chat_id, s["code"], s["name"], s["market"])
        if ok:
            send_message(chat_id, f"✅ <b>{s['name']}</b> (<code>{s['code']}</code>, {s['market']}) 구독 등록했습니다.")
        else:
            send_message(chat_id, f"이미 <b>{s['name']}</b> (<code>{s['code']}</code>)를 구독 중입니다.")

    elif action == "unsubscribe":
        ok = stock_subscription.unsubscribe(chat_id, s["code"])
        if ok:
            send_message(chat_id, f"🗑️ <b>{s['name']}</b> (<code>{s['code']}</code>) 구독 해제했습니다.")
        else:
            send_message(chat_id, f"❌ <code>{s['code']}</code>는 구독 중이 아닙니다.\n/종목 목록으로 구독 종목을 확인하세요.")

    elif action == "config":
        handle_stock_settings(chat_id, s["code"])

    elif action == "report":
        send_message(chat_id, "🔍 뉴스/토론방 조회 중...")
        try:
            from stock_news import fetch_news_for_report
            cfg = stock_subscription.get_settings(chat_id, s["code"])
            msg = fetch_news_for_report(
                s["code"], s["name"], s["market"],
                news_count=cfg["news_count"],
                board_count=cfg["board_count"],
                days=cfg["days"],
            )
            send_message(chat_id, msg if msg else f"<b>{s['name']}</b> [{s['code']}]\n새로운 소식이 없습니다.")
        except Exception as e:
            log.error("즉시 조회 실패: %s", e, exc_info=True)
            send_message(chat_id, "❌ 뉴스 조회 중 오류가 발생했습니다.")


def _find_stock_with_buttons(chat_id: int, query: str, action: str):
    """query로 종목 검색 후 action 실행.
    결과 1개 → 바로 실행 / 여러 개 → 버튼 목록 / 0개 → 안내 메시지.
    """
    results = stock_search.search_stocks(query, limit=8)
    if not results:
        send_message(chat_id, f"'{query}'에 대한 검색 결과가 없습니다.\n종목명 또는 코드를 입력해보세요.")
        return
    if len(results) == 1:
        _execute_stock_action(chat_id, results[0]["code"], action)
        return
    market_labels = {"KOSPI": "코스피", "KOSDAQ": "코스닥", "ETF": "ETF"}
    keyboard = []
    for r in results:
        ml = market_labels.get(r["market"], r["market"])
        keyboard.append([{
            "text": f"{r['name']}  {r['code']}  {ml}",
            "callback_data": f"{action}_{r['code']}",
        }])
    send_message_with_markup(chat_id, f"🔍 <b>'{query}' 검색 결과</b>", keyboard)


def handle_stock_settings(chat_id: int, code: str):
    """종목별 뉴스/토론방 건수·기간 설정 메뉴 전송."""
    s = stock_search.get_by_code(code)
    if not s:
        send_message(chat_id, f"❌ 종목코드 <code>{code}</code>를 찾을 수 없습니다.")
        return
    if not stock_subscription.is_subscribed(chat_id, s["code"]):
        send_message(chat_id, f"❌ <b>{s['name']}</b>은(는) 구독 중이 아닙니다.\n먼저 구독 등록 후 설정할 수 있습니다.")
        return

    cfg = stock_subscription.get_settings(chat_id, s["code"])
    c = s["code"]
    text = (
        f"<b>{s['name']}</b> [<code>{c}</code>] 설정\n"
        f"현재: 뉴스 {cfg['news_count']}건 · 토론방 {cfg['board_count']}건 · {cfg['days']}일"
    )
    keyboard = [
        [{"text": "〔 뉴스 건수 〕", "callback_data": "noop"}],
        [
            {"text": "0건", "callback_data": f"news_count_{c}_0"},
            {"text": "3건", "callback_data": f"news_count_{c}_3"},
            {"text": "5건", "callback_data": f"news_count_{c}_5"},
        ],
        [{"text": "〔 토론방 건수 〕", "callback_data": "noop"}],
        [
            {"text": "0건", "callback_data": f"board_count_{c}_0"},
            {"text": "3건", "callback_data": f"board_count_{c}_3"},
            {"text": "5건", "callback_data": f"board_count_{c}_5"},
        ],
        [{"text": "〔 조회 기간 〕", "callback_data": "noop"}],
        [
            {"text": "7일", "callback_data": f"days_{c}_7"},
            {"text": "14일", "callback_data": f"days_{c}_14"},
            {"text": "30일", "callback_data": f"days_{c}_30"},
        ],
    ]
    send_message_with_markup(chat_id, text, keyboard)


def handle_stock_cmd(chat_id: int, args: str):
    N = lambda s: unicodedata.normalize("NFC", s)
    parts = args.strip().split(maxsplit=1)
    subcmd = N(parts[0]) if parts else ""
    rest = parts[1].strip() if len(parts) > 1 else ""

    if not subcmd:
        send_message(
            chat_id,
            "📌 <b>종목 검색 방법</b>\n\n"
            "1️⃣ <b>명령어 방식:</b>\n"
            "/종목 삼성전자\n"
            "/종목 005930\n"
            "→ 결과에서 버튼으로 구독등록/즉시조회 선택\n\n"
            "2️⃣ <b>실시간 팝업 방식 (더 편함):</b>\n"
            "@ptw_aiwkeekly_bot 삼성\n"
            "→ 타이핑하면서 바로 목록 팝업\n"
            "→ 탭 한 번으로 선택 후 아래 버튼 표시:\n"
            "   [ ✅ 구독 등록 ] [ 🔕 구독 해제 ]\n"
            "   [ 📊 즉시 조회 ] [ ❌ 취소 ]\n\n"
            "<b>기타 명령어:</b>\n"
            "/종목 목록          - 내 구독 종목 전체\n"
            "/종목 해제 &lt;코드&gt;   - 구독 해제",
        )
        return

    if subcmd == N("등록"):
        if not rest:
            send_message(chat_id, "사용법: /종목 등록 &lt;종목명 또는 코드&gt;\n예: /종목 등록 삼성전자")
            return
        _find_stock_with_buttons(chat_id, rest.strip(), "register")
        return

    if subcmd == N("해제"):
        if not rest:
            send_message(chat_id, "사용법: /종목 해제 &lt;종목명 또는 코드&gt;\n예: /종목 해제 삼성전자")
            return
        _find_stock_with_buttons(chat_id, rest.strip(), "unsubscribe")
        return

    if subcmd == N("목록"):
        subs = stock_subscription.get_subscriptions(chat_id)
        if not subs:
            send_message(chat_id, "구독 중인 종목이 없습니다.\n/종목 등록 &lt;코드&gt;로 등록하세요.")
            return
        market_labels = {"KOSPI": "코스피", "KOSDAQ": "코스닥", "ETF": "ETF"}
        _DEFAULT = {"news_count": 5, "board_count": 3, "days": 7}
        lines = [f"📋 <b>내 구독 종목</b> (총 {len(subs)}개)"]
        for code, info in subs.items():
            ml = market_labels.get(info["market"], info["market"])
            nc = info.get("news_count", _DEFAULT["news_count"])
            bc = info.get("board_count", _DEFAULT["board_count"])
            dy = info.get("days", _DEFAULT["days"])
            cfg = f"뉴스 {nc}건 · 토론방 {bc}건 · {dy}일"
            if nc == _DEFAULT["news_count"] and bc == _DEFAULT["board_count"] and dy == _DEFAULT["days"]:
                cfg += " (기본값)"
            lines.append(
                f"\n• <b>{info['name']}</b> [<code>{code}</code>] {ml}\n"
                f"  {cfg}\n"
                f"  /종목 설정 {code}  |  /종목 해제 {code}"
            )
        send_message(chat_id, "\n".join(lines))
        return

    if subcmd == N("조회"):
        if not rest:
            send_message(chat_id, "사용법: /종목 조회 &lt;종목명 또는 코드&gt;\n예: /종목 조회 삼성전자")
            return
        _find_stock_with_buttons(chat_id, rest.strip(), "report")
        return

    if subcmd == N("설정"):
        if not rest:
            send_message(chat_id, "사용법: /종목 설정 &lt;종목명 또는 코드&gt;\n예: /종목 설정 삼성전자")
            return
        _find_stock_with_buttons(chat_id, rest.strip(), "config")
        return

    # 그 외: 검색어로 처리
    query = args.strip()
    results = stock_search.search_stocks(query)
    if not results:
        send_message(chat_id, f"'{query}'에 대한 검색 결과가 없습니다.\n종목명 또는 6자리 코드를 입력해보세요.")
        return
    send_message(chat_id, f"🔍 <b>'{query}' 검색 결과</b>")
    market_labels = {"KOSPI": "코스피", "KOSDAQ": "코스닥", "ETF": "ETF"}
    for r in results:
        label = market_labels.get(r["market"], r["market"])
        send_message_with_markup(
            chat_id,
            f"📌 <b>{r['name']}</b> (<code>{r['code']}</code>) [{label}]",
            [[
                {"text": "✅ 구독 등록", "callback_data": f"subscribe_{r['code']}"},
                {"text": "📊 즉시 조회", "callback_data": f"report_{r['code']}"},
                {"text": "💰 현재가", "callback_data": f"price_{r['code']}"},
            ]],
        )
    send_message(chat_id, f"💡 더 빠른 검색: @ptw_aiwkeekly_bot {query}")


# ── InlineQuery 핸들러 ───────────────────────────────────

def handle_inline_query(inline_query_id: str, query: str):
    """@봇이름 <검색어> 입력 시 실시간 종목 드롭다운 반환."""
    q = query.strip()
    results = []
    if q:
        stocks = stock_search.search_stocks(q, limit=10)
        for s in stocks:
            market_label = {"KOSPI": "코스피", "KOSDAQ": "코스닥", "ETF": "ETF"}.get(s["market"], s["market"])
            msg_text = (
                f"📌 <b>{s['name']}</b> (<code>{s['code']}</code>) [{market_label}]\n"
                f"원하는 작업을 선택하세요."
            )
            results.append({
                "type": "article",
                "id": s["code"],
                "title": f"{s['name']} ({s['code']})",
                "description": f"{market_label} · 코드: {s['code']}",
                "input_message_content": {
                    "message_text": msg_text,
                    "parse_mode": "HTML",
                },
                "reply_markup": {
                    "inline_keyboard": [
                        [
                            {"text": "✅ 구독 등록", "callback_data": f"subscribe_{s['code']}"},
                            {"text": "🔕 구독 해제", "callback_data": f"unsubscribe_{s['code']}"},
                        ],
                        [
                            {"text": "📊 즉시 조회", "callback_data": f"report_{s['code']}"},
                            {"text": "💰 현재가", "callback_data": f"price_{s['code']}"},
                        ],
                    ]
                },
            })
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerInlineQuery"
    requests.post(
        url,
        json={"inline_query_id": inline_query_id, "results": results, "cache_time": 30},
        timeout=10,
    )
    log.debug("InlineQuery 응답: query=%r results=%d개", q, len(results))


# ── CallbackQuery 핸들러 ──────────────────────────────────

def _answer_callback(callback_query_id: str, text: str = "", alert: bool = False):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery"
    requests.post(
        url,
        json={"callback_query_id": callback_query_id, "text": text, "show_alert": alert},
        timeout=10,
    )


def _delete_message(chat_id: int, message_id: int):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/deleteMessage"
    requests.post(url, json={"chat_id": chat_id, "message_id": message_id}, timeout=10)


def _edit_message(chat_id: int, message_id: int, text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageText"
    requests.post(
        url,
        json={"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": "HTML"},
        timeout=10,
    )


def _fetch_price_msg(code: str, market: str, name: str) -> str:
    """yfinance로 현재가 조회. 실패 시 None 반환."""
    import yfinance as yf
    import datetime

    suffix = ".KQ" if market == "KOSDAQ" else ".KS"
    df = yf.Ticker(f"{code}{suffix}").history(period="2d")
    if df.empty:
        return None
    price = df["Close"].iloc[-1]
    prev_close = df["Close"].iloc[-2] if len(df) >= 2 else price
    change = price - prev_close
    pct = (change / prev_close * 100) if prev_close else 0
    sign = "+" if change >= 0 else ""
    now_kst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    return (
        f"<b>{name}</b> [{code}]\n"
        f"현재가: {price:,.0f}원\n"
        f"전일대비: {sign}{change:,.0f}원 ({sign}{pct:.2f}%)\n"
        f"{now_kst.strftime('%Y-%m-%d %H:%M')} 기준 (KST)"
    )


def handle_callback_query(callback_query_id: str, chat_id: int, message_id: int, data: str):
    if data.startswith("register_"):
        code = data[len("register_"):]
        s = stock_search.get_by_code(code)
        if not s:
            _answer_callback(callback_query_id, "❌ 종목 정보를 찾을 수 없습니다.", alert=True)
            return
        ok = stock_subscription.subscribe(chat_id, s["code"], s["name"], s["market"])
        if ok:
            _answer_callback(callback_query_id, f"✅ {s['name']} 구독 등록 완료!")
            _edit_message(chat_id, message_id,
                          f"✅ <b>{s['name']}</b> (<code>{s['code']}</code>, {s['market']}) 구독 등록했습니다.")
        else:
            _answer_callback(callback_query_id, f"이미 {s['name']}을(를) 구독 중입니다.", alert=True)

    elif data.startswith("config_"):
        code = data[len("config_"):]
        _answer_callback(callback_query_id)
        handle_stock_settings(chat_id, code)

    elif data.startswith("subscribe_"):
        code = data[len("subscribe_"):]
        s = stock_search.get_by_code(code)
        if not s:
            _answer_callback(callback_query_id, "❌ 종목 정보를 찾을 수 없습니다.", alert=True)
            return
        ok = stock_subscription.subscribe(chat_id, s["code"], s["name"], s["market"])
        if ok:
            _answer_callback(callback_query_id, f"✅ {s['name']} 구독 등록 완료!")
            _edit_message(
                chat_id, message_id,
                f"✅ <b>{s['name']}</b> (<code>{s['code']}</code>, {s['market']}) 구독 등록했습니다.",
            )
        else:
            _answer_callback(callback_query_id, f"이미 {s['name']}을(를) 구독 중입니다.", alert=True)

    elif data.startswith("unsubscribe_"):
        code = data[len("unsubscribe_"):]
        s = stock_search.get_by_code(code)
        name = s["name"] if s else code
        ok = stock_subscription.unsubscribe(chat_id, code)
        if ok:
            _answer_callback(callback_query_id, f"🔕 {name} 구독 해제 완료!")
            _edit_message(
                chat_id, message_id,
                f"🔕 <b>{name}</b> (<code>{code}</code>) 구독 해제했습니다.",
            )
        else:
            _answer_callback(callback_query_id, f"{name}은(는) 구독 중이 아닙니다.", alert=True)

    elif data.startswith("price_"):
        code = data[len("price_"):]
        s = stock_search.get_by_code(code)
        if not s:
            _answer_callback(callback_query_id, "❌ 종목 정보를 찾을 수 없습니다.", alert=True)
            return
        _answer_callback(callback_query_id, "📡 조회 중...")
        try:
            msg = _fetch_price_msg(s["code"], s["market"], s["name"])
            send_message(chat_id, msg if msg else f"❌ {s['name']} 현재가를 가져올 수 없습니다. (장 마감 또는 데이터 없음)")
        except Exception as e:
            log.error("현재가 조회 실패: %s", e, exc_info=True)
            send_message(chat_id, "❌ 현재가 조회 중 오류가 발생했습니다.")

    elif data.startswith("report_"):
        code = data[len("report_"):]
        s = stock_search.get_by_code(code)
        if not s:
            _answer_callback(callback_query_id, "❌ 종목 정보를 찾을 수 없습니다.", alert=True)
            return
        _answer_callback(callback_query_id, "📡 조회 중...")
        send_message(chat_id, "🔍 뉴스/토론방 조회 중...")
        try:
            from stock_news import fetch_news_for_report
            cfg = stock_subscription.get_settings(chat_id, s["code"])
            msg = fetch_news_for_report(
                s["code"], s["name"], s["market"],
                news_count=cfg["news_count"],
                board_count=cfg["board_count"],
                days=cfg["days"],
            )
            if msg:
                send_message(chat_id, msg)
            else:
                send_message(chat_id, f"<b>{s['name']}</b> [{s['code']}]\n새로운 소식이 없습니다.")
        except Exception as e:
            log.error("즉시 조회 실패: %s", e, exc_info=True)
            send_message(chat_id, "❌ 뉴스 조회 중 오류가 발생했습니다.")

    elif data == "stocklist":
        subs = stock_subscription.get_subscriptions(chat_id)
        if not subs:
            _answer_callback(callback_query_id, "구독 중인 종목이 없습니다.", alert=True)
        else:
            market_labels = {"KOSPI": "코스피", "KOSDAQ": "코스닥", "ETF": "ETF"}
            _DEFAULT = {"news_count": 5, "board_count": 3, "days": 7}
            lines = [f"📋 <b>내 구독 종목</b> (총 {len(subs)}개)"]
            for code, info in subs.items():
                ml = market_labels.get(info["market"], info["market"])
                nc = info.get("news_count", _DEFAULT["news_count"])
                bc = info.get("board_count", _DEFAULT["board_count"])
                dy = info.get("days", _DEFAULT["days"])
                cfg = f"뉴스 {nc}건 · 토론방 {bc}건 · {dy}일"
                if nc == _DEFAULT["news_count"] and bc == _DEFAULT["board_count"] and dy == _DEFAULT["days"]:
                    cfg += " (기본값)"
                lines.append(
                    f"\n• <b>{info['name']}</b> [<code>{code}</code>] {ml}\n"
                    f"  {cfg}\n"
                    f"  /종목 설정 {code}  |  /종목 해제 {code}"
                )
            _answer_callback(callback_query_id)
            send_message(chat_id, "\n".join(lines))

    elif data.startswith("news_count_"):
        rest = data[len("news_count_"):]
        code, n = rest.rsplit("_", 1)
        ok = stock_subscription.update_settings(chat_id, code, news_count=int(n))
        if ok:
            _answer_callback(callback_query_id, f"✅ 뉴스 {n}건으로 설정했습니다.")
        else:
            _answer_callback(callback_query_id, "❌ 설정 실패 (구독 중인 종목만 설정 가능)", alert=True)

    elif data.startswith("board_count_"):
        rest = data[len("board_count_"):]
        code, n = rest.rsplit("_", 1)
        ok = stock_subscription.update_settings(chat_id, code, board_count=int(n))
        if ok:
            _answer_callback(callback_query_id, f"✅ 토론방 {n}건으로 설정했습니다.")
        else:
            _answer_callback(callback_query_id, "❌ 설정 실패 (구독 중인 종목만 설정 가능)", alert=True)

    elif data.startswith("days_"):
        rest = data[len("days_"):]
        code, n = rest.rsplit("_", 1)
        ok = stock_subscription.update_settings(chat_id, code, days=int(n))
        if ok:
            _answer_callback(callback_query_id, f"✅ 조회 기간 {n}일로 설정했습니다.")
        else:
            _answer_callback(callback_query_id, "❌ 설정 실패 (구독 중인 종목만 설정 가능)", alert=True)

    elif data == "noop":
        _answer_callback(callback_query_id)

    elif data == "cancel":
        _answer_callback(callback_query_id)
        _delete_message(chat_id, message_id)


# ── 명령어 디스패치 ───────────────────────────────────────

def _parse_cmd(text: str) -> str:
    """첫 토큰을 소문자+NFC 정규화해서 반환. /cmd@botname 형태도 처리."""
    raw = text.strip().split(maxsplit=1)[0]
    raw = raw.split("@")[0]
    return unicodedata.normalize("NFC", raw).lower()


def dispatch(chat_id: int, text: str, username: str = ""):
    parts = text.strip().split(maxsplit=1)
    cmd = _parse_cmd(text)
    args = parts[1] if len(parts) > 1 else ""

    N = lambda s: unicodedata.normalize("NFC", s)
    public_handlers = {
        N("/start"):    lambda: handle_start(chat_id, username),
        N("/stop"):     lambda: handle_stop(chat_id, username),
        N("/날씨"):     lambda: handle_weather_query(chat_id),
        N("/weather"):  lambda: handle_weather_query(chat_id),
        N("/location"): lambda: handle_location(chat_id, args),
        N("/코스피"):   lambda: handle_kospi_query(chat_id),
        N("/kospi"):    lambda: handle_kospi_query(chat_id),
        N("/금융"):     lambda: handle_finance_query(chat_id),
        N("/finance"):  lambda: handle_finance_query(chat_id),
        N("/help"):     lambda: send_message(chat_id, ADMIN_HELP_TEXT if is_admin(chat_id) else HELP_TEXT),
        N("/list"):     lambda: handle_list(chat_id),
        N("/종목"):     lambda: handle_stock_cmd(chat_id, args),
        N("/브리핑"):   lambda: handle_brief_cmd(chat_id, args),
    }
    admin_handlers = {
        N("/add"):         lambda: handle_add(chat_id, args),
        N("/remove"):      lambda: handle_remove(chat_id, args),
        N("/interval"):    lambda: handle_interval(chat_id, args),
        N("/run"):         lambda: handle_run(chat_id),
        N("/subscribers"): lambda: handle_subscribers(chat_id),
        N("/admins"):      lambda: handle_admins(chat_id),
        N("/promote"):     lambda: handle_promote(chat_id, args),
        N("/demote"):      lambda: handle_demote(chat_id, args),
    }

    if cmd in public_handlers:
        public_handlers[cmd]()
    elif cmd in admin_handlers:
        admin_handlers[cmd]()
    else:
        send_message(chat_id, f"알 수 없는 명령어입니다.\n\n{ADMIN_HELP_TEXT if is_admin(chat_id) else '/start 로 구독할 수 있습니다.'}")


# ── 내장 스케줄러 ────────────────────────────────────────

def _kst_to_local(kst_hour: int, kst_minute: int = 0) -> str:
    """KST 시각을 로컬 머신 시각으로 변환해 'HH:MM' 반환."""
    local_utc_offset_hours = -time.timezone / 3600  # DST 미적용 UTC 오프셋
    h = int((kst_hour - 9 + local_utc_offset_hours) % 24)
    return f"{h:02d}:{kst_minute:02d}"


def _morning_report():
    log.info("아침 리포트 실행 시작")
    try:
        from weather import send_weather_report
        send_weather_report()
    except Exception as e:
        log.error("날씨 리포트 오류: %s", e, exc_info=True)
    try:
        from market import send_market_report
        send_market_report()
    except Exception as e:
        log.error("금융 리포트 오류: %s", e, exc_info=True)
    log.info("아침 리포트 실행 완료")


def _stock_news_report():
    log.info("종목 뉴스 수집 시작")
    try:
        from stock_news import collect_and_send
        collect_and_send(send_message)
    except Exception as e:
        log.error("종목 뉴스 수집 오류: %s", e, exc_info=True)
    log.info("종목 뉴스 수집 완료")


def _brief_report(brief_type: str):
    log.info("시장 브리핑 실행: %s", brief_type)
    try:
        from market import send_market_brief_report
        send_market_brief_report(brief_type)
    except Exception as e:
        log.error("시장 브리핑 오류 (%s): %s", brief_type, e, exc_info=True)
    log.info("시장 브리핑 완료: %s", brief_type)


def _run_scheduler():
    fire_at_morning = _kst_to_local(7, 0)
    fire_at_brief_morning = _kst_to_local(8, 30)
    fire_at_brief_midday = _kst_to_local(12, 0)
    fire_at_brief_closing = _kst_to_local(15, 30)
    log.info(
        "스케줄러 시작: 날씨/금융=%s, 브리핑=%s/%s/%s (로컬)",
        fire_at_morning, fire_at_brief_morning, fire_at_brief_midday, fire_at_brief_closing,
    )
    schedule.every().day.at(fire_at_morning).do(_morning_report)
    schedule.every().day.at(fire_at_brief_morning).do(lambda: _brief_report("morning"))
    schedule.every().day.at(fire_at_brief_midday).do(lambda: _brief_report("midday"))
    schedule.every().day.at(fire_at_brief_closing).do(lambda: _brief_report("closing"))
    schedule.every(30).minutes.do(_stock_news_report)
    while True:
        schedule.run_pending()
        time.sleep(30)


# ── Long-polling 루프 ─────────────────────────────────────

def run_polling():
    log.info("텔레그램 명령어 봇 시작 (관리자 ID: %s)", TELEGRAM_CHAT_ID)
    stock_search.load_stocks()
    threading.Thread(target=_run_scheduler, daemon=True, name="scheduler").start()
    offset = None
    while True:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
            params: dict = {"timeout": 30, "allowed_updates": ["message", "inline_query", "callback_query"]}
            if offset is not None:
                params["offset"] = offset

            resp = requests.get(url, params=params, timeout=40)
            resp.raise_for_status()
            result = resp.json().get("result", [])

            for update in result:
                offset = update["update_id"] + 1

                # inline_query 처리
                if "inline_query" in update:
                    iq = update["inline_query"]
                    try:
                        handle_inline_query(iq["id"], iq.get("query", ""))
                    except Exception as e:
                        log.error("InlineQuery 처리 오류: %s", e, exc_info=True)
                    continue

                # callback_query 처리
                if "callback_query" in update:
                    cq = update["callback_query"]
                    cq_chat_id = cq["from"]["id"]
                    cq_msg = cq.get("message", {})
                    try:
                        handle_callback_query(
                            cq["id"],
                            cq_chat_id,
                            cq_msg.get("message_id"),
                            cq.get("data", ""),
                        )
                    except Exception as e:
                        log.error("CallbackQuery 처리 오류: %s", e, exc_info=True)
                    continue

                msg = update.get("message", {})
                chat_id = msg.get("chat", {}).get("id")
                text = msg.get("text", "")
                username = msg.get("from", {}).get("username", "")

                if not text.startswith("/"):
                    continue

                cmd = _parse_cmd(text)
                admin = is_admin(chat_id)
                is_public_cmd = cmd in PUBLIC_COMMANDS
                log.debug("cmd=%r is_public=%s is_admin=%s", cmd, is_public_cmd, admin)

                if not is_public_cmd and not admin:
                    log.warning("관리자 전용 명령어 시도: %s (chat_id=%s)", cmd, chat_id)
                    send_message(chat_id, "❌ 관리자 전용 명령어입니다.\n/start 로 구독할 수 있습니다.")
                    continue

                log.info("명령어 수신: %s (from %s, admin=%s)", text, chat_id, admin)
                try:
                    dispatch(chat_id, text, username)
                except Exception as e:
                    log.error("명령어 처리 오류: %s", e, exc_info=True)
                    send_message(chat_id, f"❌ 오류가 발생했습니다: {e}")

        except requests.RequestException as e:
            log.error("네트워크 오류: %s", e)
            time.sleep(10)
        except Exception as e:
            log.error("폴링 오류: %s", e, exc_info=True)
            time.sleep(5)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler("commands.log", encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )
    run_polling()
