"""Frozen #423 Rust execution; historical #375 execution remains unchanged.

Each spawned worker verifies its loaded native files before executing a seed's
four AABB games. The parent receives evidence with every completed seed block;
no partial comparison is returned on any worker or identity failure.
"""

from __future__ import annotations

import multiprocessing
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import PurePosixPath, PureWindowsPath

from lisjong_arena._artifact_io import expect_int, expect_object
from lisjong_arena.comparison import aggregate_policy_metrics, run_comparison
from lisjong_arena.model import ComparisonPlan, ComparisonResult
from lisjong_arena.shanten_backend_verification.backend import (
    native_call_count,
    require_shanten_backend,
    verify_installed_native,
)
from lisjong_arena.single_round_artifact import (
    collect_execution_provenance,
    execution_provenance_to_dict,
    parse_execution_provenance,
)

from .lock import HeuristicCandidateLockError, document_identity

REVISION = "58ef82aeb10ac77cb66290d54e67a42426919d5b"
WHEEL_SHA256 = "a4480991f04bc2686c6790857fdbf467cd576aa9a910e05e344232a0c4a8086c"
WHEEL_FILENAME = "lisjong_native-0.1.0-cp314-cp314-manylinux_2_28_x86_64.whl"
PAIR = (
    (
        "placement-aware-speed-call-kobalab-0004-belief-paijia",
        "create_placement_aware_speed_call_kobalab_0004_belief_paijia",
    ),
    ("placement-aware-speed-call", "create_placement_aware_speed_call"),
)


def require_pair(candidate, incumbent) -> None:
    for participant, (identity, factory) in zip(
        (candidate, incumbent), PAIR, strict=True
    ):
        if (
            participant.policy_identity,
            participant.factory_binding,
            participant.implementation_source,
            participant.implementation_revision,
        ) != (identity, f"lisjong_arena.policy_catalog:{factory}", "lisjong", REVISION):
            raise HeuristicCandidateLockError(
                "event 423 participant differs from the frozen candidate/Champion pair"
            )


def execution_contract(wheel_path: str) -> dict[str, object]:
    return {
        "backend": "rust",
        "lisjong_revision": REVISION,
        "native_api_version": 2,
        "source_revision": REVISION,
        "wheel_filename": WHEEL_FILENAME,
        "wheel_sha256": WHEEL_SHA256,
        "wheel_path": wheel_path,
        "candidate_configuration": "no arguments; default conditional-uniform belief-paijia",
        "executor": "spawn-seed-block-rust-423-v1",
    }


def require_contract(value: object) -> dict[str, object]:
    raw = expect_object(value, set(execution_contract("")), "rust_execution")
    # This is a recorded execution path, not a path on the verifying host.
    # Keep its bytes unchanged so the frozen contract/identity still matches.
    path = raw["wheel_path"]
    if not isinstance(path, str) or not any(
        parsed.is_absolute() and parsed.name == WHEEL_FILENAME
        for parsed in (PurePosixPath(path), PureWindowsPath(path))
    ):
        raise HeuristicCandidateLockError(
            "event 423 requires an absolute frozen wheel path"
        )
    if raw != execution_contract(path):
        raise HeuristicCandidateLockError("event 423 Rust execution contract drifted")
    return dict(raw)


def require_provenance(value: object) -> None:
    provenance = parse_execution_provenance(value)
    if (
        provenance.lisjong_revision,
        provenance.lisjong_engine_revision,
        provenance.riichienv_version,
    ) != (REVISION, "8735e89e1aea000ab59368d0368d476787827741", "0.4.10"):
        raise HeuristicCandidateLockError("event 423 dependency provenance drifted")


def _rows_identity(rows) -> str:
    return document_identity(
        {
            "rows": [
                [
                    row.seed,
                    row.rotation,
                    int(row.seat),
                    row.policy_identity,
                    row.game_mode,
                    row.score,
                    row.rank,
                ]
                for row in rows
            ]
        }
    )


