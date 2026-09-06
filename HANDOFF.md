# HANDOFF (2026-08-28 ~ 09-06 세션)

대련 출발 국경절 항공권 가격 트래커. 이 세션에서 작업 스케줄러 세팅 +
알림 로직 개선 + 후보 도시 정리 + 지정 여정 추적(`watch_trips`)을 했다.

---

## 이번 세션에 한 일 (커밋 순)

### `9458cf1` PC별 작업 스케줄러 등록 스크립트
- `setup_scheduler.ps1` / `setup_scheduler.bat` / `SCHEDULER.md` 신규.
- `\FlightWatch\track_prices`(매시간 :09) + `\FlightWatch\bot_commands`(10분마다) 등록.
- 로그인 상태에서만 실행(암호 저장 X), `MultipleInstances=IgnoreNew`,
  `StartWhenAvailable`, 배터리에서도 실행, `RestartCount 2`.
- **두 PC 각각에서 한 번씩 실행해야 함** — 작업 스케줄러 등록은 OneDrive로
  동기화 안 됨. venv는 `%LOCALAPPDATA%\flight_watch\venv`(OneDrive 밖)에 있고
  `_ensure_venv.bat`가 없거나 깨지면 자동 재빌드.
- MDSKYRTX2060에서 venv 자동 재빌드 + 첫 정상 실행 확인.

### `ef446cc` 관심 상한가(`max_price_krw`) + 헤드리스 크롬 안정화
- `config.yaml`: `max_price_krw: 800000`. 이 값 초과 목적지는 **현황 요약과
  "📉 최저가 갱신" 알림에서 제외**(리장·싼야·다낭처럼 늘 100만원대인 곳).
- 예외: 직전 최저가 대비 **25%(`SPECIAL_DEAL_DROP_PCT`) 이상 급락**하면
  상한가 위여도 `🔥 평소 비싼 노선 특가`로 알림.
- `bot_commands.py`: `/maxprice 800000` / `/maxprice none`, `/status`에 표시.
- 같이 묶인 변경(사용자가 미리 작업해둔 것): `BROWSER_LAUNCH_ARGS`
  (`--disable-dev-shm-usage` 등). 조회 중 "Page crashed"로 렌더러가 죽어 그
  시간대 알림이 통째로 날아가던 문제 대응. **적용 후 첫 완전 무크래시 완주 확인.**

### `a402715` "🎯 평소보다 저렴" 알림 (사상 최저가 아니어도 감지)
- 값이 출렁이기만 하고 기록은 안 깨는 경우를 잡으려는 것.
- 현재가가 **최근 조회가(최대 12건, `TYPICAL_LOOKBACK`)의 중앙값보다
  15%(`BELOW_TYPICAL_PCT`) 이상 싸면** 목적지 종류/상한가 무관하게 알림.
- 이력 **6건(`TYPICAL_MIN_SAMPLES`) 미만이면 판정 안 함**.
- `best.alerted_below_typical` 플래그로 저점 지속 중 반복 알림 억제 →
  정상가 복귀 시 플래그 해제 → 다음 저점 때 다시 알림.
- 기존 DB는 `init_db`가 `ALTER TABLE`로 컬럼 자동 추가(마이그레이션).
- 💰/📉/🔥가 이미 이번 조회에서 나갔으면 🎯는 생략.

### `fedd63f` 후보 도시를 중국 주요 도시 28곳으로 정리
- **제외**: 일본(도쿄·오사카·후쿠오카·삿포로), 대만·홍콩·마카오, 동남아 10곳.
- 기존 중국 15곳 + 신규: 베이징·톈진·지난·선양·창춘·하얼빈·난징·정저우·
  하이커우·란저우·우루무치.
- `bot_commands.py`의 `CITY_CATALOG`에는 이 도시들이 이미 다 있음(수정 불필요).

### `f89e0cd` 현황 요약 "…외 N건" 자름 제거
- `config.yaml`: `digest_top_n: null` → 상한가 이하는 **전부 표시**.
- `build_digest`의 `top_n`이 `int | None`. None이거나 0 이하면 무제한.
  숫자를 넣으면 예전처럼 그만큼만.
