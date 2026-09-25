"""#375 AWS wrapper / bootstrap contract tests (no AWS call, no hanchan)."""

import re
import shutil
import subprocess
import unittest
from pathlib import Path

from lisjong_arena.heuristic_candidate_aabb.protocol import SEED_BLOCK_COUNT
from lisjong_arena.overall_champion_aabb.protocol import resolve_binding_callable

_ROOT = Path(__file__).resolve().parents[1]
_AWS = _ROOT / "scripts" / "aws"
_LAUNCHER = _AWS / "run-heuristic-candidate-aabb-375.ps1"
_BOOTSTRAP = _AWS / "bootstrap-heuristic-candidate-aabb-375.sh"
_STATUS = _AWS / "status-run.ps1"


def _shell_value(text: str, name: str) -> str:
    match = re.search(rf'^{name}="([^"]+)"$', text, re.MULTILINE)
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

    def test_frozen_lisjong_revision_is_the_current_arena_pin(self) -> None:
        revision = _shell_value(self.text, "FROZEN_LISJONG_REVISION")
        project = (_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn(f"lisjong.git@{revision}", project)
        self.assertIn(revision, _LAUNCHER.read_text(encoding="utf-8"))

    def test_participant_factories_are_exact_bindings(self) -> None:
        for name in ("CANDIDATE_FACTORY", "INCUMBENT_FACTORY"):
            resolve_binding_callable(_shell_value(self.text, name))
        self.assertEqual(
            _shell_value(self.text, "INCUMBENT_IDENTITY"),
            "targeted-honor-release-terminal-progression",
        )

    def test_event_order_is_lock_then_run_then_verify(self) -> None:
        lock = self.text.index("heuristic_candidate_aabb lock")
        run = self.text.index("heuristic_candidate_aabb run")
        verify = self.text.index("heuristic_candidate_aabb verify")
        self.assertLess(self.text.index("environment_verify"), lock)
        self.assertLess(self.text.index("seed-registry"), lock)
        self.assertLess(lock, run)
        self.assertLess(run, verify)
        self.assertIn("merge-base --is-ancestor", self.text)

    def test_progress_path_is_readable_by_status_run(self) -> None:
        work_root = _shell_value(self.text, "WORK_ROOT")
        progress = f"{work_root}/output/progress.json"
        self.assertRegex(progress, r"^/mnt/[A-Za-z0-9._/-]+/progress[.]json$")
        self.assertIn(
            f'$RemoteOutput = "{work_root}/output"',
            _LAUNCHER.read_text(encoding="utf-8"),
        )
        self.assertIn("lisjong-progress-path", _STATUS.read_text(encoding="utf-8"))


class LauncherTest(unittest.TestCase):
    def setUp(self) -> None:
        self.text = _LAUNCHER.read_text(encoding="utf-8")

    def test_defaults_and_bounds(self) -> None:
        self.assertIn('[string]$InstanceType = "c7i.8xlarge"', self.text)
        self.assertIn("[ValidateRange(1, 64)][int]$MaxWorkers = 32", self.text)
        self.assertIn(
            "[ValidateRange(3600, 28800)][int]$FailSafeSeconds = 23400", self.text
        )
        self.assertIn("[double]$CostBudgetUsd = 15.0", self.text)
        self.assertIn(f"$SeedBlockCount = {SEED_BLOCK_COUNT}", self.text)
        self.assertIsNone(re.search(r"(?i)spot", self.text))

    def test_no_billable_call_precedes_pricing_dry_run_and_allocation_check(
        self,
    ) -> None:
        create = self.text.index('"create-bucket"')
        self.assertLess(self.text.index("Get-PricingDimension -Filters"), create)
        self.assertLess(self.text.index('"--dry-run"'), create)
        self.assertLess(self.text.index("require_seed_allocation"), create)
        self.assertLess(self.text.index("merge-base --is-ancestor"), create)
        self.assertLess(
            self.text.index("LISJONG_375_FAILSAFE_ARMED"),
            self.text.index("$eventSubmitted = $true"),
        )

    def test_collect_verifies_locally_before_deleting_the_bucket(self) -> None:
        collect = self.text[self.text.index("function Invoke-Collect") :]
        verify = collect.index("heuristic_candidate_aabb")
        self.assertLess(verify, collect.index("Remove-TransferBucket -Bucket $bucket"))
        self.assertIn("LISJONG_375_COMPLETION_JSON_B64", collect)
        self.assertIn("lisjong-scientific-command-id", self.text)

    def test_powershell_syntax_when_pwsh_is_available(self) -> None:
        pwsh = shutil.which("pwsh")
        if pwsh is None:
            self.skipTest("pwsh is unavailable")
        script = (
            "$e=$null; [void][System.Management.Automation.Language.Parser]::ParseFile("
            f"'{_LAUNCHER}',[ref]$null,[ref]$e); if (@($e).Count) {{ $e; exit 1 }}"
        )
        subprocess.run([pwsh, "-NoProfile", "-Command", script], check=True)


if __name__ == "__main__":
    unittest.main()
