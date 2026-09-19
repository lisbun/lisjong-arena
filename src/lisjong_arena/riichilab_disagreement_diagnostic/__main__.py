"""Offline CLI for Issue #251 aggregate-only disagreement analysis."""

import argparse
import json
from pathlib import Path

from lisjong_arena.riichilab_corpus.models import CorpusError, snapshot_from_value
from lisjong_arena.riichilab_corpus.persistence import read_json
from lisjong_arena.riichilab_source_pilot.errors import (
    SourceIdentityError,
    SourcePilotError,
)

from .analysis import (
    OUTCOME_BLOCKED,
    OUTCOME_INVALID,
    DiagnosticBlockedError,
    DiagnosticInvalidError,
    run_diagnostic,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=("Issue #251 offline RiichiLab strong-bot disagreement diagnostic")
    )
    parser.add_argument("--snapshot", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument(
        "--arena-revision",
        required=True,
        help="exact 40-character lisjong-arena commit running this diagnostic",
    )
    return parser


def _emit(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True))


def _load_snapshot(path: str):
    return snapshot_from_value(read_json(Path(path), "recent-games snapshot"))


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        snapshot = _load_snapshot(arguments.snapshot)
    except (CorpusError, OSError, ValueError) as error:
        _emit(
            {
                "outcome": OUTCOME_INVALID,
                "reason": f"{type(error).__name__}: {error}",
            }
        )
        return 2

    try:
        report = run_diagnostic(
            snapshot,
            arguments.output_dir,
            arena_revision=arguments.arena_revision,
        )
    except (DiagnosticInvalidError, SourceIdentityError) as error:
        _emit(
            {
                "outcome": OUTCOME_INVALID,
                "reason": f"{type(error).__name__}: {error}",
            }
        )
        return 2
    except (DiagnosticBlockedError, SourcePilotError, OSError) as error:
        _emit(
            {
                "outcome": OUTCOME_BLOCKED,
                "reason": f"{type(error).__name__}: {error}",
            }
        )
        return 2

    _emit(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
