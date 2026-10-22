"""#520 同点数牌paijia候補の開発比較のtest用builder・fake play。

実engine・実Policy・Rust wheelを使わない。半荘は#513のfixture（実engineの
``MatchState`` を手計算済みscriptで進める）で、Policy側の観測は合成した行である。
``fake_*`` はspawn workerへ渡すため、module-levelの関数として置く。

```text
段階1（8 seeds、4席B）     各seatの判断3件。fake_activation_proceed は各seat 1件の変更
                           -> 8 seeds × 4 seats = 32件（下限ちょうど）
段階2（K=3にpatch）         A席の判断は2件（1件PAIJIA_SWITCH）、B席は判断時間だけ
```
"""

import os

from _engine_aabb_protocol_fixtures import (
    ARENA_REVISION,
    ENVIRONMENT,
    LISJONG_REVISION,
    WHEEL_PATH,
)
from _engine_aabb_protocol_fixtures import _capture as aabb_capture
from _engine_aabb_record_fixtures import SCRIPT_Y, play

from lisjong_arena import seed_registry
from lisjong_arena.heuristic_candidate_engine_aabb import paijia_tie_plan as plan_module
from lisjong_arena.heuristic_candidate_engine_aabb.executor import PlayedHanchan
from lisjong_arena.heuristic_candidate_engine_aabb.kyoku_record import (
    SeatAssignment,
    placement_roles,
)
from lisjong_arena.heuristic_candidate_engine_aabb.protocol import SEED_DOMAIN
from lisjong_arena.heuristic_candidate_engine_aabb.record_store import HanchanCapture

ACTIVATION_SEEDS = tuple(range(520_000, 520_008))
SCREEN_SEEDS = (514_001, 514_002, 514_003)
LIMITS = {
    "enforcement": "synthetic test value",
    "process_tree_rss_bytes": 4 * 1024**3,
    "wall_seconds": 4 * 3600,
}
SCREEN_LIMITS = {**LIMITS, "enforcement": "synthetic screen value"}
DECISIONS_PER_SEAT = 3

__all__ = ["ARENA_REVISION", "ENVIRONMENT", "LISJONG_REVISION", "WHEEL_PATH"]


def allocation(stage, seeds, *, split="DEVELOPMENT", owner=None, population=None):
    return seed_registry.build_allocation_record(
        owner_issue=owner or plan_module.OWNER_ISSUE,
        protocol="arena-heuristic-candidate-engine-aabb-half-v1",
        seed_domain=SEED_DOMAIN,
        purpose="test",
        population=population or plan_module.POPULATIONS[stage],
        split=split,
        seeds=seeds,
        arena_revision="e" * 40,
        protocol_revision="test",
        provenance_reference="test",
        allocation_timestamp="2026-10-22T00:00:00Z",
    )


ACTIVATION_RECORD = allocation("activation", ACTIVATION_SEEDS)
SCREEN_RECORD = allocation("screen", SCREEN_SEEDS)


def ledger(*records):
    document = seed_registry.new_ledger()
    document["allocations"] = list(records or (ACTIVATION_RECORD, SCREEN_RECORD))
    document["allocations"].sort(key=seed_registry._sort_key)
    return seed_registry.validate_ledger(document)


def binding(document, record):
    return seed_registry.allocation_binding(document, record["allocation_identity"])


