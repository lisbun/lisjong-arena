"""Strict completion artifact for #262."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from lisjong_arena._artifact_io import canonical_json_text, write_new_artifact_file
from lisjong_arena.stage_a0_tenpai_protocol_lock import protocol as locked

from .errors import StageA0ExecutionError
from .protocol import (
    EXECUTION_PROTOCOL_ID,
    EXPECTED_LOCK_B_IDENTITY,
    GATE_FAIL,
    GATE_PASS,
    ISSUE_IDENTITY,
    OUTCOME_NO_DENSE_SIGNAL,
    OUTCOME_POLICY_NOT_ESTABLISHED,
    OUTCOME_REPRESENTATION_NOT_POWERED,
    OUTCOME_STOP_INVALID,
    OUTCOME_SUPERVISION_VALUE,
    RESULT_SCHEMA_VERSION,
)
from .training import LoadedCheckpoint


def _identity(document: dict[str, object]) -> str:
    logical = {
        key: value for key, value in document.items() if key != "result_identity"
    }
    return hashlib.sha256(canonical_json_text(logical).encode("utf-8")).hexdigest()


def _derive_outcome(gate: dict, downstream: dict | None) -> str:
    label = gate["classification"]["label"]
    if label == GATE_FAIL:
        if downstream is not None:
            raise StageA0ExecutionError("downstream must not exist after Gate FAIL")
        return OUTCOME_NO_DENSE_SIGNAL
    if label != GATE_PASS:
        raise StageA0ExecutionError("gate completion label is invalid")
    if locked.DOWNSTREAM_STATUS != "A0 DOWNSTREAM ENABLED":
        if downstream is not None:
            raise StageA0ExecutionError("NOT POWERED result must not run downstream")
        return OUTCOME_REPRESENTATION_NOT_POWERED
    if downstream is None:
        raise StageA0ExecutionError("Gate PASS + ENABLED requires downstream evidence")
    classification = downstream["paired"]["classification"]
    if classification == "POSITIVE":
        return OUTCOME_SUPERVISION_VALUE
    if classification in ("NEGATIVE", "INCONCLUSIVE"):
        return OUTCOME_POLICY_NOT_ESTABLISHED
    raise StageA0ExecutionError("downstream classification is invalid")


def build_result(
    *,
    preflight: dict,
    checkpoints: tuple[LoadedCheckpoint, ...],
    gate: dict,
    downstream: dict | None,
) -> dict[str, object]:
    if preflight.get("lock_b_identity") != EXPECTED_LOCK_B_IDENTITY:
        raise StageA0ExecutionError("preflight Lock B binding drifted")
    if preflight.get("outcome") != "PREFLIGHT PASS":
        raise StageA0ExecutionError("completion requires PREFLIGHT PASS")
    if len(checkpoints) != 6:
        raise StageA0ExecutionError("completion requires exactly six checkpoints")

    indexed = {
        (checkpoint.arm.value, checkpoint.seed): checkpoint
        for checkpoint in checkpoints
    }
    expected_keys = {
        (arm, seed) for arm in ("A", "T") for seed in locked.TRAINING_SEEDS
    }
    if set(indexed) != expected_keys:
        raise StageA0ExecutionError(
            "completion checkpoint population is not A/T x 0/1/2"
        )
    for seed in locked.TRAINING_SEEDS:
        if (
            indexed[("A", seed)].manifest["initial_policy_fingerprint"]
            != indexed[("T", seed)].manifest["initial_policy_fingerprint"]
        ):
            raise StageA0ExecutionError(
                f"A/T initialization fingerprint differs for seed {seed}"
            )

    checkpoint_documents = []
    for arm in ("A", "T"):
        for seed in locked.TRAINING_SEEDS:
            checkpoint = indexed[(arm, seed)]
            checkpoint_documents.append(
                {
                    "arm": arm,
                    "training_seed": seed,
                    "checkpoint_identity": checkpoint.identity,
                    "weights_sha256": checkpoint.weights_sha256,
                    "auxiliary_weights_sha256": checkpoint.manifest[
                        "auxiliary_weights_sha256"
                    ],
                    "initial_policy_fingerprint": checkpoint.manifest[
                        "initial_policy_fingerprint"
                    ],
                    "selected_epoch": checkpoint.manifest["selected_epoch"],
                    "selected_validation_policy_ce": checkpoint.manifest[
                        "selected_validation_policy_ce"
                    ],
                    "model_id": checkpoint.manifest["model"]["model_id"],
                    "parameter_count": checkpoint.manifest["parameter_count"],
                }
            )

    outcome = _derive_outcome(gate, downstream)
    document: dict[str, object] = {
        "schema_version": RESULT_SCHEMA_VERSION,
        "execution_protocol_id": EXECUTION_PROTOCOL_ID,
        "issue": ISSUE_IDENTITY,
        "parent_issue": "lisbun/lisjong-arena#255",
        "prerequisite_issue": "lisbun/lisjong-arena#258",
        "protocol_lock_issue": "lisbun/lisjong-arena#259",
        "project_issue": "lisbun/lisjong-project#57",
        "lock_b_identity": EXPECTED_LOCK_B_IDENTITY,
        "protocol_identity": locked.PROTOCOL_ID,
        "execution_revision": preflight["execution_provenance"][
            "lisjong_arena_revision"
        ],
        "data": {
            **preflight["data"],
            "baseline0_identity": locked.BASELINE0_IDENTITY,
            "baseline1_identity": locked.BASELINE1_IDENTITY,
            "baseline_fingerprint": preflight["baseline_fingerprint"],
        },
        "checkpoints": checkpoint_documents,
        "gate": {
            "gate_identity": gate["gate_identity"],
            "classification": gate["classification"],
            "eligible_cell_count": gate["eligible_cell_count"],
            "eligible_block_count": gate["eligible_block_count"],
            "baseline0": gate["baseline0"],
            "baseline1": gate["baseline1"],
            "t_seed_metrics": gate["t_seed_metrics"],
            "per_block_data_identity": gate["per_block_data_identity"],
        },
        "downstream": (
            {
                "status": locked.DOWNSTREAM_STATUS,
                "execution": "NOT RUN",
            }
            if downstream is None
            else downstream
        ),
        "primary_outcome": outcome,
        "protected_test_evaluated": False,
    }
    document["result_identity"] = _identity(document)
    return document


def build_invalid_result(
    *,
    stage: str,
    error: Exception,
    preflight: dict | None,
) -> dict[str, object]:
    """Build the terminal STOP / INVALID artifact after a scientific failure."""
    if type(stage) is not str or not stage:
        raise StageA0ExecutionError("invalid-result stage must be a non-empty string")
    document: dict[str, object] = {
        "schema_version": RESULT_SCHEMA_VERSION,
        "execution_protocol_id": EXECUTION_PROTOCOL_ID,
        "issue": ISSUE_IDENTITY,
        "parent_issue": "lisbun/lisjong-arena#255",
        "prerequisite_issue": "lisbun/lisjong-arena#258",
        "protocol_lock_issue": "lisbun/lisjong-arena#259",
        "project_issue": "lisbun/lisjong-project#57",
        "lock_b_identity": EXPECTED_LOCK_B_IDENTITY,
        "protocol_identity": locked.PROTOCOL_ID,
        "execution_revision": (
            None
            if preflight is None
            else preflight["execution_provenance"]["lisjong_arena_revision"]
        ),
        "failure": {
            "stage": stage,
            "error_type": type(error).__name__,
            "message": str(error),
        },
        "primary_outcome": OUTCOME_STOP_INVALID,
        "protected_test_evaluated": False,
    }
    document["result_identity"] = _identity(document)
    return document


def validate_result(document: object) -> dict[str, object]:
    if type(document) is not dict:
        raise StageA0ExecutionError("completion result must be an object")
    if document.get("schema_version") != RESULT_SCHEMA_VERSION:
        raise StageA0ExecutionError("unsupported completion result schema")
    if document.get("execution_protocol_id") != EXECUTION_PROTOCOL_ID:
        raise StageA0ExecutionError("completion execution protocol drifted")
    if document.get("lock_b_identity") != EXPECTED_LOCK_B_IDENTITY:
        raise StageA0ExecutionError("completion Lock B binding drifted")
    if document.get("protected_test_evaluated") is not False:
        raise StageA0ExecutionError("completion claims protected TEST exposure")
    if document.get("result_identity") != _identity(document):
        raise StageA0ExecutionError("completion result identity mismatch")

    if document.get("primary_outcome") == OUTCOME_STOP_INVALID:
        failure = document.get("failure")
        if type(failure) is not dict:
            raise StageA0ExecutionError("STOP / INVALID result needs a failure block")
        if set(failure) != {"stage", "error_type", "message"}:
            raise StageA0ExecutionError("STOP / INVALID failure fields are invalid")
        if any(type(failure[name]) is not str for name in failure):
            raise StageA0ExecutionError("STOP / INVALID failure values must be strings")
        if not failure["stage"] or not failure["error_type"]:
            raise StageA0ExecutionError("STOP / INVALID failure identity is empty")
        if "gate" in document or "downstream" in document:
            raise StageA0ExecutionError(
                "STOP / INVALID must not fabricate gate/downstream completion"
            )
        return document

    gate = document.get("gate")
    downstream = document.get("downstream")
    if type(gate) is not dict or type(downstream) is not dict:
        raise StageA0ExecutionError("completion gate/downstream block is invalid")
    downstream_for_outcome = (
        None if downstream.get("execution") == "NOT RUN" else downstream
    )
    rederived = _derive_outcome(
        {"classification": gate["classification"]},
        downstream_for_outcome,
    )
    if document.get("primary_outcome") != rederived:
        raise StageA0ExecutionError("stored primary outcome is not rederivable")
    return document


def save_result(document: dict[str, object], path) -> None:
    validate_result(document)
    write_new_artifact_file(Path(path), canonical_json_text(document))


def load_result(path) -> dict[str, object]:
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
        document = json.loads(text)
    except (OSError, json.JSONDecodeError) as error:
        raise StageA0ExecutionError("completion result cannot be read") from error
    if canonical_json_text(document) != text:
        raise StageA0ExecutionError("completion result is not canonical JSON")
    return validate_result(document)


__all__ = [
    "build_invalid_result",
    "build_result",
    "load_result",
    "save_result",
    "validate_result",
]
