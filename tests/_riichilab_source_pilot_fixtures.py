"""Wholly synthetic Issue #211 fixtures; no third-party log bytes.

このfixtureはIssue #170の実corpusを一切読まない。MJAI eventはtest内で
組み立てたsyntheticな値か、Arena自身のlocal RiichiEnv実行で生成した
public-format logだけであり、raw / transformed third-party recordを
含まない。

honor牌の表記はRiichiEnv 0.4.8のMJAI表記（`E` / `S` / `W` / `N` / `P` /
`F` / `C`）を使う。赤5は`5mr`等である。
"""

from __future__ import annotations

import json

_SEAT_COUNT = 4

RYUKYOKU = {
    "type": "ryukyoku",
    "reason": "exhaustive_draw",
    "deltas": [0, 0, 0, 0],
}
END_EVENTS = ({"type": "end_kyoku"}, {"type": "end_game"})

# 以下のneutral handは、各scenarioが使う牌に対してclaim能力を持たない。
NEUTRAL_1 = [
    "2p",
    "3p",
    "4p",
    "6p",
    "7p",
    "8p",
    "1s",
    "2s",
    "4s",
    "5s",
    "6s",
    "7s",
    "8s",
]
NEUTRAL_2 = ["2p", "3p", "4p", "6p", "7p", "8p", "W", "W", "N", "N", "C", "C", "F"]
NEUTRAL_3 = ["P", "P", "F", "F", "6s", "6s", "7s", "7s", "8s", "8s", "W", "W", "C"]

#: 同じ牌種が2枚以上あり、semantic dedupを踏むplain hand。
PLAIN_HAND = [
    "1m",
    "1m",
    "2m",
    "2m",
    "3m",
    "3m",
    "4m",
    "4m",
    "5m",
    "5m",
    "6m",
    "6m",
    "7m",
]

#: 3p待ちのclosed tenpai hand（riichi宣言に使う）。
TENPAI_HAND = [
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
    "8m",
    "1p",
    "2p",
]

#: 9s待ちのyakuhai（場風East）ron hand。
EAST_RON_HAND = [
    "E",
    "E",
    "E",
    "2m",
    "3m",
    "4m",
    "5m",
    "6m",
    "7m",
    "7m",
    "8m",
    "9m",
    "9s",
]

#: 9s待ちのyakuhai（自風South）ron hand。
SOUTH_RON_HAND = [
    "S",
    "S",
    "S",
    "2p",
    "3p",
    "4p",
    "6p",
    "7p",
    "8p",
    "1s",
    "2s",
    "3s",
    "9s",
]


def start_game() -> dict:
    return {"type": "start_game"}


def start_kyoku(
    hands: list[list[str]],
    *,
    bakaze: str = "E",
    kyoku: int = 1,
    honba: int = 0,
    kyotaku: int = 0,
    oya: int = 0,
    dora_marker: str = "9p",
    scores: list[int] | None = None,
) -> dict:
    if len(hands) != _SEAT_COUNT:
        raise ValueError("a start_kyoku event needs exactly four hands")
    return {
        "type": "start_kyoku",
        "bakaze": bakaze,
        "dora_marker": dora_marker,
        "honba": honba,
        "kyoku": kyoku,
        "kyotaku": kyotaku,
        "oya": oya,
        "scores": [25000] * _SEAT_COUNT if scores is None else list(scores),
        "tehais": [list(hand) for hand in hands],
    }


def tsumo(actor: int, pai: str) -> dict:
    return {"type": "tsumo", "actor": actor, "pai": pai}


def dahai(actor: int, pai: str, *, tsumogiri: bool) -> dict:
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


def hora(actor: int, target: int, pai: str, *, tsumo_win: bool = False) -> dict:
    event = {
        "type": "hora",
        "actor": actor,
        "target": target,
        "pai": pai,
        "deltas": [0, 0, 0, 0],
        "ura_markers": ["1p"],
    }
    if tsumo_win:
        event["tsumo"] = True
    return event


def none(actor: int) -> dict:
    return {"type": "none", "actor": actor}


def tile_counts(events: list[dict]) -> dict[str, int]:
    """fixtureが物理枚数上限を超えていないことをtest側で確認するための集計。"""
    counts: dict[str, int] = {}
    for event in events:
        if event["type"] == "start_kyoku":
            for hand in event["tehais"]:
                for tile in hand:
                    counts[tile] = counts.get(tile, 0) + 1
        elif event["type"] == "tsumo":
            counts[event["pai"]] = counts.get(event["pai"], 0) + 1
    return counts


