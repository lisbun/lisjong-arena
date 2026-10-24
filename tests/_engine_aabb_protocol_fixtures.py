"""#514 engine版AABB protocol / lock / runnerのtest用builder。

実engine・実Policy・Rust wheelを使わずに、lock・実行・result・verifyの契約を試す。
``fake_play*`` はspawn workerへ渡すため、module-levelの関数として置く。半荘の中身は
#513のfixture（実engineの``MatchState``で東1局から進めた手計算済みscript）である。

```text
seed s のblock
  placement 0      script X（10局、TARGET_REACHED）
  placement 1..5   script Y（8局、BANKRUPTCY）          ← SEED_A
  全配置           script Y                              ← SEED_B
S_A(SEED_A) = -500、S_B = +500、差 -1000 -> D = -1000 / 120
S_A(SEED_B) = 0                         -> D = 0
```
"""

import os
import time

from _engine_aabb_record_fixtures import SCRIPT_X, SCRIPT_Y, completions_for, play

from lisjong_arena import seed_registry
from lisjong_arena.heuristic_candidate_engine_aabb.development import (
    catalog_participant,
)
from lisjong_arena.heuristic_candidate_engine_aabb.executor import PlayedHanchan
from lisjong_arena.heuristic_candidate_engine_aabb.kyoku_record import SeatAssignment
from lisjong_arena.heuristic_candidate_engine_aabb.lock import assemble_lock_document
from lisjong_arena.heuristic_candidate_engine_aabb.protocol import (
    ENGINE_REVISION,
    FORMAL_SPLIT,
    POPULATION_PREFIX,
    SEED_DOMAIN,
    parse_participant,
)
from lisjong_arena.heuristic_candidate_engine_aabb.record_store import HanchanCapture
from lisjong_arena.lisjong_engine.errors import UnmappedActionError

LISJONG_REVISION = "b" * 40
ARENA_REVISION = "c" * 40
EVENT = 900
SEED_A = 514_001
SEED_B = 514_002
SEEDS = (SEED_A, SEED_B)
TIMEOUT_SECONDS = 60
WHEEL_PATH = os.path.abspath("lisjong_native-test.whl")
CANDIDATE = catalog_participant(
    "one-shanten-defense-placement-aware-speed-call", LISJONG_REVISION
)
INCUMBENT = catalog_participant("placement-aware-speed-call", LISJONG_REVISION)
K_RATIONALE = {
    "cost_estimate": "synthetic test value",
    "power": "synthetic test value",
    "sd_estimate": "synthetic test value",
    "target_effect": "synthetic test value",
}
ENVIRONMENT = {
    "native": {
        "api_version": 3,
        "source_revision": LISJONG_REVISION,
        "wheel_bytes": 1,
        "wheel_file": "lisjong_native-test.whl",
        "wheel_sha256": "d" * 64,
    },
    "provenance": {
        "lisjong_arena_revision": ARENA_REVISION,
        "lisjong_engine_revision": ENGINE_REVISION,
        "lisjong_revision": LISJONG_REVISION,
    },
    "python": {"implementation": "CPython", "version": "3.14.7"},
    "shanten_backend": {"backend": "rust", "lisjong_revision": LISJONG_REVISION},
}


def _allocation(*, event=EVENT, seeds=SEEDS, domain=SEED_DOMAIN, split=FORMAL_SPLIT):
    return seed_registry.build_allocation_record(
        owner_issue=f"lisbun/lisjong-arena#{event}",
        protocol="arena-heuristic-candidate-engine-aabb-half-v1",
        seed_domain=domain,
        purpose="test",
        population=f"{POPULATION_PREFIX}{event}",
        split=split,
        seeds=seeds,
        arena_revision="e" * 40,
        protocol_revision="test",
        provenance_reference="test",
        allocation_timestamp="2026-10-22T00:00:00Z",
    )


