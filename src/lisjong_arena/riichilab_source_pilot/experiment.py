"""Issue #211のone-shot orchestration。

```text
Gate 0 (exact materialization)
    -> matched row budget (raw-game isolated / deterministic truncation)
    -> Arm Y / Arm R を同じtrainerで1回ずつ学習
    -> 両armのfrozen checkpointをstrict readback
    -> result exposure前にseed planをlock
    -> 1回のABBB comparison (seeds 23000..23099 / 400 games)
    -> exactly one predeclared outcome
```

失敗は近い値で代替せず、decision orderどおりのoutcomeへ落とす。result
exposure後のseed extension / rerun / rescue pathをこのmoduleは持たない。
"""

import time
from dataclasses import dataclass
from pathlib import Path

from lisjong_arena._artifact_io import canonical_json_text

from .artifact import (
    CHECKPOINTS_DIRNAME,
    RESULT_FILENAME,
    SEED_PLAN_FILENAME,
    STRENGTH_ARTIFACT_FILENAME,
    LoadedCheckpoint,
    load_checkpoint,
    load_result,
    require_retention_key,
    result_identity,
    save_checkpoint,
    save_result,
    save_seed_plan,
    seed_plan_document,
)
from .dataset import MaterializedSource, build_row_budget
from .errors import BudgetNotMatchableError, SourcePilotError
from .evaluation import create_arm_policy, run_evaluation
from .outcome import classify_outcome, interpretation_boundary
from .protocol import (
    ARM_R,
    ARM_Y,
    PROTOCOL_ID,
    RESULT_SCHEMA_VERSION,
    Arm,
    SourcePilotOutcome,
    plan_document,
    source_identity_block,
    verify_contract_identity,
)
from .training import (
    arm_r_source_document,
    arm_y_source_document,
    materialized_tensors,
    retained_tensors,
    train_arm,
    training_config_identity,
)


@dataclass(frozen=True, slots=True)
class SourcePilotRun:
    """publishされたartifact directoryと、そこからstrict-readしたresult。"""

    path: Path
    result: dict[str, object]
    checkpoints: dict[Arm, LoadedCheckpoint]

    @property
    def outcome(self) -> SourcePilotOutcome:
        return SourcePilotOutcome(self.result["outcome"])


def _blocked_result(
    *,
    outcome: SourcePilotOutcome,
    retention: dict[str, object],
    gate0: dict[str, object] | None,
    stop_reason: str,
) -> dict[str, object]:
    document: dict[str, object] = {
        "result_schema_version": RESULT_SCHEMA_VERSION,
        "protocol_id": PROTOCOL_ID,
        "plan": plan_document(),
        "sources": source_identity_block(),
        "retention": retention,
        "gate0": gate0,
        "budget": None,
        "arms": None,
        "strength": None,
        "outcome": outcome.value,
        "stop_reason": stop_reason,
        "interpretation_boundary": interpretation_boundary(),
    }
    document["result_identity"] = result_identity(document)
    return document


