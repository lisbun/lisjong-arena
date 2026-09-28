"""Bounded #423 DEVELOPMENT calibration of the exact Rust AABB seed-block path.

Retains timing/resource/native evidence only. No scores, ranks, classification
or formal comparison artifact leaves the calibration worker.
"""

from __future__ import annotations

import argparse
import json
import math
import multiprocessing
import os
import platform
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path

from lisjong_arena._artifact_io import (
    canonical_json_text,
    expect_object,
    read_json_document,
    write_new_artifact_file,
)
from lisjong_arena._execution_safety import (
    require_clean_arena_head,
    require_merged_arena_revision,
)
from lisjong_arena.aws_execution_observability import ProgressTracker, write_progress
from lisjong_arena.aws_operational_calibration import (
    build_calibration_evidence,
    predict_runtime,
)
from lisjong_arena.durable_seed_checkpoint import publish_seed_checkpoint
from lisjong_arena.model import ComparisonPlan, PolicySpec
from lisjong_arena.overall_champion_aabb.protocol import resolve_binding_callable
from lisjong_arena.seed_registry import (
    load_ledger,
    require_allocation_binding,
    validate_binding_shape,
)

from .lock import _require_environment_consistent, document_identity
from .protocol import GAME_MODE, MAX_STEPS, SEED_DOMAIN
from .rust423 import (
    PAIR,
    _require_process,
    _run_block,
    execution_contract,
    require_contract,
    require_provenance,
    verify_process,
)

PROTOCOL = "arena-heuristic-candidate-423-calibration-v1"
POPULATION = "heuristic-candidate-423-calibration"
BLOCKS = 8
MAX_WORKERS = 8


def require_allocation(ledger, binding, seeds, *, arena_revision=None):
    if len(seeds) != BLOCKS or len(set(seeds)) != BLOCKS:
        raise ValueError("calibration requires exactly 8 unique seed blocks")
    record = require_allocation_binding(
        ledger,
        binding,
        seeds=seeds,
        owner_issue="lisbun/lisjong-arena#423",
        protocol=PROTOCOL,
        seed_domain=SEED_DOMAIN,
        population=POPULATION,
        split="DEVELOPMENT",
    )

    if arena_revision is not None and record["arena_revision"] != arena_revision:
        raise ValueError("calibration allocation belongs to another Arena revision")
    return record


def _identity(document):
    payload = {key: value for key, value in document.items() if key != "identity"}
    return {**payload, "identity": document_identity(payload)}


def _read(path):
    value = read_json_document(Path(path))
    if not isinstance(value, dict) or value != _identity(value):
        raise ValueError("calibration document identity mismatch")
    return value


def _write(path, document):
    write_new_artifact_file(Path(path), canonical_json_text(_identity(document)))


def _plan(seeds):
    specs = tuple(
        PolicySpec(
            identity=identity,
            factory=resolve_binding_callable(f"lisjong_arena.policy_catalog:{factory}"),
        )
        for identity, factory in PAIR
    )
    return ComparisonPlan(
        policy_a=specs[0],
        policy_b=specs[1],
        seeds=tuple(seeds),
        game_mode=GAME_MODE,
        max_steps=MAX_STEPS,
    )


def _timed_block(seed, wheel, provenance):
    # AL2023/Linux ru_maxrss is KiB; import only on the execution side (Windows
    # Collect can verify the evidence without resource or the native wheel).
    import resource

    started = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    tick = time.monotonic()
    cpu = time.process_time()
    _, evidence = _run_block(_plan((seed,)), wheel, provenance)
    return {
        "seed": seed,
        "started_at": started,
        "completed_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "duration_seconds": time.monotonic() - tick,
        "cpu_seconds": time.process_time() - cpu,
        "peak_process_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "process": evidence["process"],
        "game_native_calls": evidence["game_native_calls"],
    }