- `(상한 ₩800,000 초과 N건 숨김)` 줄은 그대로 유지.

### `bd8a449` + 후속 `watch_trips` — 지정한 특정 여정 추적
- 대련 국경절 검색과 **완전히 별개로**, `config.yaml`의 `watch_trips`에 출발지·
  구간·날짜를 명시한 여정을 매 실행마다 그대로 조회한다. 오픈조(들어가는 공항
  ≠ 나오는 공항) 지원 — `build_tfs_url` + `TRIP_MULTI_CITY`. `from`/`to`는 IATA.
- `check_watch_trips()` 신규. `check_and_record`를 그대로 써서 알림 종류 동일
  (💰 📉 🔥 🎯). 여정별 `budget_krw` / `max_price_krw`.
- `match: {airline, depart}` 지정 시 그 **항공사 + 가는편 출발시각**에 맞는 편만
  추적(없으면 그 구간 최저가). **파싱되는 건 가는편 카드뿐이라 귀국편 시각은
  고정 불가** — `depart`는 가는편 출발시각, 귀국편은 구글이 묶어주는 대로.
  지정 편이 그날 결과에 없으면 현황에 "지정 항공편 없음", 다시 뜨면 자동 재추적.
- 현황 요약 끝에 `✈️ 지정 여정` 섹션(직전가 대비 ▲▼), 알림은
  `✈️ 지정 여정 알림` 별도 메시지.
- 현재 항목: 인천-리스본(2027-04-09) / 바르셀로나-인천(2027-04-17) 오픈조,
  **카타르항공 오후 6:30** / **에티하드항공 오후 5:50**, 각 예산 135만.
  실측: 카타르 편은 조회됨(₩1,371,300). **에티하드 오픈조 조합은 현재 구글
  다구간 결과에 안 나옴** → "지정 항공편 없음" 상태, 다시 나타나면 자동 추적.

> 다른 세션/다른 PC가 올린 커밋: `00f49cc` `dd56485` `374b1d4`(스케줄러
> `run_hidden.vbs` 래퍼로 빈 cmd 창 숨김 + BOM 수정), `4ae6e9e`(나이 기준
> 락 회수 — 절전 후 스킵 문제), `1c7342b`(오픈조 비교). 스케줄러를 예전
> 방식으로 이미 등록해 뒀다면 `setup_scheduler.ps1`을 **다시 실행**해야 창
> 숨김 방식으로 바뀜.

---

## 내린 결정

| 주제 | 결정 | 이유 |
|------|------|------|
| 스케줄러 실행 조건 | **로그인 시에만** 실행(암호 저장 안 함) | PC 꺼진 구간은 GitHub Actions 6시간 워크플로가 메꿈 — 의도된 분담 |
| 두 PC 모두 `track_prices` 실행 | **현행 유지** (옵션 1 = 한 PC만 조회, 채택 안 함) | 충돌본이 실수로 커밋돼도 되돌리기 쉬운 수준. 구조 변경 부담이 더 큼 |
| `.gitignore`에 충돌본 패턴 추가 | **안 함** | 크게 안 꼬임. 커밋 시 `git add .` 대신 파일 콕 집어서 add 하면 됨 |
| `max_price_krw` | 800,000원, 25% 급락 시 예외 알림 | 늘 비싼 노선이 목록만 채우는 것 방지 |
| `BELOW_TYPICAL_PCT` | 15%, 최소 6샘플 | 노이즈 억제 |
| `digest_top_n` | null(전부 표시) | 사용자가 "…외 N건" 잘림 싫다고 함 |
| `watch_trips` 리스본 여정 | 카타르·에티하드 **각 항공사+가는편 시각**만 추적 | 사용자가 스크린샷의 그 두 편만 원함. 귀국편 시각은 파서 한계로 고정 불가 |
| 리스본 여정 연도 | **2027** (4/9, 4/17) | 2026년 4월은 이미 지남 |

