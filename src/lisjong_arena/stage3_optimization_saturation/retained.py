"""#150 corpusと#157 E80 armのexact identity readback。

本childは新規generationを持たない。E80 armも、E160 armのTRAIN corpusも、すべて
#150 / #157が残したimmutable artifactから来る。

```text
C:\\Dev\\lisjong-artifacts\\issue-150-phase10\\
    execution-lock.json        #150 execution lock (identity a978f6de…)
    population/                Phase 4 raw corpus + Phase 5 dataset
    S64/                       #157のE40 arm (E80 manifestのsubject evidence)

C:\\Dev\\lisjong-artifacts\\issue-157-epoch-budget\\
    execution-lock.json        #157 execution lock (identity 5331af88…)
    E80/                       E80 arm (manifest.json + weights.pt)
    epoch-budget-result.json   #157 result (identity 700d8efb…)
```

loaderはIssue #167がlockしたexact identityと一致しないartifactを拒否する。
silent substitutionも再生成もreplacement seedもE80 retrainingも行わない。exact
retained artifactが利用できない場合、このchildは進まず停止する。

```text
population identity      e59410ed5b25e5ba672a35e256cfa91933336811734f91fe6d5fae94788570f7
raw corpus identity      bffa992cb287ea586eca86c03742f0eb12c9ccd6d7769ada9c528e4c4c24dba2
dataset identity         fbfa8ad7754126595fd7433c73651786103c22584d602d2919dff6d179e01646
#157 execution lock      5331af88bb2531d2fe3d34ca41cbc0ffffd0c6aaab63b0032265c15b7cccf55c
#157 result              700d8efb087e161d09581a10fe0b87c43c51a0f4cdb1e786f6ecc5c2852600ea
E80 weights              2773fd8d61a5f8945c7663b959a1e1f0d9689e702bdb42ecd238c25ea9b30ea0
```

```text
TRAIN        360..423   64 hanchan   32,726 anchors
VALIDATION   424..439   16 hanchan    7,932 anchors
formal TEST  none
```

readbackは既存loaderをthin reuseする。#150側は#157 `load_retained()`が
（そのさらに下では#150 `load_population()` / `load_model()`が）担当し、#157側は
#157 `validate_lock()` / `load_model()` / `load_result()`が担当する。どちらも
persisted evidenceを実際に読み直し、checkpointをlocked S2へ`strict=True`で
loadし、resultをrecorded evidenceから再導出する。#167側はその上に、Issueが
lockしたidentity・split membership・baseline budget・full loss history digestの
一致を重ねる。#150 / #157 protocol / validator / result documentは変更しない。
"""

import hashlib
from dataclasses import dataclass
from pathlib import Path

from lisjong_arena.phase4_raw_corpus.codec import canonical_json_bytes
from lisjong_arena.stage3_epoch_budget.artifact import (
    load_model as load_predecessor_model,
)
from lisjong_arena.stage3_epoch_budget.artifact import (
    load_result as load_predecessor_result_file,
)
from lisjong_arena.stage3_epoch_budget.lock import (
    validate_lock as validate_predecessor_lock,
)
from lisjong_arena.stage3_epoch_budget.retained import load_retained as load_phase10
from lisjong_arena.stage3_scale_learning_curve.protocol import (
    TRAIN_SEEDS,
    VALIDATION_SEEDS,
)

from .protocol import (
    BASELINE_ARM,
    BASELINE_LOSS_HISTORY_DIGEST,
    BASELINE_MAX_EPOCHS,
    BASELINE_POOLED_MAE,
    BASELINE_SELECTED_EPOCH,
    BASELINE_WEIGHTS_SHA256,
    PATIENCE,
    PHASE10_EXECUTION_LOCK_IDENTITY,
    PHASE10_WEIGHTS_SHA256,
    PREDECESSOR_EXECUTION_LOCK_IDENTITY,
    PREDECESSOR_ISSUE,
    PREDECESSOR_OUTCOME,
    PREDECESSOR_RESULT_IDENTITY,
    RETAINED_DATASET_IDENTITY,
    RETAINED_POPULATION_IDENTITY,
    RETAINED_POPULATION_RECIPE,
    RETAINED_RAW_CORPUS_IDENTITY,
    RETAINED_SCALE,
    RETAINED_TRAIN_ANCHORS,
    RETAINED_TRAIN_HANCHAN,
    RETAINED_VALIDATION_ANCHORS,
    RETAINED_VALIDATION_HANCHAN,
    SATURATION_MAX_EPOCHS,
    SaturationError,
    baseline_training_lock,
    exact,
    identity,
)

