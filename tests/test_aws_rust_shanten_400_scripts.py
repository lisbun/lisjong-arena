"""#400 AWS bootstrap contract tests (no AWS call, no game)."""

import re
import shutil
import subprocess
import unittest
from pathlib import Path

from lisjong_arena.policy_catalog import POLICY_CATALOG
from lisjong_arena.shanten_backend_verification import backend, plan

_ROOT = Path(__file__).resolve().parents[1]
_BOOTSTRAP = _ROOT / "scripts" / "aws" / "bootstrap-rust-shanten-400.sh"


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

    def test_frozen_identities_match_the_frozen_plan_not_the_current_pin(
        self,
    ) -> None:
        # #409 moved the project pin; the #400 bootstrap and plan keep the
        # lisjong#217 combination and only accept the Arena commits pinning it.
        revision = _shell_value(self.text, "FROZEN_LISJONG_REVISION")
        self.assertEqual(revision, plan.LISJONG_REVISION)
        self.assertNotEqual(revision, backend.EXPECTED_LISJONG_REVISION)
        project = (_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertNotIn(f"lisjong.git@{revision}", project)
        self.assertIn('grep -q "lisjong.git@$FROZEN_LISJONG_REVISION"', self.text)
        riichienv = _shell_value(self.text, "FROZEN_RIICHIENV_VERSION")
        self.assertIn(f'"riichienv=={riichienv}"', project)
        self.assertEqual(_shell_value(self.text, "WHEEL_FILE"), plan.WHEEL_FILENAME)
        self.assertEqual(_shell_value(self.text, "WHEEL_SHA256"), plan.WHEEL_SHA256)
        self.assertNotEqual(plan.WHEEL_SHA256, backend.EXPECTED_WHEEL_SHA256)

    def test_workload_constants_are_the_frozen_plan(self) -> None:
        for prefix, policy in (
            ("CHAMPION", plan.CHAMPION),
            ("TWO_STEP", plan.TWO_STEP),
        ):
            self.assertEqual(
                _shell_value(self.text, f"{prefix}_CATALOG"), policy.catalog
            )
            self.assertEqual(
                _shell_value(self.text, f"{prefix}_CLASS"), policy.replay_class
            )
            self.assertEqual(
                _shell_value(self.text, f"{prefix}_DECISIONS"), policy.decisions_file
            )
            self.assertEqual(
                _shell_value(self.text, f"{prefix}_DECISIONS_SHA256"),
                policy.decisions_sha256,
            )
            self.assertEqual(
                _shell_value(self.text, f"{prefix}_SEED0_SEMANTIC"),
                policy.seed0_semantic_sha256,
            )
        self.assertEqual(_shell_value(self.text, "GAME_MODE"), plan.GAME_MODE)
        self.assertEqual(
            int(_shell_value(self.text, "MULTI_WORKERS")), plan.MULTI_WORKERS
        )
        self.assertEqual(
            tuple(range(int(_shell_value(self.text, "MULTI_SEED_LAST")) + 1)),
            plan.MULTI_SEEDS,
        )
        self.assertEqual(
            int(_shell_value(self.text, "STARTUP_REPEAT")), plan.STARTUP_REPEAT
        )
        order = re.search(r"^REPLAY_ORDER=\(([^)]*)\)$", self.text, re.MULTILINE)
        self.assertEqual(tuple(order.group(1).split()), plan.REPLAY_ORDER)
        self.assertIn(f"--repeat {plan.REPLAY_REPEAT})", self.text)
        self.assertIn("--seeds 0 ", self.text)
        self.assertEqual(plan.SINGLE_SEEDS, (0,))

    def test_policies_are_the_213_champion_and_two_step(self) -> None:
        for name in ("CHAMPION_CATALOG", "TWO_STEP_CATALOG"):
            catalog = _shell_value(self.text, name)
            self.assertEqual(POLICY_CATALOG[catalog].identity, catalog)
        self.assertEqual(
            _shell_value(self.text, "CHAMPION_CATALOG"), "placement-aware-speed-call"
        )

    def test_wheel_is_installed_binary_only_after_identity_checks(self) -> None:
        install = self.text.index("--only-binary=:all: --no-index --no-deps")
        self.assertLess(
            self.text.index('verify-wheel "$INPUT_DIR/$WHEEL_FILE"'), install
        )
        self.assertLess(self.text.index("fail-closed/result.json"), install)
        self.assertNotIn("./native", self.text)
        self.assertNotIn("rustup", self.text)
        self.assertNotRegex(self.text, r"dnf[^\n]*\b(gcc|clang|rust|cargo)\b")

    def test_equivalence_gates_precede_measurements(self) -> None:
        order = [
            self.text.index("differential/result.json"),
            self.text.index("compare-champion.json"),
            self.text.index('startup --repeat "$STARTUP_REPEAT"'),
            self.text.index("tools/benchmark_tile_efficiency.py"),
            self.text.index('--workers "$MULTI_WORKERS"'),
            self.text.index('report "$OUT"'),
        ]
        self.assertEqual(order, sorted(order))
        self.assertEqual(_shell_value(self.text, "MULTI_WORKERS"), "16")


if __name__ == "__main__":
    unittest.main()
