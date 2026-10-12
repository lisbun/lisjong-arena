"""Issue #489: 固定したChampionが同じ判断入力をどう分類したかを観測する。

分岐の分類はlisjongが判断と一緒に返す分析の`branch`をそのまま数える。Arena側で
Push / Foldや守備filterを再実装しない。
"""

from dataclasses import dataclass

from lisjong.policies.placement_aware_speed_call import (
    GameSituationMode,
    situation_mode,
)
from lisjong.policy_contract import Policy, execute_policy_with_trace
from lisjong.policy_contract.action import DiscardAction
from lisjong.policy_contract.decision_trace import DecisionTrace
from lisjong.policy_contract.riichi import RiichiState
from lisjong.structural_efficiency import (
    StructuralShantenEvaluator,
    evaluate_post_discard_hands,
)

from lisjong_arena.riichilab_disagreement_diagnostic.analysis import (
    DiagnosticBlockedError,
)
from lisjong_arena.riichilab_source_pilot.materialization import (
    MaterializationDecisionObservation,
)

NO_ANALYSIS = "NO_ANALYSIS"


class _SingleTraceSink:
    def __init__(self) -> None:
        self.trace: DecisionTrace | None = None

    def on_decision(self, trace: DecisionTrace) -> None:
        self.trace = trace


@dataclass(frozen=True, slots=True)
class ChampionView:
    """1判断について、固定したChampion側から観測した分類。"""

    branch: str
    riichi_players: str
    shanten_stage: str
    all_last_top: bool
    replay_matches: bool

    def label(self) -> str:
        return (
            f"branch={self.branch}|riichi={self.riichi_players}"
            f"|stage={self.shanten_stage}"
            f"|top={'yes' if self.all_last_top else 'no'}"
            f"|replay={'match' if self.replay_matches else 'mismatch'}"
        )


def observe_champion_view(
    policy: Policy, observation: MaterializationDecisionObservation
) -> ChampionView:
    """Championを同じ`DecisionContext`で1回実行し、lisjong側の分類を読む。"""
    decision = observation.decision
    policy_input = decision.input
    sink = _SingleTraceSink()
    try:
        replayed = execute_policy_with_trace(policy, decision, sink)
    except Exception as error:
        raise DiagnosticBlockedError(
            "champion execute_policy validation failed"
        ) from error
    branch = getattr(getattr(sink.trace, "analysis", None), "branch", None)

    seat = int(policy_input.self_seat)
    # Championは、リーチ宣言済み・成立前の他家もリーチ者として扱う。
    riichi_players = sum(
        player.riichi is not RiichiState.NONE
        for index, player in enumerate(policy_input.players)
        if index != seat
    )
    discards = tuple(
        action for action in decision.legal_actions if isinstance(action, DiscardAction)
    )
    minimum = min(
        evaluation.post_discard_shanten
        for evaluation in evaluate_post_discard_hands(
            policy_input, discards, StructuralShantenEvaluator()
        )
    )
    return ChampionView(
        branch=NO_ANALYSIS if branch is None else branch.name,
        riichi_players="1" if riichi_players == 1 else "2+",
        shanten_stage="2+" if minimum >= 2 else str(minimum),
        all_last_top=(
            situation_mode(policy_input) is GameSituationMode.ALL_LAST_TOP_SPEED
        ),
        replay_matches=replayed == observation.teacher_action,
    )


__all__ = ["ChampionView", "NO_ANALYSIS", "observe_champion_view"]
