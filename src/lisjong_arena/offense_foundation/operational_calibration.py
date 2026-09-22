"""Bounded operational calibration execution for Issue #340.

This runs the exact #331/#332 per-hanchan execution path -- the same teacher,
the same runtime qualification, the same paired corpus / player-safe
source-record writes -- on a dedicated calibration-only seed population, purely
to measure how long it takes.

It is deliberately *not* a corpus generator:

* it seals no manifest, aggregates no support and emits no P2 outcome;
* the game artifacts it writes go to a scratch directory the caller wipes
  before teardown, so no scientific-looking data is retained;
* the only retained evidence is a #339 per-seed durable receipt whose payload
  is operational timing metadata, the #329 atomic progress file, and a raw
  observation document for ``aws_operational_calibration evidence``.

A calibration result is never qualification, TRAIN, SELECT or OFFLINE-EVAL
evidence, and its seeds are excluded from scientific use forever.
"""

from __future__ import annotations

import os
import shutil
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path

from lisjong_arena.aws_execution_observability import ProgressTracker, write_progress
from lisjong_arena.durable_seed_checkpoint import publish_seed_checkpoint
from lisjong_arena.seed_registry import (
    SeedRegistryError,
    load_ledger,
    require_allocation_binding,
)

from .corpus import MAX_PROCESS_WORKERS, _write_game
from .instrumentation import (
    CALIBRATION_DURABLE_EVIDENCE_LEVEL,
    GENERATION_INSTRUMENTATION_IDENTITY,
)
from .protocol import GAME_MODE
from .qualification import (
    qualification_contract,
    read_document,
    require_qualification,
    runtime_binding,
)
from .semantics import OffenseError

OBSERVATION_SCHEMA = "arena-offense-foundation-calibration-observation-v1"

#: The dedicated calibration allocation contract, mirrored from
#: ``lisjong_arena.aws_operational_calibration`` without importing it, so the
#: instance-side workload stays independent of the admission core.
CALIBRATION_POPULATION = "operational-calibration"
CALIBRATION_PROTOCOL = "aws-operational-calibration-v1"

#: A bounded calibration never grows into a production-sized run by accident.
MAX_CALIBRATION_UNITS = 256


def _stamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _timed_game(arguments):
    """Execute one hanchan and return only its timing, never its content."""

    scratch, source_scratch, ordinal, seed, identity = arguments
    started = datetime.now(UTC)
    _write_game(
        Path(scratch), Path(source_scratch), ordinal, "CALIBRATION", seed, identity
    )
    completed = datetime.now(UTC)
    return seed, _stamp(started), _stamp(completed), os.getpid()


def require_calibration_allocation(seeds, *, binding, seed_ledger_path):
    """Fail closed unless the seeds are a dedicated ledger-resolved population."""

    try:
        record = require_allocation_binding(
            load_ledger(seed_ledger_path),
            binding,
            seeds=seeds,
            protocol=CALIBRATION_PROTOCOL,
            population=CALIBRATION_POPULATION,
        )
    except SeedRegistryError as error:
        raise OffenseError(
            f"calibration allocation authority invalid: {error}"
        ) from error
    if record["split"] is not None:
        raise OffenseError("a calibration allocation must not declare a split")
    return record