def run_source_pilot(
    *,
    arm_y_source,
    arm_r_source: MaterializedSource,
    destination: str | Path,
    backend: str,
    key: str,
    progress_callback=None,
) -> SourcePilotRun:
    """Issue #211をdecision orderどおりに1回実行し、artifactを公開する。"""
    verify_contract_identity()
    if not isinstance(arm_r_source, MaterializedSource):
        raise TypeError("arm_r_source must be a MaterializedSource")
    if type(backend) is not str or not backend:
        raise SourcePilotError("retention backend must be non-empty")
    require_retention_key(key)
    retention = {"backend": backend, "key": key}

    destination = Path(destination)
    if destination.exists():
        raise FileExistsError("source-pilot artifact destination already exists")
    destination.parent.mkdir(parents=True, exist_ok=True)

    gate0 = arm_r_source.report.to_document()
    if not arm_r_source.report.gate_passed:
        destination.mkdir(parents=True)
        save_result(
            destination / RESULT_FILENAME,
            _blocked_result(
                outcome=SourcePilotOutcome.SOURCE_MATERIALIZATION_BLOCKED,
                retention=retention,
                gate0=gate0,
                stop_reason=(
                    "the exact RiichiLab DecisionContext could not be materialized "
                    "for every decision opportunity without heuristic filling"
                ),
            ),
        )
        return SourcePilotRun(
            path=destination,
            result=load_result(destination / RESULT_FILENAME),
            checkpoints={},
        )

    try:
        budget = build_row_budget(arm_r_source)
    except BudgetNotMatchableError as error:
        destination.mkdir(parents=True)
        save_result(
            destination / RESULT_FILENAME,
            _blocked_result(
                outcome=SourcePilotOutcome.DATA_BUDGET_NOT_MATCHABLE,
                retention=retention,
                gate0=gate0,
                stop_reason=str(error),
            ),
        )
        return SourcePilotRun(
            path=destination,
            result=load_result(destination / RESULT_FILENAME),
            checkpoints={},
        )

    destination.mkdir(parents=True)
    checkpoint_root = destination / CHECKPOINTS_DIRNAME
    checkpoint_root.mkdir()

    arm_documents = {
        ARM_Y: arm_y_source_document(arm_y_source),
        ARM_R: arm_r_source_document(arm_r_source, budget),
    }
    arm_tensors = {
        ARM_Y: retained_tensors(arm_y_source),
        ARM_R: materialized_tensors(budget),
    }

    checkpoints: dict[Arm, LoadedCheckpoint] = {}
    arm_results: dict[str, object] = {}
    started = time.perf_counter()
    for arm in (ARM_Y, ARM_R):
        training = train_arm(arm, arm_tensors[arm], source_document=arm_documents[arm])
        checkpoint = save_checkpoint(checkpoint_root / arm.value, training)
        checkpoints[arm] = checkpoint
        arm_results[arm.value] = {
            "checkpoint": checkpoint.identity_document(),
            "train_row_count": training.train_row_count,
            "validation_row_count": training.validation_row_count,
            "diagnostics": training.diagnostics_document(),
        }

    # checkpointはここからもう一度strict readbackしたものだけをservingへ渡す。
    reloaded = {arm: load_checkpoint(path.path) for arm, path in checkpoints.items()}
    candidate = create_arm_policy(reloaded[ARM_R])
    baseline = create_arm_policy(reloaded[ARM_Y])

    # seed planはstrength result exposureより前にlockする。
    save_seed_plan(
        destination / SEED_PLAN_FILENAME,
        seed_plan_document(
            candidate_identity=candidate.identity,
            baseline_identity=baseline.identity,
        ),
    )

    measurement = run_evaluation(
        candidate,
        baseline,
        destination / STRENGTH_ARTIFACT_FILENAME,
        progress_callback=progress_callback,
    )
    strength = measurement.to_document()
    outcome = classify_outcome(
        protocol_valid=True,
        gate0_passed=True,
        budget_matched=True,
        interval_lower=strength["interval_lower"],
        interval_upper=strength["interval_upper"],
    )

    document: dict[str, object] = {
        "result_schema_version": RESULT_SCHEMA_VERSION,
        "protocol_id": PROTOCOL_ID,
        "plan": plan_document(),
        "sources": source_identity_block(),
        "retention": retention,
        "gate0": gate0,
        "budget": {
            "train_rows": len(budget.train_rows),
            "validation_rows": len(budget.validation_rows),
            "train_game_count": len(budget.train_game_ids),
            "validation_game_count": len(budget.validation_game_ids),
            "raw_game_partition_disjoint": True,
            "distribution": budget.distribution_document(),
        },
        "arms": {
            "training_config_identity": training_config_identity(),
            "same_trainer_for_both_arms": True,
            **arm_results,
        },
        "strength": strength,
        "outcome": outcome.value,
        "stop_reason": None,
        "interpretation_boundary": interpretation_boundary(),
        "runtime": {"total_wall_clock_seconds": time.perf_counter() - started},
    }
    document["result_identity"] = result_identity(document)
    save_result(destination / RESULT_FILENAME, document)
    return SourcePilotRun(
        path=destination,
        result=load_result(destination / RESULT_FILENAME),
        checkpoints=reloaded,
    )


def plan_text() -> str:
    """locked planのcanonical JSON text。"""
    return canonical_json_text(plan_document())


__all__ = ["SourcePilotRun", "plan_text", "run_source_pilot"]
