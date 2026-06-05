# 텔레그램 뉴스 봇

브런치 새 글을 감지해 Claude API로 요약 후 텔레그램으로 자동 발송합니다.

## 파일 구조

```
telegram_news_bot/
├── bot.py              ← 메인 실행 파일
├── config.py           ← 설정 로더
├── get_chat_id.py      ← Chat ID 확인 헬퍼
├── requirements.txt
├── .env                ← 🔑 직접 만들어야 함 (.env.example 참고)
├── seen.json           ← 자동 생성 (발송 이력)
└── sources/
    ├── __init__.py     ← BaseSource (새 소스 추가 시 상속)
    └── brunch.py       ← 브런치 크롤러
```

## 설치 및 실행

### 1단계: 패키지 설치

```bash
pip install -r requirements.txt
```

### 2단계: .env 파일 생성

`.env.example`을 복사해 `.env`로 저장 후 값을 채웁니다.

```
TELEGRAM_BOT_TOKEN=7123456789:AAF...   # BotFather에서 발급
TELEGRAM_CHAT_ID=                      # 아래 방법으로 확인
ANTHROPIC_API_KEY=sk-ant-...
CHECK_INTERVAL_MINUTES=60
```

### 3단계: 텔레그램 Chat ID 확인

봇 토큰을 .env에 입력한 뒤, 텔레그램에서 봇에게 아무 메시지를 보내고:

```bash
python get_chat_id.py
```

출력된 Chat ID를 `.env`의 `TELEGRAM_CHAT_ID`에 입력합니다.

### 4단계: 봇 실행

```bash
python bot.py
```

## 새로운 사이트 추가

`sources/` 폴더에 새 파일을 만들고 `BaseSource`를 상속합니다:

```python
from sources import BaseSource

class MySource(BaseSource):
    def fetch(self):
        # 글 목록을 스크래핑해서 반환
        return [
            {"id": "unique_id", "title": "제목", "content": "본문", "url": "https://..."}
        ]
```

그리고 `bot.py`의 `SOURCES` 리스트에 추가합니다:

```python
from sources.my_source import MySource

SOURCES = [
    BrunchSource(...),
    MySource(url="https://example.com", name="내 사이트"),
]
```

## Windows 시작 시 자동 실행 (선택)

작업 스케줄러를 사용하거나, 간단히 `start_bot.bat` 파일을 만들어 시작 프로그램에 등록합니다:

```bat
@echo off
cd /d C:\경로\telegram_news_bot
python bot.py
pause
```
