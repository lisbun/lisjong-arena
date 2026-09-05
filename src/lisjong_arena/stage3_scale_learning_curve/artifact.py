"""Phase 10 model / result artifactとそのstrict contract。

Phase 8 `phase8_sequential.artifact`はPhase 8 formal populationへ、
`stage3_entry_gate.artifact`は#131へ、`stage3_mix_pilot.artifact`は#148の
`330..353`へbindされたvalidatorである。どのhistorical constantsもvalidatorも
Phase 10のために書き換えないため、本childは別schema / 別identityのartifactを持つ。

artifactは`state_dict`だけを保存し、factory / callable / 任意codeを保存・復元
しない。既存destinationを上書きせず、内部矛盾はload時にfail closedする。
weightsとgenerated resultはGit repositoryへcommitしない。

## Model binding

model artifactは自分がどのexact TRAIN subsetから来たかを証明できる必要がある。
`population_identity`がSHA-256の形をしていることや、manifest内部でfieldが整合
していることでは足りない。したがって

```text
subset                    == subset_binding(scale, corpus, dataset, provenance)
train_anchor_identities   == そのsubset seedsのanchor identity（populationのevidence由来）
full_inventory            == population evidenceのinventory（BPTT policy共有の証拠）
training_lock             == locked training lock（scaleごとに変えられない）
selected_epoch            == Phase 8 checkpoint ruleでloss historyから再導出した値
loss_history[selected]    == evaluationのcanonical pooled MAE
```

をすべて要求する。TRAIN subsetを名乗り替えたmodel、scaleごとにconfigを変えた
model、evaluationとtraining historyが噛み合わないmodelはここで落ちる。
"""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from lisjong_arena.phase4_raw_corpus.codec import canonical_json_bytes
from lisjong_arena.phase8_sequential.protocol import checkpoint_improves

from .population import subset_binding
from .protocol import (
    ROLE,
    SCALES,
    SCHEMA,
    VALIDATION_SEEDS,
    ScaleError,
    digest,
    exact,
    finite,
    identity,
    train_seeds,
    training_lock,
)
from .result import validate_result

MANIFEST_FILENAME = "manifest.json"
WEIGHTS_FILENAME = "weights.pt"
MODEL_FIELDS = (
    "schema",
    "role",
    "execution_lock_identity",
    "scale",
    "subset",
    "train_anchor_identities",
    "full_inventory",
    "training_lock",
    "selected_epoch",
    "loss_history",
    "self_rollout_failure_count",
    "evaluation",
    "cost",
    "runtime",
    "weights_bytes",
    "weights_sha256",
)
MODEL_COST_FIELDS = ("training_cpu_seconds", "training_wall_seconds")


@dataclass(frozen=True, slots=True)
class LoadedScaleModel:
    manifest: dict[str, object]
    state_dict: object
    weights_bytes: int


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def selected_epoch_from_history(history: list) -> int:
    """Phase 8 `checkpoint_improves()`と同じ規則でselected epochを再導出する。

    単純な`min()`ではPhase 8の1e-12 tie toleranceを再現できない。historical
    contractと同じ規則を使い、tieでは先行epochを保持する。
    """
    best = float("inf")
    best_epoch = 0
    for row in history:
        if checkpoint_improves(float(row["validation_mae"]), best):
            best = float(row["validation_mae"])
            best_epoch = int(row["epoch"])
    return best_epoch


def expected_train_anchors(scale: str, population: dict[str, object]) -> list[str]:
    """そのscaleのTRAIN subsetが持つべきanchor identityをevidenceから導く。"""
    anchors = population["evidence"]["anchors_by_seed"]
    return sorted(
        anchor for seed in train_seeds(scale) for anchor in anchors[str(seed)]
    )


