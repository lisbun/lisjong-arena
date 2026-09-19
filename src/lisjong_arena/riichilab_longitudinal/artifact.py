"""Safe local aggregate/per-game diagnostic artifact output."""

from __future__ import annotations

import csv
import io
import re
from pathlib import Path

from lisjong_arena.riichilab_corpus.models import (
    CorpusError,
    canonical_json_bytes,
    sha256_bytes,
    strict_json_loads,
)
from lisjong_arena.riichilab_corpus.persistence import (
    atomic_replace,
    ensure_outside_git_worktree,
)
from lisjong_arena.riichilab_longitudinal.errors import LongitudinalAnalysisError
from lisjong_arena.riichilab_longitudinal.models import GameAnalysis

_SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}").fullmatch

_GAME_COLUMNS = (
    "game_id",
    "played_at",
    "policy_identity",
    "provenance_source",
    "provenance_complete",
    "lisjong_revision",
    "lisjong_arena_revision",
    "profile_identity",
    "durable_record_identity",
    "seat",
    "rank",
    "final_score",
    "self_rating_before",
    "rating_delta",
    "rating_after",
    "is_disconnected",
    "opponent_count_known",
    "opponent_ratings_complete",
    "opponent_rating_avg",
    "opponent_rating_min",
    "opponent_rating_max",
    "rating_gap_avg",
    "mjai_status",
    "rounds",
    "wins",
    "tsumo_wins",
    "ron_wins",
    "deal_in_rounds",
    "deal_in_loss",
    "riichi_rounds",
    "riichi_count",
    "open_call_rounds",
    "open_call_count",
    "chi_count",
    "pon_count",
    "kan_count",
    "draw_rounds",
    "draw_delta",
    "opponent_tsumo_rounds",
    "opponent_tsumo_loss",
    "dealer_rounds",
    "dealer_wins",
    "dealer_deal_ins",
    "terminal_delta",
)

_OPPONENT_COLUMNS = (
    "game_id",
    "bot_id",
    "bot_name",
    "seat",
    "rank",
    "score",
    "rating_before",
)


def _csv_bytes(columns: tuple[str, ...], rows: list[dict[str, object]]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow(
            {
                column: (
                    "true"
                    if row.get(column) is True
                    else "false"
                    if row.get(column) is False
                    else ""
                    if row.get(column) is None
                    else row.get(column)
                )
                for column in columns
            }
        )
    return buffer.getvalue().encode("utf-8")


def _game_row(game: GameAnalysis) -> dict[str, object]:
    value = game.to_value()
    provenance = value["provenance"]
    assert isinstance(provenance, dict)
    metrics = value["round_metrics"] or {}
    assert isinstance(metrics, dict)
    return {
        **{column: metrics.get(column) for column in _GAME_COLUMNS},
        "durable_record_identity": provenance["durable_record_identity"],
        "final_score": game.final_score,
        "game_id": game.game_id,
        "is_disconnected": game.is_disconnected,
        "lisjong_arena_revision": provenance["lisjong_arena_revision"],
        "lisjong_revision": provenance["lisjong_revision"],
        "mjai_status": game.mjai_status,
        "opponent_count_known": value["opponent_count_known"],
        "opponent_rating_avg": value["opponent_rating_avg"],
        "opponent_rating_max": value["opponent_rating_max"],
        "opponent_rating_min": value["opponent_rating_min"],
        "opponent_ratings_complete": value["opponent_ratings_complete"],
        "played_at": game.played_at,
        "policy_identity": provenance["policy_identity"],
        "profile_identity": provenance["profile_identity"],
        "provenance_complete": provenance["complete"],
        "provenance_source": provenance["source"],
        "rank": game.rank,
        "rating_after": value["rating_after"],
        "rating_delta": game.rating_delta,
        "rating_gap_avg": value["rating_gap_avg"],
        "seat": game.seat,
        "self_rating_before": game.self_rating_before,
    }


def write_artifacts(
    output_dir: str | Path,
    *,
    name: str,
    summary: dict[str, object],
    games: tuple[GameAnalysis, ...],
) -> tuple[Path, Path, Path]:
    """Write three deterministic local artifacts outside every Git worktree."""
    if type(name) is not str or _SAFE_NAME(name) is None:
        raise LongitudinalAnalysisError("artifact name is not a safe identifier")
    try:
        root = ensure_outside_git_worktree(Path(output_dir))
    except CorpusError as exc:
        raise LongitudinalAnalysisError(str(exc)) from exc
    root.mkdir(parents=True, exist_ok=True)
    summary_path = root / f"{name}-summary.json"
    games_path = root / f"{name}-games.csv"
    opponents_path = root / f"{name}-opponents.csv"
    for path in (summary_path, games_path, opponents_path):
        if path.exists():
            raise LongitudinalAnalysisError(
                f"refusing to overwrite existing analysis artifact: {path}"
            )

    ordered_games = tuple(
        sorted(games, key=lambda game: (game.played_at, game.game_id))
    )
    game_rows = [_game_row(game) for game in ordered_games]
    opponent_rows = [
        row.to_value() for game in ordered_games for row in sorted(game.opponents)
    ]
    logical = {
        "games": [game.to_value() for game in ordered_games],
        "opponents": opponent_rows,
        "summary": summary,
    }
    published_summary = {
        **summary,
        "artifact_identity": sha256_bytes(canonical_json_bytes(logical)),
    }
    summary_bytes = canonical_json_bytes(published_summary)
    games_bytes = _csv_bytes(_GAME_COLUMNS, game_rows)
    opponents_bytes = _csv_bytes(_OPPONENT_COLUMNS, opponent_rows)

    # Publish the summary (the bundle's machine-readable completion marker)
    # only after both local tables are fully in place.
    atomic_replace(games_path, games_bytes)
    atomic_replace(opponents_path, opponents_bytes)
    atomic_replace(summary_path, summary_bytes)
    try:
        readback = strict_json_loads(summary_path.read_bytes(), "summary artifact")
    except (CorpusError, OSError) as exc:
        raise LongitudinalAnalysisError(
            "summary artifact failed strict readback"
        ) from exc
    if readback != published_summary:
        raise LongitudinalAnalysisError(
            "summary artifact does not read back identically"
        )
    return summary_path, games_path, opponents_path


__all__ = ["write_artifacts"]
