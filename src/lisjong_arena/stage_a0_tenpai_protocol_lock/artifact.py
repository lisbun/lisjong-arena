"""Two-step machine-readable Stage A0 protocol lock artifact (#259)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from lisjong_arena._artifact_io import canonical_json_text, write_new_artifact_file
from lisjong_arena.learned_policy_offline_q.artifact import load_dataset_seed_prefix
from lisjong_arena.single_round_artifact import (
    collect_execution_provenance,
    execution_provenance_to_dict,
)
from lisjong_arena.stage_a0_tenpai_feasibility.report import (
    RETAINED_AUGMENTATION_QUALIFIED,
    load_feasibility_report,
)
from lisjong_arena.stage_a0_tenpai_feasibility.sidecar import load_sidecar

from .baseline import (
    build_train_baseline_parameters,
    validate_train_baseline_parameters,
)
from .materialize import load_public_keys
from .protocol import (
    CANONICAL_WAIT_IMPLEMENTATION_IDENTITY,
    DOWNSTREAM_LISJONG_ENGINE_REVISION,
    DOWNSTREAM_LISJONG_REVISION,
    DOWNSTREAM_RIICHIENV_VERSION,
    DOWNSTREAM_STATUS,
    EXPECTED_258_REPORT_IDENTITY,
    EXPECTED_258_SIDECAR_IDENTITY,
    LOCK_A_SCHEMA_VERSION,
    LOCK_B_SCHEMA_VERSION,
    PROTECTED_TEST_SEEDS,
    QUALIFIED_ROUTE,
    RETAINED_DATASET_IDENTITY,
    SCIENTIFIC_SEEDS,
    SOURCE_LISJONG_ENGINE_REVISION,
    SOURCE_LISJONG_REVISION,
    SOURCE_PYTHON_VERSION,
    SOURCE_RIICHIENV_VERSION,
    TRAIN_SEEDS,
    VALIDATION_SEEDS,
    StageA0ProtocolLockError,
    contract_fingerprint,
    historical_precision_document,
    static_contract_document,
)

LOCKED_ENABLED_OUTCOME = "A0 PROTOCOL LOCKED — DOWNSTREAM ENABLED"
LOCKED_NOT_POWERED_OUTCOME = "A0 PROTOCOL LOCKED — DOWNSTREAM NOT POWERED"
PROTOCOL_BLOCKED_OUTCOME = "A0 PROTOCOL BLOCKED"
STOP_INVALID_OUTCOME = "STOP / INVALID"

_LOCK_A_FIELDS = {
    "schema_version",
    "lock_identity",
    "contract_fingerprint",
    "contract",
    "prerequisite",
    "dataset",
    "creation_provenance",
    "exposure",
}
_LOCK_B_FIELDS = {
    "schema_version",
    "lock_identity",
    "lock_a_identity",
    "contract_fingerprint",
    "contract",
    "data",
    "baseline_parameters",
    "execution_provenance",
    "precision_preflight",
    "downstream_status",
    "hard_outcome",
    "exposure",
}


def _identity(document: dict[str, object]) -> str:
    logical = {
        name: value for name, value in document.items() if name != "lock_identity"
    }
    return hashlib.sha256(canonical_json_text(logical).encode("utf-8")).hexdigest()


def _partition_identity(manifest: dict, seeds: tuple[int, ...], split_name: str) -> str:
    games = manifest["games"]
    offset = 0
    records = []
    wanted = set(seeds)
    for game in games:
        count = game["row_count"]
        if game["seed"] in wanted:
            records.append(
                {
                    "seed": game["seed"],
                    "split": game["split"],
                    "row_offset": offset,
                    "row_count": count,
                    "scores": game["scores"],
                    "ranks": game["ranks"],
                }
            )
        offset += count
    if [record["seed"] for record in records] != list(seeds):
        raise StageA0ProtocolLockError(f"{split_name} game metadata is incomplete")
    document = {
        "dataset_identity": RETAINED_DATASET_IDENTITY,
        "split": split_name,
        "ordered_seeds": list(seeds),
        "games": records,
    }
    return hashlib.sha256(canonical_json_text(document).encode("utf-8")).hexdigest()


def build_lock_a(
    *,
    dataset_path,
    feasibility_report_path,
    qualified_sidecar_path,
) -> dict[str, object]:
    """Build population/semantics Lock A without reading VALIDATION payloads."""
    dataset = load_dataset_seed_prefix(dataset_path, TRAIN_SEEDS)
    if dataset.identity != RETAINED_DATASET_IDENTITY:
        raise StageA0ProtocolLockError("Lock A dataset is not the retained #259 corpus")

    report = load_feasibility_report(feasibility_report_path)
    if report.identity != EXPECTED_258_REPORT_IDENTITY:
        raise StageA0ProtocolLockError(
            "Lock A is not bound to the completed #258 report"
        )
    if report.hard_outcome != RETAINED_AUGMENTATION_QUALIFIED:
        raise StageA0ProtocolLockError(
            "#258 did not finish as retained augmentation qualified"
        )
    if (
        report.document["routes"]["recommended_stage_a0_corpus_route"]
        != QUALIFIED_ROUTE
    ):
        raise StageA0ProtocolLockError(
            "#258 recommended route is not retained augmentation"
        )

    sidecar = load_sidecar(qualified_sidecar_path)
    if sidecar.identity != EXPECTED_258_SIDECAR_IDENTITY:
        raise StageA0ProtocolLockError(
            "Lock A is not bound to the completed #258 sidecar"
        )
    if sidecar.route != QUALIFIED_ROUTE:
        raise StageA0ProtocolLockError(
            "#258 sidecar route is not retained augmentation"
        )
    sidecar_seeds = {cell.row_identity.seed for cell in sidecar.cells}
    if sidecar_seeds != set(TRAIN_SEEDS):
        raise StageA0ProtocolLockError(
            "#258 sidecar must contain exactly retained TRAIN seeds"
        )

    train_identity = _partition_identity(dataset.manifest, TRAIN_SEEDS, "TRAIN")
    validation_identity = _partition_identity(
        dataset.manifest, VALIDATION_SEEDS, "VALIDATION"
    )
    provenance = execution_provenance_to_dict(collect_execution_provenance())
    contract = static_contract_document()
    document: dict[str, object] = {
        "schema_version": LOCK_A_SCHEMA_VERSION,
        "contract_fingerprint": contract_fingerprint(),
        "contract": contract,
        "prerequisite": {
            "issue": "lisbun/lisjong-arena#258",
            "report_identity": report.identity,
            "sidecar_identity": sidecar.identity,
            "route": QUALIFIED_ROUTE,
            "hard_outcome": RETAINED_AUGMENTATION_QUALIFIED,
        },
        "dataset": {
            "identity": dataset.identity,
            "train_partition_identity": train_identity,
            "validation_partition_identity": validation_identity,
            "train_seeds": list(TRAIN_SEEDS),
            "validation_seeds": list(VALIDATION_SEEDS),
            "protected_test_seeds_unread": list(PROTECTED_TEST_SEEDS),
        },
        "creation_provenance": provenance,
        "exposure": {
            "model_training_executed": False,
            "validation_model_inference_executed": False,
            "validation_target_summary_exposed": False,
            "protected_test_payload_read": False,
            "interactive_execution_executed": False,
        },
    }
    document["lock_identity"] = _identity(document)
    return document


def _load_json_lock(path: str | Path) -> tuple[dict, str]:
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise StageA0ProtocolLockError("protocol lock is invalid JSON") from exc
    if type(value) is not dict:
        raise StageA0ProtocolLockError("protocol lock must be an object")
    if canonical_json_text(value) != text:
        raise StageA0ProtocolLockError("protocol lock bytes are not canonical JSON")
    return value, text


def load_lock_a(path: str | Path) -> dict[str, object]:
    document, _ = _load_json_lock(path)
    if set(document) != _LOCK_A_FIELDS:
        raise StageA0ProtocolLockError("Lock A fields are invalid")
    if document["schema_version"] != LOCK_A_SCHEMA_VERSION:
        raise StageA0ProtocolLockError("unsupported Lock A schema")
    if document["contract_fingerprint"] != contract_fingerprint():
        raise StageA0ProtocolLockError(
            "Lock A contract fingerprint differs from implementation"
        )
    if document["contract"] != static_contract_document():
        raise StageA0ProtocolLockError(
            "Lock A static contract differs from implementation"
        )
    if _identity(document) != document["lock_identity"]:
        raise StageA0ProtocolLockError("Lock A identity does not match its content")
    prerequisite = document["prerequisite"]
    if prerequisite != {
        "issue": "lisbun/lisjong-arena#258",
        "report_identity": EXPECTED_258_REPORT_IDENTITY,
        "sidecar_identity": EXPECTED_258_SIDECAR_IDENTITY,
        "route": QUALIFIED_ROUTE,
        "hard_outcome": RETAINED_AUGMENTATION_QUALIFIED,
    }:
        raise StageA0ProtocolLockError("Lock A #258 prerequisite binding drifted")
    dataset = document["dataset"]
    if dataset["identity"] != RETAINED_DATASET_IDENTITY:
        raise StageA0ProtocolLockError("Lock A retained dataset identity drifted")
    if dataset["train_seeds"] != list(TRAIN_SEEDS):
        raise StageA0ProtocolLockError("Lock A TRAIN population drifted")
    if dataset["validation_seeds"] != list(VALIDATION_SEEDS):
        raise StageA0ProtocolLockError("Lock A VALIDATION population drifted")
    if dataset["protected_test_seeds_unread"] != list(PROTECTED_TEST_SEEDS):
        raise StageA0ProtocolLockError("Lock A protected TEST population drifted")
    exposure = document["exposure"]
    if type(exposure) is not dict or any(exposure.values()):
        raise StageA0ProtocolLockError("Lock A records forbidden result exposure")
    return document


def save_lock(document: dict[str, object], path: str | Path) -> Path:
    path = Path(path)
    write_new_artifact_file(path, canonical_json_text(document))
    return path


def _require_downstream_execution_provenance(provenance: dict[str, object]) -> None:
    expected = {
        "lisjong_revision": DOWNSTREAM_LISJONG_REVISION,
        "lisjong_engine_revision": DOWNSTREAM_LISJONG_ENGINE_REVISION,
        "riichienv_version": DOWNSTREAM_RIICHIENV_VERSION,
    }
    for name, value in expected.items():
        if provenance.get(name) != value:
            raise StageA0ProtocolLockError(
                f"Lock B execution provenance {name} is {provenance.get(name)!r}, "
                f"expected {value!r}"
            )


def _scientific_cell_key(cell) -> tuple[int, int, int, int, int]:
    identity = cell.row_identity
    return (
        identity.seed,
        identity.step_ordinal,
        identity.decision_ordinal,
        identity.actor_seat,
        cell.identity.viewer_relative_offset,
    )


def _require_scientific_data_binding(sidecar, public_keys) -> None:
    """Fail closed on provenance drift or incomplete player-safe key coverage."""
    expected_source = {
        "lisjong_revision": SOURCE_LISJONG_REVISION,
        "lisjong_engine_revision": SOURCE_LISJONG_ENGINE_REVISION,
        "riichienv_version": SOURCE_RIICHIENV_VERSION,
        "python_version": SOURCE_PYTHON_VERSION,
    }
    sidecar_provenance = sidecar.manifest["provenance"]
    for name, expected in expected_source.items():
        if sidecar_provenance.get(name) != expected:
            raise StageA0ProtocolLockError(
                f"scientific sidecar provenance {name} drifted from the qualified "
                "retained source semantics"
            )
    if public_keys.manifest["provenance"] != sidecar_provenance:
        raise StageA0ProtocolLockError(
            "public-key provenance is not identical to the scientific sidecar"
        )

    sidecar_keys = {_scientific_cell_key(cell) for cell in sidecar.cells}
    public_key_set = {record.cell_key for record in public_keys.records}
    if len(sidecar_keys) != len(sidecar.cells):
        raise StageA0ProtocolLockError(
            "scientific sidecar contains duplicate opponent-cell identities"
        )
    if len(public_key_set) != len(public_keys.records):
        raise StageA0ProtocolLockError(
            "public-key artifact contains duplicate opponent-cell identities"
        )
    if sidecar_keys != public_key_set:
        raise StageA0ProtocolLockError(
            "public-key artifact does not cover the exact TRAIN+VALIDATION sidecar "
            "cell population"
        )


def build_lock_b(
    *,
    lock_a_path,
    scientific_sidecar_path,
    public_keys_path,
) -> dict[str, object]:
    """Finalize TRAIN-only parameters and downstream power decision before training."""
    lock_a = load_lock_a(lock_a_path)
    sidecar = load_sidecar(scientific_sidecar_path)
    public_keys = load_public_keys(public_keys_path)

    if sidecar.manifest["protocol"]["source_identity"] != RETAINED_DATASET_IDENTITY:
        raise StageA0ProtocolLockError("scientific sidecar source identity drifted")
    sidecar_seeds = {cell.row_identity.seed for cell in sidecar.cells}
    if sidecar_seeds != set(SCIENTIFIC_SEEDS):
        raise StageA0ProtocolLockError(
            "scientific sidecar must contain exactly TRAIN+VALIDATION seed prefix"
        )
    if sidecar_seeds.intersection(PROTECTED_TEST_SEEDS):
        raise StageA0ProtocolLockError("scientific sidecar contains protected TEST")
    if (
        sidecar.manifest["canonical_wait_implementation"]["implementation_identity"]
        != CANONICAL_WAIT_IMPLEMENTATION_IDENTITY
    ):
        raise StageA0ProtocolLockError("scientific sidecar exact-wait identity drifted")
    if public_keys.manifest["sidecar_identity"] != sidecar.identity:
        raise StageA0ProtocolLockError("public-key artifact is not bound to sidecar")
    _require_scientific_data_binding(sidecar, public_keys)

    baseline = build_train_baseline_parameters(sidecar, public_keys)
    validate_train_baseline_parameters(baseline)
    provenance = execution_provenance_to_dict(collect_execution_provenance())
    _require_downstream_execution_provenance(provenance)
    if (
        provenance["lisjong_arena_revision"]
        != lock_a["creation_provenance"]["lisjong_arena_revision"]
    ):
        raise StageA0ProtocolLockError(
            "Arena revision changed between Lock A and Lock B; create a fresh protocol "
            "decision instead of silently carrying the old lock forward"
        )

    precision = historical_precision_document()
    if precision["classification"] != DOWNSTREAM_STATUS:
        raise StageA0ProtocolLockError(
            "historical precision preflight no longer enables the checked-in plan"
        )

    contract = static_contract_document()
    hard_outcome = (
        LOCKED_ENABLED_OUTCOME
        if DOWNSTREAM_STATUS == "A0 DOWNSTREAM ENABLED"
        else LOCKED_NOT_POWERED_OUTCOME
    )
    document: dict[str, object] = {
        "schema_version": LOCK_B_SCHEMA_VERSION,
        "lock_a_identity": lock_a["lock_identity"],
        "contract_fingerprint": contract_fingerprint(),
        "contract": contract,
        "data": {
            "retained_dataset_identity": RETAINED_DATASET_IDENTITY,
            "train_partition_identity": lock_a["dataset"]["train_partition_identity"],
            "validation_partition_identity": lock_a["dataset"][
                "validation_partition_identity"
            ],
            "scientific_sidecar_identity": sidecar.identity,
            "public_keys_identity": public_keys.identity,
            "scientific_source_provenance": dict(sidecar.manifest["provenance"]),
            "scientific_seeds": list(SCIENTIFIC_SEEDS),
            "protected_test_seeds_unread": list(PROTECTED_TEST_SEEDS),
        },
        "baseline_parameters": baseline,
        "execution_provenance": provenance,
        "precision_preflight": precision,
        "downstream_status": DOWNSTREAM_STATUS,
        "hard_outcome": hard_outcome,
        "exposure": {
            "model_training_executed": False,
            "validation_model_inference_executed": False,
            "validation_target_summary_exposed": False,
            "protected_test_payload_read": False,
            "interactive_execution_executed": False,
        },
    }
    document["lock_identity"] = _identity(document)
    return document


def load_lock_b(path: str | Path) -> dict[str, object]:
    document, _ = _load_json_lock(path)
    if set(document) != _LOCK_B_FIELDS:
        raise StageA0ProtocolLockError("Lock B fields are invalid")
    if document["schema_version"] != LOCK_B_SCHEMA_VERSION:
        raise StageA0ProtocolLockError("unsupported Lock B schema")
    if document["contract_fingerprint"] != contract_fingerprint():
        raise StageA0ProtocolLockError(
            "Lock B contract fingerprint differs from implementation"
        )
    if document["contract"] != static_contract_document():
        raise StageA0ProtocolLockError(
            "Lock B static contract differs from implementation"
        )
    if document["precision_preflight"] != historical_precision_document():
        raise StageA0ProtocolLockError("Lock B precision preflight drifted")
    validate_train_baseline_parameters(document["baseline_parameters"])
    if document["downstream_status"] != DOWNSTREAM_STATUS:
        raise StageA0ProtocolLockError("Lock B downstream classification drifted")
    expected_outcome = (
        LOCKED_ENABLED_OUTCOME
        if DOWNSTREAM_STATUS == "A0 DOWNSTREAM ENABLED"
        else LOCKED_NOT_POWERED_OUTCOME
    )
    if document["hard_outcome"] != expected_outcome:
        raise StageA0ProtocolLockError(
            "Lock B hard outcome does not match downstream status"
        )
    data = document["data"]
    if data["retained_dataset_identity"] != RETAINED_DATASET_IDENTITY:
        raise StageA0ProtocolLockError("Lock B dataset identity drifted")
    if data["scientific_seeds"] != list(SCIENTIFIC_SEEDS):
        raise StageA0ProtocolLockError("Lock B scientific seed population drifted")
    expected_source = {
        "lisjong_revision": SOURCE_LISJONG_REVISION,
        "lisjong_engine_revision": SOURCE_LISJONG_ENGINE_REVISION,
        "riichienv_version": SOURCE_RIICHIENV_VERSION,
        "python_version": SOURCE_PYTHON_VERSION,
    }
    for name, expected in expected_source.items():
        if data["scientific_source_provenance"].get(name) != expected:
            raise StageA0ProtocolLockError(
                f"Lock B scientific source provenance {name} drifted"
            )
    if data["protected_test_seeds_unread"] != list(PROTECTED_TEST_SEEDS):
        raise StageA0ProtocolLockError("Lock B protected TEST population drifted")
    exposure = document["exposure"]
    if type(exposure) is not dict or any(exposure.values()):
        raise StageA0ProtocolLockError("Lock B records forbidden result exposure")
    _require_downstream_execution_provenance(document["execution_provenance"])
    if _identity(document) != document["lock_identity"]:
        raise StageA0ProtocolLockError("Lock B identity does not match its content")
    return document


__all__ = [
    "LOCKED_ENABLED_OUTCOME",
    "LOCKED_NOT_POWERED_OUTCOME",
    "build_lock_a",
    "build_lock_b",
    "load_lock_a",
    "load_lock_b",
    "save_lock",
]
