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
import time
import unicodedata
from pathlib import Path

import requests

from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

SOURCES_FILE = Path("sources.json")
WORKFLOW_FILE = Path(".github/workflows/run_bot.yml")
USERS_FILE = Path("users.json")

# 누구나 사용 가능한 명령어 (NFC 정규화된 소문자로 저장)
PUBLIC_COMMANDS = {
    unicodedata.normalize("NFC", c)
    for c in {"/start", "/stop", "/날씨", "/weather", "/location"}
}

log = logging.getLogger(__name__)

HELP_TEXT = (
    "사용 가능한 명령어:\n"
    "/start - 뉴스 구독 등록\n"
    "/stop - 뉴스 구독 취소\n"
    "/날씨 (또는 /weather) - 내 위치 기준 날씨/미세먼지 조회\n"
    "/location 위치명 - 내 날씨 위치 변경\n"
    "/list - 등록된 사이트 목록\n"
    "/add URL 이름 - 사이트 추가\n"
    "/remove 이름 - 사이트 삭제\n"
    "/interval 숫자 - 실행 주기 변경 (분)\n"
    "/run - 즉시 뉴스 체크 실행"
)

ADMIN_HELP_TEXT = (
    "관리자 명령어:\n"
    "/list - 등록된 사이트 목록\n"
    "/add URL 이름 - 사이트 추가\n"
    "/remove 이름 - 사이트 삭제\n"
    "/interval 숫자 - 실행 주기 변경 (분)\n"
    "/run - 즉시 뉴스 체크 실행"
)


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


def handle_list(chat_id: int):
    src = load_sources()
    usr = load_users()
    if not src["sources"]:
        send_message(chat_id, "등록된 사이트가 없습니다.\n/add URL 이름 으로 추가하세요.")
        return
    lines = [f"📋 <b>등록된 사이트 목록</b> (주기: {src['interval_minutes']}분 / 구독자: {len(usr['subscribers'])}명)\n"]
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
        N("/help"):     lambda: send_message(chat_id, HELP_TEXT),
    }
    admin_handlers = {
        N("/list"):     lambda: handle_list(chat_id),
        N("/add"):      lambda: handle_add(chat_id, args),
        N("/remove"):   lambda: handle_remove(chat_id, args),
        N("/interval"): lambda: handle_interval(chat_id, args),
        N("/run"):      lambda: handle_run(chat_id),
    }

    if cmd in public_handlers:
        public_handlers[cmd]()
    elif cmd in admin_handlers:
        admin_handlers[cmd]()
    else:
        is_admin = str(chat_id) == str(TELEGRAM_CHAT_ID)
        send_message(chat_id, f"알 수 없는 명령어입니다.\n\n{HELP_TEXT if is_admin else '/start 로 구독할 수 있습니다.'}")


# ── Long-polling 루프 ─────────────────────────────────────

def run_polling():
    log.info("텔레그램 명령어 봇 시작 (관리자 ID: %s)", TELEGRAM_CHAT_ID)
    offset = None
    while True:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
            params: dict = {"timeout": 30, "allowed_updates": ["message"]}
            if offset is not None:
                params["offset"] = offset

            resp = requests.get(url, params=params, timeout=40)
            resp.raise_for_status()
            result = resp.json().get("result", [])

            for update in result:
                offset = update["update_id"] + 1
                msg = update.get("message", {})
                chat_id = msg.get("chat", {}).get("id")
                text = msg.get("text", "")
                username = msg.get("from", {}).get("username", "")

                if not text.startswith("/"):
                    continue

                cmd = _parse_cmd(text)
                is_admin = str(chat_id) == str(TELEGRAM_CHAT_ID)
                is_public_cmd = cmd in PUBLIC_COMMANDS
                log.debug("cmd=%r is_public=%s is_admin=%s", cmd, is_public_cmd, is_admin)

                if not is_public_cmd and not is_admin:
                    log.warning("관리자 전용 명령어 시도: %s (chat_id=%s)", cmd, chat_id)
                    send_message(chat_id, "❌ 관리자 전용 명령어입니다.\n/start 로 구독할 수 있습니다.")
                    continue

                log.info("명령어 수신: %s (from %s, admin=%s)", text, chat_id, is_admin)
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
