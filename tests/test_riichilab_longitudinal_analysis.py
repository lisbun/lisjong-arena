from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from _riichilab_longitudinal_fixtures import (
    analysis_game,
    history_game,
    opponent,
    write_history,
)

from lisjong_arena.riichilab_longitudinal import (
    AnalysisFilters,
    LongitudinalAnalysisError,
    RoundMetrics,
    build_summary,
    load_games,
    rating_band,
    rating_gap_band,
    write_artifacts,
)


def _metrics(*, rounds: int = 2, wins: int = 1, deal_ins: int = 0) -> RoundMetrics:
    return RoundMetrics(
        rounds=rounds,
        dealer_rounds=1,
        wins=wins,
        tsumo_wins=wins,
        deal_in_rounds=deal_ins,
        deal_in_loss=1000 * deal_ins,
        riichi_rounds=1,
        riichi_count=1,
        riichi_wins=wins,
        open_call_rounds=1,
        open_call_count=1,
        open_wins=wins,
        chi_count=1,
        draw_rounds=rounds - wins - deal_ins,
        terminal_delta=1000 * (wins - deal_ins),
    )


def _complete_opponents(game_id: str, values: tuple[float, float, float]):
    return tuple(
        opponent(game_id, bot_id, seat, rating)
        for bot_id, seat, rating in zip((1, 2, 3), (1, 2, 3), values, strict=True)
    )


class FilterAndGroupingTest(unittest.TestCase):
    def test_date_filter_is_half_open(self) -> None:
        filters = AnalysisFilters(
            from_played_at="2026-09-14T00:00:00",
            to_played_at="2026-09-15T00:00:00",
        )
        self.assertTrue(
            filters.matches(analysis_game("start", played_at="2026-09-14T00:00:00"))
        )
        self.assertFalse(
            filters.matches(analysis_game("end", played_at="2026-09-15T00:00:00"))
        )

    def test_numeric_filters_are_min_inclusive_max_exclusive(self) -> None:
        filters = AnalysisFilters(min_self_rating=1500, max_self_rating=1600)
        self.assertTrue(filters.matches(analysis_game("low", rating=1500)))
        self.assertFalse(filters.matches(analysis_game("high", rating=1600)))

    def test_policy_disconnect_and_opponent_strength_filters(self) -> None:
        game = analysis_game(
            "filtered",
            rating=1600,
            policy="PolicyA",
            opponents=_complete_opponents("filtered", (1650, 1700, 1750)),
        )
        self.assertTrue(
            AnalysisFilters(
                policy_identity="PolicyA",
                min_opponent_avg_rating=1700,
                max_opponent_avg_rating=1800,
                min_rating_gap=-150,
                max_rating_gap=-50,
            ).matches(game)
        )
        self.assertFalse(AnalysisFilters(policy_identity="PolicyB").matches(game))
        disconnected = analysis_game("dc", disconnected=True)
        self.assertFalse(
            AnalysisFilters(exclude_disconnected=True).matches(disconnected)
        )

    def test_rating_band_boundaries_are_fixed(self) -> None:
        self.assertEqual(rating_band(1599.9), "[1500,1600)")
        self.assertEqual(rating_band(1600), "[1600,1700)")
        self.assertEqual(rating_gap_band(-150), "01:<=-150")
        self.assertEqual(rating_gap_band(-149.9), "02:(-150,-50)")
        self.assertEqual(rating_gap_band(-50), "03:[-50,50)")
        self.assertEqual(rating_gap_band(49.9), "03:[-50,50)")
        self.assertEqual(rating_gap_band(50), "04:[50,150)")
        self.assertEqual(rating_gap_band(149.9), "04:[50,150)")
        self.assertEqual(rating_gap_band(150), "05:>=150")

    def test_naive_played_at_is_not_compared_to_aware_filter(self) -> None:
        filters = AnalysisFilters(
            from_played_at="2026-09-14T00:00:00Z",
            to_played_at="2026-09-15T00:00:00Z",
        )
        with self.assertRaisesRegex(
            LongitudinalAnalysisError, "no timezone is inferred"
        ):
            filters.matches(analysis_game("naive"))


