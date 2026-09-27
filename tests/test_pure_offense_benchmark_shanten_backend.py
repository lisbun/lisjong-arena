"""Issue #406 — pure-offense benchmarkのopt-in shanten backend検査。

``require_shanten_backend``（#400でunit test済み）と native call counterを
差し替え、benchmark側の配線（processごとの検査、per-game native call検査、
fail closed、集約記録、CLIの事前拒否）だけを固定する。実RiichiEnv・wheelは使わない。
"""

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _pure_offense_benchmark_fixtures import OTHER_ARM, allocation_ledger, game_function
from _single_round_artifact_fixtures import provenance
from lisjong.policies import MinimalPolicy

from lisjong_arena import seed_registry
from lisjong_arena.model import PolicySpec
from lisjong_arena.pure_offense_benchmark import __main__ as cli
from lisjong_arena.pure_offense_benchmark import execution
from lisjong_arena.pure_offense_benchmark.artifact import load_benchmark_arm
from lisjong_arena.pure_offense_benchmark.protocol import MAX_STEPS
from lisjong_arena.shanten_backend_verification.backend import (
    NATIVE_MODULE,
    ShantenBackendVerificationError,
)
from lisjong_arena.single_round_evaluation import SingleRoundEvaluationError

_FOCAL = PolicySpec(identity="minimal", factory=MinimalPolicy)
_REVISION = "2553c1b9f22545bb2fcb914adce1879d15cdc58d"
_GAME = game_function(lambda seed, focal: OTHER_ARM[(seed, focal)])


def _process_record(backend: str) -> dict:
    native = (
        None
        if backend == "python"
        else {
            "module_file": "/x/_lisjong_native.so",
            "source_revision": _REVISION,
            "probe_native_calls": 1,
        }
    )
    return {
        "backend": backend,
        "lisjong_revision": _REVISION,
        "pid": 4321,
        "native": native,
    }


class _Counter:
    """ゲームごとに``step``だけ進むnative call counter。"""

    def __init__(self, step: int) -> None:
        self.value = 0
        self.step = step
        self.reads = 0

    def __call__(self) -> int:
        self.reads += 1
        if self.reads % 2 == 0:
            self.value += self.step
        return self.value


class _BackendPatch(unittest.TestCase):
    def setUp(self) -> None:
        execution._PROCESS_BACKEND.clear()
        self.addCleanup(execution._PROCESS_BACKEND.clear)

    def _patches(self, backend: str, *, step: int = 3, require=None):
        return (
            mock.patch.object(
                execution,
                "require_shanten_backend",
                require or mock.Mock(return_value=_process_record(backend)),
            ),
            mock.patch.object(execution, "native_call_count", _Counter(step)),
            mock.patch.object(execution, "_run_benchmark_game", _GAME),
        )


