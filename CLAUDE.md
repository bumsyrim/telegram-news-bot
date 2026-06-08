# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in real values
python get_chat_id.py  # find your Telegram chat ID after messaging the bot
```

Required `.env` variables: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `ANTHROPIC_API_KEY`. Optional: `CHECK_INTERVAL_MINUTES` (default 60), `WEATHER_API_KEY` (공공데이터포털).

## Running

Two processes run independently — start both to get full functionality:

```bash
python bot.py          # scheduled news crawler (loops every interval_minutes)
python bot.py --once   # single run (used by GitHub Actions)
python commands.py     # interactive command bot (long-polling)
python weather.py --send   # manual weather broadcast
python market.py --send    # manual market report broadcast
```

## Architecture

### Two processes

**`bot.py`** — Scheduled crawler. Reads `sources.json` for which sites to crawl and the check interval. For each registered source, calls `source.fetch()`, deduplicates against `seen.json` (MD5 hash of URL), and broadcasts new articles to all subscribers in `users.json`.

**`commands.py`** — Interactive command bot. Long-polling loop. Handles `/start`, `/stop`, `/날씨`, `/코스피`, `/금융`, `/location`, and admin-only commands (`/add`, `/remove`, `/list`, `/interval`, `/run`). `/run` invokes `bot.py --once` as a subprocess.

### Sources plugin system (`sources/`)

`BaseSource` (abstract) in `sources/__init__.py` defines the contract: `fetch() -> List[Dict]` where each dict has `{"id", "title", "content", "url"}`. New sources subclass `BaseSource` and are registered in `sources.json` with a `type` field. Currently implemented:

- `BrunchSource` — Selenium headless Chrome, waits for JS render, extracts links matching `brunch.co.kr/@.../[int]`
- `GptersSource` — Selenium headless Chrome, uses `execute_script` to avoid `StaleElementReferenceException` from SPA re-renders

Each source spins up a fresh Chrome instance per `fetch()` call and quits it in a `finally` block.

### Data files

| File | Purpose |
|------|---------|
| `sources.json` | Registered source list + `interval_minutes` |
| `seen.json` | Set of already-sent article IDs (MD5 hash of URL, 12 chars) |
| `users.json` | Subscribers dict: `{"subscribers": {"chat_id": {"location": "서울 강남구"}}}` |

### External data modules

**`weather.py`**: KMA short-term forecast API (`getVilageFcst`) + AirKorea API. `LOCATION_MAP` maps Korean city/district names to KMA grid coordinates (nx, ny) and AirKorea station names. Per-user location is stored in `users.json`; falls back to `sources.json` global location, then Seoul. Same-location subscribers are grouped to minimize API calls.

**`market.py`**: yfinance for NASDAQ/S&P500/Dow/USD-KRW. Investing.com for KOSPI200 night futures — uses `curl_cffi` with `impersonate="chrome124"` to spoof Chrome TLS fingerprint and bypass Cloudflare (Selenium headless Chrome crashes on the Cloudflare JS challenge for this site).

### Korean command dispatch

Command dict keys in `commands.py` are explicitly `unicodedata.normalize("NFC", ...)`. Incoming command text is also NFC-normalized in `_parse_cmd()` before lookup. This is required because source file encoding and runtime encoding can produce visually identical but byte-different Korean strings that silently fail matching.

### GitHub Actions

`.github/workflows/run_bot.yml` runs `bot.py --once` on a cron schedule. `seen.json` is persisted between runs via `actions/cache`. `ANTHROPIC_API_KEY` is set to `"dummy"` in the workflow — the `summarize()` function in `bot.py` exists but is not called by the current production flow (articles are sent with a raw content preview via `format_message`).

The `/interval` command in `commands.py` updates both `sources.json` and the cron expression in the workflow file directly; changes must be pushed to take effect in Actions.

## 작업 규칙

모든 코드 수정 완료 후 아래 형식으로 반드시 물어볼 것:

```
작업이 완료됐습니다. GitHub에 push하시겠습니까?

git add .
git commit -m "작업내용 요약"
git push

1. Yes (push 실행)
2. No (나중에 직접)
```

- 사용자가 1번 선택 시 push 수행
- 사용자가 2번 선택 시 push 없이 종료
