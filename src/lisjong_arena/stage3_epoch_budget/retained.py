"""#150 retained artifactのexact identity readback。

本childは新規generationを持たない。E40 armも、E80 armのTRAIN corpusも、
すべて#150が残したimmutable artifactから来る。

```text
C:\\Dev\\lisjong-artifacts\\issue-150-phase10\\
    execution-lock.json    #150 execution lock (identity a978f6de…)
    population/            Phase 4 raw corpus + Phase 5 dataset
    S64/                   E40 arm (manifest.json + weights.pt)
```

loaderはIssue #157がlockしたexact identityと一致しないartifactを拒否する。
silent substitutionも再生成もreplacement seedも行わない。exact retained
artifactが利用できない場合、このchildは進まず停止する。

```text
population identity   e59410ed5b25e5ba672a35e256cfa91933336811734f91fe6d5fae94788570f7
raw corpus identity   bffa992cb287ea586eca86c03742f0eb12c9ccd6d7769ada9c528e4c4c24dba2
dataset identity      fbfa8ad7754126595fd7433c73651786103c22584d602d2919dff6d179e01646
S64 weights           71f5ff5bf39077d9be38a99de2a3ff692349e7b58e994df70ec238ca5c59a0c1
```

```text
TRAIN        360..423   64 hanchan   32,726 anchors
VALIDATION   424..439   16 hanchan    7,932 anchors
formal TEST  none
```

readbackは#150 loaderをthin reuseする。`load_population()`はpersisted raw
corpusとpersisted datasetを実際に読み直してdatasetをraw corpusから再導出し、
`load_model()`はcheckpointをlocked S2へ`strict=True`でloadする。#157側は
その上に、Issueがlockしたidentity・split membership・baseline budgetの一致を
重ねる。#150 protocol / validator / result documentは変更しない。
"""

from dataclasses import dataclass
from pathlib import Path

from lisjong_arena.stage3_scale_learning_curve.generation import load_population
from lisjong_arena.stage3_scale_learning_curve.lock import validate_lock
from lisjong_arena.stage3_scale_learning_curve.protocol import (
    TRAIN_SEEDS,
    VALIDATION_SEEDS,
)

from .protocol import (
    BASELINE_ARM,
    BASELINE_MAX_EPOCHS,
    PATIENCE,
    RETAINED_DATASET_IDENTITY,
    RETAINED_EXECUTION_LOCK_IDENTITY,
    RETAINED_POPULATION_IDENTITY,
    RETAINED_POPULATION_RECIPE,
    RETAINED_RAW_CORPUS_IDENTITY,
    RETAINED_SCALE,
    RETAINED_SELECTED_EPOCH,
    RETAINED_TRAIN_ANCHORS,
    RETAINED_TRAIN_HANCHAN,
    RETAINED_VALIDATION_ANCHORS,
    RETAINED_VALIDATION_HANCHAN,
    RETAINED_WEIGHTS_SHA256,
    BudgetError,
    baseline_training_lock,
    exact,
    identity,
)

LOCK_FILENAME = "execution-lock.json"
POPULATION_DIRNAME = "population"
MODEL_DIRNAME = RETAINED_SCALE


@dataclass(frozen=True, slots=True)
class RetainedS64:
    """#150 S64 corpusとE40 armのreadback結果。"""

    lock: dict[str, object]
    population: dict[str, object]
    raw: object
    dataset: object
    manifest: dict[str, object]


def retained_value() -> dict[str, object]:
    """Issue #157がlockしたretained artifact identityとsplit membership。

    execution lockがこのvalueを持つので、後からどのartifactを入力にしたかを
    再導出できる。
    """
    return {
        "source_issue": "lisbun/lisjong-arena#150",
        "scale": RETAINED_SCALE,
        "baseline_arm": BASELINE_ARM,
        "execution_lock_identity": RETAINED_EXECUTION_LOCK_IDENTITY,
        "population_identity": RETAINED_POPULATION_IDENTITY,
        "raw_corpus_identity": RETAINED_RAW_CORPUS_IDENTITY,
        "dataset_identity": RETAINED_DATASET_IDENTITY,
        "weights_sha256": RETAINED_WEIGHTS_SHA256,
        "train_seeds": list(TRAIN_SEEDS),
        "validation_seeds": list(VALIDATION_SEEDS),
        "train_hanchan": RETAINED_TRAIN_HANCHAN,
        "validation_hanchan": RETAINED_VALIDATION_HANCHAN,
        "train_anchors": RETAINED_TRAIN_ANCHORS,
        "validation_anchors": RETAINED_VALIDATION_ANCHORS,
        "test_partition_present": False,
        "population_recipe": RETAINED_POPULATION_RECIPE,
        "baseline_selected_epoch": RETAINED_SELECTED_EPOCH,
        "baseline_max_epochs": BASELINE_MAX_EPOCHS,
        "regenerated": False,
        "baseline_retrained": False,
    }


