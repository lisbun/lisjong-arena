import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _round_stats_fixtures import neutral_seat_round_stats_tuple
from lisjong.policy_contract import Seat

from lisjong_arena.model import SingleRoundGameResult
from lisjong_arena.mortal_runtime import MortalDockerConfig
from lisjong_arena.mortal_single_round_evaluation import (
    MortalSingleRoundEvaluationPlan,
    MortalSingleRoundEvaluationResult,
)
from lisjong_arena.policy_catalog import POLICY_CATALOG
from lisjong_arena.single_round_compare import _run_cli, format_summary
from lisjong_arena.single_round_evaluation import aggregate_candidate_metrics

_MODULE = "lisjong_arena.single_round_compare"


class MortalSingleRoundCompareTest(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.model_path = Path(directory.name) / "mortal.pth"
        self.model_path.write_bytes(b"mortal-model")

    def arguments(self, *extra: str) -> list[str]:
        return [
            "--candidate",
            "mortal",
            "--baseline",
            "two-step",
            "--seeds",
            "0",
            "--workers",
            "1",
            "--mortal-image",
            "mortal@sha256:image",
            "--mortal-revision",
            "0cff2b52982be5b1163aa9a62fb01f03ce91e0d2",
            "--mortal-model",
            str(self.model_path),
            *extra,
        ]

    def result(self) -> MortalSingleRoundEvaluationResult:
        config = MortalDockerConfig(
            image="mortal@sha256:image",
            implementation_revision="0cff2b5",
            model_path=self.model_path,
        )
        plan = MortalSingleRoundEvaluationPlan(
            baseline=POLICY_CATALOG["two-step"],
            seeds=(0,),
            mortal_config=config,
        )
        game_results = tuple(
            SingleRoundGameResult(
                seed=0,
                rotation=rotation,
                game_mode="4p-red-single",
                candidate_seat=Seat(rotation),
                scores=tuple(40000 if seat == rotation else 20000 for seat in range(4)),
                seat_round_stats=neutral_seat_round_stats_tuple(
                    tuple(40000 if seat == rotation else 20000 for seat in range(4))
                ),
            )
            for rotation in range(4)
        )
        return MortalSingleRoundEvaluationResult(
            plan=plan,
            game_results=game_results,
            candidate_metrics=aggregate_candidate_metrics("mortal", game_results),
        )

    def test_mortal_is_not_registered_as_a_policy(self) -> None:
        self.assertNotIn("mortal", POLICY_CATALOG)

    def test_cli_dispatches_to_serial_mortal_runner(self) -> None:
        result = self.result()
        stdout = io.StringIO()
        with (
            mock.patch(
                f"{_MODULE}.run_mortal_single_round_evaluation",
                return_value=result,
            ) as mortal_runner,
            mock.patch(
                f"{_MODULE}.run_single_round_evaluation",
                side_effect=AssertionError("Policy runner must not be used"),
            ),
            contextlib.redirect_stdout(stdout),
        ):
            return_code = _run_cli(self.arguments())

        self.assertEqual(return_code, 0)
        mortal_runner.assert_called_once()
        plan = mortal_runner.call_args.args[0]
        self.assertIsInstance(plan, MortalSingleRoundEvaluationPlan)
        self.assertEqual(plan.baseline, POLICY_CATALOG["two-step"])
        self.assertEqual(plan.mortal_config.image, "mortal@sha256:image")
        self.assertIn("candidate:  mortal", stdout.getvalue())

    def test_workers_greater_than_one_is_rejected_before_execution(self) -> None:
        arguments = self.arguments()
        arguments[arguments.index("--workers") + 1] = "2"
        stderr = io.StringIO()
        with (
            mock.patch(f"{_MODULE}.run_mortal_single_round_evaluation") as runner,
            contextlib.redirect_stderr(stderr),
        ):
            return_code = _run_cli(arguments)

        self.assertEqual(return_code, 2)
        runner.assert_not_called()
        self.assertIn("requires --workers 1", stderr.getvalue())

    def test_missing_runtime_configuration_is_rejected(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            return_code = _run_cli(
                [
                    "--candidate",
                    "mortal",
                    "--baseline",
                    "two-step",
                    "--seeds",
                    "0",
                    "--workers",
                    "1",
                ]
            )

        self.assertEqual(return_code, 2)
        self.assertIn("--mortal-image", stderr.getvalue())
        self.assertIn("--mortal-revision", stderr.getvalue())
        self.assertIn("--mortal-model", stderr.getvalue())

    def test_non_two_step_baseline_is_rejected(self) -> None:
        arguments = self.arguments()
        arguments[arguments.index("--baseline") + 1] = "finite-horizon"
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            return_code = _run_cli(arguments)

        self.assertEqual(return_code, 2)
        self.assertIn("baseline must be two-step", stderr.getvalue())

    def test_failure_prints_no_partial_summary(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            mock.patch(
                f"{_MODULE}.run_mortal_single_round_evaluation",
                side_effect=RuntimeError("Mortal failed"),
            ),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            return_code = _run_cli(self.arguments())

        self.assertEqual(return_code, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("Mortal failed", stderr.getvalue())

    def test_summary_records_mortal_provenance_and_existing_metrics(self) -> None:
        summary = format_summary(self.result(), workers=1)

        self.assertIn("Docker image:             mortal@sha256:image", summary)
        self.assertIn("implementation revision:  0cff2b5", summary)
        self.assertIn(f"model path:               {self.model_path.resolve()}", summary)
        self.assertIn("model SHA256:", summary)
        self.assertIn("candidate mean score: 40000.0", summary)
        self.assertIn("mahjong metrics:", summary)


if __name__ == "__main__":
    unittest.main()
