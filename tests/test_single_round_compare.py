"""``lisjong_arena.single_round_compare`` CLIのunit test。

既存``run_single_round_evaluation()`` / ``run_single_round_evaluation_parallel()``
はmockし、CLIのwiring(Policy名解決、seed解析、worker dispatch、summary
formatting、fail closed)だけを検証する。ABBB rotation / Policy lifecycle /
serial-parallel equivalence等のevaluation semanticsは
``tests.test_single_round_evaluation`` / ``tests.test_single_round_evaluation_parallel``
が既に固定しているため、ここでは実RiichiEnvを起動する新しいlong-running test
を追加しない。
"""

import contextlib
import dataclasses
import io
import unittest
from unittest import mock

from _round_stats_fixtures import neutral_seat_round_stats_tuple
from lisjong.policy_contract import Seat

from lisjong_arena.model import (
    SingleRoundEvaluationPlan,
    SingleRoundEvaluationResult,
    SingleRoundGameResult,
)
from lisjong_arena.policy_catalog import POLICY_CATALOG
from lisjong_arena.single_round_compare import (
    _run_cli,
    format_summary,
    parse_seeds,
)
from lisjong_arena.single_round_evaluation import (
    ROTATION_COUNT,
    aggregate_candidate_metrics,
)


def _game_result_with_neutral_round_stats(
    *, seed: int, rotation: int, scores: tuple[int, int, int, int]
) -> SingleRoundGameResult:
    return SingleRoundGameResult(
        seed=seed,
        rotation=rotation,
        game_mode="4p-red-single",
        candidate_seat=Seat(rotation),
        scores=scores,
        seat_round_stats=neutral_seat_round_stats_tuple(scores),
    )


def _fake_result(
    *,
    candidate: str = "finite-horizon",
    baseline: str = "two-step",
    seeds: tuple[int, ...] = (0,),
) -> SingleRoundEvaluationResult:
    plan = SingleRoundEvaluationPlan(
        candidate=POLICY_CATALOG[candidate],
        baseline=POLICY_CATALOG[baseline],
        seeds=seeds,
    )
    game_results = tuple(
        _game_result_with_neutral_round_stats(
            seed=seed,
            rotation=rotation,
            scores=tuple(40_000 if seat == rotation else 20_000 for seat in range(4)),
        )
        for seed in seeds
        for rotation in range(ROTATION_COUNT)
    )
    return SingleRoundEvaluationResult(
        plan=plan,
        game_results=game_results,
        candidate_metrics=aggregate_candidate_metrics(candidate, game_results),
    )


class SeedParserValidTest(unittest.TestCase):
    def test_single_seed(self) -> None:
        self.assertEqual(parse_seeds("42"), (42,))

    def test_inclusive_range(self) -> None:
        self.assertEqual(parse_seeds("0:99"), tuple(range(0, 100)))

    def test_start_equals_end(self) -> None:
        self.assertEqual(parse_seeds("7:7"), (7,))

    def test_negative_single_seed_matches_existing_plan_contract(self) -> None:
        # SingleRoundEvaluationPlan / ComparisonPlanが共有する既存
        # _normalize_seeds()はintの符号を検証しないため、CLI側でも拒否しない。
        self.assertEqual(parse_seeds("-5"), (-5,))


class SeedParserInvalidTest(unittest.TestCase):
    def test_empty_string_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            parse_seeds("")

    def test_bare_colon_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            parse_seeds(":")

    def test_missing_end_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            parse_seeds("1:")

    def test_missing_start_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            parse_seeds(":2")

    def test_non_integer_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            parse_seeds("foo")

    def test_non_integer_end_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            parse_seeds("1:foo")

    def test_non_integer_start_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            parse_seeds("foo:2")

    def test_reversed_range_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            parse_seeds("10:9")

    def test_multiple_colons_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            parse_seeds("1:2:3")


