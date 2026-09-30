"""텔레그램 봇 대화로 flight_watch 설정(예산/상한가)을 바꾸고 현황을 조회하는 커맨드 처리기.

날짜·경유지(오픈조 cin x cout) 조합은 구조가 복잡해져 명령어로는 안 바꾸고
config.yaml을 직접 편집한다.

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
import sqlite3
import sys
import time
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
ROOT = HERE
sys.path.insert(0, str(HERE))

from config_io import (  # noqa: E402
    airport_names,
    iter_combos,
    load_base_config,
    load_config,
    load_overrides,
    region_thresholds,
    save_overrides,
)
from notify import TelegramNotifier  # noqa: E402
from lockfile import single_instance  # noqa: E402

OFFSET_PATH = HERE / "state" / "telegram_offset.txt"

#: 텔레그램 롱폴링 1회 대기시간(초). 이 시간 동안 새 메시지가 오면 즉시 반환된다.
LONG_POLL_TIMEOUT_SECONDS = 50

#: 이 스크립트 1회 실행이 살아 있는 시간(초).
#: 예전에는 "한 번 확인하고 바로 종료"라 스케줄러 주기(10분)만큼 답장이 늦었다.
#: 이제는 다음 실행이 시작되기 직전까지 롱폴링으로 대기해서 명령어에 즉시
#: 반응한다. 작업 설정이 MultipleInstancesPolicy=IgnoreNew라 실행이 겹치면
#: 새 인스턴스가 통째로 무시되므로, 10분 주기보다 반드시 짧게 끝내야 한다.
RUN_DURATION_SECONDS = 570

#: getUpdates가 실패했을 때 재시도까지 쉬는 시간(초)
ERROR_RETRY_DELAY_SECONDS = 5

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


def _fetch_messages(token: str, offset: int, poll_timeout: int = 0) -> tuple[list[dict], int]:
    """getUpdates로 새 메시지(텍스트)만 가져온다. TelegramNotifier.fetch_callbacks는
    callback_query(버튼) 전용이라 여기선 별도로 직접 호출한다.

    poll_timeout > 0이면 텔레그램의 롱폴링을 쓴다 — 새 메시지가 생길 때까지 서버가
    응답을 붙잡고 있다가 즉시 돌려주므로, 몇 분마다 확인하는 방식과 달리 반응이
    바로 온다. HTTP 타임아웃은 롱폴링 대기시간보다 넉넉히 길어야 한다.
    """
    url = f"https://api.telegram.org/bot{token}/getUpdates"
    params = {"timeout": poll_timeout, "allowed_updates": '["message"]'}
    if offset:
        params["offset"] = offset

    resp = requests.get(url, params=params, timeout=poll_timeout + 20)
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


def _fmt_regions(cfg: dict) -> str:
    """켜진 지역별 공항 조합 수·예산·상한가와 매 실행 조회 수를 보여준다."""
    per_region: dict[str, list] = {}
    for region, _cin, _cout in iter_combos(cfg):
        entry = per_region.setdefault(region["name"], [0, region])
        entry[0] += 1
    off = [r["name"] for r in cfg.get("regions", []) if not r.get("enabled", True)]
    lines = [f"지역 {len(per_region)}곳:"]
    for name, (n, region) in per_region.items():
        budget, ceiling = region_thresholds(cfg, region)
        ceil_text = f"₩{ceiling:,}" if ceiling else "없음"
        lines.append(f"  {name} — 조합 {n}개 · 예산 ₩{budget:,} · 상한 {ceil_text}")
    total = sum(n for n, _ in per_region.values()) * len(cfg["date_options"])
    lines.append(f"→ 매 실행 조회 {total}회")
    if off:
        lines.append(f"꺼둔 지역: {', '.join(off)}")
    return "\n".join(lines)


def _status_text(cfg: dict) -> str:
    max_price = cfg.get("max_price_krw")
    max_price_text = f"₩{max_price:,}" if max_price else "없음"
    combos = ", ".join(f"{d['depart']}→{d['return']}" for d in cfg["date_options"])
    return (
        "📋 현재 설정\n"
        f"{cfg['origin']} → {cfg['final_dest']} (오픈조)\n"
        f"기본 예산: ₩{cfg['budget_krw']:,} / 기본 상한가: {max_price_text}\n"
        "(지역에 따로 적힌 값이 있으면 그 지역은 그 값을 씁니다)\n"
        f"날짜 조합:\n  {combos}\n"
        f"{_fmt_regions(cfg)}"
    )


def _show_prices(args: list[str], overrides: dict, cfg: dict) -> str:
    """지역별 예산 이내로 트래킹 중인 항공권을 전부 보여준다.

    track_prices.py가 기록한 가격 이력 DB(state/flight_prices.sqlite)에서
    (공항 조합 × 날짜 조합)별 사상 최저가와 가장 최근 조회가를 읽어, 그 조합이
    속한 지역의 예산 이하인 것만 싼 순서로 나열한다. DB는 track_prices.py
    소유라 여기서는 mode=ro로만 연다 — 두 워크플로가 같은 파일을 쓰지 않게 한
    설계를 깨지 않기 위해서다.
    """
    # DB에 남은 키("CODE" 또는 "CIN-COUT") -> 그 조합이 속한 지역의 예산.
    # 지금 켜져 있지 않은 조합(예전 이력, 꺼둔 지역)은 예산을 알 수 없으니 뺀다.
    budget_by_key: dict[str, int] = {}
    for region, cin, cout in iter_combos(cfg):
        key = cin["code"] if cin["code"] == cout["code"] else f'{cin["code"]}-{cout["code"]}'
        budget_by_key[key] = region_thresholds(cfg, region)[0]

    db_path = HERE / cfg["state_db"]
    try:
        conn = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)
    except sqlite3.OperationalError:
        return "아직 가격 이력이 없습니다. track_prices 첫 조회가 끝나면 다시 시도하세요."

    try:
        all_rows = conn.execute(
            "SELECT destination, depart_date, return_date, best_price_krw "
            "FROM best ORDER BY best_price_krw"
        ).fetchall()
    except sqlite3.OperationalError:
        conn.close()
        return "아직 가격 이력이 없습니다. track_prices 첫 조회가 끝나면 다시 시도하세요."

    rows = [r for r in all_rows if r[0] in budget_by_key and r[3] <= budget_by_key[r[0]]]
    if not rows:
        conn.close()
        return (
            "💰 지역별 예산 이내 항공권: 아직 없음\n"
            "다음 조회에서 예산 이하가 나오면 알림이 갑니다."
        )

    code2disp = airport_names(cfg)

    def label(dest_key: str) -> str:
        # track_prices.py가 만드는 키 그대로: 같은 공항이면 "CODE", 다르면
        # "CIN-COUT". 표시 이름으로 되돌린다.
        parts = dest_key.split("-")
        names = [code2disp.get(p, p) for p in parts]
        return names[0] if len(names) == 1 else f"{names[0]}→{names[1]}"

    lines = [f"💰 지역별 예산 이내 항공권 ({len(rows)}건)", "─" * 20]
    for dest, dep, ret, best_price in rows:
        latest = conn.execute(
            "SELECT price_krw, checked_at FROM checks "
            "WHERE destination=? AND depart_date=? AND return_date=? ORDER BY checked_at DESC LIMIT 1",
            (dest, dep, ret),
        ).fetchone()
        if latest and latest[0] != best_price:
            day = latest[1][5:10].replace("-", "/")
            tail = f" · 최근 ₩{latest[0]:,} ({day})"
        else:
            tail = ""
        lines.append(f"{label(dest)} {dep[5:]}→{ret[5:]} · 최저 ₩{best_price:,}{tail}")
    conn.close()
    lines.append("")
    lines.append("가격 = 트래킹 이래 최저가. 항공편 상세는 알림 메시지에서 확인.")
    return "\n".join(lines)


HELP_TEXT = (
    "✈️ flight_watch 명령어\n"
    "/status — 현재 설정 보기\n"
    "/price — 지역별 예산 이내로 트래킹 중인 항공권 전부 보기\n"
    "/budget 800000 — 기본 예산(원) 변경 (지역에 따로 적힌 예산은 그대로)\n"
    "/maxprice 1500000 — 기본 상한가(원). 초과 조합은 현황에서 뺌 (none이면 해제)\n"
    "/help — 이 메시지\n"
    "\n"
    "날짜·경유지 후보 변경은 config.yaml을 직접 편집하세요 — 오픈조(cin x cout)"
    " 조합이라 텔레그램 명령어로 안전하게 바꾸기엔 구조가 복잡해졌습니다."
)


def _apply_budget(args: list[str], overrides: dict, cfg: dict) -> str:
    if len(args) != 1:
        return "사용법: /budget 400000"
    text = args[0].replace(",", "")
    if not text.isdigit():
        return "예산은 숫자만 입력하세요. 예: /budget 400000"
    overrides["budget_krw"] = int(text)
    return f"✅ 예산을 ₩{int(text):,}로 변경했습니다."


def _apply_maxprice(args: list[str], overrides: dict, cfg: dict) -> str:
    if len(args) != 1:
        return "사용법: /maxprice 800000  (해제하려면 /maxprice none)"
    text = args[0].replace(",", "").lower()
    if text in ("none", "off", "0"):
        overrides["max_price_krw"] = None
        return "✅ 상한가를 해제했습니다. 이제 모든 목적지가 현황에 나옵니다."
    if not text.isdigit():
        return "상한가는 숫자만 입력하세요. 예: /maxprice 800000"
    overrides["max_price_krw"] = int(text)
    return (
        f"✅ 상한가를 ₩{int(text):,}로 설정했습니다.\n"
        "이 값을 넘는 목적지는 현황·최저가 갱신 알림에서 빠집니다 "
        "(25% 이상 급락 시엔 예외로 알림)."
    )


COMMANDS = {
    "budget": _apply_budget,
    "maxprice": _apply_maxprice,
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
    if cmd == "price":
        return _show_prices(args, overrides, cfg)
    if cmd in COMMANDS:
        return COMMANDS[cmd](args, overrides, cfg)
    return f"모르는 명령어입니다: /{cmd}\n\n{HELP_TEXT}"


def main() -> None:
    """진입점. 같은 스크립트가 이미 실행 중이면 조용히 건너뛴다.

    작업 스케줄러의 IgnoreNew 설정을 이미 켜뒀지만, 실측으로 flight_watch
    프로세스가 중복으로 뜬 걸 확인해서 스크립트 자체에도 락을 건다 (자세한
    배경은 lockfile.py 참고). 이 스크립트는 실행 1회가 최대
    RUN_DURATION_SECONDS만큼 롱폴링하므로, 스케줄 간격보다 오래 걸리면
    다음 실행과 겹칠 수 있다.
    """
    with single_instance(HERE / "state" / ".bot_commands.lock", label="bot_commands") as acquired:
        if not acquired:
            return
        _run()


def _run() -> None:
    token, chat_id = _load_state()
    notifier = TelegramNotifier(token=token, chat_id=chat_id)

    deadline = time.monotonic() + RUN_DURATION_SECONDS
    total_messages = 0
    total_replies = 0

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 1:
            break
        poll_timeout = int(min(LONG_POLL_TIMEOUT_SECONDS, remaining - 1))

        offset = _get_offset()
        try:
            messages, next_offset = _fetch_messages(token, offset, poll_timeout)
        except Exception as e:  # noqa: BLE001 - 일시적 네트워크 오류로 죽지 않게
            print(f"getUpdates 실패, {ERROR_RETRY_DELAY_SECONDS}초 후 재시도: {e}")
            time.sleep(ERROR_RETRY_DELAY_SECONDS)
            continue

        # 텍스트 메시지가 없어도 offset은 넘겨야 한다. 안 그러면 사진 같은
        # 비텍스트 업데이트가 계속 대기열에 남아 롱폴링이 즉시 반환되면서
        # API를 쉴 새 없이 두드리게 된다.
        if not messages:
            if next_offset != offset:
                _save_offset(next_offset)
            continue

        overrides = load_overrides()
        for msg in messages:
            text = msg.get("text", "")
            cfg = load_base_config()
            cfg.update(overrides)  # 이전 명령어 반영분까지 누적 적용
            reply = handle_message(text, overrides, cfg)
            if reply:
                notifier.send_message(reply, parse_mode="")
                total_replies += 1

        save_overrides(overrides)
        _save_offset(next_offset)
        total_messages += len(messages)
        print(f"처리한 메시지 {len(messages)}건, 다음 offset={next_offset}", flush=True)

    print(f"종료: 이번 실행에서 메시지 {total_messages}건, 응답 {total_replies}건")


if __name__ == "__main__":
    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env")
    except ImportError:
        pass

    main()