class SummaryTest(unittest.TestCase):
    def _games(self):
        first = analysis_game(
            "game-1",
            rank=1,
            rating=1500,
            policy="PolicyA",
            metrics=_metrics(wins=1),
            opponents=_complete_opponents("game-1", (1600, 1650, 1700)),
        )
        second = analysis_game(
            "game-2",
            played_at="2026-09-14T11:00:00",
            rank=4,
            rating=1600,
            policy="PolicyB",
            metrics=_metrics(wins=0, deal_ins=1),
            opponents=_complete_opponents("game-2", (1500, 1550, 1600)),
        )
        return first, second

    def test_summary_is_deterministic_and_preserves_hanchan_denominators(self) -> None:
        first, second = self._games()
        inputs = {"history_identity": "history"}
        filters = AnalysisFilters()
        forward = build_summary((first, second), inputs=inputs, filters=filters)
        reverse = build_summary((second, first), inputs=inputs, filters=filters)
        self.assertEqual(forward, reverse)
        metric = forward["overall"]["metrics"]["win_rate"]
        self.assertEqual(metric["numerator"], 1)
        self.assertEqual(metric["denominator"], 4)
        self.assertEqual(metric["hanchan_clusters"], 2)
        self.assertEqual(
            metric["uncertainty"]["method"],
            "whole-hanchan cluster percentile bootstrap",
        )
        self.assertEqual(metric["uncertainty"]["replicates"], 2000)
        self.assertIsNotNone(metric["interval_95"]["low"])
        self.assertEqual(forward["overall"]["games"], 2)
        self.assertEqual(forward["overall"]["rounds"], 4)

    def test_grouped_results_report_sample_size_coverage_and_exploratory_status(
        self,
    ) -> None:
        games = self._games()
        summary = build_summary(
            games, inputs={"history_identity": "history"}, filters=AnalysisFilters()
        )
        self.assertEqual(summary["grouped"]["classification"], "exploratory_post_hoc")
        policy = summary["grouped"]["by_policy"]["PolicyA"]
        self.assertEqual(policy["games"], 1)
        self.assertEqual(policy["rounds"], 2)
        self.assertEqual(policy["coverage"]["opponent_ratings"]["complete_games"], 1)
        self.assertIn(
            "not a causal Policy-strength estimate",
            summary["analysis_contract"]["interpretation"],
        )

    def test_round_uncertainty_uses_only_hanchan_with_mjai(self) -> None:
        available, _ = self._games()
        missing = analysis_game(
            "missing",
            played_at="2026-09-14T12:00:00",
            rank=2,
            metrics=None,
        )
        summary = build_summary(
            (available, missing),
            inputs={"history_identity": "history"},
            filters=AnalysisFilters(),
        )
        win_rate = summary["overall"]["metrics"]["win_rate"]
        average_rank = summary["overall"]["metrics"]["average_rank"]
        self.assertEqual(win_rate["hanchan_clusters"], 1)
        self.assertEqual(win_rate["denominator"], 2)
        self.assertEqual(average_rank["hanchan_clusters"], 2)
        self.assertEqual(summary["overall"]["coverage"]["mjai"]["missing_games"], 1)

    def test_artifacts_are_versioned_and_do_not_include_raw_mjai(self) -> None:
        games = self._games()
        summary = build_summary(
            games, inputs={"history_identity": "history"}, filters=AnalysisFilters()
        )
        with tempfile.TemporaryDirectory() as directory:
            paths = write_artifacts(
                directory, name="diagnostic", summary=summary, games=games
            )
            self.assertEqual(
                [path.name for path in paths],
                [
                    "diagnostic-summary.json",
                    "diagnostic-games.csv",
                    "diagnostic-opponents.csv",
                ],
            )
            self.assertIn(b'"artifact_identity"', paths[0].read_bytes())
            self.assertFalse(
                any(path.suffix == ".gz" for path in Path(directory).iterdir())
            )


class OfflineBoundaryTest(unittest.TestCase):
    def test_normal_analysis_path_performs_no_network_access(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_history(root, (history_game(),))
            with patch(
                "urllib.request.urlopen",
                side_effect=AssertionError("network must not be used"),
            ):
                games, _ = load_games(root)
        self.assertEqual(len(games), 1)
        self.assertEqual(games[0].mjai_status, "missing")


if __name__ == "__main__":
    unittest.main()
