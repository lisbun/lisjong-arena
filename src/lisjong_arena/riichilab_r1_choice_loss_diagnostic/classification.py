"""Issue #483: R1の打牌選択行を、合法打牌候補全体から分類する。

現物の定義と宣言牌の位置の扱いはIssue #455を継承する。向聴の維持 / 悪化は、
同じ判断時点の合法打牌候補それぞれの打牌後向聴数の最小値を基準にする。
"""

from dataclasses import dataclass

from lisjong.hand_evaluation import calculate_shanten
from lisjong.policy_contract.action import DiscardAction
from lisjong.policy_contract.riichi import RiichiState

from lisjong_arena.riichilab_deal_in_risk_disagreement.analysis import (
    _genbutsu_bounds,
    _has_open_meld,
    _is_genbutsu,
    _RiichiDeclarationTracker,
    _turn_band,
)
from lisjong_arena.riichilab_disagreement_diagnostic.analysis import (
    _post_discard_shanten,
)
from lisjong_arena.riichilab_source_pilot.materialization import (
    MaterializationDecisionObservation,
)

GENBUTSU_AVAILABLE = "available"
GENBUTSU_NONE = "none"
GENBUTSU_UNDETERMINED = "undetermined"
GENBUTSU_AVAILABILITY = (GENBUTSU_AVAILABLE, GENBUTSU_NONE, GENBUTSU_UNDETERMINED)

SHANTEN_KEEP = "keep"
SHANTEN_WORSE = "worse"
UNAVAILABLE = "unavailable"

SELECTED_GENBUTSU = "genbutsu"
SELECTED_NOT_GENBUTSU = "not_genbutsu"

MINIMUM_SHANTEN_BUCKETS = ("0", "1", "2+", UNAVAILABLE)
PRE_DISCARD_SHANTEN_BUCKETS = ("-1", "0", "1", "2+", UNAVAILABLE)


def _shanten_bucket(value: int | None) -> str:
    if value is None:
        return UNAVAILABLE
    return "2+" if value >= 2 else str(value)


@dataclass(frozen=True, slots=True)
class R1Choice:
    """1つのR1打牌選択行の、選ばれた牌に依らない分類。"""

    genbutsu_availability: str
    minimum_post_discard_shanten: int | None
    pre_discard_shanten: int | None
    own_meld: str
    turn_band: str
    _genbutsu_by_action: dict[DiscardAction, bool | None]
    _shanten_by_action: dict[DiscardAction, int | None]

    def shanten_state(self, action: DiscardAction) -> str:
        """選択打牌が合法打牌候補の最小向聴数を維持するか。"""
        shanten = self._shanten_by_action[action]
        if shanten is None or self.minimum_post_discard_shanten is None:
            return UNAVAILABLE
        if shanten == self.minimum_post_discard_shanten:
            return SHANTEN_KEEP
        return SHANTEN_WORSE

    def post_discard_shanten(self, action: DiscardAction) -> int | None:
        return self._shanten_by_action[action]

    def selected_genbutsu(self, action: DiscardAction) -> str:
        genbutsu = self._genbutsu_by_action[action]
        if genbutsu is None:
            return UNAVAILABLE
        return SELECTED_GENBUTSU if genbutsu else SELECTED_NOT_GENBUTSU

    def axes(self) -> tuple[tuple[str, str], ...]:
        """内訳の軸と値。1行は各軸でちょうど1つの値に入る。"""
        minimum = _shanten_bucket(self.minimum_post_discard_shanten)
        return (
            ("all", "all"),
            ("genbutsu_availability", self.genbutsu_availability),
            ("own_meld", self.own_meld),
            ("turn_band", self.turn_band),
            ("minimum_post_discard_shanten", minimum),
            ("pre_discard_shanten", _shanten_bucket(self.pre_discard_shanten)),
            (
                "genbutsu_availability|minimum_post_discard_shanten",
                f"{self.genbutsu_availability}|{minimum}",
            ),
        )


def classify_r1_choice(
    observation: MaterializationDecisionObservation,
    tracker: _RiichiDeclarationTracker,
) -> R1Choice:
    """区分R1の打牌選択行を分類する。呼び出し側がR1であることを確認しておく。"""
    policy_input = observation.decision.input
    seat = int(policy_input.self_seat)
    bounds = tuple(
        _genbutsu_bounds(policy_input, index, tracker.declaration_range[index])
        for index, player in enumerate(policy_input.players)
        if index != seat and player.riichi is RiichiState.ACCEPTED
    )
    discards = tuple(
        action
        for action in observation.decision.legal_actions
        if isinstance(action, DiscardAction)
    )
    genbutsu = {
        action: _is_genbutsu(action.tile.tile_type, bounds) for action in discards
    }
    if any(value is True for value in genbutsu.values()):
        availability = GENBUTSU_AVAILABLE
    elif all(value is False for value in genbutsu.values()):
        availability = GENBUTSU_NONE
    else:
        availability = GENBUTSU_UNDETERMINED

    shanten = {
        action: _post_discard_shanten(observation, action) for action in discards
    }
    values = tuple(shanten.values())
    minimum = None if any(value is None for value in values) else min(values)

    # 打牌前向聴数は、ツモ牌または鳴いた後の牌を含む打牌前のconcealed handで計算する。
    try:
        pre_discard = calculate_shanten(list(policy_input.own_hand.concealed_tiles))
    except TypeError, ValueError:
        pre_discard = None

    own_discard_ordinal = len(policy_input.players[seat].discards) + 1
    return R1Choice(
        genbutsu_availability=availability,
        minimum_post_discard_shanten=minimum,
        pre_discard_shanten=pre_discard,
        own_meld="open" if _has_open_meld(policy_input.players[seat]) else "closed",
        turn_band=_turn_band(own_discard_ordinal),
        _genbutsu_by_action=genbutsu,
        _shanten_by_action=shanten,
    )


__all__ = [
    "GENBUTSU_AVAILABILITY",
    "GENBUTSU_AVAILABLE",
    "GENBUTSU_NONE",
    "GENBUTSU_UNDETERMINED",
    "MINIMUM_SHANTEN_BUCKETS",
    "PRE_DISCARD_SHANTEN_BUCKETS",
    "R1Choice",
    "SELECTED_GENBUTSU",
    "SELECTED_NOT_GENBUTSU",
    "SHANTEN_KEEP",
    "SHANTEN_WORSE",
    "UNAVAILABLE",
    "classify_r1_choice",
]
