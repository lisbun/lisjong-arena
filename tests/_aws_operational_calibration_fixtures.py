"""Deterministic fixtures for the #340 calibration / admission gate tests.

Every value here is synthetic operational timing metadata. Nothing in this
module is, or may become, scientific evidence.

The fixtures build a real canonical seed ledger, because Phase 1 resolves the
calibration allocation against seed-registry authority rather than trusting a
binding's shape.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from lisjong_arena import aws_operational_calibration as calibration
from lisjong_arena import seed_registry

CALIBRATION_START = datetime(2026, 9, 20, 0, 0, 0, tzinfo=UTC)
CALIBRATED_AT = datetime(2026, 9, 20, 0, 10, 0, tzinfo=UTC)
NOW = datetime(2026, 9, 21, 0, 0, 0, tzinfo=UTC)

ARENA_REVISION = "1" * 40
LISJONG_REVISION = "2" * 40
LISJONG_ENGINE_REVISION = "3" * 40
RIICHIENV_VERSION = "0.4.10"
WORKLOAD_IDENTITY = "offense-foundation-332-phase-a-p2"
TEACHER_IDENTITY = "lisjong.policies.TwoStepUkeirePolicy x4"
GAME_MODE = "4p-red-half"
INSTANCE_TYPE = "c7i.4xlarge"
VCPU = 16
WORKERS = 16
TOTAL_UNITS = 20
INSTRUMENTATION_IDENTITY = "offense-foundation-332/operational-progress/v1"
CALIBRATION_DURABLE_LEVEL = "per-seed-durable-receipt"\nPRODUCTION_DURABLE_LEVEL = "atomic-operational-progress"

SEED_DOMAIN = seed_registry.RIICHIENV_HALF_HANCHAN_SEED_DOMAIN
PRODUCTION_SEEDS = tuple(range(70_000, 70_020))

#: Distinct, non-overlapping calibration populations, one per fixture shape.
#: Same-domain collision is a ledger invariant, so they cannot share seeds.
CALIBRATION_SEED_SETS: dict[str, tuple[int, int]] = {
    "default": (900_000, 20),
    "percentile": (901_000, 10),
    "serial": (902_000, 20),
    "workers8": (903_000, 16),
    "workers2": (904_000, 4),
    "burstable": (905_000, 8),
    "launcher": (906_000, 64),
}

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
MEASURED_SETUP_SECONDS = 900.0
MEASURED_TEARDOWN_SECONDS = 400.0
MEASURED_BILLABLE_SECONDS = 2400.0

INSTANCE_LAUNCH_EPOCH = 1_789_999_400
FAIL_SAFE_ARM_EPOCH = 1_790_000_000
INSTANCE_ID = "i-0123456789abcdef0"

_LEDGER: dict[str, object] | None = None
_IDENTITIES: dict[tuple[int, ...], str] = {}
_PRODUCTION_IDENTITY: str | None = None


def _reserve(ledger, **kwargs):
    return seed_registry.reserve_allocation(
        ledger,
        arena_revision=ARENA_REVISION,
        protocol_revision="synthetic-fixture-v1",
        provenance_reference="synthetic #340 fixture",
        allocation_timestamp="2026-09-20T00:00:00Z",
        **kwargs,
    )


def _build_ledger() -> None:
    global _LEDGER, _PRODUCTION_IDENTITY
    ledger = seed_registry.new_ledger()
    ledger, production = _reserve(
        ledger,
        owner_issue="lisbun/lisjong-arena#332",
        protocol="offense-foundation-v1",
        seed_domain=SEED_DOMAIN,
        purpose="synthetic production population",
        population="offense-foundation",
        split="QUALIFICATION",
        seeds=PRODUCTION_SEEDS,
    )
    _PRODUCTION_IDENTITY = production["allocation_identity"]
    for first, count in CALIBRATION_SEED_SETS.values():
        seeds = tuple(range(first, first + count))
        ledger, record = _reserve(
            ledger,
            owner_issue="lisbun/lisjong-arena#340",
            protocol=calibration.CALIBRATION_PROTOCOL,
            seed_domain=SEED_DOMAIN,
            purpose="synthetic operational calibration population",
            population=calibration.CALIBRATION_POPULATION,
            split=None,
            seeds=seeds,
        )
        _IDENTITIES[seeds] = record["allocation_identity"]
    _LEDGER = ledger


def ledger() -> dict[str, object]:
    if _LEDGER is None:
        _build_ledger()
    assert _LEDGER is not None
    return _LEDGER


def production_identity() -> str:
    ledger()
    assert _PRODUCTION_IDENTITY is not None
    return _PRODUCTION_IDENTITY


def binding_for(seeds) -> dict[str, object]:
    """Return the canonical allocation binding for an exact calibration seed set."""

    return seed_registry.allocation_binding(ledger(), _IDENTITIES[tuple(seeds)])


def _stamp(value: datetime) -> str:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def tasks(
    *,
    shape: str = "default",
    workers: int = WORKERS,
    seconds: float = TASK_SECONDS,
    start: datetime = CALIBRATION_START,
    serial: bool = False,
) -> list[dict[str, object]]:
    """Return the tasks of one reserved calibration population."""

    first_seed, count = CALIBRATION_SEED_SETS[shape]
    result = []
    for index in range(count):
        step = index if serial else index // workers
        began = start + timedelta(seconds=step * seconds)
        result.append(
            {
                "seed": first_seed + index,
                "started_at": _stamp(began),
                "completed_at": _stamp(began + timedelta(seconds=seconds)),
            }
        )
    return result


def evidence(*, shape: str = "default", **overrides: object) -> dict[str, object]:
    first_seed, count = CALIBRATION_SEED_SETS[shape]
    population = tuple(range(first_seed, first_seed + count))
    values: dict[str, object] = {
        "calibration_run_id": f"calibration-{shape}-0001",
        "calibrated_at": CALIBRATED_AT,
        "seed_allocation": binding_for(population),
        "seed_allocation_population": calibration.CALIBRATION_POPULATION,
        "seed_ledger_revision": seed_registry.ledger_revision(ledger()),
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
        "tasks": tasks(shape=shape),
        "batch_scientific_wall_clock_seconds": WALL_CLOCK_SECONDS,
        "ec2_billable_runtime_seconds": MEASURED_BILLABLE_SECONDS,
        "setup_overhead_seconds": MEASURED_SETUP_SECONDS,
        "teardown_overhead_seconds": MEASURED_TEARDOWN_SECONDS,
        "durable_evidence_level": CALIBRATION_DURABLE_LEVEL,
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


def target(**overrides: object) -> dict[str, object]:
    document: dict[str, object] = {
        "arena_revision": ARENA_REVISION,
        "durable_evidence_level": PRODUCTION_DURABLE_LEVEL,
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
    document.update(overrides)
    return document


def budget(**overrides: object) -> dict[str, object]:
    document: dict[str, object] = {
        "cost_budget_usd": COST_BUDGET_USD,
        "execution_budget_seconds": EXECUTION_BUDGET_SECONDS,
        "hard_fail_safe_seconds": HARD_FAIL_SAFE_SECONDS,
        "headroom_factor": calibration.DEFAULT_HEADROOM_FACTOR,
        "normal_deadline_seconds": NORMAL_DEADLINE_SECONDS,
        "post_processing_seconds": POST_PROCESSING_SECONDS,
        "setup_seconds": SETUP_SECONDS,
        "teardown_seconds": TEARDOWN_SECONDS,
    }
    document.update(overrides)
    return document


def requirement(**overrides: object) -> dict[str, object]:
    document: dict[str, object] = {
        "schema_version": calibration.REQUIREMENT_SCHEMA_VERSION,
        "budget": budget(),
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
                "detail": "atomic operational progress; incomplete scientific phases are full-rerun only",
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
            "allocation_identities": [production_identity()],
            "seed_domain": SEED_DOMAIN,
            "seed_membership_identities": [
                seed_registry.seed_membership_identity(PRODUCTION_SEEDS)
            ],
            "seeds": list(PRODUCTION_SEEDS),
        },
        "run_id": "phase-A-20260921T000000Z-abcd1234",
        "target": target(),
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


def calibration_requirement(**overrides: object) -> dict[str, object]:
    """The bounded calibration run's own admission requirement."""

    first_seed, count = CALIBRATION_SEED_SETS["default"]
    seeds = list(range(first_seed, first_seed + count))
    document: dict[str, object] = {
        "schema_version": calibration.CALIBRATION_REQUIREMENT_SCHEMA_VERSION,
        "allocation_binding": binding_for(seeds),
        "bounds": {"max_unit_count": 64, "max_worker_count": 32},
        "budget": budget(
            cost_budget_usd=15.0,
            hard_fail_safe_seconds=14400.0,
            normal_deadline_seconds=13500.0,
            execution_budget_seconds=11100.0,
        ),
        "calibration_cost_budget_usd": 15.0,
        "charges": charges(),
        "consumer": "lisbun/lisjong-arena#340 calibration for #332 Phase A",
        "gates": {
            "artifact_destination": {
                "status": "PASS",
                "detail": "encrypted 8 GiB gp3 calibration volume retained",
            },
            "durable_evidence": {
                "status": "PASS",
                "detail": "per-seed durable receipts plus atomic progress",
            },
            "teardown_confirmation": {
                "status": "PASS",
                "detail": "collector confirms termination and records residuals",
            },
        },
        "pricing": {
            "checked_at": "2026-09-21T00:00:00Z",
            "instance_hourly_rate_usd": HOURLY_RATE_USD,
            "region": "ap-northeast-1",
            "source": "AWS Pricing API",
        },
        "run_id": "calibration-20260921T000000Z-abcd1234",
        "seeds": seeds,
        "target": target(durable_evidence_level=CALIBRATION_DURABLE_LEVEL),
    }
    for key, value in overrides.items():
        if key in ("budget", "target", "bounds", "gates") and isinstance(value, dict):
            merged = dict(document[key])  # type: ignore[arg-type]
            merged.update(value)
            document[key] = merged
        else:
            document[key] = value
    return document


