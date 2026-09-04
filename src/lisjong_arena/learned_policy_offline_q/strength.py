"""Controlled Q-vs-BC ABBB strength screen (Issue #140).

```text
candidate = Q hybrid   (offlineq-q:<Q checkpoint identity>)
baseline  = BC hybrid  (offlineq-bc:<BC checkpoint identity>)
    -> existing SingleRoundEvaluationPlan / run_single_round_evaluation()
    -> existing save_single_round_artifact() (immutable)
    -> existing loader / canonical summarizer による readback
```

このmoduleはABBB protocol semantics、rotation、statistics、artifact schemaを
再実装しない。目的はcurrent strength baselineへのpromotionではなく、Offline Q
objectiveとBC objectiveのeffect directionを測ることである。
"""

import time
from dataclasses import dataclass
from pathlib import Path

from lisjong_arena.model import PolicySpec, SingleRoundEvaluationPlan
from lisjong_arena.single_round_artifact import (
    SingleRoundStrengthArtifact,
    load_single_round_artifact,
    save_single_round_artifact,
)
from lisjong_arena.single_round_evaluation import (
    SeedBlockStatistics,
    SingleRoundStrengthSummary,
    aggregate_candidate_metrics,
    run_single_round_evaluation,
    summarize_single_round_strength,
)

from .errors import OfflineQError
from .protocol import STRENGTH_SCREEN_SEEDS, OfflineQOutcome, require_screening_seed
from .retention import RetainedCandidates
from .serving import create_bc_hybrid_runtime, create_q_hybrid_runtime

CANDIDATE_IDENTITY_PREFIX = "offlineq-q:"
BASELINE_IDENTITY_PREFIX = "offlineq-bc:"


class OfflineQStrengthError(OfflineQError):
    """Q-vs-BC ABBB screening境界の違反。"""


def build_specs(retained: RetainedCandidates) -> tuple[PolicySpec, PolicySpec]:
    """retained candidateから、同一support setを持つQ / BC hybrid specを作る。"""
    supported = retained.q_checkpoint.supported_indices
    q_runtime = create_q_hybrid_runtime(
        retained.q_checkpoint.path, supported_indices=supported
    )
    bc_runtime = create_bc_hybrid_runtime(
        retained.bc_checkpoint.path, supported_indices=supported
    )
    candidate = PolicySpec(
        identity=f"{CANDIDATE_IDENTITY_PREFIX}{retained.q_checkpoint.identity}",
        factory=q_runtime.create_policy,
    )
    baseline = PolicySpec(
        identity=f"{BASELINE_IDENTITY_PREFIX}{retained.bc_checkpoint.identity}",
        factory=bc_runtime.create_policy,
    )
    return candidate, baseline


def build_screening_plan(retained: RetainedCandidates) -> SingleRoundEvaluationPlan:
    """locked ordered seeds (281..305) でQ-vs-BC ABBB planを組み立てる。"""
    candidate, baseline = build_specs(retained)
    for seed in STRENGTH_SCREEN_SEEDS:
        require_screening_seed(seed)
    return SingleRoundEvaluationPlan(
        candidate=candidate, baseline=baseline, seeds=STRENGTH_SCREEN_SEEDS
    )


def classify_value_q_signal(statistics: SeedBlockStatistics) -> OfflineQOutcome:
    """existing seed-block normal-approx 95% intervalをOffline Q outcomeへ分類する。

    intervalが定義されない場合はfail closedし、丸めない。
    """
    if not isinstance(statistics, SeedBlockStatistics):
        raise TypeError("statistics must be a SeedBlockStatistics")
    lower = statistics.normal_approx_95_interval_lower
    upper = statistics.normal_approx_95_interval_upper
    if lower is None or upper is None:
        raise OfflineQStrengthError(
            "screening classification requires a defined normal-approx 95% interval"
        )
    if lower > 0:
        return OfflineQOutcome.VALUE_Q_OBJECTIVE_SIGNAL
    if upper < 0:
        return OfflineQOutcome.VALUE_Q_OBJECTIVE_NEGATIVE
    return OfflineQOutcome.VALUE_Q_OBJECTIVE_INCONCLUSIVE


@dataclass(frozen=True, slots=True)
class StrengthMeasurement:
    """1回のQ-vs-BC comparisonのimmutable artifactと再生成したcanonical summary。"""

    artifact: SingleRoundStrengthArtifact
    summary: SingleRoundStrengthSummary
    outcome: OfflineQOutcome
    wall_clock_seconds: float
    cpu_seconds: float

    def to_document(self) -> dict[str, object]:
        return {
            "candidate_identity": self.artifact.plan.candidate_identity,
            "baseline_identity": self.artifact.plan.baseline_identity,
            "outcome": self.outcome.value,
            "wall_clock_seconds": self.wall_clock_seconds,
            "cpu_seconds": self.cpu_seconds,
        }


def run_strength_screen(
    retained: RetainedCandidates,
    artifact_path: str | Path,
    *,
    progress_callback=None,
) -> StrengthMeasurement:
    """Q-vs-BCのcontrolled comparisonを実行し、artifactを保存してから読み直す。"""
    plan = build_screening_plan(retained)

    wall_started = time.perf_counter()
    cpu_started = time.process_time()
    result = run_single_round_evaluation(plan, progress_callback=progress_callback)
    wall_clock_seconds = time.perf_counter() - wall_started
    cpu_seconds = time.process_time() - cpu_started

    save_single_round_artifact(result, artifact_path)
    artifact = load_single_round_artifact(artifact_path)
    summary = summarize_single_round_strength(
        aggregate_candidate_metrics(
            artifact.plan.candidate_identity, artifact.game_results
        ),
        artifact.game_results,
    )
    if summary != artifact.summary:
        raise OfflineQStrengthError(
            "the canonical summary regenerated from raw results differs from the "
            "stored artifact summary"
        )
    outcome = classify_value_q_signal(summary.seed_block_statistics)
    return StrengthMeasurement(
        artifact=artifact,
        summary=summary,
        outcome=outcome,
        wall_clock_seconds=wall_clock_seconds,
        cpu_seconds=cpu_seconds,
    )


__all__ = [
    "BASELINE_IDENTITY_PREFIX",
    "CANDIDATE_IDENTITY_PREFIX",
    "OfflineQStrengthError",
    "StrengthMeasurement",
    "build_screening_plan",
    "build_specs",
    "classify_value_q_signal",
    "run_strength_screen",
]
