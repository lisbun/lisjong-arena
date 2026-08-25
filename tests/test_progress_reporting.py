"""Issue #63のABBB execution progress reportingを高速に検証する。

実RiichiEnvを長時間起動せず、serial / parallelのcompletion notificationと
``single_round_compare --progress``のpresentation contractだけを固定する。
"""

import contextlib
import io
import unittest
from unittest import mock

from _round_stats_fixtures import neutral_seat_round_stats_tuple
from lisjong.policy_contract import Seat

from lisjong_arena._parallel_execution import GameJob, GameJobOutcome, run_game_jobs
from lisjong_arena.model import (
    PolicySpec,
    SingleRoundEvaluationPlan,
    SingleRoundEvaluationResult,
    SingleRoundGameResult,
)
from lisjong_arena.policy_catalog import POLICY_CATALOG
from lisjong_arena.riichienv.local_game_runner import LocalGameResult
from lisjong_arena.single_round_compare import _ProgressReporter, _run_cli
from lisjong_arena.single_round_evaluation import (
    ROTATION_COUNT,
    aggregate_candidate_metrics,
    run_single_round_evaluation,
    run_single_round_evaluation_parallel,
)


def _local_result(seed: int) -> LocalGameResult:
    scores = (25_000, 25_000, 25_000, 25_000)
    return LocalGameResult(
        seed=seed,
        game_mode="4p-red-single",
        scores=scores,
        ranks=(1, 2, 3, 4),
        steps=1,
        decisions=4,
        seat_round_stats=neutral_seat_round_stats_tuple(scores),
    )


def _fake_result() -> SingleRoundEvaluationResult:
    plan = SingleRoundEvaluationPlan(
        candidate=POLICY_CATALOG["finite-horizon"],
        baseline=POLICY_CATALOG["two-step"],
        seeds=(0,),
    )
    scores = (25_000, 25_000, 25_000, 25_000)
    game_results = tuple(
        SingleRoundGameResult(
            seed=0,
            rotation=rotation,
            game_mode="4p-red-single",
            candidate_seat=Seat(rotation),
            scores=scores,
            seat_round_stats=neutral_seat_round_stats_tuple(scores),
        )
        for rotation in range(ROTATION_COUNT)
    )
    return SingleRoundEvaluationResult(
        plan=plan,
        game_results=game_results,
        candidate_metrics=aggregate_candidate_metrics("finite-horizon", game_results),
    )


class _StubPolicy:
    def select_action(self, policy_input):
        raise AssertionError("test stub policy must not execute")


def _stub_factory() -> _StubPolicy:
    return _StubPolicy()


def _parallel_ok_runner(job: GameJob) -> GameJobOutcome:
    return GameJobOutcome(
        seed=job.seed,
        rotation=job.rotation,
        result=f"ok:{job.seed}:{job.rotation}",
        error_text=None,
    )


def _parallel_jobs(count: int) -> list[GameJob]:
    spec = PolicySpec(identity="stub", factory=_stub_factory)
    assignment = (spec, spec, spec, spec)
    return [
        GameJob(
            seed=index,
            rotation=0,
            assignment=assignment,
            game_mode="4p-red-single",
            max_steps=100,
        )
        for index in range(count)
    ]


class ProgressReporterTest(unittest.TestCase):
    def test_formats_completed_elapsed_and_eta_on_one_rewritten_line(self) -> None:
        stream = io.StringIO()
        times = iter((100.0, 110.0, 140.0))
        reporter = _ProgressReporter(4, stream=stream, clock=lambda: next(times))

        reporter(1, 4)
        reporter(4, 4)

        output = stream.getvalue()
        self.assertIn("0/4", output)
        self.assertIn("ETA calculating", output)
        self.assertIn("1/4", output)
        self.assertIn("elapsed 00:10 ETA       00:30", output)
        self.assertIn("4/4", output)
        self.assertIn("elapsed 00:40 ETA       00:00", output)
        self.assertIn("\r", output)
        self.assertTrue(output.endswith("\n"))

    def test_rejects_changed_total(self) -> None:
        reporter = _ProgressReporter(4, stream=io.StringIO(), clock=lambda: 0.0)
        with self.assertRaises(ValueError):
            reporter(1, 8)


class SerialProgressCallbackTest(unittest.TestCase):
    def test_serial_runner_notifies_each_successful_game(self) -> None:
        plan = SingleRoundEvaluationPlan(
            candidate=POLICY_CATALOG["finite-horizon"],
            baseline=POLICY_CATALOG["two-step"],
            seeds=(0,),
        )
        notifications = []

        with mock.patch(
            "lisjong_arena.single_round_evaluation._run_single_game",
            side_effect=lambda policies, *, seed, max_steps: _local_result(seed),
        ):
            result = run_single_round_evaluation(
                plan,
                progress_callback=lambda completed, total: notifications.append(
                    (completed, total)
                ),
            )

        self.assertEqual(len(result.game_results), 4)
        self.assertEqual(
            notifications,
            [(1, 4), (2, 4), (3, 4), (4, 4)],
        )


