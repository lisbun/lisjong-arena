"""Stage 4a screening execution: checkpoint-bound candidate -> existing ABBB。

```text
retained Stage 4a bundle
    -> strict readback + freeze binding
    -> Stage 3 serving runtime (1回だけのcheckpoint load)
    -> PolicySpec(identity = learned-stage4a:<checkpoint identity>)
    -> existing SingleRoundEvaluationPlan / run_single_round_evaluation()
    -> existing save_single_round_artifact() (immutable)
    -> existing loader / canonical summarizer による readback
```

このmoduleはABBB protocol semantics、rotation、statistics、artifact schemaを
再実装しない。所有するのは「checkpoint identityへbindしたcandidate specを
作る」「locked screening populationで2 comparisonを無条件に実行する」
「artifactのcandidate identityがderived identityと一致することをfail closed
で確認する」というthin orchestrationだけである。

serial実行を第一選択とする。`run_single_round_evaluation()`をworkerへ分配
すると、Learned runtimeのspawn serialization / process-local model cacheという
別scopeが必要になり、wall-clock / process CPU measurementの境界も曖昧になる。
"""

import time
from dataclasses import dataclass
from pathlib import Path

from lisjong_arena.learned_policy_stage3.policy import (
    ServingRuntime,
    create_serving_runtime,
)
from lisjong_arena.model import PolicySpec, SingleRoundEvaluationPlan
from lisjong_arena.policy_catalog import POLICY_CATALOG
from lisjong_arena.single_round_artifact import (
    SingleRoundStrengthArtifact,
    load_single_round_artifact,
    save_single_round_artifact,
)
from lisjong_arena.single_round_evaluation import (
    ROTATION_COUNT,
    SingleRoundStrengthSummary,
    aggregate_candidate_metrics,
    run_single_round_evaluation,
    summarize_single_round_strength,
)

from .candidate import (
    BUNDLE_CHECKPOINT_DIRNAME,
    Stage4aFreeze,
    load_freeze_record,
    verify_freeze_binding,
)
from .errors import Stage4aScreeningError
from .protocol import (
    BASELINE_IDENTITY_BY_ROLE,
    GAMES_PER_COMPARATOR,
    SCREENING_GAME_MODE,
    SCREENING_SEEDS,
    ComparisonRole,
    ScreeningSignal,
    classify_screening_signal,
    derive_candidate_identity,
    require_screening_seeds,
)


@dataclass(frozen=True, slots=True)
class Stage4aCandidate:
    """checkpoint identityへbindしたABBB candidate。

    ``runtime``はcheckpointを1回だけloadしたimmutable serving runtimeであり、
    ``spec.factory``はgame / seatごとにfresh Policy instanceを返す。decision
    ごとのcheckpoint reloadは行わない。
    """

    freeze: Stage4aFreeze
    runtime: ServingRuntime
    spec: PolicySpec

    @property
    def identity(self) -> str:
        return self.spec.identity


def create_stage4a_candidate(bundle_path: str | Path) -> Stage4aCandidate:
    """retained bundleからcheckpoint-bound candidate specを作る。

    Stage 3 serving adapterのload / eval-mode / legal mask / canonical resolve
    境界をそのまま使い、Stage 4aのためにPolicy実装を複製しない。
    """
    path = Path(bundle_path)
    freeze = load_freeze_record(path)
    runtime = create_serving_runtime(path / BUNDLE_CHECKPOINT_DIRNAME)
    verify_freeze_binding(freeze, runtime.checkpoint)

    identity = derive_candidate_identity(runtime.checkpoint.identity)
    if identity != freeze.candidate_identity:
        raise Stage4aScreeningError(
            "the loaded checkpoint does not produce the frozen candidate identity"
        )
    return Stage4aCandidate(
        freeze=freeze,
        runtime=runtime,
        spec=PolicySpec(identity=identity, factory=runtime.create_policy),
    )


def baseline_spec(role: ComparisonRole) -> PolicySpec:
    """roleに対応するcurated comparatorを``POLICY_CATALOG``から解決する。"""
    if not isinstance(role, ComparisonRole):
        raise TypeError("role must be a ComparisonRole")
    identity = BASELINE_IDENTITY_BY_ROLE[role]
    spec = POLICY_CATALOG.get(identity)
    if spec is None or spec.identity != identity:
        raise Stage4aScreeningError(
            f"the locked {role.value} comparator {identity!r} is not a curated "
            "policy in the current catalog"
        )
    return spec


