"""Pre-execution protocol lock for the bounded Arena Issue #279 pilot."""

from __future__ import annotations

import math
from pathlib import Path

from lisjong_arena._artifact_io import (
    canonical_json_text,
    read_json_document,
    sha256_bytes,
    write_new_artifact_file,
)
from lisjong_arena._execution_safety import (
    require_clean_arena_head,
    require_merged_arena_revision,
)
from lisjong_arena.single_round_artifact import (
    collect_execution_provenance,
    execution_provenance_to_dict,
)

from .experiment import (
    CANDIDATE_POLICY_IDENTITY,
    CHEAP_HORIZON,
    GAME_MODE,
    MAX_STEPS,
    REFERENCE_HORIZON,
    REFERENCE_POLICY_IDENTITY,
    SOURCE_POLICY_IDENTITY,
)
from .synthetic import synthetic_validation_passes

SCHEMA_VERSION = 1
PROTOCOL_ID = "arena-issue-279-finite-horizon-ppi-pilot-v1"
CALIBRATION_SEEDS = (27_900_000, 27_900_001, 27_900_002)
_U_SEED_START = 27_910_000
_L_SEED_START = 28_910_000
DEFAULT_CHEAP_MULTIPLIER = 4
MINIMUM_LABELED_HANCHANS = 8
CONFIDENCE_LEVEL = 0.95


class PpiPilotProtocolError(ValueError):
    """The Issue #279 protocol artifact is missing or inconsistent."""


def _fingerprint(document: dict[str, object]) -> str:
    payload = canonical_json_text(document).encode("utf-8")
    return sha256_bytes(payload)


def _timing_summary(rows: object) -> dict[str, object]:
    try:
        items = tuple(rows)
    except TypeError:
        raise TypeError("timing rows must be iterable") from None
    if len(items) != len(CALIBRATION_SEEDS):
        raise ValueError("timing calibration must use exactly the locked seed count")
    if any(type(row) is not dict for row in items):
        raise TypeError("timing rows must contain objects")
    seeds = tuple(row.get("seed") for row in items)
    if seeds != CALIBRATION_SEEDS:
        raise ValueError("timing calibration seeds do not match the locked schedule")

    fields = (
        "source_generation_seconds",
        "candidate_selection_seconds",
        "cheap_evaluation_seconds",
        "reference_evaluation_seconds",
        "unlabeled_total_seconds",
        "labeled_total_seconds",
    )
    means: dict[str, float] = {}
    for field in fields:
        values = tuple(row.get(field) for row in items)
        if any(type(value) not in (int, float) for value in values):
            raise TypeError(f"timing field {field} must be numeric")
        numeric = tuple(float(value) for value in values)
        if any(not math.isfinite(value) or value < 0.0 for value in numeric):
            raise ValueError(f"timing field {field} must be finite and non-negative")
        means[f"mean_{field}"] = sum(numeric) / len(numeric)

    return {
        "seeds": list(CALIBRATION_SEEDS),
        "rows": [dict(row) for row in items],
        **means,
        "outcome_summaries_exposed": False,
    }


