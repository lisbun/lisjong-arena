"""Macro-transition dataset construction (Issue #140).

`same actor / same round`で次のeligible ordinary-discard decisionまでを
1 macro-transitionとするsupport-restricted fitted TD/Q-style datasetを
`LocalGameInspection`（`GameTrace` + step-scoped `PolicyInput` +
validated `DecisionTrace`）から構築する。

hanchan最終局だけを表す`LocalGameResult.seat_round_stats`はper-round score
labelの源泉として使わない。roundごとのsettled scoreは、次のroundの最初の
決定が持つ`PolicyInput.players[...].score`（全seat公開情報）、または
そのroundがhanchan最終局である場合は`LocalGameResult.scores`から一意に
bindする。復元できない場合はfail closedし、推測で埋めない。

```text
eligible discard state s_t
    -- yakuhai-call selected discard -->
    zero or more opponent / scaffold / ineligible decisions
    -->
next eligible discard state s_{t+1} for the same seat
or round terminal (round settlement score)
```

reward = (score_at_next_boundary - score_at_current_boundary) / 10000.0、
gamma = 1.0（gamma自体はこのmoduleではなくtraining側が使う）。future score /
terminal outcomeはtraining label / transitionとしてのみ保持し、runtime
featureへは漏らさない。
"""

from collections.abc import Iterator
from dataclasses import dataclass

from lisjong.action_vocabulary import build_legal_action_mask

from lisjong_arena.learned_policy_input import build_policy_input_feature, tensor_values
from lisjong_arena.learned_policy_stage2.recording import (
    GameRecording,
    RecordedDecision,
    encode_teacher_action,
    iter_recorded_decisions,
)

from .activation import is_eligible_ordinary_discard_choice
from .errors import OfflineQTransitionError
from .model import MacroTransitionRow
from .protocol import REWARD_SCORE_DIVISOR, action_family


def is_eligible_ordinary_discard(decision: RecordedDecision) -> bool:
    """全legal_actionsがDiscardActionかつchoice decision (>=2) の場合だけTrue。"""
    return is_eligible_ordinary_discard_choice(decision.context.legal_actions)


def _round_key(decision: RecordedDecision) -> tuple[str, int, int]:
    round_state = decision.context.input.round
    return (round_state.round_wind.value, round_state.hand_number, round_state.honba)


def _seat_score(decision: RecordedDecision, seat: int) -> int:
    return decision.context.input.players[seat].score


@dataclass(frozen=True, slots=True)
class _RoundGroup:
    round_ordinal: int
    round_key: tuple[str, int, int]
    decisions: tuple[RecordedDecision, ...]


def _group_by_round(decisions: tuple[RecordedDecision, ...]) -> tuple[_RoundGroup, ...]:
    """canonical順のdecisionをplayer-safe round identityでcontiguousに束ねる。

    `(round_wind, hand_number, honba)`は1 hanchan内で一意に進むため、既出の
    組へ戻った場合はfail closedする（生成順の破損を黙って通さない）。
    """
    groups: list[_RoundGroup] = []
    seen_keys: set[tuple[str, int, int]] = set()
    current_key: tuple[str, int, int] | None = None
    current_decisions: list[RecordedDecision] = []

    def flush() -> None:
        if current_key is None:
            return
        groups.append(
            _RoundGroup(
                round_ordinal=len(groups),
                round_key=current_key,
                decisions=tuple(current_decisions),
            )
        )

    for decision in decisions:
        key = _round_key(decision)
        if key != current_key:
            if key in seen_keys:
                raise OfflineQTransitionError(
                    f"round identity {key!r} reappeared after a different round"
                )
            flush()
            seen_keys.add(key)
            current_key = key
            current_decisions = []
        current_decisions.append(decision)

    flush()
    return tuple(groups)


def _feature_and_mask(
    decision: RecordedDecision,
) -> tuple[tuple[float, ...], tuple[bool, ...]]:
    feature = build_policy_input_feature(decision.context.input)
    values = tensor_values(feature)
    legal_mask = build_legal_action_mask(decision.context)
    return values, legal_mask


def build_macro_transitions(recording: GameRecording) -> Iterator[MacroTransitionRow]:
    """1 hanchanの全eligible ordinary-discard decisionをmacro-transition rowへ
    変換する。rowは1件ずつyieldし、全rowを同時にmaterializeしない。
    """
    if not isinstance(recording, GameRecording):
        raise TypeError("recording must be a GameRecording")

    decisions = tuple(iter_recorded_decisions(recording))
    groups = _group_by_round(decisions)
    if not groups:
        return

    final_scores = recording.result.scores

    for group_index, group in enumerate(groups):
        if group_index + 1 < len(groups):
            next_group_first = groups[group_index + 1].decisions[0]
            settled_scores = tuple(
                _seat_score(next_group_first, seat) for seat in range(4)
            )
        else:
            settled_scores = final_scores

        by_actor: dict[int, list[RecordedDecision]] = {}
        for decision in group.decisions:
            if is_eligible_ordinary_discard(decision):
                by_actor.setdefault(decision.actor_seat, []).append(decision)

        for actor_seat, eligible in by_actor.items():
            for index, decision in enumerate(eligible):
                feature_values, legal_mask = _feature_and_mask(decision)
                behavior_index = encode_teacher_action(decision)
                current_score = _seat_score(decision, actor_seat)
                round_state = decision.context.input.round

                if index + 1 < len(eligible):
                    next_decision = eligible[index + 1]
                    next_feature_values, next_legal_mask = _feature_and_mask(
                        next_decision
                    )
                    next_score = _seat_score(next_decision, actor_seat)
                    reward = (next_score - current_score) / REWARD_SCORE_DIVISOR
                    yield MacroTransitionRow(
                        seed=recording.seed,
                        split=recording.split,
                        round_ordinal=group.round_ordinal,
                        round_wind=round_state.round_wind.value,
                        hand_number=round_state.hand_number,
                        honba=round_state.honba,
                        actor_seat=actor_seat,
                        step_ordinal=decision.step_ordinal,
                        decision_ordinal=decision.decision_ordinal,
                        feature_values=feature_values,
                        legal_mask=legal_mask,
                        behavior_action_index=behavior_index,
                        behavior_action_family=action_family(behavior_index),
                        reward=float(reward),
                        terminal=False,
                        next_step_ordinal=next_decision.step_ordinal,
                        next_decision_ordinal=next_decision.decision_ordinal,
                        next_feature_values=next_feature_values,
                        next_legal_mask=next_legal_mask,
                    )
                else:
                    reward = (
                        settled_scores[actor_seat] - current_score
                    ) / REWARD_SCORE_DIVISOR
                    yield MacroTransitionRow(
                        seed=recording.seed,
                        split=recording.split,
                        round_ordinal=group.round_ordinal,
                        round_wind=round_state.round_wind.value,
                        hand_number=round_state.hand_number,
                        honba=round_state.honba,
                        actor_seat=actor_seat,
                        step_ordinal=decision.step_ordinal,
                        decision_ordinal=decision.decision_ordinal,
                        feature_values=feature_values,
                        legal_mask=legal_mask,
                        behavior_action_index=behavior_index,
                        behavior_action_family=action_family(behavior_index),
                        reward=float(reward),
                        terminal=True,
                        next_step_ordinal=None,
                        next_decision_ordinal=None,
                        next_feature_values=None,
                        next_legal_mask=None,
                    )


__all__ = ["build_macro_transitions", "is_eligible_ordinary_discard"]