def load_retained_lock(root: str | Path) -> dict[str, object]:
    """#150 execution lockを読み、lockedなidentityと一致することを要求する。"""
    import json

    path = Path(root) / LOCK_FILENAME
    if not path.is_file():
        raise BudgetError(f"retained #150 execution lock is missing: {path}")
    lock = validate_lock(json.loads(path.read_bytes()))
    exact(
        identity(lock),
        RETAINED_EXECUTION_LOCK_IDENTITY,
        "retained #150 execution lock identity",
    )
    return lock


def validate_retained_population(population: dict[str, object]) -> dict[str, object]:
    """population manifestのidentityとsplit membershipをlocked値へ固定する。"""
    for name, expected in (
        ("population_identity", RETAINED_POPULATION_IDENTITY),
        ("raw_corpus_identity", RETAINED_RAW_CORPUS_IDENTITY),
        ("dataset_identity", RETAINED_DATASET_IDENTITY),
    ):
        exact(population[name], expected, f"retained {name}")
    exact(
        population["population_plan"]["train_seeds"],
        list(TRAIN_SEEDS),
        "retained TRAIN membership",
    )
    exact(
        population["population_plan"]["validation_seeds"],
        list(VALIDATION_SEEDS),
        "retained VALIDATION membership",
    )
    exact(
        population["population_plan"]["test_partition_present"],
        False,
        "retained formal TEST partition",
    )
    anchors = population["evidence"]["anchors_by_seed"]
    exact(
        sum(len(anchors[str(seed)]) for seed in TRAIN_SEEDS),
        RETAINED_TRAIN_ANCHORS,
        "retained TRAIN anchor count",
    )
    exact(
        sum(len(anchors[str(seed)]) for seed in VALIDATION_SEEDS),
        RETAINED_VALIDATION_ANCHORS,
        "retained VALIDATION anchor count",
    )
    return population


def validate_retained_manifest(manifest: dict[str, object]) -> dict[str, object]:
    """E40 armのmodel manifestをlocked S64 identityとbaseline budgetへ固定する。

    manifest自体の内部contract（TRAIN subset binding、anchor membership、
    checkpoint再導出、weights digest）は#150 `load_model()`が既に検証して
    いる。ここではIssue #157がlockした値との一致だけを重ねる。
    """
    exact(manifest["scale"], RETAINED_SCALE, "retained model scale")
    exact(
        manifest["execution_lock_identity"],
        RETAINED_EXECUTION_LOCK_IDENTITY,
        "retained model execution lock binding",
    )
    exact(
        manifest["weights_sha256"], RETAINED_WEIGHTS_SHA256, "retained weights digest"
    )
    for name, expected in (
        ("population_identity", RETAINED_POPULATION_IDENTITY),
        ("raw_corpus_identity", RETAINED_RAW_CORPUS_IDENTITY),
        ("dataset_identity", RETAINED_DATASET_IDENTITY),
    ):
        exact(manifest["subset"][name], expected, f"retained model {name}")
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
        manifest["selected_epoch"], RETAINED_SELECTED_EPOCH, "retained selected epoch"
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


def load_retained(root: str | Path) -> RetainedS64:
    """#150 artifact rootをstrict readbackし、E40 armを確定する。

    corpus / datasetを再生成しない。E40を再trainingしない。checkpointは
    #150 `load_model()`経由でlocked S2へstrict loadしてから採用する。
    """
    from lisjong_arena.stage3_scale_learning_curve.artifact import load_model

    root = Path(root)
    lock = load_retained_lock(root)
    population, raw, dataset = load_population(root / POPULATION_DIRNAME, lock)
    validate_retained_population(population)
    _model, manifest = load_model(root / MODEL_DIRNAME, population, lock)
    validate_retained_manifest(manifest)
    return RetainedS64(
        lock=lock, population=population, raw=raw, dataset=dataset, manifest=manifest
    )


__all__ = [
    "LOCK_FILENAME",
    "MODEL_DIRNAME",
    "POPULATION_DIRNAME",
    "RetainedS64",
    "load_retained",
    "load_retained_lock",
    "retained_value",
    "validate_retained_manifest",
    "validate_retained_population",
]
