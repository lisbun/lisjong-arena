"""Pre-result execution lock for Arena #222."""

import importlib.metadata
import math
import platform
import sysconfig

from lisjong_engine.rules import RuleSet

from lisjong_arena._execution_safety import require_clean_arena_head
from lisjong_arena.phase4_raw_corpus.extraction import phase4_provenance
from lisjong_arena.stage3_mix_pilot.generation import _provenance_value

from .data import coverage_value, load_retained_records
from .evaluation import baseline_log_loss, fit_train_prevalence
from .protocol import (
    BASELINE_TOLERANCE,
    EXPECTED_BASELINE_LOG_LOSS,
    EXPECTED_VALIDATION_ALL_ZERO_ROWS,
    EXPECTED_VALIDATION_CELLS,
    EXPECTED_VALIDATION_HANCHAN,
    EXPECTED_VALIDATION_ROWS,
    EXPECTED_VALIDATION_UNAVAILABLE_ROWS,
    ROLE,
    SCHEMA,
    ClassicalWaitError,
    digest,
    evaluation_value,
    exact,
    feature_value,
    identity,
    retained_value,
    solver_value,
)

LOCK_FIELDS = (
    "schema",
    "role",
    "retained",
    "retained_readback",
    "provenance",
    "runtime",
    "features",
    "solver",
    "evaluation",
    "baseline_reference",
    "artifact_contract",
    "artifact_audit",
    "result_exposed",
    "execution_decision",
)

RUNTIME_FIELDS = (
    "python",
    "torch",
    "riichienv",
    "platform",
    "device",
    "torch_threads",
    "deterministic_algorithms",
    "free_threaded",
)


def _runtime() -> dict[str, object]:
    import torch

    torch.use_deterministic_algorithms(True)
    torch.set_num_threads(1)
    return {
        "python": platform.python_version(),
        "torch": torch.__version__,
        "riichienv": importlib.metadata.version("riichienv"),
        "platform": platform.platform(),
        "device": "cpu",
        "torch_threads": torch.get_num_threads(),
        "deterministic_algorithms": torch.are_deterministic_algorithms_enabled(),
        "free_threaded": bool(sysconfig.get_config_var("Py_GIL_DISABLED")),
    }


def validate_runtime(value: object) -> dict[str, object]:
    if type(value) is not dict or set(value) != set(RUNTIME_FIELDS):
        raise ClassicalWaitError("runtime fields are not exact")
    if type(value["python"]) is not str or not value["python"].startswith("3.14."):
        raise ClassicalWaitError("Arena #222 requires ordinary CPython 3.14")
    if type(value["torch"]) is not str or not value["torch"].startswith("2.13.0"):
        raise ClassicalWaitError("Arena #222 requires PyTorch 2.13.0")
    exact(value["riichienv"], "0.4.10", "RiichiEnv version")
    if type(value["platform"]) is not str or not value["platform"].strip():
        raise ClassicalWaitError("runtime platform is invalid")
    exact(value["device"], "cpu", "solver device")
    exact(value["torch_threads"], 1, "torch thread count")
    exact(value["deterministic_algorithms"], True, "deterministic algorithms")
    exact(value["free_threaded"], False, "free-threaded status")
    return value


def _validate_provenance(value: object) -> dict[str, object]:
    if type(value) is not dict or type(value.get("source_revisions")) is not dict:
        raise ClassicalWaitError("provenance is missing source revisions")
    revisions = value["source_revisions"]
    if set(revisions) != {"lisjong", "lisjong_engine", "lisjong_arena"}:
        raise ClassicalWaitError("source revision keys are not exact")
    for name, revision in revisions.items():
        digest(revision, f"{name} revision", 40)
    exact(value.get("fully_resolved"), True, "fully resolved provenance")
    return value


def _baseline_reference(
    train: tuple, validation: tuple
) -> tuple[dict[str, object], dict[str, object]]:
    coverage = coverage_value(validation)
    exact(
        coverage["eligible_hanchan"],
        EXPECTED_VALIDATION_HANCHAN,
        "VALIDATION eligible hanchan",
    )
    exact(
        coverage["eligible_rows"],
        EXPECTED_VALIDATION_ROWS,
        "VALIDATION eligible rows",
    )
    exact(
        coverage["eligible_cells"],
        EXPECTED_VALIDATION_CELLS,
        "VALIDATION eligible cells",
    )
    exact(
        coverage["unavailable_rows"],
        EXPECTED_VALIDATION_UNAVAILABLE_ROWS,
        "VALIDATION unavailable rows",
    )
    exact(
        coverage["all_zero_rows"],
        EXPECTED_VALIDATION_ALL_ZERO_ROWS,
        "VALIDATION all-zero rows",
    )
    baseline = fit_train_prevalence(train)
    loss = baseline_log_loss(validation, baseline)
    if not math.isclose(
        loss,
        EXPECTED_BASELINE_LOG_LOSS,
        rel_tol=0.0,
        abs_tol=BASELINE_TOLERANCE,
    ):
        raise ClassicalWaitError("exact #172 prevalence log loss was not reproduced")
    return baseline, {
        "baseline_identity": identity(baseline),
        "validation_log_loss": loss,
        "expected_validation_log_loss": EXPECTED_BASELINE_LOG_LOSS,
        "absolute_tolerance": BASELINE_TOLERANCE,
        "validation_coverage": coverage,
    }


