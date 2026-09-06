"""Locked passive tsumogiri Gate B comparator (Issue #162).

`lisbun/lisjong-arena #162`のGate Bは、`#158`のexact P1 hybrid candidateが
fresh single-round rollout上でweakなpassive baselineに対してoffensive
capabilityを示すかだけを見る、one-way viability filterである。このmoduleは
そのGate B専用comparatorを所有する。

```text
Ron / Tsumoがlegal
    -> deterministic winning action

winning actionが無く Passがlegal
    -> Pass

otherwise（own-turn discard）
    -> tsumogiri == True かつ tile == own_hand.drawn_tile のlegal DiscardAction
       をexactly 1件
```

chi / pon / daiminkan / riichi / ankan / kakan / kyuushu kyuuhaiは選ばない。
unique tsumogiri discardを解決できない場合はarbitrary fallbackせず
`PassiveTsumogiriError`でfail closedする。

## 恒久Policyではない

これはArena-owned / Gate-B-specificなexperiment comparatorであり、
`lisjong_arena.policy_catalog.POLICY_CATALOG`へ恒久登録しない。production
Policy、strength baseline、Development Championのいずれでもない。

## Determinism

PRNG、hidden mutable state、instance stateを持たない。決定は
`DecisionContext`だけの関数であり、`legal_actions`の入力順にも依存しない
（canonical `encode_legal_actions()`のvocabulary index順を唯一の総順序として
使う。これはaction semantic fieldから導出されるindexであり、list positionでは
ない）。
"""

from lisjong.action_vocabulary import encode_legal_actions
from lisjong.policy_contract import DecisionContext
from lisjong.policy_contract.action import (
    DiscardAction,
    InternalAction,
    PassAction,
    RonAction,
    TsumoAction,
)

from lisjong_arena.model import PolicySpec

from .errors import OfflineQError

PASSIVE_TSUMOGIRI_IDENTITY = "arena-p1-gate-b-passive-tsumogiri-v1"
"""Gate B comparatorのversioned semantic identity。"""

PASSIVE_TSUMOGIRI_SEMANTICS = (
    "winning-action-first",
    "then-pass",
    "then-unique-drawn-tile-tsumogiri-discard",
    "no-call",
    "no-riichi",
    "no-kan",
    "no-kyuushu-kyuuhai",
    "fail-closed-otherwise",
)
"""result documentへ記録するcomparator semanticsの列。"""

_WINNING_ACTIONS = (RonAction, TsumoAction)


class PassiveTsumogiriError(OfflineQError):
    """passive tsumogiri comparatorが決定を一意に解決できなかった場合。"""


def comparator_block() -> dict[str, object]:
    """Gate B resultへ記録するcomparator identity block。"""
    return {
        "identity": PASSIVE_TSUMOGIRI_IDENTITY,
        "semantics": list(PASSIVE_TSUMOGIRI_SEMANTICS),
        "curated_catalog_registration": False,
        "role": "weak-passive-gate-b-comparator",
    }


class PassiveTsumogiriPolicy:
    """Gate B専用のpassive tsumogiri Policy。

    stateを一切持たない（`__slots__ = ()`）。同じ`DecisionContext`に対して
    常に同じactionを返す。
    """

    __slots__ = ()

    def choose_action(self, decision: DecisionContext) -> InternalAction:
        if not isinstance(decision, DecisionContext):
            raise TypeError("decision must be a DecisionContext")
        encoded = encode_legal_actions(decision)

        for action in encoded.values():
            if isinstance(action, _WINNING_ACTIONS):
                return action

        for action in encoded.values():
            if isinstance(action, PassAction):
                return action

        drawn_tile = decision.input.own_hand.drawn_tile
        if drawn_tile is None:
            raise PassiveTsumogiriError(
                "no winning action and no Pass are legal, but this decision has no "
                "drawn tile; the passive tsumogiri comparator never falls back to "
                "an arbitrary action"
            )
        candidates = [
            action
            for action in encoded.values()
            if isinstance(action, DiscardAction)
            and action.tsumogiri
            and action.tile == drawn_tile
        ]
        if len(candidates) != 1:
            raise PassiveTsumogiriError(
                "the passive tsumogiri comparator requires exactly one legal "
                "tsumogiri discard of the drawn tile; "
                f"got {len(candidates)}"
            )
        return candidates[0]


def create_passive_tsumogiri() -> PassiveTsumogiriPolicy:
    """1 seat・1 gameぶんのfresh comparator instanceを返す。"""
    return PassiveTsumogiriPolicy()


def passive_tsumogiri_spec() -> PolicySpec:
    """Gate B planへ渡すbaseline `PolicySpec`。

    factoryはこのmodule top-levelのimport可能なcallableである。
    """
    return PolicySpec(
        identity=PASSIVE_TSUMOGIRI_IDENTITY, factory=create_passive_tsumogiri
    )


__all__ = [
    "PASSIVE_TSUMOGIRI_IDENTITY",
    "PASSIVE_TSUMOGIRI_SEMANTICS",
    "PassiveTsumogiriError",
    "PassiveTsumogiriPolicy",
    "comparator_block",
    "create_passive_tsumogiri",
    "passive_tsumogiri_spec",
]
