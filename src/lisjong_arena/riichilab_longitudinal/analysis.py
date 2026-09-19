"""Network-free deterministic RiichiLab longitudinal analysis."""

from __future__ import annotations

import math
import random
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from lisjong_arena.riichilab_longitudinal.enrichment import load_opponent_cache
from lisjong_arena.riichilab_longitudinal.errors import LongitudinalAnalysisError
from lisjong_arena.riichilab_longitudinal.mjai import analyze_mjai
from lisjong_arena.riichilab_longitudinal.models import (
    SUMMARY_SCHEMA_ID,
    UNMAPPED_POLICY,
    UNRESOLVED,
    GameAnalysis,
    OpponentCache,
    PolicyProvenance,
    canonical_played_at,
    strict_number,
)
from lisjong_arena.riichilab_longitudinal.provenance import (
    load_durable_provenance_map,
    load_policy_epochs,
    resolve_policy_provenance,
)
from lisjong_arena.riichilab_self_history.models import SelfHistoryGame
from lisjong_arena.riichilab_self_history.persistence import (
    GAMES_DIRECTORY,
    load_published_history,
)

BOOTSTRAP_REPLICATES = 2_000
BOOTSTRAP_SEED = 253
BOOTSTRAP_LOWER_PERCENTILE = 2.5
BOOTSTRAP_UPPER_PERCENTILE = 97.5
FEW_CLUSTER_WARNING_THRESHOLD = 30


def _parsed_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@dataclass(frozen=True, slots=True)
class AnalysisFilters:
    from_played_at: str | None = None
    to_played_at: str | None = None
    policy_identity: str | None = None
    exclude_disconnected: bool = False
    require_complete_opponents: bool = False
    min_self_rating: float | None = None
    max_self_rating: float | None = None
    min_opponent_avg_rating: float | None = None
    max_opponent_avg_rating: float | None = None
    min_rating_gap: float | None = None
    max_rating_gap: float | None = None

    def __post_init__(self) -> None:
        if type(self.exclude_disconnected) is not bool:
            raise LongitudinalAnalysisError("exclude_disconnected must be a boolean")
        if type(self.require_complete_opponents) is not bool:
            raise LongitudinalAnalysisError(
                "require_complete_opponents must be a boolean"
            )
        if self.from_played_at is not None:
            object.__setattr__(
                self,
                "from_played_at",
                canonical_played_at(self.from_played_at, "filter from"),
            )
        if self.to_played_at is not None:
            object.__setattr__(
                self,
                "to_played_at",
                canonical_played_at(self.to_played_at, "filter to"),
            )
        if self.from_played_at is not None and self.to_played_at is not None:
            start = _parsed_time(self.from_played_at)
            end = _parsed_time(self.to_played_at)
            if (start.tzinfo is None) != (end.tzinfo is None):
                raise LongitudinalAnalysisError(
                    "date filter bounds cannot mix timezone-naive and aware values"
                )
            if start >= end:
                raise LongitudinalAnalysisError("date filter requires from < to")
        if self.policy_identity is not None and (
            type(self.policy_identity) is not str or not self.policy_identity
        ):
            raise LongitudinalAnalysisError(
                "policy filter must be a non-empty string or None"
            )
        for name in (
            "min_self_rating",
            "max_self_rating",
            "min_opponent_avg_rating",
            "max_opponent_avg_rating",
            "min_rating_gap",
            "max_rating_gap",
        ):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, strict_number(value, name))
        for low, high, label in (
            (self.min_self_rating, self.max_self_rating, "self rating"),
            (
                self.min_opponent_avg_rating,
                self.max_opponent_avg_rating,
                "opponent average rating",
            ),
            (self.min_rating_gap, self.max_rating_gap, "rating gap"),
        ):
            if low is not None and high is not None and low >= high:
                raise LongitudinalAnalysisError(
                    f"{label} filter requires minimum < maximum"
                )

    def matches(self, game: GameAnalysis) -> bool:
        played_at = _parsed_time(game.played_at)
        for boundary in (self.from_played_at, self.to_played_at):
            if boundary is not None and (
                (played_at.tzinfo is None) != (_parsed_time(boundary).tzinfo is None)
            ):
                raise LongitudinalAnalysisError(
                    "played_at and date filter timezone awareness differ; no timezone "
                    "is inferred"
                )
        if self.from_played_at is not None and played_at < _parsed_time(
            self.from_played_at
        ):
            return False
        if self.to_played_at is not None and played_at >= _parsed_time(
            self.to_played_at
        ):
            return False
        if (
            self.policy_identity is not None
            and game.provenance.policy_identity != self.policy_identity
        ):
            return False
        if self.exclude_disconnected and game.is_disconnected:
            return False
        if self.require_complete_opponents and not game.opponent_ratings_complete:
            return False
        if (
            self.min_self_rating is not None
            and game.self_rating_before < self.min_self_rating
        ):
            return False
        if (
            self.max_self_rating is not None
            and game.self_rating_before >= self.max_self_rating
        ):
            return False
        opponent = game.opponent_rating_avg
        if self.min_opponent_avg_rating is not None and (
            opponent is None or opponent < self.min_opponent_avg_rating
        ):
            return False
        if self.max_opponent_avg_rating is not None and (
            opponent is None or opponent >= self.max_opponent_avg_rating
        ):
            return False
        gap = game.rating_gap_avg
        if self.min_rating_gap is not None and (
            gap is None or gap < self.min_rating_gap
        ):
            return False
        if self.max_rating_gap is not None and (
            gap is None or gap >= self.max_rating_gap
        ):
            return False
        return True

    def to_value(self) -> dict[str, object]:
        return {
            "date_interval_semantics": "[from,to)",
            "exclude_disconnected": self.exclude_disconnected,
            "from": self.from_played_at,
            "max_opponent_avg_rating": self.max_opponent_avg_rating,
            "max_rating_gap": self.max_rating_gap,
            "max_self_rating": self.max_self_rating,
            "min_opponent_avg_rating": self.min_opponent_avg_rating,
            "min_rating_gap": self.min_rating_gap,
            "min_self_rating": self.min_self_rating,
            "numeric_interval_semantics": "[minimum,maximum)",
            "policy": self.policy_identity,
            "require_complete_opponents": self.require_complete_opponents,
            "to": self.to_played_at,
        }