def build_screening_plan(
    candidate: Stage4aCandidate, role: ComparisonRole
) -> SingleRoundEvaluationPlan:
    """locked ordered seedsとlocked comparatorでABBB planを組み立てる。"""
    if not isinstance(candidate, Stage4aCandidate):
        raise TypeError("candidate must be a Stage4aCandidate")
    plan = SingleRoundEvaluationPlan(
        candidate=candidate.spec,
        baseline=baseline_spec(role),
        seeds=SCREENING_SEEDS,
    )
    require_screening_seeds(plan.seeds)
    return plan


@dataclass(frozen=True, slots=True)
class ComparisonMeasurement:
    """1 comparisonのimmutable artifactと、そこから再生成したcanonical summary。"""

    role: ComparisonRole
    candidate_identity: str
    baseline_identity: str
    artifact_filename: str
    artifact: SingleRoundStrengthArtifact
    summary: SingleRoundStrengthSummary
    signal: ScreeningSignal
    wall_clock_seconds: float
    cpu_seconds: float


def artifact_filename(role: ComparisonRole) -> str:
    """role + baseline identityから決まるartifact file名を返す。"""
    return f"{role.value.lower()}-{BASELINE_IDENTITY_BY_ROLE[role]}.json"


def _verify_artifact(
    artifact: SingleRoundStrengthArtifact,
    *,
    candidate: Stage4aCandidate,
    role: ComparisonRole,
) -> SingleRoundStrengthSummary:
    """artifactが実行条件どおりであることをfail closedで確認する。

    canonical summaryはartifact側のcacheをそのまま信用せず、raw game results
    から既存canonical aggregationで再生成して照合する。
    """
    plan = artifact.plan
    expected_identity = derive_candidate_identity(candidate.runtime.checkpoint.identity)
    if plan.candidate_identity != expected_identity:
        raise Stage4aScreeningError(
            "artifact candidate identity is not the identity derived from the "
            f"strict-loaded checkpoint: {plan.candidate_identity!r} != "
            f"{expected_identity!r}"
        )
    if plan.candidate_identity != candidate.freeze.candidate_identity:
        raise Stage4aScreeningError(
            "artifact candidate identity does not match the freeze record"
        )
    if plan.baseline_identity != BASELINE_IDENTITY_BY_ROLE[role]:
        raise Stage4aScreeningError(
            f"artifact baseline identity is not the locked {role.value} comparator"
        )
    require_screening_seeds(plan.seeds)
    if plan.game_mode != SCREENING_GAME_MODE or plan.rotation_count != ROTATION_COUNT:
        raise Stage4aScreeningError(
            "artifact protocol conditions are not the locked ones"
        )
    if len(artifact.game_results) != GAMES_PER_COMPARATOR:
        raise Stage4aScreeningError(
            f"artifact must contain exactly {GAMES_PER_COMPARATOR} games"
        )

    summary = summarize_single_round_strength(
        aggregate_candidate_metrics(plan.candidate_identity, artifact.game_results),
        artifact.game_results,
    )
    if summary != artifact.summary:
        raise Stage4aScreeningError(
            "the canonical summary regenerated from raw results differs from the "
            "stored artifact summary"
        )
    return summary


def run_comparison(
    candidate: Stage4aCandidate,
    role: ComparisonRole,
    artifact_path: str | Path,
    *,
    progress_callback=None,
) -> ComparisonMeasurement:
    """1 comparisonをserial実行し、artifactを保存してから読み直して集計する。

    ``progress_callback``は既存``run_single_round_evaluation()``のoptional
    notificationをそのまま渡すだけで、resultのsemanticsには影響しない。
    """
    path = Path(artifact_path)
    plan = build_screening_plan(candidate, role)

    wall_started = time.perf_counter()
    cpu_started = time.process_time()
    result = run_single_round_evaluation(plan, progress_callback=progress_callback)
    wall_clock_seconds = time.perf_counter() - wall_started
    cpu_seconds = time.process_time() - cpu_started

    save_single_round_artifact(result, path)
    artifact = load_single_round_artifact(path)
    summary = _verify_artifact(artifact, candidate=candidate, role=role)
    return ComparisonMeasurement(
        role=role,
        candidate_identity=artifact.plan.candidate_identity,
        baseline_identity=artifact.plan.baseline_identity,
        artifact_filename=path.name,
        artifact=artifact,
        summary=summary,
        signal=classify_screening_signal(summary.seed_block_statistics),
        wall_clock_seconds=wall_clock_seconds,
        cpu_seconds=cpu_seconds,
    )


__all__ = [
    "ComparisonMeasurement",
    "Stage4aCandidate",
    "artifact_filename",
    "baseline_spec",
    "build_screening_plan",
    "create_stage4a_candidate",
    "run_comparison",
]
