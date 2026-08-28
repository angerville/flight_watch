"""Telegram Bot API 래퍼 — 메시지/파일 전송, 인라인 버튼 콜백 수신.

`공모전 자동화` 프로젝트의 `pipeline/notify.py`에서 flight_watch가 실제로 쓰는
`TelegramNotifier` 클래스만 떼어낸 것이다 (2026-08-28, 저장소 분리). 그쪽의
공모전 전용 메시지 포맷터(format_review_message 등)는 이 프로젝트와 무관해서
가져오지 않았다.
"""

from __future__ import annotations

import html
import logging
import time
from dataclasses import dataclass
from pathlib import Path

import requests

log = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org/bot{token}/{method}"

#: 텔레그램 sendMessage 본문 길이 제한
MAX_MESSAGE_CHARS = 4096
#: 봇이 첨부로 보낼 수 있는 파일 크기 상한
DEFAULT_MAX_FILE_MB = 50


@dataclass
class Callback:
    """버튼 클릭 1건 (`callback_data`에 담긴 값)."""

    update_id: int
    callback_id: str
    action: str
    key: str


class TelegramNotifier:
    """텔레그램 봇 래퍼. 전송 실패는 예외로 올려 상위에서 기록하게 한다."""

    def __init__(
        self,
        token: str,
        chat_id: str,
        send_delay: float = 0.7,
        max_file_mb: int = DEFAULT_MAX_FILE_MB,
        timeout: int = 60,
    ) -> None:
        """
        Args:
            token: @BotFather에서 발급한 봇 토큰
            chat_id: 받을 채팅 ID
            send_delay: 연속 전송 사이 딜레이(초) — rate limit 배려
            max_file_mb: 이 크기를 넘는 파일은 전송을 건너뛴다
            timeout: HTTP 타임아웃(초)
        """
        self.token = token
        self.chat_id = chat_id
        self.send_delay = send_delay
        self.max_file_bytes = max_file_mb * 1024 * 1024
        self.max_file_mb = max_file_mb
        self.timeout = timeout
        self.session = requests.Session()

    # ---------------------------------------------------------- 저수준 --

    def _call(self, method: str, *, data: dict | None = None, files: dict | None = None) -> dict:
        """Bot API 호출. 실패 시 RuntimeError."""
        url = API_BASE.format(token=self.token, method=method)
        try:
            resp = self.session.post(url, data=data, files=files, timeout=self.timeout)
            payload = resp.json()
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"텔레그램 {method} 요청 실패: {type(exc).__name__}: {exc}") from exc

        if not payload.get("ok"):
            raise RuntimeError(
                f"텔레그램 {method} 오류: {payload.get('error_code')} "
                f"{payload.get('description', '')}"
            )
        return payload.get("result", {})

    def _pause(self) -> None:
        """연속 전송 사이 딜레이."""
        if self.send_delay > 0:
            time.sleep(self.send_delay)

    # ---------------------------------------------------------- 메시지 --

    def send_message(
        self,
        text: str,
        buttons: list[list[dict]] | None = None,
        parse_mode: str = "HTML",
    ) -> dict:
        """텍스트 메시지를 보낸다. 4096자를 넘으면 여러 개로 쪼갠다.

        Args:
            text: 본문 (parse_mode=HTML이면 미리 이스케이프되어 있어야 함)
            buttons: 인라인 키보드. 마지막 조각에만 붙는다.
            parse_mode: "HTML" | "MarkdownV2" | "" (없음)

        Returns:
            마지막으로 보낸 메시지의 결과 객체
        """
        chunks = _split_message(text, MAX_MESSAGE_CHARS)
        result: dict = {}

        for i, chunk in enumerate(chunks):
            data: dict = {"chat_id": self.chat_id, "text": chunk}
            if parse_mode:
                data["parse_mode"] = parse_mode
                data["link_preview_options"] = '{"is_disabled": true}'
            if buttons and i == len(chunks) - 1:
                data["reply_markup"] = _keyboard_json(buttons)

            try:
                result = self._call("sendMessage", data=data)
            except RuntimeError:
                if not parse_mode:
                    raise
                # 마크업 파싱 오류로 실패하는 경우가 흔하므로 평문으로 한 번 더 시도
                log.warning("parse_mode=%s 전송 실패 — 평문으로 재시도", parse_mode)
                data.pop("parse_mode", None)
                data["text"] = _strip_html(chunk)
                result = self._call("sendMessage", data=data)

            if i < len(chunks) - 1:
                self._pause()

        return result

    # ------------------------------------------------------------ 파일 --

    def send_document(self, path: str | Path, caption: str = "") -> bool:
        """파일을 첨부로 보낸다.

        용량 초과·손상·미지원 형식 등으로 실패하면 그 사유를 텍스트로 안내하고
        `False`를 돌려준다. 파이프라인은 계속 진행된다.
        """
        p = Path(path)

        if not p.exists():
            self._notify_file_problem(p, "파일을 찾을 수 없습니다")
            return False

        size = p.stat().st_size
        if size == 0:
            self._notify_file_problem(p, "파일 크기가 0입니다")
            return False

        if size > self.max_file_bytes:
            self.send_message(
                f"⚠️ 첨부파일 <b>{html.escape(p.name)}</b>은 용량 초과"
                f"({size / 1024 / 1024:.1f}MB &gt; {self.max_file_mb}MB)로 전송되지 않았습니다.\n"
                f"로컬 경로: <code>{html.escape(str(p))}</code>"
            )
            return False

        try:
            with p.open("rb") as fh:
                data = {"chat_id": self.chat_id}
                if caption:
                    data["caption"] = caption[:1024]
                    data["parse_mode"] = "HTML"
                self._call("sendDocument", data=data, files={"document": (p.name, fh)})
        except Exception as exc:  # noqa: BLE001
            self._notify_file_problem(p, str(exc))
            return False

        log.info("텔레그램 첨부 전송: %s (%.1f KB)", p.name, size / 1024)
        return True

    def send_photo(self, path: str | Path, caption: str = "") -> bool:
        """이미지를 보낸다. 실패 시 사유를 안내하고 False."""
        p = Path(path)
        if not p.exists() or p.stat().st_size == 0:
            self._notify_file_problem(p, "이미지 파일이 없거나 비어 있습니다")
            return False

        try:
            with p.open("rb") as fh:
                data = {"chat_id": self.chat_id}
                if caption:
                    data["caption"] = caption[:1024]
                    data["parse_mode"] = "HTML"
                self._call("sendPhoto", data=data, files={"photo": (p.name, fh)})
        except Exception as exc:  # noqa: BLE001
            # 이미지로 실패하면 문서로 한 번 더 시도 (형식 문제일 수 있음)
            log.warning("sendPhoto 실패, sendDocument로 재시도: %s", exc)
            return self.send_document(p, caption)

        return True

    def _notify_file_problem(self, path: Path, reason: str) -> None:
        """파일 전송 실패 사유를 텍스트로 안내한다 (베스트 에포트)."""
        try:
            self.send_message(
                f"⚠️ 첨부파일 <b>{html.escape(path.name)}</b>을 전송하지 못했습니다.\n"
                f"사유: {html.escape(reason[:300])}\n"
                f"로컬 경로: <code>{html.escape(str(path))}</code>"
            )
        except Exception as exc:  # noqa: BLE001 - 안내조차 실패하면 로그만
            log.error("파일 실패 안내 전송마저 실패: %s", exc)

    # ------------------------------------------------- 인라인 버튼 콜백 --

    def fetch_callbacks(self, offset: int = 0) -> tuple[list[Callback], int]:
        """지난 실행 이후 쌓인 버튼 클릭 이벤트를 모두 가져온다.

        GitHub Actions는 상시 서버가 아니라 버튼 클릭을 실시간으로 받을 수 없다.
        그래서 매 실행 **시작 시** 이 함수로 밀린 콜백을 한꺼번에 반영한다.

        Args:
            offset: 마지막으로 처리한 update_id + 1

        Returns:
            (콜백 리스트, 다음에 쓸 offset)
        """
        data = {"timeout": 0, "allowed_updates": '["callback_query"]'}
        if offset:
            data["offset"] = offset

        result = self._call("getUpdates", data=data)
        callbacks: list[Callback] = []
        max_update_id = offset - 1 if offset else 0

        for update in result:
            update_id = int(update.get("update_id", 0))
            max_update_id = max(max_update_id, update_id)

            query = update.get("callback_query")
            if not query:
                continue

            raw = query.get("data", "") or ""
            action, _, key = raw.partition(":")
            if not key:
                log.warning("알 수 없는 callback_data 무시: %r", raw)
                continue

            callbacks.append(
                Callback(
                    update_id=update_id,
                    callback_id=query.get("id", ""),
                    action=action,
                    key=key,
                )
            )

        next_offset = max_update_id + 1 if max_update_id else offset
        log.info("텔레그램 콜백 %d건 수신 (다음 offset=%d)", len(callbacks), next_offset)
        return callbacks, next_offset

    def answer_callback(self, callback_id: str, text: str = "반영되었습니다") -> None:
        """버튼의 로딩 표시를 없애는 확인 응답. 실패해도 파이프라인은 계속."""
        if not callback_id:
            return
        try:
            self._call("answerCallbackQuery", data={"callback_query_id": callback_id, "text": text})
        except Exception as exc:  # noqa: BLE001
            log.warning("answerCallbackQuery 실패 (무시): %s", exc)


# ---------------------------------------------------------------- 내부 --

def _keyboard_json(buttons: list[list[dict]]) -> str:
    """인라인 키보드를 reply_markup JSON 문자열로 직렬화한다."""
    import json

    return json.dumps({"inline_keyboard": buttons}, ensure_ascii=False)


def _split_message(text: str, limit: int) -> list[str]:
    """긴 메시지를 limit 이하 조각으로 자른다. 되도록 줄 경계에서 자른다."""
    text = text or ""
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        cut = remaining.rfind("\n", 0, limit)
        if cut < limit // 2:  # 적당한 줄바꿈이 없으면 그냥 자른다
            cut = limit
        chunks.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    if remaining:
        chunks.append(remaining)
    return chunks


def _strip_html(text: str) -> str:
    """HTML 태그를 제거해 평문으로 되돌린다 (parse_mode 실패 시 폴백)."""
    import re

    return html.unescape(re.sub(r"<[^>]+>", "", text or ""))
