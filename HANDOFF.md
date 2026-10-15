# HANDOFF (2026-08-28 ~ 10-14, 여러 세션 누적)

## 2026-10-14 — 지정 항공편(watch_trips) 추가 (오픈조 탐색은 그대로)

- 대련→인천 10/30 중국남방항공 19:10 + 인천→대련 11/2 중국남방항공
  17:30 / 22:40 왕복을 편 단위로 추적. `config.yaml`의 `watch_trips:`.
- `track_prices.check_watch_trips`: 왕복 tfs 검색 → 지정 가는편 카드
  클릭 → 오는편 화면에서 지정 시각마다 왕복 총액을 읽어 각각 별도 키
  (`WT:<name>|<가는편시각>|<오는편시각>`)로 `check_and_record`. 실행 맨 앞에서
  돌아(~10초) 시간 제한에 안 밀림. 현황 요약 끝에 "🎫 지정 항공편"으로 붙음.
  가는편 날짜가 지나면 자동으로 건너뜀. `/price`는 지역 키만 보므로 안 섞임.
- 첫 조회(10/14): 두 조합 모두 ₩311,204. 예산 ₩280,000.
- 10/15 추가: 대한항공 10/30 14:25 가는편 + 오는편 전부(`depart: all`),
  11/2 인천→대련 편도 전부(`return` 없으면 편도, `TRIP_ONE_WAY=2`). 편도 카드
  aria-label은 "최저가는 N 대한민국 원입니다"라 `_LEG_RE`/JS 필터에 추가함.
  `budget_krw: null`이면 💰 없이 📉/🎯만. 편도는 DB return_date에 출발일을 넣음.
- 10/15 같은 날 다시 정리: 위 세 항목을 **"대련⇄인천 직항" 하나**(가는편 all x
  오는편 all, `direct_only: true`)로 합침. 가는편마다 검색을 다시 불러와 클릭
  → 1회 약 50초, 13조합. 키는 `WT:대련⇄인천 직항|<항공사 HH:MM>|<항공사 HH:MM>`.
  예전 키(남방/대한항공 개별 항목) 이력은 DB에 남아 있지만 더는 안 쓰임.
- **클라우드 사용량 대응**: 10/1~14 Actions 39회 869분(월 ~1,900분 페이스,
  비공개 저장소 무료 2,000분에 빠듯). 워크플로 cron은 여전히 6시간(권한 문제로
  10/14 반영 — 이제 12시간 cron이라 아래 생략은 사실상 안 걸림. cron을 다시 6시간으로 당기면 작동)이라, 코드에서 `GITHUB_ACTIONS`일 때 UTC 6~11시·18~23시 슬롯 실행은
  지역 조회를 건너뛰고 지정 항공편만 본다(`cloud_regions_every_12h`, 기본 true).
  PC 실행은 영향 없음.

## 🔴 2026-09-30 — 트래킹 대상 완전히 교체 (제일 중요, 최신)

**"국경절은 그만 하자"** — 대련↔중국 국내 도시 왕복(국경절 여행) 추적을
그만두고, 완전히 새 여정을 추적하도록 트래커를 다시 짰다:

- **대련(DLC) 출발 (2027-01-09~11 중), 인천(ICN) 도착 (2027-01-22~24 중)**
  오픈조. 1월 초 어느 지역으로 들어가 약 2주 동안 그 지역 안에서 다니다가
  같은 지역의 (다른) 공항에서 인천으로 나온다. 들어가는 공항과 나오는
  공항이 달라도 된다.
- **공항 조합은 "지역(regions)" 안에서만 만든다.** 처음엔 후보 도시끼리
  전부 x 전부(N^2)로 조합했는데, "우루무치로 들어갔다가 상하이에서
  나오는" 식의 비현실적인 조합이 섞여서 사용자가 지적 → 지역 단위로
  재설계. `config.yaml`의 `regions:` 리스트에 지역별로 `in`(들어가는 공항)
  / `out`(나오는 공항) 또는 `airports`(둘 다)를 적고, 조합은 지역 안에서만.
  `enabled: false`로 꺼둘 수 있음. 조합 생성은 `config_io.iter_combos(cfg)`
  (같은 in/out 쌍이 두 지역에서 겹치면 한 번만), 공항코드→표시이름은
  `config_io.airport_names(cfg)`.