def normal_discard_log() -> list[dict]:
    """tsumogiriとtedashiの両方を含む、callのない最小log。"""
    return [
        start_game(),
        start_kyoku([PLAIN_HAND, NEUTRAL_1, NEUTRAL_2, NEUTRAL_3]),
        tsumo(0, "9s"),
        dahai(0, "9s", tsumogiri=True),
        tsumo(1, "9m"),
        dahai(1, "1s", tsumogiri=False),
        RYUKYOKU,
        *END_EVENTS,
    ]


def riichi_log() -> list[dict]:
    """riichi宣言とriichi discardを含むlog。"""
    return [
        start_game(),
        start_kyoku([TENPAI_HAND, NEUTRAL_1, NEUTRAL_2, NEUTRAL_3]),
        tsumo(0, "9s"),
        reach(0),
        dahai(0, "9s", tsumogiri=True),
        reach_accepted(0),
        RYUKYOKU,
        *END_EVENTS,
    ]


def riichi_stick_log() -> list[dict]:
    """riichi成立後に他家のturnが続くlog。

    current public score / riichi stick / live wall残数が、round開始時点の
    値ではなくdecision時点の値として再構成されていることを確認するために
    使う。
    """
    return [
        start_game(),
        start_kyoku([TENPAI_HAND, NEUTRAL_1, NEUTRAL_2, NEUTRAL_3]),
        tsumo(0, "9s"),
        reach(0),
        dahai(0, "9s", tsumogiri=True),
        reach_accepted(0),
        tsumo(1, "9m"),
        dahai(1, "1s", tsumogiri=False),
        tsumo(2, "9m"),
        dahai(2, "W", tsumogiri=False),
        RYUKYOKU,
        *END_EVENTS,
    ]


_CHI_HAND_0 = [
    "1p",
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
]
_CHI_HAND_1 = ["2p", "3p", "5p", "6p", "7p", "1s", "3s", "E", "E", "S", "N", "P", "F"]


def chi_log() -> list[dict]:
    """chiとpost-call discardを含むlog。"""
    return [
        start_game(),
        start_kyoku([_CHI_HAND_0, _CHI_HAND_1, NEUTRAL_2, NEUTRAL_3]),
        tsumo(0, "5s"),
        dahai(0, "1p", tsumogiri=False),
        chi(1, 0, "1p", ["2p", "3p"]),
        dahai(1, "F", tsumogiri=False),
        RYUKYOKU,
        *END_EVENTS,
    ]


_PON_HAND_0 = [
    "E",
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
]
_PON_HAND_2 = ["E", "E", "4s", "5s", "7s", "8s", "W", "W", "N", "N", "1s", "2s", "9s"]


def pon_log() -> list[dict]:
    """ponとpost-call discardを含むlog。"""
    return [
        start_game(),
        start_kyoku([_PON_HAND_0, NEUTRAL_1, _PON_HAND_2, NEUTRAL_3]),
        tsumo(0, "9p"),
        dahai(0, "E", tsumogiri=False),
        pon(2, 0, "E", ["E", "E"]),
        dahai(2, "9s", tsumogiri=False),
        RYUKYOKU,
        *END_EVENTS,
    ]


_KAN_HAND_2 = ["E", "E", "E", "5s", "7s", "8s", "W", "W", "N", "N", "1s", "2s", "9s"]


def daiminkan_log() -> list[dict]:
    """daiminkanとkan doraおよびrinshan turnを含むlog。"""
    return [
        start_game(),
        start_kyoku([_PON_HAND_0, NEUTRAL_1, _KAN_HAND_2, NEUTRAL_3]),
        tsumo(0, "9p"),
        dahai(0, "E", tsumogiri=False),
        daiminkan(2, 0, "E", ["E", "E", "E"]),
        dora("1s"),
        tsumo(2, "9s"),
        dahai(2, "9s", tsumogiri=True),
        RYUKYOKU,
        *END_EVENTS,
    ]


_ANKAN_HAND_0 = [
    "1m",
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
]