def _opponents_by_game(cache: OpponentCache | None) -> dict[str, tuple]:
    grouped: dict[str, list] = defaultdict(list)
    if cache is not None:
        for row in cache.opponents:
            grouped[row.game_id].append(row)
    return {key: tuple(sorted(value)) for key, value in grouped.items()}


def _metadata_game(
    game: SelfHistoryGame,
    *,
    provenance: PolicyProvenance,
    opponents: tuple,
) -> GameAnalysis:
    return GameAnalysis(
        game_id=game.game_id,
        played_at=game.played_at,
        seat=game.seat,
        rank=game.rank,
        final_score=game.score,
        self_rating_before=game.rating_before,
        rating_delta=game.rating_delta,
        is_disconnected=game.is_disconnected,
        provenance=provenance,
        mjai_status="missing",
        metrics=None,
        opponents=opponents,
    )


def load_games(
    history_root: str | Path,
    *,
    filters: AnalysisFilters = AnalysisFilters(),
    policy_epochs_path: str | Path | None = None,
    durable_map_path: str | Path | None = None,
    opponent_cache_path: str | Path | None = None,
    require_complete_mjai: bool = False,
) -> tuple[tuple[GameAnalysis, ...], dict[str, object]]:
    """Load all selected local inputs without any network-capable operation."""
    root = Path(history_root)
    history = load_published_history(root)
    if any(game.player_count != 4 for game in history.games):
        raise LongitudinalAnalysisError(
            "longitudinal v1 supports only four-player history"
        )
    epochs = load_policy_epochs(policy_epochs_path)
    durable = load_durable_provenance_map(durable_map_path, history)
    cache = load_opponent_cache(opponent_cache_path)
    if cache is not None:
        if cache.self_bot_id != history.bot_id:
            raise LongitudinalAnalysisError(
                "opponent cache self bot does not match self history"
            )
        if any(
            game_id not in set(history.game_ids) for game_id in cache.selected_game_ids
        ):
            raise LongitudinalAnalysisError(
                "opponent cache contains a game outside self history"
            )
    opponents = _opponents_by_game(cache)

    selected_metadata: list[tuple[SelfHistoryGame, GameAnalysis]] = []
    for game in history.games:
        provenance = resolve_policy_provenance(game, durable=durable, epochs=epochs)
        projected = _metadata_game(
            game,
            provenance=provenance,
            opponents=opponents.get(game.game_id, ()),
        )
        if filters.matches(projected):
            selected_metadata.append((game, projected))

    analyses = []
    missing = []
    for game, projected in selected_metadata:
        path = root / GAMES_DIRECTORY / f"{game.game_id}.jsonl.gz"
        if path.is_symlink():
            raise LongitudinalAnalysisError(
                f"MJAI input for {game.game_id} must be a regular file"
            )
        if not path.exists():
            missing.append(game.game_id)
            analyses.append(projected)
            continue
        if not path.is_file():
            raise LongitudinalAnalysisError(
                f"MJAI input for {game.game_id} must be a regular file"
            )
        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise LongitudinalAnalysisError(
                f"MJAI input for {game.game_id} cannot be read"
            ) from exc
        metrics = analyze_mjai(payload, self_seat=game.seat)
        analyses.append(
            GameAnalysis(
                game_id=projected.game_id,
                played_at=projected.played_at,
                seat=projected.seat,
                rank=projected.rank,
                final_score=projected.final_score,
                self_rating_before=projected.self_rating_before,
                rating_delta=projected.rating_delta,
                is_disconnected=projected.is_disconnected,
                provenance=projected.provenance,
                mjai_status="available",
                metrics=metrics,
                opponents=projected.opponents,
            )
        )
    if require_complete_mjai and missing:
        raise LongitudinalAnalysisError(
            "selected cohort is missing MJAI for: " + ", ".join(missing)
        )
    inputs = {
        "durable_record_count": len(durable),
        "history_game_count": len(history.games),
        "history_identity": history.identity,
        "legacy_epoch_count": len(epochs),
        "opponent_enrichment": (
            None
            if cache is None
            else {
                "api_failures": list(cache.api_failures),
                "bots_queried": list(cache.bots_queried),
                "cache_identity": cache.identity,
                "candidate_bot_count": cache.candidate_bot_count,
                "candidate_universe_identity": cache.candidate_universe_identity,
                "candidate_universe_source": cache.candidate_universe_source,
                "coverage": cache.coverage,
            }
        ),
        "self_bot_id": history.bot_id,
    }
    return tuple(analyses), inputs


