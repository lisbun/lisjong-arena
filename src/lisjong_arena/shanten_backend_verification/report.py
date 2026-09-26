"""Pre-registered evaluation of one #400 run directory.

The thresholds and the decision mapping below are fixed before the AWS run
(lisbun/lisjong#216 §6, lisbun/lisjong-arena#400 §3) and are applied mechanically
to the evidence the bootstrap writes.  Changing them after a result is seen is
out of contract; a change needs a recorded reason before the run.

Layout read here (written by ``scripts/aws/bootstrap-rust-shanten-400.sh``)::

    environment/compilers.json         {"absent": [...], "present": [...]}
    fail-closed/result.json            exits of the no-wheel / bad-wheel checks
    install.json                       wheel identity and install seconds
    differential/result.json           exits of the lisjong native / full suites
    games-single/compare-<policy>.json
    games-single/<policy>-<backend>/summary.json
    startup.json
    replay/<policy>-<backend>-<n>.json lisjong tools/benchmark_tile_efficiency.py
    games-multi/compare.json
    games-multi/champion-<backend>/summary.json
"""

from __future__ import annotations

import json
import statistics
from pathlib import Path

REDUCTION_MIN = 0.30
"""Primary: single-worker Champion decision time reduction vs Python."""
DECLINE_REDUCTION_BELOW = 0.15
"""#213 rule: below this the backend is not worth its maintenance cost."""
EXTRA_MEMORY_MAX_BYTES = 20_000_000
"""Additional memory per worker (20 MB, decimal)."""
STARTUP_EXTRA_MAX_MS = 100.0
"""Additional import + first calculation time."""

SINGLE_POLICIES = ("champion", "two-step")
REPLAY_POLICIES = ("champion", "two-step")


def _load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _replays(root: Path, policy: str, backend: str) -> list[dict[str, object]]:
    runs = sorted((root / "replay").glob(f"{policy}-{backend}-*.json"))
    return [_load(path) for path in runs]


def _median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def _reduction(python: float | None, rust: float | None) -> float | None:
    if python is None or rust is None or python <= 0:
        return None
    return 1.0 - rust / python


def _check(passed: bool | None) -> bool:
    return passed is True


