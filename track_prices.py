"""대련(DLC) 출발 · 인천(ICN) 도착 오픈조 항공권 최저가 트래커.

대련에서 나가서(1월 9~11일 사이) 후보 경유/체류 도시 하나를 거쳐, 인천으로
들어오는(1월 22~24일 사이) 여정을 추적한다. 출발지와 최종 도착지가 달라
"들어가는 도시 != 나오는 도시"인 오픈조라 자연어 질의("Flights from A to B")로는
조회가 안 된다 — 대신 구글 항공권이 검색 조건을 URL의 tfs 파라미터(protobuf를
base64url로 인코딩한 것)에 싣는 걸 이용해 직접 만들어 붙인다.

Ctrip/Trip.com은 자체 WAF("whaleguard")가 requests는 물론 Playwright 헤드리스
브라우저 접근까지 즉시 차단한다 (확인 결과 상태코드 432, 본문 "whaleguard
block"). 이를 우회하려면 안티디텍션/프록시 같은 차단 회피 기법이 필요해지므로
쓰지 않는다. 대신 Google Flights는 봇 차단 없이 정상 응답하고 통화를 KRW로
고정할 수 있어, 데이터 소스로 Google Flights만 쓴다.

가격/시각 파싱은 화면에 렌더링된 텍스트 줄 순서가 아니라 각 항공편 카드의
스크린리더용 aria-label 문장("총 가격은 최저 233808 대한민국 원입니다...")을
정규식으로 파싱한다 — 경유편이 끼어들면 줄 수가 달라지는 줄-순서 기반 파싱보다
훨씬 안정적이다.

실행 1회 = 설정된 경유지 x 출발일 x 도착일 조합을 모두 조회해 최저가를
기록하고, 사상 최저가 갱신 또는 예산 이내 진입 시에만 텔레그램으로 알린다
(매번 알리면 스팸이 되므로). 오는편(인천행) 시각은 비용이 커서(항공편 하나를
실제로 클릭해 다음 화면까지 들어가야 함) 매 조회마다 구하지 않고, 알림을
보낼 때만 그 항공편에 한해 조회한다.
"""

from __future__ import annotations

import base64
import logging
import os
import re
import sqlite3
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from notify import TelegramNotifier  # noqa: E402
from config_io import iter_combos, load_config, region_thresholds  # noqa: E402
from lockfile import single_instance  # noqa: E402

HERE = Path(__file__).resolve().parent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

#: 사상 최저가가 아니어도, 직전 최저가 대비 이만큼(비율) 이상 떨어지면 알림
NOTABLE_DROP_PCT = 0.10

#: 상한가(config의 max_price_krw)를 넘는 노선은 평소 관심 밖이라 현황 요약과
#: "최저가 갱신" 알림에서 빼지만, 직전 최저가 대비 이 비율 이상 급락하면
#: "진짜 특가"로 보고 상한가 위여도 그때는 알린다.
SPECIAL_DEAL_DROP_PCT = 0.25

#: "평소보다 저렴" 특가 판정 — 사상 최저가를 새로 찍지 않아도, 현재가가 최근
#: 이력의 중앙값보다 이 비율 이상 싸면 알린다 (값이 출렁이기만 하고 기록은
#: 안 깨는 경우를 잡으려는 것). 상한가와 무관하게 모든 목적지에 적용.
BELOW_TYPICAL_PCT = 0.15
#: 중앙값을 낼 때 참고하는 "직전" 조회 건수(이번 조회는 제외).
TYPICAL_LOOKBACK = 12
#: 이력이 이만큼도 안 쌓였으면 "평소" 기준이 불안정하므로 판정하지 않는다.
TYPICAL_MIN_SAMPLES = 6

#: 브라우저 실행 재시도 횟수. Task Scheduler로 돌리면 가끔 Playwright가
#: "Executable doesn't exist"로 죽는다 (chrome-headless-shell.exe 파일이
#: 실제로는 디스크에 그대로 있는데도). 원인이 무엇이든 - 브라우저가 지워졌든,
#: 일시적으로 접근이 막혔든 - 그 시간대 조회를 통째로 날리지 않도록,
#: 실패하면 브라우저를 자동으로 재설치하고 다시 시도한다.
BROWSER_LAUNCH_ATTEMPTS = 3
BROWSER_LAUNCH_RETRY_DELAY_SECONDS = 5

#: 헤드리스 크로뮴 안정화 옵션.
#:
#: 조회 도중 "Page crashed"로 렌더러가 죽는 일이 잦았다. 한 번 죽으면 그 뒤
#: 조회가 줄줄이 실패할 뿐 아니라, Playwright가 죽은 브라우저를 정리하면서
#: 파이썬 프로세스까지 함께 끌려 죽어(작업 스케줄러 종료코드 0xC000013A)
#: 그 시간대 알림이 통째로 사라졌다.
#:
#: /dev/shm 대신 임시 파일을 쓰게 하고(공유 메모리 부족으로 인한 렌더러
#: 크래시 방지), 헤드리스에서 쓸모없는 GPU/확장/백그라운드 작업을 꺼서
#: 크래시 요인 자체를 줄인다.
BROWSER_LAUNCH_ARGS = [
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-extensions",
    "--disable-background-networking",
    "--disable-renderer-backgrounding",
    "--disable-backgrounding-occluded-windows",
]


