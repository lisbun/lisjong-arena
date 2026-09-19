"""Write-once JSON artifacts for Arena #222."""

import json
import math
from pathlib import Path

from .evaluation import validate_result
from .lock import validate_lock
from .model import validate_model
from .protocol import (
    FEATURE_NAMES,
    FEATURE_SEMANTICS_ID,
    SCHEMA,
    ClassicalWaitError,
    canonical_json_bytes,
    exact,
    identity,
)

MODEL_FILENAME = "model.json"


def _read_json(path: Path, name: str) -> dict[str, object]:
    if not path.is_file():
        raise ClassicalWaitError(f"{name} is missing: {path}")
    try:
        value = json.loads(path.read_bytes())
    except (json.JSONDecodeError, OSError) as error:
        raise ClassicalWaitError(f"{name} is not valid JSON: {error}") from error
    if type(value) is not dict:
        raise ClassicalWaitError(f"{name} must be a JSON object")
    return value


def _write_once(path: Path, value: object, name: str) -> Path:
    if path.exists():
        raise FileExistsError(f"{name} destination already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json_bytes(value))
    return path


def save_lock(path: str | Path, value: dict) -> Path:
    validate_lock(value)
    return _write_once(Path(path), value, "execution lock")


def load_lock(path: str | Path) -> dict[str, object]:
    value = _read_json(Path(path), "execution lock")
    return validate_lock(value)


def validate_feature_summary(
    value: object, execution_lock_identity: str
) -> dict[str, object]:
    if type(value) is not dict or set(value) != {
        "schema",
        "execution_lock_identity",
        "feature_semantics_id",
        "partition",
        "cells",
        "features",
    }:
        raise ClassicalWaitError("feature summary fields are not exact")
    exact(value["schema"], SCHEMA + "/feature-summary", "feature summary schema")
    exact(
        value["execution_lock_identity"],
        execution_lock_identity,
        "feature summary lock binding",
    )
    exact(
        value["feature_semantics_id"],
        FEATURE_SEMANTICS_ID,
        "feature summary semantics",
    )
    exact(value["partition"], "train", "feature summary partition")
    if type(value["cells"]) is not int or value["cells"] <= 0:
        raise ClassicalWaitError("feature summary cells must be positive")
    rows = value["features"]
    if type(rows) is not list or [row.get("name") for row in rows] != list(
        FEATURE_NAMES
    ):
        raise ClassicalWaitError("feature summary names are not exact")
    for row in rows:
        if set(row) != {"name", "minimum", "maximum", "mean"}:
            raise ClassicalWaitError("feature summary row fields are not exact")
        numbers = (row["minimum"], row["maximum"], row["mean"])
        if any(
            type(number) not in (int, float) or not math.isfinite(number)
            for number in numbers
        ):
            raise ClassicalWaitError("feature summary contains a non-finite value")
        if not (
            0.0
            <= row["minimum"]
            <= row["mean"]
            <= row["maximum"]
            <= 1.0
        ):
            raise ClassicalWaitError("feature summary values must remain in [0,1]")
    return value


def save_feature_summary(
    path: str | Path, value: dict, execution_lock_identity: str
) -> Path:
    validate_feature_summary(value, execution_lock_identity)
    return _write_once(Path(path), value, "feature summary")


def load_feature_summary(
    path: str | Path, execution_lock_identity: str
) -> dict[str, object]:
    value = _read_json(Path(path), "feature summary")
    return validate_feature_summary(value, execution_lock_identity)


def save_model(
    directory: str | Path,
    value: dict,
    execution_lock_identity: str,
    baseline: dict,
) -> Path:
    destination = Path(directory)
    if destination.exists():
        raise FileExistsError(
            f"classical model destination already exists: {destination}"
        )
    validate_model(value, execution_lock_identity, baseline)
    payload = dict(value)
    payload["model_identity"] = identity(value)
    destination.mkdir(parents=True, exist_ok=False)
    path = destination / MODEL_FILENAME
    path.write_bytes(canonical_json_bytes(payload))
    return path


def load_model(
    directory: str | Path,
    execution_lock_identity: str,
    baseline: dict,
) -> dict[str, object]:
    payload = _read_json(Path(directory) / MODEL_FILENAME, "classical model")
    if "model_identity" not in payload:
        raise ClassicalWaitError("classical model identity is missing")
    recorded_identity = payload.pop("model_identity")
    exact(recorded_identity, identity(payload), "classical model identity")
    return validate_model(payload, execution_lock_identity, baseline)


def save_result(path: str | Path, value: dict, lock: dict) -> Path:
    validate_result(value, lock)
    payload = dict(value)
    payload["result_identity"] = identity(value)
    return _write_once(Path(path), payload, "result")


def load_result(path: str | Path, lock: dict) -> dict[str, object]:
    payload = _read_json(Path(path), "result")
    if "result_identity" not in payload:
        raise ClassicalWaitError("result identity is missing")
    recorded_identity = payload.pop("result_identity")
    exact(recorded_identity, identity(payload), "result identity")
    validate_result(payload, lock)
    payload["result_identity"] = recorded_identity
    return payload


__all__ = [
    "MODEL_FILENAME",
    "load_feature_summary",
    "load_lock",
    "load_model",
    "load_result",
    "save_feature_summary",
    "save_lock",
    "save_model",
    "save_result",
    "validate_feature_summary",
]
