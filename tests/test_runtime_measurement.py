import subprocess
import sys
import unittest
from unittest.mock import patch

from lisjong_arena import runtime_measurement
from lisjong_arena.phase6_snapshot import training as phase6_training


class RuntimeMeasurementTests(unittest.TestCase):
    def test_peak_process_ram_is_best_effort_bytes(self) -> None:
        value = runtime_measurement.peak_process_ram_bytes()
        self.assertTrue(value is None or (type(value) is int and value >= 0))

    def test_neutral_module_import_does_not_pull_research_or_torch(self) -> None:
        code = (
            "import sys; "
            "import lisjong_arena.runtime_measurement; "
            "assert 'torch' not in sys.modules; "
            "assert 'lisjong_arena.phase6_snapshot.training' not in sys.modules"
        )
        completed = subprocess.run(
            [sys.executable, "-c", code],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_phase6_compatibility_path_delegates_to_neutral_helper(self) -> None:
        with patch.object(
            phase6_training,
            "peak_process_ram_bytes",
            return_value=123456,
        ):
            self.assertEqual(phase6_training._peak_process_ram_bytes(), 123456)


if __name__ == "__main__":
    unittest.main()
