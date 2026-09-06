"""Arena #167 optimization-budget saturation fixtures。

E160のlarge trainingはunit testへ入れない。ここで固定するのはretained artifact
identity、execution lock、single-axis contract、determinism gate、paired
comparison、exhaustive outcome、saturation record、artifact re-derivationの
境界だけである。

population / #150 execution lock / S64 manifestは#150 fixtureを、E80 arm
manifestは#157 fixtureをそのまま再利用し、#167側はそこへE160 arm、saturation
lock、saturation resultを重ねる。#150 / #157 fixture semanticsは変更しない。
"""

from contextlib import ExitStack, contextmanager
from unittest.mock import patch

from _stage3_epoch_budget_fixtures import (
    BASELINE_SNAPSHOT_MAE,
    budget_lock_value,
    budget_manifest_value,
    loss_history,
    retained_lock_value,
    retained_manifest_value,
)
from _stage3_epoch_budget_fixtures import (
    evaluation_cell as budget_evaluation_cell,
)
from _stage3_scale_learning_curve_fixtures import (
    population_manifest,
    provenance_value,
    runtime_value,
)

from lisjong_arena.stage3_optimization_saturation import result as saturation_result
from lisjong_arena.stage3_optimization_saturation.lock import comparison_semantics
from lisjong_arena.stage3_optimization_saturation.protocol import (
    BASELINE_ARENA_REVISION,
    BASELINE_MAX_EPOCHS,
    EXECUTION_DECISION,
    ROLE,
    SATURATION_ARM,
    SCHEMA,
    baseline_training_lock,
    identity,
    saturation_training_lock,
)
from lisjong_arena.stage3_optimization_saturation.result import assemble_result
from lisjong_arena.stage3_optimization_saturation.retained import (
    loss_history_digest,
    retained_value,
)
from lisjong_arena.stage3_scale_learning_curve.artifact import (
    selected_epoch_from_history,
)

SATURATION_ARENA_REVISION = "7" * 40
"""#167 execution revision。#150 / #157実行時とは違う。"""

BASELINE_MAE = 0.38
SATURATION_MAE = 0.36

IMPROVING_TAIL = [round(0.38 - 0.0001 * offset, 12) for offset in range(1, 81)]
"""epoch 81..160で単調に改善するtail。selected epochは160になる。"""

MID_SATURATION_TAIL = [round(0.38 - 0.0001 * offset, 12) for offset in range(1, 21)] + [
    round(0.38 - 0.0001 * (20 - offset), 12) for offset in range(1, 7)
]
"""epoch 81..100で改善し、101..106で改善しないtail。

`patience = 6`で止まるので`epochs_run = 106` / `selected epoch = 100`になり、
hard capより前でsaturationが観測された状態を表す。
"""

NON_IMPROVING_TAIL = [round(0.38 + 0.0001 * offset, 12) for offset in range(1, 7)]
"""epoch 81..86で改善しないtail。selected epochは80のままになる。"""


def phase10_lock_value(**overrides) -> dict:
    """#150 execution lock fixture。#167はこれを入力evidenceとして持つ。"""
    return retained_lock_value(**overrides)


def scale_manifest_value(population: dict, phase10_lock: dict, **overrides) -> dict:
    """#150 S64 manifest fixture。E80 manifestのsubject evidenceである。"""
    return retained_manifest_value(population, phase10_lock, **overrides)


def predecessor_lock_value(**overrides) -> dict:
    """#157 execution lock fixture。"""
    return budget_lock_value(**overrides)


def evaluation_cell(population: dict, **arguments) -> dict:
    """#157 fixtureのevaluation cell構成をそのまま使う。"""
    return budget_evaluation_cell(population, **arguments)


def baseline_manifest_value(
    population: dict,
    predecessor_lock: dict,
    scale_manifest: dict,
    *,
    per_game_mae=BASELINE_MAE,
    canonical_mae: float = BASELINE_MAE,
    **overrides,
) -> dict:
    """E80 arm（#157 E80 manifest）のfixture。

    #157 fixtureは`tail_maes`でepoch 41以降を作るので、ここでは80 epochちょうど
    の単調に改善するhistoryを直接与え、最終epochがselectedになるようにする。
    """
    history = loss_history(BASELINE_MAX_EPOCHS, final_mae=canonical_mae)
    value = budget_manifest_value(
        population,
        predecessor_lock,
        scale_manifest,
        tail_maes=[],
        loss_history=history,
        selected_epoch=selected_epoch_from_history(history),
        evaluation=evaluation_cell(
            population, per_game_mae=per_game_mae, canonical_mae=canonical_mae
        ),
    )
    value.update(overrides)
    return value