def plan_document(tmp, *, stage="activation", run_kind="allocated", **overrides):
    document = overrides.pop("ledger", None) or ledger()
    record = overrides.pop(
        "record", ACTIVATION_RECORD if stage == "activation" else SCREEN_RECORD
    )
    seeds = ACTIVATION_SEEDS if stage == "activation" else SCREEN_SEEDS
    arguments = {
        "stage": stage,
        "run_kind": run_kind,
        "ordered_seeds": seeds
        if run_kind == "allocated"
        else plan_module.REHEARSAL_SEEDS,
        "allocation_binding": binding(document, record)
        if run_kind == "allocated"
        else None,
        "timeout_seconds": 60,
        "resource_limits": SCREEN_LIMITS if stage == "screen" else LIMITS,
        "screen_limits": SCREEN_LIMITS if stage == "activation" else None,
        "prerequisite": {
            "activation_plan_digest": "1" * 64,
            "activation_summary_digest": "2" * 64,
            "screen_limits": SCREEN_LIMITS,
        }
        if stage == "screen" and run_kind == "allocated"
        else None,
        "wheel_path": WHEEL_PATH,
        "destinations": {
            "diagnostics": str(tmp / f"{stage}-{run_kind}-diagnostics"),
            "output": str(tmp / f"{stage}-{run_kind}-output"),
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
# 段階1のfake play（spawn worker内）
# ---------------------------------------------------------------------------


def discard(category="MANZU", rank=5, *, red=False, tsumogiri=False):
    """``canonical_engine_value(DiscardAction)`` と同じ形の正規化済み打牌。"""
    return {
        "__type": "DiscardAction",
        "actor": "Seat.EAST",
        "tile": {
            "__type": "Tile",
            "is_red": red,
            "tile_type": {
                "__type": "TileType",
                "category": f"TileCategory.{category}",
                "rank": rank,
            },
        },
        "tsumogiri": tsumogiri,
    }


CHANGE_PAIRS = {
    "tile-type-change": (discard(), discard("PINZU", 9)),
    "red-only-change": (discard(), discard(red=True)),
    "tsumogiri-only": (discard(tsumogiri=True), discard()),
}


def analysis(stage, champion, selected):
    """``canonical_engine_value(SuitedPaijiaTieAnalysis)`` と同じ形。"""
    evaluations = []
    if stage in ("PAIJIA_SWITCH", "CHAMPION_MINIMUM_PAIJIA"):
        other = discard("SOUZU", 1)
        values = (
            ((champion, 9), (selected, 3), (other, 5))
            if stage == "PAIJIA_SWITCH"
            else ((champion, 3), (other, 5))
        )
        evaluations = [
            {"__type": "SuitedPaijiaCandidateEvaluation", "action": a, "paijia": p}
            for a, p in values
        ]
    return {
        "__type": "SuitedPaijiaTieAnalysis",
        "action_changed": champion != selected,
        "champion_action": champion,
        "paijia_evaluations": evaluations,
        "selected_action": selected,
        "stage": f"SuitedPaijiaTieStage.{stage}",
    }


def _row(outcome="same", stage="NO_ANALYSIS"):
    row = {"a_seconds": 2e-4, "b_seconds": 1e-4, "outcome": outcome, "stage": stage}
    if outcome in CHANGE_PAIRS:
        incumbent, candidate = CHANGE_PAIRS[outcome]
        detail = analysis(stage, incumbent, candidate), candidate, incumbent, []
    elif outcome == "same" and stage in ("PAIJIA_SWITCH", "CHAMPION_MINIMUM_PAIJIA"):
        action = discard()
        detail = analysis(stage, action, action), action, action, []
    elif outcome != "same":
        detail = None, discard("PINZU", 9), discard(), ["synthetic reason"]
    else:
        return row
    row["detail"] = dict(
        zip(
            ("analysis", "candidate_action", "incumbent_action", "violations"),
            detail,
            strict=True,
        )
    )
    return row


def _seat_rows(seed, seat, plan):
    """``plan(seed, seat)`` が返すoutcome（変更の種類）を1件目に置き、残りは同一。"""
    outcome = plan(seed, seat)
    first = (
        _row(outcome, "PAIJIA_SWITCH")
        if outcome in ("tile-type-change", "red-only-change", "tsumogiri-only")
        else _row(outcome, "NO_ANALYSIS")
        if outcome != "same"
        else _row("same", "CHAMPION_MINIMUM_PAIJIA")
    )
    return [first, _row(), _row("same", "NO_FINAL_TIE")]


def _activation(task, plan):
    match = play(task.seed, SCRIPT_Y)
    counts = (DECISIONS_PER_SEAT,) * 4
    capture = HanchanCapture(
        match=match,
        seed=task.seed,
        placement=task.placement,
        seats=(SeatAssignment("B", "placement-aware-speed-call"),) * 4,
        selector_call_counts=counts,
        policy_call_counts=counts,
        round_evidence=None,
    )
    analysis = {
        "seats": [
            {"decisions": _seat_rows(task.seed, seat, plan), "seat": seat}
            for seat in range(4)
        ]
    }
    return PlayedHanchan(capture, ENVIRONMENT, os.getpid(), 2048, None, analysis)


def _one_change_per_seat(seed, seat):
    return "tile-type-change" if seat % 2 == 0 else "red-only-change"


def fake_activation_proceed(task):
    return _activation(task, _one_change_per_seat)


def fake_activation_low(task):
    def plan(seed, seat):
        if seed == ACTIVATION_SEEDS[-1] and seat == 3:
            return "tsumogiri-only"  # 変更に数えない -> 31件
        return _one_change_per_seat(seed, seat)

    return _activation(task, plan)


def fake_activation_concentrated(task):
    def plan(seed, seat):
        return "tile-type-change" if seed in ACTIVATION_SEEDS[:3] else "same"

    return _activation(task, plan)


def _concentrated_rows(task):
    played = fake_activation_concentrated(task)
    if task.seed not in ACTIVATION_SEEDS[:3]:
        return played
    seats = played.policy_analysis["seats"]
    for seat in seats:
        seat["decisions"] = [_row("tile-type-change", "PAIJIA_SWITCH")] * 3
    return played


def fake_activation_many_in_few_seeds(task):
    """3 seedsに36件（下限の件数は満たすが、seed数が足りない）。"""
    return _concentrated_rows(task)


def fake_activation_invalid(task):
    def plan(seed, seat):
        if seed == ACTIVATION_SEEDS[2] and seat == 1:
            return "out-of-scope-difference"
        return _one_change_per_seat(seed, seat)

    played = _activation(task, plan)
    if task.seed == ACTIVATION_SEEDS[2]:
        # 1件の対象外差分を足しても変更件数（32）はそのまま満たす
        seat = played.policy_analysis["seats"][1]
        seat["decisions"][1] = _row("red-only-change", "PAIJIA_SWITCH")
    return played


def fake_activation_count_mismatch(task):
    played = fake_activation_proceed(task)
    if task.seed == ACTIVATION_SEEDS[4]:
        played.policy_analysis["seats"][0]["decisions"].pop()
    return played


def fake_activation_no_analysis(task):
    played = fake_activation_proceed(task)
    return PlayedHanchan(played.capture, ENVIRONMENT, os.getpid(), 2048)


# ---------------------------------------------------------------------------
# 段階2のfake play（spawn worker内）
# ---------------------------------------------------------------------------

SCREEN_COUNTS = (2, 3, 4, 5)


def _screen_seats(task, *, switches=1):
    seats = []
    for seat, role in enumerate(placement_roles(task.placement)):
        count = SCREEN_COUNTS[seat]
        entry = {"decision_seconds": [0.01] * count, "role": role, "seat": seat}
        if role == "A":
            stages = {"NO_ANALYSIS": count - switches}
            if switches:
                stages["PAIJIA_SWITCH"] = switches
            entry["stage_counts"] = {k: v for k, v in stages.items() if v}
            kinds = ("tile-type-change", "red-only-change")[:switches]
            entry["switches"] = [
                {
                    "analysis": analysis("PAIJIA_SWITCH", *CHANGE_PAIRS[kind]),
                    "kind": kind,
                }
                for kind in kinds
            ]
        seats.append(entry)
    return {"seats": seats}


def fake_screen(task):
    capture = aabb_capture(task, counts=SCREEN_COUNTS)
    return PlayedHanchan(
        capture, ENVIRONMENT, os.getpid(), 4096, None, _screen_seats(task)
    )


def fake_screen_bad_analysis(task):
    played = fake_screen(task)
    if (task.seed, task.placement) == (SCREEN_SEEDS[1], 2):
        played.policy_analysis["seats"][0]["decision_seconds"].append(0.01)
    return played


def fake_screen_missing_evidence(task):
    played = fake_screen(task)
    if (task.seed, task.placement) == (SCREEN_SEEDS[0], 3):
        capture = aabb_capture(task, evidence=False, counts=SCREEN_COUNTS)
        return PlayedHanchan(
            capture, ENVIRONMENT, os.getpid(), 4096, None, played.policy_analysis
        )
    return played


def fake_screen_invariant_stop(task):
    if (task.seed, task.placement) == (SCREEN_SEEDS[2], 1):
        from lisjong_arena.heuristic_candidate_engine_aabb.paijia_tie_worker import (
            PaijiaTieInvariantError,
        )

        raise PaijiaTieInvariantError("synthetic analysis inconsistency")
    return fake_screen(task)
