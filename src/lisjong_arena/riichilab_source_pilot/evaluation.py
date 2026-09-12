"""locked R-vs-Y development evaluationのorchestration。

ABBB protocol semantics、rotation、seed-block statistics、artifact schemaは
再実装しない。所有するのは次のthin orchestrationだけである。

```text
frozen Arm R checkpoint  -> candidate PolicySpec (learned-source-pilot-r:<id>)
frozen Arm Y checkpoint  -> baseline  PolicySpec (learned-source-pilot-y:<id>)
    -> 既存 SingleRoundEvaluationPlan (seeds 23000..23099 / 4p-red-single)
    -> 既存 run_single_round_evaluation()
    -> 既存 save_single_round_artifact() (immutable)
    -> 既存 loader + canonical aggregation によるreadback
    -> canonical candidate-vs-baseline seed-block delta + 95% interval
```

planning APIもexecution APIもseed引数を持たない。したがってresultを見て
からseedを追加・差し替える入口自体が存在しない。新しいstrength metricも
導入しない。
"""

from dataclasses import dataclass
from pathlib import Path

from lisjong_arena.model import PolicySpec, SingleRoundEvaluationPlan
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

from .artifact import LoadedCheckpoint
from .errors import SourcePilotProtocolError
from .protocol import (
    BASELINE_ARM,
    CANDIDATE_ARM,
    EVALUATION_GAME_COUNT,
    EVALUATION_GAME_MODE,
    EVALUATION_PROTOCOL,
    EVALUATION_SEEDS,
    SEED_BLOCK_COUNT,
    require_evaluation_seeds,
)
from .serving import SourcePilotRuntime, create_serving_runtime


@dataclass(frozen=True, slots=True)
class ArmPolicy:
    """1 armのfrozen checkpointへbindしたABBB policy spec。"""

    runtime: SourcePilotRuntime
    spec: PolicySpec
    checkpoint: LoadedCheckpoint

    @property
    def identity(self) -> str:
        return self.spec.identity


def create_arm_policy(checkpoint: LoadedCheckpoint) -> ArmPolicy:
    """strict-loadしたcheckpointからpolicy specを作る。

    checkpointは1回だけloadし、`spec.factory`はgame / seatごとにfresh
    Policy instanceを返す。
    """
    if not isinstance(checkpoint, LoadedCheckpoint):
        raise TypeError("checkpoint must be a LoadedCheckpoint")
    runtime = create_serving_runtime(checkpoint.arm, checkpoint)
    return ArmPolicy(
        runtime=runtime,
        spec=PolicySpec(
            identity=checkpoint.policy_identity, factory=runtime.create_policy
        ),
        checkpoint=checkpoint,
    )


def build_evaluation_plan(
    candidate: ArmPolicy, baseline: ArmPolicy
) -> SingleRoundEvaluationPlan:
    """locked seeds / locked armsでABBB planを組み立てる。"""
    for role, policy in (("candidate", candidate), ("baseline", baseline)):
        if not isinstance(policy, ArmPolicy):
            raise TypeError(f"{role} must be an ArmPolicy")
    if candidate.checkpoint.arm is not CANDIDATE_ARM:
        raise SourcePilotProtocolError("the candidate must be the locked candidate arm")
    if baseline.checkpoint.arm is not BASELINE_ARM:
        raise SourcePilotProtocolError("the baseline must be the locked baseline arm")
    if candidate.identity == baseline.identity:
        raise SourcePilotProtocolError("candidate and baseline identities must differ")
    plan = SingleRoundEvaluationPlan(
        candidate=candidate.spec,
        baseline=baseline.spec,
        seeds=EVALUATION_SEEDS,
    )
    require_evaluation_seeds(plan.seeds)
    # ``4p-red-single``は``lisjong_arena.single_round_evaluation``側の
    # protocol invariantであり、planのfieldではない。`protocol.py`が
    # ``SINGLE_ROUND_GAME_MODE``をそのままlocked valueとして取り込むことで
    # bindしている。
    return plan


