"""AWS evaluation runs向けのpure operational observability contract。

このmoduleはAWS APIを呼ばず、scientific resultも扱わない。進捗、runtime / cost
planning、post-run calibrationというoperational metadataだけを構築・検証する。
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

from lisjong_arena.riichilab_corpus.models import canonical_json_bytes
from lisjong_arena.riichilab_corpus.persistence import atomic_replace

PROGRESS_SCHEMA_VERSION: Final = "arena-aws-progress-v1"
PLAN_SCHEMA_VERSION: Final = "arena-aws-execution-plan-v1"
CALIBRATION_SCHEMA_VERSION: Final = "arena-aws-calibration-v1"

_PROGRESS_FIELDS: Final = {
    "schema_version",
    "run_id",
    "unit_kind",
    "started_at",
    "updated_at",
    "completed_units",
    "total_units",
    "elapsed_seconds",
    "throughput_per_hour",
    "eta_status",
    "eta_seconds",
    "estimated_finish_at",
    "worker_count",
}
_SCIENTIFIC_FIELDS: Final = {
    "score",
    "scores",
    "rank",
    "ranks",
    "outcome",
    "classification",
    "model_metrics",
    "metrics",
    "result",
    "results",
    "scientific_result",
    "scientific_results",
}


class AwsExecutionObservabilityError(ValueError):
    """Operational observability contract violation."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise AwsExecutionObservabilityError(message)


