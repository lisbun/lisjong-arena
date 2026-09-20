"""Immutable real-result artifact for the bounded Arena Issue #279 PPI pilot."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from lisjong_arena._artifact_io import (
    canonical_json_text,
    read_json_document,
    sha256_bytes,
    write_new_artifact_file,
)

from .experiment import HanchanMeasurement
from .protocol import validate_protocol_document
from .statistics import mean_inference

RESULT_SCHEMA_VERSION = 1
RESULT_ID = "arena-issue-279-finite-horizon-ppi-result-v1"


class PpiPilotArtifactError(ValueError):
    """The Issue #279 result artifact is malformed or inconsistent."""


def _fingerprint(document: dict[str, object]) -> str:
    return sha256_bytes(canonical_json_text(document).encode("utf-8"))


def _seed_range(spec: object, name: str) -> tuple[int, ...]:
    if type(spec) is not dict:
        raise PpiPilotArtifactError(f"{name} seed specification is invalid")
    start = spec.get("start")
    end = spec.get("end")
    count = spec.get("count")
    if type(start) is not int or type(end) is not int or type(count) is not int:
        raise PpiPilotArtifactError(f"{name} seed specification must contain integers")
    seeds = tuple(range(start, end + 1))
    if len(seeds) != count:
        raise PpiPilotArtifactError(f"{name} seed count is inconsistent")
    return seeds


def _check_measurements(
    values: object, expected_seeds: tuple[int, ...], *, labeled: bool
) -> tuple[HanchanMeasurement, ...]:
    try:
        items = tuple(values)
    except TypeError:
        raise TypeError("measurements must be iterable") from None
    if any(not isinstance(item, HanchanMeasurement) for item in items):
        raise TypeError("measurements must contain HanchanMeasurement values")
    if tuple(item.seed for item in items) != expected_seeds:
        raise PpiPilotArtifactError("measurement seeds do not match the locked schedule")
    if labeled and any(item.reference_score is None for item in items):
        raise PpiPilotArtifactError("labeled measurements must contain reference scores")
    if not labeled and any(item.reference_score is not None for item in items):
        raise PpiPilotArtifactError("U measurements must not contain reference scores")
    return items


def _runtime_summary(
    unlabeled: tuple[HanchanMeasurement, ...],
    labeled: tuple[HanchanMeasurement, ...],
) -> dict[str, object]:
    all_items = unlabeled + labeled
    return {
        "source_generation_seconds": sum(item.source_generation_seconds for item in all_items),
        "candidate_selection_seconds": sum(item.candidate_selection_seconds for item in all_items),
        "cheap_evaluation_seconds": sum(item.cheap_evaluation_seconds for item in all_items),
        "reference_evaluation_seconds": sum(item.reference_evaluation_seconds for item in labeled),
        "unlabeled_hanchan_count": len(unlabeled),
        "labeled_hanchan_count": len(labeled),
        "total_measured_worker_seconds": sum(item.total_seconds for item in all_items),
    }


def build_result_artifact(
    *,
    protocol: object,
    unlabeled_measurements: object,
    labeled_measurements: object,
) -> dict[str, object]:
    locked = validate_protocol_document(protocol)
    u_seeds = _seed_range(locked["U_seed_specification"], "U")
    l_seeds = _seed_range(locked["L_seed_specification"], "L")
    unlabeled = _check_measurements(unlabeled_measurements, u_seeds, labeled=False)
    labeled = _check_measurements(labeled_measurements, l_seeds, labeled=True)

    u_predictions = tuple(item.cheap_score for item in unlabeled)
    x_all = tuple(item.cheap_score for item in labeled)
    y_all = tuple(float(item.reference_score) for item in labeled)
    grid = locked.get("labeled_budget_grid")
    if type(grid) is not list or not grid:
        raise PpiPilotArtifactError("labeled budget grid is missing")

    budget_curve = []
    for budget in grid:
        if type(budget) is not int or budget < 2 or budget > len(labeled):
            raise PpiPilotArtifactError("labeled budget grid is invalid")
        budget_curve.append(
            {
                "labeled_budget": budget,
                **mean_inference(
                    u_predictions,
                    x_all[:budget],
                    y_all[:budget],
                    confidence_level=float(locked["confidence_level"]),
                ),
            }
        )

    without_identity: dict[str, object] = {
        "schema_version": RESULT_SCHEMA_VERSION,
        "result_id": RESULT_ID,
        "result_exposure_state": "EXPOSED",
        "protocol": locked,
        "sample_identities": {
            "U_seeds": list(u_seeds),
            "L_seeds": list(l_seeds),
            "nested_labeled_prefix_budgets": list(grid),
        },
        "measurements": {
            "U": [asdict(item) for item in unlabeled],
            "L": [asdict(item) for item in labeled],
        },
        "budget_curve": budget_curve,
        "primary_max_budget_analysis": budget_curve[-1],
        "runtime_compute_metrics": _runtime_summary(unlabeled, labeled),
        "synthetic_validation_summary": locked["synthetic_validation"],
        "failures_exclusions": [],
        "interpretation_guardrails": locked["interpretation_guardrails"],
        "engineering_follow_up_note": (
            "Whether reusable PPI infrastructure is worth building is a separate "
            "engineering judgment based on this recorded feasibility evidence."
        ),
    }
    return {**without_identity, "result_identity": _fingerprint(without_identity)}


def validate_result_artifact(value: object) -> dict[str, object]:
    if type(value) is not dict:
        raise PpiPilotArtifactError("result artifact must be an object")
    document = dict(value)
    identity = document.pop("result_identity", None)
    if type(identity) is not str or identity != _fingerprint(document):
        raise PpiPilotArtifactError("result artifact fingerprint mismatch")
    if document.get("schema_version") != RESULT_SCHEMA_VERSION:
        raise PpiPilotArtifactError("unsupported result schema version")
    if document.get("result_id") != RESULT_ID:
        raise PpiPilotArtifactError("unexpected result id")
    validate_protocol_document(document.get("protocol"))
    if document.get("result_exposure_state") != "EXPOSED":
        raise PpiPilotArtifactError("result exposure state is invalid")
    return {**document, "result_identity": identity}


def save_result_artifact(document: object, path: str | Path) -> None:
    validated = validate_result_artifact(document)
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    write_new_artifact_file(destination, canonical_json_text(validated))


def load_result_artifact(path: str | Path) -> dict[str, object]:
    try:
        return validate_result_artifact(read_json_document(Path(path)))
    except PpiPilotArtifactError:
        raise
    except (OSError, TypeError, ValueError) as exc:
        raise PpiPilotArtifactError("result artifact cannot be loaded") from exc
