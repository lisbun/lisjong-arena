"""Operator CLI for offline analysis and separately bounded enrichment."""

from __future__ import annotations

import argparse
import sys

from lisjong_arena.riichilab_longitudinal.analysis import (
    AnalysisFilters,
    build_summary,
    load_games,
)
from lisjong_arena.riichilab_longitudinal.artifact import write_artifacts
from lisjong_arena.riichilab_longitudinal.enrichment import (
    enrich_opponents,
    load_candidate_universe,
)
from lisjong_arena.riichilab_longitudinal.errors import LongitudinalAnalysisError
from lisjong_arena.riichilab_self_history.persistence import load_published_history


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("value must be a positive integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be a positive integer")
    return parsed


def _filters(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--from", dest="from_played_at")
    parser.add_argument("--to", dest="to_played_at")
    parser.add_argument("--policy", dest="policy_identity")
    parser.add_argument("--exclude-disconnected", action="store_true")
    parser.add_argument("--require-complete-opponents", action="store_true")
    parser.add_argument("--min-self-rating", type=float)
    parser.add_argument("--max-self-rating", type=float)
    parser.add_argument("--min-opponent-avg-rating", type=float)
    parser.add_argument("--max-opponent-avg-rating", type=float)
    parser.add_argument("--min-rating-gap", type=float)
    parser.add_argument("--max-rating-gap", type=float)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Offline RiichiLab longitudinal diagnostics"
    )
    commands = parser.add_subparsers(dest="command", required=True)

    analyze = commands.add_parser("analyze", help="run network-free analysis")
    analyze.add_argument("--history-root", required=True)
    analyze.add_argument("--policy-epochs")
    analyze.add_argument("--durable-record-map")
    analyze.add_argument("--opponent-cache")
    analyze.add_argument("--require-complete-mjai", action="store_true")
    analyze.add_argument("--output-dir", required=True)
    analyze.add_argument("--name", required=True)
    _filters(analyze)

    enrich = commands.add_parser(
        "enrich-opponents", help="bounded public Bot recent-game enrichment"
    )
    enrich.add_argument("--history-root", required=True)
    enrich.add_argument("--candidate-universe", required=True)
    enrich.add_argument("--game-id", action="append", required=True)
    enrich.add_argument("--max-games", type=_positive_int, required=True)
    enrich.add_argument("--max-bots", type=_positive_int, required=True)
    enrich.add_argument("--output", required=True)
    enrich.add_argument("--timeout", type=float, default=15.0)
    return parser


def _analyze(args: argparse.Namespace) -> int:
    filters = AnalysisFilters(
        from_played_at=args.from_played_at,
        to_played_at=args.to_played_at,
        policy_identity=args.policy_identity,
        exclude_disconnected=args.exclude_disconnected,
        require_complete_opponents=args.require_complete_opponents,
        min_self_rating=args.min_self_rating,
        max_self_rating=args.max_self_rating,
        min_opponent_avg_rating=args.min_opponent_avg_rating,
        max_opponent_avg_rating=args.max_opponent_avg_rating,
        min_rating_gap=args.min_rating_gap,
        max_rating_gap=args.max_rating_gap,
    )
    games, inputs = load_games(
        args.history_root,
        filters=filters,
        policy_epochs_path=args.policy_epochs,
        durable_map_path=args.durable_record_map,
        opponent_cache_path=args.opponent_cache,
        require_complete_mjai=args.require_complete_mjai,
    )
    summary = build_summary(games, inputs=inputs, filters=filters)
    paths = write_artifacts(
        args.output_dir, name=args.name, summary=summary, games=games
    )
    print(f"selected games: {len(games)}")
    for path in paths:
        print(path)
    return 0


def _enrich(args: argparse.Namespace) -> int:
    from lisjong_arena.riichilab_corpus.http import StdlibHttpTransport

    selected = tuple(sorted(set(args.game_id)))
    if len(selected) != len(args.game_id):
        raise LongitudinalAnalysisError("--game-id values must be unique")
    if len(selected) > args.max_games:
        raise LongitudinalAnalysisError(
            "selected game count exceeds --max-games; no request was issued"
        )
    history = load_published_history(args.history_root)
    universe = load_candidate_universe(args.candidate_universe)
    cache = enrich_opponents(
        history,
        selected_game_ids=selected,
        candidate_universe=universe,
        max_games=args.max_games,
        max_bots=args.max_bots,
        output_path=args.output,
        transport=StdlibHttpTransport(),
        timeout=args.timeout,
    )
    print(f"selected games: {cache.coverage['selected_games']}")
    print(f"bots queried: {len(cache.bots_queried)}")
    print(f"complete opponent games: {cache.coverage['complete_opponent_games']}")
    print(args.output)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return _analyze(args) if args.command == "analyze" else _enrich(args)
    except (LongitudinalAnalysisError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = ["build_parser", "main"]
