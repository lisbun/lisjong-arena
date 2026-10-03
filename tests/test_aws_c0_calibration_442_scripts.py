"""#442 AWS calibration contract tests (no AWS call, no game)."""

import importlib.util
import re
import shutil
import subprocess
import unittest
from pathlib import Path

from lisjong_arena.policy_source_record import binding, record
from lisjong_arena.shanten_backend_verification import backend

_ROOT = Path(__file__).resolve().parents[1]
_AWS = _ROOT / "scripts" / "aws"
_BOOTSTRAP = _AWS / "bootstrap-c0-calibration-442.sh"
_DOC = _ROOT / "docs" / "aws-c0-calibration-442.md"


def _driver():
    spec = importlib.util.spec_from_file_location(
        "calibrate_c0_442", _AWS / "calibrate_c0_442.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _shell_value(text: str, name: str) -> str:
    match = re.search(rf'^{name}="?([^"\n]+)"?$', text, re.MULTILINE)
    assert match is not None, name
    return match.group(1)


class DriverPlanTest(unittest.TestCase):
    def setUp(self) -> None:
        self.driver = _driver()

    def test_teacher_is_a_catalog_teacher(self) -> None:
        binding.resolve_teacher(self.driver.TEACHER)

    def test_populations_are_valid_development_records(self) -> None:
        sizes = {}
        for name, populations in self.driver.POPULATIONS.items():
            document = record.validate_population(
                record.population_document(record.DEVELOPMENT, populations)
            )
            sizes[name] = len(record.ordered_games(document))
        self.assertEqual(sizes, {"record200": 200, "record100": 100, "record50": 50})

    def test_smaller_records_are_leading_subsets_of_each_split(self) -> None:
        full = self.driver.POPULATIONS["record200"]
        for name in ("record100", "record50"):
            for split, seeds in self.driver.POPULATIONS[name].items():
                with self.subTest(name=name, split=split):
                    self.assertEqual(seeds, full[split][: len(seeds)])

    def test_seed_ranges_match_the_documented_plan(self) -> None:
        seeds = sorted(
            seed
            for split in self.driver.POPULATIONS["record200"].values()
            for seed in split
        )
        self.assertEqual(seeds, list(range(944600100, 944600300)))
        self.assertEqual(self.driver.EVAL_SEEDS, list(range(944600300, 944600308)))
        doc = _DOC.read_text(encoding="utf-8")
        for text in (
            "944600100..944600299",
            "944600300..944600307",
            "944600250..944600261",
        ):
            self.assertIn(text, doc)

    def test_learning_settings_match_the_documented_plan(self) -> None:
        self.assertEqual(
            (self.driver.BC_WIDTH, self.driver.CANDIDATE_WIDTH, self.driver.EPOCHS),
            (512, 256, 20),
        )


class BootstrapTest(unittest.TestCase):
    def setUp(self) -> None:
        self.text = _BOOTSTRAP.read_text(encoding="utf-8")

    def test_bash_syntax(self) -> None:
        bash = shutil.which("bash")
        if bash is None:
            self.skipTest("bash is unavailable")
        subprocess.run([bash, "-n", str(_BOOTSTRAP)], check=True)

    def test_pins_match_the_project(self) -> None:
        project = (_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        riichienv = _shell_value(self.text, "FROZEN_RIICHIENV_VERSION")
        self.assertIn(f'"riichienv=={riichienv}"', project)
        self.assertIn("torch==2.13.0", _shell_value(self.text, "FROZEN_TORCH"))
        self.assertIn('ml = ["torch==2.13.0"]', project)
        self.assertEqual(
            _shell_value(self.text, "WHEEL_FILE"), backend.EXPECTED_WHEEL_FILENAME
        )

    def test_learning_revision_is_not_the_arena_pin(self) -> None:
        # Learning reads the policy source schema, added after the Arena pin.
        revision = _shell_value(self.text, "FROZEN_LEARNING_REVISION")
        self.assertRegex(revision, r"^[0-9a-f]{40}$")
        self.assertNotEqual(revision, backend.EXPECTED_LISJONG_REVISION)
        self.assertIn(revision, _DOC.read_text(encoding="utf-8"))

    def test_runs_the_driver_from_the_checked_out_revision(self) -> None:
        self.assertIn('DRIVER="$ARENA_DIR/scripts/aws/calibrate_c0_442.py"', self.text)
        self.assertIn(
            'merge-base --is-ancestor "$ARENA_REVISION" origin/main', self.text
        )
        self.assertEqual(_shell_value(self.text, "REQUIRED_WORKERS"), "4")

    def test_line_endings_are_lf(self) -> None:
        for path in (_BOOTSTRAP, _AWS / "calibrate_c0_442.py"):
            self.assertNotIn(b"\r", path.read_bytes())


if __name__ == "__main__":
    unittest.main()
