"""Issue #375 operator CLI (lock / run / verify).

real formal executionはpost-merge operator作業であり、CIやtestからは実行しない。
AWS実行ではbootstrapがこのCLIを呼び、``--progress-json``でoperational progress
(count / timingだけ)を書く。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from lisjong_arena.model import PolicySpec
from lisjong_arena.overall_champion_aabb.protocol import (
    HEURISTIC_FAMILY,
    IMPLEMENTATION_SOURCES,
    OverallChampionProtocolError,
    ParticipantBinding,
    resolve_binding_callable,
)
from lisjong_arena.progress import ProgressReporter
from lisjong_arena.seed_registry import SeedRegistryError, load_ledger

from .experiment import run_candidate_evaluation
from .lock import (
    HeuristicCandidateLockError,
    build_lock_document,
    load_lock_document,
    locked_max_workers,
    locked_participants,
    save_lock_document,
)
from .protocol import (
    HANCHAN_COUNT,
    STOP_INVALID_LABEL,
    HeuristicCandidateProtocolError,
)
from .result import HeuristicCandidateResultError, verify_candidate_bundle
from .statistics import HeuristicCandidateStatisticsError

_ERRORS = (
    HeuristicCandidateLockError,
    HeuristicCandidateProtocolError,
    HeuristicCandidateResultError,
    HeuristicCandidateStatisticsError,
    OverallChampionProtocolError,
    SeedRegistryError,
)


def _parse_seeds(raw: str) -> tuple[int, ...]:
    """``START:END``(inclusive)または``a,b,c``形式のordered seedsを読む。"""
    values: list[int] = []
    try:
        for chunk in (part.strip() for part in raw.split(",")):
            if not chunk:
                continue
            if ":" in chunk:
                start, end = (int(part) for part in chunk.split(":"))
                if start < 0 or end < start:
                    raise ValueError
                values.extend(range(start, end + 1))
            else:
                values.append(int(chunk))
    except ValueError:
        raise argparse.ArgumentTypeError(
            "seeds must be comma-separated ints or ordered START:END ranges"
        ) from None
    if not values:
        raise argparse.ArgumentTypeError("seeds must not be empty")
    return tuple(values)


def _spec_from_binding(binding: ParticipantBinding) -> PolicySpec:
    return PolicySpec(
        identity=binding.policy_identity,
        factory=resolve_binding_callable(binding.factory_binding),  # type: ignore[arg-type]
    )


def _binding(arguments: argparse.Namespace, role: str) -> ParticipantBinding:
    return ParticipantBinding(
        family=HEURISTIC_FAMILY,
        policy_identity=getattr(arguments, f"{role}_identity"),
        factory_binding=getattr(arguments, f"{role}_factory"),
        implementation_source=getattr(arguments, f"{role}_source"),
        implementation_revision=getattr(arguments, f"{role}_revision"),
    )


def _lock(arguments: argparse.Namespace) -> int:
    binding = json.loads(Path(arguments.allocation_binding).read_text(encoding="utf-8"))
    document = build_lock_document(
        destinations={
            "comparison_artifact": arguments.comparison_artifact,
            "candidate_result": arguments.candidate_result,
        },
        candidate=_binding(arguments, "candidate"),
        incumbent=_binding(arguments, "incumbent"),
        seeds=arguments.seeds,
        max_workers=arguments.workers,
        seed_ledger=load_ledger(arguments.seed_ledger),
        allocation_binding=binding,
    )
    path = save_lock_document(document, arguments.out)
    print(f"lock_identity={document['lock_identity']}")
    print(f"seed_block_count={len(arguments.seeds)}")
    print("result_exposed=false")
    print(f"lock_written={path}")
    return 0


def _operational_progress(
    path: Path, run_id: str, workers: int
) -> Callable[[int, int], None]:
    """AWS status用progress.json(count / timingだけ)を書くcallback。"""
    from lisjong_arena.aws_execution_observability import (
        ProgressTracker,
        write_progress,
    )

    tracker = ProgressTracker(
        run_id=run_id,
        unit_kind="hanchan",
        total_units=HANCHAN_COUNT,
        worker_count=workers,
        started_at=datetime.now(UTC),
    )
    write_progress(path, tracker.snapshot(0, now=datetime.now(UTC)))

    def callback(completed: int, total: int) -> None:
        write_progress(path, tracker.snapshot(completed, now=datetime.now(UTC)))

    return callback


def _run(arguments: argparse.Namespace) -> int:
    lock = load_lock_document(arguments.lock)
    candidate, incumbent = locked_participants(lock)
    callbacks: list[Callable[[int, int], None]] = []
    reporter = None
    if arguments.progress:
        reporter = ProgressReporter(HANCHAN_COUNT, stream=sys.stderr)
        callbacks.append(reporter)
    if arguments.progress_json is not None:
        if not arguments.run_id:
            raise HeuristicCandidateLockError("--progress-json requires --run-id")
        callbacks.append(
            _operational_progress(
                arguments.progress_json, arguments.run_id, locked_max_workers(lock)
            )
        )

    def progress(completed: int, total: int) -> None:
        for callback in callbacks:
            callback(completed, total)

    try:
        outcome = run_candidate_evaluation(
            lock_path=arguments.lock,
            candidate_spec=_spec_from_binding(candidate),
            incumbent_spec=_spec_from_binding(incumbent),
            seed_ledger=load_ledger(arguments.seed_ledger),
            progress_callback=progress if callbacks else None,
        )
    finally:
        if reporter is not None:
            reporter.close()
    _report(outcome.candidate_result)
    print(f"comparison_artifact={outcome.comparison_artifact_path}")
    print(f"candidate_result={outcome.candidate_result_path}")
    return 0


def _verify(arguments: argparse.Namespace) -> int:
    _report(
        verify_candidate_bundle(
            lock_path=arguments.lock,
            comparison_path=arguments.comparison,
            result_path=arguments.result,
        )
    )
    return 0


def _report(document: dict[str, object]) -> None:
    classification = document["classification"]
    primary = document["primary"]
    assert isinstance(classification, dict) and isinstance(primary, dict)
    print(f"result_identity={document['result_identity']}")
    print(f"classification={classification['label']}")
    print(f"primary_summary={json.dumps(primary['summary'], sort_keys=True)}")
    print(
        f"block_sign_counts={json.dumps(primary['block_sign_counts'], sort_keys=True)}"
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m lisjong_arena.heuristic_candidate_aabb",
        description="Issue #375 Heuristic candidate AABB half-game protocol v1",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    lock = commands.add_parser("lock", help="build the pre-execution lock")
    lock.add_argument("--out", type=Path, required=True)
    lock.add_argument("--seeds", type=_parse_seeds, required=True)
    lock.add_argument("--workers", type=int, required=True)
    lock.add_argument("--comparison-artifact", type=Path, required=True)
    lock.add_argument("--candidate-result", type=Path, required=True)
    lock.add_argument("--seed-ledger", type=Path, required=True)
    lock.add_argument("--allocation-binding", type=Path, required=True)
    for role in ("candidate", "incumbent"):
        lock.add_argument(f"--{role}-identity", required=True)
        lock.add_argument(f"--{role}-factory", required=True)
        lock.add_argument(
            f"--{role}-source", required=True, choices=sorted(IMPLEMENTATION_SOURCES)
        )
        lock.add_argument(f"--{role}-revision", required=True)
    lock.set_defaults(handler=_lock)

    run = commands.add_parser("run", help="run the one-shot locked formal event")
    run.add_argument("--lock", type=Path, required=True)
    run.add_argument("--seed-ledger", type=Path, required=True)
    run.add_argument("--progress", action="store_true")
    run.add_argument("--progress-json", type=Path, default=None)
    run.add_argument("--run-id", default=None)
    run.set_defaults(handler=_run)

    verify = commands.add_parser("verify", help="strictly verify a finished bundle")
    verify.add_argument("--lock", type=Path, required=True)
    verify.add_argument("--comparison", type=Path, required=True)
    verify.add_argument("--result", type=Path, required=True)
    verify.set_defaults(handler=_verify)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        return int(arguments.handler(arguments))
    except _ERRORS as exc:
        print(f"classification={STOP_INVALID_LABEL}", file=sys.stderr)
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
