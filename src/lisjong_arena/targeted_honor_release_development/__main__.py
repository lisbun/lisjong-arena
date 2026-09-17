"""Issue #263 operator CLI.

Real execution is intentionally post-merge only:

    python -m lisjong_arena.targeted_honor_release_development lock ...
    python -m lisjong_arena.targeted_honor_release_development phase-a ...
    python -m lisjong_arena.targeted_honor_release_development phase-b ...
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .artifact import TargetedHonorReleaseArtifactError
from .experiment import run_phase_a, run_phase_b
from .lock import (
    TargetedHonorReleaseLockError,
    build_lock_document,
    save_lock_document,
)
from .paired import TargetedHonorReleasePairedError
from .protocol import TargetedHonorReleaseProtocolError


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
        "phase_a_diagnostic": arguments.phase_a_diagnostic,
        "candidate_artifact": arguments.candidate_artifact,
        "parent_artifact": arguments.parent_artifact,
        "paired_result": arguments.paired_result,
        "classified_result": arguments.classified_result,
    }
    document = build_lock_document(
        parent_artifact_path=arguments.source_parent_artifact,
        destinations=destinations,
        phase_b_seeds=arguments.phase_b_seeds,
        max_workers=arguments.workers,
        external_freshness_confirmed=arguments.external_freshness_confirmed,
        additional_allocated_seeds=_parse_optional_seeds(
            arguments.additional_allocated_seeds
        ),
    )
    path = save_lock_document(document, arguments.out)
    print(f"lock_identity={document['lock_identity']}")
    print(f"phase_b_seeds={arguments.phase_b_seeds[0]}:{arguments.phase_b_seeds[-1]}")
    print(f"lock_written={path}")
    return 0


def _phase_a(arguments: argparse.Namespace) -> int:
    outcome = run_phase_a(
        lock_path=arguments.lock,
        diagnostic_artifact_path=arguments.out,
    )
    gate = outcome.artifact["gate"]
    assert isinstance(gate, dict)
    summary = outcome.artifact["summary"]
    assert isinstance(summary, dict)
    print(f"result_identity={outcome.artifact['result_identity']}")
    print(f"artifact_written={outcome.artifact_path}")
    print(f"classification={gate['label']}")
    print(f"gate_passed={str(gate['passed']).lower()}")
    print(f"r5_activation_count={summary['r5_activation_count']}")
    print(f"action_change_count={summary['action_change_count']}")
    print(
        "projected_h_arm_wall_clock_hours="
        f"{summary['projected_h_arm_wall_clock_hours']}"
    )
    return 0 if gate["passed"] is True else 1


def _phase_b(arguments: argparse.Namespace) -> int:
    outcome = run_phase_b(
        lock_path=arguments.lock,
        diagnostic_artifact_path=arguments.phase_a_diagnostic,
        candidate_artifact_path=arguments.candidate_artifact,
        parent_artifact_path=arguments.parent_artifact,
        paired_result_path=arguments.paired_result,
        classified_result_path=arguments.classified_result,
    )
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
        prog="python -m lisjong_arena.targeted_honor_release_development",
        description="Issue #263 targeted honor-release evaluation",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    lock = commands.add_parser("lock", help="build the pre-execution lock")
    lock.add_argument("--out", type=Path, required=True)
    lock.add_argument("--source-parent-artifact", type=Path, required=True)
    lock.add_argument("--phase-b-seeds", type=_parse_seed_range, required=True)
    lock.add_argument("--workers", type=int, required=True)
    lock.add_argument(
        "--external-freshness-confirmed",
        action="store_true",
        help="confirm relevant Issues and local/private seed allocations were reviewed",
    )
    lock.add_argument(
        "--additional-allocated-seeds",
        type=str,
        default=None,
        help="known external/private allocations, e.g. 900:925,1000",
    )
    lock.add_argument("--phase-a-diagnostic", type=Path, required=True)
    lock.add_argument("--candidate-artifact", type=Path, required=True)
    lock.add_argument("--parent-artifact", type=Path, required=True)
    lock.add_argument("--paired-result", type=Path, required=True)
    lock.add_argument("--classified-result", type=Path, required=True)
    lock.set_defaults(handler=_lock)

    phase_a = commands.add_parser(
        "phase-a", help="replay #252 parent trajectory and measure #174 opportunity"
    )
    phase_a.add_argument("--lock", type=Path, required=True)
    phase_a.add_argument("--out", type=Path, required=True)
    phase_a.set_defaults(handler=_phase_a)

    phase_b = commands.add_parser(
        "phase-b", help="run the gated fresh paired development evaluation"
    )
    phase_b.add_argument("--lock", type=Path, required=True)
    phase_b.add_argument("--phase-a-diagnostic", type=Path, required=True)
    phase_b.add_argument("--candidate-artifact", type=Path, required=True)
    phase_b.add_argument("--parent-artifact", type=Path, required=True)
    phase_b.add_argument("--paired-result", type=Path, required=True)
    phase_b.add_argument("--classified-result", type=Path, required=True)
    phase_b.set_defaults(handler=_phase_b)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        return int(arguments.handler(arguments))
    except (
        TargetedHonorReleaseArtifactError,
        TargetedHonorReleaseLockError,
        TargetedHonorReleasePairedError,
        TargetedHonorReleaseProtocolError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