def ledger(*extra):
    document = seed_registry.new_ledger()
    document["allocations"] = [_allocation(), *extra]
    document["allocations"].sort(key=seed_registry._sort_key)
    return seed_registry.validate_ledger(document)


def allocation_binding(document=None):
    document = ledger() if document is None else document
    record = next(
        item
        for item in document["allocations"]
        if item["owner_issue"] == f"lisbun/lisjong-arena#{EVENT}"
    )
    return seed_registry.allocation_binding(document, record["allocation_identity"])


def lock_document(tmp, **overrides):
    document = overrides.pop("ledger", None) or ledger()
    arguments = {
        "event": EVENT,
        "candidate": CANDIDATE,
        "incumbent": INCUMBENT,
        "incumbent_role": "current-champion",
        "ordered_seeds": SEEDS,
        "k": len(SEEDS),
        "allocation_binding": allocation_binding(document),
        "k_rationale": K_RATIONALE,
        "timeout_seconds": TIMEOUT_SECONDS,
        "wheel_path": WHEEL_PATH,
        "destinations": {
            "diagnostics": str(tmp / "diagnostics"),
            "raw": str(tmp / "raw"),
            "result": str(tmp / "result.json"),
        },
        "environment": ENVIRONMENT,
        "arena_revision": ARENA_REVISION,
        "ledger": document,
    }
    arguments.update(overrides)
    return assemble_lock_document(**arguments)


def gate(lock, *, ledger):
    """live値をfixtureの値として照合する（正式実行のgateと同じ関数）。"""
    from lisjong_arena.heuristic_candidate_engine_aabb.lock import require_live_target

    require_live_target(
        lock, ledger=ledger, environment=ENVIRONMENT, head=ARENA_REVISION
    )


# ---------------------------------------------------------------------------
# fake play（spawn worker内で実行される）
# ---------------------------------------------------------------------------


def _script(seed, placement):
    return SCRIPT_X if (seed == SEED_A and placement == 0) else SCRIPT_Y


def _capture(task, *, evidence=True, counts=(40, 41, 42, 43)):
    match = play(task.seed, _script(task.seed, task.placement))
    candidate = parse_participant(task.candidate, "A")
    incumbent = parse_participant(task.incumbent, "B")
    from lisjong_arena.heuristic_candidate_engine_aabb.kyoku_record import (
        placement_roles,
    )

    seats = tuple(
        SeatAssignment(role, (candidate if role == "A" else incumbent).policy_identity)
        for role in placement_roles(task.placement)
    )
    return HanchanCapture(
        match=match,
        seed=task.seed,
        placement=task.placement,
        seats=seats,
        selector_call_counts=counts,
        policy_call_counts=counts,
        round_evidence=completions_for(
            match, script_x=_script(task.seed, task.placement) is SCRIPT_X
        )
        if evidence
        else None,
    )


def fake_play(task):
    return PlayedHanchan(_capture(task), ENVIRONMENT, os.getpid(), 1024)


def fake_play_policy_error(task):
    if (task.seed, task.placement) == (SEED_B, 2):
        raise RuntimeError("synthetic Policy failure")
    return fake_play(task)


def fake_play_mapping_error(task):
    if (task.seed, task.placement) == (SEED_A, 4):
        raise UnmappedActionError("synthetic mapping failure")
    return fake_play(task)


def fake_play_slow(task):
    if (task.seed, task.placement) == (SEED_A, 1):
        time.sleep(30)
    return fake_play(task)


HEARTBEAT_ENV = "ENGINE_AABB_TEST_HEARTBEAT"


def fake_play_heartbeat(task):
    """(SEED_A, 1) で止まらずに、0.2秒ごとにheartbeat fileへ追記し続ける。"""
    if (task.seed, task.placement) == (SEED_A, 1):
        path = os.environ[HEARTBEAT_ENV]
        for _ in range(300):
            with open(path, "a", encoding="utf-8") as stream:
                stream.write(f"{os.getpid()}\n")
            time.sleep(0.2)
    return fake_play(task)