class CliDispatchTest(unittest.TestCase):
    def test_workers_one_uses_the_serial_runner_only(self) -> None:
        with (
            mock.patch(
                "lisjong_arena.single_round_compare.run_single_round_evaluation",
                return_value=_fake_result(),
            ) as serial,
            mock.patch(
                "lisjong_arena.single_round_compare.run_single_round_evaluation_parallel",
                side_effect=AssertionError("parallel runner must not be called"),
            ) as parallel,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            return_code = _run_cli(
                [
                    "--candidate",
                    "finite-horizon",
                    "--baseline",
                    "two-step",
                    "--seeds",
                    "0",
                    "--workers",
                    "1",
                ]
            )

        self.assertEqual(return_code, 0)
        serial.assert_called_once()
        parallel.assert_not_called()

    def test_workers_greater_than_one_uses_the_parallel_runner_only(self) -> None:
        with (
            mock.patch(
                "lisjong_arena.single_round_compare.run_single_round_evaluation",
                side_effect=AssertionError("serial runner must not be called"),
            ) as serial,
            mock.patch(
                "lisjong_arena.single_round_compare.run_single_round_evaluation_parallel",
                return_value=_fake_result(),
            ) as parallel,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            return_code = _run_cli(
                [
                    "--candidate",
                    "finite-horizon",
                    "--baseline",
                    "two-step",
                    "--seeds",
                    "0",
                    "--workers",
                    "4",
                ]
            )

        self.assertEqual(return_code, 0)
        serial.assert_not_called()
        parallel.assert_called_once()
        self.assertEqual(parallel.call_args.kwargs["max_workers"], 4)

    def test_default_workers_is_one_and_serial(self) -> None:
        with (
            mock.patch(
                "lisjong_arena.single_round_compare.run_single_round_evaluation",
                return_value=_fake_result(),
            ) as serial,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            _run_cli(
                [
                    "--candidate",
                    "finite-horizon",
                    "--baseline",
                    "two-step",
                    "--seeds",
                    "0",
                ]
            )

        serial.assert_called_once()

    def test_candidate_and_baseline_names_resolve_to_the_catalog_specs(self) -> None:
        with (
            mock.patch(
                "lisjong_arena.single_round_compare.run_single_round_evaluation",
                return_value=_fake_result(),
            ) as serial,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            _run_cli(
                [
                    "--candidate",
                    "finite-horizon",
                    "--baseline",
                    "two-step",
                    "--seeds",
                    "0",
                    "--workers",
                    "1",
                ]
            )

        (plan,), _ = serial.call_args
        self.assertIs(plan.candidate, POLICY_CATALOG["finite-horizon"])
        self.assertIs(plan.baseline, POLICY_CATALOG["two-step"])

    def test_plan_seeds_match_the_parsed_seed_range(self) -> None:
        with (
            mock.patch(
                "lisjong_arena.single_round_compare.run_single_round_evaluation",
                return_value=_fake_result(seeds=tuple(range(0, 3))),
            ) as serial,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            _run_cli(
                [
                    "--candidate",
                    "finite-horizon",
                    "--baseline",
                    "two-step",
                    "--seeds",
                    "0:2",
                    "--workers",
                    "1",
                ]
            )

        (plan,), _ = serial.call_args
        self.assertEqual(plan.seeds, (0, 1, 2))


class FailClosedTest(unittest.TestCase):
    def test_unknown_candidate_name_exits_2(self) -> None:
        with (
            contextlib.redirect_stderr(io.StringIO()) as stderr,
            self.assertRaises(SystemExit) as caught,
        ):
            _run_cli(
                [
                    "--candidate",
                    "unknown-policy",
                    "--baseline",
                    "two-step",
                    "--seeds",
                    "0",
                ]
            )

        self.assertEqual(caught.exception.code, 2)
        self.assertIn("invalid choice", stderr.getvalue())

    def test_unknown_baseline_name_exits_2(self) -> None:
        with (
            contextlib.redirect_stderr(io.StringIO()) as stderr,
            self.assertRaises(SystemExit) as caught,
        ):
            _run_cli(
                [
                    "--candidate",
                    "finite-horizon",
                    "--baseline",
                    "unknown-policy",
                    "--seeds",
                    "0",
                ]
            )

        self.assertEqual(caught.exception.code, 2)
        self.assertIn("invalid choice", stderr.getvalue())

    def test_zero_workers_exits_2(self) -> None:
        with (
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as caught,
        ):
            _run_cli(
                [
                    "--candidate",
                    "finite-horizon",
                    "--baseline",
                    "two-step",
                    "--seeds",
                    "0",
                    "--workers",
                    "0",
                ]
            )

        self.assertEqual(caught.exception.code, 2)

    def test_negative_workers_exits_2(self) -> None:
        with (
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as caught,
        ):
            _run_cli(
                [
                    "--candidate",
                    "finite-horizon",
                    "--baseline",
                    "two-step",
                    "--seeds",
                    "0",
                    "--workers",
                    "-1",
                ]
            )

        self.assertEqual(caught.exception.code, 2)

    def test_invalid_seed_syntax_exits_2(self) -> None:
        with (
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as caught,
        ):
            _run_cli(
                [
                    "--candidate",
                    "finite-horizon",
                    "--baseline",
                    "two-step",
                    "--seeds",
                    "foo",
                ]
            )

        self.assertEqual(caught.exception.code, 2)

    def test_same_candidate_and_baseline_identity_is_rejected_by_existing_plan_validation(
        self,
    ) -> None:
        with (
            mock.patch(
                "lisjong_arena.single_round_compare.run_single_round_evaluation"
            ) as serial,
            contextlib.redirect_stderr(io.StringIO()) as stderr,
        ):
            return_code = _run_cli(
                [
                    "--candidate",
                    "two-step",
                    "--baseline",
                    "two-step",
                    "--seeds",
                    "0",
                ]
            )

        self.assertEqual(return_code, 2)
        self.assertIn("distinct identities", stderr.getvalue())
        serial.assert_not_called()

    def test_runner_failure_does_not_print_a_success_summary(self) -> None:
        stdout = io.StringIO()
        with (
            mock.patch(
                "lisjong_arena.single_round_compare.run_single_round_evaluation",
                side_effect=RuntimeError("boom"),
            ),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(io.StringIO()) as stderr,
        ):
            return_code = _run_cli(
                [
                    "--candidate",
                    "finite-horizon",
                    "--baseline",
                    "two-step",
                    "--seeds",
                    "0",
                ]
            )

        self.assertEqual(return_code, 1)
        self.assertNotIn("Policy comparison completed", stdout.getvalue())
        self.assertIn("boom", stderr.getvalue())


class SummaryTest(unittest.TestCase):
    def test_summary_reports_expected_metrics(self) -> None:
        plan = SingleRoundEvaluationPlan(
            candidate=POLICY_CATALOG["finite-horizon"],
            baseline=POLICY_CATALOG["two-step"],
            seeds=(0,),
        )
        scores_by_rotation = {
            0: (41_000, 19_000, 20_000, 20_000),
            1: (20_000, 43_000, 18_000, 19_000),
            2: (19_000, 20_000, 45_000, 16_000),
            3: (20_000, 19_000, 21_000, 40_000),
        }
        game_results = tuple(
            _game_result_with_neutral_round_stats(
                seed=0,
                rotation=rotation,
                scores=scores_by_rotation[rotation],
            )
            for rotation in range(ROTATION_COUNT)
        )
        result = SingleRoundEvaluationResult(
            plan=plan,
            game_results=game_results,
            candidate_metrics=aggregate_candidate_metrics(
                "finite-horizon", game_results
            ),
        )

        summary = format_summary(result, workers=4)
        lines = summary.splitlines()

        self.assertEqual(lines[0], "Policy comparison completed")
        self.assertIn("protocol:   ABBB / 4p-red-single", lines)
        self.assertIn("candidate:  finite-horizon", lines)
        self.assertIn("baseline:   two-step", lines)
        self.assertIn("seeds:      0 (1)", lines)
        self.assertIn("games:      4", lines)
        self.assertIn("workers:    4", lines)
        self.assertIn("candidate mean score: 42250.0", lines)
        self.assertIn("baseline mean score:  19250.0", lines)
        self.assertIn("mean delta:            +23000.0", lines)
        self.assertIn("  seat 0: 41000.0", lines)
        self.assertIn("  seat 1: 43000.0", lines)
        self.assertIn("  seat 2: 45000.0", lines)
        self.assertIn("  seat 3: 40000.0", lines)

    def test_seed_range_description_covers_multi_seed_plans(self) -> None:
        result = _fake_result(seeds=tuple(range(0, 100)))
        summary = format_summary(result, workers=4)
        self.assertIn("seeds:      0..99 (100)", summary.splitlines())
        self.assertIn("games:      400", summary.splitlines())


class MahjongMetricsSummaryTest(unittest.TestCase):
    """Issue #61のmahjong metrics表示は、domain aggregation自体を
    ``SingleRoundCandidateMahjongMetrics``へ委ねた前提でformattingだけを
    検証する。
    """

    def test_denominator_zero_metrics_are_shown_as_na_not_zero(self) -> None:
        # candidateがwin_count=0/deal_in_count=0/exhaustive_draw_count=0/
        # tenpai_reached_count=0のため、それぞれのmean / exhaustive-draw
        # tenpai rateは0.0ではなくN/Aになる。round_countは常に正なので
        # win_rate / deal_in_rate自体は0.0%として表示される。
        result = _fake_result(seeds=(0,))
        summary = format_summary(result, workers=1)
        lines = summary.splitlines()

        self.assertIn("mahjong metrics:", lines)
        self.assertIn("  win rate:                     0.0% (0/4)", lines)
        self.assertIn("  mean win points:              N/A", lines)
        self.assertIn("  deal-in rate:                 0.0% (0/4)", lines)
        self.assertIn("  mean deal-in loss:            N/A", lines)
        self.assertIn("  exhaustive-draw tenpai rate:  N/A", lines)
        self.assertIn("  mean first-tenpai turn:       N/A", lines)

    def test_populated_metrics_are_formatted_with_counts(self) -> None:
        base_result = _fake_result(seeds=(0,))
        base_metrics = base_result.candidate_metrics
        mahjong_metrics = dataclasses.replace(
            base_metrics.mahjong_metrics,
            round_count=4,
            mean_round_score_delta=123.5,
            win_count=1,
            win_rate=0.25,
            mean_win_points=6150.0,
            deal_in_count=1,
            deal_in_rate=0.25,
            mean_deal_in_loss=5420.5,
            exhaustive_draw_count=2,
            exhaustive_draw_tenpai_count=1,
            exhaustive_draw_tenpai_rate=0.5,
            tenpai_reached_count=3,
            mean_first_tenpai_turn=9.4,
        )
        candidate_metrics = dataclasses.replace(
            base_metrics, mahjong_metrics=mahjong_metrics
        )
        result = dataclasses.replace(base_result, candidate_metrics=candidate_metrics)

        summary = format_summary(result, workers=1)
        lines = summary.splitlines()

        self.assertIn("  mean round score delta:       +123.5", lines)
        self.assertIn("  win rate:                     25.0% (1/4)", lines)
        self.assertIn("  mean win points:              6150.0", lines)
        self.assertIn("  deal-in rate:                 25.0% (1/4)", lines)
        self.assertIn("  mean deal-in loss:            5420.5", lines)
        self.assertIn("  exhaustive-draw tenpai rate:  50.0% (1/2)", lines)
        self.assertIn("  mean first-tenpai turn:       9.4", lines)


if __name__ == "__main__":
    unittest.main()
