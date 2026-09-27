"""Render one AWS operational snapshot as a self-contained, read-only HTML view."""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import UTC, datetime
from html import escape
from pathlib import Path

from lisjong_arena.aws_execution_observability import (
    CALIBRATION_SCHEMA_VERSION,
    PLAN_SCHEMA_VERSION,
    AwsExecutionObservabilityError,
    validate_progress_document,
)
from lisjong_arena.riichilab_corpus.persistence import atomic_replace


def _require(condition: bool, name: str) -> None:
    if not condition:
        raise AwsExecutionObservabilityError(f"Invalid dashboard field: {name}")


def _object(value: object, name: str) -> dict:
    _require(type(value) is dict, name)
    return value


def _text(value: object, name: str) -> str:
    _require(type(value) is str and bool(value.strip()), name)
    return value


def _number(value: object, name: str, *, signed: bool = False) -> object:
    if value is not None:
        _require(
            type(value) in (int, float)
            and math.isfinite(value)
            and (signed or value >= 0),
            name,
        )
    return value


def _integer(value: object, name: str) -> int:
    _require(type(value) is int and value > 0, name)
    return value


def _range(value: object, name: str) -> object:
    if value is not None:
        _require(type(value) is list and len(value) == 2, name)
        for bound in value:
            _require(bound is not None, name)
            _number(bound, name)
        _require(value[0] <= value[1], name)
    return value


def _timestamp(value: object, name: str) -> str:
    _text(value, name)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise AwsExecutionObservabilityError(f"Invalid timestamp: {name}") from exc
    _require(parsed.tzinfo is not None, name)
    return parsed.astimezone(UTC).isoformat(timespec="seconds")


def _nested(document: dict, name: str) -> dict:
    value = document.get(name)
    return {} if value is None else _object(value, name)


def _display(value: object) -> str:
    if value is None:
        return "unavailable"
    if type(value) is list:
        return " – ".join(_display(item) for item in value)
    if type(value) is float:
        return f"{value:,.6f}".rstrip("0").rstrip(".")
    return escape(str(value), quote=True)


def _section(title: str, rows: list[tuple[str, object]]) -> str:
    cells = "".join(
        f"<div><dt>{escape(label)}</dt><dd>{_display(value)}</dd></div>"
        for label, value in rows
    )
    return f"<section><h2>{escape(title)}</h2><dl>{cells}</dl></section>"


def _pricing_rows(document: dict) -> list[tuple[str, object]]:
    pricing = _nested(document, "pricing")
    rows = []
    for field in ("source", "region", "checked_at"):
        value = pricing.get(field)
        if value is not None:
            value = (
                _timestamp(value, field)
                if field == "checked_at"
                else _text(value, field)
            )
        rows.append((f"Pricing {field}", value))
    rows.append(
        ("Instance USD/hour", _number(pricing.get("instance_hourly_rate_usd"), "rate"))
    )
    return rows


