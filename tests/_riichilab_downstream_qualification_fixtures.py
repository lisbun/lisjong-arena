"""Wholly synthetic Issue #203 fixtures; no third-party log bytes.

このfixtureはIssue #170の実corpusを一切読まない。MJAI eventはtest内で組み立てた
syntheticな値だけであり、raw / transformed third-party recordを含まない。
"""

from __future__ import annotations

import gzip
import json

_SEAT_COUNT = 4

# 13枚の初期手牌テンプレート。structural waitがsupportedかどうかを固定的に
# 制御できるよう、seatごとに異なる形を使う。
CLOSED_TENPAI_HAND = [
    "1m",
    "1m",
    "1m",
    "2m",
    "3m",
    "4m",
    "5m",
    "6m",
    "7m",
    "8m",
    "9m",
    "9m",
    "9m",
]
PLAIN_HAND_B = [
    "1p",
    "2p",
    "3p",
    "4p",
    "5p",
    "6p",
    "7p",
    "8p",
    "9p",
    "E",
    "E",
    "S",
    "S",
]
PLAIN_HAND_C = [
    "1s",
    "2s",
    "3s",
    "4s",
    "5s",
    "6s",
    "7s",
    "8s",
    "9s",
    "W",
    "W",
    "N",
    "N",
]
PLAIN_HAND_D = ["P", "P", "F", "F", "C", "C", "2m", "2m", "3p", "3p", "4s", "4s", "5s"]

DEFAULT_HANDS = [CLOSED_TENPAI_HAND, PLAIN_HAND_B, PLAIN_HAND_C, PLAIN_HAND_D]


def start_game(names: list[str] | None = None) -> dict:
    return {
        "type": "start_game",
        "names": ["seat0", "seat1", "seat2", "seat3"] if names is None else names,
    }


def start_kyoku(
    *,
    hands: list[list[str]] | None = None,
    bakaze: str = "E",
    kyoku: int = 1,
    honba: int = 0,
    kyotaku: int = 0,
    oya: int = 0,
    dora_marker: str = "9s",
    scores: list[int] | None = None,
) -> dict:
    return {
        "type": "start_kyoku",
        "bakaze": bakaze,
        "dora_marker": dora_marker,
        "honba": honba,
        "kyoku": kyoku,
        "kyotaku": kyotaku,
        "oya": oya,
        "scores": [25000] * _SEAT_COUNT if scores is None else scores,
        "tehais": [list(hand) for hand in (DEFAULT_HANDS if hands is None else hands)],
    }


def tsumo(actor: int, pai: str) -> dict:
    return {"type": "tsumo", "actor": actor, "pai": pai}


def dahai(actor: int, pai: str, *, tsumogiri: bool = False) -> dict:
    return {"type": "dahai", "actor": actor, "pai": pai, "tsumogiri": tsumogiri}


def chi(actor: int, target: int, pai: str, consumed: list[str]) -> dict:
    return {
        "type": "chi",
        "actor": actor,
        "target": target,
        "pai": pai,
        "consumed": list(consumed),
    }


def pon(actor: int, target: int, pai: str, consumed: list[str]) -> dict:
    return {
        "type": "pon",
        "actor": actor,
        "target": target,
        "pai": pai,
        "consumed": list(consumed),
    }


def daiminkan(actor: int, target: int, pai: str, consumed: list[str]) -> dict:
    return {
        "type": "daiminkan",
        "actor": actor,
        "target": target,
        "pai": pai,
        "consumed": list(consumed),
    }


def ankan(actor: int, consumed: list[str]) -> dict:
    return {"type": "ankan", "actor": actor, "consumed": list(consumed)}


def kakan(actor: int, pai: str, consumed: list[str]) -> dict:
    return {"type": "kakan", "actor": actor, "pai": pai, "consumed": list(consumed)}


def reach(actor: int) -> dict:
    return {"type": "reach", "actor": actor}


def reach_accepted(actor: int) -> dict:
    return {"type": "reach_accepted", "actor": actor}


def dora(dora_marker: str) -> dict:
    return {"type": "dora", "dora_marker": dora_marker}


def hora(
    actor: int, target: int, pai: str | None = None, *, ura: list[str] | None = None
) -> dict:
    """`hora` event。`pai`はcurrent RiichiEnv MJAIではoptionalである。"""
    event = {"type": "hora", "actor": actor, "target": target}
    if pai is not None:
        event["pai"] = pai
    if ura is not None:
        event["uradora_markers"] = list(ura)
    return event


def ryukyoku(
    *,
    actor: int | None = None,
    reason: str | None = None,
    tenpai_hands: list | None = None,
) -> dict:
    """`ryukyoku` event。current RiichiEnv MJAIではactorを持たない。"""
    event: dict = {"type": "ryukyoku"}
    if actor is not None:
        event["actor"] = actor
    if reason is not None:
        event["reason"] = reason
    if tenpai_hands is not None:
        event["tehais"] = tenpai_hands
    return event


