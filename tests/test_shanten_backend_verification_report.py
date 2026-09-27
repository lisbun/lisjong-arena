"""#400 pre-registered report: evidence completeness, criteria and decision mapping.

The fixture writes evidence exactly as a complete bootstrap run would (same
files, planned amounts and conditions) with synthetic measurements; each
regression then removes, shrinks or alters one part of it.
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from lisjong_arena.shanten_backend_verification import plan
from lisjong_arena.shanten_backend_verification.__main__ import main
from lisjong_arena.shanten_backend_verification.report import evaluate

_REVISION = plan.LISJONG_REVISION


def _write(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _read(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _worker(backend: str, pid: int, init_kib: int) -> dict:
    native = (
        None
        if backend == "python"
        else {"source_revision": _REVISION, "module_file": "x", "probe_native_calls": 1}
    )
    return {
        "backend": backend,
        "lisjong_revision": _REVISION,
        "pid": pid,
        "native": native,
        "init_peak_rss_kib": init_kib,
    }


def _games(
    directory: Path,
    *,
    policy: plan.PlannedPolicy,
    backend: str,
    seeds: tuple[int, ...],
    workers: int,
    game_s: float,
    wall_s: float,
    init_kib: int,
    used_kib: tuple[int, int],
) -> None:
    games = []
    for index, seed in enumerate(seeds):
        games.append(
            {
                "seed": seed,
                "game_mode": plan.GAME_MODE,
                "scores": [25000, 25000, 25000, 25000],
                "ranks": [1, 2, 3, 4],
                "steps": 100,
                "decisions": 400,
                "semantic_sha256": policy.seed0_semantic_sha256 if seed == 0 else "s",
                "seats": {},
                "elapsed_s": game_s,
                "native_calls": None if backend == "python" else 1000,
                "worker": _worker(backend, 1000 + index % workers, init_kib),
                "peak_rss_kib": init_kib * 10,
            }
        )
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "games.jsonl").write_text(
        "".join(json.dumps(game) + "\n" for game in games), encoding="utf-8"
    )
    _write(
        directory / "summary.json",
        {
            "measurement": "games",
            "policy": policy.catalog,
            "game_mode": plan.GAME_MODE,
            "backend": backend,
            "parent": {"backend": backend, "lisjong_revision": _REVISION, "pid": 1},
            "workers_requested": workers,
            "workers_observed": workers,
            "games": len(seeds),
            "seeds": list(seeds),
            "wall_s": wall_s,
            "games_per_hour": len(seeds) / wall_s * 3600,
            "game_elapsed_s": {"median": game_s},
            "worker_init_peak_rss_kib": {"median": init_kib},
            "worker_peak_rss_kib": {"median": init_kib * 10},
            "system_used_kib": {"baseline": used_kib[0], "peak": used_kib[1]},
        },
    )


def _compare(root: Path, relative: str, left: str, right: str, seeds, policy) -> None:
    _write(
        root / relative,
        {
            "left": str(root / left),
            "right": str(root / right),
            "seeds": list(seeds),
            "expected_semantic": {"0": policy.seed0_semantic_sha256},
            "mismatches": [],
            "ok": True,
        },
    )


def _run_directory(
    root: Path,
    *,
    champion_rust_s: float = 160.0,
    multi_rust_wall: float = 600.0,
    rust_init_kib: int = 40_000,
    startup_rust_ms: float = 80.0,
    rust_without_wheel_exit: int = 1,
) -> Path:
    _write(root / "environment" / "compilers.json", {"absent": ["cc"], "present": []})
    _write(
        root / "fail-closed" / "result.json",
        {
            "python_without_wheel_exit": 0,
            "rust_without_wheel_exit": rust_without_wheel_exit,
            "bad_wheel_exit": 2,
        },
    )
    _write(
        root / "install.json",
        {
            "wheel": {
                "file": plan.WHEEL_FILENAME,
                "sha256": plan.WHEEL_SHA256,
                "bytes": 1,
            },
            "install_s": 0.8,
        },
    )
    _write(
        root / "probe-python.json",
        {"backend": "python", "lisjong_revision": _REVISION, "native": None},
    )
    _write(
        root / "probe-rust.json",
        {
            "backend": "rust",
            "lisjong_revision": _REVISION,
            "native": {"source_revision": _REVISION},
        },
    )
    _write(
        root / "differential" / "result.json",
        {"native_tests_exit": 0, "lisjong_suite_rust_exit": 0},
    )
    for policy in plan.POLICIES:
        for backend, seconds in (("python", 540.0), ("rust", champion_rust_s)):
            _games(
                root / "games-single" / f"{policy.label}-{backend}",
                policy=policy,
                backend=backend,
                seeds=plan.SINGLE_SEEDS,
                workers=1,
                game_s=seconds,
                wall_s=seconds,
                init_kib=40_000,
                used_kib=(0, 0),
            )
        _compare(
            root,
            f"games-single/compare-{policy.label}.json",
            f"games-single/{policy.label}-python",
            f"games-single/{policy.label}-rust",
            plan.SINGLE_SEEDS,
            policy,
        )
        for backend, total in (("python", 550.0), ("rust", champion_rust_s)):
            for index in plan.replay_indices(backend):
                _write(
                    root / "replay" / f"{policy.label}-{backend}-{index}.json",
                    {
                        "mode": "policy",
                        "policy": policy.replay_class,
                        "decisions": policy.decisions,
                        "decisions_sha256": policy.decisions_sha256,
                        "repeat": 1,
                        "pass_total_s": [total + index],
                        "action_mismatches": 0,
                        "native_standard_shanten_calls": (
                            None if backend == "python" else 5_000_000
                        ),
                        "peak_rss_bytes": 700_000_000,
                        "environment": {"shanten_backend": backend},
                    },
                )
    samples = {
        backend: [{"import_first_call_ms": 1.0}] * plan.STARTUP_REPEAT
        for backend in ("python", "rust")
    }
    _write(
        root / "startup.json",
        {
            "repeat": plan.STARTUP_REPEAT,
            "samples": samples,
            "import_first_call_ms": {
                "python": {"median": 80.0},
                "rust": {"median": startup_rust_ms},
            },
        },
    )
    for backend, wall, init in (
        ("python", 1800.0, 40_000),
        ("rust", multi_rust_wall, rust_init_kib),
    ):
        _games(
            root / "games-multi" / f"champion-{backend}",
            policy=plan.CHAMPION,
            backend=backend,
            seeds=plan.MULTI_SEEDS,
            workers=plan.MULTI_WORKERS,
            game_s=wall / 2,
            wall_s=wall,
            init_kib=init,
            used_kib=(1_000_000, 12_000_000),
        )
    _compare(
        root,
        "games-multi/compare.json",
        "games-multi/champion-python",
        "games-multi/champion-rust",
        plan.MULTI_SEEDS,
        plan.CHAMPION,
    )
    return root


class ReportDecisionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp()) / "run"

    def test_complete_evidence_meeting_every_criterion_recommends(self) -> None:
        report = evaluate(_run_directory(self.root), hourly_usd=1.0)
        self.assertEqual(report["evidence"], {"complete": True, "problems": []})
        self.assertEqual(report["decision"], "recommend-opt-in")
        self.assertTrue(all(report["criteria"].values()))
        self.assertIn("compute_usd_per_1000_games", report["findings"]["multi_worker"])

    def test_any_mismatch_blocks_every_performance_decision(self) -> None:
        root = _run_directory(self.root)
        run = root / "replay" / "champion-rust-2.json"
        payload = _read(run)
        payload["action_mismatches"] = 1
        _write(run, payload)
        report = evaluate(root)
        self.assertFalse(report["criteria"]["equivalence_zero_mismatch"])
        self.assertEqual(report["decision"], "investigate-mismatch")

        root = _run_directory(Path(tempfile.mkdtemp()) / "run")
        compare = _read(root / "games-multi" / "compare.json")
        compare.update(ok=False, mismatches=[{"kind": "scores", "seed": 3}])
        _write(root / "games-multi" / "compare.json", compare)
        self.assertEqual(evaluate(root)["decision"], "investigate-mismatch")

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


class EvidenceCompletenessTest(unittest.TestCase):
    """Missing, smaller or different evidence never yields a decision."""

    def _assert_incomplete(self, mutate, expected_fragment: str) -> None:
        root = _run_directory(Path(tempfile.mkdtemp()) / "run")
        mutate(root)
        report = evaluate(root)
        self.assertEqual(report["decision"], "incomplete-evidence")
        self.assertIsNone(report["criteria"])
        self.assertIsNone(report["findings"])
        self.assertFalse(report["evidence"]["complete"])
        problems = "\n".join(report["evidence"]["problems"])
        self.assertIn(expected_fragment, problems)

    @staticmethod
    def _edit(path: Path, **changes) -> None:
        payload = _read(path)
        payload.update(changes)
        _write(path, payload)

    # ---- missing ------------------------------------------------------

    def test_missing_files(self) -> None:
        for relative in (
            "differential/result.json",
            "startup.json",
            "install.json",
            "probe-rust.json",
            "games-multi/compare.json",
            "games-multi/champion-rust/summary.json",
            "games-multi/champion-python/games.jsonl",
            "games-single/two-step-rust/summary.json",
            "replay/champion-python-4.json",
        ):
            with self.subTest(relative=relative):
                self._assert_incomplete(
                    lambda root: (root / relative).unlink(), f"{relative}: missing"
                )

    def test_missing_replay_directory(self) -> None:
        self._assert_incomplete(
            lambda root: shutil.rmtree(root / "replay"),
            "replay/champion-python-1.json: missing",
        )

    def test_empty_or_partial_differential_result(self) -> None:
        for payload in ({}, {"native_tests_exit": 0}):
            with self.subTest(payload=payload):
                self._assert_incomplete(
                    lambda root: _write(root / "differential/result.json", payload),
                    "differential/result.json: keys are",
                )

    # ---- less work than planned ---------------------------------------

    def test_single_replay_run_per_backend(self) -> None:
        def keep_first_only(root: Path) -> None:
            for policy in plan.POLICIES:
                for backend in ("python", "rust"):
                    for index in plan.replay_indices(backend)[1:]:
                        (
                            root / "replay" / f"{policy.label}-{backend}-{index}.json"
                        ).unlink()

        self._assert_incomplete(
            keep_first_only, "replay/champion-python-4.json: missing"
        )

    def test_replay_with_two_passes_or_no_pass(self) -> None:
        for totals in ([550.0, 549.0], []):
            with self.subTest(totals=totals):
                self._assert_incomplete(
                    lambda root: self._edit(
                        root / "replay/champion-rust-3.json", pass_total_s=totals
                    ),
                    "pass_total_s must hold 1",
                )

    def test_one_worker_multi_run_is_not_the_planned_configuration(self) -> None:
        def one_worker(root: Path) -> None:
            for backend in ("python", "rust"):
                self._edit(
                    root / f"games-multi/champion-{backend}/summary.json",
                    workers_requested=1,
                    workers_observed=1,
                )

        self._assert_incomplete(one_worker, "workers_requested is 1; expected 16")

    def test_fewer_workers_observed_than_requested(self) -> None:
        self._assert_incomplete(
            lambda root: self._edit(
                root / "games-multi/champion-rust/summary.json", workers_observed=15
            ),
            "workers_observed is 15; expected 16",
        )

    def test_fewer_games_or_other_seeds(self) -> None:
        def drop_last_game(root: Path) -> None:
            path = root / "games-multi/champion-python/games.jsonl"
            lines = path.read_text(encoding="utf-8").splitlines()
            path.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")

        self._assert_incomplete(
            drop_last_game, "games-multi/champion-python/games.jsonl: seeds"
        )

        def other_seeds(root: Path) -> None:
            seeds = list(range(100, 132))
            for backend in ("python", "rust"):
                self._edit(
                    root / f"games-multi/champion-{backend}/summary.json", seeds=seeds
                )

        self._assert_incomplete(other_seeds, "seeds is [100")

        self._assert_incomplete(
            lambda root: self._edit(
                root / "games-multi/champion-rust/summary.json", games=31
            ),
            "games is 31; expected 32",
        )

    def test_short_startup(self) -> None:
        self._assert_incomplete(
            lambda root: self._edit(root / "startup.json", repeat=2),
            "startup.json: repeat is 2; expected 10",
        )

    # ---- different conditions -----------------------------------------

    def test_replay_backend_policy_or_input_differs(self) -> None:
        cases = {
            "environment.shanten_backend": {
                "environment": {"shanten_backend": "python"}
            },
            "policy is": {"policy": "lisjong.policies:TwoStepUkeirePolicy"},
            "decisions_sha256": {"decisions_sha256": "0" * 64},
            "no native calls": {"native_standard_shanten_calls": None},
        }
        for fragment, changes in cases.items():
            with self.subTest(fragment=fragment):
                self._assert_incomplete(
                    lambda root: self._edit(
                        root / "replay/champion-rust-2.json", **changes
                    ),
                    fragment,
                )

    def test_game_backend_policy_or_revision_differs(self) -> None:
        cases = {
            "backend is 'python'; expected 'rust'": {"backend": "python"},
            "policy is 'two-step'": {"policy": "two-step"},
            "game_mode": {"game_mode": "4p-red-single"},
        }
        for fragment, changes in cases.items():
            with self.subTest(fragment=fragment):
                self._assert_incomplete(
                    lambda root: self._edit(
                        root / "games-multi/champion-rust/summary.json", **changes
                    ),
                    fragment,
                )

        def python_worker_in_rust_run(root: Path) -> None:
            path = root / "games-multi/champion-rust/games.jsonl"
            games = [json.loads(line) for line in path.read_text("utf-8").splitlines()]
            games[5]["worker"] = _worker("python", games[5]["worker"]["pid"], 40_000)
            games[5]["native_calls"] = None
            path.write_text("".join(json.dumps(g) + "\n" for g in games), "utf-8")

        self._assert_incomplete(python_worker_in_rust_run, "worker.backend is 'python'")

    def test_wheel_or_extension_identity_differs(self) -> None:
        self._assert_incomplete(
            lambda root: self._edit(
                root / "install.json",
                wheel={"file": plan.WHEEL_FILENAME, "sha256": "0" * 64},
            ),
            "wheel.sha256",
        )
        self._assert_incomplete(
            lambda root: self._edit(
                root / "probe-rust.json", native={"source_revision": "unknown"}
            ),
            "native.source_revision",
        )

    def test_compare_without_the_213_digest_or_on_other_runs(self) -> None:
        self._assert_incomplete(
            lambda root: self._edit(
                root / "games-single/compare-champion.json", expected_semantic={}
            ),
            "expected_semantic",
        )
        self._assert_incomplete(
            lambda root: self._edit(
                root / "games-multi/compare.json",
                right=str(root / "games-single/champion-rust"),
            ),
            "games-multi/compare.json: right",
        )

    def test_extra_replay_runs_are_not_silently_included(self) -> None:
        def extra_run(root: Path) -> None:
            shutil.copy(
                root / "replay/champion-rust-2.json",
                root / "replay/champion-rust-7.json",
            )

        self._assert_incomplete(
            extra_run, "replay/champion-rust-7.json: not part of the plan"
        )

    def test_report_cli_exits_3_on_incomplete_evidence(self) -> None:
        root = _run_directory(Path(tempfile.mkdtemp()) / "run")
        (root / "startup.json").unlink()
        self.assertEqual(main(["report", str(root)]), 3)
        complete = _run_directory(Path(tempfile.mkdtemp()) / "run")
        self.assertEqual(main(["report", str(complete)]), 0)


if __name__ == "__main__":
    unittest.main()
