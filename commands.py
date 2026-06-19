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
    for c in {"/start", "/stop", "/날씨", "/weather", "/location", "/코스피", "/kospi", "/금융", "/finance", "/help", "/list", "/종목"}
}

log = logging.getLogger(__name__)

_COMMON_HELP = (
    "[📋 공통 명령어]\n"
    "/start - 뉴스 구독 등록\n"
    "/stop - 뉴스 구독 취소\n"
    "/list - 등록된 사이트 목록\n"
    "/날씨 - 내 위치 기준 날씨 조회\n"
    "/location 위치명 - 날씨 위치 변경\n"
    "/코스피 - 코스피 지수 조회\n"
    "/금융 - 미국 시장 지표 조회\n"
    "/종목 - 종목 검색/구독\n"
    "/help - 도움말"
)

HELP_TEXT = (
    "[📋 사용 가능한 명령어]\n"
    "/start - 뉴스 구독 등록\n"
    "/stop - 뉴스 구독 취소\n"
    "/list - 등록된 사이트 목록\n"
    "/날씨 - 내 위치 기준 날씨 조회\n"
    "/location 위치명 - 날씨 위치 변경\n"
    "/코스피 - 코스피 지수 조회\n"
    "/금융 - 미국 시장 지표 조회\n"
    "/종목 - 종목 검색/구독\n"
    "/help - 도움말"
)

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
) + _COMMON_HELP


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


def handle_stock_cmd(chat_id: int, args: str):
    N = lambda s: unicodedata.normalize("NFC", s)
    parts = args.strip().split(maxsplit=1)
    subcmd = N(parts[0]) if parts else ""
    rest = parts[1].strip() if len(parts) > 1 else ""

    if not subcmd:
        send_message(
            chat_id,
            "📈 <b>종목 명령어 사용법</b>\n\n"
            "/종목 &lt;검색어&gt;      - 종목명/코드 검색\n"
            "/종목 등록 &lt;코드&gt;   - 종목 구독 등록\n"
            "/종목 해제 &lt;코드&gt;   - 종목 구독 해제\n"
            "/종목 목록          - 내 구독 종목 전체\n"
            "/종목 조회 &lt;코드&gt;   - 즉시 시세 조회\n\n"
            "예시:\n"
            "/종목 삼성전자\n"
            "/종목 005930\n"
            "/종목 등록 005930",
        )
        return

    if subcmd == N("등록"):
        if not rest:
            send_message(chat_id, "사용법: /종목 등록 &lt;종목코드&gt;\n예: /종목 등록 005930")
            return
        code = rest.strip().zfill(6)
        s = stock_search.get_by_code(code)
        if not s:
            send_message(chat_id, f"❌ 종목코드 <code>{rest.strip()}</code>를 찾을 수 없습니다.\n/종목 &lt;검색어&gt;로 먼저 검색해보세요.")
            return
        ok = stock_subscription.subscribe(chat_id, s["code"], s["name"], s["market"])
        if ok:
            send_message(chat_id, f"✅ <b>{s['name']}</b> (<code>{s['code']}</code>, {s['market']}) 구독 등록했습니다.")
        else:
            send_message(chat_id, f"이미 <b>{s['name']}</b> (<code>{s['code']}</code>)를 구독 중입니다.")
        return

    if subcmd == N("해제"):
        if not rest:
            send_message(chat_id, "사용법: /종목 해제 &lt;종목코드&gt;\n예: /종목 해제 005930")
            return
        code = rest.strip().zfill(6)
        s = stock_search.get_by_code(code)
        name = s["name"] if s else rest.strip()
        ok = stock_subscription.unsubscribe(chat_id, code)
        if ok:
            send_message(chat_id, f"🗑️ <b>{name}</b> (<code>{code}</code>) 구독 해제했습니다.")
        else:
            send_message(chat_id, f"❌ <code>{code}</code>는 구독 중이 아닙니다.\n/종목 목록으로 구독 종목을 확인하세요.")
        return

    if subcmd == N("목록"):
        subs = stock_subscription.get_subscriptions(chat_id)
        if not subs:
            send_message(chat_id, "구독 중인 종목이 없습니다.\n/종목 등록 &lt;코드&gt;로 등록하세요.")
            return
        lines = [f"📋 <b>내 구독 종목</b> ({len(subs)}개)\n"]
        for code, info in subs.items():
            lines.append(f"• <b>{info['name']}</b> (<code>{code}</code>, {info['market']})")
        send_message(chat_id, "\n".join(lines))
        return

    if subcmd == N("조회"):
        if not rest:
            send_message(chat_id, "사용법: /종목 조회 &lt;종목코드&gt;\n예: /종목 조회 005930")
            return
        code = rest.strip().zfill(6)
        s = stock_search.get_by_code(code)
        name = s["name"] if s else rest.strip()
        send_message(chat_id, f"⏳ <b>{name}</b> (<code>{code}</code>) 즉시 조회는 준비 중입니다.")
        return

    # 그 외: 검색어로 처리
    query = args.strip()
    results = stock_search.search_stocks(query)
    if not results:
        send_message(chat_id, f"'{query}'에 대한 검색 결과가 없습니다.\n종목명 또는 6자리 코드를 입력해보세요.")
        return
    lines = [f"🔍 <b>'{query}' 검색 결과</b>\n"]
    for r in results:
        lines.append(f"• <b>{r['name']}</b> (<code>{r['code']}</code>, {r['market']})")
    lines.append("\n/종목 등록 &lt;코드&gt;로 구독 등록하세요.")
    send_message(chat_id, "\n".join(lines))


# ── InlineQuery 핸들러 ───────────────────────────────────

def handle_inline_query(inline_query_id: str, query: str):
    """@봇이름 <검색어> 입력 시 실시간 종목 드롭다운 반환."""
    q = query.strip()
    results = []
    if q:
        stocks = stock_search.search_stocks(q, limit=10)
        for s in stocks:
            market_label = {"KOSPI": "코스피", "KOSDAQ": "코스닥", "ETF": "ETF"}.get(s["market"], s["market"])
            results.append({
                "type": "article",
                "id": s["code"],
                "title": f"{s['name']} ({s['code']})",
                "description": f"{market_label} · 코드: {s['code']}",
                "input_message_content": {
                    "message_text": f"/종목 등록 {s['code']}"
                },
            })
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerInlineQuery"
    requests.post(
        url,
        json={"inline_query_id": inline_query_id, "results": results, "cache_time": 30},
        timeout=10,
    )
    log.debug("InlineQuery 응답: query=%r results=%d개", q, len(results))


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


def _run_scheduler():
    fire_at = _kst_to_local(7, 0)
    log.info("스케줄러 시작: 매일 %s (로컬) = 07:00 KST", fire_at)
    schedule.every().day.at(fire_at).do(_morning_report)
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
            params: dict = {"timeout": 30, "allowed_updates": ["message", "inline_query"]}
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