- 켜둔 지역(사용자가 든 예): **광시·구이저우**(구이린·난닝·구이양),
  **푸젠**(샤먼·푸저우·우이산), **카스~중앙아시아**(in: 카스·우루무치 /
  out: 타슈켄트·사마르칸트·비슈케크·알마티), **스리랑카**(콜롬보),
  **코카서스**(트빌리시·예레반·바쿠). 꺼둔 후보: 윈난, 베트남,
  태국·라오스, 튀르키예. 우루무치는 "예시"였을 뿐 특별한 게 아님.
- 조합 수 = 지역별 (in 수 x out 수)의 합 x `date_options` 수. 지금 켜둔 건
  36조합 x 2날짜 = **72회/실행**(약 20분). 지역을 켤수록 늘어나니 GitHub
  Actions 타임아웃(40분)과 무료 사용량(월 2,000분)을 같이 볼 것.
- **클라우드 실행 주기 6시간 → 12시간 변경은 아직 미반영(권한 문제)**:
  `.github/workflows/flight_watch.yml`의 cron을 `13 0,6,12,18`에서
  `13 0,12`로 바꾸고 이름도 새 여정으로 고쳤지만, 이 PC의 GitHub 인증에
  `workflow` scope가 없어 푸시가 거부됨(`refusing to allow an OAuth App to
  create or update workflow`). 로컬 작업 트리에는 수정이 남아 있고
  **커밋 안 됨**. 반영하려면 `gh auth refresh -h github.com -s workflow`
  후 커밋·푸시하거나 GitHub 웹에서 직접 수정. 그 전까지 클라우드는 6시간
  간격 그대로라 조회가 ~72회(20분)로 늘면 월 ~2,400분으로 **무료 한도
  (2,000분)를 넘길 수 있음**.
- `config.yaml`: `origin`/`final_dest`가 IATA 코드(DLC/ICN)로 바뀜.
  `depart_dates`+`return_date`(예전, 단일 목적지 왕복용) 대신
  `date_options`(출발일·도착일 쌍 리스트, 지금 2개)로 교체.
  `destinations`(예전) 자체가 없어지고 `regions`로 대체.
- `track_prices.py`: 자연어 검색(`build_url`) 대신 **tfs 다구간 URL**
  (`build_tfs_url`, 예전에 만들었다 지웠던 걸 되살림)로만 조회 — 오픈조라
  자연어 질의가 안 통함. `_run()`은 `for iter_combos: for date_options:`
  루프. `run_results`는 7튜플(경유지 키, display, 출발일, 도착일, 가격,
  그 지역 예산, 그 지역 상한가)이고 `build_digest(conn, run_results,
  top_n)`가 행마다 자기 예산/상한가로 💰 표시·숨김을 한다. 같은 공항 조합에
  날짜조합이 여러 개라 이전가 조회에 return_date 필터도 넣음.
- **지역별 예산/상한가**: 가격대가 지역마다 크게 달라서(첫 탐색 최저가:
  푸젠 ~₩60만, 광시·구이저우 ~₩65만, 중앙아시아 ~₩135만, 코카서스 ~₩215만,
  스리랑카 ~₩389만) region에 `budget_krw`/`max_price_krw`를 따로 둘 수 있음
  (`config_io.region_thresholds`). 안 적으면 전체 기본값. 지금 값은 첫
  탐색 기준 **초기값이라 사용자가 실제로 살 만한 가격대로 조정해야 함**.
  텔레그램 `/budget` `/maxprice`는 전체 기본값만 바꾼다(지역에 값이 적혀
  있으면 그 지역엔 안 먹음).
- **조회 방식 개선(`fetch_outbound_legs`)**: 고정 9초 대기 → 폴링(최대
  `render_wait_ms`=20초). 구글이 **처음 계산하는 조합은 첫 카드가 뜨기까지
  10초 넘게** 걸려(실측 12초) 예전 방식은 결과가 있어도 "없음"으로 놓쳤다.
  카드가 뜨면 1.5초 더 기다렸다 읽고, 항공편 목록은 떴는데 전부 "가격을
  확인할 수 없습니다"면 4초 뒤 포기(그 조합엔 운임 없음). 이미 계산된
  조합은 1~5초 만에 뜸. 그래도 같은 조합이 실행마다 가격 있음/없음으로
  오락가락할 수 있음(운임 유무 자체가 변동).