def run_calibration(
    *,
    seeds,
    qualification_path,
    scratch_dir,
    receipt_dir,
    run_id,
    workload_identity,
    workers=1,
    project="pyproject.toml",
    progress_path=None,
    keep_scratch=False,
):
    """Execute the bounded calibration batch and return its raw observation."""

    seeds = list(seeds)
    if not seeds or len(set(seeds)) != len(seeds):
        raise OffenseError("calibration seeds must be a non-empty unique population")
    if len(seeds) > MAX_CALIBRATION_UNITS:
        raise OffenseError(
            f"a calibration batch is bounded to {MAX_CALIBRATION_UNITS} units"
        )
    if type(workers) is not int or not 1 <= workers <= MAX_PROCESS_WORKERS:
        raise OffenseError(
            f"workers must be an integer from 1 through {MAX_PROCESS_WORKERS}"
        )
    if workers > len(seeds):
        raise OffenseError("workers must not exceed the calibration unit count")

    binding = runtime_binding(project)
    report = read_document(qualification_path)
    require_qualification(report, binding)
    contract = qualification_contract(report)

    scratch = Path(scratch_dir)
    receipts = Path(receipt_dir)
    if scratch.exists() and any(scratch.iterdir()):
        raise OffenseError("calibration scratch directory is not fresh")
    if receipts.exists() and any(receipts.iterdir()):
        raise OffenseError("calibration receipt directory is not fresh")
    scratch.mkdir(parents=True, exist_ok=True)
    receipts.mkdir(parents=True, exist_ok=True)

    # The receipts are bound to the qualification identity, which is the exact
    # teacher/runtime this calibration measured.
    protocol_identity = str(report["identity"])
    tracker = None
    started_at = datetime.now(UTC)
    if progress_path:
        tracker = ProgressTracker(
            run_id=run_id,
            unit_kind="hanchan",
            total_units=len(seeds),
            worker_count=workers,
            started_at=started_at,
        )
        write_progress(progress_path, tracker.snapshot(0, now=started_at))

    monotonic_start = time.monotonic()
    arguments = [
        (
            str(scratch / f"game-{index:03d}"),
            str(scratch / f"source-{index:03d}"),
            index,
            seed,
            protocol_identity,
        )
        for index, seed in enumerate(seeds)
    ]
    observations: list[tuple[int, str, str, int]] = []
    if workers == 1:
        for argument in arguments:
            observations.append(_timed_game(argument))
            _publish(receipts, run_id, protocol_identity, observations[-1])
            if tracker is not None:
                write_progress(
                    progress_path,
                    tracker.snapshot(len(observations), now=datetime.now(UTC)),
                )
    else:
        executor = ProcessPoolExecutor(max_workers=workers)
        futures = [executor.submit(_timed_game, argument) for argument in arguments]
        try:
            for future in as_completed(futures):
                observations.append(future.result())
                _publish(receipts, run_id, protocol_identity, observations[-1])
                if tracker is not None:
                    write_progress(
                        progress_path,
                        tracker.snapshot(len(observations), now=datetime.now(UTC)),
                    )
        except BaseException:
            for future in futures:
                future.cancel()
            raise
        finally:
            executor.shutdown(wait=True, cancel_futures=True)
    wall_clock = time.monotonic() - monotonic_start
    completed_at = datetime.now(UTC)
    if len(observations) != len(seeds):
        raise OffenseError("incomplete calibration batch")

    if not keep_scratch:
        # The measured artifact writes are real, but nothing scientific is
        # retained from a calibration run.
        shutil.rmtree(scratch, ignore_errors=True)

    observations.sort(key=lambda entry: entry[0])
    return {
        "schema_version": OBSERVATION_SCHEMA,
        "arena_revision": contract["arena_revision"],
        "batch_scientific_wall_clock_seconds": round(wall_clock, 3),
        "calibration_run_id": run_id,
        "completed_at": _stamp(completed_at),
        "durable_evidence_level": CALIBRATION_DURABLE_EVIDENCE_LEVEL,
        "game_mode": GAME_MODE,
        "instrumentation_identity": GENERATION_INSTRUMENTATION_IDENTITY,
        "instrumentation_path": str(receipts),
        "lisjong_engine_revision": contract["lisjong_engine_revision"],
        "lisjong_revision": contract["lisjong_revision"],
        "riichienv_version": contract["riichienv_version"],
        "started_at": _stamp(started_at),
        "tasks": [
            {"seed": seed, "started_at": began, "completed_at": ended}
            for seed, began, ended, _pid in observations
        ],
        "teacher_identity": f"{contract['teacher']} x4",
        "workers_active_observed": len({entry[3] for entry in observations}),
        "worker_count_requested": workers,
        "workload_identity": workload_identity,
    }


def _publish(receipts, run_id, protocol_identity, observation):
    seed, began, ended, pid = observation
    publish_seed_checkpoint(
        receipts,
        run_id=run_id,
        seed=seed,
        protocol_identity=protocol_identity,
        # Operational timing only: a calibration receipt never carries a
        # hanchan artifact, a support count or any scientific outcome.
        payload={
            "completed_at": ended,
            "kind": "operational-calibration-timing",
            "seed": seed,
            "started_at": began,
            "worker_pid": pid,
        },
        started_at=began,
        completed_at=ended,
    )


__all__ = [
    "CALIBRATION_POPULATION",
    "CALIBRATION_PROTOCOL",
    "MAX_CALIBRATION_UNITS",
    "OBSERVATION_SCHEMA",
    "require_calibration_allocation",
    "run_calibration",
]