class ShantenBackendExecutionTest(_BackendPatch):
    def test_default_path_records_no_observation_and_never_checks(self) -> None:
        require = mock.Mock(side_effect=AssertionError("must not be called"))
        with (
            mock.patch.object(execution, "require_shanten_backend", require),
            mock.patch.object(execution, "_run_benchmark_game", _GAME),
        ):
            arm = execution.run_benchmark_arm(
                execution.benchmark_plan(_FOCAL, (1, 2)), max_workers=1
            )
        self.assertEqual(arm.shanten_backend_observations, ())

    def test_rust_checks_the_process_once_and_every_game(self) -> None:
        require = mock.Mock(return_value=_process_record("rust"))
        patches = self._patches("rust", require=require)
        with patches[0], patches[1], patches[2]:
            arm = execution.run_benchmark_arm(
                execution.benchmark_plan(_FOCAL, (1, 2)),
                max_workers=1,
                shanten_backend="rust",
            )
        require.assert_called_once_with("rust")
        self.assertEqual(len(arm.shanten_backend_observations), 8)
        self.assertTrue(
            all(item["native_calls"] == 3 for item in arm.shanten_backend_observations)
        )
        record = execution.shanten_backend_record(
            arm, backend="rust", parent=_process_record("rust")
        )
        self.assertEqual(record["games"], 8)
        self.assertEqual(record["workers_observed"], 1)
        self.assertEqual(record["min_native_calls_per_game"], 3)
        self.assertEqual(record["native_source_revision"], _REVISION)

    def test_rust_game_without_native_calls_fails_the_arm(self) -> None:
        patches = self._patches("rust", step=0)
        with patches[0], patches[1], patches[2]:
            with self.assertRaises(SingleRoundEvaluationError) as raised:
                execution.run_benchmark_arm(
                    execution.benchmark_plan(_FOCAL, (1,)),
                    max_workers=1,
                    shanten_backend="rust",
                )
        self.assertIsInstance(
            raised.exception.__cause__, ShantenBackendVerificationError
        )

    def test_rust_without_loaded_extension_fails_the_arm(self) -> None:
        patches = self._patches("rust")
        with (
            patches[0],
            patches[2],
            mock.patch.object(execution, "native_call_count", return_value=None),
        ):
            with self.assertRaises(SingleRoundEvaluationError):
                execution.run_benchmark_arm(
                    execution.benchmark_plan(_FOCAL, (1,)),
                    max_workers=1,
                    shanten_backend="rust",
                )

    def test_python_game_importing_the_extension_fails_the_arm(self) -> None:
        patches = self._patches("python")
        with (
            patches[0],
            patches[1],
            patches[2],
            mock.patch.dict(sys.modules, {NATIVE_MODULE: object()}),
        ):
            with self.assertRaises(SingleRoundEvaluationError):
                execution.run_benchmark_arm(
                    execution.benchmark_plan(_FOCAL, (1,)),
                    max_workers=1,
                    shanten_backend="python",
                )

    def test_failed_process_verification_fails_before_any_game(self) -> None:
        game = mock.Mock(side_effect=AssertionError("no game must run"))
        require = mock.Mock(side_effect=ShantenBackendVerificationError("no wheel"))
        with (
            mock.patch.object(execution, "require_shanten_backend", require),
            mock.patch.object(execution, "_run_benchmark_game", game),
        ):
            with self.assertRaises(ShantenBackendVerificationError):
                execution.run_benchmark_arm(
                    execution.benchmark_plan(_FOCAL, (1,)),
                    max_workers=1,
                    shanten_backend="rust",
                )
        game.assert_not_called()

    def test_unknown_backend_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            execution.run_benchmark_arm(
                execution.benchmark_plan(_FOCAL, (1,)),
                max_workers=1,
                shanten_backend="cython",
            )

    def test_worker_job_reports_verification_failure_as_outcome_text(self) -> None:
        plan = execution.benchmark_plan(_FOCAL, (1,))
        job = execution.GameJob(
            seed=1,
            rotation=0,
            assignment=execution._seat_assignment(plan, 0),
            game_mode="4p-red-single",
            max_steps=MAX_STEPS,
        )
        require = mock.Mock(side_effect=ShantenBackendVerificationError("no wheel"))
        with mock.patch.object(execution, "require_shanten_backend", require):
            outcome = execution._run_benchmark_game_job(job, shanten_backend="rust")
        self.assertIsNone(outcome.result)
        self.assertIn("no wheel", outcome.error_text)

        patches = self._patches("rust")
        with patches[0], patches[1], patches[2]:
            outcome = execution._run_benchmark_game_job(job, shanten_backend="rust")
        self.assertIsNone(outcome.error_text)
        self.assertEqual(outcome.shanten_backend["native_calls"], 3)
        self.assertEqual(outcome.shanten_backend["pid"], 4321)


class ShantenBackendRecordTest(_BackendPatch):
    def _arm(self, backend: str):
        patches = self._patches(backend)
        with patches[0], patches[1], patches[2]:
            return execution.run_benchmark_arm(
                execution.benchmark_plan(_FOCAL, (1,)),
                max_workers=1,
                shanten_backend=backend,
            )

    def test_mismatched_observations_fail_closed(self) -> None:
        arm = self._arm("rust")
        other = dict(_process_record("rust"))
        other["native"] = {**other["native"], "source_revision": "0" * 40}
        with self.assertRaises(ShantenBackendVerificationError):
            execution.shanten_backend_record(arm, backend="rust", parent=other)
        with self.assertRaises(ShantenBackendVerificationError):
            execution.shanten_backend_record(
                arm, backend="python", parent=_process_record("python")
            )

    def test_python_record_has_no_native_calls(self) -> None:
        arm = self._arm("python")
        record = execution.shanten_backend_record(
            arm, backend="python", parent=_process_record("python")
        )
        self.assertIsNone(record["native_source_revision"])
        self.assertIsNone(record["min_native_calls_per_game"])
        self.assertEqual(record["workers"]["4321"], {"games": 4, "native_calls": None})

    def test_arm_without_observations_has_no_record(self) -> None:
        with mock.patch.object(execution, "_run_benchmark_game", _GAME):
            arm = execution.run_benchmark_arm(
                execution.benchmark_plan(_FOCAL, (1,)), max_workers=1
            )
        with self.assertRaises(execution.PureOffenseExecutionError):
            execution.shanten_backend_record(
                arm, backend="rust", parent=_process_record("rust")
            )


