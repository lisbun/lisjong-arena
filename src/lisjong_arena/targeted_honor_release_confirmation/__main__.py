"""Issue #270 operator CLI.

Real confirmation execution is intentionally post-merge only.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .experiment import run_confirmation
from .lock import (
    TargetedHonorReleaseConfirmationLockError,
    build_lock_document,
    save_lock_document,
)
from .paired import TargetedHonorReleaseConfirmationPairedError
from .protocol import TargetedHonorReleaseConfirmationProtocolError
from .trace import TargetedHonorReleaseConfirmationTraceError


def _parse_seed_range(raw: str) -> tuple[int, ...]:
    parts = raw.split(":")
    if len(parts) != 2:
        raise argparse.ArgumentTypeError("seed range must be START:END inclusive")
    try:
        start, end = (int(part) for part in parts)
    except ValueError:
        raise argparse.ArgumentTypeError(
            "seed range endpoints must be integers"
        ) from None
    if start < 0 or end < start:
        raise argparse.ArgumentTypeError("seed range must be non-negative and ordered")
    return tuple(range(start, end + 1))


def _parse_optional_seeds(raw: str | None) -> tuple[int, ...]:
    if raw is None or not raw.strip():
        return ()
    values: list[int] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if ":" in chunk:
            values.extend(_parse_seed_range(chunk))
        else:
            try:
                value = int(chunk)
            except ValueError:
                raise argparse.ArgumentTypeError(
                    "additional allocations must be comma-separated ints/ranges"
                ) from None
            if value < 0:
                raise argparse.ArgumentTypeError(
                    "additional allocation seeds must be non-negative"
                )
            values.append(value)
    return tuple(values)


def _lock(arguments: argparse.Namespace) -> int:
    destinations = {
        "candidate_artifact": arguments.candidate_artifact,
        "parent_artifact": arguments.parent_artifact,
        "candidate_trace": arguments.candidate_trace,
        "paired_result": arguments.paired_result,
        "classified_result": arguments.classified_result,
    }
    document = build_lock_document(
        destinations=destinations,
        confirmation_seeds=arguments.confirmation_seeds,
        max_workers=arguments.workers,
        external_freshness_confirmed=arguments.external_freshness_confirmed,
        additional_allocated_seeds=_parse_optional_seeds(
            arguments.additional_allocated_seeds
        ),
    )
    path = save_lock_document(document, arguments.out)
    seeds = arguments.confirmation_seeds
    print(f"lock_identity={document['lock_identity']}")
    print(f"confirmation_seeds={seeds[0]}:{seeds[-1]}")
    print("result_exposed=false")
    print(f"lock_written={path}")
    return 0


def _run(arguments: argparse.Namespace) -> int:
    outcome = run_confirmation(lock_path=arguments.lock)
    summary = outcome.paired_result["primary_summary"]
    classification = outcome.paired_result["classification"]
    print(f"worker_count={outcome.worker_count}")
    print(f"paired_result_identity={outcome.paired_result['result_identity']}")
    print(f"classification={classification['label']}")
    print(f"primary_summary={summary}")
    print(f"classified_result={outcome.classified_result_path}")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m lisjong_arena.targeted_honor_release_confirmation",
        description="Issue #270 independent targeted honor-release confirmation",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    lock = commands.add_parser("lock", help="build the pre-execution lock")
    lock.add_argument("--out", type=Path, required=True)
    lock.add_argument("--confirmation-seeds", type=_parse_seed_range, required=True)
    lock.add_argument("--workers", type=int, required=True)
    lock.add_argument(
        "--external-freshness-confirmed",
        action="store_true",
        help="confirm public Issue and local/private seed allocations were reviewed",
    )
    lock.add_argument(
        "--additional-allocated-seeds",
        type=str,
        default=None,
        help="known external/private allocations, e.g. 900:925,1000",
    )
    lock.add_argument("--candidate-artifact", type=Path, required=True)
    lock.add_argument("--parent-artifact", type=Path, required=True)
    lock.add_argument("--candidate-trace", type=Path, required=True)
    lock.add_argument("--paired-result", type=Path, required=True)
    lock.add_argument("--classified-result", type=Path, required=True)
    lock.set_defaults(handler=_lock)

    run = commands.add_parser("run", help="run the one-shot locked confirmation")
    run.add_argument("--lock", type=Path, required=True)
    run.set_defaults(handler=_run)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        return int(arguments.handler(arguments))
    except (
        TargetedHonorReleaseConfirmationLockError,
        TargetedHonorReleaseConfirmationPairedError,
        TargetedHonorReleaseConfirmationProtocolError,
        TargetedHonorReleaseConfirmationTraceError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
