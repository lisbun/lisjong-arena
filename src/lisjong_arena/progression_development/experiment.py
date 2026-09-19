"""Issue #252 Phase B — gate済みfeasibility recordの下でだけ走るorchestration。

Phase Bは``P vs T x3``と``C vs T x3``の2 armであり、同じordered seedsと同じ
rotation shapeを使う。直接``P vs C x3``へは変更しない。

## entry pointのgate

``run_phase_b_development()``はPhase A feasibility recordをstrict readし、

```text
gate_passed == true
selected_worker_count が record に存在する
```

でなければ実行しない。gateを読まずにPhase Bを始める経路は持たない。worker数は
Phase Aのtiming / correctness evidenceからのみ来る(callerは指定できない)。

## このmoduleが所有しないもの

game runner、rotation、artifact schema、統計定義は既存Arena contractが所有
する。ここは2 armの実行順序、write-once保存、strict readback、paired result
生成をつなぐだけである。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from lisjong_arena._execution_safety import require_new_artifact_destinations
from lisjong_arena.model import (
    SingleRoundEvaluationPlan,
    SingleRoundEvaluationResult,
)
from lisjong_arena.paired_evaluation import load_arm_artifact
from lisjong_arena.single_round_artifact import (
    SingleRoundExecutionProvenance,
    SingleRoundStrengthArtifact,
    save_single_round_artifact,
)

from .feasibility import (
    FeasibilityRecord,
    default_execute,
    load_feasibility_record,
    require_passed_gate,
)
from .lock import (
    load_lock_document,
    require_live_execution_target,
    require_locked_destination,
)
from .paired import (
    PairedResultError,
    build_paired_result,
    save_paired_result,
    verify_paired_result,
)
from .protocol import (
    MAX_STEPS,
    PHASE_B_GAMES_PER_ARM,
    PHASE_B_SEEDS,
    candidate_spec,
    comparator_spec,
    parent_spec,
    require_exact_candidate_semantics,
    require_phase_b_population,
)


@dataclass(frozen=True, slots=True)
class PhaseBOutcome:
    """Phase Bが残した成果物の位置とclassified result。"""

    worker_count: int
    candidate_artifact_path: Path
    parent_artifact_path: Path
    paired_result_path: Path
    paired_result: dict[str, object]

    @property
    def classification(self) -> dict[str, object]:
        return dict(self.paired_result["classification"])  # type: ignore[arg-type]


def build_arm_plan(*, candidate_arm: bool) -> SingleRoundEvaluationPlan:
    """1 armぶんのlocked planを作る。

    candidate armは``P vs T x3``、parent armは``C vs T x3``であり、seeds、
    rotation shape、``max_steps``は両armで同一のprotocol constantである。
    """
    seeds = require_phase_b_population(PHASE_B_SEEDS)
    return SingleRoundEvaluationPlan(
        candidate=candidate_spec() if candidate_arm else parent_spec(),
        baseline=comparator_spec(),
        seeds=seeds,
        max_steps=MAX_STEPS,
    )


def _run_arm(
    plan: SingleRoundEvaluationPlan,
    *,
    worker_count: int,
    destination: Path,
    execute: Callable[..., SingleRoundEvaluationResult],
    expected_provenance: SingleRoundExecutionProvenance,
) -> SingleRoundStrengthArtifact:
    result = execute(plan, max_workers=worker_count, progress_callback=None)
    if len(result.game_results) != PHASE_B_GAMES_PER_ARM:
        raise PairedResultError(
            f"arm must produce exactly {PHASE_B_GAMES_PER_ARM} games, "
            f"got {len(result.game_results)}"
        )
    save_single_round_artifact(result, destination)
    artifact = load_arm_artifact(destination)
    if artifact.provenance != expected_provenance:
        # Phase Aのgate evidenceと違うlisjong / engine / Arena revisionで
        # development armを走らせない。
        raise PairedResultError(
            "arm execution provenance differs from the gated Phase A record"
        )
    return artifact


def run_phase_b_development(
    *,
    lock_path: str | Path,
    feasibility_record_path: str | Path,
    candidate_artifact_path: str | Path,
    parent_artifact_path: str | Path,
    paired_result_path: str | Path,
    execute: Callable[..., SingleRoundEvaluationResult] = default_execute,
) -> PhaseBOutcome:
    """lockとgate済みfeasibility recordの下で2 armを実行する。

    worker数はPhase A recordのselected worker settingだけから決まる。

    game 1より前に、lockのstrict read、live execution target(clean HEAD /
    provenance)との照合、Phase A recordのprovenanceとの照合、locked
    destinationとの一致、write-once preflightをすべて済ませる。いずれかが
    崩れていればevaluatorを1度も呼ばない。400局を走らせてwrite-once
    artifactを消費した後に失敗させない。
    """
    require_exact_candidate_semantics()

    # --- game 1より前のlock / provenance gate ------------------------------
    lock_document = load_lock_document(lock_path)
    live_provenance = require_live_execution_target(lock_document)

    record_path = require_locked_destination(
        lock_document, "feasibility_record", feasibility_record_path
    )
    record = load_feasibility_record(record_path)
    worker_count = require_passed_gate(record)
    if record.provenance != live_provenance:
        raise PairedResultError(
            "Phase A feasibility provenance differs from the live locked provenance"
        )

    destinations: dict[str, str | Path] = {
        name: require_locked_destination(lock_document, name, path)
        for name, path in (
            ("candidate_artifact", candidate_artifact_path),
            ("parent_artifact", parent_artifact_path),
            ("paired_result", paired_result_path),
        )
    }
    require_new_artifact_destinations(
        destinations,
        required_names=("candidate_artifact", "parent_artifact", "paired_result"),
    )

    candidate_artifact = _run_arm(
        build_arm_plan(candidate_arm=True),
        worker_count=worker_count,
        destination=Path(candidate_artifact_path),
        execute=execute,
        expected_provenance=record.provenance,
    )
    parent_artifact = _run_arm(
        build_arm_plan(candidate_arm=False),
        worker_count=worker_count,
        destination=Path(parent_artifact_path),
        execute=execute,
        expected_provenance=record.provenance,
    )

    document = build_paired_result(
        candidate_artifact=candidate_artifact,
        candidate_artifact_path=candidate_artifact_path,
        parent_artifact=parent_artifact,
        parent_artifact_path=parent_artifact_path,
        worker_count=worker_count,
    )
    save_paired_result(document, paired_result_path)
    # 最終verificationはself-consistencyだけでなく、保存済みarm artifactの
    # raw evidenceからpaired値・summary・classificationを再導出して突き合わせる。
    verified = verify_paired_result(
        paired_result_path,
        candidate_artifact_path=candidate_artifact_path,
        parent_artifact_path=parent_artifact_path,
    )
    return PhaseBOutcome(
        worker_count=worker_count,
        candidate_artifact_path=Path(candidate_artifact_path),
        parent_artifact_path=Path(parent_artifact_path),
        paired_result_path=Path(paired_result_path),
        paired_result=verified,
    )


def require_gate_before_phase_b(
    feasibility_record_path: str | Path,
) -> tuple[FeasibilityRecord, int]:
    """Phase B開始前のgate確認だけを行う(実行はしない)。"""
    record = load_feasibility_record(feasibility_record_path)
    return record, require_passed_gate(record)


__all__ = [
    "PhaseBOutcome",
    "build_arm_plan",
    "require_gate_before_phase_b",
    "run_phase_b_development",
]
