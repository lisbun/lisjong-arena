"""#385 operator CLI（lock / verify-lock / run / verify）。

real formal execution（Step G）はpost-merge operator作業であり、CIやtestからは
実行しない。progressは件数だけを出し、outcomeはresult書き出しまで表示しない。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from lisjong_arena.progress import ProgressReporter
from lisjong_arena.seed_registry import SeedRegistryError, load_ledger

from .execution import PairedStrengthExecutionError
from .experiment import run_paired_strength
from .lock import (
    PairedStrengthLockError,
    build_lock_document,
    load_lock_document,
    require_live_target,
    save_lock_document,
)
from .protocol import (
    HANCHAN_COUNT,
    PROTOCOL_ID,
    STOP_INVALID_LABEL,
    PairedStrengthProtocolError,
)
from .result import PairedStrengthResultError, verify_result

_ERRORS = (
    PairedStrengthExecutionError,
    PairedStrengthLockError,
    PairedStrengthProtocolError,
    PairedStrengthResultError,
    SeedRegistryError,
)


def _lock(arguments: argparse.Namespace) -> int:
    binding = json.loads(Path(arguments.allocation_binding).read_text(encoding="utf-8"))
    document = build_lock_document(
        artifact_path=arguments.artifact,
        seed_ledger=load_ledger(arguments.seed_ledger),
        allocation_binding=binding,
        result_destination=arguments.result,
    )
    path = save_lock_document(document, arguments.out)
    print(f"protocol_id={PROTOCOL_ID}")
    print(f"lock_identity={document['lock_identity']}")
    print("result_exposed=false")
    print(f"lock_written={path}")
    return 0


def _verify_lock(arguments: argparse.Namespace) -> int:
    document = require_live_target(
        load_lock_document(arguments.lock),
        artifact_path=arguments.artifact,
        seed_ledger=load_ledger(arguments.seed_ledger),
    )
    print(f"lock_identity={document['lock_identity']}")
    print("result_exposed=false")
    print("LOCK STRICT READBACK PASS")
    return 0


def _run(arguments: argparse.Namespace) -> int:
    reporter = (
        ProgressReporter(HANCHAN_COUNT, stream=sys.stderr)
        if arguments.progress
        else None
    )
    try:
        outcome = run_paired_strength(
            lock_path=arguments.lock,
            artifact_path=arguments.artifact,
            seed_ledger=load_ledger(arguments.seed_ledger),
            max_workers=arguments.workers,
            progress_callback=reporter,
        )
    finally:
        if reporter is not None:
            reporter.close()
    _report(outcome.result)
    print(f"result={outcome.result_path}")
    return 0


def _verify(arguments: argparse.Namespace) -> int:
    _report(verify_result(lock_path=arguments.lock, result_path=arguments.result))
    return 0


def _report(document: dict[str, object]) -> None:
    classification = document["classification"]
    primary = document["primary"]
    assert isinstance(classification, dict) and isinstance(primary, dict)
    print(f"result_identity={document['result_identity']}")
    print(f"classification={classification['label']}")
    print(f"primary_summary_pt={json.dumps(primary['summary_pt'], sort_keys=True)}")
    print(
        f"block_sign_counts={json.dumps(primary['block_sign_counts'], sort_keys=True)}"
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m lisjong_arena.l03_paired_strength",
        description=f"#385 {PROTOCOL_ID}",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    lock = commands.add_parser("lock", help="build the pre-execution lock")
    lock.add_argument("--out", type=Path, required=True)
    lock.add_argument("--artifact", type=Path, required=True)
    lock.add_argument("--seed-ledger", type=Path, required=True)
    lock.add_argument("--allocation-binding", type=Path, required=True)
    lock.add_argument("--result", type=Path, required=True)
    lock.set_defaults(handler=_lock)

    verify_lock = commands.add_parser(
        "verify-lock", help="strict lock readback against the live target"
    )
    verify_lock.add_argument("--lock", type=Path, required=True)
    verify_lock.add_argument("--artifact", type=Path, required=True)
    verify_lock.add_argument("--seed-ledger", type=Path, required=True)
    verify_lock.set_defaults(handler=_verify_lock)

    run = commands.add_parser("run", help="Step G: run the one-shot locked event")
    run.add_argument("--lock", type=Path, required=True)
    run.add_argument("--artifact", type=Path, required=True)
    run.add_argument("--seed-ledger", type=Path, required=True)
    run.add_argument("--workers", type=int, required=True)
    run.add_argument("--progress", action="store_true")
    run.set_defaults(handler=_run)

    verify = commands.add_parser("verify", help="strictly verify a finished result")
    verify.add_argument("--lock", type=Path, required=True)
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
