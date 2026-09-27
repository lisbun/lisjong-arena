"""#406 AWS bootstrap contract tests (no AWS call, no game)."""

import re
import shutil
import subprocess
import unittest
from pathlib import Path

from lisjong.policies.kobalab_0004_reference import KOBALAB_0004_REFERENCE_IDENTITY

from lisjong_arena.policy_catalog import POLICY_CATALOG
from lisjong_arena.pure_offense_benchmark import protocol
from lisjong_arena.shanten_backend_verification import backend
from lisjong_arena.single_round_evaluation import ROTATION_COUNT

_ROOT = Path(__file__).resolve().parents[1]
_BOOTSTRAP = _ROOT / "scripts" / "aws" / "bootstrap-pure-offense-kobalab-406.sh"


def _shell_value(text: str, name: str) -> str:
    match = re.search(rf'^{name}="?([^"\n]+)"?$', text, re.MULTILINE)
    assert match is not None, name
    return match.group(1)


class BootstrapTest(unittest.TestCase):
    def setUp(self) -> None:
        self.text = _BOOTSTRAP.read_text(encoding="utf-8")

    def test_bash_syntax(self) -> None:
        bash = shutil.which("bash")
        if bash is None:
            self.skipTest("bash is unavailable")
        subprocess.run([bash, "-n", str(_BOOTSTRAP)], check=True)

    def test_frozen_dependencies_match_the_current_pin_and_the_400_wheel(self) -> None:
        project = (_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        revision = _shell_value(self.text, "FROZEN_LISJONG_REVISION")
        self.assertEqual(revision, backend.EXPECTED_LISJONG_REVISION)
        self.assertIn(f"lisjong.git@{revision}", project)
        riichienv = _shell_value(self.text, "FROZEN_RIICHIENV_VERSION")
        self.assertIn(f'"riichienv=={riichienv}"', project)
        self.assertEqual(
            _shell_value(self.text, "WHEEL_FILE"), backend.EXPECTED_WHEEL_FILENAME
        )
        self.assertEqual(
            _shell_value(self.text, "WHEEL_SHA256"), backend.EXPECTED_WHEEL_SHA256
        )
        self.assertIn("--only-binary=:all: --no-index --no-deps", self.text)

    def test_every_arm_runs_on_the_verified_rust_backend(self) -> None:
        self.assertEqual(_shell_value(self.text, "BACKEND"), "rust")
        self.assertIn('LISJONG_SHANTEN_BACKEND="$BACKEND" "$PYTHON"', self.text)
        self.assertIn('--shanten-backend "$BACKEND"', self.text)
        self.assertIn("shanten_backend_workers=$LISJONG_WORKERS", self.text)
        # Without the wheel the rust probe must fail before the wheel is installed.
        self.assertLess(
            self.text.index("rust_without_wheel=$?"),
            self.text.index("--only-binary=:all:"),
        )
        self.assertLess(
            self.text.index("verify-wheel"), self.text.index("--only-binary=:all:")
        )

    def test_arms_and_focal_references(self) -> None:
        self.assertIn("ARMS=(ukeire two-step kobalab-0004)", self.text)
        self.assertEqual(
            _shell_value(self.text, "KOBALAB_IDENTITY"),
            KOBALAB_0004_REFERENCE_IDENTITY,
        )
        self.assertIn(
            "--focal lisjong.policies.kobalab_0004_reference:"
            "Kobalab0004ReferencePolicy --focal-identity $KOBALAB_IDENTITY",
            self.text,
        )
        # Same references as the #389 control arms.
        self.assertIn(
            "--focal lisjong.policies.ukeire:UkeirePolicy --focal-identity ukeire",
            self.text,
        )
        self.assertIn('two-step) echo "--focal two-step"', self.text)
        self.assertIn("two-step", POLICY_CATALOG)

    def test_reused_allocation_is_the_committed_389_calibration(self) -> None:
        self.assertEqual(
            _shell_value(self.text, "ALLOCATION_IDENTITY"),
            "df8460868ac26cc3505f04b5f4f8f524ccc14dc2423a114487775a1cb69ccb0f",
        )
        self.assertEqual(_shell_value(self.text, "SEED_DOMAIN"), protocol.SEED_DOMAIN)
        self.assertEqual(_shell_value(self.text, "OWNER_ISSUE"), protocol.OWNER_ISSUE)
        self.assertEqual(int(_shell_value(self.text, "ROTATIONS")), ROTATION_COUNT)
        first = int(_shell_value(self.text, "ALLOCATION_FIRST_SEED"))
        last = int(_shell_value(self.text, "ALLOCATION_LAST_SEED"))
        self.assertEqual((first, last), (389000, 389999))
        self.assertEqual(_shell_value(self.text, "SPLIT"), "DEVELOPMENT")
        self.assertEqual(_shell_value(self.text, "ALLOCATION_STATE"), "COMMITTED")
        self.assertEqual(
            _shell_value(self.text, "POPULATION"), "pure-offense-calibration"
        )

    def test_protocol_is_pinned_to_the_allocation_revision(self) -> None:
        protocol_file = _shell_value(self.text, "PROTOCOL_FILE")
        self.assertTrue((_ROOT / protocol_file).is_file())
        self.assertIn(
            'diff --quiet "$ALLOCATION_ARENA_REVISION" "$ARENA_REVISION" -- '
            '"$PROTOCOL_FILE"',
            self.text,
        )
        self.assertIn("merge-base --is-ancestor", self.text)

    def test_event_order(self) -> None:
        verify = self.text.index("environment_verify")
        allocation = self.text.index('show "$ALLOCATION_IDENTITY"')
        wheel = self.text.index("--only-binary=:all:")
        run = self.text.index("pure_offense_benchmark run")
        readback = self.text.index("load_benchmark_arm(")
        summarize = self.text.index("pure_offense_benchmark summarize")
        supplementary = self.text.index("pure_offense_benchmark.supplementary")
        self.assertLess(self.text.index("seed-registry:refs"), allocation)
        self.assertLess(verify, allocation)
        self.assertLess(allocation, wheel)
        self.assertLess(wheel, run)
        self.assertLess(run, readback)
        self.assertLess(readback, summarize)
        self.assertLess(summarize, supplementary)

    def test_no_seed_mutation_or_learning_inputs(self) -> None:
        registry_calls = re.findall(r"-m lisjong_arena\.seed_registry (.*)", self.text)
        self.assertEqual(
            registry_calls, ['--ledger "$LEDGER" show "$ALLOCATION_IDENTITY" \\']
        )
        for forbidden in ("torch", "outcome-q", "canonical-first"):
            self.assertNotIn(forbidden, self.text)


if __name__ == "__main__":
    unittest.main()
