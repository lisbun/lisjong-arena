"""Issue #211 — RiichiLab source pilot (exact #170 source vs retained yakuhai-call)。

このpackageはIssue #211 1本のためのexperiment-local harnessである。
generic external-data framework、generic replay engine、新しい麻雀rules
engine、model registry、dataset registry、generic experiment frameworkは
導入しない。既存primitiveの再利用を優先する。

```text
Gate 0   riichienv replay seam -> current DecisionContext
         8204 feature + 802 legal mask + canonical teacher action index
Budget   raw-game isolated / deterministic truncation
         TRAIN 9,116 / VALIDATION 2,555 per arm
Student  8204 -> 128 ReLU -> 802 / masked CE over exact legal actions
Serving  両arm同一のadapter / fail closed
Result   ABBB 4p-red-single / seeds 23000..23099 / 400 games
         exactly one predeclared outcome
```

実データ処理（exact #170 corpus、retained #140/#190 dataset、400-game
evaluation）はoperatorがlocalで実行する。generated dataset rowとtrained
weightsはrepositoryへcommitしない。
"""

from .dataset import (
    Gate0Report,
    MaterializedSource,
    RowBudget,
    build_row_budget,
    build_source,
    materialize_local_corpus,
)
from .errors import (
    BudgetNotMatchableError,
    MaterializationError,
    ServingError,
    SourceIdentityError,
    SourcePilotArtifactError,
    SourcePilotError,
    SourcePilotProtocolError,
)
from .materialization import (
    REPLAY_SEAM,
    DecisionKind,
    GameMaterialization,
    GameUnsupportedReason,
    MaterializedRow,
    RowUnresolvedReason,
    materialize_game,
)
from .outcome import classify_outcome, interpretation_boundary
from .protocol import (
    EVALUATION_SEEDS,
    PROTOCOL_ID,
    TRAIN_ROW_BUDGET,
    VALIDATION_ROW_BUDGET,
    Arm,
    SourcePilotOutcome,
    plan_document,
    validate_plan,
)

__all__ = [
    "EVALUATION_SEEDS",
    "PROTOCOL_ID",
    "REPLAY_SEAM",
    "TRAIN_ROW_BUDGET",
    "VALIDATION_ROW_BUDGET",
    "Arm",
    "BudgetNotMatchableError",
    "DecisionKind",
    "Gate0Report",
    "GameMaterialization",
    "GameUnsupportedReason",
    "MaterializationError",
    "MaterializedRow",
    "MaterializedSource",
    "RowBudget",
    "RowUnresolvedReason",
    "ServingError",
    "SourceIdentityError",
    "SourcePilotArtifactError",
    "SourcePilotError",
    "SourcePilotOutcome",
    "SourcePilotProtocolError",
    "build_row_budget",
    "build_source",
    "classify_outcome",
    "interpretation_boundary",
    "materialize_game",
    "materialize_local_corpus",
    "plan_document",
    "validate_plan",
]
