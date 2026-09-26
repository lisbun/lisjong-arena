"""Startup, multi-worker game and equivalence measurements (#400).

Each measurement runs one backend at a time; the backend comes from
``LISJONG_SHANTEN_BACKEND`` in the environment of the processes that compute
(the parent for ``games``, fresh child processes for ``startup``).  Workers are
``spawn`` processes, so they inherit the variable, and every worker verifies
its backend with ``require_shanten_backend()`` before it plays a game.

Memory is reported per process as ``VmHWM`` (peak RSS of the process's own
address space, KiB) and, on Linux, as system-wide used memory
(``MemTotal - MemAvailable``) sampled by the parent.  ``ru_maxrss`` is not used:
Linux carries the pre-exec high-water mark across ``exec``, so a spawned worker
or startup child would report at least its parent's RSS.  Both values are
``None`` where ``/proc`` is unavailable.
"""

from __future__ import annotations

import importlib
import json
import multiprocessing
import os
import statistics
import subprocess
import sys
import threading
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

from .backend import (
    BACKEND_ENVIRONMENT_VARIABLE,
    BACKENDS,
    EXPECTED_LISJONG_REVISION,
    NATIVE_MODULE,
    PYTHON_BACKEND,
    ShantenBackendVerificationError,
    _probe_tiles,
    native_call_count,
    require_shanten_backend,
)

GAMES_FILENAME = "games.jsonl"
SUMMARY_FILENAME = "summary.json"
_SPAWN = multiprocessing.get_context("spawn")
_MEMORY_SAMPLE_SECONDS = 2.0


def peak_rss_kib() -> int | None:
    """``VmHWM`` of this process in KiB, or ``None`` without ``/proc``."""
    try:
        text = Path("/proc/self/status").read_text(encoding="ascii")
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("VmHWM:"):
            return int(line.split()[1])
    return None


def _system_used_kib() -> int | None:
    try:
        text = Path("/proc/meminfo").read_text(encoding="ascii")
    except OSError:
        return None
    values = {}
    for line in text.splitlines():
        name, _, rest = line.partition(":")
        values[name] = int(rest.split()[0])
    return values["MemTotal"] - values["MemAvailable"]


def policy_factory(reference: str):
    """A curated ``POLICY_CATALOG`` id or an importable ``module:Class``."""
    if ":" in reference:
        module_name, _, attribute = reference.partition(":")
        return getattr(importlib.import_module(module_name), attribute)
    from lisjong_arena.policy_catalog import POLICY_CATALOG

    return POLICY_CATALOG[reference].factory


def _distribution(values: list[float]) -> dict[str, float] | None:
    if not values:
        return None
    ordered = sorted(values)
    return {
        "n": len(ordered),
        "min": ordered[0],
        "median": statistics.median(ordered),
        "mean": statistics.fmean(ordered),
        "max": ordered[-1],
    }


# --------------------------------------------------------------------------
# startup: import + first calculation in fresh processes


# The child imports nothing from lisjong_arena: the Arena package imports
# lisjong itself, which would move the measured import out of the timer.
_STARTUP_CHILD = """
import json, time
started = time.perf_counter()
from lisjong.hand_evaluation import calculate_shanten
from lisjong.policy_contract.tile import Tile, TileCategory, TileType
tiles = [Tile(TileType(TileCategory[category], rank))
         for category, rank in {tiles!r}]
value = calculate_shanten(tiles)
elapsed = time.perf_counter() - started
peak = None
try:
    for line in open("/proc/self/status", encoding="ascii"):
        if line.startswith("VmHWM:"):
            peak = int(line.split()[1])
except OSError:
    pass
print(json.dumps({{"import_first_call_ms": elapsed * 1000.0, "value": value,
                  "peak_rss_kib": peak}}))
"""


def _startup_once(backend: str) -> dict[str, object]:
    environment = dict(os.environ)
    environment[BACKEND_ENVIRONMENT_VARIABLE] = backend
    tiles = [
        (tile.tile_type.category.name, tile.tile_type.rank) for tile in _probe_tiles()
    ]
    process = subprocess.run(
        [sys.executable, "-c", _STARTUP_CHILD.format(tiles=tiles)],
        capture_output=True,
        text=True,
        env=environment,
        timeout=120,
    )
    if process.returncode != 0:
        raise ShantenBackendVerificationError(
            f"{backend} startup probe failed: {process.stderr.strip()}"
        )
    return json.loads(process.stdout)