LOCK_FILENAME = "execution-lock.json"
MODEL_DIRNAME = BASELINE_ARM
RESULT_FILENAME = "epoch-budget-result.json"


@dataclass(frozen=True, slots=True)
class RetainedEvidence:
    """#150 corpusと#157 E80 armのreadback結果。"""

    phase10_lock: dict[str, object]
    population: dict[str, object]
    raw: object
    dataset: object
    scale_manifest: dict[str, object]
    predecessor_lock: dict[str, object]
    baseline_manifest: dict[str, object]


def loss_history_digest(history: list) -> str:
    """loss historyのcanonical SHA-256。float toleranceを持たない。"""
    return hashlib.sha256(canonical_json_bytes(history)).hexdigest()


def retained_value() -> dict[str, object]:
    """Issue #167がlockしたretained artifact identityとsplit membership。

    execution lockがこのvalueを持つので、後からどのartifactを入力にしたかを
    再導出できる。
    """
    return {
        "corpus_issue": "lisbun/lisjong-arena#150",
        "predecessor_issue": PREDECESSOR_ISSUE,
        "predecessor_outcome": PREDECESSOR_OUTCOME,
        "scale": RETAINED_SCALE,
        "baseline_arm": BASELINE_ARM,
        "phase10_execution_lock_identity": PHASE10_EXECUTION_LOCK_IDENTITY,
        "phase10_weights_sha256": PHASE10_WEIGHTS_SHA256,
        "predecessor_execution_lock_identity": PREDECESSOR_EXECUTION_LOCK_IDENTITY,
        "predecessor_result_identity": PREDECESSOR_RESULT_IDENTITY,
        "population_identity": RETAINED_POPULATION_IDENTITY,
        "raw_corpus_identity": RETAINED_RAW_CORPUS_IDENTITY,
        "dataset_identity": RETAINED_DATASET_IDENTITY,
        "baseline_weights_sha256": BASELINE_WEIGHTS_SHA256,
        "baseline_loss_history_digest": BASELINE_LOSS_HISTORY_DIGEST,
        "baseline_pooled_mae": BASELINE_POOLED_MAE,
        "train_seeds": list(TRAIN_SEEDS),
        "validation_seeds": list(VALIDATION_SEEDS),
        "train_hanchan": RETAINED_TRAIN_HANCHAN,
        "validation_hanchan": RETAINED_VALIDATION_HANCHAN,
        "train_anchors": RETAINED_TRAIN_ANCHORS,
        "validation_anchors": RETAINED_VALIDATION_ANCHORS,
        "test_partition_present": False,
        "population_recipe": RETAINED_POPULATION_RECIPE,
        "baseline_selected_epoch": BASELINE_SELECTED_EPOCH,
        "baseline_max_epochs": BASELINE_MAX_EPOCHS,
        "saturation_max_epochs": SATURATION_MAX_EPOCHS,
        "regenerated": False,
        "baseline_retrained": False,
        "resumed_from_baseline_checkpoint": False,
    }


def load_predecessor_lock(root: str | Path) -> dict[str, object]:
    """#157 execution lockを読み、lockedなidentityと一致することを要求する。"""
    import json

    path = Path(root) / LOCK_FILENAME
    if not path.is_file():
        raise SaturationError(f"retained #157 execution lock is missing: {path}")
    lock = validate_predecessor_lock(json.loads(path.read_bytes()))
    exact(
        identity(lock),
        PREDECESSOR_EXECUTION_LOCK_IDENTITY,
        "retained #157 execution lock identity",
    )
    return lock