def render_dashboard(
    plan: object,
    progress: object = None,
    calibration: object = None,
    *,
    generated_at: datetime | None = None,
) -> str:
    """Validate display fields and shared identity before rendering an allowlist.

    Unknown plan/calibration fields are never serialized, including in hidden
    HTML or script data. Progress uses the existing exact-field validator.
    """
    plan = _object(plan, "plan")
    _require(plan.get("schema_version") == PLAN_SCHEMA_VERSION, "plan schema")
    for key in ("run_id", "unit_kind"):
        _text(plan.get(key), key)
    for key in ("total_units", "worker_count"):
        _integer(plan.get(key), key)
    instance = _object(plan.get("instance"), "instance")
    _text(instance.get("type"), "instance type")
    for key in ("vcpu", "memory_mib"):
        _integer(instance.get(key), key)

    if progress is not None:
        progress = validate_progress_document(progress)
        for key in ("run_id", "unit_kind", "total_units", "worker_count"):
            _require(progress[key] == plan[key], f"progress {key} mismatch")
    if calibration is not None:
        calibration = _object(calibration, "calibration")
        _require(
            calibration.get("schema_version") == CALIBRATION_SCHEMA_VERSION,
            "calibration schema",
        )
        for key in ("run_id", "unit_kind", "worker_count"):
            _require(calibration.get(key) == plan[key], f"calibration {key} mismatch")
        _integer(calibration.get("worker_count"), "calibration worker_count")
        _integer(calibration.get("vcpu"), "calibration vcpu")
        _require(calibration.get("vcpu") == instance["vcpu"], "vcpu mismatch")
        _require(
            calibration.get("instance_type") == instance["type"],
            "instance type mismatch",
        )
        completed = _integer(calibration.get("completed_units"), "completed_units")
        _require(completed <= plan["total_units"], "calibration completed_units")
        # A retained progress snapshot can legitimately predate final calibration.
        if progress is not None:
            _require(
                completed >= progress["completed_units"], "completed_units mismatch"
            )
        if "total_units" in calibration:
            _require(
                calibration["total_units"] == plan["total_units"], "total mismatch"
            )
        scientific = _number(calibration.get("scientific_runtime_seconds"), "runtime")
        billable = _number(calibration.get("ec2_billable_runtime_seconds"), "billable")
        if scientific is not None and billable is not None:
            _require(billable >= scientific, "billable runtime shorter than workload")

    generated = generated_at or datetime.now(UTC)
    generated_text = _timestamp(generated.isoformat(), "generated_at")
    p = progress or {}
    c = calibration or {}
    progress_rows = [
        (
            "Completed / total (snapshot)",
            None
            if progress is None
            else f"{p['completed_units']} / {p['total_units']}",
        ),
        (
            "Progress % (snapshot)",
            None
            if progress is None
            else round(100 * p["completed_units"] / p["total_units"], 1),
        ),
        ("Workload elapsed seconds", p.get("elapsed_seconds")),
        ("Throughput units/hour", p.get("throughput_per_hour")),
        ("ETA status", p.get("eta_status")),
        ("ETA seconds (at snapshot)", p.get("eta_seconds")),
        ("Estimated finish", p.get("estimated_finish_at")),
        ("Snapshot timestamp", p.get("updated_at")),
        ("Dashboard generated timestamp", generated_text),
        # v1 has no timer-arm timestamp. Workload start is NOT EC2 launch/arm time.
        ("Fail-safe deadline", None),
        ("Fail-safe remaining seconds", None),
    ]
    cost_rows = [
        (
            "Predicted EC2 cost range USD",
            _range(
                _nested(plan, "predicted_ec2_cost").get("range_usd"), "predicted cost"
            ),
        ),
        (
            "Estimated fail-safe EC2 exposure USD",
            _number(
                _nested(plan, "estimated_fail_safe_cost_exposure").get(
                    "ec2_compute_usd"
                ),
                "exposure",
            ),
        ),
        (
            "Retained EBS estimate USD",
            _number(plan.get("retained_ebs_estimate_usd"), "EBS"),
        ),
    ]
    for key, label in (
        ("known_other_charges", "Known other charges"),
        ("unknown_variable_charges", "Unknown variable charges"),
        ("warnings", "Plan warnings"),
    ):
        values = plan.get(key)
        if values is not None:
            _require(type(values) is list, key)
            for value in values:
                _text(value, key)
        cost_rows.append(
            (label, None if values is None else "; ".join(values) or "none listed")
        )
    cost_rows.extend(_pricing_rows(plan))

    calibration_rows = [("Completed units (calibration)", c.get("completed_units"))]
    for kind, label in (
        ("scientific", "Workload (scientific)"),
        ("ec2_billable", "EC2 billable"),
    ):
        prediction = _range(
            _nested(plan, f"{kind}_runtime_estimate").get("range_seconds"), kind
        )
        calibrated_prediction = _range(
            c.get(f"predicted_{kind}_runtime_range_seconds"), kind
        )
        if prediction is not None and calibrated_prediction is not None:
            _require(prediction == calibrated_prediction, f"{kind} prediction mismatch")
        calibration_rows.extend(
            [
                (f"{label} predicted seconds (plan)", prediction),
                (f"{label} predicted seconds (calibration)", calibrated_prediction),
                (
                    f"{label} actual seconds",
                    _number(c.get(f"{kind}_runtime_seconds"), kind),
                ),
                (
                    f"{label} prediction error seconds",
                    _number(
                        c.get(f"{kind}_runtime_prediction_error_seconds"),
                        kind,
                        signed=True,
                    ),
                ),
            ]
        )
    realized = _nested(c, "estimated_realized_cost")
    if "is_finalized_aws_invoice" in realized:
        _require(realized["is_finalized_aws_invoice"] is False, "invoice flag")
    calibration_rows.extend(
        [
            (
                "Predicted EC2 cost range USD (calibration)",
                _range(c.get("predicted_ec2_cost_range_usd"), "cost range"),
            ),
            (
                "Estimated realized EC2 cost USD",
                _number(realized.get("ec2_compute_usd"), "realized cost"),
            ),
            (
                "Cost prediction error USD",
                _number(c.get("cost_prediction_error_usd"), "cost error", signed=True),
            ),
            (
                "Actual throughput units/hour",
                _number(c.get("actual_throughput_per_hour"), "throughput"),
            ),
        ]
    )
    calibration_rows.extend(_pricing_rows(c))
    sections = _section(
        "Run",
        [
            ("Run ID", plan["run_id"]),
            ("Unit kind", plan["unit_kind"]),
            ("Total units", plan["total_units"]),
            ("Instance type", instance["type"]),
            ("vCPU", instance["vcpu"]),
            ("Memory MiB", instance["memory_mib"]),
            ("Workers", plan["worker_count"]),
        ],
    )
    sections += _section("Progress snapshot", progress_rows)
    sections += _section("Cost / planning", cost_rows)
    sections += _section("Calibration", calibration_rows)
    return (
        """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AWS execution dashboard</title>
<style>
*{box-sizing:border-box}body{margin:0;background:#f3f6fa;color:#16253b;
font:16px/1.5 system-ui,sans-serif}main{max-width:1120px;margin:auto;padding:24px}
h1{font-size:1.8rem;margin:0 0 12px}h2{font-size:1.15rem;margin:0 0 12px}
.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:18px}
section{min-width:0;background:white;padding:20px;border:1px solid #d8e1ec;
border-radius:12px}dl{margin:0}dl>div{padding:8px 0;border-bottom:1px solid #edf0f5}
dt{color:#52637a;font-size:.85rem}dd{margin:2px 0 0;font-weight:600;
overflow-wrap:anywhere}p{overflow-wrap:anywhere}.note{color:#52637a}
@media(max-width:680px){main{padding:14px}.grid{grid-template-columns:minmax(0,1fr)}}
</style></head><body><main><h1>AWS execution dashboard</h1>
<p class="note">Operational snapshot only · no automatic refresh.
Compare snapshot and generation timestamps before acting on ETA.
Costs are estimates, not a finalized AWS invoice; unknown charges are excluded.</p>
<p class="note">Fail-safe deadline and remaining time are unavailable in these v1
JSON documents. Inspect status-run.ps1 for the armed timer; workload elapsed time
cannot establish its deadline. Prediction errors use the recorded estimate range.</p>
<div class="grid">"""
        + sections
        + "</div></main></body></html>\n"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--progress", type=Path)
    parser.add_argument("--calibration", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        paths = (args.plan, args.progress, args.calibration)
        _require(
            all(path is None or path.resolve() != args.out.resolve() for path in paths),
            "output overlaps input",
        )
        documents = [
            None if path is None else json.loads(path.read_text(encoding="utf-8-sig"))
            for path in paths
        ]
        rendered = render_dashboard(*documents)
        atomic_replace(args.out, rendered.encode("utf-8"))
    except (ValueError, OSError) as exc:
        # Do not echo raw document data, local paths or credentials on failure.
        print(f"Dashboard not written: {type(exc).__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
