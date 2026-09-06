"""E160 model artifactとArena #167 result artifactのstrict contract。

#157 `stage3_epoch_budget.artifact`は#157 execution lockとmax_epochs 80の
training lockへbindされたvalidatorである。そのhistorical validatorを#167の
ために書き換えないので、本childは別schema / 別identityのartifactを持つ。

artifactは`state_dict`だけを保存し、factory / callable / 任意codeを保存・復元
しない。既存destinationを上書きせず、内部矛盾はload時にfail closedする。
weightsとgenerated resultはGit repositoryへcommitしない。

## Model binding

E160 model artifactは、自分がE80とexactに同じTRAIN subsetから来たことを証明
できる必要がある。

```text
retained                  == Issue #167がlockしたretained artifact identity
subset                    == #157 E80 manifestの subset（population / corpus / dataset / provenance）
train_anchor_identities   == #157 E80 manifestの train anchor membership
full_inventory            == population evidenceのinventory（BPTT policy共有の証拠）
training_lock             == saturation training lock（max_epochsだけが80と違う）
selected_epoch            == Phase 8 checkpoint ruleでloss historyから再導出した値
loss_history[selected]    == evaluationのcanonical pooled MAE
runtime                   == #167 execution lockのruntime
```

TRAIN subsetを名乗り替えたmodel、epoch budget以外のconfigを変えたmodel、
evaluationとtraining historyが噛み合わないmodelはここで落ちる。
"""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from lisjong_arena.phase4_raw_corpus.codec import canonical_json_bytes
from lisjong_arena.stage3_scale_learning_curve.artifact import (
    selected_epoch_from_history,
)

from .lock import validate_lock
from .protocol import (
    DETERMINISM_PREFIX_EPOCHS,
    ROLE,
    SATURATION_ARM,
    SATURATION_MAX_EPOCHS,
    SCHEMA,
    SaturationError,
    digest,
    exact,
    finite,
    identity,
    saturation_training_lock,
)
from .result import validate_result
from .retained import retained_value