def saturation_lock_value(**overrides) -> dict:
    """well-formedな#167 execution lock。

    live runtimeを要求しないので、`validate_lock()`側の境界だけを固定できる。
    """
    runtime = runtime_value()
    predecessor_runtime = runtime_value()
    revisions = provenance_value()["source_revisions"]
    value = {
        "schema": SCHEMA + "/execution-lock",
        "baseline_arena_revision": BASELINE_ARENA_REVISION,
        "retained": retained_value(),
        "predecessor_runtime": predecessor_runtime,
        "platform_matches_predecessor": (
            runtime["platform"] == predecessor_runtime["platform"]
        ),
        "baseline_training_lock": baseline_training_lock(),
        "saturation_training_lock": saturation_training_lock(),
        "comparison": comparison_semantics(),
        "provenance": provenance_value(
            source_revisions={
                "lisjong": revisions["lisjong"],
                "lisjong_engine": revisions["lisjong_engine"],
                "lisjong_arena": SATURATION_ARENA_REVISION,
            }
        ),
        "runtime": runtime,
        "artifact_audit": "Issue #167 retained artifact audit recorded 2026-09-07",
        "result_exposed": False,
        "execution_decision": EXECUTION_DECISION,
    }
    value.update(overrides)
    return value


def saturation_manifest_value(
    population: dict,
    saturation_lock: dict,
    baseline: dict,
    *,
    tail_maes=None,
    per_game_mae=SATURATION_MAE,
    self_rollout_failures: int = 0,
    physical_passed: bool = True,
    **overrides,
) -> dict:
    """E160 armのfixture。

    先頭80 epochはE80 historyをそのままcopyするので、defaultでは determinism
    gateが通る。`tail_maes`がepoch 81以降のVALIDATION MAEであり、selected epoch
    とcanonical pooled MAEはそこから再導出する。
    """
    history = list(baseline["loss_history"])
    for offset, mae in enumerate(IMPROVING_TAIL if tail_maes is None else tail_maes, 1):
        history.append(
            {
                "epoch": BASELINE_MAX_EPOCHS + offset,
                "train_mse": round(0.3 - offset * 1e-5, 12),
                "validation_mae": mae,
            }
        )
    selected = selected_epoch_from_history(history)
    canonical = history[selected - 1]["validation_mae"]
    value = {
        "schema": SCHEMA + "/model",
        "role": ROLE,
        "execution_lock_identity": identity(saturation_lock),
        "arm": SATURATION_ARM,
        "retained": retained_value(),
        "subset": baseline["subset"],
        "train_anchor_identities": baseline["train_anchor_identities"],
        "full_inventory": population["evidence"]["inventory"],
        "training_lock": saturation_training_lock(),
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
            "training_cpu_seconds": 17_800.0,
            "training_wall_seconds": 18_100.0,
            "peak_process_ram_bytes": 4_000_000_000,
        },
        "runtime": saturation_lock["runtime"],
        "weights_bytes": 1_839_437,
        "weights_sha256": "c" * 64,
    }
    value.update(overrides)
    return value


@contextmanager
def locked_identities_match(
    phase10_lock: dict,
    population: dict,
    scale_manifest: dict,
    predecessor_lock: dict,
    baseline: dict,
):
    """syntheticなfixture identityを、locked retained identityとして扱わせる。

    `retained_artifact_gate()`はIssue #167がlockした実artifactのdigestと比較
    する。unit testは実artifactを持たないので、そのgateが見るconstantだけを
    fixture identityへ向ける。gateのlogicもdecision ruleも書き換えない。
    """
    targets = {
        "PHASE10_EXECUTION_LOCK_IDENTITY": identity(phase10_lock),
        "PREDECESSOR_EXECUTION_LOCK_IDENTITY": identity(predecessor_lock),
        "RETAINED_POPULATION_IDENTITY": population["population_identity"],
        "RETAINED_RAW_CORPUS_IDENTITY": population["raw_corpus_identity"],
        "RETAINED_DATASET_IDENTITY": population["dataset_identity"],
        "BASELINE_WEIGHTS_SHA256": baseline["weights_sha256"],
        "BASELINE_SELECTED_EPOCH": baseline["selected_epoch"],
        "BASELINE_LOSS_HISTORY_DIGEST": loss_history_digest(baseline["loss_history"]),
    }
    with ExitStack() as stack:
        for name, value in targets.items():
            stack.enter_context(patch.object(saturation_result, name, value))
        yield


def saturation_result_value(
    phase10_lock: dict,
    population: dict,
    scale_manifest: dict,
    predecessor_lock: dict,
    baseline: dict,
    saturation: dict,
    lock: dict,
) -> dict:
    """内部整合した#167 result value。

    outcome / gates / comparison / saturationはfixtureが名乗らず、
    `assemble_result()`がrecorded evidenceから再導出する。
    """
    return assemble_result(
        phase10_lock,
        population,
        scale_manifest,
        predecessor_lock,
        baseline,
        saturation,
        lock,
    )


__all__ = [
    "BASELINE_MAE",
    "BASELINE_SNAPSHOT_MAE",
    "IMPROVING_TAIL",
    "MID_SATURATION_TAIL",
    "NON_IMPROVING_TAIL",
    "SATURATION_ARENA_REVISION",
    "SATURATION_MAE",
    "baseline_manifest_value",
    "evaluation_cell",
    "locked_identities_match",
    "loss_history",
    "phase10_lock_value",
    "population_manifest",
    "predecessor_lock_value",
    "saturation_lock_value",
    "saturation_manifest_value",
    "saturation_result_value",
    "scale_manifest_value",
]
