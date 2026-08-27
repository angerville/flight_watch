"""대련(DLC) 출발 국경절 항공권 최저가 트래커.

Ctrip/Trip.com은 자체 WAF("whaleguard")가 requests는 물론 Playwright 헤드리스
브라우저 접근까지 즉시 차단한다 (확인 결과 상태코드 432, 본문 "whaleguard
block"). 이를 우회하려면 안티디텍션/프록시 같은 차단 회피 기법이 필요해지므로
쓰지 않는다. 대신 Google Flights는 봇 차단 없이 정상 응답하고 통화를 KRW로
고정할 수 있어, 데이터 소스로 Google Flights만 쓴다.

실행 1회 = 설정된 목적지 x 출발일 조합을 모두 조회해 최저가를 기록하고,
사상 최저가 갱신 또는 예산 이내 진입 시에만 텔레그램으로 알린다 (매번 알리면
스팸이 되므로).
"""

from __future__ import annotations

import logging
import re
import sqlite3
import sys
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

import yaml
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline.notify import TelegramNotifier  # noqa: E402

HERE = Path(__file__).resolve().parent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

#: 사상 최저가가 아니어도, 직전 최저가 대비 이만큼(비율) 이상 떨어지면 알림
NOTABLE_DROP_PCT = 0.10


@dataclass
class FlightResult:
    price_krw: int
    airline: str
    route: str
    stops: str
    depart_time: str
    depart_hour: int | None


