"""Default local output root of the AWS operator scripts (lisjong-arena#469).

Pins the order explicit ``-OutputRoot`` > ``LISJONG_ARTIFACTS_ROOT`` > an
existing ``lisjong-artifacts`` directory beside the checkout > the script's
previous default, and that every script with a default uses the shared rule
with its unchanged directory name.  No AWS call and no real artifacts directory
is involved: the helper runs from a copy inside a temporary tree.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

_AWS = Path(__file__).resolve().parents[1] / "scripts" / "aws"
HELPER = "artifacts-root.ps1"
NAMES = {
    "lisjong-ec2.ps1": "aws-ec2",
    "run-heuristic-calibration-423.ps1": "aws-heuristic-candidate-423",
    "run-heuristic-candidate-aabb-375.ps1": "aws-heuristic-candidate-375",
    "run-heuristic-formal-423.ps1": "aws-heuristic-formal-$EvaluationEvent",
    "run-l03-c2-362.ps1": "aws-l03-c2-362",
    "run-l03-engine-367.ps1": "aws-l03-engine-367",
    "start-offense-foundation-332.ps1": "aws-offense-foundation-332",
    "collect-offense-foundation-332.ps1": "aws-offense-foundation-332",
    "start-operational-calibration-340.ps1": "aws-operational-calibration-340",
    "start-riichilab-12h.ps1": "aws-riichilab-313",
    "start-wait-shape-326.ps1": "aws-wait-shape-326",
}
_DEFAULT_BLOCK = re.compile(
    r"(?m)^if \(\[string\]::IsNullOrWhiteSpace\(\$OutputRoot\)\) \{\r?\n"
    r"((?:    .*\r?\n)+?)\}\r?\n"
)


class ScriptWiringTest(unittest.TestCase):
    def test_every_script_with_a_default_uses_the_shared_rule(self) -> None:
        with_default = {
            path.name
            for path in _AWS.glob("*.ps1")
            if _DEFAULT_BLOCK.search(path.read_text(encoding="utf-8"))
        }
        self.assertEqual(with_default, set(NAMES))

    def test_the_rule_is_applied_last_with_the_unchanged_name(self) -> None:
        for script, name in NAMES.items():
            with self.subTest(script=script):
                text = (_AWS / script).read_text(encoding="utf-8")
                blocks = _DEFAULT_BLOCK.findall(text)
                self.assertEqual(len(blocks), 1)
                lines = [line.strip() for line in blocks[0].splitlines()]
                self.assertEqual(
                    lines[-2:],
                    [
                        f'. (Join-Path $PSScriptRoot "{HELPER}")',
                        "$OutputRoot = Resolve-LisjongOutputRoot "
                        f'-Name "{name}" -Legacy $OutputRoot',
                    ],
                )
                # The previous default is still computed, with the same name.
                legacy = "\n".join(lines[:-2])
                self.assertIn(name.replace("$EvaluationEvent", ""), legacy)
                self.assertEqual(text.count("Resolve-LisjongOutputRoot"), 1)

    def test_no_user_specific_path_is_written_into_the_scripts(self) -> None:
        for path in [_AWS / HELPER, *(_AWS / script for script in NAMES)]:
            with self.subTest(script=path.name):
                self.assertNotRegex(path.read_text(encoding="utf-8"), r"(?i)C:\\Dev")


class ResolveTest(unittest.TestCase):
    def setUp(self) -> None:
        self.pwsh = shutil.which("pwsh")
        if self.pwsh is None:
            self.skipTest("pwsh is unavailable")
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        # <root>/work/checkout/scripts/aws/artifacts-root.ps1
        self.aws = self.root / "work" / "checkout" / "scripts" / "aws"
        self.aws.mkdir(parents=True)
        shutil.copyfile(_AWS / HELPER, self.aws / HELPER)
        self.sibling = self.root / "work" / "lisjong-artifacts"
        self.legacy = self.root / "legacy" / "lisjong" / "aws-ec2"

    def resolve(self, name="aws-ec2", *, artifacts_root=None):
        environment = {
            key: value
            for key, value in os.environ.items()
            if key.upper() != "LISJONG_ARTIFACTS_ROOT"
        }
        if artifacts_root is not None:
            environment["LISJONG_ARTIFACTS_ROOT"] = str(artifacts_root)
        environment["RESOLVE_NAME"] = name
        environment["RESOLVE_LEGACY"] = str(self.legacy)
        command = (
            "Set-StrictMode -Version Latest; $ErrorActionPreference = 'Stop'; "
            f". '{self.aws / HELPER}'; "
            "Resolve-LisjongOutputRoot -Name $env:RESOLVE_NAME "
            "-Legacy $env:RESOLVE_LEGACY"
        )
        return subprocess.run(
            [self.pwsh, "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True,
            text=True,
            env=environment,
        )

    def resolved(self, **options) -> Path:
        done = self.resolve(**options)
        self.assertEqual(done.returncode, 0, done.stderr)
        return Path(done.stdout.strip())

    def test_without_an_artifacts_root_the_previous_default_is_kept(self) -> None:
        self.assertEqual(self.resolved(), self.legacy)
        self.assertFalse(self.sibling.exists())

    def test_an_existing_sibling_directory_is_used(self) -> None:
        self.sibling.mkdir()
        self.assertEqual(self.resolved(), self.sibling / "aws-ec2")
        self.assertEqual(
            self.resolved(name="aws-riichilab-313"), self.sibling / "aws-riichilab-313"
        )
        # Resolving creates nothing.
        self.assertEqual(list(self.sibling.iterdir()), [])

    def test_a_sibling_file_is_not_an_artifacts_root(self) -> None:
        self.sibling.write_text("", encoding="utf-8")
        self.assertEqual(self.resolved(), self.legacy)

    def test_the_environment_variable_wins_over_the_sibling(self) -> None:
        self.sibling.mkdir()
        chosen = self.root / "chosen"
        self.assertEqual(self.resolved(artifacts_root=chosen), chosen / "aws-ec2")
        self.assertFalse(chosen.exists())
        # A blank value is not a root.
        self.assertEqual(self.resolved(artifacts_root="  "), self.sibling / "aws-ec2")

    def test_a_name_that_is_not_one_directory_is_rejected(self) -> None:
        self.sibling.mkdir()
        for name in ("..", ".", "a/b", "a\\b", "C:", " "):
            with self.subTest(name=name):
                done = self.resolve(name=name)
                self.assertNotEqual(done.returncode, 0)
                self.assertEqual(done.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
