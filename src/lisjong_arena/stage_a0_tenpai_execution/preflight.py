"""Phase E0 preflight for #262."""

from __future__ import annotations

import hashlib

from lisjong_arena._artifact_io import canonical_json_text, write_new_artifact_file
from lisjong_arena.single_round_artifact import (
    collect_execution_provenance,
    execution_provenance_to_dict,
)
from lisjong_arena.stage_a0_tenpai_protocol_lock import protocol as locked
from lisjong_arena.stage_a0_tenpai_protocol_lock.baseline import (
    validate_train_baseline_parameters,
)

from .data import load_scientific_data
from .errors import StageA0PreflightError
from .protocol import (
    EXECUTION_PROTOCOL_ID,
    EXPECTED_LOCK_B_IDENTITY,
    ISSUE_IDENTITY,
    PREFLIGHT_PASS,
    PREFLIGHT_SCHEMA_VERSION,
)


def _identity(document: dict[str, object]) -> str:
    logical = {key: value for key, value in document.items() if key != "preflight_identity"}
    return hashlib.sha256(canonical_json_text(logical).encode("utf-8")).hexdigest()


def run_preflight(
    *,
    lock_b_path,
    dataset_path,
    sidecar_path,
    public_keys_path,
) -> dict[str, object]:
    scientific = load_scientific_data(
        lock_b_path=lock_b_path,
        dataset_path=dataset_path,
        sidecar_path=sidecar_path,
        public_keys_path=public_keys_path,
    )
    lock_b = scientific.lock_b
    if lock_b["lock_identity"] != EXPECTED_LOCK_B_IDENTITY:
        raise StageA0PreflightError("unexpected Lock B identity")

    if set(locked.TRAIN_SEEDS) & set(locked.VALIDATION_SEEDS):
        raise StageA0PreflightError("TRAIN and VALIDATION seed populations overlap")
    if set(locked.SCIENTIFIC_SEEDS) & set(locked.PROTECTED_TEST_SEEDS):
        raise StageA0PreflightError("scientific seeds overlap protected TEST")
    technical_smoke = tuple(lock_b["contract"]["route"]["technical_smoke_seeds_excluded"])
    if set(technical_smoke) & set(locked.SCIENTIFIC_SEEDS):
        raise StageA0PreflightError("technical smoke population entered scientific data")

    baseline = validate_train_baseline_parameters(lock_b["baseline_parameters"])
    if baseline["train_seeds"] != list(locked.TRAIN_SEEDS):
        raise StageA0PreflightError("baseline TRAIN seed binding drifted")

    training = lock_b["contract"]["training"]
    if training["training_seeds"] != list(locked.TRAINING_SEEDS):
        raise StageA0PreflightError("training seed population drifted")
    if training["interactive_anchor_seed"] != locked.INTERACTIVE_ANCHOR_SEED:
        raise StageA0PreflightError("interactive anchor seed drifted")
    if "lambda_tenpai" in training:
        raise StageA0PreflightError("unexpected legacy lambda field")
    if training["auxiliary_head"]["lambda_tenpai"] != locked.LAMBDA_TENPAI:
        raise StageA0PreflightError("lambda_tenpai drifted")
    if training["auxiliary_head"]["class_treatment"] != locked.CLASS_IMBALANCE_TREATMENT:
        raise StageA0PreflightError("class treatment drifted")

    downstream = lock_b["contract"]["downstream"]
    if downstream["status"] != locked.DOWNSTREAM_STATUS:
        raise StageA0PreflightError("downstream status drifted")
    if downstream["opponent"]["identity"] != locked.DOWNSTREAM_OPPONENT_IDENTITY:
        raise StageA0PreflightError("downstream opponent identity drifted")
    if downstream["ordered_seeds"] != list(locked.DOWNSTREAM_SEEDS):
        raise StageA0PreflightError("downstream ordered seeds drifted")
    if downstream["rotation_count"] != locked.DOWNSTREAM_ROTATIONS:
        raise StageA0PreflightError("downstream rotation count drifted")
    if downstream["total_games"] != locked.DOWNSTREAM_TOTAL_GAMES:
        raise StageA0PreflightError("downstream game budget drifted")

    current = execution_provenance_to_dict(collect_execution_provenance())
    expected_runtime = {
        "lisjong_revision": locked.DOWNSTREAM_LISJONG_REVISION,
        "lisjong_engine_revision": locked.DOWNSTREAM_LISJONG_ENGINE_REVISION,
        "riichienv_version": locked.DOWNSTREAM_RIICHIENV_VERSION,
    }
    for name, expected in expected_runtime.items():
        if current.get(name) != expected:
            raise StageA0PreflightError(
                f"current {name} is {current.get(name)!r}, expected locked {expected!r}"
            )

    # The privileged sidecar is a separate artifact and is never concatenated
    # into the retained public feature payload. The only Policy feature contract
    # consumed by training/serving remains the locked 8204-dimensional schema.
    feature_contract = lock_b["contract"]["feature_and_action"]
    if feature_contract["feature_dimension"] != locked.FEATURE_DIMENSION:
        raise StageA0PreflightError("public Policy feature dimension drifted")
    if scientific.dataset.manifest["feature"]["dimension"] != locked.FEATURE_DIMENSION:
        raise StageA0PreflightError("retained feature artifact dimension drifted")

    document: dict[str, object] = {
        "schema_version": PREFLIGHT_SCHEMA_VERSION,
        "execution_protocol_id": EXECUTION_PROTOCOL_ID,
        "issue": ISSUE_IDENTITY,
        "lock_b_identity": lock_b["lock_identity"],
        "outcome": PREFLIGHT_PASS,
        "execution_provenance": current,
        "data": {
            "retained_dataset_identity": scientific.dataset.identity,
            "scientific_sidecar_identity": scientific.sidecar.identity,
            "public_keys_identity": scientific.public_keys.identity,
            "scientific_seeds": list(locked.SCIENTIFIC_SEEDS),
            "train_seeds": list(locked.TRAIN_SEEDS),
            "validation_seeds": list(locked.VALIDATION_SEEDS),
            "protected_test_seeds_unread": list(locked.PROTECTED_TEST_SEEDS),
            "protected_test_payload_read": False,
            "row_count": scientific.dataset.row_count,
            "opponent_cell_count": len(scientific.sidecar.cells),
        },
        "baseline_fingerprint": baseline["fingerprint"],
        "training": {
            "training_seeds": list(locked.TRAINING_SEEDS),
            "interactive_anchor_seed": locked.INTERACTIVE_ANCHOR_SEED,
            "lambda_tenpai": locked.LAMBDA_TENPAI,
            "class_treatment": locked.CLASS_IMBALANCE_TREATMENT,
        },
        "downstream": {
            "status": locked.DOWNSTREAM_STATUS,
            "opponent_identity": locked.DOWNSTREAM_OPPONENT_IDENTITY,
            "opponent_population": locked.DOWNSTREAM_OPPONENT_POPULATION,
            "ordered_seeds": list(locked.DOWNSTREAM_SEEDS),
            "rotation_count": locked.DOWNSTREAM_ROTATIONS,
            "games_per_arm": locked.DOWNSTREAM_GAMES_PER_ARM,
            "total_games": locked.DOWNSTREAM_TOTAL_GAMES,
        },
        "information_flow": {
            "policy_feature_source": "retained player-safe 8204-feature payload only",
            "privileged_sidecar_role": "training-only Tenpai targets and diagnostics",
            "privileged_truth_in_policy_input": False,
        },
    }
    document["preflight_identity"] = _identity(document)
    return document


def save_preflight(document: dict[str, object], path) -> None:
    if document.get("outcome") != PREFLIGHT_PASS:
        raise StageA0PreflightError("only a successful preflight may be published")
    if document.get("preflight_identity") != _identity(document):
        raise StageA0PreflightError("preflight identity does not match its content")
    write_new_artifact_file(path, canonical_json_text(document))


__all__ = ["run_preflight", "save_preflight"]
