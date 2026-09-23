"""#367 operator driver: diagnostic-only L0.3 lisjong-engine backend sweep.

This file is operational tooling only. It reruns the #366 diagnostic population
on the first-party lisjong-engine backend to measure throughput, and it is NOT a
scientific producer:

- no Seed Registry allocation (diagnostic seeds 910000..910399 are permanently
  ineligible for TRAIN / SELECT / strength evaluation)
- no outcome source, no target construction, no model
- one durable JSONL row per game; a failed game is recorded and the sweep
  continues

Per game (the #366 ``run_one()`` contract):

```text
seed        = 910000 + game_ordinal
focal seat  = engine Seat(game_ordinal % 4)
focal       = FocalExplorationPolicy(game_seed=seed, focal_seat)
others      = fresh ConstantResidualRuntime().create_policy() per seat
execution   = run_policy_hanchan(policies, seed=seed, rules=RuleSet.default())
```
"""

from __future__ import annotations

import argparse
import json
import math
import multiprocessing
import os
import subprocess
import sys
import time
import traceback
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path

FROZEN_REVISIONS = {
    "lisjong": "aed9c840bc120471e557fc0c8444965c0b81a9c3",
    "lisjong-engine": "96b9796c76ef5db8f3968f689a1ca6f3dfc9aa3b",
    "lisjong-arena": "668469910bf2e74de583c2b1ac00a77483e8b356",
}
SEED_BASE = 910000
GAME_COUNT = 400
MAX_WORKERS = 32
SEATS = ("EAST", "SOUTH", "WEST", "NORTH")
ROWS_FILENAME = "rows.jsonl"
SUMMARY_FILENAME = "summary.json"
PASS = "PASS"
FAIL = "FAIL"
RESULT_PASS = "AWS ENGINE BACKEND SWEEP PASS"
RESULT_FAILURES = "AWS ENGINE BACKEND SWEEP HAS FAILURES"
RESULT_INVALID = "STOP / INVALID"

_SPAWN = multiprocessing.get_context("spawn")


class SweepError(RuntimeError):
    """#367 operator precondition failure (STOP / INVALID)."""


def diagnostic_population(count: int = GAME_COUNT) -> list[tuple[int, int, int]]:
    """``(game_ordinal, seed, focal_seat_index)`` for the frozen #366 population."""
    return [(ordinal, SEED_BASE + ordinal, ordinal % 4) for ordinal in range(count)]


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _play(seed: int, focal_seat_index: int) -> tuple[object, tuple[object, ...]]:
    """One hanchan through the existing Arena lisjong-engine Policy bridge."""
    from lisjong.learning import ConstantResidualRuntime
    from lisjong_engine.rules import RuleSet
    from lisjong_engine.seat import Seat as EngineSeat

    from lisjong_arena.focal_outcome_source import FocalExplorationPolicy
    from lisjong_arena.lisjong_engine.domain_conversion import seat_from_engine_seat
    from lisjong_arena.lisjong_engine.hanchan import run_policy_hanchan

    focal = tuple(EngineSeat)[focal_seat_index]
    adapter = FocalExplorationPolicy(
        game_seed=seed, focal_seat=seat_from_engine_seat(focal)
    )
    policies = {
        seat: adapter if seat is focal else ConstantResidualRuntime().create_policy()
        for seat in EngineSeat
    }
    completed = run_policy_hanchan(policies, seed=seed, rules=RuleSet.default())
    return completed, adapter.captures


def run_one(game_ordinal: int, seed: int, focal_seat_index: int, play=_play) -> dict:
    """Run one diagnostic hanchan and return its row; never raises for a game."""
    started_at = _utc_now()
    started = time.perf_counter()
    row: dict[str, object] = {
        "focal_seat": SEATS[focal_seat_index],
        "focal_seat_index": focal_seat_index,
        "game_ordinal": game_ordinal,
        "pid": os.getpid(),
        "seed": seed,
        "started_at": started_at,
    }
    try:
        completed, captures = play(seed, focal_seat_index)
        multi = sum(
            1
            for capture in captures
            if capture.selection.survivors is not None
            and len(capture.selection.survivors) >= 2
        )
        row.update(
            completed_round_count=len(completed.history),
            end_reason=completed.end_reason.value,
            focal_decision_count=len(captures),
            focal_multi_survivor_decision_count=multi,
            status=PASS,
        )
    except Exception as error:  # a game failure is evidence, not a stop
        row.update(
            exception_class=f"{type(error).__module__}.{type(error).__qualname__}",
            exception_message=str(error),
            status=FAIL,
            traceback="".join(traceback.format_exception(error)),
        )
    row["duration_seconds"] = time.perf_counter() - started
    row["completed_at"] = _utc_now()
    return row


def _append_row(handle, row: dict) -> None:
    handle.write(json.dumps(row, sort_keys=True) + "\n")
    handle.flush()
    os.fsync(handle.fileno())