def run_startup(repeat: int) -> dict[str, object]:
    """Alternate python / rust fresh processes ``repeat`` times each."""
    if type(repeat) is not int or repeat < 1:
        raise ValueError("repeat must be an int >= 1")
    samples: dict[str, list[dict[str, object]]] = {name: [] for name in BACKENDS}
    for round_index in range(repeat):
        order = BACKENDS if round_index % 2 == 0 else tuple(reversed(BACKENDS))
        for backend in order:
            samples[backend].append(_startup_once(backend))
    values = {sample["value"] for items in samples.values() for sample in items}
    if len(values) != 1:
        raise ShantenBackendVerificationError(f"startup probe values differ: {values}")
    return {
        "measurement": "startup",
        "repeat": repeat,
        "order": "alternating, python first on even rounds",
        "samples": samples,
        "import_first_call_ms": {
            backend: _distribution(
                [sample["import_first_call_ms"] for sample in samples[backend]]
            )
            for backend in BACKENDS
        },
        "peak_rss_kib": {
            backend: _distribution(
                [
                    sample["peak_rss_kib"]
                    for sample in samples[backend]
                    if sample["peak_rss_kib"] is not None
                ]
            )
            for backend in BACKENDS
        },
    }


# --------------------------------------------------------------------------
# games: fixed-seed LocalGameRunner games over spawn workers

_WORKER: dict[str, object] = {}


def _initialize_worker(backend: str, revision: str, policy: str) -> None:
    from lisjong_arena.riichienv.local_game_runner import LocalGameRunner

    record = require_shanten_backend(backend, expected_revision=revision)
    _WORKER["backend"] = backend
    _WORKER["factory"] = policy_factory(policy)
    _WORKER["runner"] = LocalGameRunner
    _WORKER["record"] = {**record, "init_peak_rss_kib": peak_rss_kib()}


def _play(seed: int, game_mode: str) -> dict[str, object]:
    from lisjong.policy_contract.seat import Seat

    from lisjong_arena.decision_capture import (
        RecordingPolicy,
        seat_digests,
        semantic_digest,
    )

    records: list = []
    factory = _WORKER["factory"]
    policies = {seat: RecordingPolicy(factory(), records) for seat in Seat}
    calls_before = native_call_count()
    started = time.perf_counter()
    result = _WORKER["runner"](policies, seed=seed, game_mode=game_mode).run()
    elapsed = time.perf_counter() - started
    calls_after = native_call_count()
    if _WORKER["backend"] == PYTHON_BACKEND:
        if NATIVE_MODULE in sys.modules:
            raise ShantenBackendVerificationError(
                "the python backend imported the native extension during a game"
            )
        native_calls = None
    else:
        native_calls = calls_after - calls_before
        if native_calls < 1:
            raise ShantenBackendVerificationError(
                f"seed {seed}: the rust backend made no native calls"
            )
    return {
        "seed": seed,
        "game_mode": game_mode,
        "scores": list(result.scores),
        "ranks": list(result.ranks),
        "steps": result.steps,
        "decisions": len(records),
        "semantic_sha256": semantic_digest(records),
        "seats": {str(seat): value for seat, value in seat_digests(records).items()},
        "elapsed_s": elapsed,
        "native_calls": native_calls,
        "worker": _WORKER["record"],
        "peak_rss_kib": peak_rss_kib(),
    }


class _MemorySampler:
    def __init__(self) -> None:
        self.baseline_kib = _system_used_kib()
        self.peak_kib = self.baseline_kib
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self._stop.wait(_MEMORY_SAMPLE_SECONDS):
            used = _system_used_kib()
            if used is not None and (self.peak_kib is None or used > self.peak_kib):
                self.peak_kib = used

    def __enter__(self) -> "_MemorySampler":
        if self.baseline_kib is not None:
            self._thread.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join()