class ParallelExecutorProgressCallbackTest(unittest.TestCase):
    def test_parent_process_notifies_as_successful_futures_complete(self) -> None:
        notifications = []
        jobs = _parallel_jobs(4)

        outcomes = run_game_jobs(
            jobs,
            max_workers=2,
            game_runner=_parallel_ok_runner,
            progress_callback=lambda completed, total: notifications.append(
                (completed, total)
            ),
        )

        self.assertEqual(len(outcomes), 4)
        self.assertEqual(
            notifications,
            [(1, 4), (2, 4), (3, 4), (4, 4)],
        )


class ParallelEvaluationProgressCallbackTest(unittest.TestCase):
    def test_parallel_abbb_runner_forwards_progress_callback_without_changing_order(self) -> None:
        plan = SingleRoundEvaluationPlan(
            candidate=POLICY_CATALOG["finite-horizon"],
            baseline=POLICY_CATALOG["two-step"],
            seeds=(0,),
        )
        notifications = []

        def fake_run_game_jobs(jobs, *, max_workers, progress_callback=None):
            self.assertEqual(max_workers, 2)
            outcomes = {}
            for completed, job in enumerate(jobs, start=1):
                outcomes[(job.seed, job.rotation)] = GameJobOutcome(
                    seed=job.seed,
                    rotation=job.rotation,
                    result=_local_result(job.seed),
                    error_text=None,
                )
                if progress_callback is not None:
                    progress_callback(completed, len(jobs))
            return outcomes

        with mock.patch(
            "lisjong_arena.single_round_evaluation.run_game_jobs",
            side_effect=fake_run_game_jobs,
        ):
            result = run_single_round_evaluation_parallel(
                plan,
                max_workers=2,
                progress_callback=lambda completed, total: notifications.append(
                    (completed, total)
                ),
            )

        self.assertEqual(
            [(item.seed, item.rotation) for item in result.game_results],
            [(0, 0), (0, 1), (0, 2), (0, 3)],
        )
        self.assertEqual(
            notifications,
            [(1, 4), (2, 4), (3, 4), (4, 4)],
        )


class CliProgressTest(unittest.TestCase):
    def test_progress_is_opt_in_and_default_runner_call_is_unchanged(self) -> None:
        stderr = io.StringIO()
        with (
            mock.patch(
                "lisjong_arena.single_round_compare.run_single_round_evaluation",
                return_value=_fake_result(),
            ) as serial,
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(stderr),
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

        self.assertEqual(return_code, 0)
        serial.assert_called_once()
        self.assertEqual(serial.call_args.kwargs, {})
        self.assertEqual(stderr.getvalue(), "")

    def test_progress_goes_to_stderr_and_final_summary_stays_on_stdout(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()

        def serial_with_progress(plan, *, progress_callback):
            for completed in range(1, 5):
                progress_callback(completed, 4)
            return _fake_result()

        with (
            mock.patch(
                "lisjong_arena.single_round_compare.run_single_round_evaluation",
                side_effect=serial_with_progress,
            ),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            return_code = _run_cli(
                [
                    "--candidate",
                    "finite-horizon",
                    "--baseline",
                    "two-step",
                    "--seeds",
                    "0",
                    "--progress",
                ]
            )

        self.assertEqual(return_code, 0)
        self.assertIn("Policy comparison completed", stdout.getvalue())
        self.assertNotIn("Policy comparison completed", stderr.getvalue())
        self.assertIn("0/4", stderr.getvalue())
        self.assertIn("4/4", stderr.getvalue())
        self.assertIn("ETA", stderr.getvalue())

    def test_failure_with_progress_does_not_print_partial_success_summary(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()

        def failing_serial(plan, *, progress_callback):
            progress_callback(1, 4)
            raise RuntimeError("boom")

        with (
            mock.patch(
                "lisjong_arena.single_round_compare.run_single_round_evaluation",
                side_effect=failing_serial,
            ),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            return_code = _run_cli(
                [
                    "--candidate",
                    "finite-horizon",
                    "--baseline",
                    "two-step",
                    "--seeds",
                    "0",
                    "--progress",
                ]
            )

        self.assertEqual(return_code, 1)
        self.assertNotIn("Policy comparison completed", stdout.getvalue())
        self.assertIn("1/4", stderr.getvalue())
        self.assertIn("boom", stderr.getvalue())
        self.assertTrue(stderr.getvalue().endswith("\n"))


if __name__ == "__main__":
    unittest.main()