def validate_baseline_manifest(manifest: dict[str, object]) -> dict[str, object]:
    """E80 armのmodel manifestをlocked #157 identityとbaseline budgetへ固定する。

    manifest自体の内部contract（TRAIN subset binding、anchor membership、
    checkpoint再導出、weights digest、locked S2へのstrict load）は#157
    `load_model()`が既に検証している。ここではIssue #167がlockした値との一致
    だけを重ねる。
    """
    exact(manifest["arm"], BASELINE_ARM, "retained baseline arm")
    exact(
        manifest["execution_lock_identity"],
        PREDECESSOR_EXECUTION_LOCK_IDENTITY,
        "retained model execution lock binding",
    )
    exact(
        manifest["weights_sha256"],
        BASELINE_WEIGHTS_SHA256,
        "retained baseline weights digest",
    )
    for name, expected in (
        ("population_identity", RETAINED_POPULATION_IDENTITY),
        ("raw_corpus_identity", RETAINED_RAW_CORPUS_IDENTITY),
        ("dataset_identity", RETAINED_DATASET_IDENTITY),
    ):
        exact(manifest["subset"][name], expected, f"retained baseline {name}")
    exact(manifest["training_lock"], baseline_training_lock(), "retained training lock")
    config = manifest["training_lock"]["training_config"]
    exact(config["max_epochs"], BASELINE_MAX_EPOCHS, "retained max_epochs")
    exact(config["patience"], PATIENCE, "retained patience")
    exact(
        len(manifest["loss_history"]),
        BASELINE_MAX_EPOCHS,
        "retained loss history length",
    )
    exact(
        loss_history_digest(manifest["loss_history"]),
        BASELINE_LOSS_HISTORY_DIGEST,
        "retained full loss history digest",
    )
    exact(
        manifest["selected_epoch"],
        BASELINE_SELECTED_EPOCH,
        "retained selected epoch",
    )
    exact(
        manifest["evaluation"]["pooled_mae"],
        BASELINE_POOLED_MAE,
        "retained pooled VALIDATION MAE",
    )
    exact(
        len(manifest["train_anchor_identities"]),
        RETAINED_TRAIN_ANCHORS,
        "retained TRAIN anchor membership",
    )
    exact(
        len(manifest["evaluation"]["validation_anchor_identities"]),
        RETAINED_VALIDATION_ANCHORS,
        "retained VALIDATION anchor membership",
    )
    return manifest


def load_retained(
    corpus_root: str | Path, predecessor_root: str | Path
) -> RetainedEvidence:
    """#150 corpus rootと#157 artifact rootをstrict readbackし、E80 armを確定する。

    corpus / datasetを再生成しない。E80を再trainingしない。checkpointは#157
    `load_model()`経由でlocked S2へstrict loadしてから採用する。
    """
    phase10 = load_phase10(corpus_root)
    exact(
        identity(phase10.lock),
        PHASE10_EXECUTION_LOCK_IDENTITY,
        "retained #150 execution lock identity",
    )
    predecessor_lock = load_predecessor_lock(predecessor_root)
    _model, baseline_manifest = load_predecessor_model(
        Path(predecessor_root) / MODEL_DIRNAME,
        phase10.population,
        predecessor_lock,
        phase10.manifest,
    )
    validate_baseline_manifest(baseline_manifest)
    return RetainedEvidence(
        phase10_lock=phase10.lock,
        population=phase10.population,
        raw=phase10.raw,
        dataset=phase10.dataset,
        scale_manifest=phase10.manifest,
        predecessor_lock=predecessor_lock,
        baseline_manifest=baseline_manifest,
    )


def load_predecessor_result(
    predecessor_root: str | Path, predecessor_lock: dict[str, object]
) -> dict[str, object]:
    """#157 resultをrecorded evidenceから再導出し、locked identityへ固定する。

    E80 armのprovenanceをmanifestの自己整合だけで済ませないために読む。#157
    `load_result()`はresultのlogical identityを再計算し、そのvalue全体を
    `assemble_result()`でrecorded evidenceから再導出する。#167側はその上に、
    Issueがlockしたresult identityとpredecessor outcomeの一致を重ねる。
    """
    path = Path(predecessor_root) / RESULT_FILENAME
    if not path.is_file():
        raise SaturationError(f"retained #157 result is missing: {path}")
    value = load_predecessor_result_file(path, predecessor_lock)
    exact(
        value["result_identity"],
        PREDECESSOR_RESULT_IDENTITY,
        "retained #157 result identity",
    )
    exact(value["outcome"], PREDECESSOR_OUTCOME, "retained #157 outcome")
    return value


__all__ = [
    "LOCK_FILENAME",
    "MODEL_DIRNAME",
    "RESULT_FILENAME",
    "RetainedEvidence",
    "load_predecessor_lock",
    "load_predecessor_result",
    "load_retained",
    "loss_history_digest",
    "retained_value",
    "validate_baseline_manifest",
]
