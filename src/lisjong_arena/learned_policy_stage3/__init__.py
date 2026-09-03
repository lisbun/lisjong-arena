"""Learned Policy Stage 3: retained artifact -> serving-realistic candidate.

```text
explicit retained checkpoint path
    -> strict artifact loader (schema / vocabulary / model config / digest)
    -> experiment-local Learned Policy adapter
    -> actual player-safe PolicyInput
    -> arena-policy-input-feature-v1 (8204)
    -> learned logits (802)
    -> legal mask from the current decision
    -> resolve_legal_action() -> canonical InternalAction
    -> execute_policy() validation boundary
    -> actual 4p-red-half game runner
```

`lisbun/lisjong-arena #136`のbounded serving integration専用の
experiment-local harnessである。generic ML serving framework、model registry、
automatic checkpoint discoveryへは広げない。game strength、production
promotion、model improvementはこのpackageの目的ではない。

`torch`はloader / adapter / fixture pathだけのlazy importであり、protocol値の
参照はML runtimeを要求しない。
"""

from .artifact import (
    FIXTURE_CHECKPOINT_SCHEMA_VERSION,
    ServingCheckpoint,
    load_serving_checkpoint,
)
from .errors import (
    Stage3ArtifactError,
    Stage3Error,
    Stage3ProtocolError,
    Stage3ServingError,
)
from .policy import (
    LearnedServingPolicy,
    ServingDecisionSample,
    ServingLatencySummary,
    ServingRuntime,
    create_serving_runtime,
    summarize_latency,
)
from .protocol import (
    DETERMINISM_RUN_COUNT,
    EXCLUDED_STAGE2_TEST_SEEDS,
    FIXTURE_SEEDS,
    PROTOCOL_ID,
    SERVING_GAME_MODE,
    SERVING_HANCHAN_COUNT,
    SERVING_POPULATION,
    SERVING_ROLE,
    SERVING_SEEDS,
    ArtifactClass,
    Stage3Outcome,
    require_fixture_seed,
    require_serving_seed,
)
from .smoke import (
    HanchanSmokeResult,
    SafetyCounters,
    ServingSmokeResult,
    ServingSmokeRun,
    run_serving_hanchan,
    run_serving_plan,
    run_serving_smoke,
)

__all__ = [
    "DETERMINISM_RUN_COUNT",
    "EXCLUDED_STAGE2_TEST_SEEDS",
    "FIXTURE_CHECKPOINT_SCHEMA_VERSION",
    "FIXTURE_SEEDS",
    "PROTOCOL_ID",
    "SERVING_GAME_MODE",
    "SERVING_HANCHAN_COUNT",
    "SERVING_POPULATION",
    "SERVING_ROLE",
    "SERVING_SEEDS",
    "ArtifactClass",
    "HanchanSmokeResult",
    "LearnedServingPolicy",
    "SafetyCounters",
    "ServingCheckpoint",
    "ServingDecisionSample",
    "ServingLatencySummary",
    "ServingRuntime",
    "ServingSmokeResult",
    "ServingSmokeRun",
    "Stage3ArtifactError",
    "Stage3Error",
    "Stage3Outcome",
    "Stage3ProtocolError",
    "Stage3ServingError",
    "create_serving_runtime",
    "load_serving_checkpoint",
    "require_fixture_seed",
    "require_serving_seed",
    "run_serving_hanchan",
    "run_serving_plan",
    "run_serving_smoke",
    "summarize_latency",
]
