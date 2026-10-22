"""#522 混成focal比較のtest用builder・fake play。

実Policy・Rust wheelを使わない。半荘は#513のfixture（実engineの ``MatchState`` を
手計算済みscriptで進める）、Policy側の観測は合成値である。``fake_*`` はspawn workerへ
渡すため、module-levelの関数として置く。Kは32の代わりに3へ差し替える。

```text
script X final_points（seat 0..3）  -140   510  -440    70    base  -40   10  -140  -30
script Y final_points              820    50  -150  -720    base  220  -50   -50 -320
                                   （Yは seat 3 = N が8局すべて放銃、Xは seat 2 = W が2回）

条件Bは全rotationでscript Y。条件Aは、seedごとに1つのrotationだけscript X:
  SEED_1: r = 0   F_A - F_B = -140 - 820 = -960   D = -24.0
  SEED_2: r = 1             510 -  50 = +460   D = +11.5
  SEED_3: r = 3              70 + 720 = +790   D = +19.75
fake_focal_reconfirmed は SEED_1 もYだけ（D = 0）
```

focal（seat r）とO1（seat r+1）は条件Bで同じidentityなので、identityでfocalを
選ぶと値が変わる。手計算値はseat rだけを数えた値である。
"""

import os

from _engine_aabb_paijia_tie_fixtures import CHANGE_PAIRS, analysis
from _engine_aabb_protocol_fixtures import (
    ARENA_REVISION,
    ENVIRONMENT,
    LISJONG_REVISION,
    WHEEL_PATH,
)
from _engine_aabb_record_fixtures import SCRIPT_X, SCRIPT_Y, completions_for, play

from lisjong_arena import seed_registry
from lisjong_arena.heuristic_candidate_engine_aabb import (
    focal_mixed_plan as plan_module,
)
from lisjong_arena.heuristic_candidate_engine_aabb.executor import PlayedHanchan
from lisjong_arena.heuristic_candidate_engine_aabb.kyoku_record import SeatAssignment
from lisjong_arena.heuristic_candidate_engine_aabb.protocol import (
    SEED_DOMAIN,
    parse_participant,
)

SEEDS = (522_001, 522_002, 522_003)
X_ROTATION = {SEEDS[0]: 0, SEEDS[1]: 1, SEEDS[2]: 3}
X_POINTS = (-140, 510, -440, 70)
Y_POINTS = (820, 50, -150, -720)
X_BASE = (-40, 10, -140, -30)
Y_BASE = (220, -50, -50, -320)
COUNTS = (2, 3, 4, 5)
"""seat index順のPolicy呼び出し数（合成値）。"""
LIMITS = {
    "enforcement": "synthetic test value",
    "process_tree_rss_bytes": 4 * 1024**3,
    "wall_seconds": 6 * 3600,
}

__all__ = ["ARENA_REVISION", "ENVIRONMENT", "LISJONG_REVISION", "WHEEL_PATH"]


def allocation(
    seeds=SEEDS,
    *,
    owner=plan_module.OWNER_ISSUE,
    population=plan_module.POPULATION,
    split="DEVELOPMENT",
    protocol=plan_module.PROTOCOL_ID,
    domain=SEED_DOMAIN,
):
    return seed_registry.build_allocation_record(
        owner_issue=owner,
        protocol=protocol,
        seed_domain=domain,
        purpose="test",
        population=population,
        split=split,
        seeds=seeds,
        arena_revision="e" * 40,
        protocol_revision="test",
        provenance_reference="test",
        allocation_timestamp="2026-10-22T00:00:00Z",
    )


RECORD = allocation()


def ledger(*records):
    document = seed_registry.new_ledger()
    document["allocations"] = list(records or (RECORD,))
    document["allocations"].sort(key=seed_registry._sort_key)
    return seed_registry.validate_ledger(document)


def binding(document, record=RECORD):
    return seed_registry.allocation_binding(document, record["allocation_identity"])


def plan_document(tmp, *, run_kind="allocated", **overrides):
    document = overrides.pop("ledger", None) or ledger()
    record = overrides.pop("record", RECORD)
    allocated = run_kind == "allocated"
    arguments = {
        "run_kind": run_kind,
        "ordered_seeds": SEEDS if allocated else plan_module.REHEARSAL_SEEDS,
        "allocation_binding": binding(document, record) if allocated else None,
        "timeout_seconds": 60,
        "resource_limits": LIMITS,
        "wheel_path": WHEEL_PATH,
        "destinations": {
            "diagnostics": str(tmp / f"{run_kind}-diagnostics"),
            "output": str(tmp / f"{run_kind}-output"),
        },
        "environment": ENVIRONMENT,
        "arena_revision": ARENA_REVISION,
        "ledger": document,
    }
    arguments.update(overrides)
    return plan_module.assemble_plan(**arguments)