def _progress_writer(path: Path, run_id: str, total: int, workers: int):
    from lisjong_arena.aws_execution_observability import (
        ProgressTracker,
        write_progress,
    )

    tracker = ProgressTracker(
        run_id=run_id,
        unit_kind="hanchan",
        total_units=total,
        worker_count=workers,
        started_at=datetime.now(UTC),
    )
    write_progress(path, tracker.snapshot(0, now=datetime.now(UTC)))
    return lambda completed: write_progress(
        path, tracker.snapshot(completed, now=datetime.now(UTC))
    )


def sweep(
    output_dir,
    *,
    workers: int,
    population=None,
    run_id: str = "local",
    executor_factory=None,
    task=run_one,
    progress: bool = True,
) -> dict:
    """Run the population in spawn workers, appending one durable row per game.

    Rows are appended (flush + fsync) in completion order as each game finishes,
    so a later crash never erases earlier evidence. A worker-process crash is
    recorded as a FAIL row for every game it took down. Returns the summary.
    """
    if type(workers) is not int or not 1 <= workers <= MAX_WORKERS:
        raise SweepError(f"workers must be an int from 1 through {MAX_WORKERS}")
    population = diagnostic_population() if population is None else population
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    rows_path = output_dir / ROWS_FILENAME
    if rows_path.exists() or (output_dir / SUMMARY_FILENAME).exists():
        raise FileExistsError(f"refusing to overwrite: {rows_path}")
    tick = None
    if progress:
        tick = _progress_writer(
            output_dir / "progress.json", run_id, len(population), workers
        )
    if executor_factory is None:

        def executor_factory(max_workers):
            return ProcessPoolExecutor(max_workers=max_workers, mp_context=_SPAWN)

    sweep_started_at = _utc_now()
    started = time.perf_counter()
    with open(rows_path, "x", encoding="utf-8", newline="\n") as handle:
        executor = executor_factory(min(workers, len(population)))
        try:
            futures = {
                executor.submit(task, ordinal, seed, seat): (ordinal, seed, seat)
                for ordinal, seed, seat in population
            }
            for completed, future in enumerate(as_completed(futures), start=1):
                try:
                    row = future.result()
                except Exception as error:  # worker process died (not a game error)
                    ordinal, seed, seat = futures[future]
                    row = {
                        "exception_class": (
                            f"{type(error).__module__}.{type(error).__qualname__}"
                        ),
                        "exception_message": str(error),
                        "focal_seat": SEATS[seat],
                        "focal_seat_index": seat,
                        "game_ordinal": ordinal,
                        "seed": seed,
                        "status": FAIL,
                        "traceback": "".join(traceback.format_exception(error)),
                    }
                _append_row(handle, row)
                if tick is not None:
                    tick(completed)
        finally:
            executor.shutdown(wait=True, cancel_futures=True)
    wall = time.perf_counter() - started
    summary = summarize(
        read_rows(rows_path),
        population=population,
        workers=workers,
        workload_seconds=wall,
        workload_started_at=sweep_started_at,
        workload_completed_at=_utc_now(),
    )
    _write_json(output_dir / SUMMARY_FILENAME, summary)
    return summary


def read_rows(path) -> list[dict]:
    rows = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def _nearest_rank(sorted_values: list[float], fraction: float) -> float | None:
    if not sorted_values:
        return None
    rank = max(1, math.ceil(fraction * len(sorted_values)))
    return sorted_values[rank - 1]


def _concurrency(rows: list[dict]) -> dict[str, float] | None:
    """Peak / time-weighted mean of games in flight, from worker timestamps."""
    intervals = [
        (
            datetime.fromisoformat(row["started_at"]),
            datetime.fromisoformat(row["completed_at"]),
        )
        for row in rows
        if "started_at" in row and "completed_at" in row
    ]
    if not intervals:
        return None
    events = sorted(
        [(start, 1) for start, _ in intervals] + [(end, -1) for _, end in intervals],
        key=lambda event: (event[0], event[1]),
    )
    first, last = events[0][0], events[-1][0]
    span = (last - first).total_seconds()
    active = peak = 0
    weighted = 0.0
    previous = first
    for moment, delta in events:
        weighted += active * (moment - previous).total_seconds()
        previous = moment
        active += delta
        peak = max(peak, active)
    return {
        "mean": round(weighted / span, 3) if span > 0 else float(peak),
        "peak": peak,
    }


