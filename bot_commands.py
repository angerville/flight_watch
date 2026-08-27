"""텔레그램 봇 대화로 flight_watch 설정(예산/날짜/목적지)을 바꾸는 커맨드 처리기.

track_prices.py(시간당 1회, Playwright 필요, 느림)와 분리된 가벼운 스크립트로
10~15분마다 돌린다 — 브라우저를 안 띄우니 명령어 반응이 훨씬 빠르다.

설정은 두 파일로 나뉜다:
  - config.yaml      : 사람이 손으로 편집하는 주석 달린 기본값 (이 스크립트는
                        건드리지 않는다 — 편집하면 주석이 다 날아가므로)
  - state/overrides.json : 이 스크립트가 명령어로 바꾼 값만 담는다.
                        track_prices.py가 시작할 때 이 둘을 합쳐서 씀.

오프셋(state/telegram_offset.txt)과 overrides.json은 이 스크립트만 쓰고,
가격 이력 DB(state/flight_prices.sqlite)는 track_prices.py만 쓴다 — 두
워크플로가 서로 다른 파일만 건드리게 나눠서 동시 실행 시 git 커밋이 충돌하지
않게 했다.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

from config_io import load_base_config, load_config, load_overrides, save_overrides  # noqa: E402
from pipeline.notify import TelegramNotifier  # noqa: E402

OFFSET_PATH = HERE / "state" / "telegram_offset.txt"

# 표시 이름(한글) -> Google Flights 검색어(영문). /destinations, /adddest 에서 씀.
# 대련에서 갈 만한 중국 국내 주요 도시 위주로 넉넉히 잡음 — 목록에 없으면
# /adddest <표시이름> <영문검색어> 로 직접 추가할 수 있다.
CITY_CATALOG: dict[str, str] = {
    "싼야": "Sanya", "하이커우": "Haikou", "쿤밍": "Kunming", "리장": "Lijiang",
    "다리": "Dali", "시솽반나": "Xishuangbanna", "구이린": "Guilin",
    "장자제": "Zhangjiajie", "구이양": "Guiyang", "청두": "Chengdu",
    "충칭": "Chongqing", "시안": "Xian", "란저우": "Lanzhou", "우루무치": "Urumqi",
    "라싸": "Lhasa", "시닝": "Xining", "칭다오": "Qingdao", "지난": "Jinan",
    "상하이": "Shanghai", "항저우": "Hangzhou", "닝보": "Ningbo", "난징": "Nanjing",
    "우시": "Wuxi", "쑤저우": "Suzhou", "허페이": "Hefei", "광저우": "Guangzhou",
    "선전": "Shenzhen", "샤먼": "Xiamen", "푸저우": "Fuzhou", "창사": "Changsha",
    "난창": "Nanchang", "우한": "Wuhan", "정저우": "Zhengzhou", "하얼빈": "Harbin",
    "창춘": "Changchun", "선양": "Shenyang", "베이징": "Beijing", "톈진": "Tianjin",
    "스자좡": "Shijiazhuang",
    # 일본 / 대만 / 홍콩·마카오 / 동남아 (한국은 출발지 기준 여행지로 의미 없어 제외)
    "도쿄": "Tokyo", "오사카": "Osaka", "후쿠오카": "Fukuoka", "삿포로": "Sapporo",
    "나고야": "Nagoya", "오키나와": "Okinawa",
    "타이베이": "Taipei", "가오슝": "Kaohsiung",
    "홍콩": "Hong Kong", "마카오": "Macau",
    "방콕": "Bangkok", "치앙마이": "Chiang Mai", "푸켓": "Phuket",
    "호치민": "Ho Chi Minh City", "하노이": "Hanoi", "다낭": "Da Nang",
    "마닐라": "Manila", "세부": "Cebu", "보라카이": "Boracay",
    "싱가포르": "Singapore", "쿠알라룸푸르": "Kuala Lumpur",
    "프놈펜": "Phnom Penh", "시엠립": "Siem Reap",
    "발리": "Bali", "자카르타": "Jakarta",
}


def _load_state():
    token = os.environ.get("FLIGHT_TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("FLIGHT_TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        raise SystemExit("FLIGHT_TELEGRAM_BOT_TOKEN/FLIGHT_TELEGRAM_CHAT_ID가 없습니다")
    return token, chat_id


def _get_offset() -> int:
    if OFFSET_PATH.exists():
        return int(OFFSET_PATH.read_text().strip() or 0)
    return 0


def _save_offset(offset: int) -> None:
    OFFSET_PATH.parent.mkdir(parents=True, exist_ok=True)
    OFFSET_PATH.write_text(str(offset))


def _fetch_messages(token: str, offset: int) -> tuple[list[dict], int]:
    """getUpdates로 새 메시지(텍스트)만 가져온다. TelegramNotifier.fetch_callbacks는
    callback_query(버튼) 전용이라 여기선 별도로 직접 호출한다."""
    url = f"https://api.telegram.org/bot{token}/getUpdates"
    params = {"timeout": 0, "allowed_updates": '["message"]'}
    if offset:
        params["offset"] = offset

    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    payload = resp.json()
    if not payload.get("ok"):
        raise RuntimeError(f"getUpdates 실패: {payload}")

    messages: list[dict] = []
    max_update_id = offset - 1 if offset else 0
    for update in payload.get("result", []):
        update_id = int(update.get("update_id", 0))
        max_update_id = max(max_update_id, update_id)
        msg = update.get("message")
        if msg and "text" in msg:
            messages.append(msg)

    next_offset = max_update_id + 1 if max_update_id else offset
    return messages, next_offset


def _fmt_dest_list(destinations: list[dict]) -> str:
    return ", ".join(d["display"] for d in destinations)


def _fmt_dates(cfg: dict) -> str:
    lines = []
    for d in cfg["depart_dates"]:
        h = d.get("min_departure_hour")
        constraint = f" ({h}시 이후만)" if h is not None else ""
        lines.append(f"  {d['date']}{constraint}")
    return "\n".join(lines) + f"\n  귀국: {cfg['return_date']}"


def _status_text(cfg: dict) -> str:
    return (
        "📋 현재 설정\n"
        f"예산: ₩{cfg['budget_krw']:,}\n"
        f"날짜:\n{_fmt_dates(cfg)}\n"
        f"목적지({len(cfg['destinations'])}개): {_fmt_dest_list(cfg['destinations'])}"
    )


HELP_TEXT = (
    "✈️ flight_watch 명령어\n"
    "/status — 현재 설정 보기\n"
    "/budget 400000 — 예산(원) 변경\n"
    "/dates 2026-09-30 2026-10-01 2026-10-07 — 출발일(들) + 마지막 값은 귀국일\n"
    "/minhour 2026-09-30 17 — 그 출발일은 17시 이후 항공편만 (0 또는 none이면 해제)\n"
    "/destinations 상하이 시안 칭다오 — 목적지 목록 전체 교체 (카탈로그에 있는 이름만)\n"
    "/adddest 하얼빈 Harbin — 카탈로그에 없는 도시를 표시이름+영문검색어로 추가\n"
    "/help — 이 메시지"
)


def _apply_budget(args: list[str], overrides: dict, cfg: dict) -> str:
    if len(args) != 1:
        return "사용법: /budget 400000"
    text = args[0].replace(",", "")
    if not text.isdigit():
        return "예산은 숫자만 입력하세요. 예: /budget 400000"
    overrides["budget_krw"] = int(text)
    return f"✅ 예산을 ₩{int(text):,}로 변경했습니다."


def _apply_dates(args: list[str], overrides: dict, cfg: dict) -> str:
    if len(args) < 2:
        return "사용법: /dates 2026-09-30 2026-10-01 2026-10-07 (마지막이 귀국일)"
    date_re = re.compile(r"^\d{4}-\d{2}-\d{2}$")
    if not all(date_re.match(a) for a in args):
        return "날짜는 YYYY-MM-DD 형식이어야 합니다."

    *depart_dates, return_date = args
    existing = {d["date"]: d.get("min_departure_hour") for d in cfg["depart_dates"]}
    overrides["depart_dates"] = [
        {"date": d, "min_departure_hour": existing.get(d)} for d in depart_dates
    ]
    overrides["return_date"] = return_date
    return f"✅ 출발일 {', '.join(depart_dates)} / 귀국일 {return_date}로 변경했습니다."


def _apply_minhour(args: list[str], overrides: dict, cfg: dict) -> str:
    if len(args) != 2:
        return "사용법: /minhour 2026-09-30 17  (해제하려면 /minhour 2026-09-30 none)"
    date, hour_text = args
    depart_dates = overrides.get("depart_dates", cfg["depart_dates"])
    depart_dates = [dict(d) for d in depart_dates]
    match = next((d for d in depart_dates if d["date"] == date), None)
    if match is None:
        dates = ", ".join(d["date"] for d in depart_dates)
        return f"{date}는 현재 출발일 목록에 없습니다. 현재 출발일: {dates}"

    if hour_text.lower() in ("none", "0", "해제"):
        match["min_departure_hour"] = None
        overrides["depart_dates"] = depart_dates
        return f"✅ {date} 출발시각 제약을 해제했습니다."

    if not hour_text.isdigit() or not (0 <= int(hour_text) <= 23):
        return "시각은 0~23 사이 숫자거나 none 이어야 합니다."

    match["min_departure_hour"] = int(hour_text)
    overrides["depart_dates"] = depart_dates
    return f"✅ {date}는 이제 {hour_text}시 이후 출발편만 찾습니다."


def _apply_destinations(args: list[str], overrides: dict, cfg: dict) -> str:
    if not args:
        return "사용법: /destinations 상하이 시안 칭다오 (공백 또는 쉼표로 구분)"
    names = [n.strip() for n in " ".join(args).replace(",", " ").split() if n.strip()]
    unknown = [n for n in names if n not in CITY_CATALOG]
    if unknown:
        return (
            f"카탈로그에 없는 도시: {', '.join(unknown)}\n"
            "먼저 /adddest <표시이름> <영문검색어> 로 추가하거나, 철자를 확인하세요."
        )

    custom = {d["display"]: d for d in load_overrides().get("destinations", []) if d["display"] not in CITY_CATALOG}
    overrides["destinations"] = [{"query": CITY_CATALOG[n], "display": n} for n in names]
    return f"✅ 목적지를 {len(names)}개로 교체했습니다: {', '.join(names)}"


def _apply_adddest(args: list[str], overrides: dict, cfg: dict) -> str:
    if len(args) != 2:
        return "사용법: /adddest 하얼빈 Harbin"
    display, query = args
    current = overrides.get("destinations", cfg["destinations"])
    current = [d for d in current if d["display"] != display]
    current.append({"query": query, "display": display})
    overrides["destinations"] = current
    CITY_CATALOG[display] = query  # 이번 실행 중에는 /destinations 에서도 바로 인식되게
    return f"✅ 목적지 '{display}'({query})를 추가했습니다. 총 {len(current)}개."


COMMANDS = {
    "budget": _apply_budget,
    "dates": _apply_dates,
    "minhour": _apply_minhour,
    "destinations": _apply_destinations,
    "adddest": _apply_adddest,
}


def handle_message(text: str, overrides: dict, cfg: dict) -> str | None:
    text = text.strip()
    if not text.startswith("/"):
        return None

    parts = text[1:].split()
    if not parts:
        return None
    cmd, args = parts[0].lower(), parts[1:]

    if cmd == "help":
        return HELP_TEXT
    if cmd == "status":
        return _status_text(cfg)
    if cmd in COMMANDS:
        return COMMANDS[cmd](args, overrides, cfg)
    return f"모르는 명령어입니다: /{cmd}\n\n{HELP_TEXT}"


def main() -> None:
    token, chat_id = _load_state()
    offset = _get_offset()
    messages, next_offset = _fetch_messages(token, offset)

    if not messages:
        return

    overrides = load_overrides()
    notifier = TelegramNotifier(token=token, chat_id=chat_id)
    reply_count = 0

    for msg in messages:
        text = msg.get("text", "")
        cfg = load_base_config()
        cfg.update(overrides)  # 이전 명령어 반영분까지 누적 적용
        reply = handle_message(text, overrides, cfg)
        if reply:
            notifier.send_message(reply, parse_mode="")
            reply_count += 1

    save_overrides(overrides)
    _save_offset(next_offset)
    print(f"처리한 메시지 {len(messages)}건, 응답 {reply_count}건, 다음 offset={next_offset}")


if __name__ == "__main__":
    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env")
    except ImportError:
        pass

    main()