def install_browser() -> None:
    """Playwright 브라우저(chromium)를 현재 파이썬 환경에 (재)설치한다."""
    log.info("Playwright 브라우저를 설치합니다 (몇 분 걸릴 수 있음)...")
    # encoding/errors를 명시하지 않으면 Windows 기본 코드페이지(cp949)로 디코딩하다
    # playwright가 출력하는 UTF-8 박스 문자에서 UnicodeDecodeError가 난다.
    proc = subprocess.run(
        [sys.executable, "-m", "playwright", "install", "chromium"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode == 0:
        log.info("Playwright 브라우저 설치 완료")
    else:
        log.warning("Playwright 브라우저 설치 실패 (코드 %d): %s", proc.returncode, proc.stderr.strip()[:500])


def launch_browser(p):
    """브라우저를 띄운다. 실패하면 재설치까지 시도한 뒤 재시도한다."""
    last_err: Exception | None = None
    for attempt in range(1, BROWSER_LAUNCH_ATTEMPTS + 1):
        try:
            return p.chromium.launch(headless=True, args=BROWSER_LAUNCH_ARGS)
        except Exception as e:  # noqa: BLE001 - 어떤 실패든 일단 복구를 시도한다
            last_err = e
            log.warning("브라우저 실행 실패 (시도 %d/%d): %s", attempt, BROWSER_LAUNCH_ATTEMPTS, e)
            if attempt < BROWSER_LAUNCH_ATTEMPTS:
                install_browser()
                time.sleep(BROWSER_LAUNCH_RETRY_DELAY_SECONDS)
    assert last_err is not None
    raise last_err


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

    # 기존 DB에 없을 수 있는 컬럼은 여기서 채운다 (마이그레이션 도구가 따로 없어서).
    best_cols = {r[1] for r in conn.execute("PRAGMA table_info(best)")}
    if "alerted_below_typical" not in best_cols:
        conn.execute("ALTER TABLE best ADD COLUMN alerted_below_typical INTEGER NOT NULL DEFAULT 0")

    conn.commit()
    return conn


#: 오픈조/다구간 tfs 인코딩용 트립 타입. 딱 왕복(같은 도시로 들고 남)이면
#: TRIP_ROUND, 들어가는 곳과 나오는 곳이 다르면 TRIP_MULTI_CITY.
TRIP_ROUND = 1
TRIP_ONE_WAY = 2
TRIP_MULTI_CITY = 3


def _pb_varint(n: int) -> bytes:
    out = b""
    while True:
        chunk = n & 0x7F
        n >>= 7
        out += bytes([chunk | (0x80 if n else 0)])
        if not n:
            return out


def _pb_bytes(field: int, data: bytes) -> bytes:
    return _pb_varint((field << 3) | 2) + _pb_varint(len(data)) + data


def _pb_str(field: int, s: str) -> bytes:
    return _pb_bytes(field, s.encode())


def _pb_int(field: int, n: int) -> bytes:
    return _pb_varint(field << 3) + _pb_varint(n)


def build_tfs_url(legs: list[tuple[str, str, str]], trip: int) -> str:
    """구간 목록으로 구글 항공권 조회 URL을 만든다.

    legs: [(출발일 YYYY-MM-DD, 출발지 IATA, 도착지 IATA), ...]
    trip: TRIP_ROUND(왕복) 또는 TRIP_MULTI_CITY(다구간/오픈조)
    """
    body = b""
    for date, frm, to in legs:
        leg = _pb_str(2, date) + _pb_bytes(13, _pb_str(2, frm)) + _pb_bytes(14, _pb_str(2, to))
        body += _pb_bytes(3, leg)
    body += _pb_int(8, 1)      # 성인 1명
    body += _pb_int(9, 1)      # 이코노미
    body += _pb_int(19, trip)
    tfs = base64.urlsafe_b64encode(body).decode().rstrip("=")
    return f"https://www.google.com/travel/flights?tfs={tfs}&hl=ko&gl=KR&curr=KRW"


# 항공편 카드의 aria-label 파싱. 왕복/다구간은 "왕복 총 가격은 최저 ...", 편도는
# "최저가는 ..." 형태다. 가격은 여정 전체 총액, 카드에 적힌 시각은 가는편의 것이다.
_LEG_RE = re.compile(
    r"(?:(?:왕복\s*)?총 가격은 최저|최저가는)\s*(?P<price>\d+)\s*대한민국 원입니다\.\s*"
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
            .filter(l => l && (l.includes('총 가격은 최저') || l.startsWith('최저가는')))
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

    # 결과가 뜨는 즉시 읽는다. 예전엔 고정 시간(9초)만 기다렸는데, 구글이 처음
    # 계산하는 조합(한 번도 조회 안 한 날짜·공항 쌍)은 첫 카드가 뜨기까지 10초
    # 넘게 걸리기도 해서(실측 12초) 결과가 있는데도 "없음"으로 놓쳤다. 반대로
    # 이미 계산된 조합은 1~5초면 뜨니 폴링하면 평균 조회도 빨라진다.
    # render_wait_ms는 이제 "최대 대기 시간"이다.
    deadline = time.monotonic() + render_wait_ms / 1000
    legs: list[FlightLeg] = []
    noprice_since: float | None = None
    while time.monotonic() < deadline:
        page.wait_for_timeout(500)
        legs = _extract_legs(page)
        if legs:
            # 카드가 하나씩 그려지므로 나머지가 마저 뜰 여유를 준 뒤 다시 읽는다.
            page.wait_for_timeout(1500)
            legs = _extract_legs(page)
            break

        # 항공편 목록은 떴는데 전부 "가격을 확인할 수 없습니다"면(그 조합엔 판매 중인
        # 운임이 없음) 더 기다려도 소용없다. 잠깐 뜨는 자리표시일 수 있으니 4초
        # 넘게 이어질 때만 포기한다.
        body = page.inner_text("body")
        if "개가 반환되었습니다" in body and "가격을 확인할 수 없습니다" in body:
            noprice_since = noprice_since or time.monotonic()
            if time.monotonic() - noprice_since >= 4:
                break
        else:
            noprice_since = None

    body = page.inner_text("body")
    if "whaleguard" in body.lower():
        log.error("차단 응답 감지 (whaleguard) — 조회 중단: %s", url)
        return []

    return legs


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


def _hhmm_to_display(hhmm: str) -> str:
    """"19:10" -> "오후 7:10" (항공편 카드의 dep_display 표기에 맞춘다)."""
    hour, minute = (int(x) for x in hhmm.split(":"))
    period = "오후" if hour >= 12 else "오전"
    hour12 = hour % 12 or 12
    return f"{period} {hour12}:{minute:02d}"


def _display_to_hhmm(display: str) -> str:
    """"오후 7:10" -> "19:10"."""
    period, hm = display.split()
    hour, minute = (int(x) for x in hm.split(":"))
    if period == "오후" and hour != 12:
        hour += 12
    if period == "오전" and hour == 12:
        hour = 0
    return f"{hour:02d}:{minute:02d}"


def _pick_legs(legs: list[FlightLeg], spec: dict, direct_only: bool = False) -> list[tuple[str, FlightLeg | None]]:
    """구간 설정(airline, depart)에 맞는 편을 (표시 라벨, 편) 목록으로 고른다.

    depart가 "all"이면 검색 결과에 나온 편 전부(같은 항공사·시각은 최저가 하나만,
    출발시각순)를 "항공사 HH:MM" 라벨로 돌려준다. 시각(들)을 적었으면 그 순서대로
    "HH:MM" 라벨과 맞는 편(없으면 None)을 돌려준다.
    """
    airline = spec.get("airline")
    depart = spec.get("depart", "all")
    after = spec.get("after")  # "HH:MM" — 이 시각 이후(포함) 출발편만
    legs = [
        l for l in legs
        if (not airline or airline in l.airline)
        and (not direct_only or l.stops == "직항")
        and (not after or _display_to_hhmm(l.dep_display) >= after)
    ]
    if depart == "all":
        cheapest: dict[tuple[str, str], FlightLeg] = {}
        for l in legs:
            k = (l.airline, l.dep_display)
            if k not in cheapest or l.price_krw < cheapest[k].price_krw:
                cheapest[k] = l
        picked = sorted(cheapest.values(), key=lambda l: _display_to_hhmm(l.dep_display))
        return [(f"{l.airline} {_display_to_hhmm(l.dep_display)}", l) for l in picked]
    times = depart if isinstance(depart, list) else [depart]
    return [
        (t, min((l for l in legs if l.dep_display == _hhmm_to_display(t)), key=lambda l: l.price_krw, default=None))
        for t in times
    ]


def _price_delta(price: int, prev_price: int | None) -> str:
    if prev_price is None:
        return " (신규)"
    if price > prev_price:
        return f" ▲{price - prev_price:,}"
    if price < prev_price:
        return f" ▼{prev_price - price:,}"
    return " ="


def check_watch_trips(page, conn, cfg) -> tuple[list[str], list[str]]:
    """config의 watch_trips(항공사·시각까지 정해진 특정 편)를 조회한다.

    - return이 있으면 왕복: 지정한 가는편(또는 "all"이면 가는편마다) 카드를 클릭한
      뒤, 오는편 화면에서 지정 시각(또는 전부)의 편마다 왕복 총액을 읽는다. 오는편
      카드의 가격은 "이 가는편 + 이 오는편" 왕복 총액이라 조합마다 별도 항목(키)이다.
    - direct_only: true면 가는편·오는편 모두 직항만 본다.
    - return이 없으면 편도: outbound에 맞는 편마다 편도 가격을 기록한다.
    가는편 날짜가 지난 여정은 건너뛴다.

    돌려주는 값: (알림 메시지 목록, 현황 요약에 붙일 줄 목록)
    """
    trips = [t for t in cfg.get("watch_trips") or [] if t["outbound"]["date"] >= time.strftime("%Y-%m-%d")]
    if not trips:
        return [], []

    alert_msgs: list[str] = []
    digest_lines: list[str] = ["", "🎫 지정 항공편"]

    def record(name, key, line_head, dep_date, ret_date, leg, price, budget, ceiling, extra):
        prev = conn.execute(
            "SELECT price_krw FROM checks WHERE destination=? AND depart_date=? AND return_date=? "
            "ORDER BY id DESC LIMIT 1",
            (key, dep_date, ret_date),
        ).fetchone()
        log.info("지정 항공편 %s: ₩%s", line_head, f"{price:,}")
        alert = check_and_record(
            conn, key, name, dep_date, ret_date, FlightLeg(**{**leg.__dict__, "price_krw": price}), budget, ceiling,
        )
        if alert:
            alert_msgs.append(alert + extra)
        mark = "💰" if budget and price <= budget else "·"
        digest_lines.append(f"{mark} {line_head} ₩{price:,}{_price_delta(price, prev[0] if prev else None)}")

    for trip in trips:
        name, ob, rt = trip["name"], trip["outbound"], trip.get("return")
        dep_date = ob["date"]
        # 예산을 null로 두면 💰 예산 알림 없이 📉/🎯(하락) 알림만 받는다.
        budget = trip.get("budget_krw") or 0
        ceiling = trip.get("max_price_krw")
        direct = bool(trip.get("direct_only"))

        if rt is None:
            url = build_tfs_url([(dep_date, ob["from"], ob["to"])], TRIP_ONE_WAY)
            found = fetch_outbound_legs(page, url, cfg["render_wait_ms"])
            for label, leg in _pick_legs(found, ob, direct) or [("", None)]:
                line_head = f"{name} {dep_date[5:]} {label}".rstrip()
                if leg is None:
                    why = "검색 결과에 없음" if found else "조회 실패"
                    log.info("지정 항공편 건너뜀 (%s): %s", why, line_head)
                    digest_lines.append(f"  {line_head}: {why}")
                    continue
                # 편도는 return_date 자리에 출발일을 그대로 넣는다(컬럼이 NOT NULL).
                record(name, f"WT:{name}|{label}", line_head, dep_date, dep_date, leg, leg.price_krw,
                       budget, ceiling, f"\n[편도] {leg.dep_airport} {leg.dep_display} 출발 · {leg.airline}")
            time.sleep(cfg["request_delay_seconds"])
            continue

        ret_date = rt["date"]
        url = build_tfs_url([(dep_date, ob["from"], ob["to"]), (ret_date, rt["from"], rt["to"])], TRIP_ROUND)
        found = fetch_outbound_legs(page, url, cfg["render_wait_ms"])
        outbounds = _pick_legs(found, ob, direct)
        if not outbounds or all(leg is None for _, leg in outbounds):
            head = f"{name} {dep_date[5:]} {ob.get('depart', 'all')}"
            why = "가는편이 검색 결과에 없음" if found else "조회 실패"
            log.info("지정 항공편 건너뜀 (%s): %s", why, head)
            digest_lines.append(f"  {head}: {why}")
            continue

        for i, (ob_label, outbound) in enumerate(outbounds):
            head = f"{name} {dep_date[5:]} {ob_label}"
            if outbound is None:
                log.info("지정 항공편 건너뜀 (가는편이 검색 결과에 없음): %s", head)
                digest_lines.append(f"  {head}: 가는편이 검색 결과에 없음")
                continue
            # 가는편을 하나 고르면 페이지가 그 편의 오는편 목록으로 바뀌므로, 두 번째
            # 가는편부터는 검색 결과를 다시 불러와 같은 편(항공사·시각) 카드를 찾는다.
            if i > 0:
                time.sleep(cfg["request_delay_seconds"])
                reloaded = fetch_outbound_legs(page, url, cfg["render_wait_ms"])
                outbound = next(
                    (l for l in reloaded if l.airline == outbound.airline and l.dep_display == outbound.dep_display),
                    None,
                )
                if outbound is None:
                    log.info("지정 항공편 건너뜀 (다시 불러온 결과에 가는편 없음): %s", head)
                    digest_lines.append(f"  {head}: 조회 실패")
                    continue

            # 오는편(출발공항이 가는편과 다른 카드)이 뜰 때까지 기다린다.
            returns: list[FlightLeg] = []
            try:
                page.locator(f'[aria-label="{outbound.raw_label}"]').first.click(force=True, timeout=10000)
                deadline = time.monotonic() + cfg["render_wait_ms"] / 1000
                while time.monotonic() < deadline and not returns:
                    page.wait_for_timeout(1000)
                    returns = [l for l in _extract_legs(page) if l.dep_airport != outbound.dep_airport]
                if returns:
                    page.wait_for_timeout(1500)
                    returns = [l for l in _extract_legs(page) if l.dep_airport != outbound.dep_airport]
            except Exception as exc:  # noqa: BLE001
                log.warning("지정 항공편 가는편 클릭 실패: %s (%s)", head, exc)

            for ret_label, ret_leg in _pick_legs(returns, rt, direct) or [("", None)]:
                line_head = f"{head} / {ret_date[5:]} {ret_label}".rstrip()
                if ret_leg is None:
                    why = "오는편이 검색 결과에 없음" if returns else "오는편 조회 실패"
                    log.info("지정 항공편 건너뜀 (%s): %s", why, line_head)
                    digest_lines.append(f"  {line_head}: {why}")
                    continue
                # 왕복 총액은 오는편 카드 쪽 값이고, 나머지 표시 정보는 가는편 기준으로 남긴다.
                record(name, f"WT:{name}|{ob_label}|{ret_label}", line_head, dep_date, ret_date, outbound,
                       ret_leg.price_krw, budget, ceiling,
                       f"\n[오는편] {ret_leg.dep_airport} {ret_leg.dep_display} 출발 · {ret_leg.airline} ({ret_date})")

        time.sleep(cfg["request_delay_seconds"])

    return alert_msgs, digest_lines


#: 텔레그램 전송 재시도 간격(초). 조회를 13분이나 돌려놓고 마지막 전송 한 번이
#: 실패했다고 결과를 통째로 버리면 안 된다 — 실제로 이 PC의 유선 연결이
#: 간헐적으로 끊겨(DNS 조회 실패) 전체 실행이 통째로 날아간 적이 있다.
#: 끊김이 대개 수십 초 안에 회복되므로 간격을 점점 늘려가며 다시 시도한다.
SEND_RETRY_DELAYS_SECONDS = (5, 15, 45)


def send_with_retry(notifier, text: str) -> bool:
    """텔레그램으로 보내되, 일시적인 네트워크 오류면 몇 번 다시 시도한다.

    끝내 실패하면 예외를 올리지 않고 내용을 로그에 남긴다 — 여기서 죽으면
    이미 DB에 저장까지 끝낸 조회 결과가 로그에도 안 남아 통째로 사라진다.
    """
    for attempt in range(1, len(SEND_RETRY_DELAYS_SECONDS) + 2):
        try:
            notifier.send_message(text, parse_mode="")
            if attempt > 1:
                log.info("텔레그램 전송 성공 (시도 %d회째)", attempt)
            return True
        except Exception as exc:  # noqa: BLE001 - 어떤 실패든 재시도 대상
            if attempt > len(SEND_RETRY_DELAYS_SECONDS):
                log.error("텔레그램 전송 최종 실패: %s\n보내려던 내용:\n%s", exc, text)
                return False
            delay = SEND_RETRY_DELAYS_SECONDS[attempt - 1]
            log.warning("텔레그램 전송 실패(%d회째), %d초 후 재시도: %s", attempt, delay, exc)
            time.sleep(delay)
    return False


def page_alive(page) -> bool:
    """페이지(및 그 브라우저)가 아직 살아 있는지 확인한다."""
    try:
        page.evaluate("1")
        return True
    except Exception:  # noqa: BLE001
        return False


def restart_browser(p, browser):
    """죽은 브라우저를 정리하고 새로 띄운다. (browser, page)를 돌려준다."""
    close_quietly(browser)
    new_browser = launch_browser(p)
    return new_browser, new_browser.new_page()


def close_quietly(browser) -> None:
    """브라우저를 닫되, 이미 죽어 있으면 조용히 넘어간다.

    브라우저가 크래시한 상태에서 close()를 부르면 예외가 나는데, 그걸 그대로
    두면 조회를 다 끝내고도 알림/현황을 못 보내고 죽는다 (실제로 그랬음).
    """
    try:
        browser.close()
    except Exception as exc:  # noqa: BLE001
        log.warning("브라우저 종료 중 오류(무시): %s", exc)


def build_digest(
    conn: sqlite3.Connection,
    run_results: list[tuple[str, str, str, str, int, int, int | None]],
    top_n: int | None,
) -> str:
    """이번 조회 결과 현황 요약을 만든다 (가격 변동이 없어도 매번 보내는 용도).

    직전 조회가와 비교한 등락(▲▼)을 같이 보여줘, 값이 그대로인지 움직이는
    중인지 한눈에 알 수 있게 한다.

    run_results: (경유지 키, display, 출발일, 도착일, 가격, 예산, 상한가). 지역마다
    가격대가 크게 달라 예산/상한가를 행마다 따로 들고 있다. 같은 공항 조합에 날짜
    조합이 여러 개 있을 수 있어 이전가 조회 때 도착일도 같이 걸러야 서로 다른
    조합끼리 안 섞인다.
    """
    if not run_results:
        return "✈️ 이번 조회에서 가격을 가져오지 못했습니다."

    rows = []
    for dest_key, display, dep_date, ret_date, price, budget, ceiling in run_results:
        prev = conn.execute(
            "SELECT price_krw FROM checks WHERE destination=? AND depart_date=? AND return_date=? "
            "ORDER BY id DESC LIMIT 2",
            (dest_key, dep_date, ret_date),
        ).fetchall()
        # [0]은 방금 기록한 이번 조회가라 [1]이 직전 조회가.
        prev_price = prev[1][0] if len(prev) > 1 else None
        rows.append((price, display, dep_date, ret_date, prev_price, budget, ceiling))

    rows.sort(key=lambda r: r[0])
    within = [r for r in rows if r[0] <= r[5]]

    # 자기 지역 상한가를 넘는 조합은 요약에서 뺀다. 상한이 없거나 그 이하인 것만 남긴다.
    shown = [r for r in rows if r[6] is None or r[0] <= r[6]]
    hidden = len(rows) - len(shown)

    # top_n이 None이거나 0 이하면 (상한가 이하) 전부 표시한다.
    limit = top_n if (top_n and top_n > 0) else len(shown)

    lines = [
        f"✈️ 항공권 현황 ({time.strftime('%m/%d %H:%M')})",
        f"💰 예산 이내: {len(within)}건 / 조회 {len(rows)}건",
        "─" * 20,
    ]
    for price, display, dep_date, ret_date, prev_price, budget, _ceiling in shown[:limit]:
        if prev_price is None:
            delta = " (신규)"
        elif price > prev_price:
            delta = f" ▲{price - prev_price:,}"
        elif price < prev_price:
            delta = f" ▼{prev_price - price:,}"
        else:
            delta = " ="
        mark = "💰" if price <= budget else "·"
        lines.append(f"{mark} {display} {dep_date[5:]}→{ret_date[5:]} ₩{price:,}{delta}")

    if len(shown) > limit:
        lines.append(f"…외 {len(shown) - limit}건")
    if hidden:
        lines.append(f"(지역별 상한가 초과 {hidden}건 숨김)")
    return "\n".join(lines)


def check_and_record(
    conn: sqlite3.Connection,
    destination: str,
    display: str,
    dep_date: str,
    ret_date: str,
    result: FlightLeg,
    budget_krw: int,
    max_price_krw: int | None = None,
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
        "SELECT best_price_krw, alerted_under_budget, alerted_below_typical "
        "FROM best WHERE destination=? AND depart_date=? AND return_date=?",
        (destination, dep_date, ret_date),
    ).fetchone()

    alert = None
    detail = (
        f"{display} ({dep_date} → {ret_date})\n"
        f"[가는편] {result.dep_display} 출발 · {result.airline} · {result.stops} · {result.arr_airport} 도착\n"
        f"₩{result.price_krw:,}"
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
        prev_best, alerted, alerted_typ = row
        now_price = result.price_krw
        under_budget = now_price <= budget_krw
        over_ceiling = max_price_krw is not None and now_price > max_price_krw

        # (A) 사상 최저가 갱신 계열 알림
        if now_price < prev_best:
            drop_pct = (prev_best - now_price) / prev_best
            newly_crossed_budget = under_budget and not alerted
            notable = drop_pct >= NOTABLE_DROP_PCT
            special_deal = drop_pct >= SPECIAL_DEAL_DROP_PCT

            if newly_crossed_budget:
                alert = f"💰 예산 이내로 진입 (이전 최저가 ₩{prev_best:,})\n{detail}"
            elif notable and not over_ceiling:
                alert = f"📉 최저가 갱신 (-{drop_pct:.0%}, 이전 ₩{prev_best:,})\n{detail}"
            elif notable and over_ceiling and special_deal:
                # 평소 상한가를 넘는 노선이라도 급락하면 "진짜 특가"로 보고 알린다.
                alert = f"🔥 평소 비싼 노선 특가 (-{drop_pct:.0%}, 이전 ₩{prev_best:,})\n{detail}"

        # (B) 사상 최저가가 아니어도, 최근 조회가의 중앙값보다 확 싸면 특가로 본다.
        #     (이번 조회는 방금 checks에 넣었으므로 OFFSET 1로 제외한다.)
        recent = [
            r[0]
            for r in conn.execute(
                "SELECT price_krw FROM checks "
                "WHERE destination=? AND depart_date=? AND return_date=? AND price_krw IS NOT NULL "
                "ORDER BY id DESC LIMIT ? OFFSET 1",
                (destination, dep_date, ret_date, TYPICAL_LOOKBACK),
            )
        ]
        new_alerted_typ = alerted_typ
        if len(recent) >= TYPICAL_MIN_SAMPLES:
            typical = statistics.median(recent)
            below_pct = (typical - now_price) / typical
            below_typical = below_pct >= BELOW_TYPICAL_PCT
            # 저점이 이어지는 동안 매시간 반복 알림하지 않도록 플래그로 억제하고,
            # 정상가로 돌아오면 플래그를 풀어 다음 저점 때 다시 알린다.
            new_alerted_typ = 1 if below_typical else 0
            if below_typical and not alerted_typ and alert is None:
                alert = (
                    f"🎯 평소보다 저렴 (-{below_pct:.0%}, 최근 중앙값 ₩{round(typical):,})\n{detail}"
                )

        best_price = now_price if now_price < prev_best else prev_best
        conn.execute(
            "UPDATE best SET best_price_krw=?, alerted_under_budget=?, alerted_below_typical=? "
            "WHERE destination=? AND depart_date=? AND return_date=?",
            (best_price, int(alerted or under_budget), int(new_alerted_typ),
             destination, dep_date, ret_date),
        )

    conn.commit()
    return alert


def main() -> None:
    """진입점. 같은 스크립트가 이미 실행 중이면 조용히 건너뛴다.

    작업 스케줄러의 IgnoreNew 설정을 이미 켜뒀지만, 실측으로 프로세스 2개가
    동시에 뜬 걸 확인해서 스크립트 자체에도 락을 건다 (자세한 배경은
    lockfile.py 참고).
    """
    # 1회 조회는 길어야 40분 안팎. 90분 넘게 락이 잡혀 있으면 절전 등으로 멈춘
    # 실행이므로 회수한다 (작업 스케줄러 실행 시간 제한 2시간보다 짧게 잡음).
    with single_instance(
        HERE / "state" / ".track_prices.lock",
        label="track_prices",
        stale_after=timedelta(minutes=90),
    ) as acquired:
        if not acquired:
            return
        _run()


def _run() -> None:
    cfg = load_config()
    db_path = HERE / cfg["state_db"]
    conn = init_db(db_path)

    alerts: list[str] = []
    # (경유지 키, display, 출발일, 도착일, 가격, 그 지역 예산, 그 지역 상한가)
    run_results: list[tuple[str, str, str, str, int, int, int | None]] = []
    checked = 0
    skipped = 0
    timed_out = False
    regions_skipped = False
    started = time.monotonic()
    max_minutes = cfg.get("max_run_minutes")
    run_limit = max_minutes * 60 if max_minutes else None

    with sync_playwright() as p:
        browser = launch_browser(p)
        page = browser.new_page()

        # 지정 항공편은 조회 1~2회뿐이라 시간 제한에 밀리지 않게 맨 먼저 돈다.
        try:
            wt_alerts, wt_lines = check_watch_trips(page, conn, cfg)
        except Exception as exc:  # noqa: BLE001 - 여기서 실패해도 지역 조회는 계속한다
            log.warning("지정 항공편 조회 중 오류(건너뜀): %s", exc)
            wt_alerts, wt_lines = [], []
        alerts.extend(wt_alerts)
        if not page_alive(page):
            log.warning("브라우저가 죽어 재시작합니다 (지정 항공편 이후)")
            browser, page = restart_browser(p, browser)

        # 지역(region) 안에서만 (들어가는 공항 x 나오는 공항)을 조합한다 — 1월 초에
        # 들어가 2주 안에 다닐 수 있는 범위라서, 우루무치로 들어갔다가 상하이에서
        # 나오는 식의 비현실적인 조합은 애초에 안 만든다. 같은 공항으로 들고 나는
        # 조합(대각선)도 포함된다. 자세한 건 config_io.iter_combos 참고.
        combos = list(iter_combos(cfg))
        # 클라우드(GitHub Actions)는 비공개 저장소 무료 사용량(월 2,000분)이 빠듯해서,
        # 지역 조회(1회 20분 남짓)는 UTC 0시·12시 슬롯 실행에서만 한다. 6시·18시 슬롯은
        # 지정 항공편만 보고 끝낸다. cron이 30분~1시간씩 늦게 도는 걸 감안해 6시간
        # 단위 슬롯으로 판정한다. PC 실행은 해당 없음.
        if (
            os.environ.get("GITHUB_ACTIONS") == "true"
            and cfg.get("cloud_regions_every_12h", True)
            and (time.gmtime().tm_hour // 6) % 2 == 1
        ):
            log.info("클라우드 6시·18시(UTC) 슬롯 — 지역 조회 생략, 지정 항공편만 조회")
            combos = []
            regions_skipped = True
        # 시간 제한(max_run_minutes)에 걸려 중간에 끊기는 날에도 뒤쪽 지역이 매번
        # 굶지 않도록, 시작 위치를 시간마다 돌려가며(로테이션) 조회한다.
        if combos:
            shift = int(time.time() // 3600) % len(combos)
            combos = combos[shift:] + combos[:shift]
        log.info(
            "이번 실행: 공항 조합 %d개 x 날짜 %d개 = %d회 조회 예정",
            len(combos), len(cfg["date_options"]), len(combos) * len(cfg["date_options"]),
        )
        for region, cin, cout in combos:
            region_name = region["name"]
            budget, ceiling = region_thresholds(cfg, region)
            key = cin["code"] if cin["code"] == cout["code"] else f'{cin["code"]}-{cout["code"]}'
            label = cin["display"] if cin["code"] == cout["code"] else f'{cin["display"]}→{cout["display"]}'

            for combo in cfg["date_options"]:
                if run_limit and time.monotonic() - started > run_limit:
                    timed_out = True
                    break
                dep_date, ret_date = combo["depart"], combo["return"]
                legs = [
                    (dep_date, cfg["origin"], cin["code"]),
                    (ret_date, cout["code"], cfg["final_dest"]),
                ]
                url = build_tfs_url(legs, TRIP_MULTI_CITY)
                found = fetch_outbound_legs(page, url, cfg["render_wait_ms"])

                # 브라우저/페이지가 죽으면(메모리 부족 등으로 "Page crashed",
                # "browser has been closed") 이후 모든 조회가 줄줄이 실패한다.
                # 한 번 죽었다고 남은 조합을 전부 날리지 않도록 되살린다.
                if not found and not page_alive(page):
                    log.warning("브라우저가 죽어 재시작합니다")
                    browser, page = restart_browser(p, browser)
                    found = fetch_outbound_legs(page, url, cfg["render_wait_ms"])

                result = pick_cheapest(found, None)
                checked += 1

                if result is None:
                    skipped += 1
                    log.info("가격 확인 실패 — 건너뜀: [%s] %s (%s→%s)", region_name, label, dep_date, ret_date)
                else:
                    log.info(
                        "[%s] %s (%s→%s, %s 출발): ₩%s",
                        region_name, label, dep_date, ret_date, result.dep_display, f"{result.price_krw:,}",
                    )
                    alert = check_and_record(
                        conn, key, label, dep_date, ret_date, result, budget, ceiling,
                    )
                    run_results.append((key, label, dep_date, ret_date, result.price_krw, budget, ceiling))
                    if alert:
                        # 오는편 화면은 고정 대기라, 최대 대기(render_wait_ms)를 그대로 쓰면
                        # 알림마다 너무 오래 걸려서 9초로 묶어둔다.
                        return_leg = fetch_return_leg(page, result, min(cfg["render_wait_ms"], 9000))
                        if return_leg:
                            alert += f"\n[오는편] {return_leg.dep_airport} {return_leg.dep_display} 출발 ({ret_date})"
                        else:
                            alert += "\n[오는편] 시각 확인 실패 — 링크에서 직접 확인 필요"
                        alerts.append(alert)

                time.sleep(cfg["request_delay_seconds"])

            if timed_out:
                break

        close_quietly(browser)

    log.info("완료: %d건 조회, %d건 실패, %d건 알림", checked, skipped, len(alerts))
    if timed_out:
        log.warning("시간 제한(%s분)에 걸려 %d회 조회 후 중단했습니다", max_minutes, checked)

    # 보낼 메시지를 먼저 모은다. 알림(가격 급락/예산 진입)은 조건부지만,
    # hourly_digest가 켜져 있으면 변동이 없어도 매 실행마다 현황을 보낸다.
    messages: list[str] = []
    if alerts:
        header = f"✈️ {cfg['origin']}→{cfg['final_dest']} 항공권 알림 ({len(alerts)}건)\n{'─' * 20}\n"
        messages.append(header + "\n\n".join(alerts))
    if cfg.get("hourly_digest"):
        if regions_skipped:
            # 지역 조회를 안 한 실행은 "가격을 못 가져왔다"가 아니라 지정 항공편만 보낸다.
            digest = f"✈️ 지정 항공편 현황 ({time.strftime('%m/%d %H:%M')}) — 1월 여정은 12시간마다 조회"
        else:
            digest = build_digest(conn, run_results, cfg.get("digest_top_n"))
        if wt_lines:
            digest += "\n" + "\n".join(wt_lines)
        if timed_out:
            digest += f"\n(시간 제한으로 {checked}회 조회 후 중단 — 나머지는 다음 실행에서)"
        messages.append(digest)

    if messages:
        # 공모전 파이프라인(contest_auto)이 쓰는 TELEGRAM_BOT_TOKEN과는 별도로,
        # 이 트래커 전용 봇(FLIGHT_TELEGRAM_BOT_TOKEN)을 쓴다 — 알림이 섞이지
        # 않게 하려고 일부러 분리함.
        token = os.environ.get("FLIGHT_TELEGRAM_BOT_TOKEN", "").strip()
        chat_id = os.environ.get("FLIGHT_TELEGRAM_CHAT_ID", "").strip()
        if not token or not chat_id:
            log.warning("FLIGHT_TELEGRAM_BOT_TOKEN/FLIGHT_TELEGRAM_CHAT_ID가 없어 알림을 보내지 못했습니다")
        else:
            notifier = TelegramNotifier(token=token, chat_id=chat_id)
            for msg in messages:
                send_with_retry(notifier, msg)

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