MANIFEST_FILENAME = "manifest.json"
WEIGHTS_FILENAME = "weights.pt"
MODEL_FIELDS = (
    "schema",
    "role",
    "execution_lock_identity",
    "arm",
    "retained",
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
class LoadedSaturationModel:
    manifest: dict[str, object]
    state_dict: object
    weights_bytes: int


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def validate_loss_history(value: object, name: str) -> list:
    """loss historyのrow shapeとepoch連続性を固定する。"""
    if type(value) is not list or not value:
        raise SaturationError(f"{name} must be non-empty")
    for row in value:
        if type(row) is not dict or set(row) != {
            "epoch",
            "train_mse",
            "validation_mae",
        }:
            raise SaturationError(f"{name} rows are not exact")
        finite(row["train_mse"], "train MSE")
        finite(row["validation_mae"], "validation MAE")
    if [row["epoch"] for row in value] != list(range(1, len(value) + 1)):
        raise SaturationError(f"{name} epochs must be contiguous from one")
    return value


def validate_model_manifest(
    value: object,
    population: dict[str, object],
    lock: dict[str, object],
    baseline_manifest: dict[str, object],
) -> dict[str, object]:
    """E160 manifestを、E80とexactに同じTRAIN subsetとsaturation lockへbindする。"""
    validate_lock(lock)
    if type(value) is not dict or set(value) != set(MODEL_FIELDS):
        raise SaturationError("model manifest fields are not exact")
    exact(value["schema"], SCHEMA + "/model", "model schema")
    exact(value["role"], ROLE, "model role")
    exact(value["execution_lock_identity"], identity(lock), "execution lock binding")
    exact(value["arm"], SATURATION_ARM, "saturation arm")
    exact(value["retained"], retained_value(), "retained artifact binding")
    exact(
        value["subset"],
        baseline_manifest["subset"],
        "TRAIN subset shared with the retained E80 arm",
    )
    exact(
        value["train_anchor_identities"],
        baseline_manifest["train_anchor_identities"],
        "TRAIN anchor membership shared with the retained E80 arm",
    )
    exact(
        value["full_inventory"],
        population["evidence"]["inventory"],
        "shared full-population inventory",
    )
    exact(
        value["training_lock"], saturation_training_lock(), "saturation training lock"
    )

    history = validate_loss_history(value["loss_history"], "loss_history")
    if not DETERMINISM_PREFIX_EPOCHS <= len(history) <= SATURATION_MAX_EPOCHS:
        raise SaturationError(
            "the saturation history must cover the determinism prefix without "
            "exceeding the locked epoch budget"
        )
    selected = value["selected_epoch"]
    if type(selected) is not int or not 1 <= selected <= len(history):
        raise SaturationError("selected_epoch is outside the recorded history")
    exact(
        selected,
        selected_epoch_from_history(history),
        "selected checkpoint under the locked Phase 8 rule",
    )
    evaluation = value["evaluation"]
    if type(evaluation) is not dict:
        raise SaturationError("model manifest lacks its VALIDATION evaluation")
    exact(
        history[selected - 1]["validation_mae"],
        evaluation["canonical_pooled_mae"],
        "selected-epoch VALIDATION MAE against the recorded evaluation",
    )
    failures = value["self_rollout_failure_count"]
    if type(failures) is not int or failures < 0:
        raise SaturationError("self-rollout failure count must be a nonnegative int")

    cost = value["cost"]
    if type(cost) is not dict or set(cost) != set(MODEL_COST_FIELDS) | {
        "peak_process_ram_bytes"
    }:
        raise SaturationError("training cost fields are not exact")
    for name in MODEL_COST_FIELDS:
        finite(cost[name], name)
    peak = cost["peak_process_ram_bytes"]
    if peak is not None and (type(peak) is not int or peak <= 0):
        raise SaturationError("peak process RAM must be a positive int or null")
    exact(value["runtime"], lock["runtime"], "model runtime")
    weights_bytes = value["weights_bytes"]
    if type(weights_bytes) is not int or weights_bytes <= 0:
        raise SaturationError("weights_bytes must be a positive int")
    digest(value["weights_sha256"], "weights_sha256")
    return value


def model_manifest_without_weights(
    *,
    lock: dict[str, object],
    binding: dict[str, object],
    result,
    evaluation: dict[str, object],
    training_cpu_seconds: float,
) -> dict[str, object]:
    """weights digestを除くE160 model manifestを構成する。"""
    return {
        "schema": SCHEMA + "/model",
        "role": ROLE,
        "execution_lock_identity": identity(lock),
        "arm": SATURATION_ARM,
        "retained": retained_value(),
        "subset": binding["subset"],
        "train_anchor_identities": binding["train_anchor_identities"],
        "full_inventory": binding["full_inventory"],
        "training_lock": saturation_training_lock(),
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
    baseline_manifest: dict[str, object],
) -> LoadedSaturationModel:
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
        validate_model_manifest(manifest, population, lock, baseline_manifest)
        (staging / MANIFEST_FILENAME).write_bytes(canonical_json_bytes(manifest))
        state_dict = torch.load(weights_path, weights_only=True, map_location="cpu")
        reference = model.state_dict()
        if set(state_dict) != set(reference) or any(
            not torch.equal(state_dict[name], reference[name]) for name in state_dict
        ):
            raise SaturationError("staged state_dict readback differs")
        staging.rename(destination)
    return load_model_artifact(destination, population, lock, baseline_manifest)


def load_model_artifact(
    destination: str | Path,
    population: dict[str, object],
    lock: dict[str, object],
    baseline_manifest: dict[str, object],
) -> LoadedSaturationModel:
    import torch

    destination = Path(destination)
    if {path.name for path in destination.iterdir()} != {
        MANIFEST_FILENAME,
        WEIGHTS_FILENAME,
    }:
        raise SaturationError("artifact contains missing or extra files")
    manifest_bytes = (destination / MANIFEST_FILENAME).read_bytes()
    manifest = validate_model_manifest(
        json.loads(manifest_bytes), population, lock, baseline_manifest
    )
    if canonical_json_bytes(manifest) != manifest_bytes:
        raise SaturationError("manifest bytes are not canonical JSON")
    weights = (destination / WEIGHTS_FILENAME).read_bytes()
    if len(weights) != manifest["weights_bytes"]:
        raise SaturationError("weights byte count differs")
    if _sha256(weights) != manifest["weights_sha256"]:
        raise SaturationError("weights digest differs")
    state_dict = torch.load(
        destination / WEIGHTS_FILENAME, weights_only=True, map_location="cpu"
    )
    return LoadedSaturationModel(manifest, state_dict, len(weights))


def load_model(
    destination: str | Path,
    population: dict[str, object],
    lock: dict[str, object],
    baseline_manifest: dict[str, object],
):
    """E160 artifactからlocked S2 modelをrestoreする。任意codeは復元しない。

    weightsのbyte数とSHA-256が整合していても、それはfileが記録どおりである
    ことしか示さない。checkpointがlocked S2 familyのものであることは、
    `strict=True`のstate dict loadだけが証明できる。
    """
    from lisjong_arena.phase8_sequential.model import create_model, parameter_count
    from lisjong_arena.phase8_sequential.protocol import Candidate

    loaded = load_model_artifact(destination, population, lock, baseline_manifest)
    model = create_model(Candidate.S2)
    try:
        model.load_state_dict(loaded.state_dict, strict=True)
    except (RuntimeError, TypeError, AttributeError) as exc:
        raise SaturationError(
            f"checkpoint does not strict load into the locked S2 model: {exc}"
        ) from exc
    exact(
        parameter_count(model),
        loaded.manifest["training_lock"]["parameter_count"],
        "restored model parameter count",
    )
    return model, loaded.manifest


def save_result(
    destination: str | Path, value: dict[str, object], lock: dict[str, object]
) -> Path:
    """Arena #167 resultをcanonical JSONとして一度だけ書く。"""
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
        raise SaturationError("result bytes are not canonical JSON")
    if type(value) is not dict or "result_identity" not in value:
        raise SaturationError("result lacks its logical identity")
    recorded = value["result_identity"]
    without_identity = {
        name: item for name, item in value.items() if name != "result_identity"
    }
    if recorded != _sha256(canonical_json_bytes(without_identity)):
        raise SaturationError("result logical identity differs")
    validate_result(without_identity, lock)
    return value


__all__ = [
    "MANIFEST_FILENAME",
    "MODEL_COST_FIELDS",
    "MODEL_FIELDS",
    "WEIGHTS_FILENAME",
    "LoadedSaturationModel",
    "load_model",
    "load_model_artifact",
    "load_result",
    "model_manifest_without_weights",
    "save_model_artifact",
    "save_result",
    "validate_loss_history",
    "validate_model_manifest",
]
