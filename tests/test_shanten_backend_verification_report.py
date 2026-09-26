"""#400 pre-registered report: criteria and decision mapping on synthetic evidence."""

import json
import tempfile
import unittest
from pathlib import Path

from lisjong_arena.shanten_backend_verification.report import evaluate


def _write(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _summary(game_s: float, wall_s: float, *, init_kib: int, used_kib: tuple) -> dict:
    return {
        "game_elapsed_s": {"median": game_s},
        "worker_init_peak_rss_kib": {"median": init_kib},
        "worker_peak_rss_kib": {"median": init_kib * 10},
        "workers_requested": 16,
        "workers_observed": 16,
        "games": 32,
        "wall_s": wall_s,
        "games_per_hour": 32 / wall_s * 3600,
        "system_used_kib": {"baseline": used_kib[0], "peak": used_kib[1]},
    }


def _run_directory(
    root: Path,
    *,
    champion_rust_s: float = 160.0,
    multi_rust_wall: float = 600.0,
    rust_init_kib: int = 40_000,
    startup_rust_ms: float = 80.0,
    replay_mismatch: int = 0,
    compare_ok: bool = True,
    rust_without_wheel_exit: int = 1,
) -> Path:
    _write(root / "environment" / "compilers.json", {"absent": [], "present": []})
    _write(
        root / "fail-closed" / "result.json",
        {
            "python_without_wheel_exit": 0,
            "rust_without_wheel_exit": rust_without_wheel_exit,
            "bad_wheel_exit": 2,
        },
    )
    _write(root / "install.json", {"wheel": {"sha256": "x"}, "install_s": 0.8})
    _write(
        root / "differential" / "result.json",
        {"native_tests_exit": 0, "lisjong_suite_rust_exit": 0},
    )
    for policy in ("champion", "two-step"):
        _write(
            root / "games-single" / f"compare-{policy}.json",
            {"ok": True, "mismatches": []},
        )
        for backend, seconds in (("python", 540.0), ("rust", champion_rust_s)):
            _write(
                root / "games-single" / f"{policy}-{backend}" / "summary.json",
                _summary(seconds, seconds, init_kib=40_000, used_kib=(0, 0)),
            )
        for index in (1, 2, 3):
            for backend, total in (("python", 550.0), ("rust", champion_rust_s)):
                _write(
                    root / "replay" / f"{policy}-{backend}-{index}.json",
                    {
                        "pass_total_s": [total + index],
                        "action_mismatches": replay_mismatch
                        if backend == "rust"
                        else 0,
                        "peak_rss_bytes": 700_000_000,
                    },
                )
    _write(
        root / "startup.json",
        {
            "import_first_call_ms": {
                "python": {"median": 80.0},
                "rust": {"median": startup_rust_ms},
            }
        },
    )
    _write(
        root / "games-multi" / "compare.json",
        {"ok": compare_ok, "mismatches": [] if compare_ok else [{"kind": "scores"}]},
    )
    _write(
        root / "games-multi" / "champion-python" / "summary.json",
        _summary(800.0, 1800.0, init_kib=40_000, used_kib=(1_000_000, 12_000_000)),
    )
    _write(
        root / "games-multi" / "champion-rust" / "summary.json",
        _summary(
            240.0,
            multi_rust_wall,
            init_kib=rust_init_kib,
            used_kib=(1_000_000, 12_000_000),
        ),
    )
    return root


class ReportDecisionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp()) / "run"

    def test_all_criteria_met_recommends_opt_in(self) -> None:
        report = evaluate(_run_directory(self.root), hourly_usd=1.0)
        self.assertEqual(report["decision"], "recommend-opt-in")
        self.assertTrue(all(report["criteria"].values()))
        self.assertIn("compute_usd_per_1000_games", report["findings"]["multi_worker"])

    def test_any_mismatch_blocks_every_performance_decision(self) -> None:
        for arguments in ({"replay_mismatch": 1}, {"compare_ok": False}):
            with self.subTest(arguments=arguments):
                root = Path(tempfile.mkdtemp()) / "run"
                report = evaluate(_run_directory(root, **arguments))
                self.assertFalse(report["criteria"]["equivalence_zero_mismatch"])
                self.assertEqual(report["decision"], "investigate-mismatch")

    def test_small_single_worker_gain_declines(self) -> None:
        report = evaluate(_run_directory(self.root, champion_rust_s=500.0))
        self.assertEqual(report["decision"], "decline")

    def test_single_worker_pass_without_multi_worker_effect_needs_more_study(
        self,
    ) -> None:
        report = evaluate(_run_directory(self.root, multi_rust_wall=1500.0))
        self.assertFalse(report["criteria"]["multi_worker_reduction_at_least_30pct"])
        self.assertTrue(report["criteria"]["champion_reduction_at_least_30pct"])
        self.assertEqual(report["decision"], "further-study")

    def test_memory_startup_and_fail_closed_thresholds(self) -> None:
        cases = {
            "extra_memory_per_worker_at_most_20mb": {"rust_init_kib": 70_000},
            "extra_startup_at_most_100ms": {"startup_rust_ms": 181.0},
            "compiler_free_install_and_fail_closed": {"rust_without_wheel_exit": 0},
        }
        for criterion, arguments in cases.items():
            with self.subTest(criterion=criterion):
                root = Path(tempfile.mkdtemp()) / "run"
                report = evaluate(_run_directory(root, **arguments))
                self.assertFalse(report["criteria"][criterion])
                self.assertEqual(report["decision"], "further-study")


if __name__ == "__main__":
    unittest.main()
