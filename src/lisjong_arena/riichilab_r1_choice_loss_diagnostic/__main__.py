"""Offline CLI for the Issue #483 aggregate-only R1 choice / loss diagnostic."""

import argparse
import json
from pathlib import Path

from lisjong_arena.riichilab_corpus.models import CorpusError, snapshot_from_value
from lisjong_arena.riichilab_corpus.persistence import read_json
from lisjong_arena.riichilab_disagreement_diagnostic.analysis import (
    OUTCOME_BLOCKED,
    OUTCOME_INVALID,
    DiagnosticBlockedError,
    DiagnosticInvalidError,
)
from lisjong_arena.riichilab_source_pilot.errors import (
    SourceIdentityError,
    SourcePilotError,
)

from .champion_play import run_champion_play
from .strong_bots import run_strong_bots


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Issue #483 offline R1 choice / loss diagnostic"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    strong = commands.add_parser("strong-bots", help="exact #170 corpus side")
    strong.add_argument("--snapshot", required=True)
    strong.add_argument("--output-dir", required=True)
    champion = commands.add_parser("champion-play", help="#441 self-history side")
    champion.add_argument("--dev-history", required=True)
    champion.add_argument("--baseline-history", required=True)
    for command in (strong, champion):
        command.add_argument(
            "--arena-revision",
            required=True,
            help="exact 40-character lisjong-arena commit running this diagnostic",
        )
    return parser


def _emit(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True))


def _run(arguments: argparse.Namespace) -> dict[str, object]:
    if arguments.command == "champion-play":
        return run_champion_play(
            {"dev": arguments.dev_history, "baseline": arguments.baseline_history},
            arena_revision=arguments.arena_revision,
        )
    try:
        snapshot = snapshot_from_value(
            read_json(Path(arguments.snapshot), "recent-games snapshot")
        )
    except (CorpusError, OSError, ValueError) as error:
        raise DiagnosticInvalidError(f"{type(error).__name__}: {error}") from error
    return run_strong_bots(
        snapshot, arguments.output_dir, arena_revision=arguments.arena_revision
    )


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        report = _run(arguments)
    except (DiagnosticInvalidError, SourceIdentityError) as error:
        _emit(
            {"outcome": OUTCOME_INVALID, "reason": f"{type(error).__name__}: {error}"}
        )
        return 2
    except (DiagnosticBlockedError, SourcePilotError, OSError) as error:
        _emit(
            {"outcome": OUTCOME_BLOCKED, "reason": f"{type(error).__name__}: {error}"}
        )
        return 2

    _emit(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