def summarize(
    rows: list[dict],
    *,
    population=None,
    workers: int | None = None,
    workload_seconds: float | None = None,
    workload_started_at: str | None = None,
    workload_completed_at: str | None = None,
) -> dict:
    """Run summary. Any deviation from the frozen population is STOP / INVALID."""
    population = diagnostic_population() if population is None else population
    expected = {ordinal: (seed, seat) for ordinal, seed, seat in population}
    ordinals = [row["game_ordinal"] for row in rows]
    contract_errors = []
    if sorted(ordinals) != sorted(expected):
        contract_errors.append("rows are not exactly one per population game")
    for row in rows:
        if expected.get(row["game_ordinal"]) != (
            row["seed"],
            row["focal_seat_index"],
        ):
            contract_errors.append(
                f"game {row['game_ordinal']} seed / focal seat differs from population"
            )
        if row["status"] not in (PASS, FAIL):
            contract_errors.append(f"game {row['game_ordinal']} has unknown status")
    passed = [row for row in rows if row["status"] == PASS]
    failed = [row for row in rows if row["status"] == FAIL]
    durations = sorted(
        row["duration_seconds"] for row in rows if "duration_seconds" in row
    )
    failures = Counter(
        (row["exception_class"], row["exception_message"]) for row in failed
    )
    seats = Counter(row["focal_seat"] for row in rows)
    if contract_errors:
        result = RESULT_INVALID
    elif failed:
        result = RESULT_FAILURES
    else:
        result = RESULT_PASS
    throughput = None
    if workload_seconds:
        throughput = round(len(rows) / workload_seconds * 3600.0, 2)
    return {
        "attempted": len(rows),
        "concurrency": _concurrency(rows),
        "contract_errors": contract_errors,
        "diagnostic_seeds": [min(expected.values())[0], max(expected.values())[0]],
        "end_reasons": dict(sorted(Counter(r["end_reason"] for r in passed).items())),
        "fail": len(failed),
        "failed_games": sorted(row["game_ordinal"] for row in failed),
        "failures": [
            {"count": count, "exception_class": cls, "exception_message": msg}
            for (cls, msg), count in sorted(failures.items())
        ],
        "focal_decisions": sum(row["focal_decision_count"] for row in passed),
        "focal_multi_survivor_decisions": sum(
            row["focal_multi_survivor_decision_count"] for row in passed
        ),
        "focal_seat_counts": {seat: seats[seat] for seat in SEATS},
        "game_duration_seconds": {
            "max": durations[-1] if durations else None,
            "mean": sum(durations) / len(durations) if durations else None,
            "p50": _nearest_rank(durations, 0.5),
            "p90": _nearest_rank(durations, 0.9),
        },
        "pass": len(passed),
        "result": result,
        "scientific": False,
        "throughput_hanchan_per_hour": throughput,
        "workers": workers,
        "workload_completed_at": workload_completed_at,
        "workload_seconds": workload_seconds,
        "workload_started_at": workload_started_at,
    }


# ---------------------------------------------------------------------------
# environment identity
# ---------------------------------------------------------------------------


def installed_revision(distribution: str) -> str:
    text = metadata.distribution(distribution).read_text("direct_url.json")
    if text is None:
        raise SweepError(f"{distribution}: direct_url.json is missing")
    revision = json.loads(text).get("vcs_info", {}).get("commit_id")
    if type(revision) is not str:
        raise SweepError(f"{distribution}: not installed from an exact VCS revision")
    return revision


def verify_environment(arena_checkout) -> dict[str, str]:
    """Frozen revisions: clean Arena checkout imported, exact lisjong / engine."""
    checkout = Path(arena_checkout).resolve()

    def git(*args):
        return subprocess.run(
            ["git", "-C", str(checkout), *args],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    import lisjong_arena

    revisions = {
        "lisjong": installed_revision("lisjong"),
        "lisjong-engine": installed_revision("lisjong-engine"),
        "lisjong-arena": git("rev-parse", "HEAD"),
    }
    if revisions != FROZEN_REVISIONS:
        raise SweepError(f"revisions differ from the frozen set: {revisions}")
    if git("status", "--porcelain", "--untracked-files=no"):
        raise SweepError("Arena checkout is not clean")
    if not Path(lisjong_arena.__file__).resolve().is_relative_to(checkout / "src"):
        raise SweepError("lisjong_arena is not imported from the frozen checkout")
    return revisions


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    verify = commands.add_parser("verify-environment")
    verify.add_argument("--arena-checkout", required=True)
    run = commands.add_parser("sweep")
    run.add_argument("--arena-checkout", required=True)
    run.add_argument("--output-dir", required=True)
    run.add_argument("--workers", type=int, required=True)
    run.add_argument("--run-id", default="local")
    summary = commands.add_parser("summarize")
    summary.add_argument("--rows", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "summarize":
            report = summarize(read_rows(args.rows))
        else:
            revisions = verify_environment(args.arena_checkout)
            if args.command == "verify-environment":
                print(json.dumps(revisions, sort_keys=True))
                return 0
            output_dir = Path(args.output_dir)
            output_dir.mkdir(parents=True, exist_ok=True)
            _write_json(output_dir / "revisions.json", revisions)
            report = sweep(
                output_dir,
                workers=args.workers,
                run_id=args.run_id,
            )
    except (SweepError, FileExistsError, subprocess.CalledProcessError) as error:
        print(f"{RESULT_INVALID}: {error}", file=sys.stderr)
        return 2
    print(json.dumps(report, sort_keys=True))
    if report["result"] == RESULT_PASS:
        return 0
    return 3 if report["result"] == RESULT_FAILURES else 2


if __name__ == "__main__":
    raise SystemExit(main())
