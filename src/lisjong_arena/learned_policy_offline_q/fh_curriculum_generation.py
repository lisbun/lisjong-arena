"""Per-arm teacher execution and dataset generation (Issue #165).

```text
CurriculumArm
    -> curated Arena factory (policy_catalog)
    -> fresh teacher instance per seat and per game
    -> LocalGameRunner (4p-red-half, locked seed)
    -> LocalGameInspectionRecorder
    -> build_macro_transitions()          (#140 contract, unchanged)
    -> FiniteHorizonCurriculumDatasetWriter
```

`lisjong_arena.learned_policy_stage2.recording`のexecution seamをそのまま使う。
違うのはteacher factoryとsplit解決だけであり、`LocalGameRunner`、`GameTrace`、
`LocalGameInspectionRecorder`、`DecisionTrace`の扱いは変更しない。
`DecisionTrace.analysis`は読まない。

teacher action-family countsは、macro-transition rowではなく**実行された全
teacher decision**から数える。macro-transition rowはeligible ordinary discard
だけを保持するので、riichi / call / winning decisionはrowには現れない。
countsは`encode_teacher_action()`がencode / resolve round tripでvalidateした
canonical actionからだけ導出する。

実dataset生成（32 hanchan x 2 arm）はローカルのone-shot実行であり、CIでも
Claude Code環境でも実行しない。
"""

import time

from lisjong.policy_contract import Seat

from lisjong_arena.learned_policy_stage2.recording import (
    GameRecording,
    encode_teacher_action,
    iter_recorded_decisions,
)
from lisjong_arena.riichienv.local_game_runner import (
    LocalGameInspectionRecorder,
    LocalGameRunner,
)

from .errors import OfflineQRecordingError
from .fh_curriculum import (
    DATASET_GAME_MODE,
    DATASET_ORDERED_SEEDS,
    TEACHER_POPULATION_SIZE,
    CurriculumArm,
    require_arm,
    require_dataset_seed,
    split_for_seed,
    teacher_factory,
    teacher_policy_class,
)
from .fh_curriculum_dataset import (
    FiniteHorizonCurriculumDatasetWriter,
    LoadedFiniteHorizonCurriculumDataset,
)
from .protocol import action_family, verify_contract_identity
from .transitions import build_macro_transitions


def build_arm_teacher_population(arm: CurriculumArm) -> dict[Seat, object]:
    """armのteacherを各seatへfresh instanceとして割り当てる。

    seat間でinstanceを共有せず、game間でも再利用しない（このfunctionは
    game単位で呼ばれる）。curated factoryが返すPolicy classがarmの記録する
    classと一致しない場合はfail closedする。
    """
    factory = teacher_factory(arm)
    expected = teacher_policy_class(arm)
    policies = {seat: factory() for seat in Seat}
    if len(policies) != TEACHER_POPULATION_SIZE:
        raise OfflineQRecordingError(
            "the teacher population must cover exactly the four seats"
        )
    if len({id(policy) for policy in policies.values()}) != len(policies):
        raise OfflineQRecordingError("each seat must use a distinct teacher instance")
    for policy in policies.values():
        if type(policy) is not expected:
            raise OfflineQRecordingError(
                f"the {arm.value!r} arm teacher must be a {expected.__name__}"
            )
    return policies


def record_arm_game(arm: CurriculumArm, seed: int) -> GameRecording:
    """1 armの1 locked seedを、teacher x4のfixed-seed hanchanとして実行する。"""
    verify_contract_identity()
    require_arm(arm)
    require_dataset_seed(seed)
    split = split_for_seed(seed)
    recorder = LocalGameInspectionRecorder()
    runner = LocalGameRunner(
        build_arm_teacher_population(arm),
        seed=seed,
        game_mode=DATASET_GAME_MODE,
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


def teacher_action_family_counts(
    recording: GameRecording,
) -> tuple[int, dict[str, int]]:
    """1 hanchanの全teacher decisionをaction familyごとに数える。

    `(decision_count, counts)`を返す。countsはvalidated canonical actionの
    vocabulary indexから導出し、`decision_count`はそれらの総和である。
    """
    if not isinstance(recording, GameRecording):
        raise TypeError("recording must be a GameRecording")
    counts: dict[str, int] = {}
    decision_count = 0
    for decision in iter_recorded_decisions(recording):
        family = action_family(encode_teacher_action(decision))
        counts[family] = counts.get(family, 0) + 1
        decision_count += 1
    if decision_count == 0:
        raise OfflineQRecordingError("a recorded hanchan produced no teacher decisions")
    return decision_count, counts


def generate_arm_dataset(
    arm: CurriculumArm,
    destination,
    *,
    provenance: dict[str, str] | None = None,
    progress_callback=None,
) -> LoadedFiniteHorizonCurriculumDataset:
    """1 armのlocked 32 hanchan datasetをwrite-onceで生成する。

    Arm Y / Arm Fは同じordered seedsと同じsplitを使い、teacherだけが異なる。
    失敗時はstagingを破棄し、部分的なdatasetを公開しない。
    """
    require_arm(arm)
    writer = FiniteHorizonCurriculumDatasetWriter(
        destination, arm=arm, provenance=provenance
    )
    try:
        for seed in DATASET_ORDERED_SEEDS:
            recording = record_arm_game(arm, seed)
            decision_count, counts = teacher_action_family_counts(recording)
            entry = writer.add_game(
                seed=seed,
                split=recording.split,
                scores=recording.result.scores,
                ranks=recording.result.ranks,
                decision_count=decision_count,
                teacher_action_family_counts=counts,
                rows=build_macro_transitions(recording),
            )
            if progress_callback is not None:
                progress_callback(recording, entry)
        return writer.finalize()
    except BaseException:
        writer.discard()
        raise


__all__ = [
    "build_arm_teacher_population",
    "generate_arm_dataset",
    "record_arm_game",
    "teacher_action_family_counts",
]
