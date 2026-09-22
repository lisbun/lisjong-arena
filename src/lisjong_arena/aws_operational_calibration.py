"""AWS operational calibration evidence and the launch admission gate (#340).

This module owns operational **performance** evidence and the machine-readable
Go/No-Go record that must be satisfied before a billable production run is
created, and again before the first scientific unit is submitted.  It calls no
AWS API, executes no workload, and never touches scientific evidence: a
calibration observation is timing/resource metadata only, and admission is
never permitted to read or emit a score, rank, support count or qualification
result.

Boundaries this module deliberately keeps:

* calibration evidence is operational only.  It never becomes qualification,
  TRAIN, SELECT or OFFLINE-EVAL evidence, and a calibration PASS authorizes no
  scientific run by itself;
* a small-sample p90 is a descriptive order statistic, not a guaranteed upper
  bound.  The percentile method and the sample size travel with every record;
* worker scaling is never extrapolated.  Prediction requires evidence measured
  at exactly the requested worker count whose observed concurrency actually
  reached it;
* an unknown charge that the operator declares material is never treated as
  zero.  It makes the cost prediction unavailable, which is a No-Go.

It reuses the existing #329 observability contract (scientific runtime vs EC2
billable runtime), the #339 durable per-seed receipt primitive and the #346
Arena seed ledger binding shape rather than introducing another cloud
framework.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Mapping, Sequence

from lisjong_arena import seed_registry
from lisjong_arena.durable_seed_checkpoint import verify_seed_checkpoint_set
from lisjong_arena.riichilab_corpus.models import canonical_json_bytes
from lisjong_arena.riichilab_corpus.persistence import atomic_replace

EVIDENCE_SCHEMA_VERSION: Final = "arena-aws-calibration-evidence-v1"
REQUIREMENT_SCHEMA_VERSION: Final = "arena-aws-admission-requirement-v1"
OBSERVATION_SCHEMA_VERSION: Final = "arena-aws-phase2-observation-v1"
ADMISSION_SCHEMA_VERSION: Final = "arena-aws-launch-admission-v1"
INCOMPLETE_OBSERVATION_SCHEMA_VERSION: Final = (
    "arena-aws-incomplete-execution-observation-v1"
)

CALIBRATION_REQUIREMENT_SCHEMA_VERSION: Final = (
    "arena-aws-calibration-admission-requirement-v1"
)

OWNER_REPOSITORY: Final = seed_registry.OWNER_REPOSITORY

#: A calibration allocation is a dedicated, ledger-resolved population. These
#: two values are what makes an allocation a calibration allocation; the owner
#: Issue is recorded but intentionally not constrained, because a calibration
#: may be owned by the study it serves.
CALIBRATION_POPULATION: Final = "operational-calibration"
CALIBRATION_PROTOCOL: Final = "aws-operational-calibration-v1"

#: Ordered from weakest to strongest.  An admission requirement declares the
#: minimum level the production run must provide; the calibration must have
#: been measured with at least that same level, because the durable write path
#: is part of the runtime being predicted.
DURABLE_EVIDENCE_LEVELS: Final = (
    "none",
    "atomic-operational-progress",
    "per-seed-durable-receipt",
)

#: Burstable families cannot be extrapolated from a short burst observation to
#: sustained multi-hour performance without an explicit credit/sustained-mode
#: basis.
BURSTABLE_INSTANCE_FAMILIES: Final = frozenset({"t2", "t3", "t3a", "t4g"})

DEFAULT_HEADROOM_FACTOR: Final = 1.5
PERCENTILE_METHOD: Final = (
    "nearest-rank order statistic over the observed per-unit durations; "
    "descriptive for this sample only and not a guaranteed statistical bound"
)

_SHA256 = re.compile(r"\A[0-9a-f]{64}\Z")
_SHA1 = re.compile(r"\A[0-9a-f]{40}\Z")
_INSTANCE_ID = re.compile(r"\Ai-[0-9a-f]{8,}\Z")
_TOLERANCE: Final = 1e-9

#: Result-bearing keys that must never appear anywhere in an operational
#: calibration or admission document, at any nesting depth.
_FORBIDDEN_SCIENTIFIC_KEYS: Final = frozenset(
    {
        "accuracy",
        "classification",
        "elo",
        "f1",
        "f2",
        "metrics",
        "model_metrics",
        "outcome",
        "p2_outcome",
        "placement",
        "precision",
        "qualification_outcome",
        "rank",
        "ranks",
        "recall",
        "result",
        "results",
        "reward",
        "score",
        "scores",
        "scientific_result",
        "scientific_results",
        "support",
        "win_rate",
    }
)

_BINDING_FIELDS: Final = frozenset(
    {
        "allocation_identity",
        "ledger_revision",
        "owner_repository",
        "seed_domain",
        "seed_membership_identity",
    }
)

_EVIDENCE_FIELDS: Final = frozenset(
    {
        "arena_revision",
        "batch_scientific_wall_clock_seconds",
        "burstable_sustained_basis",
        "calibrated_at",
        "calibration_identity",
        "calibration_run_id",
        "durable_evidence_level",
        "ec2_billable_runtime_seconds",
        "game_mode",
        "instance_family",
        "instance_type",
        "instrumentation_identity",
        "instrumentation_path",
        "limitations",
        "lisjong_engine_revision",
        "lisjong_revision",
        "mean_observed_concurrency",
        "p50_seconds_per_unit",
        "p90_seconds_per_unit",
        "peak_observed_concurrency",
        "percentile_method",
        "riichienv_version",
        "schema_version",
        "seed_allocation",
        "seed_allocation_population",
        "seed_ledger_revision",
        "setup_overhead_seconds",
        "task_count",
        "task_durations_seconds",
        "tasks",
        "teacher_identity",
        "teardown_overhead_seconds",
        "throughput_units_per_hour",
        "unavailable_fields",
        "vcpu",
        "worker_count_requested",
        "workers_active_observed",
        "workload_identity",
    }
)

_TARGET_FIELDS: Final = frozenset(
    {
        "arena_revision",
        "durable_evidence_level",
        "game_mode",
        "instance_type",
        "instrumentation_identity",
        "lisjong_engine_revision",
        "lisjong_revision",
        "riichienv_version",
        "teacher_identity",
        "total_units",
        "vcpu",
        "worker_count",
        "workload_identity",
    }
)

_POLICY_FIELDS: Final = frozenset(
    {"freshness_max_age_seconds", "minimum_task_count", "minimum_tasks_per_worker"}
)

_BUDGET_FIELDS: Final = frozenset(
    {
        "cost_budget_usd",
        "execution_budget_seconds",
        "hard_fail_safe_seconds",
        "headroom_factor",
        "normal_deadline_seconds",
        "post_processing_seconds",
        "setup_seconds",
        "teardown_seconds",
    }
)

_CHARGE_FIELDS: Final = frozenset({"label", "material", "max_usd", "reason", "usd"})

_REQUIREMENT_FIELDS: Final = frozenset(
    {
        "budget",
        "calibration_policy",
        "charges",
        "consumer",
        "gates",
        "pricing",
        "production_allocation",
        "run_id",
        "schema_version",
        "target",
    }
)

_REQUIREMENT_GATE_NAMES: Final = (
    "protocol_lock",
    "seed_registry",
    "durable_evidence",
    "artifact_destination",
    "reattach",
)

_CALIBRATION_REQUIREMENT_GATE_NAMES: Final = (
    "durable_evidence",
    "artifact_destination",
    "teardown_confirmation",
)

_CALIBRATION_BOUNDS_FIELDS: Final = frozenset({"max_unit_count", "max_worker_count"})

_CALIBRATION_REQUIREMENT_FIELDS: Final = frozenset(
    {
        "allocation_binding",
        "bounds",
        "budget",
        "calibration_cost_budget_usd",
        "charges",
        "consumer",
        "gates",
        "pricing",
        "run_id",
        "schema_version",
        "seeds",
        "target",
    }
)

_OBSERVATION_FIELDS: Final = frozenset(
    {
        "arena_revision",
        "availability_zone",
        "fail_safe_arm_epoch",
        "fail_safe_armed",
        "fail_safe_deadline_epoch",
        "instance_id",
        "instance_launch_epoch",
        "instance_type",
        "observed_at_epoch",
        "phase_one_admission_identity",
        "recovery_identity",
        "retained_destination",
        "run_id",
        "schema_version",
        "vcpu",
        "worker_count",
    }
)

PHASE_ONE_GATE_NAMES: Final = (
    "protocol-lock",
    "seed-registry-allocation",
    "matching-calibration",
    "runtime-prediction",
    "cost-prediction",
    "cost-budget",
    "runtime-headroom",
    "normal-deadline",
    "hard-fail-safe",
    "durable-evidence-support",
    "artifact-destination-retention",
    "reattach-capability",
)

PHASE_TWO_GATE_NAMES: Final = (
    "actual-environment-identity",
    "hard-fail-safe-armed",
    "retained-destination-writable",
    "recovery-identity-persisted",
    "remaining-budget-sufficient",
)

#: The bounded calibration run has its own admission. It deliberately contains
#: no matching-calibration gate: requiring calibration evidence in order to
#: calibrate would be circular. Its cost bound is therefore the worst case --
#: the full independent hard fail-safe window -- not a prediction.
PHASE_ZERO_GATE_NAMES: Final = (
    "calibration-allocation-authority",
    "calibration-scope-bounded",
    "calibration-cost-budget",
    "hard-fail-safe",
    "durable-evidence-support",
    "artifact-destination-retention",
    "teardown-confirmation",
)

GATE_NAMES_BY_PHASE: Final = {
    0: PHASE_ZERO_GATE_NAMES,
    1: PHASE_ONE_GATE_NAMES,
    2: PHASE_TWO_GATE_NAMES,
}

CLOCK_ORIGINS: Final = {
    "execution_budget_seconds": "scientific workload start",
    "hard_fail_safe_seconds": "instance-side fail-safe arm epoch",
    "normal_deadline_seconds": "instance-side fail-safe arm epoch",
    "predicted_scientific_runtime_seconds": "scientific workload start",
}


class AwsOperationalCalibrationError(ValueError):
    """Operational calibration or launch admission contract violation."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise AwsOperationalCalibrationError(message)


def _text(value: object, name: str) -> str:
    _require(
        type(value) is str and bool(value.strip()), f"{name} must be a non-empty string"
    )
    return str(value)


def _optional_text(value: object, name: str) -> str | None:
    if value is None:
        return None
    return _text(value, name)


def _sha256_text(value: object, name: str) -> str:
    text = _text(value, name)
    _require(bool(_SHA256.fullmatch(text)), f"{name} must be lowercase hex SHA-256")
    return text


def _sha1_text(value: object, name: str) -> str:
    text = _text(value, name)
    _require(bool(_SHA1.fullmatch(text)), f"{name} must be a full lowercase commit SHA")
    return text


def _positive_int(value: object, name: str) -> int:
    _require(type(value) is int and value > 0, f"{name} must be a positive int")
    return int(value)


def _number(value: object, name: str) -> float:
    _require(
        type(value) in (int, float) and math.isfinite(float(value)),
        f"{name} must be a finite number",
    )
    return float(value)


def _positive_number(value: object, name: str) -> float:
    number = _number(value, name)
    _require(number > 0, f"{name} must be positive")
    return number


def _non_negative_number(value: object, name: str) -> float:
    number = _number(value, name)
    _require(number >= 0, f"{name} must be non-negative")
    return number


def _optional_positive_number(value: object, name: str) -> float | None:
    if value is None:
        return None
    return _positive_number(value, name)


def _string_list(value: object, name: str) -> list[str]:
    _require(type(value) is list, f"{name} must be a list")
    return [_text(item, f"{name} entry") for item in value]


