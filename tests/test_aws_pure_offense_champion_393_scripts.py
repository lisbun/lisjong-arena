"""#393 AWS bootstrap contract tests (no AWS call, no game)."""

import re
import shutil
import subprocess
import unittest
from pathlib import Path

from lisjong_arena.policy_catalog import POLICY_CATALOG
from lisjong_arena.pure_offense_benchmark import protocol
from lisjong_arena.single_round_evaluation import ROTATION_COUNT

_ROOT = Path(__file__).resolve().parents[1]
_BOOTSTRAP = _ROOT / "scripts" / "aws" / "bootstrap-pure-offense-champion-393.sh"


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

    def test_frozen_dependencies_are_the_current_arena_pins(self) -> None:
        project = (_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        revision = _shell_value(self.text, "FROZEN_LISJONG_REVISION")
        self.assertEqual(revision, "2a9debebdbbe4d10841fa4371a6cf6bf19ce9de1")
        self.assertIn(f"lisjong.git@{revision}", project)
        riichienv = _shell_value(self.text, "FROZEN_RIICHIENV_VERSION")
        self.assertIn(f'"riichienv=={riichienv}"', project)

    def test_focal_is_the_curated_champion_binding(self) -> None:
        focal = _shell_value(self.text, "FOCAL")
        self.assertEqual(focal, "placement-aware-speed-call")
        self.assertEqual(POLICY_CATALOG[focal].identity, focal)
        self.assertIn('run --focal "$FOCAL"', self.text)
        self.assertNotIn("--focal-identity", self.text)
        self.assertNotIn("--focal-artifact", self.text)

    def test_allocation_matches_the_v1_benchmark_contract(self) -> None:
        self.assertRegex(
            _shell_value(self.text, "ALLOCATION_IDENTITY"), r"^[0-9a-f]{64}$"
        )
        self.assertEqual(_shell_value(self.text, "SEED_DOMAIN"), protocol.SEED_DOMAIN)
        self.assertEqual(_shell_value(self.text, "OWNER_ISSUE"), protocol.OWNER_ISSUE)
        self.assertEqual(int(_shell_value(self.text, "ROTATIONS")), ROTATION_COUNT)
        first = int(_shell_value(self.text, "ALLOCATION_FIRST_SEED"))
        last = int(_shell_value(self.text, "ALLOCATION_LAST_SEED"))
        self.assertEqual((first, last), (390000, 390499))
        self.assertEqual(_shell_value(self.text, "SPLIT"), "DEVELOPMENT")
        self.assertEqual(
            _shell_value(self.text, "PROVENANCE_REFERENCE"),
            "https://github.com/lisbun/lisjong-arena/issues/393",
        )

    def test_benchmark_package_is_pinned_to_the_allocation_revision(self) -> None:
        package = _shell_value(self.text, "BENCHMARK_PACKAGE")
        self.assertTrue((_ROOT / package / "protocol.py").is_file())
        self.assertIn('"$ALLOCATION_ARENA_REVISION" "$ARENA_REVISION" -- \\', self.text)
        self.assertIn("merge-base --is-ancestor", self.text)

    def test_event_order_is_verify_then_run_then_readback(self) -> None:
        verify = self.text.index("environment_verify")
        allocation = self.text.index('show "$ALLOCATION_IDENTITY"')
        run = self.text.index("pure_offense_benchmark run")
        readback = self.text.index("load_benchmark_arm(")
        summarize = self.text.index("pure_offense_benchmark summarize")
        self.assertLess(self.text.index("seed-registry:refs"), allocation)
        self.assertLess(verify, allocation)
        self.assertLess(allocation, run)
        self.assertLess(run, readback)
        self.assertLess(readback, summarize)

    def test_no_seed_mutation_or_outcome_q_inputs(self) -> None:
        registry_calls = re.findall(r"-m lisjong_arena\.seed_registry (.*)", self.text)
        self.assertEqual(
            registry_calls, ['--ledger "$LEDGER" show "$ALLOCATION_IDENTITY" \\']
        )
        for forbidden in ("torch", "outcome-q", "canonical-first"):
            self.assertNotIn(forbidden, self.text)
        self.assertNotIn("LISJONG_INPUT_DIR", self.text)


if __name__ == "__main__":
    unittest.main()
