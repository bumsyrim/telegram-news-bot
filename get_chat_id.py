"""
Chat ID 확인 스크립트
실행 방법: python get_chat_id.py
"""

import requests
import os
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TOKEN or "여기에" in TOKEN:
    print("❌ .env 파일에 TELEGRAM_BOT_TOKEN을 먼저 입력하세요!")
    exit(1)

print("🔍 Chat ID를 확인합니다...")
print("   텔레그램에서 봇에게 '/start' 또는 아무 메시지나 보내세요.\n")

resp = requests.get(f"https://api.telegram.org/bot{TOKEN}/getUpdates", timeout=10)
data = resp.json()

if not data.get("ok"):
    print(f"❌ 오류: {data}")
    exit(1)

updates = data.get("result", [])
if not updates:
    print("❌ 메시지가 없습니다. 먼저 봇에게 메시지를 보내주세요!")
    exit(1)

print("✅ 발견된 채팅:")
seen = set()
for update in updates:
    msg = update.get("message") or update.get("channel_post")
    if not msg:
        continue
    chat = msg["chat"]
    cid = chat["id"]
    if cid in seen:
        continue
    seen.add(cid)
    chat_type = chat.get("type", "")
    name = chat.get("title") or f"{chat.get('first_name','')} {chat.get('last_name','')}".strip()
    print(f"   Chat ID: {cid}  |  이름: {name}  |  타입: {chat_type}")

print("\n📋 .env 파일의 TELEGRAM_CHAT_ID에 위의 Chat ID를 입력하세요.")
print("   예) TELEGRAM_CHAT_ID=123456789")