---

## 현재 상태 · 주의사항

- **`main`이 `origin/main`과 동기화됨** (세션 종료 시점).
- `.git` 폴더가 OneDrive로 동기화됨 → 로컬이 origin과 갈라질 수 있음.
  push 전 `git status -sb`로 ahead/behind 확인, 갈라졌으면 `git pull --rebase`.
  이번 세션에서 한 번 그랬음(rebase로 정리).
- `git pull --rebase --autostash`가 **`track_prices` 실행 중이면** 실패함
  (`unable to unlink state/flight_prices.sqlite` — 파일 잠김). 실행 끝나길
  기다리거나 sqlite를 먼저 커밋하고 rebase.
- **중복 프로세스**: `track_prices.py` / `bot_commands.py`가 실행되면 venv
  파이썬 외에 `Python311\python.exe` 자식이 하나 더 뜬다(둘 다 락 잡음).
  `lockfile.py` + `IgnoreNew` + `4ae6e9e`(나이 기준 락 회수)가 방어책.
  근본 원인은 아직 못 밝힘.
- `state/`에 OneDrive 충돌본(`flight_prices-DESKTOP-*.sqlite`,
  `flight_prices-MDskyrtx2060.sqlite`)과 오래된 락 파일이 쌓임 — 두 PC가
  모두 같은 sqlite에 써서 생기는 것. 그냥 두기로 함(위 표 참고).
- 가격 이력은 **PC 쪽만 소유**. GitHub Actions(6시간) 실행은 알림만 보내고
  DB에 안 씀. PC가 오래 꺼져 있으면 기준 DB가 안 갱신돼 같은 알림이
  반복될 수 있음(감수).

---

## 레퍼런스

### 알림 종류 (`track_prices.py` `check_and_record`)
| 알림 | 조건 |
|------|------|
| 🆕 예산 이내로 발견 | 처음 조회한 목적지가 예산(₩500,000) 이하 |
| 💰 예산 이내로 진입 | 새로 예산 이하로 하락 |
| 📉 최저가 갱신 | 사상 최저가 + 직전보다 10%↑ 하락 + 가격 ≤ 상한가(₩800,000) |
| 🔥 평소 비싼 노선 특가 | 사상 최저가 + 직전보다 25%↑ 하락 + 가격 > 상한가 |
| 🎯 평소보다 저렴 | 최근 중앙값보다 15%↑ 싸다 (최저가 갱신 아니어도) |

지정 여정(`watch_trips`)도 같은 알림을 쓰되 여정별 `budget_krw`/`max_price_krw`
기준. 현황 요약 끝 `✈️ 지정 여정` 섹션 + `✈️ 지정 여정 알림` 별도 메시지.

### 텔레그램 명령어 (`bot_commands.py`)
`/status` `/price` `/budget 400000` `/maxprice 800000`(또는 `none`)
`/dates 2026-09-30 2026-10-01 2026-10-07` `/minhour 2026-09-30 17`
`/destinations 상하이 시안 …` `/adddest 하얼빈 Harbin` `/help`
→ PC가 켜져 있고 로그인돼 있을 때만 응답(클라우드 명령어 폴링은 비활성).

### 주요 상수 (`track_prices.py` 상단)
`NOTABLE_DROP_PCT=0.10` `SPECIAL_DEAL_DROP_PCT=0.25`
`BELOW_TYPICAL_PCT=0.15` `TYPICAL_LOOKBACK=12` `TYPICAL_MIN_SAMPLES=6`
`BROWSER_LAUNCH_ARGS`(헤드리스 크롬 안정화)

### 실행/설정 파일
- `setup_scheduler.ps1` / `.bat` — 각 PC에서 1회. `-Remove`로 해제.
- `SCHEDULER.md` — 설치/확인/제거 방법.
- `config.yaml` — 사람이 편집. `state/overrides.json`(봇 명령어 반영분)이 덮어씀.
- 로그: `task_run.log`(가격 조회), `bot_run.log`(명령어).
