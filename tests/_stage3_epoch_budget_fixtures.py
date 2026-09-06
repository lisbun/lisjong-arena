"""Arena #157 epoch-budget adequacy fixtures。

E80のlarge trainingはunit testへ入れない。ここで固定するのはretained artifact
identity、execution lock、single-axis contract、determinism gate、paired
comparison、exhaustive outcome、artifact re-derivationの境界だけである。

population / #150 execution lock / E40 manifestは#150 fixtureをそのまま再利用し、
#157側はそこへE80 arm、budget lock、budget resultを重ねる。#150 fixture
semanticsは変更しない。
"""

from contextlib import ExitStack, contextmanager
from unittest.mock import patch

from _stage3_scale_learning_curve_fixtures import (
    ARENA_EXECUTION_REVISION,
    evaluation_value,
    lock_value,
    model_manifest,
    population_manifest,
    provenance_value,
    runtime_value,
)

from lisjong_arena.stage3_epoch_budget import result as budget_result
from lisjong_arena.stage3_epoch_budget.lock import comparison_semantics
from lisjong_arena.stage3_epoch_budget.protocol import (
    BASELINE_ARENA_REVISION,
    BASELINE_ARM,
    BASELINE_MAX_EPOCHS,
    BUDGET_ARM,
    EXECUTION_DECISION,
    ROLE,
    SCHEMA,
    budget_training_lock,
    identity,
)
from lisjong_arena.stage3_epoch_budget.result import assemble_result
from lisjong_arena.stage3_epoch_budget.retained import retained_value
from lisjong_arena.stage3_scale_learning_curve.artifact import (
    selected_epoch_from_history,
)
from lisjong_arena.stage3_scale_learning_curve.protocol import VALIDATION_SEEDS

BUDGET_ARENA_REVISION = "4" * 40
"""#157 execution revision。#150実行時 (`ARENA_EXECUTION_REVISION`) とは違う。"""

RETAINED_MAE = 0.40
BUDGET_MAE = 0.38
BASELINE_SNAPSHOT_MAE = 0.48

IMPROVING_TAIL = [round(0.40 - 0.001 * offset, 12) for offset in range(1, 21)]
"""epoch 41..60で単調に改善するtail。selected epochは60になる。"""

WORSENING_TAIL = [round(0.40 + 0.001 * offset, 12) for offset in range(1, 7)]
"""epoch 41..46で改善しないtail。selected epochは40のままになる。"""


def retained_lock_value(**overrides) -> dict:
    """#150 execution lock fixture。#157はこれを入力evidenceとして持つ。"""
    return lock_value(**overrides)


def loss_history(count: int, *, final_mae: float, start: float = 0.5) -> list:
    """単調に改善するloss history。最終epochがselectedになる。"""
    step = (start - final_mae) / count
    return [
        {
            "epoch": epoch,
            "train_mse": round(start + 0.1 - step * epoch, 12),
            "validation_mae": (
                final_mae if epoch == count else round(start - step * epoch, 12)
            ),
        }
        for epoch in range(1, count + 1)
    ]


def budget_lock_value(**overrides) -> dict:
    """well-formedな#157 execution lock。

    live runtimeを要求しないので、`validate_lock()`側の境界だけを固定できる。
    """
    runtime = runtime_value()
    retained_runtime = runtime_value()
    revisions = provenance_value()["source_revisions"]
    value = {
        "schema": SCHEMA + "/execution-lock",
        "baseline_arena_revision": BASELINE_ARENA_REVISION,
        "retained": retained_value(),
        "retained_runtime": retained_runtime,
        "platform_matches_retained": (
            runtime["platform"] == retained_runtime["platform"]
        ),
        "budget_training_lock": budget_training_lock(),
        "comparison": comparison_semantics(),
        "provenance": provenance_value(
            source_revisions={
                "lisjong": revisions["lisjong"],
                "lisjong_engine": revisions["lisjong_engine"],
                "lisjong_arena": BUDGET_ARENA_REVISION,
            }
        ),
        "runtime": runtime,
        "artifact_audit": "Issue #157 retained artifact audit recorded 2026-09-06",
        "result_exposed": False,
        "execution_decision": EXECUTION_DECISION,
    }
    value.update(overrides)
    return value


def evaluation_cell(
    population: dict,
    *,
    per_game_mae,
    canonical_mae: float,
    baseline: float = BASELINE_SNAPSHOT_MAE,
    physical_passed: bool = True,
) -> dict:
    """per-hanchan MAEを個別に与えられるevaluation cell。

    depth diagnostics、physical consistency、inference blockは#150 fixtureの
    ものをそのまま使う。conditional-uniform baselineはarm間で共有する。
    """
    cell = evaluation_value(
        population,
        mae=canonical_mae,
        baseline=baseline,
        physical_passed=physical_passed,
    )
    if type(per_game_mae) in (int, float):
        per_game_mae = [float(per_game_mae)] * len(VALIDATION_SEEDS)
    rows = [
        {**row, "candidate_mae": mae, "delta_mae": row["snapshot_mae"] - mae}
        for row, mae in zip(cell["per_game"], per_game_mae, strict=True)
    ]
    anchors = sum(row["sample_count"] for row in rows)
    cell["per_game"] = rows
    cell["pooled_mae"] = (
        sum(row["candidate_mae"] * row["sample_count"] for row in rows) / anchors
    )
    cell["canonical_pooled_mae"] = canonical_mae
    return cell


