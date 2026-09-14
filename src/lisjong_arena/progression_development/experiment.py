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
from .paired import (
    PairedResultError,
    build_paired_result,
    load_arm_artifact,
    load_paired_result,
    save_paired_result,
)
from .protocol import (
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


def build_arm_plan(*, candidate_arm: bool, max_steps: int) -> SingleRoundEvaluationPlan:
    """1 armぶんのlocked planを作る。

    candidate armは``P vs T x3``、parent armは``C vs T x3``であり、seedsと
    rotation shapeは両armで同一である。
    """
    seeds = require_phase_b_population(PHASE_B_SEEDS)
    return SingleRoundEvaluationPlan(
        candidate=candidate_spec() if candidate_arm else parent_spec(),
        baseline=comparator_spec(),
        seeds=seeds,
        max_steps=max_steps,
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
    feasibility_record_path: str | Path,
    candidate_artifact_path: str | Path,
    parent_artifact_path: str | Path,
    paired_result_path: str | Path,
    max_steps: int = 10_000,
    execute: Callable[..., SingleRoundEvaluationResult] = default_execute,
) -> PhaseBOutcome:
    """gate済みfeasibility recordの下で2 armを実行し、paired resultを残す。

    worker数はPhase A recordのselected worker settingだけから決まる。gateが
    通っていないrecordではPhase Bを開始しない。
    """
    record = load_feasibility_record(feasibility_record_path)
    worker_count = require_passed_gate(record)
    require_exact_candidate_semantics()

    destinations: dict[str, str | Path] = {
        "candidate_artifact": Path(candidate_artifact_path),
        "parent_artifact": Path(parent_artifact_path),
        "paired_result": Path(paired_result_path),
    }
    require_new_artifact_destinations(
        destinations,
        required_names=("candidate_artifact", "parent_artifact", "paired_result"),
    )

    candidate_artifact = _run_arm(
        build_arm_plan(candidate_arm=True, max_steps=max_steps),
        worker_count=worker_count,
        destination=Path(candidate_artifact_path),
        execute=execute,
        expected_provenance=record.provenance,
    )
    parent_artifact = _run_arm(
        build_arm_plan(candidate_arm=False, max_steps=max_steps),
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
    verified = load_paired_result(paired_result_path)
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