def _aware(value: datetime, name: str) -> datetime:
    _require(value.tzinfo is not None, f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    return (
        _aware(value, "timestamp").isoformat(timespec="seconds").replace("+00:00", "Z")
    )


def _parse_timestamp(value: object, name: str) -> datetime:
    text = _text(value, name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise AwsOperationalCalibrationError(f"{name} must be RFC3339") from error
    return _aware(parsed, name)


def _digest(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _fits(value: float, limit: float) -> bool:
    return value <= limit + _TOLERANCE


def _round(value: float) -> float:
    return round(value, 6)


def assert_no_scientific_fields(document: object, *, context: str) -> None:
    """Fail closed if any result-bearing key appears at any nesting depth."""

    if type(document) is dict:
        forbidden = _FORBIDDEN_SCIENTIFIC_KEYS & set(document)
        _require(
            not forbidden,
            f"scientific fields are forbidden in {context}: {sorted(forbidden)!r}",
        )
        for value in document.values():
            assert_no_scientific_fields(value, context=context)
    elif type(document) is list:
        for value in document:
            assert_no_scientific_fields(value, context=context)


def instance_family(instance_type: str) -> str:
    return _text(instance_type, "instance_type").split(".", 1)[0].lower()


def nearest_rank_percentile(values: Sequence[float], percentile: float) -> float:
    """Return the nearest-rank order statistic for ``percentile`` in 0..100."""

    _require(bool(values), "percentile requires at least one observation")
    _require(0 < percentile <= 100, "percentile must be within (0, 100]")
    ordered = sorted(float(value) for value in values)
    index = math.ceil(percentile / 100.0 * len(ordered)) - 1
    return ordered[min(max(index, 0), len(ordered) - 1)]


def _peak_concurrency(intervals: Sequence[tuple[float, float]]) -> int:
    """Return the maximum number of simultaneously open task intervals.

    A task that ends exactly when another starts is not counted as concurrent,
    so the observed value is conservative rather than optimistic.
    """

    events: list[tuple[float, int]] = []
    for start, end in intervals:
        events.append((start, 1))
        events.append((end, -1))
    # -1 sorts before +1 at an identical instant, so touching intervals do not
    # inflate the observed concurrency.
    events.sort(key=lambda event: (event[0], event[1]))
    peak = 0
    current = 0
    for _instant, delta in events:
        current += delta
        peak = max(peak, current)
    return peak


def _validate_binding_shape(value: object, name: str) -> dict[str, object]:
    _require(type(value) is dict, f"{name} must be an object")
    _require(
        set(value) == _BINDING_FIELDS, f"{name} fields do not match the v1 binding"
    )
    _require(
        value["owner_repository"] == OWNER_REPOSITORY, f"{name} is not Arena-owned"
    )
    _sha256_text(value["allocation_identity"], f"{name}.allocation_identity")
    _sha256_text(value["ledger_revision"], f"{name}.ledger_revision")
    _sha256_text(value["seed_membership_identity"], f"{name}.seed_membership_identity")
    _text(value["seed_domain"], f"{name}.seed_domain")
    return dict(value)


def _normalized_tasks(tasks: object) -> list[dict[str, object]]:
    _require(type(tasks) is list and bool(tasks), "tasks must be a non-empty list")
    seen: set[int] = set()
    normalized: list[dict[str, object]] = []
    for entry in tasks:
        _require(type(entry) is dict, "each task must be an object")
        _require(
            set(entry) <= {"seed", "started_at", "completed_at", "duration_seconds"},
            "task fields are invalid",
        )
        seed = entry.get("seed")
        _require(type(seed) is int and 0 <= seed < 2**32, "task seed is invalid")
        _require(seed not in seen, f"duplicate task seed {seed}")
        seen.add(int(seed))
        started = _parse_timestamp(entry.get("started_at"), "task started_at").replace(
            microsecond=0
        )
        completed = _parse_timestamp(
            entry.get("completed_at"), "task completed_at"
        ).replace(microsecond=0)
        duration = (completed - started).total_seconds()
        _require(duration > 0, f"task seed {seed} has a non-positive duration")
        declared = entry.get("duration_seconds")
        if declared is not None:
            _require(
                abs(_number(declared, "duration_seconds") - duration) <= 1.0,
                f"task seed {seed} duration_seconds is inconsistent",
            )
        normalized.append(
            {
                "seed": int(seed),
                "started_at": _timestamp(started),
                "completed_at": _timestamp(completed),
                "duration_seconds": _round(duration),
            }
        )
    normalized.sort(key=lambda task: task["seed"])
    return normalized


def tasks_from_durable_receipts(
    checkpoint_dir: str | Path,
    *,
    run_id: str,
    protocol_identity: str,
    expected_seeds: Sequence[int],
) -> list[dict[str, object]]:
    """Derive per-task intervals from #339 durable per-seed receipts.

    The receipt set is verified first, so an incomplete calibration batch
    fails closed here instead of producing a shorter, faster-looking sample.
    """

    receipts = verify_seed_checkpoint_set(
        checkpoint_dir,
        run_id=run_id,
        protocol_identity=protocol_identity,
        expected_seeds=expected_seeds,
    )
    return _normalized_tasks(
        [
            {
                "seed": seed,
                "started_at": receipt["started_at"],
                "completed_at": receipt["completed_at"],
            }
            for seed, receipt in receipts.items()
        ]
    )


def build_calibration_evidence(
    *,
    calibration_run_id: str,
    calibrated_at: datetime,
    seed_allocation: Mapping[str, object],
    seed_allocation_population: str,
    seed_ledger_revision: str,
    arena_revision: str,
    lisjong_revision: str,
    lisjong_engine_revision: str,
    riichienv_version: str,
    workload_identity: str,
    teacher_identity: str,
    game_mode: str,
    instance_type: str,
    vcpu: int,
    worker_count_requested: int,
    workers_active_observed: int | None,
    tasks: Sequence[Mapping[str, object]],
    batch_scientific_wall_clock_seconds: float,
    ec2_billable_runtime_seconds: float | None,
    setup_overhead_seconds: float | None,
    teardown_overhead_seconds: float | None,
    durable_evidence_level: str,
    instrumentation_identity: str,
    instrumentation_path: str,
    burstable_sustained_basis: str | None = None,
    limitations: Sequence[str] = (),
) -> dict[str, object]:
    """Build one dedicated operational calibration evidence document."""

    normalized = _normalized_tasks(list(tasks))
    durations = [float(task["duration_seconds"]) for task in normalized]
    wall_clock = _positive_number(
        batch_scientific_wall_clock_seconds, "batch_scientific_wall_clock_seconds"
    )
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    intervals = [
        (
            (
                _parse_timestamp(task["started_at"], "started_at") - epoch
            ).total_seconds(),
            (
                _parse_timestamp(task["completed_at"], "completed_at") - epoch
            ).total_seconds(),
        )
        for task in normalized
    ]
    span = max(end for _start, end in intervals) - min(
        start for start, _end in intervals
    )
    _require(
        wall_clock + 1.0 >= span,
        "batch wall clock is shorter than the observed task span",
    )
    billable = _optional_positive_number(
        ec2_billable_runtime_seconds, "ec2_billable_runtime_seconds"
    )
    if billable is not None:
        _require(
            billable >= wall_clock,
            "EC2 billable runtime cannot be shorter than the scientific wall clock",
        )
    setup = (
        None
        if setup_overhead_seconds is None
        else _non_negative_number(setup_overhead_seconds, "setup_overhead_seconds")
    )
    teardown = (
        None
        if teardown_overhead_seconds is None
        else _non_negative_number(
            teardown_overhead_seconds, "teardown_overhead_seconds"
        )
    )
    if billable is not None and setup is not None and teardown is not None:
        _require(
            billable + 1.0 >= setup + wall_clock + teardown,
            "EC2 billable runtime is shorter than its own measured decomposition",
        )
    workers_requested = _positive_int(worker_count_requested, "worker_count_requested")
    workers_active = (
        None
        if workers_active_observed is None
        else _positive_int(workers_active_observed, "workers_active_observed")
    )
    _require(
        durable_evidence_level in DURABLE_EVIDENCE_LEVELS,
        "durable_evidence_level is not a recognized level",
    )
    family = instance_family(instance_type)
    basis = _optional_text(burstable_sustained_basis, "burstable_sustained_basis")

    unavailable: list[str] = []
    if billable is None:
        unavailable.append("ec2_billable_runtime_seconds")
    if setup is None:
        unavailable.append("setup_overhead_seconds")
    if teardown is None:
        unavailable.append("teardown_overhead_seconds")
    if workers_active is None:
        unavailable.append("workers_active_observed")

    document: dict[str, object] = {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "calibration_identity": "",
        "calibration_run_id": _text(calibration_run_id, "calibration_run_id"),
        "calibrated_at": _timestamp(_aware(calibrated_at, "calibrated_at")),
        "seed_allocation": _validate_binding_shape(seed_allocation, "seed_allocation"),
        "seed_allocation_population": _text(
            seed_allocation_population, "seed_allocation_population"
        ),
        "seed_ledger_revision": _sha256_text(
            seed_ledger_revision, "seed_ledger_revision"
        ),
        "arena_revision": _sha1_text(arena_revision, "arena_revision"),
        "lisjong_revision": _sha1_text(lisjong_revision, "lisjong_revision"),
        "lisjong_engine_revision": _sha1_text(
            lisjong_engine_revision, "lisjong_engine_revision"
        ),
        "riichienv_version": _text(riichienv_version, "riichienv_version"),
        "workload_identity": _text(workload_identity, "workload_identity"),
        "teacher_identity": _text(teacher_identity, "teacher_identity"),
        "game_mode": _text(game_mode, "game_mode"),
        "instance_type": _text(instance_type, "instance_type"),
        "instance_family": family,
        "vcpu": _positive_int(vcpu, "vcpu"),
        "worker_count_requested": workers_requested,
        "workers_active_observed": workers_active,
        "task_count": len(normalized),
        "tasks": normalized,
        "task_durations_seconds": [_round(value) for value in durations],
        "peak_observed_concurrency": _peak_concurrency(intervals),
        "mean_observed_concurrency": _round(sum(durations) / wall_clock),
        "batch_scientific_wall_clock_seconds": _round(wall_clock),
        "ec2_billable_runtime_seconds": None if billable is None else _round(billable),
        "setup_overhead_seconds": None if setup is None else _round(setup),
        "teardown_overhead_seconds": None if teardown is None else _round(teardown),
        "p50_seconds_per_unit": _round(nearest_rank_percentile(durations, 50)),
        "p90_seconds_per_unit": _round(nearest_rank_percentile(durations, 90)),
        "throughput_units_per_hour": _round(len(normalized) * 3600.0 / wall_clock),
        "percentile_method": PERCENTILE_METHOD,
        "durable_evidence_level": durable_evidence_level,
        "instrumentation_identity": _text(
            instrumentation_identity, "instrumentation_identity"
        ),
        "instrumentation_path": _text(instrumentation_path, "instrumentation_path"),
        "burstable_sustained_basis": basis,
        "unavailable_fields": sorted(unavailable),
        "limitations": _string_list(list(limitations), "limitations"),
    }
    document["calibration_identity"] = _digest(
        {key: value for key, value in document.items() if key != "calibration_identity"}
    )
    assert_no_scientific_fields(document, context="calibration evidence")
    return document


def validate_calibration_evidence(document: object) -> dict[str, object]:
    """Fail closed unless the evidence is internally consistent and untampered."""

    _require(type(document) is dict, "calibration evidence must be an object")
    evidence = dict(document)
    _require(
        set(evidence) == _EVIDENCE_FIELDS, "calibration evidence fields are invalid"
    )
    _require(
        evidence["schema_version"] == EVIDENCE_SCHEMA_VERSION,
        "unsupported calibration evidence schema",
    )
    assert_no_scientific_fields(evidence, context="calibration evidence")
    identity = _sha256_text(evidence["calibration_identity"], "calibration_identity")
    recomputed = build_calibration_evidence(
        calibration_run_id=str(evidence["calibration_run_id"]),
        calibrated_at=_parse_timestamp(evidence["calibrated_at"], "calibrated_at"),
        seed_allocation=_validate_binding_shape(
            evidence["seed_allocation"], "seed_allocation"
        ),
        seed_allocation_population=str(evidence["seed_allocation_population"]),
        seed_ledger_revision=str(evidence["seed_ledger_revision"]),
        arena_revision=str(evidence["arena_revision"]),
        lisjong_revision=str(evidence["lisjong_revision"]),
        lisjong_engine_revision=str(evidence["lisjong_engine_revision"]),
        riichienv_version=str(evidence["riichienv_version"]),
        workload_identity=str(evidence["workload_identity"]),
        teacher_identity=str(evidence["teacher_identity"]),
        game_mode=str(evidence["game_mode"]),
        instance_type=str(evidence["instance_type"]),
        vcpu=evidence["vcpu"],
        worker_count_requested=evidence["worker_count_requested"],
        workers_active_observed=evidence["workers_active_observed"],
        tasks=evidence["tasks"],
        batch_scientific_wall_clock_seconds=evidence[
            "batch_scientific_wall_clock_seconds"
        ],
        ec2_billable_runtime_seconds=evidence["ec2_billable_runtime_seconds"],
        setup_overhead_seconds=evidence["setup_overhead_seconds"],
        teardown_overhead_seconds=evidence["teardown_overhead_seconds"],
        durable_evidence_level=evidence["durable_evidence_level"],
        instrumentation_identity=str(evidence["instrumentation_identity"]),
        instrumentation_path=str(evidence["instrumentation_path"]),
        burstable_sustained_basis=evidence["burstable_sustained_basis"],
        limitations=evidence["limitations"],
    )
    _require(
        recomputed == evidence,
        "calibration evidence derived values are inconsistent with its observations",
    )
    _require(identity == recomputed["calibration_identity"], "identity mismatch")
    return evidence


def write_calibration_evidence(path: str | Path, document: object) -> None:
    destination = Path(path)
    _require(not destination.exists(), "calibration destination already exists")
    atomic_replace(
        destination, canonical_json_bytes(validate_calibration_evidence(document))
    )


def read_calibration_evidence(path: str | Path) -> dict[str, object]:
    source = Path(path)
    try:
        document = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise AwsOperationalCalibrationError(
            f"cannot read calibration evidence {source}: {error}"
        ) from error
    return validate_calibration_evidence(document)


def _validate_charge(value: object, index: int) -> dict[str, object]:
    _require(type(value) is dict, f"charge {index} must be an object")
    _require(set(value) == _CHARGE_FIELDS, f"charge {index} fields are invalid")
    label = _text(value["label"], f"charge {index} label")
    material = value["material"]
    _require(type(material) is bool, f"charge {index} material must be a bool")
    usd = (
        None
        if value["usd"] is None
        else _non_negative_number(value["usd"], f"charge {index} usd")
    )
    max_usd = (
        None
        if value["max_usd"] is None
        else _non_negative_number(value["max_usd"], f"charge {index} max_usd")
    )
    reason = _optional_text(value["reason"], f"charge {index} reason")
    _require(
        material or reason is not None,
        f"charge {label!r} declared immaterial must record why",
    )
    return {
        "label": label,
        "material": material,
        "max_usd": max_usd,
        "reason": reason,
        "usd": usd,
    }


def validate_admission_requirement(document: object) -> dict[str, object]:
    _require(type(document) is dict, "admission requirement must be an object")
    requirement = dict(document)
    _require(
        set(requirement) == _REQUIREMENT_FIELDS,
        "admission requirement fields are invalid",
    )
    _require(
        requirement["schema_version"] == REQUIREMENT_SCHEMA_VERSION,
        "unsupported admission requirement schema",
    )
    assert_no_scientific_fields(requirement, context="admission requirement")
    _text(requirement["consumer"], "consumer")
    _text(requirement["run_id"], "run_id")

    target = requirement["target"]
    _require(
        type(target) is dict and set(target) == _TARGET_FIELDS, "target is invalid"
    )
    for name in ("arena_revision", "lisjong_revision", "lisjong_engine_revision"):
        _sha1_text(target[name], f"target.{name}")
    for name in (
        "riichienv_version",
        "workload_identity",
        "teacher_identity",
        "game_mode",
        "instance_type",
        "instrumentation_identity",
    ):
        _text(target[name], f"target.{name}")
    _positive_int(target["vcpu"], "target.vcpu")
    _positive_int(target["worker_count"], "target.worker_count")
    _positive_int(target["total_units"], "target.total_units")
    _require(
        target["durable_evidence_level"] in DURABLE_EVIDENCE_LEVELS
        and target["durable_evidence_level"] != "none",
        "target.durable_evidence_level must be a recognized non-'none' level",
    )

    policy = requirement["calibration_policy"]
    _require(
        type(policy) is dict and set(policy) == _POLICY_FIELDS,
        "calibration_policy is invalid",
    )
    _positive_number(policy["freshness_max_age_seconds"], "freshness_max_age_seconds")
    _positive_number(policy["minimum_tasks_per_worker"], "minimum_tasks_per_worker")
    _positive_int(policy["minimum_task_count"], "minimum_task_count")

    budget = requirement["budget"]
    _require(
        type(budget) is dict and set(budget) == _BUDGET_FIELDS, "budget is invalid"
    )
    _require(
        _number(budget["headroom_factor"], "headroom_factor") >= 1.0,
        "headroom_factor must be at least 1.0",
    )
    for name in (
        "execution_budget_seconds",
        "hard_fail_safe_seconds",
        "normal_deadline_seconds",
    ):
        _positive_number(budget[name], f"budget.{name}")
    for name in ("post_processing_seconds", "setup_seconds", "teardown_seconds"):
        _non_negative_number(budget[name], f"budget.{name}")
    _non_negative_number(budget["cost_budget_usd"], "budget.cost_budget_usd")

    pricing = requirement["pricing"]
    _require(type(pricing) is dict, "pricing must be an object")
    _require(
        set(pricing) == {"checked_at", "instance_hourly_rate_usd", "region", "source"},
        "pricing provenance fields are invalid",
    )
    _text(pricing["source"], "pricing.source")
    _text(pricing["region"], "pricing.region")
    _parse_timestamp(pricing["checked_at"], "pricing.checked_at")
    if pricing["instance_hourly_rate_usd"] is not None:
        _non_negative_number(
            pricing["instance_hourly_rate_usd"], "instance_hourly_rate_usd"
        )

    charges = requirement["charges"]
    _require(type(charges) is list, "charges must be a list")
    requirement["charges"] = [
        _validate_charge(charge, index) for index, charge in enumerate(charges)
    ]

    gates = requirement["gates"]
    _require(
        type(gates) is dict and set(gates) == set(_REQUIREMENT_GATE_NAMES),
        "requirement gates are invalid",
    )
    for name in _REQUIREMENT_GATE_NAMES:
        entry = gates[name]
        _require(type(entry) is dict, f"gate {name} must be an object")
        _require(
            entry.get("status") in ("PASS", "FAIL"),
            f"gate {name} status must be PASS or FAIL",
        )
        _text(entry.get("detail"), f"gate {name} detail")

    allocation = requirement["production_allocation"]
    _require(
        type(allocation) is dict
        and set(allocation)
        == {
            "allocation_identities",
            "seed_domain",
            "seed_membership_identities",
            "seeds",
        },
        "production_allocation is invalid",
    )
    for name in ("allocation_identities", "seed_membership_identities"):
        values = allocation[name]
        _require(type(values) is list and bool(values), f"{name} must be non-empty")
        for value in values:
            _sha256_text(value, name)
    _text(allocation["seed_domain"], "production_allocation.seed_domain")
    seeds = allocation["seeds"]
    _require(
        type(seeds) is list and bool(seeds), "production_allocation.seeds is required"
    )
    for value in seeds:
        _require(
            type(value) is int and 0 <= value < 2**32,
            "production_allocation.seeds must be unsigned 32-bit integers",
        )
    return requirement


def calibration_seeds(evidence: Mapping[str, object]) -> tuple[int, ...]:
    """Return the seeds the calibration actually executed, in ledger order."""

    return tuple(sorted(int(task["seed"]) for task in evidence["tasks"]))  # type: ignore[index]


def resolve_calibration_allocation(
    evidence: Mapping[str, object], ledger: object
) -> dict[str, object]:
    """Resolve the calibration binding against canonical seed-ledger authority.

    This is the #346 ``require_allocation_binding`` contract, not a shape
    check: the immutable allocation identity must be uniquely present in the
    canonical ledger, still active, in the declared domain, owned by the
    dedicated calibration population/protocol, and its membership must be
    exactly the seeds the calibration executed. Unrelated later reservations
    do not invalidate it, matching the retained-binding semantics.
    """

    seeds = calibration_seeds(evidence)
    record = seed_registry.require_allocation_binding(
        ledger,
        evidence["seed_allocation"],
        seeds=seeds,
        protocol=CALIBRATION_PROTOCOL,
        population=CALIBRATION_POPULATION,
    )
    if record["split"] is not None:
        raise seed_registry.SeedRegistryError(
            "a calibration allocation must not declare a scientific split"
        )
    return record


def evaluate_calibration_match(
    evidence: Mapping[str, object],
    requirement: Mapping[str, object],
    *,
    now: datetime,
    ledger: object = None,
) -> dict[str, object]:
    """Evaluate one calibration against the production admission requirement.

    ``ledger`` is the canonical seed-registry authority. It is not optional in
    practice: without it the calibration allocation cannot be resolved and the
    authority check fails closed, because a locally fabricated binding of
    well-formed SHA-256 values would otherwise satisfy the shape check alone.
    """

    candidate = validate_calibration_evidence(dict(evidence))
    required = validate_admission_requirement(dict(requirement))
    target = required["target"]
    policy = required["calibration_policy"]
    allocation = required["production_allocation"]
    evaluated_at = _aware(now, "now")
    checks: list[dict[str, object]] = []

    def record(name: str, ok: bool, detail: str) -> None:
        checks.append(
            {"name": name, "status": "PASS" if ok else "FAIL", "detail": detail}
        )

    for field in (
        "arena_revision",
        "lisjong_revision",
        "lisjong_engine_revision",
        "riichienv_version",
        "workload_identity",
        "teacher_identity",
        "game_mode",
        "instance_type",
        "instrumentation_identity",
    ):
        observed = candidate[field]
        expected = target[field]
        record(
            f"identity:{field}",
            observed == expected,
            f"calibration {field}={observed!r}; required {expected!r}",
        )

    record(
        "identity:vcpu",
        candidate["vcpu"] == target["vcpu"],
        f"calibration vcpu={candidate['vcpu']}; required {target['vcpu']}",
    )

    workers = int(target["worker_count"])
    measured_workers = int(candidate["worker_count_requested"])
    record(
        "worker-count-exact",
        measured_workers == workers,
        (
            f"calibration measured {measured_workers} workers; "
            f"{workers} requested. Worker counts are never extrapolated."
        ),
    )
    # The next two checks are evaluated against the calibration's own worker
    # count, so an otherwise sound measurement at a different worker count is
    # still reported as an empirically supported point rather than as noise.
    record(
        "concurrency-exercised",
        int(candidate["peak_observed_concurrency"]) >= measured_workers,
        (
            f"peak observed concurrency {candidate['peak_observed_concurrency']} "
            f"must reach the {measured_workers} workers it requested"
        ),
    )
    minimum_tasks = max(
        int(policy["minimum_task_count"]),
        math.ceil(measured_workers * float(policy["minimum_tasks_per_worker"])),
    )
    record(
        "task-count-sufficient",
        int(candidate["task_count"]) >= minimum_tasks,
        (
            f"calibration observed {candidate['task_count']} tasks; "
            f"at least {minimum_tasks} required to exercise {measured_workers} workers"
        ),
    )

    required_level = DURABLE_EVIDENCE_LEVELS.index(
        str(target["durable_evidence_level"])
    )
    observed_level = DURABLE_EVIDENCE_LEVELS.index(
        str(candidate["durable_evidence_level"])
    )
    record(
        "durable-evidence-level",
        observed_level >= required_level,
        (
            f"calibration durable evidence level {candidate['durable_evidence_level']!r} "
            f"must be at least {target['durable_evidence_level']!r}"
        ),
    )

    calibrated_at = _parse_timestamp(candidate["calibrated_at"], "calibrated_at")
    age = (evaluated_at - calibrated_at).total_seconds()
    record(
        "freshness",
        0 <= age <= float(policy["freshness_max_age_seconds"]),
        (
            f"calibration age {age:.0f}s against the "
            f"{float(policy['freshness_max_age_seconds']):.0f}s freshness policy"
        ),
    )

    missing_required = sorted(
        set(candidate["unavailable_fields"])
        & {
            "ec2_billable_runtime_seconds",
            "setup_overhead_seconds",
            "teardown_overhead_seconds",
        }
    )
    record(
        "completeness",
        not missing_required,
        (
            "billable-overhead fields are unavailable: "
            f"{missing_required!r}; incomplete calibration cannot price a run"
            if missing_required
            else "all admission-required calibration fields are present"
        ),
    )

    burstable = candidate["instance_family"] in BURSTABLE_INSTANCE_FAMILIES
    record(
        "burstable-sustained-basis",
        (not burstable) or bool(candidate["burstable_sustained_basis"]),
        (
            f"{candidate['instance_family']!r} is burstable; an explicit sustained / "
            "CPU-credit basis is required before a short observation is used"
            if burstable
            else f"{candidate['instance_family']!r} is not a burstable family"
        ),
    )

    executed_seeds = set(calibration_seeds(candidate))
    production_seeds = set(int(seed) for seed in allocation["seeds"])
    overlapping = sorted(executed_seeds & production_seeds)
    isolated = (
        candidate["seed_allocation_population"] == CALIBRATION_POPULATION
        and candidate["seed_allocation"]["allocation_identity"]
        not in set(allocation["allocation_identities"])
        and candidate["seed_allocation"]["seed_membership_identity"]
        not in set(allocation["seed_membership_identities"])
        and not overlapping
    )
    record(
        "calibration-seed-isolation",
        isolated,
        (
            "calibration must use a dedicated "
            f"{CALIBRATION_POPULATION!r} allocation that is not a production "
            f"allocation; seeds shared with the production population: "
            f"{overlapping!r}"
        ),
    )

    if ledger is None:
        record(
            "calibration-allocation-authority",
            False,
            "canonical seed-registry ledger was not supplied; a calibration "
            "binding is never accepted on its shape alone",
        )
    else:
        try:
            resolved = resolve_calibration_allocation(candidate, ledger)
        except seed_registry.SeedRegistryError as error:
            record(
                "calibration-allocation-authority",
                False,
                f"calibration allocation does not resolve against canonical "
                f"seed-registry authority: {error}",
            )
        else:
            same_domain = resolved["seed_domain"] == allocation["seed_domain"]
            record(
                "calibration-allocation-authority",
                same_domain,
                (
                    f"calibration allocation {resolved['allocation_identity']} is "
                    f"{resolved['state']} in domain {resolved['seed_domain']!r}; "
                    f"the production population uses "
                    f"{allocation['seed_domain']!r}. Both must share a domain so "
                    "the ledger's same-domain non-overlap invariant applies."
                ),
            )

    reasons = [
        f"{check['name']}: {check['detail']}"
        for check in checks
        if check["status"] == "FAIL"
    ]
    return {
        "calibration_identity": candidate["calibration_identity"],
        "calibration_run_id": candidate["calibration_run_id"],
        "calibrated_at": candidate["calibrated_at"],
        "age_seconds": _round(age),
        "worker_count_requested": candidate["worker_count_requested"],
        "status": "NO-MATCH" if reasons else "MATCH",
        "checks": checks,
        "reasons": reasons,
    }


def select_matching_calibration(
    calibrations: Sequence[Mapping[str, object]],
    requirement: Mapping[str, object],
    *,
    now: datetime,
    ledger: object = None,
) -> dict[str, object]:
    """Pick the freshest matching calibration, or report why none matched.

    ``supported_worker_counts`` lists the worker counts for which admissible
    evidence actually exists.  A requested worker count outside that set is a
    No-Go: this function never interpolates or extrapolates between points.
    """

    reports = [
        evaluate_calibration_match(candidate, requirement, now=now, ledger=ledger)
        for candidate in calibrations
    ]
    matched = [report for report in reports if report["status"] == "MATCH"]
    matched.sort(key=lambda report: str(report["calibrated_at"]), reverse=True)
    supported: set[int] = set()
    for candidate, report in zip(calibrations, reports, strict=True):
        blocking = {
            str(check["name"])
            for check in report["checks"]
            if check["status"] == "FAIL" and check["name"] != "worker-count-exact"
        }
        if not blocking:
            supported.add(int(dict(candidate)["worker_count_requested"]))
    selected = None
    if matched:
        identity = matched[0]["calibration_identity"]
        selected = next(
            dict(candidate)
            for candidate in calibrations
            if dict(candidate)["calibration_identity"] == identity
        )
    return {
        "selected": selected,
        "reports": reports,
        "supported_worker_counts": sorted(supported),
    }


def predict_runtime(
    evidence: Mapping[str, object],
    *,
    total_units: int,
    worker_count: int,
    headroom_factor: float,
) -> dict[str, object]:
    """Predict scientific runtime from measured evidence at the same shape.

    The lower bound rescales the observed batch wall clock by the unit ratio at
    the *same* worker count.  The upper bound adds the scheduling tail:
    ``ceil(units / workers)`` waves at the observed p90.  Neither divides a
    single-unit duration by the worker count.
    """

    candidate = validate_calibration_evidence(dict(evidence))
    units = _positive_int(total_units, "total_units")
    workers = _positive_int(worker_count, "worker_count")
    factor = _number(headroom_factor, "headroom_factor")
    _require(factor >= 1.0, "headroom_factor must be at least 1.0")
    _require(
        int(candidate["worker_count_requested"]) == workers,
        "runtime prediction requires calibration at the exact worker count",
    )
    wall_clock = float(candidate["batch_scientific_wall_clock_seconds"])
    task_count = int(candidate["task_count"])
    p90 = float(candidate["p90_seconds_per_unit"])
    lower = wall_clock * units / task_count
    waves = math.ceil(units / workers)
    tail = waves * p90
    upper = max(lower, tail)
    return {
        "available": True,
        "basis": (
            f"{EVIDENCE_SCHEMA_VERSION}:{candidate['calibration_identity']} "
            f"instance={candidate['instance_type']} "
            f"workers={workers} calibration_tasks={task_count} "
            f"wall_clock_seconds={wall_clock} "
            f"p50={candidate['p50_seconds_per_unit']} "
            f"p90={candidate['p90_seconds_per_unit']}"
        ),
        "calibration_identity": candidate["calibration_identity"],
        "headroom_adjusted_upper_seconds": _round(upper * factor),
        "headroom_factor": factor,
        "method": (
            "lower = observed batch wall clock scaled by the unit ratio at the same "
            "worker count; upper = max(lower, ceil(units / workers) * observed p90). "
            "The p90 term is a descriptive tail allowance, not a statistical bound."
        ),
        "predicted_lower_seconds": _round(lower),
        "predicted_upper_seconds": _round(upper),
        "sample_task_count": task_count,
        "total_units": units,
        "unit_scaled_lower_seconds": _round(lower),
        "wave_tail_upper_seconds": _round(tail),
        "waves": waves,
        "worker_count": workers,
    }


def unavailable_runtime(reason: str) -> dict[str, object]:
    return {"available": False, "reason": _text(reason, "reason")}


def measured_billable_overhead(
    calibration: Mapping[str, object] | None, budget: Mapping[str, object]
) -> dict[str, object]:
    """Combine the planned overhead allowance with the measured calibration one.

    A planned setup/teardown allowance smaller than the measured matching basis
    must never make the predicted cost cheaper, so each component is the
    element-wise maximum of planned and measured. The calibration's own
    residual billable time -- the part of its EC2 billable runtime that is
    neither setup, scientific wall clock nor teardown -- is carried through as
    well, so launch/attach/SSM-wait overhead is not silently dropped.
    """

    planned_setup = float(budget["setup_seconds"])
    planned_post = float(budget["post_processing_seconds"])
    planned_teardown = float(budget["teardown_seconds"])
    measured_setup = None
    measured_teardown = None
    measured_residual = None
    if calibration is not None:
        measured_setup = calibration["setup_overhead_seconds"]
        measured_teardown = calibration["teardown_overhead_seconds"]
        billable = calibration["ec2_billable_runtime_seconds"]
        if (
            billable is not None
            and measured_setup is not None
            and measured_teardown is not None
        ):
            measured_residual = max(
                0.0,
                float(billable)
                - float(measured_setup)
                - float(calibration["batch_scientific_wall_clock_seconds"])
                - float(measured_teardown),
            )
    effective_setup = max(
        planned_setup, 0.0 if measured_setup is None else float(measured_setup)
    )
    effective_teardown = max(
        planned_teardown, 0.0 if measured_teardown is None else float(measured_teardown)
    )
    effective_residual = 0.0 if measured_residual is None else float(measured_residual)
    return {
        "effective_residual_seconds": _round(effective_residual),
        "effective_setup_seconds": _round(effective_setup),
        "effective_teardown_seconds": _round(effective_teardown),
        "measured_residual_seconds": (
            None if measured_residual is None else _round(measured_residual)
        ),
        "measured_setup_seconds": (
            None if measured_setup is None else _round(float(measured_setup))
        ),
        "measured_teardown_seconds": (
            None if measured_teardown is None else _round(float(measured_teardown))
        ),
        "planned_post_processing_seconds": _round(planned_post),
        "planned_setup_seconds": _round(planned_setup),
        "planned_teardown_seconds": _round(planned_teardown),
        "planned_allowance_below_measured": bool(
            (measured_setup is not None and planned_setup < float(measured_setup))
            or (
                measured_teardown is not None
                and planned_teardown < float(measured_teardown)
            )
        ),
        "total_seconds": _round(
            effective_setup + planned_post + effective_teardown + effective_residual
        ),
    }


def predict_cost(
    runtime: Mapping[str, object],
    requirement: Mapping[str, object],
    calibration: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Price the predicted EC2 billable window plus declared material charges.

    The billable window is built from the *measured* calibration overhead
    combined conservatively with the planned allowance, so a calibration with a
    longer observed launch/setup/teardown really does predict a higher cost.
    """

    required = validate_admission_requirement(dict(requirement))
    budget = required["budget"]
    pricing = required["pricing"]
    charges = required["charges"]
    matched = (
        None
        if calibration is None
        else validate_calibration_evidence(dict(calibration))
    )
    overhead_basis = measured_billable_overhead(matched, budget)
    overhead = float(overhead_basis["total_seconds"])
    unbounded = sorted(
        str(charge["label"])
        for charge in charges
        if charge["material"] and charge["usd"] is None and charge["max_usd"] is None
    )
    known = sum(float(charge["usd"]) for charge in charges if charge["usd"] is not None)
    bounded = sum(
        float(charge["max_usd"])
        for charge in charges
        if charge["usd"] is None and charge["max_usd"] is not None
    )
    common: dict[str, object] = {
        "charges": charges,
        "known_charges_usd": _round(known),
        "bounded_charges_usd": _round(bounded),
        "unbounded_material_charges": unbounded,
        "is_finalized_aws_invoice": False,
        "overhead_basis": overhead_basis,
        "pricing": pricing,
    }
    if matched is None and runtime.get("available"):
        return {
            "available": False,
            "reason": (
                "predicted EC2 cost requires the measured billable-overhead basis "
                "from the matching calibration"
            ),
            **common,
        }
    if not runtime.get("available"):
        return {
            "available": False,
            "reason": "runtime prediction unavailable",
            **common,
        }
    if pricing["instance_hourly_rate_usd"] is None:
        return {
            "available": False,
            "reason": "instance hourly rate unavailable; pricing provenance incomplete",
            **common,
        }
    if unbounded:
        return {
            "available": False,
            "reason": (
                "unknown material charge is unbounded and is never treated as zero: "
                f"{unbounded!r}"
            ),
            **common,
        }
    rate = float(pricing["instance_hourly_rate_usd"])
    billable_lower = overhead + float(runtime["predicted_lower_seconds"])
    billable_upper = overhead + float(runtime["headroom_adjusted_upper_seconds"])
    ec2_lower = billable_lower / 3600.0 * rate
    ec2_upper = billable_upper / 3600.0 * rate
    return {
        "available": True,
        "billable_overhead_seconds": _round(overhead),
        "predicted_ec2_billable_runtime_range_seconds": [
            _round(billable_lower),
            _round(billable_upper),
        ],
        "predicted_ec2_cost_range_usd": [_round(ec2_lower), _round(ec2_upper)],
        "predicted_total_cost_range_usd": [
            _round(ec2_lower + known),
            _round(ec2_upper + known + bounded),
        ],
        "reason": None,
        **common,
    }


def evaluate_budget(
    runtime: Mapping[str, object], requirement: Mapping[str, object]
) -> dict[str, object]:
    """Separate the execution budget, the normal deadline and the hard fail-safe."""

    required = validate_admission_requirement(dict(requirement))
    budget = required["budget"]
    execution_budget = float(budget["execution_budget_seconds"])
    normal_deadline = float(budget["normal_deadline_seconds"])
    hard_fail_safe = float(budget["hard_fail_safe_seconds"])
    setup = float(budget["setup_seconds"])
    post = float(budget["post_processing_seconds"])
    teardown = float(budget["teardown_seconds"])
    deadline_consistency = _fits(normal_deadline + teardown, hard_fail_safe)
    if not runtime.get("available"):
        unavailable = {
            "status": "FAIL",
            "detail": "runtime prediction unavailable",
        }
        return {
            "clock_origins": dict(CLOCK_ORIGINS),
            "hard-fail-safe": {
                "status": "PASS" if deadline_consistency else "FAIL",
                "detail": (
                    f"normal deadline {normal_deadline}s + teardown {teardown}s "
                    f"against hard fail-safe {hard_fail_safe}s"
                ),
            },
            "normal-deadline": dict(unavailable),
            "runtime-headroom": dict(unavailable),
        }
    adjusted = float(runtime["headroom_adjusted_upper_seconds"])
    normal_required = setup + adjusted + post
    return {
        "clock_origins": dict(CLOCK_ORIGINS),
        "hard-fail-safe": {
            "status": "PASS" if deadline_consistency else "FAIL",
            "detail": (
                f"normal deadline {normal_deadline}s + teardown {teardown}s "
                f"against hard fail-safe {hard_fail_safe}s"
            ),
        },
        "normal-deadline": {
            "status": "PASS" if _fits(normal_required, normal_deadline) else "FAIL",
            "detail": (
                f"setup {setup}s + headroom-adjusted runtime {adjusted}s + "
                f"post-processing {post}s = {_round(normal_required)}s against "
                f"normal deadline {normal_deadline}s from the fail-safe arm epoch"
            ),
        },
        "runtime-headroom": {
            "status": "PASS" if _fits(adjusted, execution_budget) else "FAIL",
            "detail": (
                f"predicted upper {runtime['predicted_upper_seconds']}s x headroom "
                f"{runtime['headroom_factor']} = {adjusted}s against execution budget "
                f"{execution_budget}s from the workload start"
            ),
        },
    }


def _gate(name: str, ok: bool, detail: str) -> dict[str, object]:
    return {"name": name, "status": "PASS" if ok else "FAIL", "detail": detail}


def build_phase_one_admission(
    requirement: Mapping[str, object],
    calibrations: Sequence[Mapping[str, object]],
    *,
    now: datetime,
    ledger: object = None,
) -> dict[str, object]:
    """Evaluate the pre-billing admission gate. No-Go forbids billable creation."""

    required = validate_admission_requirement(dict(requirement))
    target = required["target"]
    gates_input = required["gates"]
    evaluated_at = _aware(now, "now")
    selection = select_matching_calibration(
        calibrations, required, now=evaluated_at, ledger=ledger
    )
    selected = selection["selected"]

    if selected is None:
        runtime = unavailable_runtime(
            "no calibration matches this exact code/workload/instance/worker "
            f"configuration; empirically supported worker counts: "
            f"{selection['supported_worker_counts']!r}"
        )
    else:
        runtime = predict_runtime(
            selected,
            total_units=int(target["total_units"]),
            worker_count=int(target["worker_count"]),
            headroom_factor=float(required["budget"]["headroom_factor"]),
        )
    cost = predict_cost(runtime, required, selected)
    budget_checks = evaluate_budget(runtime, required)

    calibration_reasons: list[str] = []
    for report in selection["reports"]:
        calibration_reasons.extend(
            f"calibration {report['calibration_identity'][:16]}: {reason}"
            for reason in report["reasons"]
        )
    if not calibrations:
        calibration_reasons.append("no calibration evidence was supplied")

    gates = [
        _gate(
            "protocol-lock",
            gates_input["protocol_lock"]["status"] == "PASS",
            str(gates_input["protocol_lock"]["detail"]),
        ),
        _gate(
            "seed-registry-allocation",
            gates_input["seed_registry"]["status"] == "PASS",
            str(gates_input["seed_registry"]["detail"]),
        ),
        _gate(
            "matching-calibration",
            selected is not None,
            (
                f"selected calibration {selected['calibration_identity']}"
                if selected is not None
                else "; ".join(calibration_reasons)
            ),
        ),
        _gate(
            "runtime-prediction",
            bool(runtime.get("available")),
            str(runtime.get("basis") or runtime.get("reason")),
        ),
        _gate(
            "cost-prediction",
            bool(cost.get("available")),
            str(cost.get("reason") or "predicted EC2 billable cost is available"),
        ),
        _gate(
            "cost-budget",
            bool(cost.get("available"))
            and _fits(
                float(cost["predicted_total_cost_range_usd"][1]),
                float(required["budget"]["cost_budget_usd"]),
            ),
            (
                f"predicted upper total {cost['predicted_total_cost_range_usd'][1]} USD "
                f"against budget {required['budget']['cost_budget_usd']} USD"
                if cost.get("available")
                else "cost prediction unavailable"
            ),
        ),
        _gate(
            "runtime-headroom",
            budget_checks["runtime-headroom"]["status"] == "PASS",
            str(budget_checks["runtime-headroom"]["detail"]),
        ),
        _gate(
            "normal-deadline",
            budget_checks["normal-deadline"]["status"] == "PASS",
            str(budget_checks["normal-deadline"]["detail"]),
        ),
        _gate(
            "hard-fail-safe",
            budget_checks["hard-fail-safe"]["status"] == "PASS",
            str(budget_checks["hard-fail-safe"]["detail"]),
        ),
        _gate(
            "durable-evidence-support",
            gates_input["durable_evidence"]["status"] == "PASS",
            str(gates_input["durable_evidence"]["detail"]),
        ),
        _gate(
            "artifact-destination-retention",
            gates_input["artifact_destination"]["status"] == "PASS",
            str(gates_input["artifact_destination"]["detail"]),
        ),
        _gate(
            "reattach-capability",
            gates_input["reattach"]["status"] == "PASS",
            str(gates_input["reattach"]["detail"]),
        ),
    ]
    _require(
        tuple(str(gate["name"]) for gate in gates) == PHASE_ONE_GATE_NAMES,
        "phase 1 gate set is incomplete",
    )
    blocking = [
        f"{gate['name']}: {gate['detail']}"
        for gate in gates
        if gate["status"] == "FAIL"
    ]
    decision = "NO-GO" if blocking else "GO"
    limitations = ["small-sample percentiles are descriptive, not guaranteed bounds"]
    if selected is not None:
        limitations.extend(str(item) for item in selected["limitations"])
        limitations.extend(
            f"calibration field unavailable: {field}"
            for field in selected["unavailable_fields"]
        )
    document: dict[str, object] = {
        "schema_version": ADMISSION_SCHEMA_VERSION,
        "admission_identity": "",
        "admission_phase": 1,
        "billable_resource_creation_authorized": decision == "GO",
        "blocking_reasons": blocking,
        "budget": required["budget"],
        "calibration": {
            "calibrated_at": None if selected is None else selected["calibrated_at"],
            "identity": None if selected is None else selected["calibration_identity"],
            "match_reports": selection["reports"],
            "run_id": None if selected is None else selected["calibration_run_id"],
            "supported_worker_counts": selection["supported_worker_counts"],
        },
        "clock_origins": dict(CLOCK_ORIGINS),
        "consumer": required["consumer"],
        "cost_prediction": cost,
        "decision": decision,
        "evaluated_at": _timestamp(evaluated_at),
        "gates": gates,
        "limitations": limitations,
        "phase_one_admission_identity": None,
        "run_id": required["run_id"],
        "runtime_prediction": runtime,
        "scientific_submission_authorized": False,
        "target": target,
        "workload_submission_authorized": False,
    }
    document["admission_identity"] = _digest(
        {key: value for key, value in document.items() if key != "admission_identity"}
    )
    return validate_admission_record(document)


def validate_phase_two_observation(document: object) -> dict[str, object]:
    _require(type(document) is dict, "phase 2 observation must be an object")
    observation = dict(document)
    _require(
        set(observation) == _OBSERVATION_FIELDS,
        "phase 2 observation fields are invalid",
    )
    _require(
        observation["schema_version"] == OBSERVATION_SCHEMA_VERSION,
        "unsupported phase 2 observation schema",
    )
    assert_no_scientific_fields(observation, context="phase 2 observation")
    _text(observation["run_id"], "run_id")
    _sha256_text(
        observation["phase_one_admission_identity"], "phase_one_admission_identity"
    )
    _require(type(observation["instance_id"]) is str, "instance_id must be a string")
    _text(observation["instance_type"], "instance_type")
    _text(observation["availability_zone"], "availability_zone")
    _sha1_text(observation["arena_revision"], "arena_revision")
    _positive_int(observation["vcpu"], "vcpu")
    _positive_int(observation["worker_count"], "worker_count")
    _require(
        type(observation["fail_safe_armed"]) is bool, "fail_safe_armed must be a bool"
    )
    for name in (
        "fail_safe_arm_epoch",
        "fail_safe_deadline_epoch",
        "instance_launch_epoch",
        "observed_at_epoch",
    ):
        _require(type(observation[name]) is int, f"{name} must be an int")
    destination = observation["retained_destination"]
    _require(type(destination) is dict, "retained_destination must be an object")
    _require(
        set(destination)
        == {
            "encrypted",
            "path",
            "retention",
            "size_gib",
            "verified_on_instance_id",
            "volume_id",
            "write_probe",
        },
        "retained_destination fields are invalid",
    )
    recovery = observation["recovery_identity"]
    _require(type(recovery) is dict, "recovery_identity must be an object")
    _require(
        set(recovery) == {"path", "run_id", "status", "verified_on_instance_id"},
        "recovery_identity fields are invalid",
    )
    return observation


def build_phase_two_admission(
    phase_one: Mapping[str, object],
    observation: Mapping[str, object],
    *,
    now: datetime,
) -> dict[str, object]:
    """Evaluate the post-provisioning gate before the first scientific submission."""

    admitted = validate_admission_record(dict(phase_one))
    _require(
        admitted["admission_phase"] in (0, 1),
        "phase 2 requires a calibration (phase 0) or production (phase 1) record",
    )
    _require(
        admitted["decision"] == "GO",
        "phase 2 requires a prior GO; a NO-GO run creates no instance",
    )
    observed = validate_phase_two_observation(dict(observation))
    evaluated_at = _aware(now, "now")
    target = admitted["target"]
    budget = admitted["budget"]
    runtime = admitted["runtime_prediction"]

    instance_id = observed["instance_id"]
    instance_present = type(instance_id) is str and bool(
        _INSTANCE_ID.fullmatch(str(instance_id))
    )
    identity_mismatch = [
        name
        for name, observed_value, expected in (
            ("run_id", observed["run_id"], admitted["run_id"]),
            (
                "phase_one_admission_identity",
                observed["phase_one_admission_identity"],
                admitted["admission_identity"],
            ),
            ("instance_type", observed["instance_type"], target["instance_type"]),
            ("vcpu", observed["vcpu"], target["vcpu"]),
            ("worker_count", observed["worker_count"], target["worker_count"]),
            ("arena_revision", observed["arena_revision"], target["arena_revision"]),
        )
        if observed_value != expected
    ]

    hard_fail_safe = float(budget["hard_fail_safe_seconds"])
    arm_epoch = int(observed["fail_safe_arm_epoch"])
    deadline_epoch = int(observed["fail_safe_deadline_epoch"])
    observed_at = int(observed["observed_at_epoch"])
    launch_epoch = int(observed["instance_launch_epoch"])
    armed = (
        instance_present
        and bool(observed["fail_safe_armed"])
        and abs((deadline_epoch - arm_epoch) - hard_fail_safe) <= 60
        and deadline_epoch > observed_at
    )

    destination = observed["retained_destination"]
    writable = (
        instance_present
        and destination["write_probe"] == "PASS"
        and bool(str(destination["volume_id"] or "").strip())
        and destination["verified_on_instance_id"] == instance_id
    )
    recovery = observed["recovery_identity"]
    persisted = (
        instance_present
        and recovery["status"] == "PASS"
        and bool(str(recovery["path"] or "").strip())
        and recovery["verified_on_instance_id"] == instance_id
        and recovery["run_id"] == admitted["run_id"]
    )

    elapsed = observed_at - arm_epoch
    realized_billable = float(observed_at - launch_epoch)
    cost = admitted["cost_prediction"]
    overhead_basis = cost.get("overhead_basis") or {}
    effective_teardown = float(
        overhead_basis.get("effective_teardown_seconds", budget["teardown_seconds"])
    )
    post_processing = float(budget["post_processing_seconds"])
    if runtime.get("available"):
        remaining_workload = float(runtime["headroom_adjusted_upper_seconds"])
        remaining_basis = "headroom-adjusted predicted runtime"
    elif admitted["admission_phase"] == 0:
        # A calibration has no prior prediction by design, so its remaining
        # exposure is bounded by its own independent hard fail-safe window.
        remaining_workload = max(
            0.0,
            hard_fail_safe - float(elapsed) - post_processing - effective_teardown,
        )
        remaining_basis = "worst-case remaining hard fail-safe window"
    else:
        remaining_workload = None
        remaining_basis = "unavailable"

    ordering_ok = launch_epoch <= arm_epoch <= observed_at
    if remaining_workload is None:
        time_ok = False
        time_detail = "the admitted runtime prediction was unavailable"
        cost_ok = False
        cost_detail = "the admitted runtime prediction was unavailable"
    else:
        remaining_required = (
            float(elapsed) + remaining_workload + post_processing + effective_teardown
        )
        time_ok = ordering_ok and _fits(remaining_required, hard_fail_safe)
        time_detail = (
            f"time: elapsed since fail-safe arm {elapsed}s + {remaining_basis} "
            f"{_round(remaining_workload)}s + post-processing {post_processing}s + "
            f"teardown {effective_teardown}s = {_round(remaining_required)}s against "
            f"hard fail-safe {hard_fail_safe}s"
        )
        rate = cost["pricing"]["instance_hourly_rate_usd"]
        cost_budget = float(budget["cost_budget_usd"])
        if rate is None or not cost.get("available"):
            cost_ok = False
            cost_detail = (
                "cost: the admitted cost prediction is unavailable, so remaining "
                "monetary budget cannot be re-evaluated"
            )
        else:
            projected_billable = (
                realized_billable
                + remaining_workload
                + post_processing
                + effective_teardown
            )
            projected_cost = (
                projected_billable / 3600.0 * float(rate)
                + float(cost["known_charges_usd"])
                + float(cost["bounded_charges_usd"])
            )
            cost_ok = ordering_ok and _fits(projected_cost, cost_budget)
            cost_detail = (
                f"cost: realized billable {_round(realized_billable)}s since "
                f"instance launch + remaining {_round(remaining_workload + post_processing + effective_teardown)}s "
                f"at {rate} USD/h + known {cost['known_charges_usd']} USD + bounded "
                f"{cost['bounded_charges_usd']} USD = {_round(projected_cost)} USD "
                f"against cost budget {cost_budget} USD"
            )
    budget_ok = time_ok and cost_ok
    budget_detail = (
        f"{time_detail}; {cost_detail}"
        if ordering_ok
        else (
            f"instance launch {launch_epoch} / fail-safe arm {arm_epoch} / "
            f"observation {observed_at} are not in order; {time_detail}; {cost_detail}"
        )
    )

    gates = [
        _gate(
            "actual-environment-identity",
            instance_present and not identity_mismatch,
            (
                f"instance {instance_id!r}; mismatched fields {identity_mismatch!r}"
                if identity_mismatch or not instance_present
                else f"instance {instance_id} matches the admitted plan"
            ),
        ),
        _gate(
            "hard-fail-safe-armed",
            armed,
            (
                f"armed={observed['fail_safe_armed']} arm_epoch={arm_epoch} "
                f"deadline_epoch={deadline_epoch} against hard fail-safe "
                f"{hard_fail_safe}s on instance {instance_id!r}"
            ),
        ),
        _gate(
            "retained-destination-writable",
            writable,
            (
                f"volume {destination['volume_id']!r} write probe "
                f"{destination['write_probe']!r} verified on instance "
                f"{destination['verified_on_instance_id']!r}"
            ),
        ),
        _gate(
            "recovery-identity-persisted",
            persisted,
            (
                f"recovery identity {recovery['status']!r} at {recovery['path']!r} "
                f"verified on instance {recovery['verified_on_instance_id']!r}"
            ),
        ),
        _gate("remaining-budget-sufficient", budget_ok, budget_detail),
    ]
    _require(
        tuple(str(gate["name"]) for gate in gates) == PHASE_TWO_GATE_NAMES,
        "phase 2 gate set is incomplete",
    )
    blocking = [
        f"{gate['name']}: {gate['detail']}"
        for gate in gates
        if gate["status"] == "FAIL"
    ]
    decision = "NO-GO" if blocking else "GO"
    document: dict[str, object] = {
        "schema_version": ADMISSION_SCHEMA_VERSION,
        "admission_identity": "",
        "admission_phase": 2,
        "billable_resource_creation_authorized": True,
        "blocking_reasons": blocking,
        "budget": budget,
        "calibration": admitted["calibration"],
        "clock_origins": dict(CLOCK_ORIGINS),
        "consumer": admitted["consumer"],
        "cost_prediction": admitted["cost_prediction"],
        "decision": decision,
        "evaluated_at": _timestamp(evaluated_at),
        "gates": gates,
        "limitations": (
            list(admitted["limitations"])
            if decision == "GO"
            else [
                *admitted["limitations"],
                "phase 2 No-Go: submit no workload and proceed to bounded "
                "cleanup, recording any residual resource",
            ]
        ),
        "phase_one_admission_identity": admitted["admission_identity"],
        "run_id": admitted["run_id"],
        "runtime_prediction": runtime,
        # A calibration run never becomes permission to submit a scientific
        # seed, however its own phase 2 turns out.
        "scientific_submission_authorized": (
            decision == "GO" and admitted["admission_phase"] == 1
        ),
        "target": target,
        "workload_submission_authorized": decision == "GO",
    }
    document["admission_identity"] = _digest(
        {key: value for key, value in document.items() if key != "admission_identity"}
    )
    return validate_admission_record(document)


_ADMISSION_FIELDS: Final = frozenset(
    {
        "admission_identity",
        "admission_phase",
        "billable_resource_creation_authorized",
        "blocking_reasons",
        "budget",
        "calibration",
        "clock_origins",
        "consumer",
        "cost_prediction",
        "decision",
        "evaluated_at",
        "gates",
        "limitations",
        "phase_one_admission_identity",
        "run_id",
        "runtime_prediction",
        "schema_version",
        "scientific_submission_authorized",
        "target",
        "workload_submission_authorized",
    }
)


def validate_admission_record(document: object) -> dict[str, object]:
    _require(type(document) is dict, "admission record must be an object")
    record = dict(document)
    _require(set(record) == _ADMISSION_FIELDS, "admission record fields are invalid")
    _require(
        record["schema_version"] == ADMISSION_SCHEMA_VERSION,
        "unsupported admission record schema",
    )
    assert_no_scientific_fields(record, context="admission record")
    _require(
        record["admission_phase"] in (0, 1, 2), "admission_phase must be 0, 1 or 2"
    )
    _require(record["decision"] in ("GO", "NO-GO"), "decision must be GO or NO-GO")
    identity = _sha256_text(record["admission_identity"], "admission_identity")
    _require(
        identity
        == _digest(
            {key: value for key, value in record.items() if key != "admission_identity"}
        ),
        "admission record identity mismatch",
    )
    gates = record["gates"]
    expected = GATE_NAMES_BY_PHASE[int(record["admission_phase"])]
    _require(type(gates) is list, "gates must be a list")
    _require(
        tuple(str(gate["name"]) for gate in gates) == expected,
        "admission gate set is incomplete",
    )
    failing = [gate for gate in gates if gate["status"] == "FAIL"]
    _require(
        (record["decision"] == "NO-GO") == bool(failing),
        "decision is inconsistent with the gate results",
    )
    _require(
        len(record["blocking_reasons"]) == len(failing),
        "blocking reasons are inconsistent with the failing gates",
    )
    if record["admission_phase"] == 0:
        _require(
            record["scientific_submission_authorized"] is False,
            "a calibration admission never authorizes scientific submission",
        )
        _require(
            record["billable_resource_creation_authorized"]
            == (record["decision"] == "GO")
            and record["workload_submission_authorized"]
            == (record["decision"] == "GO"),
            "calibration admission authorization is inconsistent",
        )
        _require(
            record["phase_one_admission_identity"] is None,
            "a calibration admission does not reference an earlier admission",
        )
    elif record["admission_phase"] == 1:
        _require(
            record["scientific_submission_authorized"] is False
            and record["workload_submission_authorized"] is False,
            "phase 1 never authorizes submission",
        )
        _require(
            record["billable_resource_creation_authorized"]
            == (record["decision"] == "GO"),
            "phase 1 billable authorization is inconsistent",
        )
        _require(
            record["phase_one_admission_identity"] is None,
            "phase 1 does not reference an earlier admission",
        )
    else:
        _sha256_text(
            record["phase_one_admission_identity"], "phase_one_admission_identity"
        )
        _require(
            record["workload_submission_authorized"] == (record["decision"] == "GO"),
            "phase 2 submission authorization is inconsistent",
        )
        _require(
            record["scientific_submission_authorized"] is False
            or record["workload_submission_authorized"] is True,
            "scientific submission cannot outrank workload submission",
        )
    return record


def validate_calibration_admission_requirement(document: object) -> dict[str, object]:
    """Validate the bounded calibration run's own admission requirement."""

    _require(type(document) is dict, "calibration requirement must be an object")
    requirement = dict(document)
    _require(
        set(requirement) == _CALIBRATION_REQUIREMENT_FIELDS,
        "calibration requirement fields are invalid",
    )
    _require(
        requirement["schema_version"] == CALIBRATION_REQUIREMENT_SCHEMA_VERSION,
        "unsupported calibration requirement schema",
    )
    assert_no_scientific_fields(requirement, context="calibration requirement")
    _text(requirement["consumer"], "consumer")
    _text(requirement["run_id"], "run_id")

    target = requirement["target"]
    _require(
        type(target) is dict and set(target) == _TARGET_FIELDS,
        "calibration target is invalid",
    )
    for name in ("arena_revision", "lisjong_revision", "lisjong_engine_revision"):
        _sha1_text(target[name], f"target.{name}")
    for name in (
        "riichienv_version",
        "workload_identity",
        "teacher_identity",
        "game_mode",
        "instance_type",
        "instrumentation_identity",
    ):
        _text(target[name], f"target.{name}")
    _positive_int(target["vcpu"], "target.vcpu")
    _positive_int(target["worker_count"], "target.worker_count")
    _positive_int(target["total_units"], "target.total_units")
    _require(
        target["durable_evidence_level"] in DURABLE_EVIDENCE_LEVELS
        and target["durable_evidence_level"] != "none",
        "target.durable_evidence_level must be a recognized non-'none' level",
    )

    bounds = requirement["bounds"]
    _require(
        type(bounds) is dict and set(bounds) == _CALIBRATION_BOUNDS_FIELDS,
        "calibration bounds are invalid",
    )
    _positive_int(bounds["max_unit_count"], "bounds.max_unit_count")
    _positive_int(bounds["max_worker_count"], "bounds.max_worker_count")

    budget = requirement["budget"]
    _require(
        type(budget) is dict and set(budget) == _BUDGET_FIELDS,
        "calibration budget is invalid",
    )
    _require(
        _number(budget["headroom_factor"], "headroom_factor") >= 1.0,
        "headroom_factor must be at least 1.0",
    )
    for name in (
        "execution_budget_seconds",
        "hard_fail_safe_seconds",
        "normal_deadline_seconds",
    ):
        _positive_number(budget[name], f"budget.{name}")
    for name in ("post_processing_seconds", "setup_seconds", "teardown_seconds"):
        _non_negative_number(budget[name], f"budget.{name}")
    _non_negative_number(budget["cost_budget_usd"], "budget.cost_budget_usd")
    _non_negative_number(
        requirement["calibration_cost_budget_usd"], "calibration_cost_budget_usd"
    )

    pricing = requirement["pricing"]
    _require(type(pricing) is dict, "pricing must be an object")
    _require(
        set(pricing) == {"checked_at", "instance_hourly_rate_usd", "region", "source"},
        "pricing provenance fields are invalid",
    )
    _text(pricing["source"], "pricing.source")
    _text(pricing["region"], "pricing.region")
    _parse_timestamp(pricing["checked_at"], "pricing.checked_at")
    if pricing["instance_hourly_rate_usd"] is not None:
        _non_negative_number(
            pricing["instance_hourly_rate_usd"], "instance_hourly_rate_usd"
        )

    charges = requirement["charges"]
    _require(type(charges) is list, "charges must be a list")
    requirement["charges"] = [
        _validate_charge(charge, index) for index, charge in enumerate(charges)
    ]

    gates = requirement["gates"]
    _require(
        type(gates) is dict and set(gates) == set(_CALIBRATION_REQUIREMENT_GATE_NAMES),
        "calibration requirement gates are invalid",
    )
    for name in _CALIBRATION_REQUIREMENT_GATE_NAMES:
        entry = gates[name]
        _require(type(entry) is dict, f"gate {name} must be an object")
        _require(
            entry.get("status") in ("PASS", "FAIL"),
            f"gate {name} status must be PASS or FAIL",
        )
        _text(entry.get("detail"), f"gate {name} detail")

    _validate_binding_shape(requirement["allocation_binding"], "allocation_binding")
    seeds = requirement["seeds"]
    _require(type(seeds) is list and bool(seeds), "calibration seeds are required")
    for value in seeds:
        _require(
            type(value) is int and 0 <= value < 2**32,
            "calibration seeds must be unsigned 32-bit integers",
        )
    _require(len(set(seeds)) == len(seeds), "calibration seeds contain a duplicate")
    return requirement


def build_calibration_admission(
    requirement: Mapping[str, object],
    *,
    now: datetime,
    ledger: object = None,
) -> dict[str, object]:
    """Admit the bounded calibration run itself.

    There is deliberately no matching-calibration gate here: requiring
    calibration evidence to run a calibration would be circular. The monetary
    bound is instead the worst case -- the whole independent hard fail-safe
    window at the current rate plus every declared charge -- so the run is
    self-authorizing within an explicitly approved calibration budget.
    """

    required = validate_calibration_admission_requirement(dict(requirement))
    target = required["target"]
    bounds = required["bounds"]
    budget = required["budget"]
    pricing = required["pricing"]
    charges = required["charges"]
    gates_input = required["gates"]
    evaluated_at = _aware(now, "now")
    seeds = [int(seed) for seed in required["seeds"]]

    authority_detail = "canonical seed-registry ledger was not supplied"
    authority_ok = False
    if ledger is not None:
        probe = {
            "seed_allocation": required["allocation_binding"],
            "tasks": [{"seed": seed} for seed in seeds],
        }
        try:
            resolved = resolve_calibration_allocation(probe, ledger)
        except seed_registry.SeedRegistryError as error:
            authority_detail = (
                "calibration allocation does not resolve against canonical "
                f"seed-registry authority: {error}"
            )
        else:
            authority_ok = True
            authority_detail = (
                f"calibration allocation {resolved['allocation_identity']} is "
                f"{resolved['state']} in domain {resolved['seed_domain']!r} for "
                f"population {resolved['population']!r} / protocol "
                f"{resolved['protocol']!r} with exactly {len(seeds)} seeds"
            )

    units = int(target["total_units"])
    workers = int(target["worker_count"])
    scope_ok = (
        units == len(seeds)
        and units <= int(bounds["max_unit_count"])
        and workers <= int(bounds["max_worker_count"])
        and workers <= units
    )
    hard_fail_safe = float(budget["hard_fail_safe_seconds"])
    teardown = float(budget["teardown_seconds"])
    unbounded = sorted(
        str(charge["label"])
        for charge in charges
        if charge["material"] and charge["usd"] is None and charge["max_usd"] is None
    )
    known = sum(float(charge["usd"]) for charge in charges if charge["usd"] is not None)
    bounded = sum(
        float(charge["max_usd"])
        for charge in charges
        if charge["usd"] is None and charge["max_usd"] is not None
    )
    rate = pricing["instance_hourly_rate_usd"]
    calibration_budget = float(required["calibration_cost_budget_usd"])
    if rate is None:
        cost_ok = False
        worst_case = None
        cost_detail = "instance hourly rate unavailable; pricing provenance incomplete"
        cost_prediction: dict[str, object] = {
            "available": False,
            "reason": "instance hourly rate unavailable",
            "bounded_charges_usd": _round(bounded),
            "charges": charges,
            "is_finalized_aws_invoice": False,
            "known_charges_usd": _round(known),
            "overhead_basis": None,
            "pricing": pricing,
            "unbounded_material_charges": unbounded,
        }
    elif unbounded:
        cost_ok = False
        worst_case = None
        cost_detail = (
            "unknown material charge is unbounded and is never treated as zero: "
            f"{unbounded!r}"
        )
        cost_prediction = {
            "available": False,
            "reason": cost_detail,
            "bounded_charges_usd": _round(bounded),
            "charges": charges,
            "is_finalized_aws_invoice": False,
            "known_charges_usd": _round(known),
            "overhead_basis": None,
            "pricing": pricing,
            "unbounded_material_charges": unbounded,
        }
    else:
        worst_case = (
            (hard_fail_safe + teardown) / 3600.0 * float(rate) + known + bounded
        )
        cost_ok = _fits(worst_case, calibration_budget)
        cost_detail = (
            f"worst case = whole hard fail-safe window {hard_fail_safe}s + teardown "
            f"{teardown}s at {rate} USD/h + known {_round(known)} USD + bounded "
            f"{_round(bounded)} USD = {_round(worst_case)} USD against the approved "
            f"calibration budget {calibration_budget} USD"
        )
        cost_prediction = {
            "available": True,
            "bounded_charges_usd": _round(bounded),
            "charges": charges,
            "is_finalized_aws_invoice": False,
            "known_charges_usd": _round(known),
            "overhead_basis": None,
            "predicted_ec2_billable_runtime_range_seconds": [
                0.0,
                _round(hard_fail_safe + teardown),
            ],
            "predicted_ec2_cost_range_usd": [
                0.0,
                _round((hard_fail_safe + teardown) / 3600.0 * float(rate)),
            ],
            "predicted_total_cost_range_usd": [
                _round(known),
                _round(worst_case),
            ],
            "pricing": pricing,
            "reason": None,
            "unbounded_material_charges": unbounded,
        }

    fail_safe_ok = (
        _fits(float(budget["normal_deadline_seconds"]) + teardown, hard_fail_safe)
        and hard_fail_safe > 0
    )
    gates = [
        _gate("calibration-allocation-authority", authority_ok, authority_detail),
        _gate(
            "calibration-scope-bounded",
            scope_ok,
            (
                f"{units} units over {len(seeds)} allocated seeds with {workers} "
                f"workers against caps {bounds['max_unit_count']} units / "
                f"{bounds['max_worker_count']} workers"
            ),
        ),
        _gate("calibration-cost-budget", cost_ok, cost_detail),
        _gate(
            "hard-fail-safe",
            fail_safe_ok,
            (
                f"independent calibration fail-safe {hard_fail_safe}s with normal "
                f"deadline {budget['normal_deadline_seconds']}s + teardown "
                f"{teardown}s"
            ),
        ),
        _gate(
            "durable-evidence-support",
            gates_input["durable_evidence"]["status"] == "PASS",
            str(gates_input["durable_evidence"]["detail"]),
        ),
        _gate(
            "artifact-destination-retention",
            gates_input["artifact_destination"]["status"] == "PASS",
            str(gates_input["artifact_destination"]["detail"]),
        ),
        _gate(
            "teardown-confirmation",
            gates_input["teardown_confirmation"]["status"] == "PASS",
            str(gates_input["teardown_confirmation"]["detail"]),
        ),
    ]
    _require(
        tuple(str(gate["name"]) for gate in gates) == PHASE_ZERO_GATE_NAMES,
        "calibration gate set is incomplete",
    )
    blocking = [
        f"{gate['name']}: {gate['detail']}"
        for gate in gates
        if gate["status"] == "FAIL"
    ]
    decision = "NO-GO" if blocking else "GO"
    document: dict[str, object] = {
        "schema_version": ADMISSION_SCHEMA_VERSION,
        "admission_identity": "",
        "admission_phase": 0,
        "billable_resource_creation_authorized": decision == "GO",
        "blocking_reasons": blocking,
        "budget": budget,
        "calibration": {
            "calibrated_at": None,
            "identity": None,
            "match_reports": [],
            "run_id": None,
            "supported_worker_counts": [],
        },
        "clock_origins": dict(CLOCK_ORIGINS),
        "consumer": required["consumer"],
        "cost_prediction": cost_prediction,
        "decision": decision,
        "evaluated_at": _timestamp(evaluated_at),
        "gates": gates,
        "limitations": [
            "a calibration run produces operational timing evidence only; it is "
            "never qualification, TRAIN, SELECT or OFFLINE-EVAL evidence",
            "a calibration GO authorizes this bounded measurement only; it does "
            "not authorize any scientific execution",
            "the cost bound is the worst-case fail-safe window, not a prediction: "
            "no prior calibration is required to run a calibration",
        ],
        "phase_one_admission_identity": None,
        "run_id": required["run_id"],
        "runtime_prediction": {
            "available": False,
            "reason": (
                "a calibration is the measurement; requiring a prior calibrated "
                "runtime here would be circular"
            ),
        },
        "scientific_submission_authorized": False,
        "target": target,
        "workload_submission_authorized": decision == "GO",
    }
    document["admission_identity"] = _digest(
        {key: value for key, value in document.items() if key != "admission_identity"}
    )
    return validate_admission_record(document)


def write_admission_record(path: str | Path, document: object) -> None:
    destination = Path(path)
    _require(not destination.exists(), "admission destination already exists")
    atomic_replace(
        destination, canonical_json_bytes(validate_admission_record(document))
    )


def read_admission_record(path: str | Path) -> dict[str, object]:
    source = Path(path)
    try:
        document = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise AwsOperationalCalibrationError(
            f"cannot read admission record {source}: {error}"
        ) from error
    return validate_admission_record(document)


def historical_incomplete_observation() -> dict[str, object]:
    """Return the #326 incomplete run as planning history, with its gaps intact.

    The 8h window is the hard fail-safe bound, not a completion time. Completed
    units and per-seed durations were never durably recorded, so they stay
    ``None`` here instead of being back-derived into a throughput or a p50/p90.
    This record is deliberately a different schema from calibration evidence,
    so it can never be selected as a matching calibration.
    """

    return {
        "schema_version": INCOMPLETE_OBSERVATION_SCHEMA_VERSION,
        "admissible_as_calibration": False,
        "arena_revision": "13317bd85bc2f80a575d487cae9dda1bebd7f5f0",
        "batch_scientific_wall_clock_seconds": None,
        "completed_units": None,
        "ec2_billable_runtime_seconds": None,
        "execution_status": "INCOMPLETE AT HARD FAIL-SAFE",
        "forbidden_derivations": [
            "96 hanchan completed within 8 hours",
            "12 hanchan per hour",
            "any per-seed p50/p90 or throughput for this run",
            "a scientific FAIL for the #322 F1/F2 qualification",
        ],
        "game_mode": "4p-red-half",
        "hard_fail_safe_seconds": 28800.0,
        "inadmissible_reasons": [
            "completed unit count is unknown; the 8h hard fail-safe window bounded "
            "the run and is not a completion time",
            "per-seed durations were never durably recorded, so p50/p90 and "
            "throughput cannot be derived",
            "the fail-safe clock origin differs from the workload start, so the "
            "observed window is not a scientific runtime",
            "t3.small / 2 workers does not match any current production shape",
        ],
        "instance_type": "t3.small",
        "lisjong_revision": "15799e5f0fe47f2e2b2c39060de804d99c51492d",
        "observed_worker_cpu_percent_approximate": 99.8,
        "p50_seconds_per_unit": None,
        "p90_seconds_per_unit": None,
        "provenance_reference": (
            "https://github.com/lisbun/lisjong-arena/issues/326#issuecomment-5762374300"
        ),
        "run_id": "20260921T061727Z-184f3b4c",
        "seed_population": "2000..2095",
        "target_units": 96,
        "task_durations_seconds": None,
        "teacher_identity": "targeted-honor-release-terminal-progression x4",
        "throughput_units_per_hour": None,
        "unavailable_fields": [
            "batch_scientific_wall_clock_seconds",
            "completed_units",
            "ec2_billable_runtime_seconds",
            "p50_seconds_per_unit",
            "p90_seconds_per_unit",
            "task_durations_seconds",
            "throughput_units_per_hour",
        ],
        "worker_count_requested": 2,
    }


def _read_json(path: str | Path, name: str) -> object:
    source = Path(path)
    try:
        return json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise AwsOperationalCalibrationError(
            f"cannot read {name} {source}: {error}"
        ) from error


def build_calibration_evidence_from_document(document: object) -> dict[str, object]:
    """Build evidence from an operator input document.

    ``tasks`` may be supplied directly, or ``durable_receipts`` may name a #339
    checkpoint directory plus the expected calibration seeds.
    """

    _require(type(document) is dict, "calibration input must be an object")
    assert_no_scientific_fields(document, context="calibration input")
    values = dict(document)
    receipts = values.pop("durable_receipts", None)
    if receipts is not None:
        _require(
            values.get("tasks") is None, "supply tasks or durable_receipts, not both"
        )
        _require(type(receipts) is dict, "durable_receipts must be an object")
        _require(
            set(receipts) == {"checkpoint_dir", "expected_seeds", "protocol_identity"},
            "durable_receipts fields are invalid",
        )
        values["tasks"] = tasks_from_durable_receipts(
            receipts["checkpoint_dir"],
            run_id=str(values.get("calibration_run_id")),
            protocol_identity=str(receipts["protocol_identity"]),
            expected_seeds=list(receipts["expected_seeds"]),
        )
    values["calibrated_at"] = _parse_timestamp(
        values.get("calibrated_at"), "calibrated_at"
    )
    return build_calibration_evidence(**values)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    evidence = commands.add_parser(
        "evidence", help="build dedicated operational calibration evidence"
    )
    evidence.add_argument("--input", required=True)
    evidence.add_argument("--output", required=True)
    validate = commands.add_parser(
        "validate-evidence", help="strict-read existing calibration evidence"
    )
    validate.add_argument("--calibration", required=True)
    calibration_gate = commands.add_parser(
        "admit-calibration",
        help="bounded calibration admission; requires no prior calibration",
    )
    calibration_gate.add_argument("--requirement", required=True)
    calibration_gate.add_argument("--seed-ledger", required=True)
    calibration_gate.add_argument("--output", required=True)
    calibration_gate.add_argument("--now")
    phase_one = commands.add_parser(
        "admit-phase-1", help="pre-billing admission gate; No-Go forbids creation"
    )
    phase_one.add_argument("--requirement", required=True)
    phase_one.add_argument("--calibration", action="append", default=[])
    phase_one.add_argument("--seed-ledger", required=True)
    phase_one.add_argument("--output", required=True)
    phase_one.add_argument("--now")
    phase_two = commands.add_parser(
        "admit-phase-2", help="pre-submission admission gate after provisioning"
    )
    phase_two.add_argument("--admission", required=True)
    phase_two.add_argument("--observation", required=True)
    phase_two.add_argument("--output", required=True)
    phase_two.add_argument("--now")
    historical = commands.add_parser(
        "historical-observation",
        help="emit the #326 incomplete observation with its gaps preserved",
    )
    historical.add_argument("--output")
    return parser


def _now(value: object) -> datetime:
    if value is None:
        return datetime.now(UTC)
    return _parse_timestamp(value, "now")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "evidence":
            document = build_calibration_evidence_from_document(
                _read_json(args.input, "calibration input")
            )
            write_calibration_evidence(args.output, document)
            print(
                json.dumps(
                    {
                        "calibration_identity": document["calibration_identity"],
                        "p50_seconds_per_unit": document["p50_seconds_per_unit"],
                        "p90_seconds_per_unit": document["p90_seconds_per_unit"],
                        "peak_observed_concurrency": document[
                            "peak_observed_concurrency"
                        ],
                        "task_count": document["task_count"],
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "validate-evidence":
            document = read_calibration_evidence(args.calibration)
            print(
                json.dumps({"calibration_identity": document["calibration_identity"]})
            )
            return 0
        if args.command == "historical-observation":
            document = historical_incomplete_observation()
            if args.output:
                destination = Path(args.output)
                _require(
                    not destination.exists(), "observation destination already exists"
                )
                atomic_replace(destination, canonical_json_bytes(document))
            print(json.dumps(document, sort_keys=True, separators=(",", ":")))
            return 0
        if args.command == "admit-calibration":
            record = build_calibration_admission(
                _read_json(args.requirement, "calibration requirement"),
                now=_now(args.now),
                ledger=seed_registry.load_ledger(args.seed_ledger),
            )
        elif args.command == "admit-phase-1":
            record = build_phase_one_admission(
                _read_json(args.requirement, "admission requirement"),
                [read_calibration_evidence(path) for path in args.calibration],
                now=_now(args.now),
                ledger=seed_registry.load_ledger(args.seed_ledger),
            )
        else:
            record = build_phase_two_admission(
                read_admission_record(args.admission),
                _read_json(args.observation, "phase 2 observation"),
                now=_now(args.now),
            )
        write_admission_record(args.output, record)
        print(
            json.dumps(
                {
                    "admission_identity": record["admission_identity"],
                    "admission_phase": record["admission_phase"],
                    "billable_resource_creation_authorized": record[
                        "billable_resource_creation_authorized"
                    ],
                    "blocking_reasons": record["blocking_reasons"],
                    "decision": record["decision"],
                    "scientific_submission_authorized": record[
                        "scientific_submission_authorized"
                    ],
                    "workload_submission_authorized": record[
                        "workload_submission_authorized"
                    ],
                },
                sort_keys=True,
            )
        )
        return 0 if record["decision"] == "GO" else 3
    except Exception as error:  # noqa: BLE001 - operator-facing fail-closed CLI
        print(f"STOP / INVALID: {type(error).__name__}: {error}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ADMISSION_SCHEMA_VERSION",
    "AwsOperationalCalibrationError",
    "BURSTABLE_INSTANCE_FAMILIES",
    "CALIBRATION_POPULATION",
    "CALIBRATION_PROTOCOL",
    "CALIBRATION_REQUIREMENT_SCHEMA_VERSION",
    "DEFAULT_HEADROOM_FACTOR",
    "DURABLE_EVIDENCE_LEVELS",
    "EVIDENCE_SCHEMA_VERSION",
    "INCOMPLETE_OBSERVATION_SCHEMA_VERSION",
    "OBSERVATION_SCHEMA_VERSION",
    "PHASE_ONE_GATE_NAMES",
    "PHASE_TWO_GATE_NAMES",
    "PHASE_ZERO_GATE_NAMES",
    "REQUIREMENT_SCHEMA_VERSION",
    "assert_no_scientific_fields",
    "build_calibration_admission",
    "build_calibration_evidence",
    "build_calibration_evidence_from_document",
    "build_phase_one_admission",
    "build_phase_two_admission",
    "evaluate_budget",
    "evaluate_calibration_match",
    "calibration_seeds",
    "historical_incomplete_observation",
    "instance_family",
    "measured_billable_overhead",
    "nearest_rank_percentile",
    "predict_cost",
    "predict_runtime",
    "read_admission_record",
    "read_calibration_evidence",
    "resolve_calibration_allocation",
    "select_matching_calibration",
    "tasks_from_durable_receipts",
    "validate_admission_record",
    "validate_admission_requirement",
    "validate_calibration_admission_requirement",
    "validate_calibration_evidence",
    "validate_phase_two_observation",
    "write_admission_record",
    "write_calibration_evidence",
]
