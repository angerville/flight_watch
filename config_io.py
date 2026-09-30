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


def iter_combos(cfg: dict):
    """활성화된 지역마다 (지역 dict, 들어가는 공항, 나오는 공항) 조합을 돌려준다.

    지역(region)은 "1월 초에 그곳으로 들어가 2주 안에 다닐 수 있는 범위"다.
    들어가는 공항(in)과 나오는 공항(out)은 같은 지역 안에서만 짝지어서,
    "우루무치로 들어갔다가 상하이에서 나오는" 식의 비현실적인 조합을 막는다.
    in/out을 따로 안 적고 airports만 적으면 둘 다 그 목록을 쓴다(대각선 =
    같은 공항으로 들고 나는 조합 포함). 지역이 겹쳐 같은 (in,out) 쌍이 두 번
    나오면 한 번만 돌려준다. 지역 dict를 그대로 주는 건 호출부가 지역별
    budget_krw / max_price_krw(있으면)를 읽을 수 있게 하려는 것.
    """
    seen: set[tuple[str, str]] = set()
    for region in cfg.get("regions") or []:
        if not region.get("enabled", True):
            continue
        ins = region.get("in") or region.get("airports") or []
        outs = region.get("out") or region.get("airports") or []
        for cin in ins:
            for cout in outs:
                pair = (cin["code"], cout["code"])
                if pair in seen:
                    continue
                seen.add(pair)
                yield region, cin, cout


def region_thresholds(cfg: dict, region: dict) -> tuple[int, int | None]:
    """지역의 (예산, 상한가). 지역에 따로 적혀 있으면 그 값, 없으면 전체 기본값.

    가격대가 지역마다 크게 달라서(중국 남부 60~85만원, 중앙아시아 130만원대,
    코카서스 200만원대) 예산/상한가를 지역별로 둘 수 있게 했다. 상한가는 null도
    "상한 없음"이라는 값이라, 키가 있으면 null이어도 그대로 존중한다.
    """
    budget = region.get("budget_krw", cfg["budget_krw"])
    ceiling = region["max_price_krw"] if "max_price_krw" in region else cfg.get("max_price_krw")
    return budget, ceiling


def airport_names(cfg: dict) -> dict[str, str]:
    """IATA 코드 -> 표시 이름. 꺼둔 지역의 공항도 포함한다(이력 표시용)."""
    names: dict[str, str] = {}
    for region in cfg.get("regions") or []:
        for key in ("in", "out", "airports"):
            for a in region.get(key) or []:
                names[a["code"]] = a["display"]
    return names


def load_config() -> dict:
    """config.yaml(주석 달린 기본값)에 state/overrides.json(텔레그램 명령어로
    바뀐 값)이 있으면 덮어씌워 합친다. overrides.json은 bot_commands.py 전용
    소유 파일이라 사람이 손으로 다듬는 config.yaml의 주석이 봇 편집 때 날아가지
    않는다.
    """
    cfg = load_base_config()
    cfg.update(load_overrides())
    return cfg