def validate_lock(value):
    raw = expect_object(
        value,
        {
            "identity",
            "protocol",
            "purpose",
            "seeds",
            "allocation_binding",
            "provenance",
            "rust_execution",
            "participants",
            "workers",
            "instance_type",
            "vcpu",
            "platform",
            "run_id",
        },
        "calibration_lock",
    )
    if (
        raw != _identity(raw)
        or raw["protocol"] != PROTOCOL
        or raw["purpose"] != "DEVELOPMENT timing only; no strength claim"
    ):
        raise ValueError("invalid calibration lock identity/protocol/purpose")
    seeds = raw["seeds"]
    if (
        not isinstance(seeds, list)
        or len(seeds) != BLOCKS
        or any(type(s) is not int or not 0 <= s < 2**32 for s in seeds)
        or len(set(seeds)) != BLOCKS
    ):
        raise ValueError("invalid calibration seeds")
    validate_binding_shape(raw["allocation_binding"], seeds=tuple(seeds))
    require_contract(raw["rust_execution"])
    require_provenance(raw["provenance"])
    if raw["participants"] != [list(pair) for pair in PAIR]:
        raise ValueError("calibration participants drifted")
    if type(raw["workers"]) is not int or not 1 <= raw["workers"] <= MAX_WORKERS:
        raise ValueError("calibration workers must be 1..8")
    if (
        raw["instance_type"] != "c7i.2xlarge"
        or raw["vcpu"] != 8
        or raw["platform"] != "Linux-x86_64"
    ):
        raise ValueError("calibration requires c7i.2xlarge / Linux x86_64 / 8 vCPU")
    if not isinstance(raw["run_id"], str) or not raw["run_id"]:
        raise ValueError("run_id missing")
    return dict(raw)


