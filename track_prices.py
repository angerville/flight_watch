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
import statistics
import subprocess
import sys
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from notify import TelegramNotifier  # noqa: E402
from config_io import load_config  # noqa: E402
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
    run_results: list[tuple[str, str, str, int]],
    budget_krw: int,
    top_n: int,
    max_price_krw: int | None = None,
) -> str:
    """이번 조회 결과 현황 요약을 만든다 (가격 변동이 없어도 매번 보내는 용도).

    직전 조회가와 비교한 등락(▲▼)을 같이 보여줘, 값이 그대로인지 움직이는
    중인지 한눈에 알 수 있게 한다.
    """
    if not run_results:
        return "✈️ 이번 조회에서 가격을 가져오지 못했습니다."

    rows = []
    for dest_query, display, dep_date, price in run_results:
        prev = conn.execute(
            "SELECT price_krw FROM checks WHERE destination=? AND depart_date=? "
            "ORDER BY id DESC LIMIT 2",
            (dest_query, dep_date),
        ).fetchall()
        # [0]은 방금 기록한 이번 조회가라 [1]이 직전 조회가.
        prev_price = prev[1][0] if len(prev) > 1 else None
        rows.append((price, display, dep_date, prev_price))

    rows.sort(key=lambda r: r[0])
    within = [r for r in rows if r[0] <= budget_krw]

    # 상한가를 넘는 노선(리장·싼야처럼 늘 100만원대인 곳)은 요약에서 뺀다.
    # 상한이 없거나 그 이하인 것만 남긴다.
    shown = [r for r in rows if max_price_krw is None or r[0] <= max_price_krw]
    hidden = len(rows) - len(shown)

    lines = [
        f"✈️ 항공권 현황 ({time.strftime('%m/%d %H:%M')})",
        f"예산 ₩{budget_krw:,} 이내: {len(within)}건 / 조회 {len(rows)}건",
        "─" * 20,
    ]
    for price, display, dep_date, prev_price in shown[:top_n]:
        if prev_price is None:
            delta = " (신규)"
        elif price > prev_price:
            delta = f" ▲{price - prev_price:,}"
        elif price < prev_price:
            delta = f" ▼{prev_price - price:,}"
        else:
            delta = " ="
        mark = "💰" if price <= budget_krw else "·"
        lines.append(f"{mark} {display} {dep_date[5:]} ₩{price:,}{delta}")

    if len(shown) > top_n:
        lines.append(f"…외 {len(shown) - top_n}건")
    if hidden:
        lines.append(f"(상한 ₩{max_price_krw:,} 초과 {hidden}건 숨김)")
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
    with single_instance(HERE / "state" / ".track_prices.lock", label="track_prices") as acquired:
        if not acquired:
            return
        _run()


def _run() -> None:
    cfg = load_config()
    db_path = HERE / cfg["state_db"]
    conn = init_db(db_path)

    alerts: list[str] = []
    run_results: list[tuple[str, str, str, int]] = []  # (query, display, 출발일, 가격)
    checked = 0
    skipped = 0

    with sync_playwright() as p:
        browser = launch_browser(p)
        page = browser.new_page()

        for dest in cfg["destinations"]:
            for dep_entry in cfg["depart_dates"]:
                dep_date = dep_entry["date"]
                min_hour = dep_entry.get("min_departure_hour")

                url = build_url(cfg["origin"], dest["query"], dep_date, cfg["return_date"])
                legs = fetch_outbound_legs(page, url, cfg["render_wait_ms"])

                # 브라우저/페이지가 죽으면(메모리 부족 등으로 "Page crashed",
                # "browser has been closed") 이후 모든 조회가 줄줄이 실패한다.
                # 한 번 죽었다고 남은 목적지를 전부 날리지 않도록 되살린다.
                if not legs and not page_alive(page):
                    log.warning("브라우저가 죽어 재시작합니다")
                    browser, page = restart_browser(p, browser)
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
                        conn, dest["query"], dest["display"], dep_date, cfg["return_date"], result,
                        cfg["budget_krw"], cfg.get("max_price_krw"),
                    )
                    run_results.append((dest["query"], dest["display"], dep_date, result.price_krw))
                    if alert:
                        return_leg = fetch_return_leg(page, result, cfg["render_wait_ms"])
                        if return_leg:
                            alert += f"\n[오는편] {return_leg.dep_airport} {return_leg.dep_display} 출발 ({cfg['return_date']})"
                        else:
                            alert += "\n[오는편] 시각 확인 실패 — 링크에서 직접 확인 필요"
                        alerts.append(alert)

                time.sleep(cfg["request_delay_seconds"])

        close_quietly(browser)

    log.info("완료: %d건 조회, %d건 실패, %d건 알림", checked, skipped, len(alerts))

    # 보낼 메시지를 먼저 모은다. 알림(가격 급락/예산 진입)은 조건부지만,
    # hourly_digest가 켜져 있으면 변동이 없어도 매 실행마다 현황을 보낸다.
    messages: list[str] = []
    if alerts:
        header = f"✈️ 대련 출발 국경절 항공권 알림 ({len(alerts)}건)\n{'─' * 20}\n"
        messages.append(header + "\n\n".join(alerts))
    if cfg.get("hourly_digest"):
        messages.append(
            build_digest(
                conn, run_results, cfg["budget_krw"], cfg.get("digest_top_n", 10),
                cfg.get("max_price_krw"),
            )
        )

    if messages:
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