def _aware_utc(value: datetime, name: str) -> datetime:
    _require(value.tzinfo is not None, f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    return (
        _aware_utc(value, "timestamp")
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _parse_timestamp(value: object, name: str) -> datetime:
    _require(type(value) is str and bool(value), f"{name} must be a timestamp")
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise AwsExecutionObservabilityError(f"{name} must be a timestamp") from exc
    return _aware_utc(parsed, name)


def _positive_number(value: object, name: str) -> float:
    _require(
        type(value) in (int, float)
        and math.isfinite(float(value))
        and float(value) > 0,
        f"{name} must be a positive finite number",
    )
    return float(value)


def _non_negative_number(value: object, name: str) -> float:
    _require(
        type(value) in (int, float)
        and math.isfinite(float(value))
        and float(value) >= 0,
        f"{name} must be a non-negative finite number",
    )
    return float(value)


def calculate_compute_cost(runtime_seconds: float, hourly_rate_usd: float) -> float:
    """Return an estimate, not an AWS invoice amount."""

    runtime = _non_negative_number(runtime_seconds, "runtime_seconds")
    rate = _non_negative_number(hourly_rate_usd, "hourly_rate_usd")
    return round(runtime / 3600.0 * rate, 6)


def validate_progress_document(document: object) -> dict[str, object]:
    _require(type(document) is dict, "progress must be an object")
    progress = document
    _require(set(progress) == _PROGRESS_FIELDS, "progress fields are invalid")
    _require(
        not (_SCIENTIFIC_FIELDS & set(progress)),
        "scientific fields are forbidden in progress",
    )
    _require(
        progress["schema_version"] == PROGRESS_SCHEMA_VERSION,
        "progress schema version is invalid",
    )
    for name in ("run_id", "unit_kind"):
        _require(
            type(progress[name]) is str and bool(str(progress[name]).strip()),
            f"{name} must be a non-empty string",
        )
    started = _parse_timestamp(progress["started_at"], "started_at")
    updated = _parse_timestamp(progress["updated_at"], "updated_at")
    _require(updated >= started, "updated_at precedes started_at")
    completed = progress["completed_units"]
    total = progress["total_units"]
    workers = progress["worker_count"]
    _require(type(completed) is int, "completed_units must be an int")
    _require(type(total) is int and total > 0, "total_units must be positive")
    _require(0 <= completed <= total, "completed_units must be within total_units")
    _require(type(workers) is int and workers > 0, "worker_count must be positive")
    elapsed = _non_negative_number(progress["elapsed_seconds"], "elapsed_seconds")
    expected_elapsed = (updated - started).total_seconds()
    _require(abs(elapsed - expected_elapsed) <= 1.0, "elapsed_seconds is inconsistent")
    throughput = progress["throughput_per_hour"]
    if throughput is not None:
        _positive_number(throughput, "throughput_per_hour")

    eta_status = progress["eta_status"]
    _require(
        eta_status in ("warming-up", "available", "complete"),
        "eta_status is invalid",
    )
    if eta_status == "warming-up":
        _require(
            progress["eta_seconds"] is None and progress["estimated_finish_at"] is None,
            "warming-up ETA values must be unavailable",
        )
        _require(completed < total, "completed progress cannot be warming-up")
    elif eta_status == "available":
        eta = _non_negative_number(progress["eta_seconds"], "eta_seconds")
        finish = _parse_timestamp(
            progress["estimated_finish_at"], "estimated_finish_at"
        )
        _require(completed < total, "incomplete progress must use available ETA")
        _require(
            abs((finish - updated).total_seconds() - eta) <= 1.0,
            "estimated_finish_at is inconsistent",
        )
    else:
        _require(completed == total, "complete ETA requires all units")
        _require(progress["eta_seconds"] == 0, "complete ETA must be zero")
        _require(
            _parse_timestamp(progress["estimated_finish_at"], "estimated_finish_at")
            == updated,
            "complete estimated_finish_at must equal updated_at",
        )
    return progress


def validate_progress_transition(
    previous: object, current: object
) -> dict[str, object]:
    before = validate_progress_document(previous)
    after = validate_progress_document(current)
    for immutable in (
        "run_id",
        "unit_kind",
        "started_at",
        "total_units",
        "worker_count",
    ):
        _require(before[immutable] == after[immutable], f"{immutable} is immutable")
    _require(
        int(after["completed_units"]) >= int(before["completed_units"]),
        "completed_units cannot decrease",
    )
    _require(
        _parse_timestamp(after["updated_at"], "updated_at")
        >= _parse_timestamp(before["updated_at"], "updated_at"),
        "updated_at cannot decrease",
    )
    return after


@dataclass(slots=True)
class ProgressTracker:
    run_id: str
    unit_kind: str
    total_units: int
    worker_count: int
    started_at: datetime
    minimum_completed_for_eta: int = 2
    _last: dict[str, object] | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self.started_at = _aware_utc(self.started_at, "started_at")
        _require(bool(self.run_id.strip()), "run_id must be non-empty")
        _require(bool(self.unit_kind.strip()), "unit_kind must be non-empty")
        _require(
            type(self.total_units) is int and self.total_units > 0, "invalid total"
        )
        _require(
            type(self.worker_count) is int and self.worker_count > 0,
            "invalid worker_count",
        )
        _require(
            type(self.minimum_completed_for_eta) is int
            and self.minimum_completed_for_eta >= 2,
            "minimum_completed_for_eta must be at least 2",
        )

    def snapshot(self, completed_units: int, *, now: datetime) -> dict[str, object]:
        updated = _aware_utc(now, "now")
        elapsed = max(0.0, (updated - self.started_at).total_seconds())
        throughput = None
        if completed_units > 0 and elapsed > 0:
            throughput = round(completed_units * 3600.0 / elapsed, 3)

        eta_status = "warming-up"
        eta_seconds: int | None = None
        estimated_finish: str | None = None
        if completed_units == self.total_units:
            eta_status = "complete"
            eta_seconds = 0
            estimated_finish = _timestamp(updated)
        elif completed_units >= self.minimum_completed_for_eta and throughput:
            eta_status = "available"
            eta_seconds = round(
                (self.total_units - completed_units) * 3600 / throughput
            )
            estimated_finish = _timestamp(updated + timedelta(seconds=eta_seconds))

        document: dict[str, object] = {
            "schema_version": PROGRESS_SCHEMA_VERSION,
            "run_id": self.run_id,
            "unit_kind": self.unit_kind,
            "started_at": _timestamp(self.started_at),
            "updated_at": _timestamp(updated),
            "completed_units": completed_units,
            "total_units": self.total_units,
            "elapsed_seconds": round(elapsed, 1),
            "throughput_per_hour": throughput,
            "eta_status": eta_status,
            "eta_seconds": eta_seconds,
            "estimated_finish_at": estimated_finish,
            "worker_count": self.worker_count,
        }
        if self._last is None:
            validate_progress_document(document)
        else:
            validate_progress_transition(self._last, document)
        self._last = document
        return document


def write_progress(path: str | Path, document: object) -> None:
    destination = Path(path)
    progress = validate_progress_document(document)
    if destination.exists():
        try:
            previous = json.loads(destination.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AwsExecutionObservabilityError(
                "existing progress document is unreadable"
            ) from exc
        validate_progress_transition(previous, progress)
    atomic_replace(destination, canonical_json_bytes(progress))


def pricing_provenance(
    *,
    source: str,
    checked_at: datetime,
    region: str,
    instance_hourly_rate_usd: float | None,
) -> dict[str, object]:
    _require(bool(source.strip()), "pricing source must be non-empty")
    _require(bool(region.strip()), "pricing region must be non-empty")
    return {
        "source": source,
        "checked_at": _timestamp(checked_at),
        "region": region,
        "instance_hourly_rate_usd": (
            None
            if instance_hourly_rate_usd is None
            else _non_negative_number(
                instance_hourly_rate_usd, "instance_hourly_rate_usd"
            )
        ),
    }


def build_execution_plan(
    *,
    run_id: str,
    unit_kind: str,
    total_units: int,
    instance_type: str,
    vcpu: int,
    memory_mib: int,
    worker_count: int,
    fail_safe_seconds: float,
    pricing: dict[str, object] | None,
    predicted_runtime_seconds: tuple[float, float] | None,
    estimate_basis: str | None,
    retained_ebs_estimate_usd: float | None,
    known_other_charges: tuple[str, ...] = (),
    unknown_variable_charges: tuple[str, ...] = (),
) -> dict[str, object]:
    _require(
        bool(run_id.strip()) and bool(unit_kind.strip()), "run identity is invalid"
    )
    _require(type(total_units) is int and total_units > 0, "total_units is invalid")
    _require(type(vcpu) is int and vcpu > 0, "vcpu is invalid")
    _require(type(memory_mib) is int and memory_mib > 0, "memory_mib is invalid")
    _require(type(worker_count) is int and worker_count > 0, "worker_count is invalid")
    fail_safe = _positive_number(fail_safe_seconds, "fail_safe_seconds")

    runtime: dict[str, object]
    predicted_cost: dict[str, object] | None = None
    if predicted_runtime_seconds is None:
        _require(not estimate_basis, "runtime basis requires a numeric range")
        runtime = {
            "confidence": "LOW",
            "range_seconds": None,
            "basis": None,
            "reason": "no matching historical evidence",
        }
    else:
        low = _positive_number(predicted_runtime_seconds[0], "runtime lower bound")
        high = _positive_number(predicted_runtime_seconds[1], "runtime upper bound")
        _require(low <= high, "runtime range is reversed")
        _require(
            bool(estimate_basis and estimate_basis.strip()), "runtime basis is required"
        )
        runtime = {
            "confidence": "CALIBRATED",
            "range_seconds": [low, high],
            "basis": estimate_basis,
            "reason": None,
        }
        if pricing is not None and pricing.get("instance_hourly_rate_usd") is not None:
            rate = _non_negative_number(
                pricing.get("instance_hourly_rate_usd"), "instance hourly rate"
            )
            predicted_cost = {
                "kind": "predicted cost",
                "range_usd": [
                    calculate_compute_cost(low, rate),
                    calculate_compute_cost(high, rate),
                ],
            }

    fail_safe_exposure = None
    if pricing is not None and pricing.get("instance_hourly_rate_usd") is not None:
        rate = _non_negative_number(
            pricing.get("instance_hourly_rate_usd"), "instance hourly rate"
        )
        fail_safe_exposure = {
            "label": "estimated fail-safe cost exposure",
            "ec2_compute_usd": calculate_compute_cost(fail_safe, rate),
            "does_not_include_unknown_variable_charges": True,
        }

    warnings = []
    if worker_count > vcpu:
        warnings.append("worker_count exceeds vCPU count")
    return {
        "schema_version": PLAN_SCHEMA_VERSION,
        "run_id": run_id,
        "unit_kind": unit_kind,
        "total_units": total_units,
        "instance": {
            "type": instance_type,
            "vcpu": vcpu,
            "memory_mib": memory_mib,
        },
        "worker_count": worker_count,
        "runtime_estimate": runtime,
        "pricing": pricing,
        "predicted_ec2_cost": predicted_cost,
        "retained_ebs_estimate_usd": (
            None
            if retained_ebs_estimate_usd is None
            else _non_negative_number(
                retained_ebs_estimate_usd, "retained_ebs_estimate_usd"
            )
        ),
        "known_other_charges": list(known_other_charges),
        "estimated_fail_safe_cost_exposure": fail_safe_exposure,
        "unknown_variable_charges": list(unknown_variable_charges),
        "warnings": warnings,
    }


def _range_error(actual: float, predicted: tuple[float, float] | None) -> float | None:
    if predicted is None:
        return None
    low, high = predicted
    if actual < low:
        return round(actual - low, 6)
    if actual > high:
        return round(actual - high, 6)
    return 0.0


def build_calibration(
    *,
    run_id: str,
    unit_kind: str,
    completed_units: int,
    scientific_runtime_seconds: float,
    ec2_billable_runtime_seconds: float,
    predicted_runtime_seconds: tuple[float, float] | None,
    instance_type: str,
    vcpu: int,
    worker_count: int,
    pricing: dict[str, object],
) -> dict[str, object]:
    scientific_runtime = _positive_number(
        scientific_runtime_seconds, "scientific_runtime_seconds"
    )
    billable_runtime = _positive_number(
        ec2_billable_runtime_seconds, "ec2_billable_runtime_seconds"
    )
    _require(
        billable_runtime >= scientific_runtime,
        "EC2 billable runtime cannot be shorter than scientific runtime",
    )
    _require(
        type(completed_units) is int and completed_units > 0, "invalid completed_units"
    )
    rate = _non_negative_number(
        pricing.get("instance_hourly_rate_usd"), "instance hourly rate"
    )
    actual_cost = calculate_compute_cost(billable_runtime, rate)
    predicted_cost_range = None
    if predicted_runtime_seconds is not None:
        predicted_cost_range = (
            calculate_compute_cost(predicted_runtime_seconds[0], rate),
            calculate_compute_cost(predicted_runtime_seconds[1], rate),
        )
    return {
        "schema_version": CALIBRATION_SCHEMA_VERSION,
        "run_id": run_id,
        "unit_kind": unit_kind,
        "completed_units": completed_units,
        "predicted_runtime_range_seconds": (
            None
            if predicted_runtime_seconds is None
            else list(predicted_runtime_seconds)
        ),
        "scientific_runtime_seconds": scientific_runtime,
        "ec2_billable_runtime_seconds": billable_runtime,
        "actual_throughput_per_hour": round(
            completed_units * 3600 / scientific_runtime, 3
        ),
        "predicted_cost_range_usd": (
            None if predicted_cost_range is None else list(predicted_cost_range)
        ),
        "estimated_realized_cost": {
            "kind": "estimated realized cost",
            "ec2_compute_usd": actual_cost,
            "is_finalized_aws_invoice": False,
        },
        "instance_type": instance_type,
        "vcpu": vcpu,
        "worker_count": worker_count,
        "pricing": pricing,
        "runtime_prediction_error_seconds": _range_error(
            scientific_runtime, predicted_runtime_seconds
        ),
        "cost_prediction_error_usd": _range_error(actual_cost, predicted_cost_range),
    }


def write_calibration(path: str | Path, document: dict[str, object]) -> None:
    destination = Path(path)
    _require(not destination.exists(), "calibration destination already exists")
    atomic_replace(destination, canonical_json_bytes(document))


def write_plan(path: str | Path, document: dict[str, object]) -> None:
    destination = Path(path)
    _require(not destination.exists(), "plan destination already exists")
    atomic_replace(destination, canonical_json_bytes(document))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    plan = subparsers.add_parser("plan")
    plan.add_argument("--output-path", required=True)
    plan.add_argument("--run-id", required=True)
    plan.add_argument("--unit-kind", required=True)
    plan.add_argument("--total-units", type=int, required=True)
    plan.add_argument("--instance-type", required=True)
    plan.add_argument("--vcpu", type=int, required=True)
    plan.add_argument("--memory-mib", type=int, required=True)
    plan.add_argument("--worker-count", type=int, required=True)
    plan.add_argument("--fail-safe-seconds", type=float, required=True)
    plan.add_argument("--pricing-source")
    plan.add_argument("--pricing-checked-at")
    plan.add_argument("--pricing-region")
    plan.add_argument("--instance-hourly-rate-usd", type=float)
    plan.add_argument("--predicted-runtime-min-seconds", type=float)
    plan.add_argument("--predicted-runtime-max-seconds", type=float)
    plan.add_argument("--estimate-basis")
    plan.add_argument("--retained-ebs-estimate-usd", type=float)
    plan.add_argument("--known-other-charge", action="append", default=[])
    plan.add_argument("--unknown-variable-charge", action="append", default=[])
    calibration = subparsers.add_parser("calibration")
    calibration.add_argument("--output-path", required=True)
    calibration.add_argument("--run-id", required=True)
    calibration.add_argument("--unit-kind", required=True)
    calibration.add_argument("--completed-units", type=int, required=True)
    calibration.add_argument("--scientific-runtime-seconds", type=float, required=True)
    calibration.add_argument(
        "--ec2-billable-runtime-seconds", type=float, required=True
    )
    calibration.add_argument("--predicted-runtime-min-seconds", type=float)
    calibration.add_argument("--predicted-runtime-max-seconds", type=float)
    calibration.add_argument("--instance-type", required=True)
    calibration.add_argument("--vcpu", type=int, required=True)
    calibration.add_argument("--worker-count", type=int, required=True)
    calibration.add_argument("--pricing-source", required=True)
    calibration.add_argument("--pricing-checked-at", required=True)
    calibration.add_argument("--pricing-region", required=True)
    calibration.add_argument("--instance-hourly-rate-usd", type=float, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    predicted = None
    if (
        args.predicted_runtime_min_seconds is not None
        or args.predicted_runtime_max_seconds is not None
    ):
        _require(
            args.predicted_runtime_min_seconds is not None
            and args.predicted_runtime_max_seconds is not None,
            "both predicted runtime bounds are required",
        )
        predicted = (
            args.predicted_runtime_min_seconds,
            args.predicted_runtime_max_seconds,
        )
    if args.command == "plan":
        pricing_values = (
            args.pricing_source,
            args.pricing_checked_at,
            args.pricing_region,
        )
        pricing = None
        if any(value is not None for value in pricing_values):
            _require(
                all(value is not None for value in pricing_values),
                "complete pricing provenance is required",
            )
            pricing = pricing_provenance(
                source=args.pricing_source,
                checked_at=_parse_timestamp(
                    args.pricing_checked_at, "pricing_checked_at"
                ),
                region=args.pricing_region,
                instance_hourly_rate_usd=args.instance_hourly_rate_usd,
            )
        document = build_execution_plan(
            run_id=args.run_id,
            unit_kind=args.unit_kind,
            total_units=args.total_units,
            instance_type=args.instance_type,
            vcpu=args.vcpu,
            memory_mib=args.memory_mib,
            worker_count=args.worker_count,
            fail_safe_seconds=args.fail_safe_seconds,
            pricing=pricing,
            predicted_runtime_seconds=predicted,
            estimate_basis=args.estimate_basis,
            retained_ebs_estimate_usd=args.retained_ebs_estimate_usd,
            known_other_charges=tuple(args.known_other_charge),
            unknown_variable_charges=tuple(args.unknown_variable_charge),
        )
        write_plan(args.output_path, document)
        print(json.dumps(document, sort_keys=True, separators=(",", ":")))
        return 0
    checked_at = _parse_timestamp(args.pricing_checked_at, "pricing_checked_at")
    pricing = pricing_provenance(
        source=args.pricing_source,
        checked_at=checked_at,
        region=args.pricing_region,
        instance_hourly_rate_usd=args.instance_hourly_rate_usd,
    )
    document = build_calibration(
        run_id=args.run_id,
        unit_kind=args.unit_kind,
        completed_units=args.completed_units,
        scientific_runtime_seconds=args.scientific_runtime_seconds,
        ec2_billable_runtime_seconds=args.ec2_billable_runtime_seconds,
        predicted_runtime_seconds=predicted,
        instance_type=args.instance_type,
        vcpu=args.vcpu,
        worker_count=args.worker_count,
        pricing=pricing,
    )
    write_calibration(args.output_path, document)
    print(json.dumps(document, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "AwsExecutionObservabilityError",
    "CALIBRATION_SCHEMA_VERSION",
    "PLAN_SCHEMA_VERSION",
    "PROGRESS_SCHEMA_VERSION",
    "ProgressTracker",
    "build_calibration",
    "build_execution_plan",
    "calculate_compute_cost",
    "pricing_provenance",
    "validate_progress_document",
    "validate_progress_transition",
    "write_calibration",
    "write_plan",
    "write_progress",
]