def summarize(lock, receipts):
    lock = validate_lock(lock)
    raw = expect_object(
        receipts,
        {"identity", "lock_identity", "parent", "blocks", "wall_seconds"},
        "receipts",
    )
    if raw != _identity(raw) or raw["lock_identity"] != lock["identity"]:
        raise ValueError("receipt identity/lock mismatch")
    parent = _require_process(raw["parent"])
    if parent["provenance"] != lock["provenance"]:
        raise ValueError("parent provenance mismatch")
    blocks = raw["blocks"]
    if not isinstance(blocks, list) or len(blocks) != BLOCKS:
        raise ValueError("incomplete calibration")
    durations, cpus, peaks, pids, intervals = [], [], [], set(), []
    for seed, value in zip(lock["seeds"], blocks, strict=True):
        row = expect_object(
            value,
            {
                "seed",
                "started_at",
                "completed_at",
                "duration_seconds",
                "cpu_seconds",
                "peak_process_rss_kib",
                "process",
                "game_native_calls",
            },
            "block",
        )
        if type(row["seed"]) is not int or row["seed"] != seed:
            raise ValueError("calibration seed order mismatch")
        process = _require_process(row["process"])
        if (
            process["provenance"] != lock["provenance"]
            or process["backend"]["pid"] == parent["backend"]["pid"]
        ):
            raise ValueError("worker provenance/PID mismatch")
        if type(row["game_native_calls"]) is not int or row["game_native_calls"] <= 0:
            raise ValueError("calibration worker did not execute native games")
        for name in ("duration_seconds", "cpu_seconds", "peak_process_rss_kib"):
            number = row[name]
            if (
                type(number) not in (float, int)
                or not math.isfinite(number)
                or number <= 0
            ):
                raise ValueError(f"invalid {name}")
        begin, end = (
            datetime.fromisoformat(row[key]) for key in ("started_at", "completed_at")
        )
        if (
            begin.tzinfo is None
            or end.tzinfo is None
            or abs((end - begin).total_seconds() - row["duration_seconds"]) > 1
        ):
            raise ValueError("timing interval mismatch")
        intervals.append((begin.timestamp(), end.timestamp()))
        durations.append(row["duration_seconds"])
        cpus.append(row["cpu_seconds"])
        peaks.append(row["peak_process_rss_kib"])
        pids.add(process["backend"]["pid"])
    wall = raw["wall_seconds"]
    if (
        type(wall) not in (float, int)
        or not math.isfinite(wall)
        or wall <= 0
        or wall + 1 < max(e for _, e in intervals) - min(b for b, _ in intervals)
    ):
        raise ValueError("invalid calibration wall time")
    if len(pids) > lock["workers"]:
        raise ValueError("worker count exceeds lock")
    # Reuse #340's evidence schema and same-worker wave-tail prediction.
    provenance = lock["provenance"]
    measured = build_calibration_evidence(
        calibration_run_id=lock["run_id"],
        calibrated_at=datetime.fromtimestamp(max(end for _, end in intervals), UTC),
        seed_allocation=lock["allocation_binding"],
        seed_allocation_population=POPULATION,
        seed_ledger_revision=lock["allocation_binding"]["ledger_revision"],
        arena_revision=provenance["lisjong_arena_revision"],
        lisjong_revision=provenance["lisjong_revision"],
        lisjong_engine_revision=provenance["lisjong_engine_revision"],
        riichienv_version=provenance["riichienv_version"],
        workload_identity=PROTOCOL,
        teacher_identity="AABB: " + " / ".join(pair[0] for pair in PAIR),
        game_mode=GAME_MODE,
        instance_type=lock["instance_type"],
        vcpu=lock["vcpu"],
        worker_count_requested=lock["workers"],
        workers_active_observed=len(pids),
        tasks=[
            {
                key: row[key]
                for key in ("seed", "started_at", "completed_at", "duration_seconds")
            }
            for row in blocks
        ],
        batch_scientific_wall_clock_seconds=wall,
        ec2_billable_runtime_seconds=None,
        setup_overhead_seconds=None,
        teardown_overhead_seconds=None,
        durable_evidence_level="per-seed-durable-receipt",
        instrumentation_identity=PROTOCOL,
        instrumentation_path="receipts",
        limitations=(
            "One unit is four AABB hanchan; use 100 units for formal prediction",
        ),
    )
    prediction = predict_runtime(
        measured, total_units=100, worker_count=lock["workers"], headroom_factor=1.5
    )
    return _identity(
        {
            "schema": PROTOCOL,
            "lock_identity": lock["identity"],
            "receipts_identity": raw["identity"],
            "classification": "CALIBRATION COMPLETE; NOT A STRENGTH RESULT",
            "seed_blocks": BLOCKS,
            "hanchan": 4 * BLOCKS,
            "workers_observed": len(pids),
            "wall_seconds": wall,
            "hanchan_per_hour": 4 * BLOCKS * 3600 / wall,
            "p50_seconds_per_block": measured["p50_seconds_per_unit"],
            "p90_seconds_per_block": measured["p90_seconds_per_unit"],
            "max_seconds_per_block": max(durations),
            "worker_cpu_seconds": sum(cpus),
            "max_process_rss_mib": max(peaks) / 1024,
            "provisional_400_hanchan_seconds": prediction[
                "headroom_adjusted_upper_seconds"
            ],
            "operational_calibration": measured,
            "runtime_prediction": prediction,
            "mean_worker_cpu_cores": sum(cpus) / wall,
            "limitations": [
                "8 blocks; unseen slow games may exceed the estimate",
                "same revision/instance/workers only; no automatic formal launch",
                "RSS is worker process high-water mark; not per-game allocation",
                "setup/collection/teardown costs are outside workload timing",
            ],
        }
    )


def verify(bundle):
    root = Path(bundle)
    expected = summarize(
        _read(root / "calibration-lock.json"), _read(root / "calibration-receipts.json")
    )
    if expected != _read(root / "calibration-result.json"):
        raise ValueError("calibration result differs from raw timing evidence")
    return expected


