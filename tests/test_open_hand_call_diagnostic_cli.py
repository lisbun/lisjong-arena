"""Issue #196 CLI dispatch and provenance preflight tests。"""

from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest import mock

from _single_round_artifact_fixtures import provenance
from test_open_hand_call_diagnostic_artifact import _diagnostic_result

from lisjong_arena.open_hand_call_diagnostics import (
    OPEN_HAND_DIAGNOSTIC_LISJONG_REVISION,
)
from lisjong_arena.single_round_compare import _run_cli


def _arguments(root: Path, *, workers: int = 1) -> list[str]:
    return [
        "--candidate",
        "lisjong.policies:OpenHandYakuAwareCallPolicy",
        "--candidate-id",
        "open-hand-yaku-aware-call",
        "--baseline",
        "yakuhai-call",
        "--seeds",
        "101",
        "--workers",
        str(workers),
        "--artifact-out",
        str(root / "strength.json"),
        "--open-hand-call-diagnostics-out",
        str(root / "diagnostic.json"),
    ]


def _valid_provenance():
    return replace(provenance(), lisjong_revision=OPEN_HAND_DIAGNOSTIC_LISJONG_REVISION)


class OpenHandDiagnosticCliTest(unittest.TestCase):
    def test_serial_diagnostic_dispatch_saves_strength_then_bound_sidecar(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            diagnostic_result = _diagnostic_result()
            with (
                mock.patch(
                    "lisjong_arena.single_round_compare.collect_execution_provenance",
                    return_value=_valid_provenance(),
                ),
                mock.patch(
                    "lisjong_arena.single_round_compare."
                    "run_open_hand_diagnostic_evaluation",
                    return_value=diagnostic_result,
                ) as serial,
                mock.patch(
                    "lisjong_arena.single_round_compare."
                    "run_open_hand_diagnostic_evaluation_parallel"
                ) as parallel,
                mock.patch(
                    "lisjong_arena.single_round_compare.save_single_round_artifact"
                ) as save_strength,
                mock.patch(
                    "lisjong_arena.single_round_compare."
                    "save_open_hand_diagnostic_artifact"
                ) as save_diagnostic,
                mock.patch(
                    "lisjong_arena.single_round_compare."
                    "load_open_hand_diagnostic_artifact"
                ) as load_diagnostic,
                redirect_stdout(io.StringIO()),
                redirect_stderr(io.StringIO()),
            ):
                return_code = _run_cli(_arguments(root))

            self.assertEqual(return_code, 0)
            serial.assert_called_once()
            parallel.assert_not_called()
            save_strength.assert_called_once_with(
                diagnostic_result.evaluation_result, root / "strength.json"
            )
            save_diagnostic.assert_called_once()
            load_diagnostic.assert_called_once()

    def test_parallel_diagnostic_dispatch_uses_existing_worker_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            diagnostic_result = _diagnostic_result()
            with (
                mock.patch(
                    "lisjong_arena.single_round_compare.collect_execution_provenance",
                    return_value=_valid_provenance(),
                ),
                mock.patch(
                    "lisjong_arena.single_round_compare."
                    "run_open_hand_diagnostic_evaluation"
                ) as serial,
                mock.patch(
                    "lisjong_arena.single_round_compare."
                    "run_open_hand_diagnostic_evaluation_parallel",
                    return_value=diagnostic_result,
                ) as parallel,
                mock.patch(
                    "lisjong_arena.single_round_compare.save_single_round_artifact"
                ),
                mock.patch(
                    "lisjong_arena.single_round_compare."
                    "save_open_hand_diagnostic_artifact"
                ),
                mock.patch(
                    "lisjong_arena.single_round_compare."
                    "load_open_hand_diagnostic_artifact"
                ),
                redirect_stdout(io.StringIO()),
                redirect_stderr(io.StringIO()),
            ):
                return_code = _run_cli(_arguments(root, workers=2))

            self.assertEqual(return_code, 0)
            serial.assert_not_called()
            self.assertEqual(parallel.call_args.kwargs["max_workers"], 2)

    def test_provenance_unavailable_fails_before_evaluation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stderr = io.StringIO()
            with (
                mock.patch(
                    "lisjong_arena.single_round_compare.collect_execution_provenance",
                    side_effect=RuntimeError("missing VCS metadata"),
                ),
                mock.patch(
                    "lisjong_arena.single_round_compare."
                    "run_open_hand_diagnostic_evaluation"
                ) as runner,
                redirect_stdout(io.StringIO()),
                redirect_stderr(stderr),
            ):
                return_code = _run_cli(_arguments(root))

            self.assertEqual(return_code, 1)
            self.assertIn("provenance preflight failed", stderr.getvalue())
            runner.assert_not_called()

    def test_wrong_lisjong_revision_fails_before_evaluation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stderr = io.StringIO()
            wrong_revision = replace(_valid_provenance(), lisjong_revision="0" * 40)
            with (
                mock.patch(
                    "lisjong_arena.single_round_compare.collect_execution_provenance",
                    return_value=wrong_revision,
                ),
                mock.patch(
                    "lisjong_arena.single_round_compare."
                    "run_open_hand_diagnostic_evaluation"
                ) as runner,
                redirect_stdout(io.StringIO()),
                redirect_stderr(stderr),
            ):
                return_code = _run_cli(_arguments(root))

            self.assertEqual(return_code, 1)
            self.assertIn(OPEN_HAND_DIAGNOSTIC_LISJONG_REVISION, stderr.getvalue())
            runner.assert_not_called()

    def test_purpose_specific_option_rejects_non_exact_candidate_reference(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            arguments = _arguments(root)
            arguments[1] = "lisjong.policies:TwoStepUkeirePolicy"
            stderr = io.StringIO()
            with (
                mock.patch(
                    "lisjong_arena.single_round_compare.collect_execution_provenance"
                ) as preflight,
                redirect_stdout(io.StringIO()),
                redirect_stderr(stderr),
            ):
                return_code = _run_cli(arguments)

            self.assertEqual(return_code, 2)
            self.assertIn("require exact candidate", stderr.getvalue())
            preflight.assert_not_called()


if __name__ == "__main__":
    unittest.main()
