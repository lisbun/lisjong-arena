"""Offline CLI for the Issue #503 impact-range count."""

import argparse
import json

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

from .impact import run_impact_count


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Issue #503 offline impact count")
    parser.add_argument("--dev-history", required=True)
    parser.add_argument("--baseline-history", required=True)
    parser.add_argument("--arena-revision", required=True)
    parser.add_argument("--lisjong-revision", required=True)
    parser.add_argument(
        "--limit-games",
        type=int,
        default=None,
        help="smoke only: stop after this many games per bot (skips locked checks)",
    )
    return parser


def _emit(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        report = run_impact_count(
            {"dev": arguments.dev_history, "baseline": arguments.baseline_history},
            arena_revision=arguments.arena_revision,
            lisjong_revision=arguments.lisjong_revision,
            limit_games=arguments.limit_games,
        )
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
