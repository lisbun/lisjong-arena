"""#524 単独副露者genbutsu候補のshadow変更頻度gateのtest用builder・fake play。

実engine・実Policy・Rust wheelを使わない。半荘は#513のfixture（実engineの
``MatchState`` を手計算済みscriptで進める）で、Policy側の観測は合成した行である。
``fake_*`` はspawn workerへ渡すため、module-levelの関数として置く。

```text
8 seeds × 4席B、各seatの判断3件
  fake_proceed         各seat 1件のSWITCHED変更 -> 32件 / 8 seeds（下限ちょうど）
  fake_low             1件だけ変更なし          -> 31件
  fake_four_seeds      4 seeds × 4 seats × 2件  -> 32件 / 4 seeds（seed数の下限ちょうど）
  fake_three_seeds     3 seeds × 4 seats × 3件  -> 36件 / 3 seeds
```
"""

import os

from _engine_aabb_protocol_fixtures import (
    ARENA_REVISION,
    ENVIRONMENT,
    LISJONG_REVISION,
    WHEEL_PATH,
)
from _engine_aabb_record_fixtures import SCRIPT_Y, play

from lisjong_arena import seed_registry
from lisjong_arena.heuristic_candidate_engine_aabb import (
    single_open_genbutsu_plan as plan_module,
)
from lisjong_arena.heuristic_candidate_engine_aabb.executor import PlayedHanchan
from lisjong_arena.heuristic_candidate_engine_aabb.kyoku_record import SeatAssignment
from lisjong_arena.heuristic_candidate_engine_aabb.protocol import SEED_DOMAIN
from lisjong_arena.heuristic_candidate_engine_aabb.record_store import HanchanCapture

SEEDS = tuple(range(524_000, 524_008))
LIMITS = {
    "enforcement": "synthetic test value",
    "process_tree_rss_bytes": 4 * 1024**3,
    "wall_seconds": 2 * 3600,
}
DECISIONS_PER_SEAT = 3

__all__ = ["ARENA_REVISION", "ENVIRONMENT", "LISJONG_REVISION", "WHEEL_PATH"]


def allocation(seeds=SEEDS, *, split="DEVELOPMENT", owner=None, population=None):
    return seed_registry.build_allocation_record(
        owner_issue=owner or plan_module.OWNER_ISSUE,
        protocol="arena-heuristic-candidate-engine-aabb-half-v1",
        seed_domain=SEED_DOMAIN,
        purpose="test",
        population=population or plan_module.POPULATION,
        split=split,
        seeds=seeds,
        arena_revision="e" * 40,
        protocol_revision="test",
        provenance_reference="test",
        allocation_timestamp="2026-10-24T00:00:00Z",
    )


RECORD = allocation()


def ledger(*records):
    document = seed_registry.new_ledger()
    document["allocations"] = list(records or (RECORD,))
    document["allocations"].sort(key=seed_registry._sort_key)
    return seed_registry.validate_ledger(document)


def binding(document, record):
    return seed_registry.allocation_binding(document, record["allocation_identity"])