def run(*, seeds, ledger, binding, wheel, workers, run_id, output, instance_type):
    root = Path(output)
    for name in (
        "calibration-lock.json",
        "calibration-receipts.json",
        "calibration-result.json",
        "receipts",
    ):
        if (root / name).exists():
            raise ValueError("calibration destinations are not fresh")
    require_allocation(ledger, binding, tuple(seeds))
    _require_environment_consistent()
    head = require_clean_arena_head()
    require_merged_arena_revision(head, branch="main")
    require_allocation(ledger, binding, tuple(seeds), arena_revision=head)
    parent = verify_process(wheel)
    document = _identity(
        {
            "protocol": PROTOCOL,
            "purpose": "DEVELOPMENT timing only; no strength claim",
            "seeds": list(seeds),
            "allocation_binding": binding,
            "provenance": parent["provenance"],
            "rust_execution": execution_contract(str(Path(wheel).resolve())),
            "participants": [list(pair) for pair in PAIR],
            "workers": workers,
            "instance_type": instance_type,
            "vcpu": os.cpu_count(),
            "platform": f"{platform.system()}-{platform.machine()}",
            "run_id": run_id,
        }
    )
    validate_lock(document)
    if head != parent["provenance"]["lisjong_arena_revision"]:
        raise ValueError("Arena provenance mismatch")
    _write(root / "calibration-lock.json", document)
    tracker = ProgressTracker(
        run_id=run_id,
        unit_kind="hanchan",
        total_units=4 * BLOCKS,
        worker_count=workers,
        started_at=datetime.now(UTC),
    )
    write_progress(root / "progress.json", tracker.snapshot(0, now=datetime.now(UTC)))
    observations = {}
    tick = time.monotonic()
    with ProcessPoolExecutor(
        max_workers=workers, mp_context=multiprocessing.get_context("spawn")
    ) as pool:
        futures = {
            pool.submit(_timed_block, seed, wheel, parent["provenance"]): seed
            for seed in seeds
        }
        try:
            for future in as_completed(futures):
                row = future.result()
                seed = futures[future]
                if row["seed"] != seed:
                    raise ValueError("worker returned the wrong seed")
                observations[seed] = row
                publish_seed_checkpoint(
                    root / "receipts",
                    run_id=run_id,
                    seed=seed,
                    protocol_identity=document["identity"],
                    payload=row,
                    started_at=row["started_at"],
                    completed_at=row["completed_at"],
                )
                write_progress(
                    root / "progress.json",
                    tracker.snapshot(4 * len(observations), now=datetime.now(UTC)),
                )
        except BaseException:
            for future in futures:
                future.cancel()
            pool.terminate_workers()
            raise
    if verify_process(wheel) != parent or require_clean_arena_head() != head:
        raise ValueError("execution identity changed during calibration")
    receipts = _identity(
        {
            "lock_identity": document["identity"],
            "parent": parent,
            "blocks": [observations[seed] for seed in seeds],
            "wall_seconds": time.monotonic() - tick,
        }
    )
    summary = summarize(document, receipts)
    _write(root / "calibration-receipts.json", receipts)
    _write(root / "calibration-result.json", summary)
    return verify(root)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    run_parser = commands.add_parser("run")
    for name in (
        "seed-ledger",
        "allocation-binding",
        "wheel",
        "run-id",
        "output",
        "seeds",
    ):
        run_parser.add_argument(f"--{name}", required=True)
    run_parser.add_argument("--instance-type", choices=("c7i.2xlarge",), required=True)
    run_parser.add_argument("--workers", type=int, required=True)
    check = commands.add_parser("verify")
    check.add_argument("--bundle", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "verify":
            summary = verify(args.bundle)
        else:
            first, last = (int(value) for value in args.seeds.split(":"))
            if first < 0 or last >= 2**32 or last - first + 1 != BLOCKS:
                raise ValueError("calibration requires a bounded 8-seed range")
            summary = run(
                seeds=tuple(range(first, last + 1)),
                ledger=load_ledger(args.seed_ledger),
                binding=json.loads(Path(args.allocation_binding).read_text()),
                wheel=args.wheel,
                workers=args.workers,
                run_id=args.run_id,
                output=args.output,
                instance_type=args.instance_type,
            )
        print(f"result_identity={summary['identity']}")
        print(f"classification={summary['classification']}")
        print(json.dumps(summary, sort_keys=True))
        return 0
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"STOP / INVALID: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