def _budget_grid(n_max: int) -> list[int]:
    if n_max < MINIMUM_LABELED_HANCHANS:
        raise ValueError(
            "compute budget cannot afford the minimum labeled pilot size "
            f"({MINIMUM_LABELED_HANCHANS})"
        )
    candidates = {
        max(MINIMUM_LABELED_HANCHANS, n_max // 8),
        max(MINIMUM_LABELED_HANCHANS, n_max // 4),
        max(MINIMUM_LABELED_HANCHANS, n_max // 2),
        n_max,
    }
    return sorted(value for value in candidates if value <= n_max)


def create_protocol_document(
    *,
    timing_rows: object,
    synthetic_validation: object,
    total_compute_budget_seconds: float,
    provenance: dict[str, object],
    arena_revision: str,
    cheap_multiplier: int = DEFAULT_CHEAP_MULTIPLIER,
) -> dict[str, object]:
    """Build a locked protocol without consuming real U/L outcomes."""
    if not synthetic_validation_passes(synthetic_validation):
        raise PpiPilotProtocolError("synthetic validation did not pass")
    if type(total_compute_budget_seconds) not in (int, float):
        raise TypeError("total_compute_budget_seconds must be numeric")
    total_budget = float(total_compute_budget_seconds)
    if not math.isfinite(total_budget) or total_budget <= 0.0:
        raise ValueError("total_compute_budget_seconds must be finite and positive")
    if type(cheap_multiplier) is not int or cheap_multiplier < 1:
        raise ValueError("cheap_multiplier must be a positive integer")
    if type(provenance) is not dict:
        raise TypeError("provenance must be an object")
    if type(arena_revision) is not str or len(arena_revision) != 40:
        raise ValueError("arena_revision must be a full commit id")

    timing = _timing_summary(timing_rows)
    unlabeled_cost = float(timing["mean_unlabeled_total_seconds"])
    labeled_cost = float(timing["mean_labeled_total_seconds"])
    bundle_cost = labeled_cost + cheap_multiplier * unlabeled_cost
    if bundle_cost <= 0.0:
        raise PpiPilotProtocolError("timing calibration produced zero total cost")
    n_max = int(total_budget // bundle_cost)
    grid = _budget_grid(n_max)
    cheap_sample_size = cheap_multiplier * n_max

    u_seeds = tuple(range(_U_SEED_START, _U_SEED_START + cheap_sample_size))
    l_seeds = tuple(range(_L_SEED_START, _L_SEED_START + n_max))
    if set(u_seeds) & set(l_seeds):
        raise PpiPilotProtocolError("U and L seed schedules overlap")
    if set(CALIBRATION_SEEDS) & (set(u_seeds) | set(l_seeds)):
        raise PpiPilotProtocolError("calibration seeds overlap the pilot samples")

    without_identity: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "protocol_id": PROTOCOL_ID,
        "result_exposed": False,
        "state_generation_identity": {
            "policy": SOURCE_POLICY_IDENTITY,
            "seat_assignment": "TwoStepUkeirePolicy x4",
            "game_mode": GAME_MODE,
            "max_steps": MAX_STEPS,
        },
        "candidate_identity": CANDIDATE_POLICY_IDENTITY,
        "reference_policy_identity": REFERENCE_POLICY_IDENTITY,
        "cheap_evaluator_identity": {
            "kind": "exact conditional structural completion probability difference",
            "horizon_self_draw_slots": CHEAP_HORIZON,
            "candidate_minus_reference": True,
        },
        "reference_measurement_identity": {
            "kind": "high-fidelity reference measurement",
            "measurement": "exact conditional structural completion probability difference",
            "horizon_self_draw_slots": REFERENCE_HORIZON,
            "candidate_minus_reference": True,
            "ground_truth_claim": False,
        },
        "environment_identity": {
            "execution_environment": provenance.get("execution_environment"),
            "python_version": provenance.get("python_version"),
            "riichienv_version": provenance.get("riichienv_version"),
            "arena_revision": arena_revision,
        },
        "dependency_versions": dict(provenance),
        "U_seed_specification": {
            "start": u_seeds[0],
            "end": u_seeds[-1],
            "count": len(u_seeds),
        },
        "L_seed_specification": {
            "start": l_seeds[0],
            "end": l_seeds[-1],
            "count": len(l_seeds),
        },
        "decision_opportunity_definition": (
            "source decision with at least two legal DiscardAction candidates and "
            "a DiscardAction selected by the locked source two-step policy; the fixed "
            "candidate is evaluated on the same DecisionContext and must also return "
            "a DiscardAction or execution fails closed"
        ),
        "hanchan_aggregation_semantics": (
            "one observation per source hanchan; mean over predefined choice-discard "
            "opportunities; same candidate/reference action contributes 0; a hanchan "
            "with no eligible opportunity contributes 0"
        ),
        "estimand_definition": (
            "equal-hanchan-weighted mean H3 local candidate-minus-reference "
            "structural-completion-probability advantage over the locked two-step-x4 "
            "source-state distribution"
        ),
        "reference_measurement_budget": {
            "labeled_hanchans_max": n_max,
            "single_worker_equivalent_total_budget_seconds": total_budget,
        },
        "randomness_semantics": {
            "source": "RiichiEnv deterministic seed schedule",
            "cheap_evaluator": "deterministic exact evaluator",
            "reference_evaluator": "deterministic exact evaluator",
            "common_random_numbers": "not applicable: no stochastic rollout",
        },
        "labeled_budget_grid": grid,
        "cheap_sample_size": cheap_sample_size,
        "PPI_method": "mean PPI with lambda=1 on independent U/L hanchans",
        "PPI_plus_method": (
            "mean PPI++ power tuning; plug-in variance-minimizing lambda clipped to [0,1]"
        ),
        "CI_method": "two-sided normal-approximation interval from independent U/L variance",
        "confidence_level": CONFIDENCE_LEVEL,
        "failure_handling": (
            "fail closed; no result-dependent replacement sampling; any failed hanchan "
            "aborts the immutable pilot run"
        ),
        "timing_only_calibration": timing,
        "synthetic_validation": synthetic_validation,
        "compute_plan": {
            "cheap_multiplier": cheap_multiplier,
            "predicted_unlabeled_hanchan_seconds": unlabeled_cost,
            "predicted_labeled_hanchan_seconds": labeled_cost,
            "predicted_total_seconds": cheap_sample_size * unlabeled_cost
            + n_max * labeled_cost,
        },
        "interpretation_guardrails": [
            "diagnostic local counterfactual estimand, not candidate-vs-reference match strength",
            "high-fidelity reference measurement is not ground truth",
            "statistical efficiency does not establish evaluator correctness beyond the locked target",
            "results are not formal promotion or holdout evidence",
        ],
    }
    return {**without_identity, "protocol_identity": _fingerprint(without_identity)}


def validate_protocol_document(value: object) -> dict[str, object]:
    if type(value) is not dict:
        raise PpiPilotProtocolError("protocol must be a JSON object")
    document = dict(value)
    identity = document.pop("protocol_identity", None)
    if type(identity) is not str or identity != _fingerprint(document):
        raise PpiPilotProtocolError("protocol fingerprint mismatch")
    if document.get("schema_version") != SCHEMA_VERSION:
        raise PpiPilotProtocolError("unsupported protocol schema version")
    if document.get("protocol_id") != PROTOCOL_ID:
        raise PpiPilotProtocolError("unexpected protocol id")
    if document.get("result_exposed") is not False:
        raise PpiPilotProtocolError("pre-execution protocol must not expose results")
    if document.get("candidate_identity") != CANDIDATE_POLICY_IDENTITY:
        raise PpiPilotProtocolError("candidate identity is not the fixed pilot task")
    if document.get("reference_policy_identity") != REFERENCE_POLICY_IDENTITY:
        raise PpiPilotProtocolError("reference identity is not the fixed pilot task")
    if not synthetic_validation_passes(document.get("synthetic_validation")):
        raise PpiPilotProtocolError("locked synthetic validation is invalid")
    return {**document, "protocol_identity": identity}


def save_protocol_document(document: object, path: str | Path) -> None:
    validated = validate_protocol_document(document)
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    write_new_artifact_file(destination, canonical_json_text(validated))


def load_protocol_document(path: str | Path) -> dict[str, object]:
    try:
        return validate_protocol_document(read_json_document(Path(path)))
    except PpiPilotProtocolError:
        raise
    except (OSError, TypeError, ValueError) as exc:
        raise PpiPilotProtocolError("protocol cannot be loaded") from exc


def current_execution_identity() -> tuple[str, dict[str, object]]:
    revision = require_clean_arena_head()
    require_merged_arena_revision(revision, branch="main")
    provenance = execution_provenance_to_dict(collect_execution_provenance())
    return revision, provenance


def require_current_protocol(document: object) -> dict[str, object]:
    validated = validate_protocol_document(document)
    revision, provenance = current_execution_identity()
    environment = validated["environment_identity"]
    if type(environment) is not dict or environment.get("arena_revision") != revision:
        raise PpiPilotProtocolError("current Arena revision does not match the lock")
    if validated.get("dependency_versions") != provenance:
        raise PpiPilotProtocolError(
            "current dependency provenance does not match the lock"
        )
    return validated