def ankan_log() -> list[dict]:
    """ankanとkan dora、rinshan turnを含むlog。"""
    return [
        start_game(),
        start_kyoku([_ANKAN_HAND_0, NEUTRAL_1, NEUTRAL_2, NEUTRAL_3]),
        tsumo(0, "9s"),
        ankan(0, ["1m", "1m", "1m", "1m"]),
        dora("1s"),
        tsumo(0, "9s"),
        dahai(0, "9s", tsumogiri=True),
        RYUKYOKU,
        *END_EVENTS,
    ]


_KAKAN_HAND_0 = [
    "3s",
    "3s",
    "1m",
    "1m",
    "2m",
    "2m",
    "3m",
    "3m",
    "4m",
    "4m",
    "5m",
    "5m",
    "6m",
]
_KAKAN_HAND_3 = [
    "2m",
    "3m",
    "4m",
    "5m",
    "6m",
    "7m",
    "1p",
    "1p",
    "1p",
    "9m",
    "9m",
    "1s",
    "2s",
]
_KAKAN_NEUTRAL_1 = [
    "2p",
    "3p",
    "4p",
    "6p",
    "7p",
    "8p",
    "4s",
    "5s",
    "6s",
    "7s",
    "8s",
    "9s",
    "9s",
]


def kakan_log(*, with_chankan_ron: bool) -> list[dict]:
    """kakanを含むlog。`with_chankan_ron`で槍槓ronの有無を切り替える。"""
    events = [
        start_game(),
        start_kyoku([_KAKAN_HAND_0, _KAKAN_NEUTRAL_1, NEUTRAL_2, _KAKAN_HAND_3]),
        tsumo(0, "9p"),
        dahai(0, "9p", tsumogiri=True),
        tsumo(1, "3s"),
        dahai(1, "3s", tsumogiri=True),
        pon(0, 1, "3s", ["3s", "3s"]),
        dahai(0, "6m", tsumogiri=False),
        tsumo(1, "9p"),
        dahai(1, "9p", tsumogiri=True),
        tsumo(2, "9p"),
        dahai(2, "9p", tsumogiri=True),
        tsumo(3, "5p"),
        dahai(3, "5p", tsumogiri=True),
        tsumo(0, "3s"),
        kakan(0, "3s", ["3s", "3s", "3s"]),
    ]
    if with_chankan_ron:
        events.append(hora(3, 0, "3s"))
    else:
        events.extend([dora("1s"), tsumo(0, "9p"), dahai(0, "9p", tsumogiri=True)])
        events.append(RYUKYOKU)
    events.extend(END_EVENTS)
    return events


def tsumo_win_log() -> list[dict]:
    """自摸和了を含むlog。"""
    return [
        start_game(),
        start_kyoku(
            [
                [
                    "E",
                    "E",
                    "E",
                    "2m",
                    "3m",
                    "4m",
                    "5m",
                    "6m",
                    "7m",
                    "7m",
                    "8m",
                    "9m",
                    "9s",
                ],
                NEUTRAL_1,
                NEUTRAL_2,
                NEUTRAL_3,
            ]
        ),
        tsumo(0, "9s"),
        hora(0, 0, "9s", tsumo_win=True),
        *END_EVENTS,
    ]


def ron_log() -> list[dict]:
    """単一ronを含むlog。"""
    return [
        start_game(),
        start_kyoku([PLAIN_HAND, NEUTRAL_1, NEUTRAL_2, EAST_RON_HAND]),
        tsumo(0, "9s"),
        dahai(0, "9s", tsumogiri=True),
        hora(3, 0, "9s"),
        *END_EVENTS,
    ]


def multi_ron_log() -> list[dict]:
    """同じdiscardへの2人のronを含むlog。"""
    return [
        start_game(),
        start_kyoku([PLAIN_HAND, SOUTH_RON_HAND, NEUTRAL_2, EAST_RON_HAND]),
        tsumo(0, "9s"),
        dahai(0, "9s", tsumogiri=True),
        hora(1, 0, "9s"),
        hora(3, 0, "9s"),
        *END_EVENTS,
    ]


def explicit_pass_log() -> list[dict]:
    """明示`none`によるexplicit passを含むlog。"""
    return [
        start_game(),
        start_kyoku([PLAIN_HAND, NEUTRAL_1, NEUTRAL_2, NEUTRAL_3]),
        tsumo(0, "4p"),
        dahai(0, "4p", tsumogiri=True),
        none(1),
        tsumo(1, "1m"),
        dahai(1, "1m", tsumogiri=True),
        RYUKYOKU,
        *END_EVENTS,
    ]