def validate_lock(value: object) -> dict[str, object]:
    if type(value) is not dict or set(value) != set(LOCK_FIELDS):
        raise ClassicalWaitError("execution lock fields are not exact")
    exact(value["schema"], SCHEMA + "/execution-lock", "lock schema")
    exact(value["role"], ROLE, "lock role")
    exact(value["retained"], retained_value(), "retained contract")
    retained_readback = value["retained_readback"]
    if type(retained_readback) is not dict:
        raise ClassicalWaitError("retained readback must be a JSON object")
    for name in (
        "phase150_execution_lock_identity",
        "population_identity",
        "raw_corpus_identity",
        "dataset_identity",
    ):
        digest(retained_readback.get(name), f"retained {name}")
    exact(
        retained_readback["train_seeds"],
        retained_value()["train_seeds"],
        "TRAIN membership",
    )
    exact(
        retained_readback["validation_seeds"],
        retained_value()["validation_seeds"],
        "VALIDATION membership",
    )
    exact(retained_readback["formal_test"], False, "formal TEST status")
    _validate_provenance(value["provenance"])
    validate_runtime(value["runtime"])
    exact(value["features"], feature_value(), "feature lock")
    exact(value["solver"], solver_value(), "solver lock")
    exact(value["evaluation"], evaluation_value(), "evaluation lock")
    reference = value["baseline_reference"]
    if type(reference) is not dict or set(reference) != {
        "baseline_identity",
        "validation_log_loss",
        "expected_validation_log_loss",
        "absolute_tolerance",
        "validation_coverage",
    }:
        raise ClassicalWaitError("baseline reference fields are not exact")
    digest(reference["baseline_identity"], "baseline identity")
    exact(
        reference["expected_validation_log_loss"],
        EXPECTED_BASELINE_LOG_LOSS,
        "baseline reference",
    )
    exact(reference["absolute_tolerance"], BASELINE_TOLERANCE, "baseline tolerance")
    if not math.isclose(
        reference["validation_log_loss"],
        EXPECTED_BASELINE_LOG_LOSS,
        rel_tol=0.0,
        abs_tol=BASELINE_TOLERANCE,
    ):
        raise ClassicalWaitError("locked prevalence reference differs from #172")
    exact(
        value["artifact_contract"],
        {
            "execution_lock": "execution-lock.json",
            "feature_summary": "feature-summary.json",
            "classical_model": "classical-model/model.json",
            "result": "result.json",
            "write_once": True,
            "generated_artifacts_committed": False,
        },
        "artifact contract",
    )
    if type(value["artifact_audit"]) is not str or not value["artifact_audit"].strip():
        raise ClassicalWaitError("execution lock requires a retained-artifact audit")
    exact(value["result_exposed"], False, "pre-exposure result flag")
    exact(
        value["execution_decision"],
        "ONE-SHOT DEVELOPMENT EVALUATION AFTER MERGE",
        "execution decision",
    )
    return value


def current_receipt(
    *,
    arena_revision: str,
    corpus_root: str,
    artifact_audit: str,
) -> dict[str, object]:
    clean_head = require_clean_arena_head()
    provenance = _provenance_value(phase4_provenance(RuleSet.default()))
    _validate_provenance(provenance)
    exact(
        clean_head,
        provenance["source_revisions"]["lisjong_arena"],
        "clean Arena HEAD against installed execution provenance",
    )
    exact(clean_head, arena_revision, "clean Arena HEAD against execution target")
    train, validation, retained_readback = load_retained_records(corpus_root)
    _baseline, baseline_reference = _baseline_reference(train, validation)
    receipt = {
        "schema": SCHEMA + "/execution-lock",
        "role": ROLE,
        "retained": retained_value(),
        "retained_readback": retained_readback,
        "provenance": provenance,
        "runtime": _runtime(),
        "features": feature_value(),
        "solver": solver_value(),
        "evaluation": evaluation_value(),
        "baseline_reference": baseline_reference,
        "artifact_contract": {
            "execution_lock": "execution-lock.json",
            "feature_summary": "feature-summary.json",
            "classical_model": "classical-model/model.json",
            "result": "result.json",
            "write_once": True,
            "generated_artifacts_committed": False,
        },
        "artifact_audit": artifact_audit,
        "result_exposed": False,
        "execution_decision": "ONE-SHOT DEVELOPMENT EVALUATION AFTER MERGE",
    }
    return validate_lock(receipt)


def require_current_lock(lock: dict, *, corpus_root: str) -> str:
    validate_lock(lock)
    actual = current_receipt(
        arena_revision=lock["provenance"]["source_revisions"]["lisjong_arena"],
        corpus_root=corpus_root,
        artifact_audit=lock["artifact_audit"],
    )
    exact(actual, lock, "live execution lock")
    return identity(lock)


__all__ = [
    "LOCK_FIELDS",
    "RUNTIME_FIELDS",
    "current_receipt",
    "require_current_lock",
    "validate_lock",
    "validate_runtime",
]