def retained_manifest_value(
    population: dict,
    retained_lock: dict,
    *,
    per_game_mae=RETAINED_MAE,
    canonical_mae: float = RETAINED_MAE,
    epochs: int = BASELINE_MAX_EPOCHS,
    **overrides,
) -> dict:
    """E40 arm（#150 S64 manifest）のfixture。"""
    history = loss_history(epochs, final_mae=canonical_mae)
    value = model_manifest(
        "S64",
        population,
        retained_lock,
        mae=canonical_mae,
        selected_epoch=selected_epoch_from_history(history),
        loss_history=history,
        evaluation=evaluation_cell(
            population, per_game_mae=per_game_mae, canonical_mae=canonical_mae
        ),
    )
    value.update(overrides)
    return value


def budget_manifest_value(
    population: dict,
    budget_lock: dict,
    retained: dict,
    *,
    tail_maes=None,
    per_game_mae=BUDGET_MAE,
    self_rollout_failures: int = 0,
    physical_passed: bool = True,
    **overrides,
) -> dict:
    """E80 armのfixture。

    先頭40 epochはretained historyをそのままcopyするので、defaultでは
    determinism gateが通る。`tail_maes`がepoch 41以降のVALIDATION MAEであり、
    selected epochとcanonical pooled MAEはそこから再導出する。
    """
    history = list(retained["loss_history"])
    for offset, mae in enumerate(IMPROVING_TAIL if tail_maes is None else tail_maes, 1):
        history.append(
            {
                "epoch": BASELINE_MAX_EPOCHS + offset,
                "train_mse": round(0.3 - offset * 1e-4, 12),
                "validation_mae": mae,
            }
        )
    selected = selected_epoch_from_history(history)
    canonical = history[selected - 1]["validation_mae"]
    value = {
        "schema": SCHEMA + "/model",
        "role": ROLE,
        "execution_lock_identity": identity(budget_lock),
        "arm": BUDGET_ARM,
        "retained": retained_value(),
        "subset": retained["subset"],
        "train_anchor_identities": retained["train_anchor_identities"],
        "full_inventory": population["evidence"]["inventory"],
        "training_lock": budget_training_lock(),
        "selected_epoch": selected,
        "loss_history": history,
        "self_rollout_failure_count": self_rollout_failures,
        "evaluation": evaluation_cell(
            population,
            per_game_mae=per_game_mae,
            canonical_mae=canonical,
            physical_passed=physical_passed,
        ),
        "cost": {
            "training_cpu_seconds": 10_300.0,
            "training_wall_seconds": 10_450.0,
            "peak_process_ram_bytes": 4_000_000_000,
        },
        "runtime": budget_lock["runtime"],
        "weights_bytes": 1_839_437,
        "weights_sha256": "b" * 64,
    }
    value.update(overrides)
    return value


@contextmanager
def locked_identities_match(retained_lock: dict, population: dict, retained: dict):
    """syntheticなfixture identityを、locked retained identityとして扱わせる。

    `retained_artifact_gate()`はIssue #157がlockした実artifactのdigestと比較
    する。unit testは実artifactを持たないので、そのgateが見るconstantだけを
    fixture identityへ向ける。gateのlogicもdecision ruleも書き換えない。
    """
    targets = {
        "RETAINED_EXECUTION_LOCK_IDENTITY": identity(retained_lock),
        "RETAINED_POPULATION_IDENTITY": population["population_identity"],
        "RETAINED_RAW_CORPUS_IDENTITY": population["raw_corpus_identity"],
        "RETAINED_DATASET_IDENTITY": population["dataset_identity"],
        "RETAINED_WEIGHTS_SHA256": retained["weights_sha256"],
        "RETAINED_SELECTED_EPOCH": retained["selected_epoch"],
    }
    with ExitStack() as stack:
        for name, value in targets.items():
            stack.enter_context(patch.object(budget_result, name, value))
        yield


def budget_result_value(
    retained_lock: dict,
    population: dict,
    retained: dict,
    budget: dict,
    lock: dict,
) -> dict:
    """内部整合した#157 result value。

    outcome / gates / comparisonはfixtureが名乗らず、`assemble_result()`が
    recorded evidenceから再導出する。
    """
    return assemble_result(retained_lock, population, retained, budget, lock)


__all__ = [
    "ARENA_EXECUTION_REVISION",
    "BASELINE_ARM",
    "BASELINE_SNAPSHOT_MAE",
    "BUDGET_ARENA_REVISION",
    "BUDGET_ARM",
    "BUDGET_MAE",
    "IMPROVING_TAIL",
    "RETAINED_MAE",
    "WORSENING_TAIL",
    "budget_lock_value",
    "budget_manifest_value",
    "budget_result_value",
    "evaluation_cell",
    "locked_identities_match",
    "loss_history",
    "population_manifest",
    "retained_lock_value",
    "retained_manifest_value",
]
