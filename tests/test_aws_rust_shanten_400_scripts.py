"""#400 AWS bootstrap contract tests (no AWS call, no game)."""

import re
import shutil
import subprocess
import unittest
from pathlib import Path

from lisjong_arena.policy_catalog import POLICY_CATALOG
from lisjong_arena.shanten_backend_verification import backend

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

    def test_frozen_identities_match_the_python_constants_and_the_pin(self) -> None:
        revision = _shell_value(self.text, "FROZEN_LISJONG_REVISION")
        self.assertEqual(revision, backend.EXPECTED_LISJONG_REVISION)
        project = (_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn(f"lisjong.git@{revision}", project)
        riichienv = _shell_value(self.text, "FROZEN_RIICHIENV_VERSION")
        self.assertIn(f'"riichienv=={riichienv}"', project)
        self.assertEqual(
            _shell_value(self.text, "WHEEL_FILE"), backend.EXPECTED_WHEEL_FILENAME
        )
        self.assertEqual(
            _shell_value(self.text, "WHEEL_SHA256"), backend.EXPECTED_WHEEL_SHA256
        )

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
