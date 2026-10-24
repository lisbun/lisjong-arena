"""Project #100: pure, fail-closed pre-start checks; no scheduler/credentials.

Inputs must come from a trusted controller, never a candidate/LLM session.
PASS is a resource check, not governance approval or execution authority.
"""

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from lisjong_arena._artifact_io import (
    ArtifactValidationError,
    canonical_json_text,
    expect_object,
    read_json_document,
    write_new_artifact_file,
)

SCHEMA = "arena-strength-loop-guard-v1"
SERVICES = ("aws", "llm")
ACTIONS = ("development_run", "llm_session", "candidate_pr")


def _integer(value, name, minimum=0):
    if type(value) is not int or value < minimum:
        raise ArtifactValidationError(f"{name} must be an integer >= {minimum}")
    return value


def _boolean(value, name):
    if type(value) is not bool:
        raise ArtifactValidationError(f"{name} must be a boolean")
    return value


def _cost(value, name):
    costs = expect_object(value, set(SERVICES), name)
    for service in SERVICES:
        _integer(costs[service], f"{name}.{service}")
    return costs


def evaluate(document, *, now):
    """Return a machine digest and decision; USD values are integer microdollars.

    Each open run reserves max(estimate - billed portion, 0). billed_total
    already includes that portion: it is never added twice. Completed rows
    require reconciled final billing and retain zero outstanding reservation.
    """
    doc = expect_object(
        document, {"schema", "policy", "snapshot", "request"}, "guard input"
    )
    if doc["schema"] != SCHEMA:
        raise ArtifactValidationError("unsupported guard schema")
    policy = expect_object(
        doc["policy"],
        {
            "enabled",
            "month",
            "budget_microusd",
            "max_age_seconds",
            "stagnation_limit",
            "weekly_session_limit",
            "weekly_pr_limit",
        },
        "policy",
    )
    snapshot = expect_object(
        doc["snapshot"],
        {
            "as_of",
            "paused",
            "billing_complete",
            "billed_microusd",
            "runs",
            "cycles_without_promotion",
            "iso_week",
            "sessions_this_week",
            "prs_this_week",
        },
        "snapshot",
    )
    request = expect_object(doc["request"], {"action", "estimated_microusd"}, "request")
    if request["action"] not in ACTIONS:
        raise ArtifactValidationError(
            "formal evaluation/promotion is not a supported action"
        )
    if now.tzinfo is None or now.utcoffset().total_seconds() != 0:
        raise ArtifactValidationError("now must be UTC")
    if type(snapshot["as_of"]) is not str or not snapshot["as_of"].endswith("Z"):
        raise ArtifactValidationError("as_of must be UTC RFC3339")
    try:
        observed = datetime.fromisoformat(snapshot["as_of"][:-1] + "+00:00")
    except ValueError as error:
        raise ArtifactValidationError("invalid as_of") from error
    max_age = _integer(policy["max_age_seconds"], "max_age_seconds", 1)
    if not 0 <= (now - observed).total_seconds() <= max_age:
        raise ArtifactValidationError("snapshot is stale or from the future")
    month = now.strftime("%Y-%m")
    if policy["month"] != month or observed.strftime("%Y-%m") != month:
        raise ArtifactValidationError(
            "budget/snapshot month must match current UTC billing month"
        )
    week = now.strftime("%G-W%V")
    if snapshot["iso_week"] != week:
        raise ArtifactValidationError("weekly counters must match current ISO week")
    budgets = _cost(policy["budget_microusd"], "budget")
    # Existing total AWS cap, not an allocation/launch authorization.
    aws_cap = 30_000_000 if month == "2026-10" else 20_000_000
    if budgets["aws"] > aws_cap:
        raise ArtifactValidationError("AWS budget exceeds the approved month cap")
    billed = _cost(snapshot["billed_microusd"], "billed")
    estimate = _cost(request["estimated_microusd"], "estimate")
    reasons = []
    if not _boolean(policy["enabled"], "enabled"):
        reasons.append("DISABLED")
    if _boolean(snapshot["paused"], "paused"):
        reasons.append("PAUSED")
    if not _boolean(snapshot["billing_complete"], "billing_complete"):
        reasons.append("BILLING_UNRECONCILED")
    if type(snapshot["runs"]) is not list:
        raise ArtifactValidationError("runs must be an array")
    outstanding = dict.fromkeys(SERVICES, 0)
    attributed = dict.fromkeys(SERVICES, 0)
    seen = set()
    for value in snapshot["runs"]:
        row = expect_object(
            value,
            {"run_id", "estimated_microusd", "billed_microusd", "finalized"},
            "run",
        )
        identity = row["run_id"]
        if type(identity) is not str or not identity.strip() or identity in seen:
            raise ArtifactValidationError("run IDs must be nonempty and unique")
        seen.add(identity)
        expected = _cost(row["estimated_microusd"], "run estimate")
        actual = _cost(row["billed_microusd"], "run billed")
        final = _boolean(row["finalized"], "finalized")
        for service in SERVICES:
            attributed[service] += actual[service]
            if not final:
                if actual[service] > expected[service]:
                    raise ArtifactValidationError(
                        "open run exceeded its estimate; reconcile before starting"
                    )
                outstanding[service] += expected[service] - actual[service]
    if any(attributed[s] > billed[s] for s in SERVICES):
        raise ArtifactValidationError("attributed billing exceeds billed total")
    cycles = _integer(snapshot["cycles_without_promotion"], "cycles_without_promotion")
    limit = _integer(policy["stagnation_limit"], "stagnation_limit", 1)
    if cycles >= limit:
        reasons.append("STAGNATION_LIMIT")
    for counter, limit_name, action in (
        ("sessions_this_week", "weekly_session_limit", "llm_session"),
        ("prs_this_week", "weekly_pr_limit", "candidate_pr"),
    ):
        used = _integer(snapshot[counter], counter)
        maximum = _integer(policy[limit_name], limit_name)
        if request["action"] == action and used >= maximum:
            reasons.append(limit_name.upper())
    projected = {s: billed[s] + outstanding[s] + estimate[s] for s in SERVICES}
    warnings = []
    for service in SERVICES:
        if projected[service] >= budgets[service]:
            reasons.append(f"{service.upper()}_BUDGET_LIMIT")
        elif projected[service] * 5 >= budgets[service] * 4:
            warnings.append(f"{service.upper()}_BUDGET_80_PERCENT")
    return {
        "schema": SCHEMA,
        "month": month,
        "as_of": snapshot["as_of"],
        "status": "STOP" if reasons else "RESOURCE_CHECK_PASSED",
        "reasons": reasons,
        "warnings": warnings,
        "billed_microusd": billed,
        "outstanding_microusd": outstanding,
        "projected_microusd": projected,
        "cycles_without_promotion": cycles,
        "sessions_this_week": snapshot["sessions_this_week"],
        "prs_this_week": snapshot["prs_this_week"],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        result = evaluate(
            read_json_document(args.input), now=datetime.now(timezone.utc)
        )
        serialized = canonical_json_text(result)
        write_new_artifact_file(args.output, serialized)
        if read_json_document(args.output) != result:
            raise ArtifactValidationError("output readback mismatch")
        print(serialized, end="")
        return 2 if result["status"] == "STOP" else 0
    except (ValueError, OSError) as error:
        print(f"STOP / INVALID: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
