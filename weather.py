"""
날씨/미세먼지 조회 → 텔레그램 발송
기상청 단기예보 API + 에어코리아 API
"""
import argparse
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

log = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))
USERS_FILE = Path("users.json")
SOURCES_FILE = Path("sources.json")

GRADE_EMOJI = {
    "1": "😊 좋음",
    "2": "🙂 보통",
    "3": "😷 나쁨",
    "4": "🤢 매우나쁨",
}

# ── 위치 매핑 (기상청 격자 좌표 + 에어코리아 측정소) ───────────────

_SEOUL_GU = [
    ("종로구",  60, 127, "종로구"),
    ("중구",    60, 127, "중구"),
    ("용산구",  60, 126, "용산구"),
    ("성동구",  61, 127, "성동구"),
    ("광진구",  62, 127, "광진구"),
    ("동대문구",61, 127, "동대문구"),
    ("중랑구",  62, 128, "중랑구"),
    ("성북구",  61, 128, "성북구"),
    ("강북구",  61, 129, "강북구"),
    ("도봉구",  61, 130, "도봉구"),
    ("노원구",  61, 129, "노원구"),
    ("은평구",  59, 127, "은평구"),
    ("서대문구",59, 127, "서대문구"),
    ("마포구",  59, 127, "마포구"),
    ("양천구",  58, 126, "양천구"),
    ("강서구",  58, 126, "강서구"),
    ("구로구",  58, 125, "구로구"),
    ("금천구",  59, 124, "금천구"),
    ("영등포구",58, 126, "영등포구"),
    ("동작구",  59, 125, "동작구"),
    ("관악구",  59, 125, "관악구"),
    ("서초구",  61, 125, "서초구"),
    ("강남구",  61, 125, "강남구"),
    ("송파구",  62, 125, "송파구"),
    ("강동구",  62, 126, "강동구"),
]

LOCATION_MAP: dict = {
    "서울": {"nx": 60, "ny": 127, "station": "종로구", "display": "서울"},
    **{
        gu: {"nx": nx, "ny": ny, "station": st, "display": f"서울 {gu}"}
        for gu, nx, ny, st in _SEOUL_GU
    },
    **{
        f"서울 {gu}": {"nx": nx, "ny": ny, "station": st, "display": f"서울 {gu}"}
        for gu, nx, ny, st in _SEOUL_GU
    },
    "부산":  {"nx": 98,  "ny": 76,  "station": "연제구",        "display": "부산"},
    "대구":  {"nx": 89,  "ny": 91,  "station": "수성구",        "display": "대구"},
    "인천":  {"nx": 55,  "ny": 124, "station": "미추홀구",      "display": "인천"},
    "광주":  {"nx": 58,  "ny": 74,  "station": "북구",          "display": "광주"},
    "대전":  {"nx": 67,  "ny": 100, "station": "서구",          "display": "대전"},
    "울산":  {"nx": 102, "ny": 84,  "station": "울주군",        "display": "울산"},
    "세종":  {"nx": 66,  "ny": 103, "station": "세종",          "display": "세종"},
    "수원":  {"nx": 60,  "ny": 121, "station": "수원시 권선구", "display": "수원"},
    "성남":  {"nx": 62,  "ny": 123, "station": "성남시 분당구", "display": "성남"},
    "제주":  {"nx": 52,  "ny": 38,  "station": "제주시",        "display": "제주"},
}


def get_location_config() -> dict:
    """sources.json에서 위치 설정 읽기"""
    if SOURCES_FILE.exists():
        data = json.loads(SOURCES_FILE.read_text(encoding="utf-8"))
        key = data.get("location", "서울")
        if key in LOCATION_MAP:
            return LOCATION_MAP[key]
        log.warning("알 수 없는 위치 '%s', 서울 기본값 사용", key)
    return LOCATION_MAP["서울"]


# ── PM 등급 계산 ───────────────────────────────────────────────────

def _pm_grade(value: int, kind: str) -> str:
    if kind == "pm10":
        if value <= 30:  return "1"
        if value <= 80:  return "2"
        if value <= 150: return "3"
        return "4"
    else:  # pm25
        if value <= 15: return "1"
        if value <= 35: return "2"
        if value <= 75: return "3"
        return "4"


