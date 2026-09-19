"""Write-once canonical artifacts for Arena #291."""

import json
from pathlib import Path

from .data import validate_centering
from .evaluation import validate_result
from .lock import validate_lock
from .model import validate_model
from .protocol import (
    SCHEMA,
    E160OffsetProbeError,
    canonical_json_bytes,
    exact,
    identity,
)

MODEL_FILENAME = "model.json"


def _read_json(path: Path, name: str) -> dict[str, object]:
    if not path.is_file():
        raise E160OffsetProbeError(f"{name} is missing: {path}")
    try:
        raw = path.read_bytes()
        value = json.loads(raw)
    except (json.JSONDecodeError, OSError) as error:
        raise E160OffsetProbeError(f"{name} is not valid JSON: {error}") from error
    if type(value) is not dict:
        raise E160OffsetProbeError(f"{name} must be a JSON object")
    if canonical_json_bytes(value) != raw:
        raise E160OffsetProbeError(f"{name} bytes are not canonical JSON")
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
    return validate_lock(_read_json(Path(path), "execution lock"))


def validate_latent_summary(
    value: object,
    execution_lock_identity: str,
    locked_reference: dict,
) -> dict[str, object]:
    if type(value) is not dict or set(value) != {
        "schema",
        "execution_lock_identity",
        "latent_reference",
    }:
        raise E160OffsetProbeError("latent summary fields are not exact")
    exact(value["schema"], SCHEMA + "/latent-summary", "latent summary schema")
    exact(
        value["execution_lock_identity"],
        execution_lock_identity,
        "latent summary lock binding",
    )
    exact(
        value["latent_reference"],
        locked_reference,
        "latent summary locked reference",
    )
    validate_centering(value["latent_reference"]["centering"])
    return value


def save_latent_summary(
    path: str | Path,
    latent_reference: dict,
    execution_lock_identity: str,
) -> Path:
    value = {
        "schema": SCHEMA + "/latent-summary",
        "execution_lock_identity": execution_lock_identity,
        "latent_reference": latent_reference,
    }
    validate_latent_summary(value, execution_lock_identity, latent_reference)
    return _write_once(Path(path), value, "latent summary")


def load_latent_summary(
    path: str | Path,
    execution_lock_identity: str,
    locked_reference: dict,
) -> dict[str, object]:
    value = _read_json(Path(path), "latent summary")
    return validate_latent_summary(
        value,
        execution_lock_identity,
        locked_reference,
    )


def save_model(
    directory: str | Path,
    value: dict,
    execution_lock_identity: str,
    baseline: dict,
    centering: dict,
    latent_fingerprint: str,
) -> Path:
    destination = Path(directory)
    if destination.exists():
        raise FileExistsError(f"probe model destination already exists: {destination}")
    validate_model(
        value,
        execution_lock_identity,
        baseline,
        centering,
        latent_fingerprint,
    )
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
    centering: dict,
    latent_fingerprint: str,
) -> dict[str, object]:
    payload = _read_json(Path(directory) / MODEL_FILENAME, "probe model")
    if "model_identity" not in payload:
        raise E160OffsetProbeError("probe model identity is missing")
    recorded_identity = payload.pop("model_identity")
    exact(recorded_identity, identity(payload), "probe model identity")
    return validate_model(
        payload,
        execution_lock_identity,
        baseline,
        centering,
        latent_fingerprint,
    )


def save_result(path: str | Path, value: dict, lock: dict) -> Path:
    validate_result(value, lock)
    payload = dict(value)
    payload["result_identity"] = identity(value)
    return _write_once(Path(path), payload, "result")


def load_result(path: str | Path, lock: dict) -> dict[str, object]:
    payload = _read_json(Path(path), "result")
    if "result_identity" not in payload:
        raise E160OffsetProbeError("result identity is missing")
    recorded_identity = payload.pop("result_identity")
    exact(recorded_identity, identity(payload), "result identity")
    validate_result(payload, lock)
    payload["result_identity"] = recorded_identity
    return payload


__all__ = [
    "MODEL_FILENAME",
    "load_latent_summary",
    "load_lock",
    "load_model",
    "load_result",
    "save_latent_summary",
    "save_lock",
    "save_model",
    "save_result",
    "validate_latent_summary",
]