def validate_model_manifest(
    value: object, population: dict[str, object], lock: dict[str, object]
) -> dict[str, object]:
    """model manifestをexact TRAIN subset / dataset / source provenanceへbindする。"""
    if type(value) is not dict or set(value) != set(MODEL_FIELDS):
        raise ScaleError("model manifest fields are not exact")
    exact(value["schema"], SCHEMA + "/model", "model schema")
    exact(value["role"], ROLE, "model role")
    exact(value["execution_lock_identity"], identity(lock), "execution lock binding")
    scale = value["scale"]
    if scale not in SCALES:
        raise ScaleError("model does not name a locked Phase 10 scale")
    exact(
        value["subset"],
        subset_binding(
            scale,
            raw_corpus_identity=population["raw_corpus_identity"],
            dataset_identity=population["dataset_identity"],
            provenance=lock["provenance"],
        ),
        "TRAIN subset binding",
    )
    exact(
        value["train_anchor_identities"],
        expected_train_anchors(scale, population),
        "TRAIN anchor membership",
    )
    anchors_by_seed = population["evidence"]["anchors_by_seed"]
    validation_anchors = {
        anchor for seed in VALIDATION_SEEDS for anchor in anchors_by_seed[str(seed)]
    }
    if validation_anchors.intersection(value["train_anchor_identities"]):
        raise ScaleError("TRAIN subset leaks VALIDATION anchors")
    exact(
        value["full_inventory"],
        population["evidence"]["inventory"],
        "shared full-population inventory",
    )
    exact(value["training_lock"], training_lock(), "shared training lock")

    history = value["loss_history"]
    if type(history) is not list or not history:
        raise ScaleError("loss_history must be non-empty")
    for row in history:
        if type(row) is not dict or set(row) != {
            "epoch",
            "train_mse",
            "validation_mae",
        }:
            raise ScaleError("loss_history rows are not exact")
        finite(row["train_mse"], "train MSE")
        finite(row["validation_mae"], "validation MAE")
    if [row["epoch"] for row in history] != list(range(1, len(history) + 1)):
        raise ScaleError("loss_history epochs must be contiguous from one")
    selected = value["selected_epoch"]
    if type(selected) is not int or not 1 <= selected <= len(history):
        raise ScaleError("selected_epoch is outside the recorded history")
    exact(
        selected,
        selected_epoch_from_history(history),
        "selected checkpoint under the locked Phase 8 rule",
    )
    evaluation = value["evaluation"]
    if type(evaluation) is not dict:
        raise ScaleError("model manifest lacks its VALIDATION evaluation")
    exact(
        history[selected - 1]["validation_mae"],
        evaluation["canonical_pooled_mae"],
        "selected-epoch VALIDATION MAE against the recorded evaluation",
    )
    failures = value["self_rollout_failure_count"]
    if type(failures) is not int or failures < 0:
        raise ScaleError("self-rollout failure count must be a nonnegative int")

    cost = value["cost"]
    if type(cost) is not dict or set(cost) != set(MODEL_COST_FIELDS) | {
        "peak_process_ram_bytes"
    }:
        raise ScaleError("training cost fields are not exact")
    for name in MODEL_COST_FIELDS:
        finite(cost[name], name)
    peak = cost["peak_process_ram_bytes"]
    if peak is not None and (type(peak) is not int or peak <= 0):
        raise ScaleError("peak process RAM must be a positive int or null")
    exact(value["runtime"], lock["runtime"], "model runtime")
    weights_bytes = value["weights_bytes"]
    if type(weights_bytes) is not int or weights_bytes <= 0:
        raise ScaleError("weights_bytes must be a positive int")
    digest(value["weights_sha256"], "weights_sha256")
    return value


def validate_nested_subsets(
    manifests: dict[str, dict], population: dict[str, object]
) -> None:
    """S16 ⊂ S32 ⊂ S64 のnested membershipと、S64 == full TRAINを固定する。"""
    anchors = {
        scale: set(manifests[scale]["train_anchor_identities"]) for scale in SCALES
    }
    for smaller, larger in (("S16", "S32"), ("S32", "S64")):
        if not anchors[smaller] < anchors[larger]:
            raise ScaleError(
                f"{smaller} TRAIN anchors are not a strict subset of {larger}"
            )
    exact(
        sorted(anchors["S64"]),
        expected_train_anchors("S64", population),
        "S64 covers the full TRAIN population",
    )


def model_manifest_without_weights(
    *,
    scale: str,
    lock: dict[str, object],
    binding: dict[str, object],
    result,
    evaluation: dict[str, object],
    training_cpu_seconds: float,
) -> dict[str, object]:
    """weights digestを除くPhase 10 model manifestを構成する。"""
    return {
        "schema": SCHEMA + "/model",
        "role": ROLE,
        "execution_lock_identity": identity(lock),
        "scale": scale,
        "subset": binding["subset"],
        "train_anchor_identities": binding["train_anchor_identities"],
        "full_inventory": binding["full_inventory"],
        "training_lock": training_lock(),
        "selected_epoch": result.selected_epoch,
        "loss_history": [
            {
                "epoch": row.epoch,
                "train_mse": row.train_mse,
                "validation_mae": row.validation_mae,
            }
            for row in result.history
        ],
        "self_rollout_failure_count": 0,
        "evaluation": evaluation,
        "cost": {
            "training_cpu_seconds": training_cpu_seconds,
            "training_wall_seconds": result.training_wall_clock_seconds,
            "peak_process_ram_bytes": result.peak_process_ram_bytes,
        },
        "runtime": lock["runtime"],
    }


