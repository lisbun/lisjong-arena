"""Immutable lock, readout model, coverage, and result artifacts."""

import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory

from .lock import validate_lock
from .model import create_readout_head
from .protocol import (
    E160_WEIGHTS_SHA256,
    MAX_EPOCHS,
    PATIENCE,
    ROLE,
    SCHEMA,
    Phase11Error,
    canonical_json_bytes,
    digest,
    exact,
    finite,
    identity,
    readout_value,
    retained_value,
    training_value,
)
from .result import validate_result

MANIFEST_FILENAME = "manifest.json"
WEIGHTS_FILENAME = "weights.pt"
MODEL_FIELDS = (
    "schema",
    "role",
    "execution_lock_identity",
    "coverage_identity",
    "retained",
    "architecture",
    "training",
    "selected_epoch",
    "epochs_run",
    "history",
    "train_binary_log_loss",
    "validation_binary_log_loss",
    "optimizer_parameter_names",
    "frozen_e160_weights_sha256",
    "frozen_digest_before",
    "frozen_digest_after",
    "cost",
    "runtime",
    "weights_bytes",
    "weights_sha256",
)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def save_lock(path: str | Path, lock: dict) -> Path:
    destination = Path(path)
    if destination.exists():
        raise FileExistsError(f"lock destination already exists: {destination}")
    validate_lock(lock)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(canonical_json_bytes(lock))
    return destination


def load_lock(path: str | Path) -> dict[str, object]:
    data = Path(path).read_bytes()
    try:
        value = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise Phase11Error("execution lock is not valid JSON") from error
    if canonical_json_bytes(value) != data:
        raise Phase11Error("execution lock bytes are not canonical JSON")
    return validate_lock(value)


def _selected_epoch(history: list) -> int:
    best = float("inf")
    selected = 0
    for row in history:
        value = row["validation_binary_log_loss"]
        if value < best and abs(value - best) > 1e-12:
            best = value
            selected = row["epoch"]
    return selected


def validate_model_manifest(
    value: object, lock: dict, coverage_identity: str
) -> dict[str, object]:
    validate_lock(lock)
    if type(value) is not dict or set(value) != set(MODEL_FIELDS):
        raise Phase11Error("readout model manifest fields are not exact")
    exact(value["schema"], SCHEMA + "/model", "model schema")
    exact(value["role"], ROLE, "model role")
    exact(value["execution_lock_identity"], identity(lock), "model lock binding")
    exact(value["coverage_identity"], coverage_identity, "model coverage binding")
    exact(value["retained"], retained_value(), "model retained binding")
    exact(value["architecture"], readout_value(), "readout architecture")
    exact(value["training"], training_value(), "readout training contract")
    history = value["history"]
    if type(history) is not list or not history or len(history) > MAX_EPOCHS:
        raise Phase11Error("training history length is invalid")
    for row in history:
        if type(row) is not dict or set(row) != {
            "epoch",
            "train_binary_log_loss",
            "validation_binary_log_loss",
        }:
            raise Phase11Error("training history row fields are not exact")
        if type(row["epoch"]) is not int:
            raise Phase11Error("history epoch must be a JSON int")
        finite(row["train_binary_log_loss"], "TRAIN log loss", minimum=0)
        finite(row["validation_binary_log_loss"], "VALIDATION log loss", minimum=0)
    exact(
        [row["epoch"] for row in history],
        list(range(1, len(history) + 1)),
        "history epochs",
    )
    if type(value["selected_epoch"]) is not int:
        raise Phase11Error("selected_epoch must be a JSON int")
    exact(value["selected_epoch"], _selected_epoch(history), "selected epoch")
    exact(value["epochs_run"], len(history), "epochs run")
    if len(history) < MAX_EPOCHS:
        exact(
            len(history) - value["selected_epoch"],
            PATIENCE,
            "patience-triggered early stop",
        )
    finite(value["train_binary_log_loss"], "selected TRAIN log loss", minimum=0)
    finite(
        value["validation_binary_log_loss"], "selected VALIDATION log loss", minimum=0
    )
    exact(
        value["validation_binary_log_loss"],
        history[value["selected_epoch"] - 1]["validation_binary_log_loss"],
        "selected VALIDATION score",
    )
    exact(
        value["optimizer_parameter_names"],
        ["0.weight", "0.bias", "2.weight", "2.bias"],
        "readout-only optimizer parameter names",
    )
    exact(
        value["frozen_e160_weights_sha256"], E160_WEIGHTS_SHA256, "frozen E160 weights"
    )
    digest(value["frozen_digest_before"], "frozen before digest")
    exact(value["frozen_digest_after"], value["frozen_digest_before"], "frozen bytes")
    cost = value["cost"]
    if type(cost) is not dict or set(cost) != {
        "training_cpu_seconds",
        "training_wall_seconds",
    }:
        raise Phase11Error("training cost fields are not exact")
    finite(cost["training_cpu_seconds"], "training CPU seconds", minimum=0)
    finite(cost["training_wall_seconds"], "training wall seconds", minimum=0)
    exact(value["runtime"], lock["runtime"], "model runtime")
    if type(value["weights_bytes"]) is not int or value["weights_bytes"] <= 0:
        raise Phase11Error("weights_bytes must be a positive JSON int")
    digest(value["weights_sha256"], "readout weights SHA-256")
    return value