- **실행 시간 제한**: `max_run_minutes: 33` — 넘으면 거기서 멈추고 모인
  결과로 알림/현황을 보냄(GitHub Actions 40분에 잘려 아무것도 못 보내는 걸
  방지). 멈추는 날 뒤쪽 지역이 굶지 않게 조합 시작 위치를 시간마다
  로테이션.
- 첫 탐색(2027-01-09→22, 67조합) 결과: 대부분 유효한 운임이 나오지만
  카스/우루무치→사마르칸트·비슈케크, 하노이/다낭 일부 등은 계속 없음.
  코카서스·스리랑카·태국 등 먼 지역은 2~3회 경유라 ₩200만~400만대로 비쌈.
- 경유지 키(DB `destination`): 같은 공항으로 들고 나면 `"CODE"`, 다르면
  `"CIN-COUT"`.
- **실측 확인**: 대칭 조합(DLC→PVG→ICN ₩588,920, DLC→URC→ICN ₩1,016,931)은
  잘 나옴. **비대칭 조합은 0건인 경우가 있음** — 구글 다구간 엔진이 그
  조합에 묶음 운임을 안 주는 것으로 보임(에티하드 사례와 같은 종류).
  코드가 이미 "조회 실패 — 건너뜀"으로 조용히 넘겨서 에러는 안 남.
- `bot_commands.py`: `/dates` `/minhour` `/destinations` `/adddest`
  **명령어 전부 제거**(새 스키마와 구조가 안 맞아 남겨두면 config 깨뜨릴
  위험 — 날짜/경유지는 이제 config.yaml 직접 편집). `CITY_CATALOG`도
  같이 삭제(쓰는 곳 없어짐). `/budget` `/maxprice` `/status` `/price`
  `/help`는 새 스키마에 맞게 고쳐서 계속 동작.
- **`state/flight_prices.sqlite`의 `checks`/`best` 테이블 전부 비움**
  (국경절 이력 14,438건 + best 89건 삭제, VACUUM. 이후 개발 중 정기 작업이 중간
  버전으로 한 번 돌아 40건이 쌓여서 한 번 더 비움 — 예산 기준이 바뀌면 이전
  알림 플래그가 꼬이므로). 예전 트립 완전히
  끝난 거라 `/price`·현황요약에 안 섞이게 클린 슬레이트로 시작.
- `open_jaw`/`watch_trips` 관련 이전 기능은 여전히 없음(9월에 이미
  제거됨, 아래 참고) — tfs 빌더만 이번에 다시 들여왔다.

---

## 2026-08-28 ~ 09-06 세션 (예전 국경절 트래커 — 위 교체로 대부분 무효)

대련 출발 국경절 항공권 가격 트래커였을 때 작업. 스케줄러 세팅 + 알림
로직 개선 + 후보 도시 정리를 했다. (지정 여정/조합 최저가 기능은 넣었다가
사용자 요청으로 다시 제거 — 아래 참고. 이후 09-30에 트립 자체를 통째로
교체하면서 이 절의 destinations/depart_dates 관련 세부사항은 대부분
유효하지 않다 — 스케줄러·알림 엔진(💰📉🔥🎯) 설계는 그대로 유효.)

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

### `bd8a449` `43b5201` `watch_trips` — 추가했다가 **되돌림**
- 인천-리스본/바르셀로나-인천 오픈조를 카타르·에티하드로 추적하는 기능을
  넣었으나, 사용자가 취소 요청 → 관련 코드/설정 전부 제거함(아래 커밋).
- **같이 제거**: `open_jaw`(조합 최저가) 기능 — `check_open_jaw`,
  `check_watch_trips`, `build_tfs_url`/protobuf 헬퍼, `TRIP_*` 상수,
  `import base64`, config의 `open_jaw:` / `watch_trips:` 블록.
- 현재 트래커는 **대련 국경절 목적지 추적 하나만** 한다(가장 단순한 상태).
  다구간/오픈조 URL 조립 코드가 필요하면 git 히스토리 `43b5201` 참고.

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
| `watch_trips` / `open_jaw` | **둘 다 제거** (코드+설정) | 사용자가 리스본 추적·조합 최저가 취소 요청. 트래커를 대련 목적지 추적 하나로 단순화 |

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