def observation(prior: dict[str, object], **overrides: object) -> dict[str, object]:
    document: dict[str, object] = {
        "schema_version": calibration.OBSERVATION_SCHEMA_VERSION,
        "arena_revision": ARENA_REVISION,
        "availability_zone": "ap-northeast-1a",
        "boot_fail_safe_armed": True,
        # The boot timer holds the EC2 launch clock, so its deadline is
        # launch + (pre-arm allowance + hard fail-safe + teardown).
        "boot_fail_safe_deadline_epoch": INSTANCE_LAUNCH_EPOCH
        + int(calibration.boot_fail_safe_seconds(prior["budget"])),  # type: ignore[index]
        "fail_safe_arm_epoch": FAIL_SAFE_ARM_EPOCH,
        "fail_safe_armed": True,
        "fail_safe_deadline_epoch": FAIL_SAFE_ARM_EPOCH
        + int(float(prior["budget"]["hard_fail_safe_seconds"])),  # type: ignore[index]
        "instance_id": INSTANCE_ID,
        "instance_launch_epoch": INSTANCE_LAUNCH_EPOCH,
        "instance_type": INSTANCE_TYPE,
        "observed_at_epoch": FAIL_SAFE_ARM_EPOCH + 600,
        "phase_one_admission_identity": prior["admission_identity"],
        "recovery_identity": {
            "path": "/mnt/lisjong-332-output/.lisjong-admission/recovery-identity.json",
            "run_id": prior["run_id"],
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
        "run_id": prior["run_id"],
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
