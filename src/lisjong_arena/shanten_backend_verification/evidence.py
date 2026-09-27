"""Completeness and condition checks of one #400 run directory.

``report.evaluate()`` runs these checks before it computes any metric.  Any
problem (a missing or malformed file, a smaller amount of work than planned, a
different backend, Policy, seed set, worker count or input identity) makes the
evidence incomplete, and no adoption decision is produced from it.  A normal
bootstrap run produces complete evidence; these checks protect re-evaluation
of partial, edited or mixed directories.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import plan
from .backend import BACKENDS

DIFFERENTIAL_KEYS = frozenset({"native_tests_exit", "lisjong_suite_rust_exit"})
FAIL_CLOSED_KEYS = frozenset(
    {"python_without_wheel_exit", "rust_without_wheel_exit", "bad_wheel_exit"}
)


class _Evidence:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.problems: list[str] = []

    def problem(self, message: str) -> None:
        self.problems.append(message)

    def load(self, relative: str):
        path = self.root / relative
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self.problem(f"{relative}: missing")
        except (OSError, ValueError) as error:
            self.problem(f"{relative}: unreadable ({error})")
        return None

    def load_lines(self, relative: str) -> list | None:
        path = self.root / relative
        try:
            return [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        except FileNotFoundError:
            self.problem(f"{relative}: missing")
        except (OSError, ValueError) as error:
            self.problem(f"{relative}: unreadable ({error})")
        return None

    def expect(self, relative: str, field: str, actual, expected) -> bool:
        if actual != expected:
            self.problem(f"{relative}: {field} is {actual!r}; expected {expected!r}")
            return False
        return True


def _is_int(value) -> bool:
    return type(value) is int


def _check_exit_codes(evidence: _Evidence, relative: str, keys: frozenset) -> None:
    result = evidence.load(relative)
    if result is None:
        return
    if not isinstance(result, dict) or set(result) != keys:
        evidence.problem(
            f"{relative}: keys are {sorted(result) if isinstance(result, dict) else result!r}; "
            f"expected {sorted(keys)}"
        )
        return
    for key in sorted(keys):
        if not _is_int(result[key]):
            evidence.problem(f"{relative}: {key} is not an exit code")


def _check_distribution(evidence: _Evidence) -> None:
    compilers = evidence.load("environment/compilers.json")
    if compilers is not None and (
        not isinstance(compilers, dict)
        or not isinstance(compilers.get("present"), list)
        or not isinstance(compilers.get("absent"), list)
    ):
        evidence.problem("environment/compilers.json: malformed")
    _check_exit_codes(evidence, "fail-closed/result.json", FAIL_CLOSED_KEYS)
    install = evidence.load("install.json")
    if install is not None:
        wheel = install.get("wheel") if isinstance(install, dict) else None
        if not isinstance(wheel, dict):
            evidence.problem("install.json: wheel identity is missing")
        else:
            evidence.expect(
                "install.json", "wheel.file", wheel.get("file"), plan.WHEEL_FILENAME
            )
            evidence.expect(
                "install.json",
                "wheel.sha256",
                wheel.get("sha256"),
                plan.WHEEL_SHA256,
            )
        if not isinstance(install.get("install_s"), (int, float)):
            evidence.problem("install.json: install_s is missing")
    for backend in BACKENDS:
        relative = f"probe-{backend}.json"
        probe = evidence.load(relative)
        if probe is None:
            continue
        evidence.expect(relative, "backend", probe.get("backend"), backend)
        evidence.expect(
            relative,
            "lisjong_revision",
            probe.get("lisjong_revision"),
            plan.LISJONG_REVISION,
        )
        native = probe.get("native")
        if backend == "python":
            evidence.expect(relative, "native", native, None)
        elif not isinstance(native, dict):
            evidence.problem(f"{relative}: native identity is missing")
        else:
            evidence.expect(
                relative,
                "native.source_revision",
                native.get("source_revision"),
                plan.LISJONG_REVISION,
            )


def _check_games(
    evidence: _Evidence,
    directory: str,
    *,
    policy: plan.PlannedPolicy,
    backend: str,
    workers: int,
    seeds: tuple[int, ...],
) -> None:
    relative = f"{directory}/summary.json"
    summary = evidence.load(relative)
    if summary is not None:
        expected = {
            "measurement": "games",
            "backend": backend,
            "policy": policy.catalog,
            "game_mode": plan.GAME_MODE,
            "workers_requested": workers,
            "workers_observed": workers,
            "games": len(seeds),
            "seeds": list(seeds),
        }
        for field, value in expected.items():
            evidence.expect(relative, field, summary.get(field), value)
        parent = summary.get("parent")
        if not isinstance(parent, dict):
            evidence.problem(f"{relative}: parent backend record is missing")
        else:
            evidence.expect(relative, "parent.backend", parent.get("backend"), backend)
            evidence.expect(
                relative,
                "parent.lisjong_revision",
                parent.get("lisjong_revision"),
                plan.LISJONG_REVISION,
            )
    games_relative = f"{directory}/games.jsonl"
    games = evidence.load_lines(games_relative)
    if games is None:
        return
    evidence.expect(
        games_relative, "seeds", sorted(g.get("seed") for g in games), list(seeds)
    )
    pids = set()
    for game in games:
        worker = game.get("worker")
        where = f"{games_relative} seed {game.get('seed')!r}"
        if not isinstance(worker, dict):
            evidence.problem(f"{where}: worker record is missing")
            continue
        pids.add(worker.get("pid"))
        evidence.expect(where, "worker.backend", worker.get("backend"), backend)
        evidence.expect(
            where,
            "worker.lisjong_revision",
            worker.get("lisjong_revision"),
            plan.LISJONG_REVISION,
        )
        evidence.expect(where, "game_mode", game.get("game_mode"), plan.GAME_MODE)
        if backend == "python":
            evidence.expect(where, "native_calls", game.get("native_calls"), None)
        else:
            native = worker.get("native")
            if not isinstance(native, dict):
                evidence.problem(f"{where}: worker native identity is missing")
            else:
                evidence.expect(
                    where,
                    "worker.native.source_revision",
                    native.get("source_revision"),
                    plan.LISJONG_REVISION,
                )
            calls = game.get("native_calls")
            if not _is_int(calls) or calls < 1:
                evidence.problem(f"{where}: no native calls recorded")
    evidence.expect(games_relative, "distinct worker pids", len(pids), workers)


def _check_compare(
    evidence: _Evidence,
    relative: str,
    *,
    left: str,
    right: str,
    seeds: tuple[int, ...],
    policy: plan.PlannedPolicy,
) -> None:
    result = evidence.load(relative)
    if result is None:
        return
    for field, suffix in (("left", left), ("right", right)):
        value = str(result.get(field, "")).replace("\\", "/")
        if not value.endswith(suffix):
            evidence.problem(f"{relative}: {field} is {value!r}; expected .../{suffix}")
    evidence.expect(relative, "seeds", result.get("seeds"), list(seeds))
    evidence.expect(
        relative,
        "expected_semantic",
        result.get("expected_semantic"),
        {"0": policy.seed0_semantic_sha256},
    )
    if not isinstance(result.get("ok"), bool) or not isinstance(
        result.get("mismatches"), list
    ):
        evidence.problem(f"{relative}: ok / mismatches are malformed")


def _check_replays(evidence: _Evidence) -> None:
    directory = evidence.root / "replay"
    expected_files = {
        f"{policy.label}-{backend}-{index}.json"
        for policy in plan.POLICIES
        for backend in BACKENDS
        for index in plan.replay_indices(backend)
    }
    present = (
        {path.name for path in directory.glob("*.json")}
        if directory.is_dir()
        else set()
    )
    for name in sorted(present - expected_files):
        evidence.problem(f"replay/{name}: not part of the plan")
    for policy in plan.POLICIES:
        for backend in BACKENDS:
            for index in plan.replay_indices(backend):
                relative = f"replay/{policy.label}-{backend}-{index}.json"
                run = evidence.load(relative)
                if run is None:
                    continue
                expected = {
                    "mode": "policy",
                    "policy": policy.replay_class,
                    "decisions": policy.decisions,
                    "decisions_sha256": policy.decisions_sha256,
                    "repeat": plan.REPLAY_REPEAT,
                }
                for field, value in expected.items():
                    evidence.expect(relative, field, run.get(field), value)
                totals = run.get("pass_total_s")
                if (
                    not isinstance(totals, list)
                    or len(totals) != plan.REPLAY_REPEAT
                    or not all(isinstance(t, (int, float)) and t > 0 for t in totals)
                ):
                    evidence.problem(
                        f"{relative}: pass_total_s must hold {plan.REPLAY_REPEAT} "
                        "positive pass time(s)"
                    )
                if not _is_int(run.get("action_mismatches")):
                    evidence.problem(f"{relative}: action_mismatches is missing")
                environment = run.get("environment")
                evidence.expect(
                    relative,
                    "environment.shanten_backend",
                    environment.get("shanten_backend")
                    if isinstance(environment, dict)
                    else None,
                    backend,
                )
                calls = run.get("native_standard_shanten_calls")
                if backend == "python":
                    evidence.expect(
                        relative, "native_standard_shanten_calls", calls, None
                    )
                elif not _is_int(calls) or calls < 1:
                    evidence.problem(f"{relative}: no native calls recorded")


def _check_startup(evidence: _Evidence) -> None:
    startup = evidence.load("startup.json")
    if startup is None:
        return
    evidence.expect(
        "startup.json", "repeat", startup.get("repeat"), plan.STARTUP_REPEAT
    )
    samples = startup.get("samples")
    for backend in BACKENDS:
        count = len(samples.get(backend, [])) if isinstance(samples, dict) else None
        evidence.expect(
            "startup.json", f"{backend} samples", count, plan.STARTUP_REPEAT
        )
    medians = startup.get("import_first_call_ms")
    for backend in BACKENDS:
        value = medians.get(backend) if isinstance(medians, dict) else None
        if not isinstance(value, dict) or not isinstance(
            value.get("median"), (int, float)
        ):
            evidence.problem(f"startup.json: {backend} median is missing")


def validate_evidence(run_directory: str | Path) -> list[str]:
    """Return every completeness / condition problem; empty means complete."""
    evidence = _Evidence(Path(run_directory))
    _check_distribution(evidence)
    _check_exit_codes(evidence, "differential/result.json", DIFFERENTIAL_KEYS)
    for policy in plan.POLICIES:
        for backend in BACKENDS:
            _check_games(
                evidence,
                f"games-single/{policy.label}-{backend}",
                policy=policy,
                backend=backend,
                workers=1,
                seeds=plan.SINGLE_SEEDS,
            )
        _check_compare(
            evidence,
            f"games-single/compare-{policy.label}.json",
            left=f"games-single/{policy.label}-python",
            right=f"games-single/{policy.label}-rust",
            seeds=plan.SINGLE_SEEDS,
            policy=policy,
        )
    _check_startup(evidence)
    _check_replays(evidence)
    for backend in BACKENDS:
        _check_games(
            evidence,
            f"games-multi/champion-{backend}",
            policy=plan.CHAMPION,
            backend=backend,
            workers=plan.MULTI_WORKERS,
            seeds=plan.MULTI_SEEDS,
        )
    _check_compare(
        evidence,
        "games-multi/compare.json",
        left="games-multi/champion-python",
        right="games-multi/champion-rust",
        seeds=plan.MULTI_SEEDS,
        policy=plan.CHAMPION,
    )
    return evidence.problems