def end_kyoku() -> dict:
    return {"type": "end_kyoku"}


def end_game() -> dict:
    return {"type": "end_game"}


def simple_game(
    *,
    hands: list[list[str]] | None = None,
    body: list[dict] | None = None,
) -> list[dict]:
    """1局だけのsynthetic gameを組み立てる。"""
    inner = (
        [
            tsumo(0, "5mr"),
            dahai(0, "5mr", tsumogiri=True),
            tsumo(1, "1p"),
            dahai(1, "1p", tsumogiri=True),
            tsumo(2, "1s"),
            dahai(2, "1s", tsumogiri=True),
            tsumo(3, "P"),
            dahai(3, "P", tsumogiri=True),
        ]
        if body is None
        else body
    )
    return [
        start_game(),
        start_kyoku(hands=hands),
        *inner,
        ryukyoku(),
        end_kyoku(),
        end_game(),
    ]


def gzip_jsonl(events: list[dict]) -> bytes:
    text = "".join(json.dumps(event, separators=(",", ":")) + "\n" for event in events)
    return gzip.compress(text.encode("utf-8"), mtime=0)


def none(actor: int) -> dict:
    return {"type": "none", "actor": actor}


ALL_FAMILIES_HANDS = [
    ["1m", "1m", "1m", "1m", "2m", "3m", "4m", "5m", "6m", "7m", "8m", "9m", "9m"],
    ["2p", "2p", "3p", "5p", "6p", "7p", "8p", "9p", "9p", "E", "E", "S", "S"],
    ["1s", "1s", "2s", "3s", "4s", "5s", "6s", "7s", "8s", "9s", "W", "W", "N"],
    ["P", "P", "P", "F", "F", "C", "C", "4m", "4m", "4m", "6s", "6s", "7s"],
]

SECOND_ROUND_HANDS = [
    ["1m", "2m", "3m", "4m", "5m", "6m", "7m", "8m", "9m", "1p", "2p", "3p", "4p"],
    ["5p", "6p", "7p", "8p", "9p", "1s", "2s", "3s", "4s", "5s", "6s", "7s", "8s"],
    ["9s", "E", "S", "W", "N", "P", "F", "C", "1m", "2m", "3m", "4m", "5m"],
    ["6m", "7m", "8m", "9m", "1p", "2p", "3p", "4p", "5p", "6p", "7p", "8p", "9p"],
]


def all_action_families_game() -> list[dict]:
    """observed action family全種を1 gameへ収めたsynthetic fixture。

    rules legalityの再現ではなく、reconstruction / mapping境界の網羅が目的で
    ある。牌の物理枚数（各34種4枚）だけは実際に整合させている。
    """
    return [
        start_game(),
        start_kyoku(hands=ALL_FAMILIES_HANDS, dora_marker="8s"),
        tsumo(0, "2p"),
        dahai(0, "2p", tsumogiri=True),
        pon(1, 0, "2p", ["2p", "2p"]),
        dahai(1, "S"),
        tsumo(2, "1p"),
        dahai(2, "6s"),
        pon(3, 2, "6s", ["6s", "6s"]),
        dahai(3, "7s"),
        tsumo(0, "4p"),
        dahai(0, "4p", tsumogiri=True),
        chi(1, 0, "4p", ["3p", "5p"]),
        dahai(1, "E"),
        tsumo(2, "9s"),
        dahai(2, "9s", tsumogiri=True),
        tsumo(3, "P"),
        ankan(3, ["P", "P", "P", "P"]),
        dora("9m"),
        tsumo(3, "5s"),
        dahai(3, "5s", tsumogiri=True),
        tsumo(0, "9m"),
        dahai(0, "4m"),
        daiminkan(3, 0, "4m", ["4m", "4m", "4m"]),
        dora("2m"),
        tsumo(3, "7s"),
        dahai(3, "7s", tsumogiri=True),
        tsumo(1, "2p"),
        kakan(1, "2p", ["2p", "2p", "2p"]),
        dora("1p"),
        tsumo(1, "6p"),
        dahai(1, "6p", tsumogiri=True),
        tsumo(2, "1s"),
        reach(2),
        dahai(2, "1s", tsumogiri=True),
        reach_accepted(2),
        tsumo(3, "F"),
        dahai(3, "F", tsumogiri=True),
        hora(2, 3, "F", ura=["1m"]),
        end_kyoku(),
        start_kyoku(
            hands=SECOND_ROUND_HANDS, kyoku=2, oya=1, dora_marker="E", kyotaku=1
        ),
        tsumo(1, "9s"),
        dahai(1, "9s", tsumogiri=True),
        none(2),
        tsumo(2, "E"),
        hora(2, 2, "E"),
        end_kyoku(),
        end_game(),
    ]
