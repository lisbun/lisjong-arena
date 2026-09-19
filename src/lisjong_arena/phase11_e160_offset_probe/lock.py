"""Pre-result execution lock for Arena #291."""

import importlib.metadata
import math
import platform
import sysconfig

from lisjong_engine.rules import RuleSet

from lisjong_arena._execution_safety import require_clean_arena_head
from lisjong_arena.phase4_raw_corpus.extraction import phase4_provenance
from lisjong_arena.phase11_public_riichi_wait_readout.retained import load_retained
from lisjong_arena.stage3_mix_pilot.generation import _provenance_value

from .data import (
    latent_reference,
    load_latent_records,
    retained_receipt,
    validate_centering,
)
from .evaluation import baseline_log_loss, fit_train_prevalence
from .protocol import (
    BASELINE_TOLERANCE,
    EXPECTED_BASELINE_LOG_LOSS,
    EXPECTED_TRAIN_ALL_ZERO_ROWS,
    EXPECTED_TRAIN_CELLS,
    EXPECTED_TRAIN_HANCHAN,
    EXPECTED_TRAIN_ROWS,
    EXPECTED_TRAIN_UNAVAILABLE_ROWS,
    EXPECTED_VALIDATION_ALL_ZERO_ROWS,
    EXPECTED_VALIDATION_CELLS,
    EXPECTED_VALIDATION_HANCHAN,
    EXPECTED_VALIDATION_ROWS,
    EXPECTED_VALIDATION_UNAVAILABLE_ROWS,
    ROLE,
    SCHEMA,
    E160OffsetProbeError,
    centering_value,
    digest,
    evaluation_value,
    exact,
    identity,
    probe_value,
    representation_value,
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
    "representation",
    "centering_contract",
    "probe",
    "solver",
    "evaluation",
    "baseline_reference",
    "latent_reference",
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
    if torch.cuda.is_available():
        raise E160OffsetProbeError("Arena #291 requires the locked CPU-only runtime")
    return {
        "python": platform.python_version(),
        "torch": str(torch.__version__),
        "riichienv": importlib.metadata.version("riichienv"),
        "platform": platform.platform(),
        "device": "cpu",
        "torch_threads": torch.get_num_threads(),
        "deterministic_algorithms": torch.are_deterministic_algorithms_enabled(),
        "free_threaded": bool(sysconfig.get_config_var("Py_GIL_DISABLED")),
    }


def validate_runtime(value: object) -> dict[str, object]:
    if type(value) is not dict or set(value) != set(RUNTIME_FIELDS):
        raise E160OffsetProbeError("runtime fields are not exact")
    if type(value["python"]) is not str or not value["python"].startswith("3.14."):
        raise E160OffsetProbeError("Arena #291 requires ordinary CPython 3.14")
    if type(value["torch"]) is not str or not value["torch"].startswith("2.13.0"):
        raise E160OffsetProbeError("Arena #291 requires PyTorch 2.13.0")
    exact(value["riichienv"], "0.4.10", "RiichiEnv version")
    if type(value["platform"]) is not str or not value["platform"].strip():
        raise E160OffsetProbeError("runtime platform is invalid")
    exact(value["device"], "cpu", "solver device")
    exact(value["torch_threads"], 1, "torch thread count")
    exact(value["deterministic_algorithms"], True, "deterministic algorithms")
    exact(value["free_threaded"], False, "free-threaded status")
    return value


def _validate_provenance(value: object) -> dict[str, object]:
    if type(value) is not dict or type(value.get("source_revisions")) is not dict:
        raise E160OffsetProbeError("provenance is missing source revisions")
    revisions = value["source_revisions"]
    if set(revisions) != {"lisjong", "lisjong_engine", "lisjong_arena"}:
        raise E160OffsetProbeError("source revision keys are not exact")
    for name, revision in revisions.items():
        digest(revision, f"{name} revision", 40)
    exact(value.get("fully_resolved"), True, "fully resolved provenance")
    return value


def _current_provenance(clean_head: str) -> dict[str, object]:
    provenance = _provenance_value(phase4_provenance(RuleSet.default()))
    if (
        type(provenance) is not dict
        or type(provenance.get("source_revisions")) is not dict
    ):
        raise E160OffsetProbeError("provenance is missing source revisions")
    revisions = provenance["source_revisions"]
    arena_revision = revisions.get("lisjong_arena")
    if arena_revision is None:
        revisions["lisjong_arena"] = clean_head
        provenance["fully_resolved"] = all(
            type(revisions.get(name)) is str
            for name in ("lisjong", "lisjong_engine", "lisjong_arena")
        )
    else:
        exact(
            arena_revision,
            clean_head,
            "installed Arena revision against clean source HEAD",
        )
    return _validate_provenance(provenance)


def _validate_coverage(
    value: dict,
    *,
    hanchan: int,
    rows: int,
    cells: int,
    unavailable: int,
    all_zero: int,
    name: str,
) -> None:
    exact(value["eligible_hanchan"], hanchan, f"{name} eligible hanchan")
    exact(value["eligible_rows"], rows, f"{name} eligible rows")
    exact(value["eligible_cells"], cells, f"{name} eligible cells")
    exact(value["unavailable_rows"], unavailable, f"{name} unavailable rows")
    exact(value["all_zero_rows"], all_zero, f"{name} all-zero rows")


def _validate_latent_reference(value: object) -> dict[str, object]:
    if type(value) is not dict or set(value) != {
        "frozen_state_digest",
        "latent_fingerprint_semantics_id",
        "latent_fingerprint",
        "train_coverage",
        "validation_coverage",
        "centering",
    }:
        raise E160OffsetProbeError("latent reference fields are not exact")
    digest(value["frozen_state_digest"], "frozen state digest")
    digest(value["latent_fingerprint"], "latent fingerprint")
    _validate_coverage(
        value["train_coverage"],
        hanchan=EXPECTED_TRAIN_HANCHAN,
        rows=EXPECTED_TRAIN_ROWS,
        cells=EXPECTED_TRAIN_CELLS,
        unavailable=EXPECTED_TRAIN_UNAVAILABLE_ROWS,
        all_zero=EXPECTED_TRAIN_ALL_ZERO_ROWS,
        name="TRAIN",
    )
    _validate_coverage(
        value["validation_coverage"],
        hanchan=EXPECTED_VALIDATION_HANCHAN,
        rows=EXPECTED_VALIDATION_ROWS,
        cells=EXPECTED_VALIDATION_CELLS,
        unavailable=EXPECTED_VALIDATION_UNAVAILABLE_ROWS,
        all_zero=EXPECTED_VALIDATION_ALL_ZERO_ROWS,
        name="VALIDATION",
    )
    validate_centering(value["centering"])
    return value


def _baseline_reference(train: tuple, validation: tuple) -> dict[str, object]:
    baseline = fit_train_prevalence(train)
    loss = baseline_log_loss(validation, baseline)
    if not math.isclose(
        loss,
        EXPECTED_BASELINE_LOG_LOSS,
        rel_tol=0.0,
        abs_tol=BASELINE_TOLERANCE,
    ):
        raise E160OffsetProbeError(
            "exact retained prevalence log loss was not reproduced"
        )
    return {
        "baseline_identity": identity(baseline),
        "validation_log_loss": loss,
        "expected_validation_log_loss": EXPECTED_BASELINE_LOG_LOSS,
        "absolute_tolerance": BASELINE_TOLERANCE,
    }


def validate_lock(value: object) -> dict[str, object]:
    if type(value) is not dict or set(value) != set(LOCK_FIELDS):
        raise E160OffsetProbeError("execution lock fields are not exact")
    exact(value["schema"], SCHEMA + "/execution-lock", "lock schema")
    exact(value["role"], ROLE, "lock role")
    exact(value["retained"], retained_value(), "retained contract")
    retained = value["retained_readback"]
    if type(retained) is not dict:
        raise E160OffsetProbeError("retained readback must be an object")
    for name in (
        "phase150_execution_lock_identity",
        "population_identity",
        "raw_corpus_identity",
        "dataset_identity",
        "phase167_execution_lock_identity",
        "phase167_result_identity",
        "e160_weights_sha256",
    ):
        digest(retained.get(name), f"retained {name}")
    exact(retained["train_seeds"], retained_value()["train_seeds"], "TRAIN seeds")
    exact(
        retained["validation_seeds"],
        retained_value()["validation_seeds"],
        "VALIDATION seeds",
    )
    exact(retained["formal_test"], False, "formal TEST status")
    _validate_provenance(value["provenance"])
    validate_runtime(value["runtime"])
    exact(value["representation"], representation_value(), "representation lock")
    exact(value["centering_contract"], centering_value(), "centering contract")
    exact(value["probe"], probe_value(), "probe architecture")
    exact(value["solver"], solver_value(), "solver lock")
    exact(value["evaluation"], evaluation_value(), "evaluation lock")
    reference = value["baseline_reference"]
    if type(reference) is not dict or set(reference) != {
        "baseline_identity",
        "validation_log_loss",
        "expected_validation_log_loss",
        "absolute_tolerance",
    }:
        raise E160OffsetProbeError("baseline reference fields are not exact")
    digest(reference["baseline_identity"], "baseline identity")
    exact(
        reference["expected_validation_log_loss"],
        EXPECTED_BASELINE_LOG_LOSS,
        "baseline expected log loss",
    )
    exact(reference["absolute_tolerance"], BASELINE_TOLERANCE, "baseline tolerance")
    if not math.isclose(
        reference["validation_log_loss"],
        EXPECTED_BASELINE_LOG_LOSS,
        rel_tol=0.0,
        abs_tol=BASELINE_TOLERANCE,
    ):
        raise E160OffsetProbeError("locked prevalence reference differs")
    _validate_latent_reference(value["latent_reference"])
    exact(
        value["artifact_contract"],
        {
            "execution_lock": "execution-lock.json",
            "latent_summary": "latent-summary.json",
            "model": "e160-offset-probe/model.json",
            "result": "result.json",
            "write_once": True,
            "generated_artifacts_committed": False,
        },
        "artifact contract",
    )
    if type(value["artifact_audit"]) is not str or not value["artifact_audit"].strip():
        raise E160OffsetProbeError("execution lock requires an artifact audit")
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
    phase157_root: str,
    phase167_root: str,
    artifact_audit: str,
) -> dict[str, object]:
    clean_head = require_clean_arena_head()
    provenance = _current_provenance(clean_head)
    exact(clean_head, arena_revision, "clean Arena HEAD against execution target")
    train, validation, evidence, snapshot = load_latent_records(
        corpus_root,
        phase157_root,
        phase167_root,
    )
    latent = latent_reference(train, validation, snapshot.digest)
    _validate_latent_reference(latent)
    receipt = {
        "schema": SCHEMA + "/execution-lock",
        "role": ROLE,
        "retained": retained_value(),
        "retained_readback": retained_receipt(evidence),
        "provenance": provenance,
        "runtime": _runtime(),
        "representation": representation_value(),
        "centering_contract": centering_value(),
        "probe": probe_value(),
        "solver": solver_value(),
        "evaluation": evaluation_value(),
        "baseline_reference": _baseline_reference(train, validation),
        "latent_reference": latent,
        "artifact_contract": {
            "execution_lock": "execution-lock.json",
            "latent_summary": "latent-summary.json",
            "model": "e160-offset-probe/model.json",
            "result": "result.json",
            "write_once": True,
            "generated_artifacts_committed": False,
        },
        "artifact_audit": artifact_audit,
        "result_exposed": False,
        "execution_decision": "ONE-SHOT DEVELOPMENT EVALUATION AFTER MERGE",
    }
    return validate_lock(receipt)


def require_current_lock(
    lock: dict,
    *,
    corpus_root: str,
    phase157_root: str,
    phase167_root: str,
) -> str:
    validate_lock(lock)
    clean_head = require_clean_arena_head()
    provenance = _current_provenance(clean_head)
    exact(
        provenance,
        lock["provenance"],
        "current execution provenance",
    )
    exact(_runtime(), lock["runtime"], "current runtime")
    evidence = load_retained(corpus_root, phase157_root, phase167_root)
    exact(
        retained_receipt(evidence),
        lock["retained_readback"],
        "current retained evidence",
    )
    return identity(lock)


__all__ = [
    "LOCK_FIELDS",
    "RUNTIME_FIELDS",
    "current_receipt",
    "require_current_lock",
    "validate_lock",
    "validate_runtime",
]
