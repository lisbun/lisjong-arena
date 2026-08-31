"""``lisjong_arena.summarize_single_round_artifacts`` CLIのunit test。

artifactのload / merge / aggregation semanticsは
``tests.test_single_round_artifact``が固定しているため、ここではCLIのwiring
(argument parsing、artifact順序、summary formatting、fail closed)だけを
検証する。
"""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

import _single_round_artifact_fixtures as fixtures

from lisjong_arena.single_round_compare import format_summary
from lisjong_arena.summarize_single_round_artifacts import _run_cli


def _run(paths: list[Path]) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        exit_code = _run_cli([str(path) for path in paths])
    return exit_code, stdout.getvalue(), stderr.getvalue()


def _strength_body(text: str) -> str:
    """strength summary本体(scores〜seed-block statistics)だけを切り出す。"""
    start = text.index("candidate mean score:")
    end = text.find("\n", text.index("negative seed blocks:"))
    return text[start:] if end == -1 else text[start:end]


class SingleArtifactSummaryTest(unittest.TestCase):
    def test_summarizes_one_artifact(self) -> None:
        result = fixtures.evaluation_result(seeds=(20_200, 20_201))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run.json"
            fixtures.save(result, path)

            exit_code, stdout, stderr = _run([path])

        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("Single-round strength artifact summary", stdout)
        self.assertIn("protocol:   ABBB / 4p-red-single", stdout)
        self.assertIn(f"candidate:  {fixtures.CANDIDATE}", stdout)
        self.assertIn(f"baseline:   {fixtures.BASELINE}", stdout)
        self.assertIn("seeds:      20200..20201 (2)", stdout)
        self.assertIn("games:      8", stdout)
        self.assertIn("artifacts:  1", stdout)
        self.assertIn("run.json: seeds 20200..20201 (2), games 8", stdout)

    def test_strength_metrics_match_the_single_round_compare_summary(self) -> None:
        result = fixtures.evaluation_result(seeds=(20_200, 20_201))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run.json"
            fixtures.save(result, path)

            _, stdout, _ = _run([path])

        self.assertEqual(
            _strength_body(stdout),
            _strength_body(format_summary(result, workers=4)),
        )

    def test_execution_presentation_fields_are_not_invented(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run.json"
            fixtures.save(fixtures.evaluation_result(), path)

            _, stdout, _ = _run([path])

        self.assertNotIn("workers", stdout)
        self.assertNotIn("elapsed", stdout)

    def test_reports_reproducibility_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run.json"
            fixtures.save(fixtures.evaluation_result(), path)

            _, stdout, _ = _run([path])

        self.assertIn("provenance:", stdout)
        self.assertIn(fixtures.ARENA_REVISION, stdout)
        self.assertIn(fixtures.LISJONG_REVISION, stdout)
        self.assertIn(fixtures.LISJONG_ENGINE_REVISION, stdout)
        self.assertIn("RiichiEnv version:", stdout)
        self.assertIn("Python version:", stdout)


class CumulativeSummaryTest(unittest.TestCase):
    def test_summarizes_compatible_disjoint_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "run-a.json"
            second = Path(directory) / "run-b.json"
            fixtures.save(fixtures.evaluation_result(seeds=(1, 2)), first)
            fixtures.save(fixtures.evaluation_result(seeds=(3,)), second)

            exit_code, stdout, stderr = _run([first, second])

        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("artifacts:  2", stdout)
        self.assertIn("games:      12", stdout)
        self.assertIn("seeds:      1..3 (3)", stdout)
        self.assertIn("[1] ", stdout)
        self.assertIn("[2] ", stdout)

    def test_cumulative_metrics_equal_a_one_shot_run_over_the_same_seeds(
        self,
    ) -> None:
        one_shot = fixtures.evaluation_result(seeds=(1, 2, 3))
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "run-a.json"
            second = Path(directory) / "run-b.json"
            fixtures.save(fixtures.evaluation_result(seeds=(1, 2)), first)
            fixtures.save(fixtures.evaluation_result(seeds=(3,)), second)

            _, stdout, _ = _run([first, second])

        self.assertEqual(
            _strength_body(stdout),
            _strength_body(format_summary(one_shot, workers=1)),
        )


class FailClosedTest(unittest.TestCase):
    def test_missing_artifact_exits_non_zero_without_a_summary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            exit_code, stdout, stderr = _run([Path(directory) / "absent.json"])

        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("failed to load artifact", stderr)

    def test_malformed_artifact_exits_non_zero_without_a_summary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run.json"
            path.write_text('{"schema_version": 1', encoding="utf-8")

            exit_code, stdout, stderr = _run([path])

        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("failed to load artifact", stderr)

    def test_incompatible_artifacts_exit_non_zero_without_a_summary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "run-a.json"
            second = Path(directory) / "run-b.json"
            fixtures.save(fixtures.evaluation_result(seeds=(1,)), first)
            fixtures.save(
                fixtures.evaluation_result(seeds=(2,), candidate="two-step"), second
            )

            exit_code, stdout, stderr = _run([first, second])

        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("incompatible artifacts", stderr)

    def test_overlapping_seeds_exit_non_zero_without_a_summary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "run-a.json"
            second = Path(directory) / "run-b.json"
            fixtures.save(fixtures.evaluation_result(seeds=(1, 2)), first)
            fixtures.save(fixtures.evaluation_result(seeds=(2, 3)), second)

            exit_code, stdout, stderr = _run([first, second])

        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("incompatible artifacts", stderr)

    def test_no_artifact_path_is_rejected_by_argparse(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as context:
                _run_cli([])
        self.assertEqual(context.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