_RED_FIVE_HAND = [
    "5mr",
    "1m",
    "1m",
    "2m",
    "3m",
    "4m",
    "6m",
    "7m",
    "8m",
    "9m",
    "9m",
    "1p",
    "2p",
]


def red_five_log() -> list[dict]:
    """赤5を保持し、赤5をdiscardするlog。"""
    return [
        start_game(),
        start_kyoku([_RED_FIVE_HAND, NEUTRAL_1, NEUTRAL_2, NEUTRAL_3]),
        tsumo(0, "5m"),
        dahai(0, "5mr", tsumogiri=False),
        RYUKYOKU,
        *END_EVENTS,
    ]


def unknown_event_log() -> list[dict]:
    """未知のevent typeを含むlog。"""
    return [
        start_game(),
        start_kyoku([PLAIN_HAND, NEUTRAL_1, NEUTRAL_2, NEUTRAL_3]),
        {"type": "kita", "actor": 0, "pai": "N"},
        *END_EVENTS,
    ]


def hidden_variant_logs() -> tuple[list[dict], list[dict]]:
    """公開eventが同一で、他家のconcealed handだけが違う2つのlog。

    student featureがopponent concealed truthを読んでいないことを、
    同じtarget seat rowがbit一致することで確認するために使う。
    """
    shared = [
        tsumo(0, "9s"),
        dahai(0, "9s", tsumogiri=True),
        RYUKYOKU,
        *END_EVENTS,
    ]
    variant_a = [
        start_game(),
        start_kyoku([PLAIN_HAND, NEUTRAL_1, NEUTRAL_2, NEUTRAL_3]),
        *shared,
    ]
    variant_b = [
        start_game(),
        start_kyoku([PLAIN_HAND, NEUTRAL_2, NEUTRAL_3, NEUTRAL_1]),
        *shared,
    ]
    return variant_a, variant_b


def future_divergent_logs() -> tuple[list[dict], list[dict]]:
    """共通prefixのあと未来だけが違う2つのlog。"""
    prefix = [
        start_game(),
        start_kyoku([PLAIN_HAND, NEUTRAL_1, NEUTRAL_2, NEUTRAL_3]),
        tsumo(0, "9s"),
        dahai(0, "9s", tsumogiri=True),
    ]
    short = [*prefix, RYUKYOKU, *END_EVENTS]
    long = [
        *prefix,
        tsumo(1, "9m"),
        dahai(1, "1s", tsumogiri=False),
        tsumo(2, "9m"),
        dahai(2, "W", tsumogiri=False),
        RYUKYOKU,
        *END_EVENTS,
    ]
    return short, long


def generated_game_log(seed: int, *, game_mode: str = "4p-red-single"):
    """Arena自身のlocal RiichiEnv実行から、1 gameのpublic-format logを作る。

    戻り値は`(mjai events, live decisions)`である。live decisionは
    `(seat, PolicyInput, legal actions, selected action)`のtupleであり、
    live semanticsとreplay semanticsの等価性testでのみ使う。
    """
    from lisjong.policies import (
        YakuhaiCallGenbutsuDefenseFiniteHorizonHandValueAwarePolicy,
    )
    from lisjong.policy_contract import Seat

    from lisjong_arena.riichienv.local_game_runner import (
        LocalGameInspectionRecorder,
        LocalGameRunner,
    )

    recorder = LocalGameInspectionRecorder()
    LocalGameRunner(
        seed=seed,
        game_mode=game_mode,
        policies={
            Seat(index): YakuhaiCallGenbutsuDefenseFiniteHorizonHandValueAwarePolicy()
            for index in range(_SEAT_COUNT)
        },
        inspection_recorder=recorder,
    ).run()
    inspection = recorder.snapshot()
    events = [json.loads(event.event) for event in inspection.game_trace.events]
    decisions = [
        (
            int(seat_decision.seat),
            seat_decision.policy_input,
            tuple(seat_decision.decision_trace.legal_actions),
            seat_decision.decision_trace.selected_action,
        )
        for step in inspection.step_observations
        for seat_decision in step.seat_decisions
    ]
    return events, decisions