# ── API 호출 ───────────────────────────────────────────────────────

def fetch_weather(nx: int, ny: int, api_key: str) -> dict:
    """기상청 단기예보 API.

    TMN(최저기온)·TMX(최고기온)는 0200 발표 시각에서만 제공됨.
    - 오전 7시 이전: 전날 0200 발표 데이터 (전날 최저·최고기온)
    - 오전 7시 이후: 당일 0200 발표 데이터 (당일 최저·최고기온)
    """
    now = datetime.now(KST)

    # 7시 이전이면 전날 기준
    if now.hour < 7:
        target = now - timedelta(days=1)
    else:
        target = now

    target_date = target.strftime("%Y%m%d")

    url = "https://apis.data.go.kr/1360000/VilageFcstInfoService_2.0/getVilageFcst"
    params = {
        "serviceKey": api_key,
        "pageNo": 1,
        "numOfRows": 1000,
        "dataType": "JSON",
        "base_date": target_date,
        "base_time": "0200",  # TMN·TMX가 포함되는 유일한 발표 시각
        "nx": nx,
        "ny": ny,
    }
    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    items = resp.json()["response"]["body"]["items"]["item"]

    result: dict = {"pop": 0}

    for item in items:
        if item["fcstDate"] != target_date:
            continue
        cat, val, fcst_time = item["category"], item["fcstValue"], item["fcstTime"]

        if cat == "TMX":
            result["max_temp"] = val
        elif cat == "TMN":
            result["min_temp"] = val
        elif cat == "POP":
            result["pop"] = max(result["pop"], int(val))
        elif cat == "SKY" and fcst_time == "1200":
            result["sky"] = val
        elif cat == "PTY" and fcst_time == "1200":
            result["pty"] = val

    log.debug("날씨 파싱 결과 (base=%s 0200): %s", target_date, result)
    return result