def save_model_artifact(
    destination: str | Path,
    model,
    manifest_without_weight_fields: dict[str, object],
    population: dict[str, object],
    lock: dict[str, object],
) -> LoadedScaleModel:
    """state_dictとmanifestをatomicに一度だけpublishする。"""
    import torch

    destination = Path(destination)
    if destination.exists():
        raise FileExistsError(f"destination already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(
        prefix=f".{destination.name}-staging-", dir=destination.parent
    ) as staging_name:
        staging = Path(staging_name)
        weights_path = staging / WEIGHTS_FILENAME
        torch.save(model.state_dict(), weights_path)
        weights = weights_path.read_bytes()
        manifest = dict(manifest_without_weight_fields)
        manifest["weights_bytes"] = len(weights)
        manifest["weights_sha256"] = _sha256(weights)
        validate_model_manifest(manifest, population, lock)
        (staging / MANIFEST_FILENAME).write_bytes(canonical_json_bytes(manifest))
        state_dict = torch.load(weights_path, weights_only=True, map_location="cpu")
        reference = model.state_dict()
        if set(state_dict) != set(reference) or any(
            not torch.equal(state_dict[name], reference[name]) for name in state_dict
        ):
            raise ScaleError("staged state_dict readback differs")
        staging.rename(destination)
    return load_model_artifact(destination, population, lock)


def load_model_artifact(
    destination: str | Path, population: dict[str, object], lock: dict[str, object]
) -> LoadedScaleModel:
    import torch

    destination = Path(destination)
    if {path.name for path in destination.iterdir()} != {
        MANIFEST_FILENAME,
        WEIGHTS_FILENAME,
    }:
        raise ScaleError("artifact contains missing or extra files")
    manifest_bytes = (destination / MANIFEST_FILENAME).read_bytes()
    manifest = validate_model_manifest(json.loads(manifest_bytes), population, lock)
    if canonical_json_bytes(manifest) != manifest_bytes:
        raise ScaleError("manifest bytes are not canonical JSON")
    weights = (destination / WEIGHTS_FILENAME).read_bytes()
    if len(weights) != manifest["weights_bytes"]:
        raise ScaleError("weights byte count differs")
    if _sha256(weights) != manifest["weights_sha256"]:
        raise ScaleError("weights digest differs")
    state_dict = torch.load(
        destination / WEIGHTS_FILENAME, weights_only=True, map_location="cpu"
    )
    return LoadedScaleModel(manifest, state_dict, len(weights))


def load_model(
    destination: str | Path, population: dict[str, object], lock: dict[str, object]
):
    """artifactからlocked S2 modelをrestoreする。任意codeは復元しない。"""
    from lisjong_arena.phase8_sequential.model import create_model, parameter_count
    from lisjong_arena.phase8_sequential.protocol import Candidate

    loaded = load_model_artifact(destination, population, lock)
    model = create_model(Candidate.S2)
    model.load_state_dict(loaded.state_dict, strict=True)
    exact(
        parameter_count(model),
        loaded.manifest["training_lock"]["parameter_count"],
        "restored model parameter count",
    )
    return model, loaded.manifest


def save_result(
    destination: str | Path, value: dict[str, object], lock: dict[str, object]
) -> Path:
    """Phase 10 resultをcanonical JSONとして一度だけ書く。"""
    destination = Path(destination)
    if destination.exists():
        raise FileExistsError(f"destination already exists: {destination}")
    validate_result(value, lock)
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(value)
    payload["result_identity"] = _sha256(canonical_json_bytes(value))
    destination.write_bytes(canonical_json_bytes(payload))
    return destination


def load_result(destination: str | Path, lock: dict[str, object]) -> dict[str, object]:
    data = Path(destination).read_bytes()
    value = json.loads(data)
    if canonical_json_bytes(value) != data:
        raise ScaleError("result bytes are not canonical JSON")
    if type(value) is not dict or "result_identity" not in value:
        raise ScaleError("result lacks its logical identity")
    recorded = value["result_identity"]
    without_identity = {
        name: item for name, item in value.items() if name != "result_identity"
    }
    if recorded != _sha256(canonical_json_bytes(without_identity)):
        raise ScaleError("result logical identity differs")
    validate_result(without_identity, lock)
    return value


__all__ = [
    "MANIFEST_FILENAME",
    "MODEL_COST_FIELDS",
    "MODEL_FIELDS",
    "WEIGHTS_FILENAME",
    "LoadedScaleModel",
    "expected_train_anchors",
    "load_model",
    "load_model_artifact",
    "load_result",
    "model_manifest_without_weights",
    "save_model_artifact",
    "save_result",
    "selected_epoch_from_history",
    "validate_model_manifest",
    "validate_nested_subsets",
]
