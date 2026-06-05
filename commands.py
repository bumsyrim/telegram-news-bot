"""
텔레그램 명령어 봇 - long-polling 방식
/list /add /remove /interval /run
"""
import json
import logging
import re
import subprocess
import sys
import time
from pathlib import Path

import requests

from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

SOURCES_FILE = Path("sources.json")
WORKFLOW_FILE = Path(".github/workflows/run_bot.yml")

log = logging.getLogger(__name__)

HELP_TEXT = (
    "사용 가능한 명령어:\n"
    "/list - 등록된 사이트 목록\n"
    "/add URL 이름 - 사이트 추가\n"
    "/remove 이름 - 사이트 삭제\n"
    "/interval 숫자 - 실행 주기 변경 (분)\n"
    "/run - 즉시 뉴스 체크 실행"
)


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

def handle_list(chat_id: int):
    data = load_sources()
    if not data["sources"]:
        send_message(chat_id, "등록된 사이트가 없습니다.\n/add URL 이름 으로 추가하세요.")
        return
    lines = [f"📋 <b>등록된 사이트 목록</b> (주기: {data['interval_minutes']}분)\n"]
    for i, s in enumerate(data["sources"], 1):
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

def dispatch(chat_id: int, text: str):
    parts = text.strip().split(maxsplit=1)
    cmd = parts[0].lower().split("@")[0]  # /cmd@botname 형태 처리
    args = parts[1] if len(parts) > 1 else ""

    handlers = {
        "/list": lambda: handle_list(chat_id),
        "/add": lambda: handle_add(chat_id, args),
        "/remove": lambda: handle_remove(chat_id, args),
        "/interval": lambda: handle_interval(chat_id, args),
        "/run": lambda: handle_run(chat_id),
        "/help": lambda: send_message(chat_id, HELP_TEXT),
        "/start": lambda: send_message(chat_id, f"안녕하세요! 뉴스 봇입니다.\n\n{HELP_TEXT}"),
    }

    handler = handlers.get(cmd)
    if handler:
        handler()
    else:
        send_message(chat_id, f"알 수 없는 명령어입니다.\n\n{HELP_TEXT}")


# ── Long-polling 루프 ─────────────────────────────────────

def run_polling():
    log.info("텔레그램 명령어 봇 시작 (허용 채팅 ID: %s)", TELEGRAM_CHAT_ID)
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

                if not text.startswith("/"):
                    continue
                if str(chat_id) != str(TELEGRAM_CHAT_ID):
                    log.warning("허용되지 않은 채팅 ID: %s", chat_id)
                    continue

                log.info("명령어 수신: %s", text)
                try:
                    dispatch(chat_id, text)
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
