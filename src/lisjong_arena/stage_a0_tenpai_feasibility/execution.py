"""current flat-BC実行境界からのobserved decision取得。

teacher population、game mode、decisionのcanonical順序は既存のflat-BC
recording seamをそのまま再利用し、Stage A0側で再実装しない。

```text
LocalGameRunner(teacher x4, seed, 4p-red-half)
    + LocalGameInspectionRecorder      public PolicyInput / DecisionTrace
    + DecisionPointHiddenStateRecorder privileged pre-action snapshot
        -> ObservedDecision            同一decision pointの両surface
```
"""

from collections.abc import Iterator
from dataclasses import dataclass

from lisjong.policy_contract import Seat

from lisjong_arena.learned_policy_stage2.recording import (
    build_teacher_population,
    iter_inspection_decisions,
)
from lisjong_arena.riichienv.local_game_runner import (
    LocalGameInspection,
    LocalGameInspectionRecorder,
    LocalGameResult,
    LocalGameRunner,
)

from .emission import ObservedDecision
from .hidden_state import DecisionPointHiddenStateRecorder
from .protocol import GAME_MODE, verify_contract_identity


@dataclass(frozen=True, slots=True)
class ObservedGame:
    """1 hanchanのpublic inspectionと、同じ実行のprivileged snapshot群。"""

    seed: int
    result: LocalGameResult
    inspection: LocalGameInspection
    hidden: DecisionPointHiddenStateRecorder

    def __post_init__(self) -> None:
        if self.result.seed != self.seed:
            raise ValueError("result seed does not match the executed seed")
        if self.hidden.observed_step_count != self.result.steps:
            raise ValueError(
                "privileged snapshot count does not match the executed step count"
            )


def run_observed_game(seed: int, *, max_steps: int | None = None) -> ObservedGame:
    """teacher x4のfixed-seed hanchanを1本実行し、両surfaceを記録する。

    `LocalGameRunner`のdefault behaviorは変えず、opt-inなprivileged observerを
    追加で渡すだけである。privileged snapshotはPolicyへもPolicyInputへも
    GameTraceへも接続されない。
    """
    verify_contract_identity()
    inspection_recorder = LocalGameInspectionRecorder()
    hidden_recorder = DecisionPointHiddenStateRecorder()
    runner = LocalGameRunner(
        build_teacher_population(),
        seed=seed,
        game_mode=GAME_MODE,
        max_steps=max_steps,
        inspection_recorder=inspection_recorder,
        decision_point_observer=hidden_recorder,
    )
    result = runner.run()
    return ObservedGame(
        seed=seed,
        result=result,
        inspection=inspection_recorder.snapshot(),
        hidden=hidden_recorder,
    )


def iter_observed_decisions(game: ObservedGame) -> Iterator[ObservedDecision]:
    """1 hanchanのdecisionを、同じstepのprivileged snapshotと組にして返す。"""
    if not isinstance(game, ObservedGame):
        raise TypeError("game must be an ObservedGame")
    for decision in iter_inspection_decisions(
        game.inspection, expected_decision_count=game.result.decisions
    ):
        yield ObservedDecision(
            step_ordinal=decision.step_ordinal,
            decision_ordinal=decision.decision_ordinal,
            actor_seat=Seat(decision.actor_seat),
            context=decision.context,
            selected_action=decision.selected_action,
            hidden=game.hidden.snapshot(decision.step_ordinal),
        )


def observed_decisions_for_seed(seed: int) -> Iterator[ObservedDecision]:
    """1 seedをcurrent revisionで実行し、そのobserved decisionを返す。

    retained augmentation qualificationとfresh live-label smokeが共有する
    default execution seamである。
    """
    return iter_observed_decisions(run_observed_game(seed))


__all__ = [
    "ObservedGame",
    "observed_decisions_for_seed",
    "iter_observed_decisions",
    "run_observed_game",
]
