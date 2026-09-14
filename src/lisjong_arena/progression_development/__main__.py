"""Issue #252 two-phase evaluationのoperator CLI。

```text
python -m lisjong_arena.progression_development lock    --out LOCK ...
python -m lisjong_arena.progression_development phase-a --lock LOCK --out RECORD
python -m lisjong_arena.progression_development phase-b --lock LOCK --feasibility-record RECORD ...
```

real Phase A / Phase B executionはmerge後のoperator作業である。CLIは
非対話でありpromptもretryも持たない。``lock``はreviewed merged mainでだけ
成立し、``phase-a`` / ``phase-b``はそのlockをconsumeして初めて実行できる。
lockが無い、lockと違うdestinationを指定した、live execution targetがlocked
revisionと違う、のいずれでもrunnerを1度も呼ばずにfail closedする。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .experiment import run_phase_b_development
from .feasibility import FeasibilityError, run_phase_a_feasibility
from .lock import ProgressionLockError, build_lock_document, save_lock_document
from .paired import PairedResultError
from .protocol import INFEASIBLE_LABEL, ProgressionProtocolError


def _logical_cpu_count() -> int:
    return os.cpu_count() or 1


def _lock(arguments: argparse.Namespace) -> int:
    document = build_lock_document(
        {
            "feasibility_record": arguments.feasibility_record,
            "candidate_artifact": arguments.candidate_artifact,
            "parent_artifact": arguments.parent_artifact,
            "paired_result": arguments.paired_result,
        },
        branch=arguments.branch,
    )
    path = save_lock_document(document, arguments.out)
    print(f"lock_identity={document['lock_identity']}")
    print(f"lock_written={path}")
    return 0


def _phase_a(arguments: argparse.Namespace) -> int:
    record = run_phase_a_feasibility(
        lock_path=arguments.lock,
        destination=arguments.out,
        logical_cpu_count=_logical_cpu_count(),
        max_steps=arguments.max_steps,
    )
    print(f"record_identity={record.record_identity}")
    print(f"record_written={arguments.out}")
    print(f"gate_passed={str(record.gate.gate_passed).lower()}")
    if record.gate.gate_passed:
        print(f"selected_worker_count={record.gate.selected_worker_count}")
        print(
            "projected_phase_b_arm_wall_clock_hours="
            f"{record.gate.projected_phase_b_arm_wall_clock_hours}"
        )
        return 0
    print(f"classification={INFEASIBLE_LABEL}")
    for reason in record.gate.failure_reasons:
        print(f"failure_reason={reason}")
    return 1


def _phase_b(arguments: argparse.Namespace) -> int:
    outcome = run_phase_b_development(
        lock_path=arguments.lock,
        feasibility_record_path=arguments.feasibility_record,
        candidate_artifact_path=arguments.candidate_artifact,
        parent_artifact_path=arguments.parent_artifact,
        paired_result_path=arguments.paired_result,
        max_steps=arguments.max_steps,
    )
    summary = outcome.paired_result["primary_summary"]
    print(f"worker_count={outcome.worker_count}")
    print(f"result_identity={outcome.paired_result['result_identity']}")
    print(f"classification={outcome.classification['label']}")
    print(f"primary_summary={summary}")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m lisjong_arena.progression_development",
        description="Issue #252 progression development evaluation",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    lock_parser = subparsers.add_parser("lock", help="build the pre-execution lock")
    lock_parser.add_argument("--out", type=Path, required=True)
    lock_parser.add_argument("--feasibility-record", type=Path, required=True)
    lock_parser.add_argument("--candidate-artifact", type=Path, required=True)
    lock_parser.add_argument("--parent-artifact", type=Path, required=True)
    lock_parser.add_argument("--paired-result", type=Path, required=True)
    lock_parser.add_argument("--branch", default="main")
    lock_parser.set_defaults(handler=_lock)

    phase_a_parser = subparsers.add_parser(
        "phase-a", help="run the locked technical feasibility worker sweep"
    )
    phase_a_parser.add_argument("--lock", type=Path, required=True)
    phase_a_parser.add_argument("--out", type=Path, required=True)
    phase_a_parser.add_argument("--max-steps", type=int, default=10_000)
    phase_a_parser.set_defaults(handler=_phase_a)

    phase_b_parser = subparsers.add_parser(
        "phase-b", help="run the locked and gated paired development evaluation"
    )
    phase_b_parser.add_argument("--lock", type=Path, required=True)
    phase_b_parser.add_argument("--feasibility-record", type=Path, required=True)
    phase_b_parser.add_argument("--candidate-artifact", type=Path, required=True)
    phase_b_parser.add_argument("--parent-artifact", type=Path, required=True)
    phase_b_parser.add_argument("--paired-result", type=Path, required=True)
    phase_b_parser.add_argument("--max-steps", type=int, default=10_000)
    phase_b_parser.set_defaults(handler=_phase_b)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        return int(arguments.handler(arguments))
    except (
        FeasibilityError,
        PairedResultError,
        ProgressionLockError,
        ProgressionProtocolError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