def load_config() -> dict:
    with open(HERE / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def init_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS checks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            destination TEXT NOT NULL,
            depart_date TEXT NOT NULL,
            return_date TEXT NOT NULL,
            price_krw INTEGER,
            airline TEXT,
            route TEXT,
            stops TEXT,
            checked_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS best (
            destination TEXT NOT NULL,
            depart_date TEXT NOT NULL,
            return_date TEXT NOT NULL,
            best_price_krw INTEGER NOT NULL,
            alerted_under_budget INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (destination, depart_date, return_date)
        )
        """
    )
    conn.commit()
    return conn


def build_url(origin: str, dest_query: str, dep_date: str, ret_date: str) -> str:
    q = f"Flights from {origin} to {dest_query} on {dep_date} through {ret_date}"
    return f"https://www.google.com/travel/flights?q={urllib.parse.quote(q)}&hl=ko&gl=KR&curr=KRW"


_DURATION_RE = re.compile(r"^\d+시간(\s*\d+분)?$|^\d+분$")
_ROUTE_RE = re.compile(r"^[A-Z]{2,4}[–-][A-Z]{2,4}$")
_STOPS_RE = re.compile(r"^(직항|경유\s*\d+회)$")
_TIME_RE = re.compile(r"^(오전|오후)\s*(\d{1,2}):(\d{2})$")


def _parse_hour(line: str) -> int | None:
    """"오후 10:15" 같은 표기를 24시간제 시(hour)로 바꾼다. 매칭 안 되면 None."""
    m = _TIME_RE.match(line)
    if not m:
        return None
    period, hour = m.group(1), int(m.group(2))
    if period == "오후" and hour != 12:
        hour += 12
    if period == "오전" and hour == 12:
        hour = 0
    return hour


def parse_all_flights(body_text: str) -> list[FlightResult]:
    """렌더링된 본문 텍스트에서 가격이 매겨진 모든 항공편을 뽑아낸다.

    Google Flights 결과 한 건은 대략 "출발시간 / - / 도착시간 / 항공사 /
    소요시간 / 구간 / 직항|경유n회 / (경유 시 환승 상세) / CO2e.. / 배출률 /
    ₩가격 / 왕복" 순서로 렌더링되는데, 경유 항공편은 환승 상세 줄이 끼어들어
    엔트리 하나의 줄 수가 고정이 아니다. 그래서 "₩숫자\\n왕복" 로 각 엔트리의
    가격을 먼저 찾고, 그 앞의 "왕복"(또는 목록 시작)부터 이번 가격 줄까지를
    한 엔트리 블록으로 보고 그 안에서 출발시간/소요시간/구간/직항·경유를
    정규식으로 찾는다 ("가격을 확인할 수 없습니다"인 항목은 애초에 매칭 안 됨).
    """
    lines = [ln.strip() for ln in body_text.splitlines()]
    price_lines: list[int] = [
        i - 1
        for i, line in enumerate(lines)
        if line == "왕복" and i > 0 and re.match(r"^₩([\d,]+)$", lines[i - 1])
    ]

    results: list[FlightResult] = []
    block_start = 0
    for price_idx in price_lines:
        window = lines[block_start:price_idx]
        price = int(re.match(r"^₩([\d,]+)$", lines[price_idx]).group(1).replace(",", ""))

        duration_li = next((k for k, l in enumerate(window) if _DURATION_RE.match(l)), None)
        route_li = next((k for k, l in enumerate(window) if _ROUTE_RE.match(l)), None)
        stops_li = next((k for k, l in enumerate(window) if _STOPS_RE.match(l)), None)
        depart_li = next((k for k, l in enumerate(window) if _TIME_RE.match(l)), None)

        results.append(
            FlightResult(
                price_krw=price,
                airline=window[duration_li - 1] if duration_li else "",
                route=window[route_li] if route_li is not None else "",
                stops=window[stops_li] if stops_li is not None else "",
                depart_time=window[depart_li] if depart_li is not None else "",
                depart_hour=_parse_hour(window[depart_li]) if depart_li is not None else None,
            )
        )
        block_start = price_idx + 2  # 가격 줄 다음의 "왕복" 줄까지 건너뛴다

    return results


def pick_cheapest(flights: list[FlightResult], min_departure_hour: int | None) -> FlightResult | None:
    """가격순으로 정렬해 출발시각 제약을 만족하는 첫 항공편을 고른다."""
    eligible = [f for f in flights if min_departure_hour is None or (f.depart_hour is not None and f.depart_hour >= min_departure_hour)]
    if not eligible:
        return None
    return min(eligible, key=lambda f: f.price_krw)


def fetch_flights(page, url: str, render_wait_ms: int) -> list[FlightResult]:
    try:
        resp = page.goto(url, timeout=45000, wait_until="domcontentloaded")
    except Exception as exc:  # noqa: BLE001
        log.warning("페이지 로드 실패: %s (%s)", url, exc)
        return []

    if resp is None or resp.status != 200:
        log.warning("비정상 응답 status=%s url=%s", resp.status if resp else None, url)
        return []

    page.wait_for_timeout(render_wait_ms)
    body = page.inner_text("body")
    if "whaleguard" in body.lower():
        log.error("차단 응답 감지 (whaleguard) — 조회 중단: %s", url)
        return []

    return parse_all_flights(body)


def check_and_record(
    conn: sqlite3.Connection,
    destination: str,
    display: str,
    dep_date: str,
    ret_date: str,
    result: FlightResult,
    budget_krw: int,
) -> str | None:
    """이번 조회 결과를 기록하고, 알림이 필요하면 알림 문구를 돌려준다."""
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "INSERT INTO checks (destination, depart_date, return_date, price_krw, airline, route, stops, checked_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (destination, dep_date, ret_date, result.price_krw, result.airline, result.route, result.stops, now),
    )

    row = conn.execute(
        "SELECT best_price_krw, alerted_under_budget FROM best WHERE destination=? AND depart_date=? AND return_date=?",
        (destination, dep_date, ret_date),
    ).fetchone()

    alert = None
    detail = (
        f"{display} ({dep_date} {result.depart_time} 출발 → {ret_date})\n"
        f"{result.airline} · {result.route} · {result.stops}\n₩{result.price_krw:,} 왕복"
    )

    if row is None:
        under_budget = result.price_krw <= budget_krw
        conn.execute(
            "INSERT INTO best (destination, depart_date, return_date, best_price_krw, alerted_under_budget) VALUES (?,?,?,?,?)",
            (destination, dep_date, ret_date, result.price_krw, int(under_budget)),
        )
        if under_budget:
            alert = f"🆕 예산 이내로 발견\n{detail}"
    else:
        prev_best, alerted = row
        if result.price_krw < prev_best:
            drop_pct = (prev_best - result.price_krw) / prev_best
            under_budget = result.price_krw <= budget_krw
            newly_crossed_budget = under_budget and not alerted
            notable = drop_pct >= NOTABLE_DROP_PCT

            conn.execute(
                "UPDATE best SET best_price_krw=?, alerted_under_budget=? WHERE destination=? AND depart_date=? AND return_date=?",
                (result.price_krw, int(alerted or under_budget), destination, dep_date, ret_date),
            )

            if newly_crossed_budget:
                alert = f"💰 예산 이내로 진입 (이전 최저가 ₩{prev_best:,})\n{detail}"
            elif notable:
                alert = f"📉 최저가 갱신 (-{drop_pct:.0%}, 이전 ₩{prev_best:,})\n{detail}"

    conn.commit()
    return alert


def main() -> None:
    cfg = load_config()
    db_path = HERE / cfg["state_db"]
    conn = init_db(db_path)

    alerts: list[str] = []
    checked = 0
    skipped = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        for dest in cfg["destinations"]:
            for dep_entry in cfg["depart_dates"]:
                dep_date = dep_entry["date"]
                min_hour = dep_entry.get("min_departure_hour")

                url = build_url(cfg["origin"], dest["query"], dep_date, cfg["return_date"])
                flights = fetch_flights(page, url, cfg["render_wait_ms"])
                result = pick_cheapest(flights, min_hour)
                checked += 1

                if result is None:
                    skipped += 1
                    reason = "결과 없음" if not flights else f"{min_hour}시 이후 출발편 없음"
                    log.info("가격 확인 실패 — 건너뜀: %s (%s, %s)", dest["display"], dep_date, reason)
                else:
                    log.info(
                        "%s (%s, %s 출발): ₩%s", dest["display"], dep_date, result.depart_time, f"{result.price_krw:,}"
                    )
                    alert = check_and_record(
                        conn, dest["query"], dest["display"], dep_date, cfg["return_date"], result, cfg["budget_krw"]
                    )
                    if alert:
                        alerts.append(alert)

                time.sleep(cfg["request_delay_seconds"])

        browser.close()

    log.info("완료: %d건 조회, %d건 실패, %d건 알림", checked, skipped, len(alerts))

    if alerts:
        import os

        # 공모전 파이프라인(contest_auto)이 쓰는 TELEGRAM_BOT_TOKEN과는 별도로,
        # 이 트래커 전용 봇(FLIGHT_TELEGRAM_BOT_TOKEN)을 쓴다 — 알림이 섞이지
        # 않게 하려고 일부러 분리함.
        token = os.environ.get("FLIGHT_TELEGRAM_BOT_TOKEN", "").strip()
        chat_id = os.environ.get("FLIGHT_TELEGRAM_CHAT_ID", "").strip()
        if not token or not chat_id:
            log.warning("FLIGHT_TELEGRAM_BOT_TOKEN/FLIGHT_TELEGRAM_CHAT_ID가 없어 알림을 보내지 못했습니다")
        else:
            notifier = TelegramNotifier(token=token, chat_id=chat_id)
            header = f"✈️ 대련 출발 국경절 항공권 알림 ({len(alerts)}건)\n{'─' * 20}\n"
            notifier.send_message(header + "\n\n".join(alerts), parse_mode="")

    conn.close()


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    main()
