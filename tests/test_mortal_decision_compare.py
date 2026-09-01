import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _mortal_decision_analysis_fixtures as artifact_fixtures
from test_mortal_decision_evaluation import _comparison, _local_result

from lisjong_arena.mortal_decision_analysis_artifact import (
    DECISIONS_FILENAME,
    MANIFEST_FILENAME,
    MortalDecisionAnalysisArtifactError,
    load_mortal_decision_analysis,
)
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


class MortalDecisionCompareArtifactExportTest(unittest.TestCase):
    """``--artifact-dir``のopt-in exportと、未指定時の既存behavior。"""

    def setUp(self) -> None:
        workspace = tempfile.TemporaryDirectory()
        self.addCleanup(workspace.cleanup)
        self.workspace = Path(workspace.name)
        self.config = artifact_fixtures.mortal_config(self.workspace)
        self.result = artifact_fixtures.evaluation_result(self.config, seeds=(0,))
        self.directory = self.workspace / "analysis"

    def arguments(self, *extra: str) -> list[str]:
        return [
            "--policy",
            "combined",
            "--seeds",
            "0",
            "--mortal-image",
            self.config.image,
            "--mortal-revision",
            self.config.implementation_revision,
            "--mortal-model",
            str(self.config.model_path),
            *extra,
        ]

    def run_cli(self, *extra: str, result=None, runner_error: Exception | None = None):
        stdout = io.StringIO()
        stderr = io.StringIO()
        patch = (
            mock.patch(
                f"{_MODULE}.run_mortal_decision_evaluation",
                side_effect=runner_error,
            )
            if runner_error is not None
            else mock.patch(
                f"{_MODULE}.run_mortal_decision_evaluation",
                return_value=self.result if result is None else result,
            )
        )
        with (
            patch as runner,
            artifact_fixtures.patched_provenance(),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            return_code = _run_cli(self.arguments(*extra))
        return return_code, stdout.getvalue(), stderr.getvalue(), runner

    def test_opt_out_keeps_the_existing_summary_only_behavior(self) -> None:
        return_code, stdout, stderr, _ = self.run_cli()

        self.assertEqual(return_code, 0)
        self.assertIn("Mortal same-state decision diagnostic", stdout)
        self.assertEqual(stderr, "")
        self.assertEqual(
            sorted(item.name for item in self.workspace.iterdir()), ["mortal.pth"]
        )

    def test_export_does_not_change_execution_or_decision_semantics(self) -> None:
        without = self.run_cli()
        with_export = self.run_cli("--artifact-dir", str(self.directory))

        self.assertEqual(without[0], with_export[0])
        self.assertEqual(without[1], with_export[1])
        self.assertEqual(
            without[3].call_args.args[0].policy.identity,
            with_export[3].call_args.args[0].policy.identity,
        )
        self.assertEqual(
            without[3].call_args.args[0].seeds, with_export[3].call_args.args[0].seeds
        )
        self.assertEqual(without[3].call_count, with_export[3].call_count)

        artifact = load_mortal_decision_analysis(self.directory)
        self.assertEqual(
            [row.driver_mortal_action for row in artifact.decisions],
            [record.driver_mortal_action for record in self.result.summary.records],
        )
        self.assertEqual(
            [row.shadow_policy_action for row in artifact.decisions],
            [record.shadow_policy_action for record in self.result.summary.records],
        )
        self.assertEqual(
            artifact.manifest.total_paired_decisions,
            self.result.summary.total_paired_decisions,
        )

    def test_export_writes_the_manifest_and_every_decision_row(self) -> None:
        return_code, _, _, _ = self.run_cli("--artifact-dir", str(self.directory))

        self.assertEqual(return_code, 0)
        self.assertEqual(
            sorted(item.name for item in self.directory.iterdir()),
            [DECISIONS_FILENAME, MANIFEST_FILENAME],
        )
        rows = (
            (self.directory / DECISIONS_FILENAME)
            .read_text(encoding="utf-8")
            .splitlines()
        )
        self.assertEqual(len(rows), self.result.summary.total_paired_decisions)

    def test_existing_artifact_path_fails_before_running_mortal(self) -> None:
        self.directory.mkdir()
        return_code, stdout, stderr, runner = self.run_cli(
            "--artifact-dir", str(self.directory)
        )

        self.assertEqual(return_code, 2)
        self.assertEqual(stdout, "")
        self.assertIn("--artifact-dir path already exists", stderr)
        runner.assert_not_called()

    def test_missing_parent_directory_fails_before_running_mortal(self) -> None:
        return_code, _, stderr, runner = self.run_cli(
            "--artifact-dir", str(self.workspace / "absent" / "analysis")
        )

        self.assertEqual(return_code, 2)
        self.assertIn("parent directory does not exist", stderr)
        runner.assert_not_called()

    def test_run_failure_leaves_no_artifact(self) -> None:
        return_code, stdout, stderr, _ = self.run_cli(
            "--artifact-dir",
            str(self.directory),
            runner_error=RuntimeError("Mortal failed"),
        )

        self.assertEqual(return_code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("Mortal failed", stderr)
        self.assertFalse(self.directory.exists())

    def test_export_failure_reports_and_leaves_no_complete_artifact(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            mock.patch(
                f"{_MODULE}.run_mortal_decision_evaluation", return_value=self.result
            ),
            mock.patch(
                f"{_MODULE}.save_mortal_decision_analysis",
                side_effect=MortalDecisionAnalysisArtifactError("unsupported value"),
            ),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            return_code = _run_cli(
                self.arguments("--artifact-dir", str(self.directory))
            )

        self.assertEqual(return_code, 1)
        self.assertIn("analysis artifact export failed", stderr.getvalue())
        self.assertFalse(self.directory.exists())


if __name__ == "__main__":
    unittest.main()