def verify_process(wheel_path: str) -> dict[str, object]:
    backend = require_shanten_backend("rust", expected_revision=REVISION)
    wheel = verify_installed_native(wheel_path)
    if wheel["sha256"] != WHEEL_SHA256 or wheel["file"] != WHEEL_FILENAME:
        raise HeuristicCandidateLockError("event 423 wheel identity drifted")
    record = {
        "backend": backend,
        "wheel": wheel,
        "provenance": execution_provenance_to_dict(collect_execution_provenance()),
    }
    _require_process(record)
    return record


def _require_process(value: object) -> dict[str, object]:
    raw = expect_object(value, {"backend", "wheel", "provenance"}, "process")
    require_provenance(raw["provenance"])
    backend = expect_object(
        raw["backend"], {"backend", "lisjong_revision", "pid", "native"}, "backend"
    )
    native = expect_object(
        backend["native"],
        {"module_file", "source_revision", "api_version", "probe_native_calls"},
        "native",
    )
    wheel = expect_object(
        raw["wheel"],
        {"file", "sha256", "bytes", "installed_directory", "files"},
        "wheel",
    )
    if (
        backend["backend"],
        backend["lisjong_revision"],
        native["source_revision"],
        native["api_version"],
        wheel["file"],
        wheel["sha256"],
    ) != ("rust", REVISION, REVISION, 2, WHEEL_FILENAME, WHEEL_SHA256):
        raise HeuristicCandidateLockError("Rust process identity mismatch")
    for name, value in (
        ("pid", backend["pid"]),
        ("probe_native_calls", native["probe_native_calls"]),
        ("bytes", wheel["bytes"]),
    ):
        if expect_int(value, name) <= 0:
            raise HeuristicCandidateLockError(f"{name} must be positive")
    if not isinstance(wheel["files"], list) or not wheel["files"]:
        raise HeuristicCandidateLockError("installed wheel file evidence is missing")
    for path in (native["module_file"], wheel["installed_directory"], *wheel["files"]):
        if not isinstance(path, str) or not path:
            raise HeuristicCandidateLockError("native file path evidence is missing")
    return dict(raw)


def event_module(event):
    """Resolve only the two reviewed, frozen event contracts."""
    if type(event) is not int:
        raise HeuristicCandidateLockError("unsupported Rust evaluation event")
    if event == 423:
        return sys.modules[__name__]
    if event == 436:
        from . import rust436

        return rust436
    raise HeuristicCandidateLockError("unsupported Rust evaluation event")


def _run_block(plan: ComparisonPlan, wheel_path: str, provenance: object, event=423):
    try:
        return _execute_block(plan, wheel_path, provenance, event=event)
    except Exception as exc:
        # Policy/game exceptions need not be picklable across spawn.
        raise HeuristicCandidateLockError(
            f"seed={plan.seeds[0]} worker failed: {type(exc).__name__}: {exc}"
        ) from None


def _execute_block(
    plan: ComparisonPlan, wheel_path: str, provenance: object, *, event=423
):
    api = event_module(event)
    before = api.verify_process(wheel_path)
    if before["provenance"] != provenance:
        raise HeuristicCandidateLockError("worker provenance differs from lock")
    calls = native_call_count()
    result = run_comparison(plan)
    game_calls = native_call_count() - calls
    after = api.verify_process(wheel_path)
    if before != after:
        raise HeuristicCandidateLockError(
            "worker native identity changed during execution"
        )
    return result, {
        "seed": plan.seeds[0],
        "process": before,
        "game_native_calls": game_calls,
        "rows_identity": _rows_identity(result.seat_results),
    }


def require_evidence(
    lock: dict[str, object], value: object, rows, *, event=423
) -> dict[str, object]:
    try:
        return _require_evidence(lock, value, rows, event=event)
    except (ValueError, TypeError, KeyError) as exc:
        raise HeuristicCandidateLockError(str(exc)) from exc


