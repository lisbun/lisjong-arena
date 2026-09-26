"""Issue #389 operator CLIのunit test（実RiichiEnvは起動しない）。"""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _pure_offense_benchmark_fixtures import (
    OTHER,
    OTHER_ARM,
    REFERENCE,
    allocation_ledger,
    game_function,
    save_arm,
)
from _single_round_artifact_fixtures import provenance

from lisjong_arena import seed_registry
from lisjong_arena.pure_offense_benchmark import __main__ as cli
from lisjong_arena.pure_offense_benchmark import execution
from lisjong_arena.pure_offense_benchmark.artifact import load_benchmark_arm


class PureOffenseCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        ledger, record = allocation_ledger()
        self.ledger_path = self.root / "ledger.json"
        seed_registry.write_ledger(self.ledger_path, ledger)
        self.allocation = record["allocation_identity"]

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run_arguments(self, out: Path) -> list[str]:
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
            str(out),
        ]

    def test_run_writes_an_arm_from_the_registry_allocation(self) -> None:
        out = self.root / "arm"
        with (
            mock.patch.object(
                execution,
                "_run_benchmark_game",
                game_function(lambda seed, focal: OTHER_ARM[(seed, focal)]),
            ),
            mock.patch.object(cli, "collect_execution_provenance"),
            mock.patch(
                "lisjong_arena.single_round_artifact.collect_execution_provenance",
                return_value=provenance(),
            ),
            contextlib.redirect_stdout(io.StringIO()) as stdout,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(cli.main(self._run_arguments(out)), 0)
        self.assertIn("focal_identity=shanten", stdout.getvalue())
        arm = load_benchmark_arm(out)
        self.assertEqual(arm.focal_reference, "lisjong.policies.shanten:ShantenPolicy")
        self.assertEqual(arm.seeds, (1, 2))

    def test_run_fails_closed_before_any_game(self) -> None:
        existing = self.root / "existing"
        existing.mkdir()
        game = mock.Mock(side_effect=AssertionError("no game must run"))
        with mock.patch.object(execution, "_run_benchmark_game", game):
            with self.assertRaises(FileExistsError):
                cli.main(self._run_arguments(existing))
            arguments = self._run_arguments(self.root / "new")
            arguments[arguments.index("--allocation-identity") + 1] = "0" * 64
            with self.assertRaises(ValueError):
                cli.main(arguments)
        game.assert_not_called()

    def test_summarize_prints_and_writes(self) -> None:
        reference = save_arm(
            self.root, "reference", REFERENCE, lambda seed, focal: ("draw", None)
        )
        other = save_arm(
            self.root, "other", OTHER, lambda seed, focal: OTHER_ARM[(seed, focal)]
        )
        out = self.root / "summary.json"
        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            code = cli.main(
                ["summarize", str(reference), str(other), "--out", str(out)]
            )
        self.assertEqual(code, 0)
        self.assertTrue(out.is_file())
        self.assertIn("paired: other - reference", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