def fake_play_other_environment(task):
    played = fake_play(task)
    if (task.seed, task.placement) == (SEED_B, 5):
        environment = {
            **ENVIRONMENT,
            "python": {"implementation": "CPython", "version": "3.14.8"},
        }
        return PlayedHanchan(played.capture, environment, played.worker_pid, 1)
    return played


def fake_play_count_mismatch(task):
    if (task.seed, task.placement) == (SEED_A, 3):
        capture = _capture(task)
        from dataclasses import replace

        capture = replace(capture, policy_call_counts=(40, 41, 42, 44))
        return PlayedHanchan(capture, ENVIRONMENT, os.getpid(), 1)
    return fake_play(task)


def fake_play_missing_evidence(task):
    if (task.seed, task.placement) == (SEED_B, 0):
        return PlayedHanchan(
            _capture(task, evidence=False), ENVIRONMENT, os.getpid(), 1
        )
    return fake_play(task)


def fake_play_wrong_seed(task):
    played = fake_play(task)
    if (task.seed, task.placement) == (SEED_B, 1):
        from dataclasses import replace

        return PlayedHanchan(
            replace(played.capture, placement=2), ENVIRONMENT, played.worker_pid, 1
        )
    return played


# ---------------------------------------------------------------------------
# #517 calibration / backend check
# ---------------------------------------------------------------------------

PYTHON_ENVIRONMENT = {
    "native": None,
    "provenance": ENVIRONMENT["provenance"],
    "python": ENVIRONMENT["python"],
    "shanten_backend": {"backend": "python", "lisjong_revision": LISJONG_REVISION},
}


def _actions(task, *, drift=False):
    count = 40 + 41 + 42 + 43
    actions = [
        ["EAST", {"__type": "SyntheticAction", "index": n}] for n in range(count)
    ]
    if drift and (task.seed, task.placement) == (472_006, 3):
        actions[7] = ["EAST", {"__type": "SyntheticAction", "index": -1}]
    return tuple(actions)


def fake_play_rust_actions(task):
    return PlayedHanchan(_capture(task), ENVIRONMENT, os.getpid(), 1024, _actions(task))


def fake_play_python_actions(task):
    return PlayedHanchan(
        _capture(task), PYTHON_ENVIRONMENT, os.getpid(), 1024, _actions(task)
    )


def fake_play_python_drift(task):
    return PlayedHanchan(
        _capture(task),
        PYTHON_ENVIRONMENT,
        os.getpid(),
        1024,
        _actions(task, drift=True),
    )


def fake_play_rust_missing_actions(task):
    return PlayedHanchan(
        _capture(task), ENVIRONMENT, os.getpid(), 1024, _actions(task)[1:]
    )


# ---------------------------------------------------------------------------
# #531 Rust-only gate
# ---------------------------------------------------------------------------

GATE_COUNTERS = {
    "discard_evaluations": 30,
    "numeric_shanten": 500,
    "progression_evaluations": 0,
}


def _gate_play(task, counters):
    return PlayedHanchan(
        _capture(task),
        ENVIRONMENT,
        os.getpid(),
        1024,
        _actions(task),
        native_counters=counters,
    )


def fake_play_gate(task):
    return _gate_play(task, dict(GATE_COUNTERS))


def fake_play_gate_counter_drift(task):
    counters = dict(GATE_COUNTERS)
    if (task.seed, task.placement) == (472_005, 2):
        counters["numeric_shanten"] += 1
    return _gate_play(task, counters)


def fake_play_gate_action_drift(task):
    return PlayedHanchan(
        _capture(task),
        ENVIRONMENT,
        os.getpid(),
        1024,
        _actions(task, drift=True),
        native_counters=dict(GATE_COUNTERS),
    )


def fake_play_gate_no_native_call(task):
    return _gate_play(task, {**GATE_COUNTERS, "numeric_shanten": 0})


def fake_play_gate_missing_counters(task):
    return _gate_play(task, None)