def _nearest_rank(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    rank = max(1, math.ceil(percentile / 100 * len(ordered)))
    return ordered[rank - 1]


def _cluster_interval(
    games: tuple[GameAnalysis, ...],
    statistic: Callable[[tuple[GameAnalysis, ...]], float | None],
) -> tuple[float | None, float | None, int]:
    if not games:
        return None, None, 0
    rng = random.Random(BOOTSTRAP_SEED)
    count = len(games)
    values = []
    for _ in range(BOOTSTRAP_REPLICATES):
        selected = tuple(games[rng.randrange(count)] for _ in range(count))
        value = statistic(selected)
        if value is not None:
            values.append(value)
    if not values:
        return None, None, 0
    return (
        _nearest_rank(values, BOOTSTRAP_LOWER_PERCENTILE),
        _nearest_rank(values, BOOTSTRAP_UPPER_PERCENTILE),
        len(values),
    )


def _metric(
    games: tuple[GameAnalysis, ...],
    *,
    numerator: Callable[[tuple[GameAnalysis, ...]], float],
    denominator: Callable[[tuple[GameAnalysis, ...]], int],
    analysis_role: str,
) -> dict[str, object]:
    def statistic(sample: tuple[GameAnalysis, ...]) -> float | None:
        divisor = denominator(sample)
        return None if divisor == 0 else numerator(sample) / divisor

    divisor = denominator(games)
    raw_numerator = numerator(games)
    estimate = None if divisor == 0 else raw_numerator / divisor
    low, high, valid = _cluster_interval(games, statistic)
    warnings = []
    if len(games) < FEW_CLUSTER_WARNING_THRESHOLD:
        warnings.append("few_hanchan_clusters")
    if divisor == 0:
        warnings.append("no_eligible_denominator")
    return {
        "analysis_role": analysis_role,
        "denominator": divisor,
        "estimate": estimate,
        "hanchan_clusters": len(games),
        "interval_95": {"high": high, "low": low},
        "numerator": raw_numerator,
        "rounds": sum(game.metrics.rounds for game in games if game.metrics),
        "uncertainty": {
            "method": "whole-hanchan cluster percentile bootstrap",
            "replicates": BOOTSTRAP_REPLICATES,
            "seed": BOOTSTRAP_SEED,
            "valid_replicates": valid,
        },
        "warning_flags": warnings,
    }


def _rank_numerator(rank: int) -> Callable[[tuple[GameAnalysis, ...]], float]:
    return lambda games: sum(game.rank == rank for game in games)


def _round_sum(name: str) -> Callable[[tuple[GameAnalysis, ...]], float]:
    return lambda games: sum(
        getattr(game.metrics, name) for game in games if game.metrics is not None
    )


def _round_denominator(games: tuple[GameAnalysis, ...]) -> int:
    return sum(game.metrics.rounds for game in games if game.metrics is not None)


def metric_summary(games: tuple[GameAnalysis, ...]) -> dict[str, object]:
    def game_count(sample: tuple[GameAnalysis, ...]) -> int:
        return len(sample)

    secondary = "secondary_prespecified_diagnostic"
    primary = "primary_prespecified_descriptive"
    round_games = tuple(game for game in games if game.metrics is not None)
    return {
        "average_final_score": _metric(
            games,
            numerator=lambda sample: sum(game.final_score for game in sample),
            denominator=game_count,
            analysis_role=primary,
        ),
        "average_rank": _metric(
            games,
            numerator=lambda sample: sum(game.rank for game in sample),
            denominator=game_count,
            analysis_role=primary,
        ),
        "deal_in_rate": _metric(
            round_games,
            numerator=_round_sum("deal_in_rounds"),
            denominator=_round_denominator,
            analysis_role=secondary,
        ),
        "first_rate": _metric(
            games,
            numerator=_rank_numerator(1),
            denominator=game_count,
            analysis_role=primary,
        ),
        "fourth_rate": _metric(
            games,
            numerator=_rank_numerator(4),
            denominator=game_count,
            analysis_role=primary,
        ),
        "open_call_round_rate": _metric(
            round_games,
            numerator=_round_sum("open_call_rounds"),
            denominator=_round_denominator,
            analysis_role=secondary,
        ),
        "riichi_rate": _metric(
            round_games,
            numerator=_round_sum("riichi_rounds"),
            denominator=_round_denominator,
            analysis_role=secondary,
        ),
        "top2_rate": _metric(
            games,
            numerator=lambda sample: sum(game.rank <= 2 for game in sample),
            denominator=game_count,
            analysis_role=primary,
        ),
        "win_rate": _metric(
            round_games,
            numerator=_round_sum("wins"),
            denominator=_round_denominator,
            analysis_role=secondary,
        ),
    }


def _distribution(values: tuple[float, ...]) -> dict[str, object]:
    return {
        "count": len(values),
        "max": max(values) if values else None,
        "mean": sum(values) / len(values) if values else None,
        "min": min(values) if values else None,
    }


def rating_band(value: float) -> str:
    lower = math.floor(value / 100) * 100
    return f"[{lower},{lower + 100})"


def rating_gap_band(value: float) -> str:
    if value <= -150:
        return "01:<=-150"
    if value < -50:
        return "02:(-150,-50)"
    if value < 50:
        return "03:[-50,50)"
    if value < 150:
        return "04:[50,150)"
    return "05:>=150"


def _coverage(games: tuple[GameAnalysis, ...]) -> dict[str, object]:
    complete = sum(game.opponent_ratings_complete for game in games)
    partial = sum(
        not game.opponent_ratings_complete and bool(game.known_opponent_ratings)
        for game in games
    )
    unknown = len(games) - complete - partial
    sources = Counter(game.provenance.source for game in games)
    return {
        "mjai": {
            "available_games": sum(game.metrics is not None for game in games),
            "missing_games": sum(game.metrics is None for game in games),
        },
        "opponent_ratings": {
            "complete_games": complete,
            "coverage_rate": complete / len(games) if games else None,
            "partial_games": partial,
            "unknown_games": unknown,
        },
        "provenance": {
            "complete_durable_games": sum(game.provenance.complete for game in games),
            "field_coverage": {
                "durable_record_identity": sum(
                    game.provenance.durable_record_identity is not None
                    for game in games
                ),
                "lisjong_arena_revision": sum(
                    game.provenance.lisjong_arena_revision not in (None, UNRESOLVED)
                    for game in games
                ),
                "lisjong_revision": sum(
                    game.provenance.lisjong_revision not in (None, UNRESOLVED)
                    for game in games
                ),
                "policy_identity": sum(
                    game.provenance.policy_identity != UNMAPPED_POLICY for game in games
                ),
                "profile_identity": sum(
                    game.provenance.profile_identity not in (None, UNRESOLVED)
                    for game in games
                ),
            },
            "source_counts": dict(sorted(sources.items())),
            "unmapped_games": sources.get("unmapped", 0),
        },
    }


def _diagnostic_counts(games: tuple[GameAnalysis, ...]) -> dict[str, object]:
    names = tuple(
        field
        for field in next(
            (game.metrics.to_value() for game in games if game.metrics), {}
        )
    )
    return {
        name: sum(getattr(game.metrics, name) for game in games if game.metrics)
        for name in names
    }


def cohort_summary(games: tuple[GameAnalysis, ...]) -> dict[str, object]:
    opponent_averages = tuple(
        game.opponent_rating_avg
        for game in games
        if game.opponent_rating_avg is not None
    )
    rating_gaps = tuple(
        game.rating_gap_avg for game in games if game.rating_gap_avg is not None
    )
    return {
        "coverage": _coverage(games),
        "disconnected_games": sum(game.is_disconnected for game in games),
        "games": len(games),
        "mahjong_counts": _diagnostic_counts(games),
        "metrics": metric_summary(games),
        "opponent_rating_before": _distribution(opponent_averages),
        "rank_counts": {
            str(rank): sum(game.rank == rank for game in games) for rank in range(1, 5)
        },
        "rating_delta_sum": sum(game.rating_delta for game in games),
        "rating_gap": _distribution(rating_gaps),
        "rounds": sum(game.metrics.rounds for game in games if game.metrics),
        "self_rating_before": _distribution(
            tuple(game.self_rating_before for game in games)
        ),
        "time_range": {
            "from": games[0].played_at if games else None,
            "to_inclusive": games[-1].played_at if games else None,
        },
    }


def _groups(
    games: tuple[GameAnalysis, ...], key: Callable[[GameAnalysis], str]
) -> dict[str, object]:
    grouped: dict[str, list[GameAnalysis]] = defaultdict(list)
    for game in games:
        grouped[key(game)].append(game)
    return {
        label: cohort_summary(tuple(values))
        for label, values in sorted(grouped.items())
    }


def build_summary(
    games: tuple[GameAnalysis, ...],
    *,
    inputs: dict[str, object],
    filters: AnalysisFilters,
) -> dict[str, object]:
    """Build a versioned descriptive report; no strength classification exists."""
    ordered = tuple(sorted(games, key=lambda game: (game.played_at, game.game_id)))
    policies = Counter(game.provenance.policy_identity for game in ordered)
    return {
        "analysis_contract": {
            "evidence_type": "observational_longitudinal_diagnostic",
            "interpretation": (
                "Describes what happened in the selected cohort; it is not a "
                "causal Policy-strength estimate or Champion decision."
            ),
            "multiple_comparisons": (
                "Grouped slices are exploratory and generate hypotheses only."
            ),
            "opponent_confounding": (
                "Policy period, self rating, opponent population, matchmaking, "
                "table composition and time may co-vary."
            ),
            "resampling_unit": "whole hanchan",
        },
        "filters": filters.to_value(),
        "grouped": {
            "by_opponent_avg_rating_band": _groups(
                ordered,
                lambda game: (
                    "<missing>"
                    if game.opponent_rating_avg is None
                    else rating_band(game.opponent_rating_avg)
                ),
            ),
            "by_policy": _groups(ordered, lambda game: game.provenance.policy_identity),
            "by_rating_gap_band": _groups(
                ordered,
                lambda game: (
                    "<missing>"
                    if game.rating_gap_avg is None
                    else rating_gap_band(game.rating_gap_avg)
                ),
            ),
            "by_self_rating_band": _groups(
                ordered, lambda game: rating_band(game.self_rating_before)
            ),
            "classification": "exploratory_post_hoc",
        },
        "grouping_semantics": {
            "opponent_rating_bands": "100-point [lower,upper) bands",
            "rating_gap_bands": [
                "<=-150",
                "(-150,-50)",
                "[-50,50)",
                "[50,150)",
                ">=150",
            ],
            "self_rating_bands": "100-point [lower,upper) bands",
        },
        "inputs": dict(inputs),
        "overall": cohort_summary(ordered),
        "policy_distribution": dict(sorted(policies.items())),
        "schema": SUMMARY_SCHEMA_ID,
        "schema_version": 1,
    }


__all__ = [
    "BOOTSTRAP_REPLICATES",
    "BOOTSTRAP_SEED",
    "FEW_CLUSTER_WARNING_THRESHOLD",
    "AnalysisFilters",
    "build_summary",
    "cohort_summary",
    "load_games",
    "metric_summary",
    "rating_band",
    "rating_gap_band",
]