class ShantenBackendCliTest(_BackendPatch):
    def setUp(self) -> None:
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        ledger, record = allocation_ledger()
        self.ledger_path = self.root / "ledger.json"
        seed_registry.write_ledger(self.ledger_path, ledger)
        self.allocation = record["allocation_identity"]

    def _arguments(self, *extra: str) -> list[str]:
        return [
            "run",
            "--focal",
            "lisjong.policies.shanten:ShantenPolicy",
            "--focal-identity",
            "shanten",
            "--ledger",
            str(self.ledger_path),
            "--allocation-identity",
            self.allocation,
            "--workers",
            "1",
            "--out",
            str(self.root / "arm"),
            *extra,
        ]

    def test_rust_run_writes_the_arm_and_a_separate_backend_record(self) -> None:
        record_path = self.root / "arm.shanten-backend.json"
        require = mock.Mock(return_value=_process_record("rust"))
        patches = self._patches("rust", require=require)
        with (
            patches[0],
            patches[1],
            patches[2],
            mock.patch.object(
                cli, "require_shanten_backend", return_value=_process_record("rust")
            ),
            mock.patch.object(cli, "collect_execution_provenance"),
            mock.patch(
                "lisjong_arena.single_round_artifact.collect_execution_provenance",
                return_value=provenance(),
            ),
            contextlib.redirect_stdout(io.StringIO()) as stdout,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            code = cli.main(
                self._arguments(
                    "--shanten-backend",
                    "rust",
                    "--shanten-backend-record",
                    str(record_path),
                )
            )
        self.assertEqual(code, 0)
        self.assertIn("shanten_backend=rust", stdout.getvalue())
        self.assertIn("shanten_backend_workers=1", stdout.getvalue())
        record = json.loads(record_path.read_text(encoding="utf-8"))
        self.assertEqual(record["backend"], "rust")
        self.assertEqual(record["focal_identity"], "shanten")
        self.assertEqual(record["workers_requested"], 1)
        self.assertEqual(record["games"], 8)
        # arm artifactは既存schemaのままで、backend記録を含まない。
        arm_dir = self.root / "arm"
        self.assertEqual(
            sorted(path.name for path in arm_dir.iterdir()),
            ["offense.json", "strength.json"],
        )
        self.assertEqual(load_benchmark_arm(arm_dir).seeds, (1, 2))

    def test_invalid_backend_options_fail_before_any_game(self) -> None:
        existing = self.root / "existing.json"
        existing.write_text("{}", encoding="utf-8")
        game = mock.Mock(side_effect=AssertionError("no game must run"))
        failing = mock.Mock(side_effect=ShantenBackendVerificationError("no wheel"))
        with (
            mock.patch.object(execution, "_run_benchmark_game", game),
            mock.patch.object(cli, "require_shanten_backend", failing),
        ):
            with self.assertRaises(ValueError):
                cli.main(self._arguments("--shanten-backend", "rust"))
            with self.assertRaises(ValueError):
                cli.main(self._arguments("--shanten-backend-record", str(existing)))
            with self.assertRaises(FileExistsError):
                cli.main(
                    self._arguments(
                        "--shanten-backend",
                        "rust",
                        "--shanten-backend-record",
                        str(existing),
                    )
                )
            with self.assertRaises(ShantenBackendVerificationError):
                cli.main(
                    self._arguments(
                        "--shanten-backend",
                        "rust",
                        "--shanten-backend-record",
                        str(self.root / "new.json"),
                    )
                )
        game.assert_not_called()
        self.assertFalse((self.root / "arm").exists())


if __name__ == "__main__":
    unittest.main()
