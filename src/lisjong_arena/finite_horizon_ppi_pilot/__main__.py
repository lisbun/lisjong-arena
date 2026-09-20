"""CLI for the bounded Arena Issue #279 PPI feasibility pilot."""

from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from lisjong_arena._artifact_io import canonical_json_text, write_new_artifact_file

from .artifact import build_result_artifact, save_result_artifact
from .experiment import HanchanMeasurement, measure_hanchan, timing_only_measurement
from .protocol import (
    CALIBRATION_SEEDS,
    create_protocol_document,
    current_execution_identity,
    load_protocol_document,
    require_current_protocol,
    save_protocol_document,
)
from .report import save_report
from .synthetic import run_synthetic_validation, synthetic_validation_passes


def _measure_job(args: tuple[int, bool]) -> HanchanMeasurement:
    seed, include_reference = args
    return measure_hanchan(seed, include_reference=include_reference)


def _measure_many(
    seeds: tuple[int, ...], *, include_reference: bool, workers: int
) -> tuple[HanchanMeasurement, ...]:
    jobs = tuple((seed, include_reference) for seed in seeds)
    if workers == 1:
        return tuple(_measure_job(job) for job in jobs)
    with ProcessPoolExecutor(max_workers=workers) as pool:
        return tuple(pool.map(_measure_job, jobs))


def _timing_rows() -> tuple[dict[str, int | float], ...]:
    return tuple(timing_only_measurement(seed) for seed in CALIBRATION_SEEDS)


def _seed_spec(document: dict[str, object], key: str) -> tuple[int, ...]:
    value = document[key]
    if type(value) is not dict:
        raise ValueError(f"{key} is invalid")
    start, end, count = value["start"], value["end"], value["count"]
    if type(start) is not int or type(end) is not int or type(count) is not int:
        raise ValueError(f"{key} is invalid")
    seeds = tuple(range(start, end + 1))
    if len(seeds) != count:
        raise ValueError(f"{key} is inconsistent")
    return seeds


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    synthetic = sub.add_parser("synthetic", help="run synthetic validation only")
    synthetic.add_argument("--out", type=Path, required=True)
    synthetic.add_argument("--replications", type=int, default=500)

    lock = sub.add_parser("lock", help="timing-calibrate and write the real protocol lock")
    lock.add_argument("--out", type=Path, required=True)
    lock.add_argument("--total-budget-hours", type=float, required=True)
    lock.add_argument("--cheap-multiplier", type=int, default=4)
    lock.add_argument("--synthetic-replications", type=int, default=500)

    run = sub.add_parser("run", help="execute the exact locked real pilot")
    run.add_argument("--lock", type=Path, required=True)
    run.add_argument("--out", type=Path, required=True)
    run.add_argument("--report", type=Path, required=True)
    run.add_argument("--workers", type=int, default=1)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)

    if args.command == "synthetic":
        summary = run_synthetic_validation(args.replications)
        if not synthetic_validation_passes(summary):
            raise SystemExit("synthetic validation failed")
        args.out.parent.mkdir(parents=True, exist_ok=True)
        write_new_artifact_file(args.out, canonical_json_text(summary))
        print(f"synthetic_validation_passed=true out={args.out}")
        return 0

    if args.command == "lock":
        revision, provenance = current_execution_identity()
        synthetic = run_synthetic_validation(args.synthetic_replications)
        if not synthetic_validation_passes(synthetic):
            raise SystemExit("synthetic validation failed")
        timing = _timing_rows()
        protocol = create_protocol_document(
            timing_rows=timing,
            synthetic_validation=synthetic,
            total_compute_budget_seconds=args.total_budget_hours * 3600.0,
            provenance=provenance,
            arena_revision=revision,
            cheap_multiplier=args.cheap_multiplier,
        )
        save_protocol_document(protocol, args.out)
        budget = protocol["reference_measurement_budget"]
        if type(budget) is not dict:
            raise RuntimeError("protocol budget is invalid")
        print(
            f"protocol_identity={protocol['protocol_identity']} "
            f"L_max={budget['labeled_hanchans_max']} "
            f"U={protocol['cheap_sample_size']} out={args.out}"
        )
        return 0

    if args.workers < 1:
        raise SystemExit("--workers must be >= 1")
    protocol = require_current_protocol(load_protocol_document(args.lock))
    u_seeds = _seed_spec(protocol, "U_seed_specification")
    l_seeds = _seed_spec(protocol, "L_seed_specification")

    print(f"running_U={len(u_seeds)} workers={args.workers}", flush=True)
    unlabeled = _measure_many(
        u_seeds, include_reference=False, workers=args.workers
    )
    print(f"running_L={len(l_seeds)} workers={args.workers}", flush=True)
    labeled = _measure_many(
        l_seeds, include_reference=True, workers=args.workers
    )
    result = build_result_artifact(
        protocol=protocol,
        unlabeled_measurements=unlabeled,
        labeled_measurements=labeled,
    )
    save_result_artifact(result, args.out)
    save_report(result, args.report)
    print(
        f"result_identity={result['result_identity']} "
        f"artifact={args.out} report={args.report}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
