"""Deterministic fixtures for the #340 calibration / admission gate tests.

Every value here is synthetic operational timing metadata. Nothing in this
module is, or may become, scientific evidence.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from lisjong_arena import aws_operational_calibration as calibration

CALIBRATION_START = datetime(2026, 9, 20, 0, 0, 0, tzinfo=UTC)
CALIBRATED_AT = datetime(2026, 9, 20, 0, 10, 0, tzinfo=UTC)
NOW = datetime(2026, 9, 21, 0, 0, 0, tzinfo=UTC)

ARENA_REVISION = "1" * 40
LISJONG_REVISION = "2" * 40
LISJONG_ENGINE_REVISION = "3" * 40
RIICHIENV_VERSION = "0.4.10"
WORKLOAD_IDENTITY = "offense-foundation-332-phase-a-p2"
TEACHER_IDENTITY = "TwoStepUkeirePolicy x4"
GAME_MODE = "4p-red-half"
INSTANCE_TYPE = "c7i.4xlarge"
VCPU = 16
WORKERS = 16
TOTAL_UNITS = 20
INSTRUMENTATION_IDENTITY = "offense-foundation-332/operational-progress/v1"
DURABLE_LEVEL = "atomic-operational-progress"

CALIBRATION_ALLOCATION_IDENTITY = "a" * 64
CALIBRATION_MEMBERSHIP_IDENTITY = "b" * 64
LEDGER_REVISION = "c" * 64
PRODUCTION_ALLOCATION_IDENTITY = "d" * 64
PRODUCTION_MEMBERSHIP_IDENTITY = "e" * 64

HOURLY_RATE_USD = 0.856
SETUP_SECONDS = 1200.0
POST_PROCESSING_SECONDS = 1200.0
TEARDOWN_SECONDS = 900.0
HARD_FAIL_SAFE_SECONDS = 43200.0
NORMAL_DEADLINE_SECONDS = HARD_FAIL_SAFE_SECONDS - TEARDOWN_SECONDS
EXECUTION_BUDGET_SECONDS = 39900.0
COST_BUDGET_USD = 20.0

#: 20 tasks of 60s on 16 workers: one saturated wave of 16 plus a 4-task tail.
WALL_CLOCK_SECONDS = 125.0
TASK_SECONDS = 60.0

FAIL_SAFE_ARM_EPOCH = 1_790_000_000
INSTANCE_ID = "i-0123456789abcdef0"


def _stamp(value: datetime) -> str:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def tasks(
    *,
    count: int = TOTAL_UNITS,
    workers: int = WORKERS,
    seconds: float = TASK_SECONDS,
    first_seed: int = 900_000,
    start: datetime = CALIBRATION_START,
) -> list[dict[str, object]]:
    """Return ``count`` tasks scheduled in saturated waves of ``workers``."""

    result = []
    for index in range(count):
        offset = timedelta(seconds=(index // workers) * seconds)
        began = start + offset
        result.append(
            {
                "seed": first_seed + index,
                "started_at": _stamp(began),
                "completed_at": _stamp(began + timedelta(seconds=seconds)),
            }
        )
    return result


def evidence(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "calibration_run_id": "calibration-20260920T000000Z-0001",
        "calibrated_at": CALIBRATED_AT,
        "seed_allocation": {
            "allocation_identity": CALIBRATION_ALLOCATION_IDENTITY,
            "ledger_revision": LEDGER_REVISION,
            "owner_repository": "lisbun/lisjong-arena",
            "seed_domain": "riichienv-4p-red-half-hanchan-v1",
            "seed_membership_identity": CALIBRATION_MEMBERSHIP_IDENTITY,
        },
        "seed_allocation_population": calibration.CALIBRATION_POPULATION,
        "seed_ledger_revision": LEDGER_REVISION,
        "arena_revision": ARENA_REVISION,
        "lisjong_revision": LISJONG_REVISION,
        "lisjong_engine_revision": LISJONG_ENGINE_REVISION,
        "riichienv_version": RIICHIENV_VERSION,
        "workload_identity": WORKLOAD_IDENTITY,
        "teacher_identity": TEACHER_IDENTITY,
        "game_mode": GAME_MODE,
        "instance_type": INSTANCE_TYPE,
        "vcpu": VCPU,
        "worker_count_requested": WORKERS,
        "workers_active_observed": WORKERS,
        "tasks": tasks(),
        "batch_scientific_wall_clock_seconds": WALL_CLOCK_SECONDS,
        "ec2_billable_runtime_seconds": 2400.0,
        "setup_overhead_seconds": 900.0,
        "teardown_overhead_seconds": 400.0,
        "durable_evidence_level": DURABLE_LEVEL,
        "instrumentation_identity": INSTRUMENTATION_IDENTITY,
        "instrumentation_path": "issue-332/phase-A/operational/progress.json",
    }
    values.update(overrides)
    return calibration.build_calibration_evidence(**values)  # type: ignore[arg-type]


def charges() -> list[dict[str, object]]:
    return [
        {
            "label": "retained encrypted 8 GiB gp3 output volume (30 days)",
            "material": True,
            "max_usd": None,
            "reason": None,
            "usd": 0.96,
        },
        {
            "label": "public IPv4 address hours",
            "material": True,
            "max_usd": 0.07,
            "reason": None,
            "usd": None,
        },
        {
            "label": "data transfer out",
            "material": False,
            "max_usd": None,
            "reason": "artifacts stay on the retained EBS volume; no bulk egress",
            "usd": None,
        },
    ]


def requirement(**overrides: object) -> dict[str, object]:
    budget: dict[str, object] = {
        "cost_budget_usd": COST_BUDGET_USD,
        "execution_budget_seconds": EXECUTION_BUDGET_SECONDS,
        "hard_fail_safe_seconds": HARD_FAIL_SAFE_SECONDS,
        "headroom_factor": calibration.DEFAULT_HEADROOM_FACTOR,
        "normal_deadline_seconds": NORMAL_DEADLINE_SECONDS,
        "post_processing_seconds": POST_PROCESSING_SECONDS,
        "setup_seconds": SETUP_SECONDS,
        "teardown_seconds": TEARDOWN_SECONDS,
    }
    target: dict[str, object] = {
        "arena_revision": ARENA_REVISION,
        "durable_evidence_level": DURABLE_LEVEL,
        "game_mode": GAME_MODE,
        "instance_type": INSTANCE_TYPE,
        "instrumentation_identity": INSTRUMENTATION_IDENTITY,
        "lisjong_engine_revision": LISJONG_ENGINE_REVISION,
        "lisjong_revision": LISJONG_REVISION,
        "riichienv_version": RIICHIENV_VERSION,
        "teacher_identity": TEACHER_IDENTITY,
        "total_units": TOTAL_UNITS,
        "vcpu": VCPU,
        "worker_count": WORKERS,
        "workload_identity": WORKLOAD_IDENTITY,
    }
    document: dict[str, object] = {
        "schema_version": calibration.REQUIREMENT_SCHEMA_VERSION,
        "budget": budget,
        "calibration_policy": {
            "freshness_max_age_seconds": 2_592_000.0,
            "minimum_task_count": 8,
            "minimum_tasks_per_worker": 1.0,
        },
        "charges": charges(),
        "consumer": "lisbun/lisjong-arena#332 Phase A",
        "gates": {
            "artifact_destination": {
                "status": "PASS",
                "detail": "encrypted 8 GiB gp3 volume retained after teardown",
            },
            "durable_evidence": {
                "status": "PASS",
                "detail": "atomic operational progress at operational/progress.json",
            },
            "protocol_lock": {
                "status": "PASS",
                "detail": "Phase A protocol lock materialized from merged-main "
                "qualification",
            },
            "reattach": {
                "status": "PASS",
                "detail": "RunId tag discovery plus -ReattachRunId verified",
            },
            "seed_registry": {
                "status": "PASS",
                "detail": "canonical seed-registry ledger validated; allocations "
                "active",
            },
        },
        "pricing": {
            "checked_at": "2026-09-21T00:00:00Z",
            "instance_hourly_rate_usd": HOURLY_RATE_USD,
            "region": "ap-northeast-1",
            "source": "AWS Pricing API",
        },
        "production_allocation": {
            "allocation_identities": [PRODUCTION_ALLOCATION_IDENTITY],
            "seed_membership_identities": [PRODUCTION_MEMBERSHIP_IDENTITY],
        },
        "run_id": "phase-A-20260921T000000Z-abcd1234",
        "target": target,
    }
    for key, value in overrides.items():
        if key in ("budget", "target", "calibration_policy") and isinstance(
            value, dict
        ):
            merged = dict(document[key])  # type: ignore[arg-type]
            merged.update(value)
            document[key] = merged
        else:
            document[key] = value
    return document


def observation(phase_one: dict[str, object], **overrides: object) -> dict[str, object]:
    document: dict[str, object] = {
        "schema_version": calibration.OBSERVATION_SCHEMA_VERSION,
        "arena_revision": ARENA_REVISION,
        "availability_zone": "ap-northeast-1a",
        "fail_safe_arm_epoch": FAIL_SAFE_ARM_EPOCH,
        "fail_safe_armed": True,
        "fail_safe_deadline_epoch": FAIL_SAFE_ARM_EPOCH + int(HARD_FAIL_SAFE_SECONDS),
        "instance_id": INSTANCE_ID,
        "instance_type": INSTANCE_TYPE,
        "observed_at_epoch": FAIL_SAFE_ARM_EPOCH + 600,
        "phase_one_admission_identity": phase_one["admission_identity"],
        "recovery_identity": {
            "path": "/mnt/lisjong-332-output/.lisjong-admission/recovery-identity.json",
            "run_id": phase_one["run_id"],
            "status": "PASS",
            "verified_on_instance_id": INSTANCE_ID,
        },
        "retained_destination": {
            "encrypted": True,
            "path": "/mnt/lisjong-332-output",
            "retention": "retained after teardown",
            "size_gib": 8,
            "verified_on_instance_id": INSTANCE_ID,
            "volume_id": "vol-0123456789abcdef0",
            "write_probe": "PASS",
        },
        "run_id": phase_one["run_id"],
        "vcpu": VCPU,
        "worker_count": WORKERS,
    }
    for key, value in overrides.items():
        if key in ("recovery_identity", "retained_destination") and isinstance(
            value, dict
        ):
            merged = dict(document[key])  # type: ignore[arg-type]
            merged.update(value)
            document[key] = merged
        else:
            document[key] = value
    return document
