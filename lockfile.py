"""같은 스크립트의 중복 인스턴스 실행을 막는 파일 락.

Windows 작업 스케줄러의 `MultipleInstances: IgnoreNew` 설정을 이미 켜뒀지만,
실측으로 track_prices.py 프로세스 2개가 동시에 떠 있는 걸 확인했다(하나는
CPU 0으로 멈춰 있었음 — 아마 sqlite/브라우저 자원을 서로 기다리며 막힌
것으로 보인다). 정확한 원인(스케줄러 vs OS 레벨 경합)을 못 밝혔으므로,
스케줄러 설정만 믿지 않고 스크립트 자체에도 락을 건다 — 이중 방어.

PID 기반: 락 파일에 PID를 적어두고, 다음 실행 때 그 PID가 아직 살아있는지
직접 확인한다. 그냥 "파일이 있으면 실행 중"으로 보면 비정상 종료(크래시,
강제 종료) 후 락 파일만 남아 그 뒤로 영원히 다시 못 도는 사고가 난다.
"""

from __future__ import annotations

import logging
import os
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

log = logging.getLogger(__name__)

#: 락 파일이 이만큼 오래되면 PID가 살아 있어 보여도 "멈춘 실행"으로 보고 회수한다.
#: 노트북을 실행 도중 닫으면(절전) 그 프로세스가 죽지도 정리되지도 않은 채 남아,
#: 다음 실행마다 _pid_alive 가 True 를 돌려주며 영원히 건너뛰는 사고가 났다.
#: 정상 실행이 절대 이보다 오래 걸리지 않는 값으로 호출부가 정한다.
DEFAULT_STALE_AFTER = timedelta(minutes=20)


def _fmt_age(delta: timedelta | None) -> str:
    if delta is None:
        return "?"
    mins = max(0, int(delta.total_seconds() // 60))
    if mins < 120:
        return f"{mins}분"
    return f"{mins // 60}시간 {mins % 60}분"


def _pid_alive(pid: int) -> bool:
    """그 PID의 프로세스가 지금 살아있는지 확인한다."""
    if sys.platform == "win32":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


@contextmanager
def single_instance(
    lock_path: Path,
    label: str = "",
    stale_after: timedelta = DEFAULT_STALE_AFTER,
) -> Iterator[bool]:
    """이미 같은 스크립트가 실행 중이면 `False`를 준다 — 호출부가 조용히 종료해야 한다.

    사용법:
        with single_instance(HERE / "state" / ".track_prices.lock") as acquired:
            if not acquired:
                return
            ... 본 작업 ...

    락을 얻었으면(True) with 블록이 끝날 때(정상 종료든 예외든) 자동으로
    락 파일을 지운다.

    `stale_after`보다 오래된 락은 그 PID가 살아 있어 보여도 회수한다 — 절전으로
    멈춘(죽지도 정리되지도 않은) 이전 실행이 다음 실행들을 영원히 막던 문제 방지.
    정상 실행이 절대 이보다 오래 걸리지 않는 값을 호출부가 넘겨야 한다.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    if lock_path.exists():
        try:
            existing_pid = int(lock_path.read_text().strip())
        except (ValueError, OSError):
            existing_pid = None

        try:
            age: timedelta | None = datetime.now(timezone.utc) - datetime.fromtimestamp(
                lock_path.stat().st_mtime, tz=timezone.utc
            )
        except OSError:
            age = None

        alive = existing_pid is not None and _pid_alive(existing_pid)
        stale_by_age = age is not None and age > stale_after

        if alive and not stale_by_age:
            log.warning(
                "%s이미 실행 중인 인스턴스가 있어 건너뜁니다 (PID %d, 락 %s 경과)",
                f"{label}: " if label else "", existing_pid, _fmt_age(age),
            )
            yield False
            return

        if alive and stale_by_age:
            log.warning(
                "%sPID %d가 살아 있으나 락이 %s째 진행 없음 — 멈춘 실행으로 보고 회수합니다",
                f"{label}: " if label else "", existing_pid, _fmt_age(age),
            )
        else:
            log.info(
                "%s이전 실행이 남긴 락 파일 정리 (PID %s는 이미 종료됨)",
                f"{label}: " if label else "", existing_pid,
            )

    lock_path.write_text(str(os.getpid()))
    try:
        yield True
    finally:
        try:
            lock_path.unlink(missing_ok=True)
        except OSError as exc:  # noqa: BLE001 - 락 해제 실패해도 프로그램은 계속 종료
            log.warning("락 파일 삭제 실패(무시): %s", exc)
