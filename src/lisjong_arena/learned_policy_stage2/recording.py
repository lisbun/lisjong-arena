"""Stage 2 experiment-local recording seam: actual teacher decision -> dataset row.

既存のRiichiEnv実行境界をそのまま使い、Stage 2固有のdataset semanticsだけを
ここへ閉じる。

```text
LocalGameRunner (4p-red-half)
        ↓  LocalGameInspectionRecorder
PolicyInput + DecisionTrace   (execute_policy_with_trace validated)
        ↓
DecisionContext (same input / same legal actions)
        ↓
build_policy_input_feature() -> tensor_values()
build_legal_action_mask() / encode_action() / resolve_legal_action()
        ↓
Stage2DecisionRow
```

`LocalGameRunner`、`GameTrace`、`LocalGameInspectionRecorder`は変更しない。
objective execution observationへteacher-internal analysisを混ぜないため、
`DecisionTrace.analysis`は読まず、rowへも保存しない。

teacher actionは必ず`execute_policy_with_trace()`がvalidateしたcanonical
`InternalAction`から取得する。dataset生成のために`Policy.choose_action()`を
直接呼び出してvalidationを迂回しない。
"""

import time
from collections.abc import Iterator
from dataclasses import dataclass

from lisjong.action_vocabulary import (
    build_legal_action_mask,
    encode_action,
    resolve_legal_action,
)
from lisjong.policy_contract import DecisionContext, Seat

from lisjong_arena.learned_policy_input import (
    build_policy_input_feature,
    tensor_values,
)
from lisjong_arena.policy_catalog import create_yakuhai_call
from lisjong_arena.riichienv.local_game_runner import (
    LocalGameInspection,
    LocalGameInspectionRecorder,
    LocalGameResult,
    LocalGameRunner,
)

from .errors import Stage2RecordingError
from .model import Stage2DecisionRow
from .protocol import (
    GAME_MODE,
    TEACHER_IDENTITY,
    TEACHER_POLICY_CLASS,
    Split,
    action_family,
    split_for_seed,
    verify_contract_identity,
)


@dataclass(frozen=True, slots=True)
class GameRecording:
    """1 hanchanのteacher execution observationとsplit membership。"""

    seed: int
    split: Split
    result: LocalGameResult
    inspection: LocalGameInspection
    wall_clock_seconds: float
    cpu_seconds: float

    def __post_init__(self) -> None:
        if type(self.seed) is not int:
            raise TypeError("seed must be an int")
        if not isinstance(self.split, Split):
            raise TypeError("split must be a Split")
        if not isinstance(self.result, LocalGameResult):
            raise TypeError("result must be a LocalGameResult")
        if not isinstance(self.inspection, LocalGameInspection):
            raise TypeError("inspection must be a LocalGameInspection")
        for name in ("wall_clock_seconds", "cpu_seconds"):
            value = getattr(self, name)
            if type(value) is not float or value < 0.0:
                raise ValueError(f"{name} must be a non-negative float")
        if self.result.seed != self.seed:
            raise Stage2RecordingError("result seed does not match the recorded seed")
        if self.result.game_mode != GAME_MODE:
            raise Stage2RecordingError(
                f"unsupported Stage 2 game mode: {self.result.game_mode!r}"
            )


def build_teacher_population() -> dict[Seat, object]:
    """各seatへfresh teacher instanceを割り当てる。seat間でinstanceを共有しない。"""
    policies = {seat: create_yakuhai_call() for seat in Seat}
    if len({id(policy) for policy in policies.values()}) != len(policies):
        raise Stage2RecordingError("each seat must use a distinct teacher instance")
    for policy in policies.values():
        if type(policy).__name__ != TEACHER_POLICY_CLASS:
            raise Stage2RecordingError(
                f"teacher {TEACHER_IDENTITY!r} must be a {TEACHER_POLICY_CLASS}"
            )
    return policies


def record_teacher_game(seed: int) -> GameRecording:
    """locked populationの1 seedを、teacher x4のfixed-seed hanchanとして実行する。"""
    verify_contract_identity()
    split = split_for_seed(seed)
    recorder = LocalGameInspectionRecorder()
    runner = LocalGameRunner(
        build_teacher_population(),
        seed=seed,
        game_mode=GAME_MODE,
        inspection_recorder=recorder,
    )
    wall_start = time.perf_counter()
    cpu_start = time.process_time()
    result = runner.run()
    wall_clock = time.perf_counter() - wall_start
    cpu_seconds = time.process_time() - cpu_start
    return GameRecording(
        seed=seed,
        split=split,
        result=result,
        inspection=recorder.snapshot(),
        wall_clock_seconds=wall_clock,
        cpu_seconds=cpu_seconds,
    )


@dataclass(frozen=True, slots=True)
class RowEncodeCost:
    """feature encodeだけのaccumulated cost。measurement専用でrowへは入らない。"""

    decision_count: int
    feature_encode_seconds: float