def evaluate(run_directory: str | Path, *, hourly_usd: float | None = None):
    root = Path(run_directory)
    findings: dict[str, object] = {}

    # ---- distribution / fail-closed -------------------------------------
    compilers = _load(root / "environment" / "compilers.json")
    fail_closed = _load(root / "fail-closed" / "result.json")
    install = _load(root / "install.json")
    findings["distribution"] = {
        "compilers_present": compilers["present"],
        "install_s": install["install_s"],
        "wheel": install["wheel"],
        "fail_closed": fail_closed,
    }
    distribution_ok = (
        not compilers["present"]
        and fail_closed["python_without_wheel_exit"] == 0
        and fail_closed["rust_without_wheel_exit"] != 0
        and fail_closed["bad_wheel_exit"] != 0
    )

    # ---- equivalence -----------------------------------------------------
    differential = _load(root / "differential" / "result.json")
    single_compares = {
        policy: _load(root / "games-single" / f"compare-{policy}.json")
        for policy in SINGLE_POLICIES
    }
    multi_compare = _load(root / "games-multi" / "compare.json")
    replay_mismatches = {
        f"{policy}-{backend}": [run["action_mismatches"] for run in runs]
        for policy in REPLAY_POLICIES
        for backend in ("python", "rust")
        for runs in [_replays(root, policy, backend)]
    }
    equivalence_ok = (
        all(code == 0 for code in differential.values())
        and all(result["ok"] for result in single_compares.values())
        and multi_compare["ok"]
        and all(
            values and all(value == 0 for value in values)
            for values in replay_mismatches.values()
        )
    )
    findings["equivalence"] = {
        "differential_exits": differential,
        "single_game_mismatches": {
            policy: result["mismatches"] for policy, result in single_compares.items()
        },
        "multi_game_mismatches": multi_compare["mismatches"],
        "replay_action_mismatches": replay_mismatches,
        "ok": equivalence_ok,
    }

    # ---- single worker: fixed-decision replay ---------------------------
    replay: dict[str, object] = {}
    for policy in REPLAY_POLICIES:
        totals = {
            backend: [
                total
                for run in _replays(root, policy, backend)
                for total in run["pass_total_s"]
            ]
            for backend in ("python", "rust")
        }
        peaks = {
            backend: [
                run["peak_rss_bytes"]
                for run in _replays(root, policy, backend)
                if run["peak_rss_bytes"] is not None
            ]
            for backend in ("python", "rust")
        }
        medians = {backend: _median(values) for backend, values in totals.items()}
        peak_medians = {backend: _median(values) for backend, values in peaks.items()}
        replay[policy] = {
            "pass_total_s": totals,
            "median_s": medians,
            "reduction": _reduction(medians["python"], medians["rust"]),
            "peak_rss_bytes_median": peak_medians,
            "extra_peak_rss_bytes": (
                None
                if None in peak_medians.values()
                else peak_medians["rust"] - peak_medians["python"]
            ),
        }
    findings["replay"] = replay
    champion_reduction = replay["champion"]["reduction"]

    # ---- single worker: games and startup -------------------------------
    single_games = {}
    for policy in SINGLE_POLICIES:
        entry = {}
        for backend in ("python", "rust"):
            summary = _load(
                root / "games-single" / f"{policy}-{backend}" / "summary.json"
            )
            entry[backend] = {
                "game_s": summary["game_elapsed_s"]["median"],
                "worker_init_peak_rss_kib": summary["worker_init_peak_rss_kib"],
            }
        entry["reduction"] = _reduction(
            entry["python"]["game_s"], entry["rust"]["game_s"]
        )
        single_games[policy] = entry
    findings["single_games"] = single_games

    startup = _load(root / "startup.json")
    startup_median = {
        backend: startup["import_first_call_ms"][backend]["median"]
        for backend in ("python", "rust")
    }
    startup_extra = startup_median["rust"] - startup_median["python"]
    findings["startup"] = {"median_ms": startup_median, "extra_ms": startup_extra}

    # ---- multi worker ----------------------------------------------------
    multi = {
        backend: _load(root / "games-multi" / f"champion-{backend}" / "summary.json")
        for backend in ("python", "rust")
    }

    def init_rss(backend: str) -> float | None:
        values = multi[backend]["worker_init_peak_rss_kib"]
        return None if values is None else values["median"] * 1024

    def system_increase(backend: str) -> int | None:
        memory = multi[backend]["system_used_kib"]
        if memory["baseline"] is None or memory["peak"] is None:
            return None
        return (memory["peak"] - memory["baseline"]) * 1024

    workers = multi["rust"]["workers_requested"]
    python_increase = system_increase("python")
    rust_increase = system_increase("rust")
    python_init = init_rss("python")
    rust_init = init_rss("rust")
    multi_findings = {
        "workers_requested": {b: multi[b]["workers_requested"] for b in multi},
        "workers_observed": {b: multi[b]["workers_observed"] for b in multi},
        "games": {b: multi[b]["games"] for b in multi},
        "wall_s": {b: multi[b]["wall_s"] for b in multi},
        "games_per_hour": {b: multi[b]["games_per_hour"] for b in multi},
        "wall_reduction": _reduction(
            multi["python"]["wall_s"], multi["rust"]["wall_s"]
        ),
        "worker_peak_rss_kib": {b: multi[b]["worker_peak_rss_kib"] for b in multi},
        "extra_worker_init_rss_bytes": (
            None if None in (python_init, rust_init) else rust_init - python_init
        ),
        "system_used_increase_bytes": {
            "python": python_increase,
            "rust": rust_increase,
        },
    }
    if hourly_usd is not None:
        multi_findings["compute_usd_per_1000_games"] = {
            b: 1000.0 / multi[b]["games_per_hour"] * hourly_usd for b in multi
        }
    findings["multi_worker"] = multi_findings

    # ---- pre-registered criteria ----------------------------------------
    replay_extra = replay["champion"]["extra_peak_rss_bytes"]
    init_extra = multi_findings["extra_worker_init_rss_bytes"]
    criteria = {
        "equivalence_zero_mismatch": equivalence_ok,
        "compiler_free_install_and_fail_closed": distribution_ok,
        "champion_reduction_at_least_30pct": (
            None if champion_reduction is None else champion_reduction >= REDUCTION_MIN
        ),
        "extra_memory_per_worker_at_most_20mb": (
            None
            if replay_extra is None or init_extra is None
            else max(replay_extra, init_extra) <= EXTRA_MEMORY_MAX_BYTES
        ),
        "extra_startup_at_most_100ms": startup_extra <= STARTUP_EXTRA_MAX_MS,
        "all_workers_observed": all(
            multi[b]["workers_observed"] == multi[b]["workers_requested"] for b in multi
        ),
        "multi_worker_reduction_at_least_30pct": (
            None
            if multi_findings["wall_reduction"] is None
            else multi_findings["wall_reduction"] >= REDUCTION_MIN
        ),
        "multi_worker_system_memory_within_budget": (
            None
            if python_increase is None or rust_increase is None
            else rust_increase <= python_increase + workers * EXTRA_MEMORY_MAX_BYTES
        ),
    }

    if not equivalence_ok:
        decision = "investigate-mismatch"
    elif (
        champion_reduction is not None and champion_reduction < DECLINE_REDUCTION_BELOW
    ):
        decision = "decline"
    elif all(_check(value) for value in criteria.values()):
        decision = "recommend-opt-in"
    else:
        decision = "further-study"

    return {
        "run_directory": str(root),
        "thresholds": {
            "reduction_min": REDUCTION_MIN,
            "decline_reduction_below": DECLINE_REDUCTION_BELOW,
            "extra_memory_max_bytes": EXTRA_MEMORY_MAX_BYTES,
            "startup_extra_max_ms": STARTUP_EXTRA_MAX_MS,
        },
        "findings": findings,
        "criteria": criteria,
        "decision": decision,
    }