def gate(plan, *, ledger):
    """live値をfixtureの値として照合する（実行と同じ関数）。"""
    plan_module.require_live_target(
        plan, ledger=ledger, environment=ENVIRONMENT, head=ARENA_REVISION
    )


# ---------------------------------------------------------------------------
# fake play（spawn worker内）
# ---------------------------------------------------------------------------


def _uses_x(task, x_rotation):
    return task.condition == "A" and x_rotation.get(task.seed) == task.rotation


def _seats(task):
    seats = []
    for seat, role in enumerate(plan_module.seat_roles(task.rotation)):
        count = COUNTS[seat]
        entry = {"decision_seconds": [0.01] * count, "role": role, "seat": seat}
        if role == "focal" and task.condition == "A":
            entry["stage_counts"] = {"NO_ANALYSIS": count - 1, "PAIJIA_SWITCH": 1}
            entry["switches"] = [
                {
                    "analysis": analysis(
                        "PAIJIA_SWITCH", *CHANGE_PAIRS["tile-type-change"]
                    ),
                    "kind": "tile-type-change",
                }
            ]
        seats.append(entry)
    return {"seats": seats}


def _capture(task, *, x_rotation=X_ROTATION, evidence=True):
    script_x = _uses_x(task, x_rotation)
    match = play(task.seed, SCRIPT_X if script_x else SCRIPT_Y)
    participants = {
        role: parse_participant(document, role)
        for role, document in task.participants.items()
    }
    seats = tuple(
        SeatAssignment(
            role,
            participants[task.condition if role == "focal" else role].policy_identity,
        )
        for role in plan_module.seat_roles(task.rotation)
    )
    return plan_module.FocalHanchanCapture(
        match=match,
        seed=task.seed,
        rotation=task.rotation,
        condition=task.condition,
        seats=seats,
        selector_call_counts=COUNTS,
        policy_call_counts=COUNTS,
        round_evidence=completions_for(match, script_x=script_x) if evidence else None,
    )


def fake_focal(task):
    return PlayedHanchan(
        _capture(task), ENVIRONMENT, os.getpid(), 4096, None, _seats(task)
    )


def fake_focal_reconfirmed(task):
    x_rotation = {seed: r for seed, r in X_ROTATION.items() if seed != SEEDS[0]}
    return PlayedHanchan(
        _capture(task, x_rotation=x_rotation),
        ENVIRONMENT,
        os.getpid(),
        4096,
        None,
        _seats(task),
    )


def fake_focal_policy_error(task):
    if (task.seed, task.rotation, task.condition) == (SEEDS[1], 2, "B"):
        raise RuntimeError("synthetic Policy failure")
    return fake_focal(task)


def fake_focal_bad_analysis(task):
    played = fake_focal(task)
    if (task.seed, task.rotation, task.condition) == (SEEDS[2], 1, "A"):
        played.policy_analysis["seats"][0]["decision_seconds"].append(0.01)
    return played


def fake_focal_missing_evidence(task):
    if (task.seed, task.rotation, task.condition) == (SEEDS[0], 3, "B"):
        return PlayedHanchan(
            _capture(task, evidence=False),
            ENVIRONMENT,
            os.getpid(),
            4096,
            None,
            _seats(task),
        )
    return fake_focal(task)


def fake_focal_swapped_seats(task):
    """focalとO1のroleを入れ替えた記録（identityは同じでもroleで拒否される）。"""
    played = fake_focal(task)
    if (task.seed, task.rotation, task.condition) != (SEEDS[0], 0, "B"):
        return played
    capture = played.capture
    seats = list(capture.seats)
    seats[0], seats[1] = seats[1], seats[0]
    swapped = plan_module.FocalHanchanCapture(
        match=capture.match,
        seed=capture.seed,
        rotation=capture.rotation,
        condition=capture.condition,
        seats=tuple(seats),
        selector_call_counts=capture.selector_call_counts,
        policy_call_counts=capture.policy_call_counts,
        round_evidence=capture.round_evidence,
    )
    return PlayedHanchan(swapped, ENVIRONMENT, os.getpid(), 4096, None, _seats(task))