def fetch_air_quality(station: str, api_key: str) -> dict:
    """에어코리아 실시간 대기질 API"""
    url = "https://apis.data.go.kr/B552584/ArpltnInforInqireSvc/getMsrstnAcctoRltmMesureDnsty"
    params = {
        "serviceKey": api_key,
        "returnType": "json",
        "numOfRows": 1,
        "pageNo": 1,
        "stationName": station,
        "dataTerm": "DAILY",
        "ver": "1.0",
    }
    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    items = resp.json()["response"]["body"]["items"]

    if not items:
        return {}

    item = items[0]

    def safe_int(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return 0

    pm10 = safe_int(item.get("pm10Value"))
    pm25 = safe_int(item.get("pm25Value"))
    return {
        "pm10": pm10,
        "pm25": pm25,
        "pm10_grade": str(item.get("pm10Grade") or _pm_grade(pm10, "pm10")),
        "pm25_grade": str(item.get("pm25Grade") or _pm_grade(pm25, "pm25")),
    }


# ── 메시지 포매팅 ──────────────────────────────────────────────────

def format_weather_message(display: str, weather: dict, air: dict) -> str:
    sky_map = {"1": "맑음 ☀️", "3": "구름많음 ⛅", "4": "흐림 ☁️"}
    pty_map = {"0": "", "1": "비 🌧️", "2": "비/눈 🌨️", "3": "눈 ❄️", "4": "소나기 ⛈️"}

    sky = sky_map.get(str(weather.get("sky", "1")), "맑음 ☀️")
    pty = pty_map.get(str(weather.get("pty", "0")), "")
    condition = pty if pty else sky
    date_str = datetime.now(KST).strftime("%Y년 %m월 %d일")

    lines = [
        "<b>[날씨]</b>",
        f"{display} · {date_str}",
        f"- 기온: {weather.get('min_temp', '-')}°C ~ {weather.get('max_temp', '-')}°C",
        f"- 날씨: {condition}",
        f"- 강수확률: {weather.get('pop', 0)}%",
    ]

    if air:
        lines += [
            f"- 미세먼지(PM10): {air.get('pm10', '-')}㎍/㎥ {GRADE_EMOJI.get(air.get('pm10_grade', '1'), '')}",
            f"- 초미세먼지(PM2.5): {air.get('pm25', '-')}㎍/㎥ {GRADE_EMOJI.get(air.get('pm25_grade', '1'), '')}",
        ]

    return "\n".join(lines)


# ── 구독자별 위치 조회 ────────────────────────────────────────────

def get_subscriber_location(chat_id: int) -> dict:
    """구독자의 개인 위치 반환. 미설정 시 sources.json 전역 설정(서울)."""
    if USERS_FILE.exists():
        data = json.loads(USERS_FILE.read_text(encoding="utf-8"))
        subs = data.get("subscribers", {})
        if isinstance(subs, dict):
            user = subs.get(str(chat_id), {})
            loc_key = user.get("location")
            if loc_key and loc_key in LOCATION_MAP:
                return LOCATION_MAP[loc_key]
    return get_location_config()


def _load_subscribers_with_location(fallback_id) -> list:
    """구독자 목록을 (chat_id, loc_config) 리스트로 반환."""
    default_loc = get_location_config()

    if USERS_FILE.exists():
        data = json.loads(USERS_FILE.read_text(encoding="utf-8"))
        subs = data.get("subscribers", {})

        # 구형 포맷(리스트) 호환
        if isinstance(subs, list):
            return [(int(uid), default_loc) for uid in subs] if subs else [(int(fallback_id), default_loc)]

        if isinstance(subs, dict) and subs:
            result = []
            for uid, user_data in subs.items():
                loc_key = user_data.get("location")
                loc = LOCATION_MAP.get(loc_key, default_loc) if loc_key else default_loc
                result.append((int(uid), loc))
            return result

    return [(int(fallback_id), default_loc)]


# ── 발송 ──────────────────────────────────────────────────────────

def send_weather_report():
    from collections import defaultdict
    from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, WEATHER_API_KEY

    if not WEATHER_API_KEY:
        log.error("WEATHER_API_KEY 가 설정되지 않았습니다.")
        return

    subscribers = _load_subscribers_with_location(TELEGRAM_CHAT_ID)
    log.info("날씨 알림 대상: %d명", len(subscribers))

    # 같은 위치 구독자끼리 묶어 API 호출 최소화
    location_groups: dict = defaultdict(list)
    for chat_id, loc in subscribers:
        group_key = (loc["nx"], loc["ny"], loc["station"])
        location_groups[group_key].append((chat_id, loc))

    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    total_sent = total_failed = 0

    for (nx, ny, station), users in location_groups.items():
        loc = users[0][1]
        log.info("날씨 조회: %s (%d명)", loc["display"], len(users))

        weather, air = {}, {}
        try:
            weather = fetch_weather(nx, ny, WEATHER_API_KEY)
        except Exception as e:
            log.error("날씨 조회 실패 (%s): %s", loc["display"], e, exc_info=True)
        try:
            air = fetch_air_quality(station, WEATHER_API_KEY)
        except Exception as e:
            log.error("대기질 조회 실패 (%s): %s", station, e, exc_info=True)

        if not weather and not air:
            log.warning("%s 날씨/대기질 모두 실패, 건너뜀", loc["display"])
            continue

        msg = format_weather_message(loc["display"], weather, air)

        for chat_id, _ in users:
            try:
                resp = requests.post(
                    api_url,
                    json={"chat_id": chat_id, "text": msg, "parse_mode": "HTML"},
                    timeout=10,
                )
                resp.raise_for_status()
                total_sent += 1
            except Exception as e:
                log.warning("발송 실패 (chat_id=%s): %s", chat_id, e)
                total_failed += 1

    log.info("날씨 알림 완료: 성공 %d명 / 실패 %d명", total_sent, total_failed)


# ── 진입점 ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    parser = argparse.ArgumentParser(description="날씨/미세먼지 텔레그램 알림")
    parser.add_argument("--send", action="store_true", help="날씨 알림 발송")
    args = parser.parse_args()

    if args.send:
        send_weather_report()
    else:
        parser.print_help()