def plan_document(tmp, *, run_kind="allocated", name="run", **overrides):
    document = overrides.pop("ledger", None) or ledger()
    record = overrides.pop("record", RECORD)
    arguments = {
        "run_kind": run_kind,
        "ordered_seeds": SEEDS if run_kind == "allocated" else (472_005, 472_006),
        "allocation_binding": binding(document, record)
        if run_kind == "allocated"
        else None,
        "timeout_seconds": 600,
        "resource_limits": LIMITS,
        "wheel_path": WHEEL_PATH,
        "destinations": {
            "diagnostics": str(tmp / f"{name}-{run_kind}-diagnostics"),
            "output": str(tmp / f"{name}-{run_kind}-output"),
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
# 合成した正規化済みの値（canonical_engine_valueと同じ形）
# ---------------------------------------------------------------------------


def tile_type(category="MANZU", rank=5):
    return {"__type": "TileType", "category": f"TileCategory.{category}", "rank": rank}


def discard(category="MANZU", rank=5, *, red=False, tsumogiri=False):
    return {
        "__type": "DiscardAction",
        "actor": "Seat.SEAT_0",
        "tile": {
            "__type": "Tile",
            "is_red": red,
            "tile_type": tile_type(category, rank),
        },
        "tsumogiri": tsumogiri,
    }


def evaluation(action, shanten=2, ukeire=10, retained=3, yaku=1):
    return {
        "__type": "GenbutsuCandidateEvaluation",
        "action": action,
        "current_ukeire_count": ukeire,
        "post_discard_shanten": shanten,
        "retained_real_value": retained,
        "yaku_route_value": yaku,
    }


PARENT = discard()
GENBUTSU = discard("PINZU", 9)


def analysis(stage, parent=PARENT, selected=PARENT):
    """``SingleOpenGenbutsuAnalysis`` の正規化表現（比較値ありのstageだけ使う）。"""
    switched = stage == "SWITCHED"
    alternative = GENBUTSU if switched else discard("PINZU", 9)
    values = {} if switched else {"ukeire": 4}  # 受入が下がる -> 代替候補なし
    return {
        "__type": "SingleOpenGenbutsuAnalysis",
        "action_changed": parent != selected,
        "alternative_evaluations": [evaluation(alternative, **values)],
        "genbutsu_tile_types": [tile_type("PINZU", 9)],
        "open_opponent_seat": 2,
        "parent_action": parent,
        "parent_evaluation": evaluation(parent),
        "parent_in_genbutsu": False,
        "qualifying_actions": [selected] if switched else [],
        "selected_action": selected,
        "stage": f"SingleOpenGenbutsuStage.{stage}",
    }


def _detail(analysis_value, candidate, incumbent, violations=(), plain=None):
    return {
        "analysis": analysis_value,
        "candidate_action": candidate,
        "incumbent_action": incumbent,
        "plain_candidate_action": candidate if plain is None else plain,
        "violations": list(violations),
    }


def row(kind="same"):
    """合成した判断1件。``kind`` は保存されるoutcomeとstageを決める。"""
    base = {"a_plain_seconds": 2e-6, "a_seconds": 3e-6, "b_seconds": 1e-6}
    if kind == "change":
        detail = _detail(analysis("SWITCHED", PARENT, GENBUTSU), GENBUTSU, PARENT)
        return {**base, "outcome": "tile-type-change", "stage": "SWITCHED", "detail": detail}  # fmt: skip
    if kind == "kept":
        detail = _detail(analysis("NO_QUALIFYING_ALTERNATIVE"), PARENT, PARENT)
        return {**base, "outcome": "same", "stage": "NO_QUALIFYING_ALTERNATIVE", "detail": detail}  # fmt: skip
    if kind == "invariant":
        detail = _detail(None, GENBUTSU, PARENT, ["synthetic reason"])
        return {**base, "outcome": "analysis-invariant-violation", "stage": "SWITCHED", "detail": detail}  # fmt: skip
    if kind == "trace":
        detail = _detail(None, GENBUTSU, PARENT, ["synthetic reason"], plain=PARENT)
        return {**base, "outcome": "trace-action-mismatch", "stage": "SWITCHED", "detail": detail}  # fmt: skip
    return {**base, "outcome": "same", "stage": "NO_OPEN_OPPONENT"}


# ---------------------------------------------------------------------------
# fake play（spawn worker内）
# ---------------------------------------------------------------------------


def _played(task, seat_rows):
    match = play(task.seed, SCRIPT_Y)
    counts = tuple(len(seat_rows(task.seed, seat)) for seat in range(4))
    capture = HanchanCapture(
        match=match,
        seed=task.seed,
        placement=task.placement,
        seats=(SeatAssignment("B", "placement-aware-speed-call"),) * 4,
        selector_call_counts=counts,
        policy_call_counts=counts,
        round_evidence=None,
    )
    analysis_value = {
        "seats": [
            {"decisions": seat_rows(task.seed, seat), "seat": seat} for seat in range(4)
        ]
    }
    return PlayedHanchan(capture, ENVIRONMENT, os.getpid(), 2048, None, analysis_value)


def _one_change(seed, seat):
    return [row("change"), row("kept"), row()]


def fake_proceed(task):
    return _played(task, _one_change)


def fake_low(task):
    def rows(seed, seat):
        if (seed, seat) == (SEEDS[-1], 3):
            return [row("kept"), row("kept"), row()]  # 変更なし -> 31件
        return _one_change(seed, seat)

    return _played(task, rows)


def fake_four_seeds(task):
    def rows(seed, seat):
        if seed in SEEDS[:4]:
            return [row("change"), row("change"), row()]
        return [row(), row(), row()]

    return _played(task, rows)


def fake_three_seeds(task):
    def rows(seed, seat):
        if seed in SEEDS[:3]:
            return [row("change")] * 3
        return [row(), row(), row()]

    return _played(task, rows)


def fake_invariant(task):
    def rows(seed, seat):
        if (seed, seat) == (SEEDS[2], 1):
            return [row("change"), row("invariant"), row()]
        return _one_change(seed, seat)

    return _played(task, rows)


def fake_trace_mismatch(task):
    def rows(seed, seat):
        if (seed, seat) == (SEEDS[5], 0):
            return [row("change"), row("trace"), row()]
        return _one_change(seed, seat)

    return _played(task, rows)


def fake_count_mismatch(task):
    played = fake_proceed(task)
    if task.seed == SEEDS[4]:
        played.policy_analysis["seats"][0]["decisions"].pop()
    return played


def fake_no_analysis(task):
    played = fake_proceed(task)
    return PlayedHanchan(played.capture, ENVIRONMENT, os.getpid(), 2048)