class _RoundOrdinals:
    """player-safeなround identityを、出現順の0-based ordinalへ写す。

    `(round_wind, hand_number, honba)`は1 hanchan内で一意に進むため、既出の
    組へ戻った場合はfail closedする（生成順の破損を黙って通さない）。
    """

    __slots__ = ("_ordinals", "_current")

    def __init__(self) -> None:
        self._ordinals: dict[tuple[str, int, int], int] = {}
        self._current: tuple[str, int, int] | None = None

    def resolve(self, key: tuple[str, int, int]) -> int:
        if key == self._current:
            return self._ordinals[key]
        if key in self._ordinals:
            raise Stage2RecordingError(
                f"round identity {key!r} reappeared after a different round"
            )
        ordinal = len(self._ordinals)
        self._ordinals[key] = ordinal
        self._current = key
        return ordinal

    @property
    def round_count(self) -> int:
        return len(self._ordinals)


@dataclass(frozen=True, slots=True)
class RecordedDecision:
    """1 teacher decisionのcanonical順序とvalidated context / action。"""

    step_ordinal: int
    decision_ordinal: int
    actor_seat: int
    context: DecisionContext
    selected_action: object


def iter_recorded_decisions(
    recording: GameRecording,
) -> Iterator[RecordedDecision]:
    """1 hanchanのteacher decisionをcanonical順で返す。

    canonical順は`step_ordinal`昇順、step内は`actor_seat`昇順で固定する。
    `DecisionContext`は記録された`PolicyInput`と、`execute_policy_with_trace()`
    がPolicyへ提示したのと同じ`legal_actions`から再構成する。
    `DecisionTrace.analysis`は読まない。
    """
    if not isinstance(recording, GameRecording):
        raise TypeError("recording must be a GameRecording")

    decision_ordinal = 0
    for step in recording.inspection.step_observations:
        previous_seat = -1
        for observation in step.seat_decisions:
            actor_seat = int(observation.seat)
            if actor_seat <= previous_seat:
                raise Stage2RecordingError(
                    "seat decisions must be ordered by ascending seat"
                )
            previous_seat = actor_seat
            trace = observation.decision_trace
            yield RecordedDecision(
                step_ordinal=step.step_ordinal,
                decision_ordinal=decision_ordinal,
                actor_seat=actor_seat,
                context=DecisionContext(
                    input=observation.policy_input,
                    legal_actions=trace.legal_actions,
                ),
                selected_action=trace.selected_action,
            )
            decision_ordinal += 1

    if decision_ordinal != recording.result.decisions:
        raise Stage2RecordingError(
            "recorded decision count does not match the executed decision count"
        )


def encode_teacher_action(decision: RecordedDecision) -> int:
    """teacher actionをvocabulary indexへencodeし、同一contextでresolveし直す。"""
    index = encode_action(decision.selected_action)
    resolved = resolve_legal_action(index, decision.context)
    if resolved is not decision.selected_action:
        raise Stage2RecordingError(
            "encode / resolve round trip did not return the canonical teacher action"
        )
    return index


def build_decision_rows(
    recording: GameRecording,
    *,
    cost: list[RowEncodeCost] | None = None,
) -> Iterator[Stage2DecisionRow]:
    """1 hanchanの全teacher decisionをcanonical順のrowへ変換する。

    rowは1件ずつyieldし、全rowを同時にmaterializeしない。`cost`を渡した場合、
    feature encodeのみのaccumulated wall-clockを1件だけappendする（rowそのもの
    には測定値を入れない）。
    """
    rounds = _RoundOrdinals()
    encode_seconds = 0.0
    decision_count = 0

    for decision in iter_recorded_decisions(recording):
        policy_input = decision.context.input
        encode_start = time.perf_counter()
        feature = build_policy_input_feature(policy_input)
        values = tensor_values(feature)
        encode_seconds += time.perf_counter() - encode_start

        legal_mask = build_legal_action_mask(decision.context)
        teacher_index = encode_teacher_action(decision)

        round_state = policy_input.round
        round_key = (
            round_state.round_wind.value,
            round_state.hand_number,
            round_state.honba,
        )
        yield Stage2DecisionRow(
            seed=recording.seed,
            split=recording.split,
            step_ordinal=decision.step_ordinal,
            decision_ordinal=decision.decision_ordinal,
            round_ordinal=rounds.resolve(round_key),
            round_wind=round_key[0],
            hand_number=round_key[1],
            honba=round_key[2],
            actor_seat=decision.actor_seat,
            legal_mask=legal_mask,
            feature_values=values,
            teacher_action_index=teacher_index,
            teacher_action_family=action_family(teacher_index),
        )
        decision_count += 1

    if cost is not None:
        cost.append(
            RowEncodeCost(
                decision_count=decision_count,
                feature_encode_seconds=encode_seconds,
            )
        )


def round_count(recording: GameRecording) -> int:
    """1 hanchanのplayer-safe round identityの数を数える。"""
    rounds = _RoundOrdinals()
    for decision in iter_recorded_decisions(recording):
        round_state = decision.context.input.round
        rounds.resolve(
            (
                round_state.round_wind.value,
                round_state.hand_number,
                round_state.honba,
            )
        )
    return rounds.round_count


__all__ = [
    "GameRecording",
    "RecordedDecision",
    "RowEncodeCost",
    "build_decision_rows",
    "build_teacher_population",
    "encode_teacher_action",
    "iter_recorded_decisions",
    "record_teacher_game",
    "round_count",
]
