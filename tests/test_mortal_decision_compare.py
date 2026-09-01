import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test_mortal_decision_evaluation import _comparison, _local_result

from lisjong_arena.mortal_decision_compare import _run_cli, format_summary
from lisjong_arena.mortal_decision_evaluation import (
    MortalDecisionEvaluationPlan,
    run_mortal_decision_evaluation,
)
from lisjong_arena.mortal_runtime import MortalDockerConfig
from lisjong_arena.policy_catalog import POLICY_CATALOG

_MODULE = "lisjong_arena.mortal_decision_compare"


class MortalDecisionCompareCliTest(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.model = Path(directory.name) / "mortal.pth"
        self.model.write_bytes(b"model")
        self.config = MortalDockerConfig(
            image="mortal@sha256:image",
            implementation_revision="revision",
            model_path=self.model,
        )

    def arguments(self):
        return [
            "--policy",
            "combined",
            "--seeds",
            "0",
            "--mortal-image",
            "mortal@sha256:image",
            "--mortal-revision",
            "revision",
            "--mortal-model",
            str(self.model),
        ]

    def result(self):
        plan = MortalDecisionEvaluationPlan(
            policy=POLICY_CATALOG["combined"],
            seeds=(0,),
            mortal_config=self.config,
        )

        def run_game(
            policies,
            shadow_policy,
            *,
            mortal_seat,
            seed,
            **kwargs,
        ):
            return _local_result(seed, mortal_seat), (
                _comparison(seed, int(mortal_seat)),
            )

        with mock.patch(
            "lisjong_arena.mortal_decision_evaluation._run_mortal_decision_game",
            side_effect=run_game,
        ):
            return run_mortal_decision_evaluation(plan)

    def test_cli_resolves_policy_and_dispatches_dedicated_diagnostic(self) -> None:
        result = self.result()
        stdout = io.StringIO()
        with (
            mock.patch(
                f"{_MODULE}.run_mortal_decision_evaluation", return_value=result
            ) as runner,
            contextlib.redirect_stdout(stdout),
        ):
            return_code = _run_cli(self.arguments())

        self.assertEqual(return_code, 0)
        runner.assert_called_once()
        plan = runner.call_args.args[0]
        self.assertIs(plan.policy, POLICY_CATALOG["combined"])
        self.assertEqual(plan.seeds, (0,))
        self.assertIn("Mortal same-state decision diagnostic", stdout.getvalue())

    def test_cli_reuses_explicit_policy_reference_resolution(self) -> None:
        arguments = self.arguments()
        arguments[arguments.index("combined")] = "lisjong.policies:MinimalPolicy"
        arguments[2:2] = ["--policy-id", "shadow-minimal"]
        with (
            mock.patch(
                f"{_MODULE}.run_mortal_decision_evaluation", return_value=self.result()
            ) as runner,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            return_code = _run_cli(arguments)

        self.assertEqual(return_code, 0)
        self.assertEqual(runner.call_args.args[0].policy.identity, "shadow-minimal")

    def test_summary_separates_diagnostic_from_strength_and_states_roles(self) -> None:
        summary = format_summary(self.result())

        self.assertIn("total:          4", summary)
        self.assertIn("agreements:     4", summary)
        self.assertIn("pass / pass: 4", summary)
        self.assertIn("Mortal driver / lisjong shadow", summary)
        self.assertIn("not an error or ground truth", summary)
        self.assertIn("shadow-only", summary)
        self.assertNotIn("mean score", summary)

    def test_failure_prints_no_partial_summary(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            mock.patch(
                f"{_MODULE}.run_mortal_decision_evaluation",
                side_effect=RuntimeError("Mortal failed"),
            ),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            return_code = _run_cli(self.arguments())

        self.assertEqual(return_code, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("Mortal failed", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
