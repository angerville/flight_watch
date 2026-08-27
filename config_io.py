"""config.yaml + state/overrides.json 로딩/저장.

track_prices.py(무거움, playwright 필요)와 bot_commands.py(가벼움, 매 10~15분
실행)가 공통으로 쓰는 부분만 분리했다 — bot_commands.py가 이 모듈만 임포트하면
playwright 없이도 동작해야 매 실행마다 브라우저를 새로 까는 비용 없이 빠르게
돈다.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
OVERRIDES_PATH = HERE / "state" / "overrides.json"


def load_base_config() -> dict:
    with open(HERE / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_overrides() -> dict:
    if not OVERRIDES_PATH.exists():
        return {}
    with open(OVERRIDES_PATH, encoding="utf-8") as f:
        return json.load(f)


def save_overrides(overrides: dict) -> None:
    OVERRIDES_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OVERRIDES_PATH, "w", encoding="utf-8") as f:
        json.dump(overrides, f, ensure_ascii=False, indent=2)


def load_config() -> dict:
    """config.yaml(주석 달린 기본값)에 state/overrides.json(텔레그램 명령어로
    바뀐 값)이 있으면 덮어씌워 합친다. overrides.json은 bot_commands.py 전용
    소유 파일이라 사람이 손으로 다듬는 config.yaml의 주석이 봇 편집 때 날아가지
    않는다.
    """
    cfg = load_base_config()
    cfg.update(load_overrides())
    return cfg