@dataclass(frozen=True, slots=True)
class StrengthMeasurement:
    """1 comparisonのimmutable artifactと、raw resultから再生成したsummary。"""

    candidate_identity: str
    baseline_identity: str
    artifact: SingleRoundStrengthArtifact
    summary: SingleRoundStrengthSummary

    def to_document(self) -> dict[str, object]:
        statistics = self.summary.seed_block_statistics
        return {
            "evaluation_protocol": EVALUATION_PROTOCOL,
            "game_mode": EVALUATION_GAME_MODE,
            "candidate_identity": self.candidate_identity,
            "baseline_identity": self.baseline_identity,
            "seeds": list(EVALUATION_SEEDS),
            "seed_blocks": statistics.seed_block_count,
            "rotations_per_seed": ROTATION_COUNT,
            "games": len(self.artifact.game_results),
            "mean_seed_block_delta": statistics.mean_seed_block_delta,
            "sample_standard_deviation": statistics.sample_standard_deviation,
            "standard_error": statistics.standard_error,
            "interval_lower": statistics.normal_approx_95_interval_lower,
            "interval_upper": statistics.normal_approx_95_interval_upper,
            "positive_seed_blocks": statistics.positive_seed_block_count,
            "zero_seed_blocks": statistics.zero_seed_block_count,
            "negative_seed_blocks": statistics.negative_seed_block_count,
            "mean_candidate_game_delta": self.summary.mean_candidate_game_delta,
            "mean_baseline_score": self.summary.mean_baseline_score,
        }


def verify_strength_artifact(
    artifact: SingleRoundStrengthArtifact,
    *,
    candidate_identity: str,
    baseline_identity: str,
) -> SingleRoundStrengthSummary:
    """artifactがlocked protocol条件どおりであることをfail closedで確認する。"""
    plan = artifact.plan
    if plan.candidate_identity != candidate_identity:
        raise SourcePilotProtocolError(
            "artifact candidate identity is not the frozen Arm R identity"
        )
    if plan.baseline_identity != baseline_identity:
        raise SourcePilotProtocolError(
            "artifact baseline identity is not the frozen Arm Y identity"
        )
    require_evaluation_seeds(plan.seeds)
    if plan.game_mode != EVALUATION_GAME_MODE or plan.rotation_count != ROTATION_COUNT:
        raise SourcePilotProtocolError(
            "artifact protocol conditions are not the locked ones"
        )
    if len(artifact.game_results) != EVALUATION_GAME_COUNT:
        raise SourcePilotProtocolError(
            f"artifact must contain exactly {EVALUATION_GAME_COUNT} games"
        )
    summary = summarize_single_round_strength(
        aggregate_candidate_metrics(plan.candidate_identity, artifact.game_results),
        artifact.game_results,
    )
    if summary != artifact.summary:
        raise SourcePilotProtocolError(
            "the canonical summary regenerated from raw results differs from the "
            "artifact summary"
        )
    if summary.seed_block_statistics.seed_block_count != SEED_BLOCK_COUNT:
        raise SourcePilotProtocolError(
            "artifact seed-block count is not the locked one"
        )
    return summary


def run_evaluation(
    candidate: ArmPolicy,
    baseline: ArmPolicy,
    artifact_path: str | Path,
    *,
    progress_callback=None,
) -> StrengthMeasurement:
    """locked populationでABBB comparisonを1回だけ実行する。"""
    plan = build_evaluation_plan(candidate, baseline)
    result = run_single_round_evaluation(plan, progress_callback=progress_callback)
    path = Path(artifact_path)
    save_single_round_artifact(result, path)
    artifact = load_single_round_artifact(path)
    summary = verify_strength_artifact(
        artifact,
        candidate_identity=candidate.identity,
        baseline_identity=baseline.identity,
    )
    return StrengthMeasurement(
        candidate_identity=candidate.identity,
        baseline_identity=baseline.identity,
        artifact=artifact,
        summary=summary,
    )


__all__ = [
    "ArmPolicy",
    "StrengthMeasurement",
    "build_evaluation_plan",
    "create_arm_policy",
    "run_evaluation",
    "verify_strength_artifact",
]
