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
from .serving import HybridPolicy, create_bc_hybrid_runtime, create_q_hybrid_runtime
from .support import support_set_identity

CANDIDATE_IDENTITY_PREFIX = "offlineq-q:"
BASELINE_IDENTITY_PREFIX = "offlineq-bc:"


class OfflineQStrengthError(OfflineQError):
    """Q-vs-BC ABBB screening境界の違反。"""


class _PolicyInstanceRegistry:
    """factoryをwrapし、ABBB runnerが生成した全HybridPolicy instanceを回収する。

    既存``run_single_round_evaluation()``はgame / seatごとにfresh Policy
    instanceを``spec.factory()``から生成して使い捨てる。ABBB protocol自体は
    変更せず、そのinstance群への参照だけをここで保持し、実行後にlearned
    activation / scaffold fallback / support fallback rateを再構成する。
    """

    __slots__ = ("_create", "instances")

    def __init__(self, create) -> None:
        self._create = create
        self.instances: list[HybridPolicy] = []

    def create_policy(self) -> HybridPolicy:
        policy = self._create()
        self.instances.append(policy)
        return policy


@dataclass(frozen=True, slots=True)
class ActivationDiagnostics:
    """strength population全体でのlearned activation / fallback集計。"""

    policy_instance_count: int
    total_decisions: int
    total_activations: int
    total_scaffold_fallbacks: int
    total_support_fallbacks: int

    @property
    def activation_rate(self) -> float:
        return self.total_activations / self.total_decisions

    @property
    def scaffold_fallback_rate(self) -> float:
        return self.total_scaffold_fallbacks / self.total_decisions

    @property
    def support_fallback_rate(self) -> float:
        return self.total_support_fallbacks / self.total_decisions

    def to_document(self) -> dict[str, object]:
        return {
            "policy_instance_count": self.policy_instance_count,
            "total_decisions": self.total_decisions,
            "total_activations": self.total_activations,
            "activation_rate": self.activation_rate,
            "total_scaffold_fallbacks": self.total_scaffold_fallbacks,
            "scaffold_fallback_rate": self.scaffold_fallback_rate,
            "total_support_fallbacks": self.total_support_fallbacks,
            "support_fallback_rate": self.support_fallback_rate,
        }


def _collect_activation_diagnostics(
    instances: list[HybridPolicy],
) -> ActivationDiagnostics:
    total_decisions = sum(len(policy.samples) for policy in instances)
    if total_decisions == 0:
        raise OfflineQStrengthError("strength screen produced no decisions to diagnose")
    return ActivationDiagnostics(
        policy_instance_count=len(instances),
        total_decisions=total_decisions,
        total_activations=sum(policy.activation_count for policy in instances),
        total_scaffold_fallbacks=sum(
            policy.scaffold_fallback_count for policy in instances
        ),
        total_support_fallbacks=sum(
            policy.support_fallback_count for policy in instances
        ),
    )


def build_specs(
    retained: RetainedCandidates,
) -> tuple[PolicySpec, PolicySpec, _PolicyInstanceRegistry, _PolicyInstanceRegistry]:
    """retained candidateから、同一support setを持つQ / BC hybrid specを作る。

    support setのcanonical digestを両方のPolicySpec identityへ明示的に
    埋め込む。Q checkpointのsupported_indicesを差し替えると、Q identityだけ
    でなくBC identityも変わるため、「同じbaseline identityのままBC hybridの
    fallback境界を変える」ことができない。
    """
    supported = retained.q_checkpoint.supported_indices
    digest = support_set_identity(supported)
    q_runtime = create_q_hybrid_runtime(
        retained.q_checkpoint.path, supported_indices=supported
    )
    bc_runtime = create_bc_hybrid_runtime(
        retained.bc_checkpoint.path, supported_indices=supported
    )
    q_registry = _PolicyInstanceRegistry(q_runtime.create_policy)
    bc_registry = _PolicyInstanceRegistry(bc_runtime.create_policy)
    candidate = PolicySpec(
        identity=(
            f"{CANDIDATE_IDENTITY_PREFIX}{retained.q_checkpoint.identity}"
            f"+support:{digest}"
        ),
        factory=q_registry.create_policy,
    )
    baseline = PolicySpec(
        identity=(
            f"{BASELINE_IDENTITY_PREFIX}{retained.bc_checkpoint.identity}"
            f"+support:{digest}"
        ),
        factory=bc_registry.create_policy,
    )
    return candidate, baseline, q_registry, bc_registry


def build_screening_plan(
    retained: RetainedCandidates,
) -> tuple[SingleRoundEvaluationPlan, _PolicyInstanceRegistry, _PolicyInstanceRegistry]:
    """locked ordered seeds (281..305) でQ-vs-BC ABBB planを組み立てる。"""
    candidate, baseline, q_registry, bc_registry = build_specs(retained)
    for seed in STRENGTH_SCREEN_SEEDS:
        require_screening_seed(seed)
    plan = SingleRoundEvaluationPlan(
        candidate=candidate, baseline=baseline, seeds=STRENGTH_SCREEN_SEEDS
    )
    return plan, q_registry, bc_registry


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
    candidate_diagnostics: ActivationDiagnostics
    baseline_diagnostics: ActivationDiagnostics
    wall_clock_seconds: float
    cpu_seconds: float

    def to_document(self) -> dict[str, object]:
        return {
            "candidate_identity": self.artifact.plan.candidate_identity,
            "baseline_identity": self.artifact.plan.baseline_identity,
            "outcome": self.outcome.value,
            "candidate_diagnostics": self.candidate_diagnostics.to_document(),
            "baseline_diagnostics": self.baseline_diagnostics.to_document(),
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
    plan, q_registry, bc_registry = build_screening_plan(retained)

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
        candidate_diagnostics=_collect_activation_diagnostics(q_registry.instances),
        baseline_diagnostics=_collect_activation_diagnostics(bc_registry.instances),
        wall_clock_seconds=wall_clock_seconds,
        cpu_seconds=cpu_seconds,
    )


__all__ = [
    "BASELINE_IDENTITY_PREFIX",
    "CANDIDATE_IDENTITY_PREFIX",
    "ActivationDiagnostics",
    "OfflineQStrengthError",
    "StrengthMeasurement",
    "build_screening_plan",
    "build_specs",
    "classify_value_q_signal",
    "run_strength_screen",
]
