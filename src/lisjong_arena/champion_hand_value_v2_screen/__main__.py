"""Operator CLI for Issue #297."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .evidence import ChampionHandValueV2EvidenceError
from .experiment import run_screen
from .lock import (
    ChampionHandValueV2LockError,
    build_lock_document,
    save_lock_document,
)
from .protocol import ChampionHandValueV2ProtocolError
from .trace import ChampionHandValueV2TraceError


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
        raise argparse.ArgumentTypeError(
            "seed range must be non-negative and ordered"
        )
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
        "strength_artifact": arguments.strength_artifact,
        "composition_trace": arguments.composition_trace,
        "result": arguments.result,
        "classified_result": arguments.classified_result,
    }
    document = build_lock_document(
        destinations=destinations,
        seeds=arguments.seeds,
        max_workers=arguments.workers,
        external_freshness_confirmed=arguments.external_freshness_confirmed,
        additional_allocated_seeds=_parse_optional_seeds(
            arguments.additional_allocated_seeds
        ),
    )
    path = save_lock_document(document, arguments.out)
    seeds = arguments.seeds
    print(f"lock_identity={document['lock_identity']}")
    print(f"seeds={seeds[0]}:{seeds[-1]}")
    print(f"workers={arguments.workers}")
    print("result_exposed=false")
    print(f"lock_written={path}")
    return 0


def _progress(completed: int, total: int) -> None:
    if completed == total or completed == 1 or completed % 10 == 0:
        print(
            f"progress={completed}/{total} ({100.0 * completed / total:.1f}%)",
            flush=True,
        )


def _run(arguments: argparse.Namespace) -> int:
    outcome = run_screen(
        lock_path=arguments.lock,
        progress_callback=_progress,
    )
    summary = outcome.result["primary_summary"]
    classification = outcome.classified_result["classification"]
    print(f"worker_count={outcome.worker_count}")
    print(f"classification={classification}")
    print(f"primary_summary={summary}")
    print(f"result_identity={outcome.result['result_identity']}")
    print(f"classified_result={outcome.classified_result_path}")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m lisjong_arena.champion_hand_value_v2_screen",
        description="Issue #297 Champion + HandValue v2 bounded ABBB screen",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    lock = commands.add_parser("lock", help="create the pre-execution lock")
    lock.add_argument("--out", type=Path, required=True)
    lock.add_argument("--seeds", type=_parse_seed_range, required=True)
    lock.add_argument("--workers", type=int, required=True)
    lock.add_argument(
        "--external-freshness-confirmed",
        action="store_true",
        help="confirm public Issue plus local/private seed audit completed",
    )
    lock.add_argument(
        "--additional-allocated-seeds",
        type=str,
        default=None,
        help="known local/private allocations, e.g. 60000:60020,61000",
    )
    lock.add_argument("--strength-artifact", type=Path, required=True)
    lock.add_argument("--composition-trace", type=Path, required=True)
    lock.add_argument("--result", type=Path, required=True)
    lock.add_argument("--classified-result", type=Path, required=True)
    lock.set_defaults(handler=_lock)

    run = commands.add_parser("run", help="run the one-shot locked 400-game screen")
    run.add_argument("--lock", type=Path, required=True)
    run.set_defaults(handler=_run)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        return int(arguments.handler(arguments))
    except (
        ChampionHandValueV2EvidenceError,
        ChampionHandValueV2LockError,
        ChampionHandValueV2ProtocolError,
        ChampionHandValueV2TraceError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
