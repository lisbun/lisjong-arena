"""Operator CLI for snapshot -> plan -> bounded acquisition -> validation/report.

`qualify-downstream`だけはIssue #203のoffline qualificationであり、network
acquisition pathを一切呼ばない。この`__main__`はoperator entry pointとしての
composition rootであり、`riichilab_corpus`のlibrary moduleから
`riichilab_downstream_qualification`へ依存しない（依存方向は
qualification -> corpusの一方向のままである）。
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from lisjong_arena.riichilab_corpus.acquisition import (
    REPORT_FILENAME,
    acquire_from_plan,
    create_acquisition_plan,
    plan_from_value,
    validate_cached_corpus,
)
from lisjong_arena.riichilab_corpus.api import snapshot_recent_games
from lisjong_arena.riichilab_corpus.http import StdlibHttpTransport
from lisjong_arena.riichilab_corpus.models import (
    MAX_ACQUISITION_CEILING,
    RecentGamesSnapshot,
    canonical_json_bytes,
    snapshot_from_value,
)
from lisjong_arena.riichilab_corpus.persistence import (
    ensure_outside_git_worktree,
    read_json,
    write_new_json,
)
from lisjong_arena.riichilab_downstream_qualification.classification import (
    OverallOutcome,
)
from lisjong_arena.riichilab_downstream_qualification.qualification import (
    qualify_local_corpus,
)
from lisjong_arena.riichilab_downstream_qualification.report import write_report


def _load_snapshot(path: Path) -> RecentGamesSnapshot:
    return snapshot_from_value(read_json(path, "recent-games snapshot"))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Bounded RiichiLab server-side MJAI corpus for personal, "
            "non-commercial local research"
        )
    )
    commands = parser.add_subparsers(dest="command", required=True)

    snapshot = commands.add_parser(
        "snapshot", help="fetch only current target-bot recent-game metadata"
    )
    snapshot.add_argument("--output", type=Path, required=True)
    snapshot.add_argument("--timeout", type=float, default=15.0)

    plan = commands.add_parser(
        "plan", help="dry-run cache inspection and lock an explicit acquisition ceiling"
    )
    plan.add_argument("--snapshot", type=Path, required=True)
    plan.add_argument("--output-dir", type=Path, required=True)
    plan.add_argument("--ceiling", type=int, required=True)
    plan.add_argument("--plan-output", type=Path, required=True)

    acquire = commands.add_parser(
        "acquire", help="execute exactly one previously reviewed bounded plan"
    )
    acquire.add_argument("--snapshot", type=Path, required=True)
    acquire.add_argument("--plan", type=Path, required=True)
    acquire.add_argument("--confirm-plan-identity", required=True)
    acquire.add_argument("--timeout", type=float, default=15.0)

    validate = commands.add_parser(
        "validate", help="strictly revalidate cached bytes for one snapshot"
    )
    validate.add_argument("--snapshot", type=Path, required=True)
    validate.add_argument("--output-dir", type=Path, required=True)

    report = commands.add_parser(
        "report", help="print the most recent local completion report"
    )
    report.add_argument("--output-dir", type=Path, required=True)

    qualify = commands.add_parser(
        "qualify-downstream",
        help=(
            "offline Issue #203 downstream reconstruction qualification "
            "against the locked local corpus"
        ),
    )
    qualify.add_argument("--snapshot", type=Path, required=True)
    qualify.add_argument("--output-dir", type=Path, required=True)
    qualify.add_argument("--report-output", type=Path, required=True)
    return parser


def _print(value: object) -> None:
    print(
        json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2)
    )


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if arguments.command == "snapshot":
        output = (
            ensure_outside_git_worktree(arguments.output.parent) / arguments.output.name
        )
        result = snapshot_recent_games(StdlibHttpTransport(), timeout=arguments.timeout)
        write_new_json(output, result.to_value())
        _print(result.to_value())
        return 0

    if arguments.command == "plan":
        snapshot = _load_snapshot(arguments.snapshot)
        result = create_acquisition_plan(
            snapshot, arguments.output_dir, requested_ceiling=arguments.ceiling
        )
        output = (
            ensure_outside_git_worktree(arguments.plan_output.parent)
            / arguments.plan_output.name
        )
        write_new_json(output, result.to_value())
        _print(result.to_value())
        return 0

    if arguments.command == "acquire":
        snapshot = _load_snapshot(arguments.snapshot)
        plan = plan_from_value(read_json(arguments.plan, "acquisition plan"))
        if arguments.confirm_plan_identity != plan.plan_identity:
            raise SystemExit("--confirm-plan-identity does not match the reviewed plan")
        _print(
            acquire_from_plan(
                snapshot, plan, StdlibHttpTransport(), timeout=arguments.timeout
            )
        )
        return 0

    if arguments.command == "validate":
        _print(
            validate_cached_corpus(
                _load_snapshot(arguments.snapshot), arguments.output_dir
            )
        )
        return 0

    if arguments.command == "qualify-downstream":
        # Issue #203はofflineのみである。snapshot / acquire pathと
        # `StdlibHttpTransport`はこのbranchから呼ばない。
        result = qualify_local_corpus(
            _load_snapshot(arguments.snapshot), arguments.output_dir
        )
        # `write_report`がGit worktree外であることを再確認し、既存fileを
        # 上書きせずに書き出す。
        write_report(arguments.report_output, result)
        _print(result.to_value())
        return 0 if result.overall_outcome is not OverallOutcome.STOP_INVALID else 1

    output = ensure_outside_git_worktree(arguments.output_dir)
    report = read_json(output / REPORT_FILENAME, "acquisition report")
    # Re-serialization also rejects any non-JSON value supplied by a future reader.
    canonical_json_bytes(report)
    _print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main", "MAX_ACQUISITION_CEILING"]
