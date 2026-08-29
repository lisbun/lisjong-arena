"""``lisjong_arena.policy_performance_profile`` CLIのunit test。

既存``run_policy_timing_profile()`` / ``run_policy_hotspot_profile()``はmockし、
CLIのwiring(Policy名解決、seed解析、mode dispatch、report formatting、
fail closed、serial-only scope)だけを検証する。timing / profile modeの
計測semantics自体は``tests.test_policy_performance``が固定しているため、
ここでは実RiichiEnvを起動する新しいlong-running testを追加しない。
"""

import contextlib
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
from lisjong_arena.policy_performance import (
    DecisionTimingMetrics,
    PolicyHotspotProfileResult,
    PolicyTimingProfileResult,
    ProfileFunctionStat,
)
from lisjong_arena.policy_performance_profile import (
    _run_cli,
    build_arg_parser,
    format_hotspot_report,
    format_timing_report,
)
from lisjong_arena.single_round_evaluation import (
    ROTATION_COUNT,
    aggregate_candidate_metrics,
)


def _game_result(
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


def _fake_evaluation_result(
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
        _game_result(
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


def _fake_timing_metrics() -> DecisionTimingMetrics:
    return DecisionTimingMetrics(
        decision_count=4,
        total_decision_time_ns=4_000_000,
        mean_decision_latency_ns=1_000_000.0,
        p50_decision_latency_ns=900_000,
        p95_decision_latency_ns=1_900_000,
        max_decision_latency_ns=2_000_000,
        decisions_per_second=1_000.0,
    )


def _fake_timing_profile(**overrides: object) -> PolicyTimingProfileResult:
    fields: dict[str, object] = dict(
        result=_fake_evaluation_result(),
        candidate_decision_metrics=_fake_timing_metrics(),
        evaluation_elapsed_seconds=2.5,
        games_per_second=1.6,
    )
    fields.update(overrides)
    return PolicyTimingProfileResult(**fields)


def _fake_hotspot_profile(**overrides: object) -> PolicyHotspotProfileResult:
    fields: dict[str, object] = dict(
        result=_fake_evaluation_result(),
        function_stats=(
            ProfileFunctionStat(
                module="lisjong.hand_evaluation.shanten",
                qualified_name="/x/lisjong/hand_evaluation/shanten.py:10(calculate_standard_shanten)",
                call_count=1000,
                self_time_seconds=0.5,
                cumulative_time_seconds=0.6,
            ),
            ProfileFunctionStat(
                module="",
                qualified_name="~:0(builtins.sum)",
                call_count=10,
                self_time_seconds=0.01,
                cumulative_time_seconds=0.01,
            ),
        ),
    )
    fields.update(overrides)
    return PolicyHotspotProfileResult(**fields)


class ArgParserTest(unittest.TestCase):
    def test_candidate_and_baseline_choices_are_first_party_only(self) -> None:
        parser = build_arg_parser(prog="test")
        candidate_action = next(
            action for action in parser._actions if action.dest == "candidate"
        )
        baseline_action = next(
            action for action in parser._actions if action.dest == "baseline"
        )
        self.assertEqual(set(candidate_action.choices), set(POLICY_CATALOG))
        self.assertEqual(set(baseline_action.choices), set(POLICY_CATALOG))
        self.assertNotIn("mortal", candidate_action.choices)

    def test_mode_is_required_and_exclusive(self) -> None:
        parser = build_arg_parser(prog="test")
        mode_action = next(
            action for action in parser._actions if action.dest == "mode"
        )
        self.assertTrue(mode_action.required)
        self.assertEqual(set(mode_action.choices), {"timing", "profile"})

    def test_no_workers_option_exists(self) -> None:
        parser = build_arg_parser(prog="test")
        option_strings = {
            option for action in parser._actions for option in action.option_strings
        }
        self.assertNotIn("--workers", option_strings)

    def test_unknown_candidate_is_rejected(self) -> None:
        parser = build_arg_parser(prog="test")
        with (
            self.assertRaises(SystemExit),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            parser.parse_args(
                [
                    "--candidate",
                    "not-a-real-policy",
                    "--baseline",
                    "two-step",
                    "--seeds",
                    "0",
                    "--mode",
                    "timing",
                ]
            )


class CliDispatchTest(unittest.TestCase):
    def test_timing_mode_calls_only_the_timing_entry_point(self) -> None:
        with (
            mock.patch(
                "lisjong_arena.policy_performance_profile.run_policy_timing_profile",
                return_value=_fake_timing_profile(),
            ) as timing,
            mock.patch(
                "lisjong_arena.policy_performance_profile.run_policy_hotspot_profile",
                side_effect=AssertionError("profile entry point must not be called"),
            ) as hotspot,
            contextlib.redirect_stdout(io.StringIO()) as stdout,
        ):
            return_code = _run_cli(
                [
                    "--candidate",
                    "finite-horizon",
                    "--baseline",
                    "two-step",
                    "--seeds",
                    "0",
                    "--mode",
                    "timing",
                ]
            )

        self.assertEqual(return_code, 0)
        timing.assert_called_once()
        hotspot.assert_not_called()
        self.assertIn("timing mode", stdout.getvalue())
        self.assertIn("candidate decisions:", stdout.getvalue())

    def test_profile_mode_calls_only_the_hotspot_entry_point(self) -> None:
        with (
            mock.patch(
                "lisjong_arena.policy_performance_profile.run_policy_timing_profile",
                side_effect=AssertionError("timing entry point must not be called"),
            ) as timing,
            mock.patch(
                "lisjong_arena.policy_performance_profile.run_policy_hotspot_profile",
                return_value=_fake_hotspot_profile(),
            ) as hotspot,
            contextlib.redirect_stdout(io.StringIO()) as stdout,
        ):
            return_code = _run_cli(
                [
                    "--candidate",
                    "combined",
                    "--baseline",
                    "two-step",
                    "--seeds",
                    "0",
                    "--mode",
                    "profile",
                ]
            )

        self.assertEqual(return_code, 0)
        hotspot.assert_called_once()
        timing.assert_not_called()
        self.assertIn("profile mode", stdout.getvalue())
        self.assertIn("calculate_standard_shanten", stdout.getvalue())

    def test_same_candidate_and_baseline_identity_is_rejected(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()) as stderr:
            return_code = _run_cli(
                [
                    "--candidate",
                    "finite-horizon",
                    "--baseline",
                    "finite-horizon",
                    "--seeds",
                    "0",
                    "--mode",
                    "timing",
                ]
            )
        self.assertEqual(return_code, 2)
        self.assertIn("invalid comparison", stderr.getvalue())

    def test_execution_failure_does_not_print_a_partial_report(self) -> None:
        with (
            mock.patch(
                "lisjong_arena.policy_performance_profile.run_policy_timing_profile",
                side_effect=RuntimeError("boom"),
            ),
            contextlib.redirect_stdout(io.StringIO()) as stdout,
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
                    "--mode",
                    "timing",
                ]
            )

        self.assertEqual(return_code, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("policy performance profile failed", stderr.getvalue())
        self.assertIn("boom", stderr.getvalue())


class FormatTimingReportTest(unittest.TestCase):
    def test_report_contains_protocol_and_metrics_fields(self) -> None:
        report = format_timing_report(_fake_timing_profile())

        self.assertIn("protocol:       ABBB / 4p-red-single", report)
        self.assertIn("candidate:      finite-horizon", report)
        self.assertIn("baseline:       two-step", report)
        self.assertIn("workers:        1", report)
        self.assertIn("count:        4", report)
        self.assertIn("throughput:", report)
        self.assertIn("decisions/s", report)
        self.assertIn("games/s", report)

    def test_report_never_mentions_profile_mode_language(self) -> None:
        report = format_timing_report(_fake_timing_profile())
        self.assertNotIn("hotspot", report.lower())


class FormatHotspotReportTest(unittest.TestCase):
    def test_report_lists_top_n_hotspots_and_disclaims_absolute_latency(self) -> None:
        report = format_hotspot_report(_fake_hotspot_profile(), top_n=1)

        self.assertIn("calculate_standard_shanten", report)
        self.assertNotIn("builtins.sum", report)  # top_n=1 keeps only the hottest row
        self.assertIn("do not use this elapsed time as an absolute latency", report)

    def test_report_shows_all_rows_when_top_n_covers_every_stat(self) -> None:
        report = format_hotspot_report(_fake_hotspot_profile(), top_n=25)
        self.assertIn("calculate_standard_shanten", report)
        self.assertIn("builtins.sum", report)


if __name__ == "__main__":
    unittest.main()
