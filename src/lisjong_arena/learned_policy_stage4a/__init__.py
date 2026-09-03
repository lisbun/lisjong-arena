"""Learned Policy Stage 4a: retained candidateのbounded strength screening。

```text
Gate 0
    locked generation population (TRAIN 200..209 / VALIDATION 210..212)
        -> Stage 2 locked model / training config
        -> non-ephemeral retention (write-once bundle)
        -> strict readback + freeze binding
        -> candidate identity = learned-stage4a:<checkpoint identity>

Screening
    -> existing ABBB / 4p-red-single protocol
    -> ordered seeds 220..244 / 4 rotations / 100 games per comparator
    -> primary  vs yakuhai-call   (Stage 2 teacher = current strength baseline)
    -> secondary vs two-step      (low-cost comparator)
    -> existing immutable SingleRoundStrengthArtifact
    -> existing canonical summarizer
    -> predeclared seed-block screening classification
    -> one exhaustive Stage 4a outcome
```

`lisbun/lisjong-arena #138`のbounded screening専用のArena-local orchestration
である。ABBB protocol semantics、artifact schema、statisticsは既存
`lisjong_arena.single_round_evaluation` / `lisjong_arena.single_round_artifact`
をそのまま使い、serving boundaryは`lisjong_arena.learned_policy_stage3`の
adapterをそのまま使う。generic model registry、generic evaluation framework、
production promotionはこのpackageの目的ではない。

このscreening単独はpromotion evidenceではない。`torch`はcandidate生成 /
serving pathだけのlazy dependencyであり、protocol値の参照はML runtimeを
要求しない。
"""

from .candidate import (
    BUNDLE_CHECKPOINT_DIRNAME,
    CANDIDATE_PURPOSE,
    FREEZE_RECORD_FILENAME,
    FREEZE_RECORD_SCHEMA_VERSION,
    RetentionTarget,
    Stage4aFreeze,
    build_freeze_document,
    freeze_candidate,
    load_freeze_record,
    parse_freeze_document,
    resolve_retention_target,
    strict_readback,
    verify_freeze_binding,
    write_freeze_record,
)
from .errors import (
    Stage4aError,
    Stage4aFreezeError,
    Stage4aProtocolError,
    Stage4aRetentionError,
    Stage4aScreeningError,
)
from .evaluation import (
    ComparisonMeasurement,
    Stage4aCandidate,
    artifact_filename,
    baseline_spec,
    build_screening_plan,
    create_stage4a_candidate,
    run_comparison,
)
from .protocol import (
    BASELINE_IDENTITY_BY_ROLE,
    CANDIDATE_GENERATION_SEEDS,
    CANDIDATE_GENERATION_TRAIN_SEEDS,
    CANDIDATE_GENERATION_VALIDATION_SEEDS,
    CANDIDATE_IDENTITY_PREFIX,
    EXCLUDED_STAGE2_TEST_SEEDS,
    EXCLUDED_STAGE3_SERVING_SEEDS,
    GAMES_PER_COMPARATOR,
    PRIMARY_BASELINE_IDENTITY,
    PROTOCOL_ID,
    ROTATIONS_PER_SEED,
    SCREENING_GAME_MODE,
    SCREENING_SEEDS,
    SECONDARY_BASELINE_IDENTITY,
    SEED_BLOCK_COUNT,
    ComparisonRole,
    ScreeningSignal,
    Stage4aOutcome,
    classify_screening_signal,
    decide_outcome,
    derive_candidate_identity,
    require_candidate_generation_seed,
    require_screening_seeds,
)
from .result import (
    Stage4aScreeningResult,
    build_screening_result,
    format_measurement_report,
    format_result_report,
    measurement_document,
    write_result,
)

__all__ = [
    "BASELINE_IDENTITY_BY_ROLE",
    "BUNDLE_CHECKPOINT_DIRNAME",
    "CANDIDATE_GENERATION_SEEDS",
    "CANDIDATE_GENERATION_TRAIN_SEEDS",
    "CANDIDATE_GENERATION_VALIDATION_SEEDS",
    "CANDIDATE_IDENTITY_PREFIX",
    "CANDIDATE_PURPOSE",
    "EXCLUDED_STAGE2_TEST_SEEDS",
    "EXCLUDED_STAGE3_SERVING_SEEDS",
    "FREEZE_RECORD_FILENAME",
    "FREEZE_RECORD_SCHEMA_VERSION",
    "GAMES_PER_COMPARATOR",
    "PRIMARY_BASELINE_IDENTITY",
    "PROTOCOL_ID",
    "ROTATIONS_PER_SEED",
    "SCREENING_GAME_MODE",
    "SCREENING_SEEDS",
    "SECONDARY_BASELINE_IDENTITY",
    "SEED_BLOCK_COUNT",
    "ComparisonMeasurement",
    "ComparisonRole",
    "RetentionTarget",
    "ScreeningSignal",
    "Stage4aCandidate",
    "Stage4aError",
    "Stage4aFreeze",
    "Stage4aFreezeError",
    "Stage4aOutcome",
    "Stage4aProtocolError",
    "Stage4aRetentionError",
    "Stage4aScreeningError",
    "Stage4aScreeningResult",
    "artifact_filename",
    "baseline_spec",
    "build_freeze_document",
    "build_screening_plan",
    "build_screening_result",
    "classify_screening_signal",
    "create_stage4a_candidate",
    "decide_outcome",
    "derive_candidate_identity",
    "format_measurement_report",
    "format_result_report",
    "freeze_candidate",
    "load_freeze_record",
    "measurement_document",
    "parse_freeze_document",
    "require_candidate_generation_seed",
    "require_screening_seeds",
    "resolve_retention_target",
    "run_comparison",
    "strict_readback",
    "verify_freeze_binding",
    "write_freeze_record",
    "write_result",
]