def _require_evidence(
    lock: dict[str, object], value: object, rows, *, event=423
) -> dict[str, object]:
    api = event_module(event)
    contract = api.require_contract(lock["rust_execution"])
    raw = expect_object(value, {"contract", "parent", "blocks"}, "rust_evidence")
    if raw["contract"] != contract:
        raise HeuristicCandidateLockError("Rust evidence differs from the lock")
    parent = api._require_process(raw["parent"])
    if parent["provenance"] != lock["provenance"]:
        raise HeuristicCandidateLockError("parent provenance differs from lock")
    blocks = raw["blocks"]
    seeds = lock["protocol"]["ordered_seeds"]
    if not isinstance(blocks, list) or len(blocks) != len(seeds):
        raise HeuristicCandidateLockError(
            "Rust evidence must cover every locked seed block"
        )
    pids = set()
    for seed, value in zip(seeds, blocks, strict=True):
        block = expect_object(
            value, {"seed", "process", "game_native_calls", "rows_identity"}, "block"
        )
        if expect_int(block["seed"], "seed") != seed:
            raise HeuristicCandidateLockError("Rust evidence seed order mismatch")
        process = api._require_process(block["process"])
        if process["provenance"] != lock["provenance"]:
            raise HeuristicCandidateLockError("worker provenance differs from lock")
        if block["rows_identity"] != _rows_identity(
            row for row in rows if row.seed == seed
        ):
            raise HeuristicCandidateLockError(
                "worker evidence differs from comparison rows"
            )
        pid = process["backend"]["pid"]
        if pid == parent["backend"]["pid"]:
            raise HeuristicCandidateLockError(
                "Rust event requires actual spawned worker evidence"
            )
        pids.add(pid)
        if expect_int(block["game_native_calls"], "game_native_calls") <= 0:
            raise HeuristicCandidateLockError(
                "worker games did not reach native shanten"
            )
    if len(pids) > lock["max_workers"]:
        raise HeuristicCandidateLockError("worker evidence exceeds locked worker count")
    return dict(raw)


def execute_rust(
    plan: ComparisonPlan, *, lock: dict[str, object], progress_callback=None, event=423
):
    api = event_module(event)
    contract = api.require_contract(lock["rust_execution"])
    parent = api.verify_process(contract["wheel_path"])
    if parent["provenance"] != lock["provenance"]:
        raise HeuristicCandidateLockError("parent provenance differs from lock")
    completed = {}
    with ProcessPoolExecutor(
        max_workers=lock["max_workers"], mp_context=multiprocessing.get_context("spawn")
    ) as pool:
        futures = {
            pool.submit(
                _run_block,
                ComparisonPlan(
                    policy_a=plan.policy_a,
                    policy_b=plan.policy_b,
                    seeds=(seed,),
                    game_mode=plan.game_mode,
                    max_steps=plan.max_steps,
                ),
                contract["wheel_path"],
                lock["provenance"],
                *((event,) if event != 423 else ()),
            ): seed
            for seed in plan.seeds
        }
        try:
            for future in as_completed(futures):
                seed = futures[future]
                result, evidence = future.result()
                if result.plan.seeds != (seed,) or evidence["seed"] != seed:
                    raise HeuristicCandidateLockError(
                        "worker returned a different seed"
                    )
                completed[seed] = result, evidence
                if progress_callback is not None:
                    progress_callback(len(completed) * 4, len(plan.seeds) * 4)
        except BaseException:
            for future in futures:
                future.cancel()
            pool.terminate_workers()
            raise
    if api.verify_process(contract["wheel_path"]) != parent:
        raise HeuristicCandidateLockError(
            "parent native identity changed during execution"
        )
    rows = tuple(row for seed in plan.seeds for row in completed[seed][0].seat_results)
    evidence = require_evidence(
        lock,
        {
            "contract": contract,
            "parent": parent,
            "blocks": [completed[seed][1] for seed in plan.seeds],
        },
        rows,
        event=event,
    )
    return ComparisonResult(
        plan=plan,
        seat_results=rows,
        metrics_a=aggregate_policy_metrics(plan.policy_a.identity, rows),
        metrics_b=aggregate_policy_metrics(plan.policy_b.identity, rows),
    ), evidence