def model_manifest_without_weights(
    *, lock: dict, coverage_identity: str, result, training_cpu_seconds: float
) -> dict[str, object]:
    return {
        "schema": SCHEMA + "/model",
        "role": ROLE,
        "execution_lock_identity": identity(lock),
        "coverage_identity": coverage_identity,
        "retained": retained_value(),
        "architecture": readout_value(),
        "training": training_value(),
        "selected_epoch": result.selected_epoch,
        "epochs_run": len(result.history),
        "history": [asdict(row) for row in result.history],
        "train_binary_log_loss": result.train_binary_log_loss,
        "validation_binary_log_loss": result.validation_binary_log_loss,
        "optimizer_parameter_names": list(result.optimizer_parameter_names),
        "frozen_e160_weights_sha256": E160_WEIGHTS_SHA256,
        "frozen_digest_before": result.frozen_digest_before,
        "frozen_digest_after": result.frozen_digest_after,
        "cost": {
            "training_cpu_seconds": training_cpu_seconds,
            "training_wall_seconds": result.training_wall_seconds,
        },
        "runtime": lock["runtime"],
    }


def save_model(
    destination: str | Path,
    head,
    manifest_without_weights: dict,
    lock: dict,
    coverage_identity: str,
) -> dict[str, object]:
    import torch

    destination = Path(destination)
    if destination.exists():
        raise FileExistsError(f"model destination already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(
        prefix=f".{destination.name}-staging-", dir=destination.parent
    ) as staging_name:
        staging = Path(staging_name)
        weights_path = staging / WEIGHTS_FILENAME
        torch.save(head.state_dict(), weights_path)
        weights = weights_path.read_bytes()
        manifest = dict(manifest_without_weights)
        manifest["weights_bytes"] = len(weights)
        manifest["weights_sha256"] = _sha256(weights)
        validate_model_manifest(manifest, lock, coverage_identity)
        (staging / MANIFEST_FILENAME).write_bytes(canonical_json_bytes(manifest))
        staging.rename(destination)
    _head, loaded = load_model(destination, lock, coverage_identity)
    return loaded


def load_model(destination: str | Path, lock: dict, coverage_identity: str):
    import torch

    destination = Path(destination)
    if not destination.is_dir() or {path.name for path in destination.iterdir()} != {
        MANIFEST_FILENAME,
        WEIGHTS_FILENAME,
    }:
        raise Phase11Error("readout artifact contains missing or extra files")
    manifest_bytes = (destination / MANIFEST_FILENAME).read_bytes()
    try:
        manifest = json.loads(manifest_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise Phase11Error("readout manifest is not valid JSON") from error
    validate_model_manifest(manifest, lock, coverage_identity)
    if canonical_json_bytes(manifest) != manifest_bytes:
        raise Phase11Error("readout manifest bytes are not canonical JSON")
    weights_path = destination / WEIGHTS_FILENAME
    weights = weights_path.read_bytes()
    exact(len(weights), manifest["weights_bytes"], "readout weight bytes")
    exact(_sha256(weights), manifest["weights_sha256"], "readout weight digest")
    state_dict = torch.load(weights_path, weights_only=True, map_location="cpu")
    head = create_readout_head()
    try:
        head.load_state_dict(state_dict, strict=True)
    except (RuntimeError, TypeError, AttributeError) as error:
        raise Phase11Error(
            f"weights do not strict-load into the fixed readout: {error}"
        ) from error
    return head, manifest


def save_result(path: str | Path, value: dict, lock: dict) -> Path:
    destination = Path(path)
    if destination.exists():
        raise FileExistsError(f"result destination already exists: {destination}")
    validate_result(value, lock)
    payload = dict(value)
    payload["result_identity"] = identity(value)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(canonical_json_bytes(payload))
    return destination


def load_result(path: str | Path, lock: dict) -> dict[str, object]:
    data = Path(path).read_bytes()
    try:
        payload = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise Phase11Error("result is not valid JSON") from error
    if canonical_json_bytes(payload) != data or type(payload) is not dict:
        raise Phase11Error("result bytes are not canonical JSON")
    recorded = payload.pop("result_identity", None)
    exact(recorded, identity(payload), "result identity")
    validate_result(payload, lock)
    payload["result_identity"] = recorded
    return payload


__all__ = [
    "MANIFEST_FILENAME",
    "MODEL_FIELDS",
    "WEIGHTS_FILENAME",
    "load_lock",
    "load_model",
    "load_result",
    "model_manifest_without_weights",
    "save_lock",
    "save_model",
    "save_result",
    "validate_model_manifest",
]