def run_games(
    *,
    policy: str,
    seeds: list[int],
    game_mode: str,
    backend: str,
    workers: int,
    out_dir: str | Path,
    expected_revision: str = EXPECTED_LISJONG_REVISION,
) -> dict[str, object]:
    if type(workers) is not int or workers < 1:
        raise ValueError("workers must be an int >= 1")
    if not seeds or len(set(seeds)) != len(seeds):
        raise ValueError("seeds must be a non-empty list of distinct ints")
    out = Path(out_dir)
    if out.exists():
        raise FileExistsError(f"refusing to reuse {out}")
    parent = require_shanten_backend(backend, expected_revision=expected_revision)
    out.mkdir(parents=True)

    games: list[dict[str, object]] = []
    with _MemorySampler() as memory:
        started = time.perf_counter()
        executor = ProcessPoolExecutor(
            max_workers=workers,
            mp_context=_SPAWN,
            initializer=_initialize_worker,
            initargs=(backend, expected_revision, policy),
        )
        try:
            futures = [executor.submit(_play, seed, game_mode) for seed in seeds]
            for future in as_completed(futures):
                games.append(future.result())
        finally:
            executor.shutdown(wait=True, cancel_futures=True)
        wall = time.perf_counter() - started

    games.sort(key=lambda game: game["seed"])
    with (out / GAMES_FILENAME).open("w", encoding="utf-8") as handle:
        for game in games:
            handle.write(json.dumps(game, sort_keys=True) + "\n")

    per_worker: dict[int, dict[str, object]] = {}
    for game in games:
        worker = game["worker"]
        entry = per_worker.setdefault(
            worker["pid"],
            {"games": 0, "init_peak_rss_kib": worker["init_peak_rss_kib"]},
        )
        entry["games"] += 1
        entry["peak_rss_kib"] = (
            max(entry.get("peak_rss_kib") or 0, game["peak_rss_kib"] or 0) or None
        )
    summary = {
        "measurement": "games",
        "policy": policy,
        "game_mode": game_mode,
        "backend": backend,
        "parent": parent,
        "workers_requested": workers,
        "workers_observed": len(per_worker),
        "games": len(games),
        "seeds": [game["seed"] for game in games],
        "wall_s": wall,
        "games_per_hour": len(games) / wall * 3600.0,
        "game_elapsed_s": _distribution([game["elapsed_s"] for game in games]),
        "native_calls": (
            None
            if backend == PYTHON_BACKEND
            else sum(game["native_calls"] for game in games)
        ),
        "worker_init_peak_rss_kib": _distribution(
            [
                entry["init_peak_rss_kib"]
                for entry in per_worker.values()
                if entry["init_peak_rss_kib"] is not None
            ]
        ),
        "worker_peak_rss_kib": _distribution(
            [
                entry["peak_rss_kib"]
                for entry in per_worker.values()
                if entry["peak_rss_kib"] is not None
            ]
        ),
        "system_used_kib": {
            "baseline": memory.baseline_kib,
            "peak": memory.peak_kib,
            "sample_seconds": _MEMORY_SAMPLE_SECONDS,
        },
        "per_worker": {str(pid): entry for pid, entry in sorted(per_worker.items())},
        "cpu_count": os.cpu_count(),
        "python": sys.version,
    }
    (out / SUMMARY_FILENAME).write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return summary


# --------------------------------------------------------------------------
# compare: per-seed equality of two game runs

_COMPARED_FIELDS = ("scores", "ranks", "steps", "decisions", "semantic_sha256", "seats")


def _read_games(directory: str | Path) -> dict[int, dict[str, object]]:
    games = {}
    for line in (Path(directory) / GAMES_FILENAME).read_text("utf-8").splitlines():
        game = json.loads(line)
        if game["seed"] in games:
            raise ValueError(f"{directory}: duplicate seed {game['seed']}")
        games[game["seed"]] = game
    return games


def compare_games(
    left: str | Path,
    right: str | Path,
    *,
    expected_semantic: dict[int, str] | None = None,
) -> dict[str, object]:
    """Compare two ``games`` outputs seed by seed; ``ok`` only if all match."""
    first = _read_games(left)
    second = _read_games(right)
    mismatches: list[dict[str, object]] = []
    if set(first) != set(second):
        mismatches.append(
            {
                "kind": "seed-set",
                "left_only": sorted(set(first) - set(second)),
                "right_only": sorted(set(second) - set(first)),
            }
        )
    for seed in sorted(set(first) & set(second)):
        for field in _COMPARED_FIELDS:
            if first[seed][field] != second[seed][field]:
                mismatches.append({"kind": field, "seed": seed})
    for seed, digest in sorted((expected_semantic or {}).items()):
        for name, games in (("left", first), ("right", second)):
            actual = games.get(seed, {}).get("semantic_sha256")
            if actual != digest:
                mismatches.append(
                    {"kind": "expected-semantic", "seed": seed, "side": name}
                )
    return {
        "left": str(left),
        "right": str(right),
        "seeds": sorted(set(first) | set(second)),
        "mismatches": mismatches,
        "ok": not mismatches,
    }
