"""대련(DLC) 출발 국경절 항공권 최저가 트래커.

Ctrip/Trip.com은 자체 WAF("whaleguard")가 requests는 물론 Playwright 헤드리스
브라우저 접근까지 즉시 차단한다 (확인 결과 상태코드 432, 본문 "whaleguard
block"). 이를 우회하려면 안티디텍션/프록시 같은 차단 회피 기법이 필요해지므로
쓰지 않는다. 대신 Google Flights는 봇 차단 없이 정상 응답하고 통화를 KRW로
고정할 수 있어, 데이터 소스로 Google Flights만 쓴다.

가격/시각 파싱은 화면에 렌더링된 텍스트 줄 순서가 아니라 각 항공편 카드의
스크린리더용 aria-label 문장("왕복 총 가격은 최저 233808 대한민국
원입니다...")을 정규식으로 파싱한다 — 경유편이 끼어들면 줄 수가 달라지는
줄-순서 기반 파싱보다 훨씬 안정적이다.

실행 1회 = 설정된 목적지 x 출발일 조합을 모두 조회해 최저가를 기록하고,
사상 최저가 갱신 또는 예산 이내 진입 시에만 텔레그램으로 알린다 (매번 알리면
스팸이 되므로). 귀국편 시각은 비용이 커서(항공편 하나를 실제로 클릭해 다음
화면까지 들어가야 함) 매 조회마다 구하지 않고, 알림을 보낼 때만 그 항공편에
한해 조회한다.
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

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline.notify import TelegramNotifier  # noqa: E402
from config_io import load_config  # noqa: E402

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
class FlightLeg:
    price_krw: int
    airline: str
    stops: str
    dep_display: str  # "오후 10:15"
    dep_hour: int | None
    dep_airport: str
    arr_display: str
    arr_airport: str
    duration: str
    raw_label: str  # 이 항공편 카드를 다시 찾아 클릭하기 위한 원문 aria-label


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
            stops TEXT,
            dep_display TEXT,
            arr_airport TEXT,
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


_LEG_RE = re.compile(
    r"왕복 총 가격은 최저\s*(?P<price>\d+)\s*대한민국 원입니다\.\s*"
    r"(?P<airline>.+?)의\s*(?P<stops>직항|\d+회\s*경유)\s*항공편입니다\.\s*"
    r"\S+요일,\s*\d+월\s*\d+\s*(?P<dep_period>오전|오후)\s*(?P<dep_time>\d{1,2}:\d{2})에\s*"
    r"(?P<dep_airport>.+?)에서\s*출발하여\s*"
    r"\S+요일,\s*\d+월\s*\d+\s*(?P<arr_period>오전|오후)\s*(?P<arr_time>\d{1,2}:\d{2})에\s*"
    r"(?P<arr_airport>.+?)에\s*도착합니다\.\s*"
    r"총 비행 시간은\s*(?P<duration>.+?)입니다\."
)


def _parse_hour(period: str, time_str: str) -> int | None:
    """"오후", "10:15" -> 22 (24시간제 시). 매칭 실패는 상위에서 처리."""
    try:
        hour = int(time_str.split(":")[0])
    except (ValueError, IndexError):
        return None
    if period == "오후" and hour != 12:
        hour += 12
    if period == "오전" and hour == 12:
        hour = 0
    return hour


def _extract_legs(page) -> list[FlightLeg]:
    """현재 페이지에 보이는 항공편 카드를 aria-label에서 파싱해 전부 뽑아낸다."""
    labels: list[str] = page.evaluate(
        """
        () => Array.from(document.querySelectorAll('[aria-label]'))
            .map(e => e.getAttribute('aria-label'))
            .filter(l => l && l.startsWith('왕복 총 가격은'))
        """
    )
    legs: list[FlightLeg] = []
    for label in labels:
        m = _LEG_RE.search(label)
        if not m:
            continue
        gd = m.groupdict()
        legs.append(
            FlightLeg(
                price_krw=int(gd["price"]),
                airline=gd["airline"].strip(),
                stops=gd["stops"].strip(),
                dep_display=f'{gd["dep_period"]} {gd["dep_time"]}',
                dep_hour=_parse_hour(gd["dep_period"], gd["dep_time"]),
                dep_airport=gd["dep_airport"].strip(),
                arr_display=f'{gd["arr_period"]} {gd["arr_time"]}',
                arr_airport=gd["arr_airport"].strip(),
                duration=gd["duration"].strip(),
                raw_label=label,
            )
        )
    return legs


def pick_cheapest(legs: list[FlightLeg], min_departure_hour: int | None) -> FlightLeg | None:
    """가격순으로 정렬해 출발시각 제약을 만족하는 항공편 중 최저가를 고른다."""
    eligible = [
        f for f in legs if min_departure_hour is None or (f.dep_hour is not None and f.dep_hour >= min_departure_hour)
    ]
    if not eligible:
        return None
    return min(eligible, key=lambda f: f.price_krw)


def fetch_outbound_legs(page, url: str, render_wait_ms: int) -> list[FlightLeg]:
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

    return _extract_legs(page)


def fetch_return_leg(page, outbound: FlightLeg, render_wait_ms: int) -> FlightLeg | None:
    """알림 보낼 항공편으로 확정된 가는편을 실제로 클릭해 귀국편 화면으로 들어간 뒤,
    가는편과 총 가격이 일치하는(=같은 조합의) 귀국편을 찾아 돌려준다.

    가는편 카드 위에 소요시간 표시 div가 겹쳐 있어 기본 클릭이 "가려짐" 판정으로
    실패하므로 force=True로 클릭한다.
    """
    try:
        page.locator(f'[aria-label="{outbound.raw_label}"]').first.click(force=True, timeout=10000)
    except Exception as exc:  # noqa: BLE001
        log.warning("가는편 클릭 실패 — 귀국편 시각 생략: %s", exc)
        return None

    page.wait_for_timeout(render_wait_ms)
    legs = _extract_legs(page)
    if not legs:
        return None

    exact = next((l for l in legs if l.price_krw == outbound.price_krw), None)
    if exact:
        return exact

    log.warning("귀국편 중 가는편과 총액이 일치하는 항목을 못 찾음 — 최저가로 대체")
    return min(legs, key=lambda l: l.price_krw)


def check_and_record(
    conn: sqlite3.Connection,
    destination: str,
    display: str,
    dep_date: str,
    ret_date: str,
    result: FlightLeg,
    budget_krw: int,
) -> str | None:
    """이번 조회 결과를 기록하고, 알림이 필요하면 알림 문구(가는편만)를 돌려준다.

    귀국편 시각은 여기서 채우지 않는다 — 호출부(main)가 알림이 실제로 필요할
    때만 fetch_return_leg로 추가 조회해 붙인다.
    """
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "INSERT INTO checks (destination, depart_date, return_date, price_krw, airline, stops, dep_display, arr_airport, checked_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (destination, dep_date, ret_date, result.price_krw, result.airline, result.stops, result.dep_display, result.arr_airport, now),
    )

    row = conn.execute(
        "SELECT best_price_krw, alerted_under_budget FROM best WHERE destination=? AND depart_date=? AND return_date=?",
        (destination, dep_date, ret_date),
    ).fetchone()

    alert = None
    detail = (
        f"{display} ({dep_date})\n"
        f"[가는편] {result.dep_display} 출발 · {result.airline} · {result.stops} · {result.arr_airport} 도착\n"
        f"₩{result.price_krw:,} 왕복"
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
                legs = fetch_outbound_legs(page, url, cfg["render_wait_ms"])
                result = pick_cheapest(legs, min_hour)
                checked += 1

                if result is None:
                    skipped += 1
                    reason = "결과 없음" if not legs else f"{min_hour}시 이후 출발편 없음"
                    log.info("가격 확인 실패 — 건너뜀: %s (%s, %s)", dest["display"], dep_date, reason)
                else:
                    log.info(
                        "%s (%s, %s 출발): ₩%s", dest["display"], dep_date, result.dep_display, f"{result.price_krw:,}"
                    )
                    alert = check_and_record(
                        conn, dest["query"], dest["display"], dep_date, cfg["return_date"], result, cfg["budget_krw"]
                    )
                    if alert:
                        return_leg = fetch_return_leg(page, result, cfg["render_wait_ms"])
                        if return_leg:
                            alert += f"\n[오는편] {return_leg.dep_airport} {return_leg.dep_display} 출발 ({cfg['return_date']})"
                        else:
                            alert += "\n[오는편] 시각 확인 실패 — 링크에서 직접 확인 필요"
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
    try:
        # 로컬 실행 시 .env를 자동으로 읽어 환경변수로 반영한다. GitHub Actions
        # 등 이미 환경변수가 주입된 곳에서는 .env도 dotenv 패키지도 없어도 되게
        # 조용히 넘어간다.
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env")
    except ImportError:
        pass

    main()
