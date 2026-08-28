# PC 작업 스케줄러 설정

프로젝트 폴더는 OneDrive로 두 PC에 동기화되지만 **Windows 작업 스케줄러 등록은
PC마다 따로** 저장된다(동기화 안 됨). 번갈아 쓰는 **두 PC 각각에서 한 번씩**
아래를 실행해야 양쪽 모두 자동으로 돈다.

## 설치 (해당 PC에서 1회)

`setup_scheduler.bat` 더블클릭. 또는 터미널에서:

```
powershell -ExecutionPolicy Bypass -File .\setup_scheduler.ps1
```

관리자 권한 불필요. 등록되는 작업(`작업 스케줄러 > \FlightWatch\`):

| 작업 | 실행 파일 | 주기 | 하는 일 |
|------|-----------|------|---------|
| `track_prices` | `run_flight_watch.bat` | 매시간(:09) | Playwright로 항공권 가격 조회·알림 |
| `bot_commands` | `run_bot_commands.bat` | 10분마다 | 텔레그램 명령어 롱폴링(1회 570초 상주) |

- **로그인 상태에서만** 실행 → 그 PC를 실제로 쓰는 동안만 돈다.
- PC가 꺼져 있는 구간은 GitHub Actions 워크플로(6시간 간격)가 메꾼다.
- 두 PC를 동시에 켜두지는 말 것 — 같은 텔레그램 봇 토큰을 두 곳에서 폴링하면
  명령어를 서로 뺏어간다(`.github/workflows/flight_watch_commands.yml` 주석 참고).
- 첫 실행 때 OneDrive 밖(`%LOCALAPPDATA%\flight_watch\venv`)에 파이썬 환경을
  자동으로 만든다(몇 분 소요, `_ensure_venv.bat`).

## 확인

```
Get-ScheduledTask -TaskPath '\FlightWatch\' | Select TaskName, State
Get-ScheduledTaskInfo -TaskPath '\FlightWatch\' -TaskName track_prices
```

로그: `task_run.log`(가격 조회), `bot_run.log`(명령어).

## 즉시 한 번 실행

```
Start-ScheduledTask -TaskPath '\FlightWatch\' -TaskName track_prices
```

## 제거

```
powershell -ExecutionPolicy Bypass -File .\setup_scheduler.ps1 -Remove
```
