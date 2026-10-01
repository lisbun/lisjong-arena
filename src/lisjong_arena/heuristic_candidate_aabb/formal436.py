"""AWS admission for event 436; uses the existing AABB execution and statistics."""

from __future__ import annotations

import subprocess

from lisjong_arena.seed_registry import require_allocation_binding

from .lock import locked_event, parse_lock_document
from .protocol import PROTOCOL_ID, SEED_DOMAIN, require_population

REVIEWED_BASE = "fe2cbda70ca97e72964f3ff41434015b451adcc5"


def require_allocation(ledger, binding, seeds, *, arena_revision):
    record = require_allocation_binding(
        ledger,
        binding,
        seeds=require_population(seeds),
        owner_issue="lisbun/lisjong-arena#436",
        protocol=PROTOCOL_ID,
        seed_domain=SEED_DOMAIN,
        population="heuristic-candidate-aabb-436",
        split="FORMAL-EVAL",
    )
    if record["arena_revision"] != arena_revision:
        raise ValueError("formal allocation belongs to another Arena revision")
    return record


def runtime_plan(repository, revision):
    """Record the uncalibrated 32-worker choice without borrowing #423 timings."""
    changed = subprocess.check_output(
        [
            "git",
            "-C",
            str(repository),
            "diff",
            "--name-only",
            REVIEWED_BASE,
            revision,
            "--",
            "src",
            "pyproject.toml",
            ":(exclude)src/lisjong_arena/heuristic_candidate_aabb/formal436.py",
        ],
        text=True,
    ).strip()
    if changed:
        raise ValueError(f"workload differs from reviewed #436 path: {changed}")
    return {
        "available": False,
        "basis": "Operator selected AWS 32 workers; no matching runtime calibration.",
        "instance_type": "c7i.8xlarge",
        "workers": 32,
        "matching_calibration": False,
        "reviewed_workload_revision": REVIEWED_BASE,
        "formal_execution_revision": revision,
        "workload_seconds": None,
        "predicted_lower_seconds": None,
        "predicted_upper_seconds": None,
        "limitations": ["The launch-clock deadline is a cost guard, not an ETA."],
    }


def require_aws_lock(value):
    """Bind saved evidence to #436 before collection can delete its S3 copy."""
    lock = parse_lock_document(value)
    if locked_event(lock) != 436:
        raise ValueError("AWS event 436 requires an event 436 lock")
    if lock["max_workers"] != 32:
        raise ValueError("AWS event 436 requires 32 workers")
    return lock
