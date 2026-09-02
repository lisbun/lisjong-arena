"""Learned Policy Stage 2: minimal behavior-cloning vertical slice.

```text
first-party teacher decision (yakuhai-call x4, 4p-red-half)
    -> actual DecisionContext
    -> arena-policy-input-feature-v1 (8204)
    -> lisjong-action-vocabulary-1 legal mask (802)
    -> versioned dataset artifact + whole-hanchan split
    -> fixed 1x128 MLP + masked cross-entropy
    -> frozen checkpoint
    -> one-shot TEST evaluation
```

`lisbun/lisjong-arena #133`のbounded experiment専用のexperiment-local harness
である。generic ML framework、generic trainer / dataset abstraction、model
registry、databaseへは広げない。torchはtraining / evaluation pathだけの
lazy importとし、dataset生成とartifact readbackはML runtimeを要求しない。
"""

from .artifact import (
    DATASET_SCHEMA_VERSION,
    GameManifestEntry,
    LoadedStage2Dataset,
    Stage2DatasetWriter,
    Stage2RowRecord,
    dataset_identity,
    load_dataset,
)
from .coverage import DatasetCoverage, SplitCoverage, build_coverage
from .errors import (
    Stage2ArtifactError,
    Stage2ContractIdentityError,
    Stage2Error,
    Stage2EvaluationError,
    Stage2ProtocolError,
    Stage2RecordingError,
)
from .model import Stage2DecisionRow
from .protocol import (
    ACTION_FAMILY_NAMES,
    FEATURE_DIMENSION,
    GAME_MODE,
    ORDERED_SEEDS,
    PROTOCOL_ID,
    SPLIT_SEEDS,
    TEACHER_IDENTITY,
    VOCABULARY_SIZE,
    Split,
    Stage2Outcome,
    action_family,
    split_for_seed,
    verify_contract_identity,
    vocabulary_fingerprint,
)
from .recording import (
    GameRecording,
    RecordedDecision,
    build_decision_rows,
    build_teacher_population,
    iter_recorded_decisions,
    record_teacher_game,
    round_count,
)

__all__ = [
    "ACTION_FAMILY_NAMES",
    "DATASET_SCHEMA_VERSION",
    "FEATURE_DIMENSION",
    "GAME_MODE",
    "ORDERED_SEEDS",
    "PROTOCOL_ID",
    "SPLIT_SEEDS",
    "TEACHER_IDENTITY",
    "VOCABULARY_SIZE",
    "DatasetCoverage",
    "GameManifestEntry",
    "GameRecording",
    "LoadedStage2Dataset",
    "RecordedDecision",
    "Split",
    "SplitCoverage",
    "Stage2ArtifactError",
    "Stage2ContractIdentityError",
    "Stage2DatasetWriter",
    "Stage2DecisionRow",
    "Stage2Error",
    "Stage2EvaluationError",
    "Stage2Outcome",
    "Stage2ProtocolError",
    "Stage2RecordingError",
    "Stage2RowRecord",
    "action_family",
    "build_coverage",
    "build_decision_rows",
    "build_teacher_population",
    "dataset_identity",
    "iter_recorded_decisions",
    "load_dataset",
    "record_teacher_game",
    "round_count",
    "split_for_seed",
    "verify_contract_identity",
    "vocabulary_fingerprint",
]
