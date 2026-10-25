"""#544 riichi-first-dama-branch-v1のtest用builder・fake play。

riichi-dama-branch-v1のfixture（#513のscript X、K0・K9の分岐context、D armの局の差し替え）を
再利用し、行動記録とdigestが整合するRと、R1 wrapperの記録を持つD armを作る。

```text
R の行動記録  48判断（各席12）。K0（局0）: index 3でEがリーチ、4で宣言牌。
              K9（局9）: index 40でEがリーチ、41で宣言牌。分岐のprefix / post digestは
              この行動記録から計算する（verifyが再計算して照合する）
D arm         status branched、分岐点の行動 = 宣言牌。``resumed`` では分岐後にEが1回リーチする
```
"""

import functools
import os

import _engine_aabb_riichi_dama_branch_fixtures as v1
from _engine_aabb_protocol_fixtures import (
    ARENA_REVISION,
    ENVIRONMENT,
    LISJONG_REVISION,
    WHEEL_PATH,
)
from _engine_aabb_record_fixtures import SCRIPT_X, play
from lisjong.policy_contract.action import DiscardAction, RiichiAction
from lisjong.policy_contract.seat import Seat

from lisjong_arena import seed_registry
from lisjong_arena.heuristic_candidate_engine_aabb import (
    riichi_first_dama_plan as plan_module,
)
from lisjong_arena.heuristic_candidate_engine_aabb.call_value_shadow_worker import (
    action_sequence_digest,
    canonical_digest,
    encode_action,
)
from lisjong_arena.heuristic_candidate_engine_aabb.executor import PlayedHanchan
from lisjong_arena.heuristic_candidate_engine_aabb.kyoku_record import SeatAssignment
from lisjong_arena.heuristic_candidate_engine_aabb.lock import EXECUTION_BRANCH
from lisjong_arena.heuristic_candidate_engine_aabb.record_store import HanchanCapture
from lisjong_arena.heuristic_candidate_engine_aabb.riichi_dama_branch_worker import (
    ArmCapture,
)
from lisjong_arena.overall_champion_aabb.lock import document_identity

__all__ = ["ARENA_REVISION", "ENVIRONMENT", "LISJONG_REVISION", "WHEEL_PATH"]

OWNER = "lisbun/lisjong-arena#545"
DECISIONS = v1.DECISIONS
CHAMPION = v1.CHAMPION
BRANCH_POINTS = v1.BRANCH_POINTS
DECLARATION = v1.DECLARATION
RIICHI = RiichiAction(actor=Seat.SEAT_0)

# ---------------------------------------------------------------------------
# Rの行動記録（digestが整合する48判断）
# ---------------------------------------------------------------------------

_TOTAL = 4 * DECISIONS
_FIXED = {3: (0, RIICHI), 4: (0, DECLARATION), 40: (0, RIICHI), 41: (0, DECLARATION)}


def _ordinal(index: int) -> int:
    if index < 5:
        return 0
    if index >= 40:
        return 9
    return 1 + (index - 5) * 8 // 35


@functools.cache
def _plan_entries() -> tuple[tuple[int, int, object], ...]:
    """(round ordinal, seat, action) の判断順の列（各席12判断）。"""
    remaining = [DECISIONS - 4, DECISIONS, DECISIONS, DECISIONS]
    entries = []
    cursor = 1
    for index in range(_TOTAL):
        if index in _FIXED:
            seat, action = _FIXED[index]
        else:
            while remaining[cursor % 4] == 0:
                cursor += 1
            seat = cursor % 4
            remaining[seat] -= 1
            cursor += 1
            action = DiscardAction(
                actor=Seat(seat), tile=v1.tile("9m"), tsumogiri=False
            )
        entries.append((_ordinal(index), seat, action))
    return tuple(entries)


def actions_record() -> list[list]:
    rows = []
    for ordinal, seat, action in _plan_entries():
        value = encode_action(action)
        rows.append([ordinal, seat, value, 0, 1, canonical_digest([value])])
    return rows


def seat_digests() -> list[str]:
    per_seat = [[], [], [], []]
    for _, seat, action in _plan_entries():
        per_seat[seat].append(action)
    return [action_sequence_digest(items) for items in per_seat]


def entries_digest(start: int, stop: int) -> str:
    rows = actions_record()
    return canonical_digest([[row[1], row[2]] for row in rows[start:stop]])


def _round_stop(ordinal: int) -> int:
    return max(i + 1 for i, row in enumerate(actions_record()) if row[0] == ordinal)


def r_record(seed: int, round_ordinal: int) -> dict:
    record = v1.r_record(seed, round_ordinal)
    index = record["decision_index"]
    record["prefix_sha256"] = entries_digest(0, index)
    record["post_sha256"] = entries_digest(index, _round_stop(round_ordinal))
    return record


# ---------------------------------------------------------------------------
# D arm
# ---------------------------------------------------------------------------


def d_record(
    seed: int, ordinal: int, branch: dict, outcome: str, *, resumed=False, **changes
) -> dict:
    """fake D armの記録（workerの ``d_arm`` と同じ形）。"""
    base = v1.d_record(seed, ordinal, branch, outcome)
    index = base["branch_decision_index"]
    record = {
        "branch_action": branch["declaration"],
        "branch_context_sha256": base["branch_context_sha256"],
        "branch_decision_index": index,
        "branch_ordinal": ordinal,
        "decisions": base["decisions"],
        "first_riichi_after_branch": {
            "decision_index": index + 3,
            "seat_decisions_after_branch": 2,
        }
        if resumed
        else None,
        "invalid": [],
        "post_sha256": base["post_sha256"],
        "prefix_sha256": entries_digest(0, index),
        "riichi_after_branch": 1 if resumed else 0,
        "round": base["round"],
        "round_ordinal": base["round_ordinal"],
        "seat": base["seat"],
        "seed": seed,
        "settlement": base["settlement"],
        "status": "branched",
        "wrapper_riichi_after_branch": 1 if resumed else 0,
    }
    record.update(changes)
    return record


# ---------------------------------------------------------------------------
# ledger・plan・見積もり
# ---------------------------------------------------------------------------


def allocation(seeds=plan_module.SEEDS, **changes):
    arguments = {
        "owner": OWNER,
        "protocol": plan_module.ALLOCATION_PROTOCOL,
        "population": plan_module.POPULATION,
    }
    arguments.update(changes)
    return v1.allocation(seeds, **arguments)


RECORD = allocation()


def ledger(*records):
    return v1.ledger(*(records or (RECORD,)))


def binding(document, record_value=RECORD):
    return seed_registry.allocation_binding(
        document, record_value["allocation_identity"]
    )


TARGET = {"branch": EXECUTION_BRANCH, "revision": ARENA_REVISION}
ELAPSED_BEFORE = 2700.0
"""本計測より前の段階（gate約1300秒＋rehearsal約1400秒）の経過の合成値。"""


def time_estimate(
    *, r_wall=50.0, d_wall=26.0, branches=23, r_hanchans=2, elapsed=1400.0, **changes
):
    values = plan_module.estimate_values(
        r_walls=[r_wall] * r_hanchans,
        d_walls=[d_wall] * branches,
        branches=branches,
        rehearsal_seat_hanchans=4 * r_hanchans,
        rehearsal_elapsed_seconds=elapsed,
    )
    payload = {
        **values,
        "environment_digest": document_identity(ENVIRONMENT),
        "execution_target": TARGET,
        "observed_plan_digest": "1" * 64,
        "observed_summary_digest": "2" * 64,
        "schema": plan_module.ESTIMATE_SCHEMA,
        "unobserved_plan_digest": "3" * 64,
    }
    payload.update(changes)
    return {**payload, "estimate_digest": document_identity(payload)}


def plan_document(
    tmp, *, run_kind="allocated", observation="observed", name="run", **overrides
):
    document = overrides.pop("ledger", None) or ledger()
    allocated = run_kind == "allocated"
    overrides.setdefault("allocation_binding", binding(document) if allocated else None)
    overrides.setdefault("time_estimate", time_estimate() if allocated else None)
    overrides.setdefault(
        "elapsed_before_seconds", ELAPSED_BEFORE if allocated else None
    )
    arguments = {
        "run_kind": run_kind,
        "observation": observation,
        "owner_issue": OWNER,
        "limit_enforcement": "synthetic test value",
        "wheel_path": WHEEL_PATH,
        "destinations": {
            "diagnostics": str(tmp / f"{name}-{run_kind}-{observation}-diagnostics"),
            "output": str(tmp / f"{name}-{run_kind}-{observation}-output"),
        },
        "environment": ENVIRONMENT,
        "arena_revision": ARENA_REVISION,
        "ledger": document,
    }
    arguments.update(overrides)
    return plan_module.assemble_plan(**arguments)


def write_plan(tmp, **kwargs):
    document = plan_document(tmp, **kwargs)
    path = (
        tmp
        / f"{kwargs.get('name', 'run')}-{kwargs.get('run_kind', 'allocated')}-{kwargs.get('observation', 'observed')}.json"
    )
    plan_module.save_plan(document, path)
    return path, document


def gate(plan, *, ledger):
    plan_module.require_live_target(
        plan, ledger=ledger, environment=ENVIRONMENT, head=ARENA_REVISION
    )


# ---------------------------------------------------------------------------
# fake play（spawn worker内）
# ---------------------------------------------------------------------------


def _r_played(task, rounds, *, observed=True, digests=None, actions=None):
    match = play(task.seed, SCRIPT_X)
    counts = (DECISIONS,) * 4
    capture = HanchanCapture(
        match=match,
        seed=task.seed,
        placement=task.placement,
        seats=(SeatAssignment("B", CHAMPION),) * 4,
        selector_call_counts=counts,
        policy_call_counts=counts,
        round_evidence=None,
    )
    analysis = {
        "action_sequence_sha256": digests or seat_digests(),
        "decisions": 4 * DECISIONS,
    }
    if observed:
        analysis |= {
            "actions": actions or actions_record(),
            "branches": [r_record(task.seed, ordinal) for ordinal in rounds],
            "riichi_declined": [0, 0, 0, 0],
            "rounds": len(match.history),
        }
    return PlayedHanchan(capture, ENVIRONMENT, os.getpid(), 2048, None, analysis)


def _d_played(task, outcome, **changes):
    record = d_record(task.seed, task.branch_ordinal, task.branch, outcome, **changes)
    return PlayedHanchan(
        ArmCapture(task.seed, task.branch_ordinal),
        ENVIRONMENT,
        os.getpid(),
        1024,
        None,
        record,
    )


def fake_r_both(task):
    """全seedでK0とK9の2分岐（16 seeds × 2 = 32分岐）。"""
    return _r_played(task, (0, 9))


def fake_r_other_actions(task):
    """行動記録の合法手の位置だけ違う（verifyの再実行で検出する）。"""
    actions = actions_record()
    actions[0] = [*actions[0][:3], 0, 2, actions[0][5]]
    return _r_played(task, (0, 9), actions=actions)


def fake_r_unobserved(task):
    return _r_played(task, (), observed=False)


def fake_r_unobserved_other(task):
    digests = ["f" * 64] * 4 if task.seed == plan_module.REHEARSAL_SEEDS[1] else None
    return _r_played(task, (), observed=False, digests=digests)


def fake_r_failure(task):
    if task.seed == plan_module.SEEDS[2]:
        raise RuntimeError("synthetic worker failure")
    return fake_r_both(task)


def fake_d_better(task):
    """K0は和了（Δ > 0）、K9は分岐後に再びリーチして流局。"""
    return _d_played(task, "better", resumed=task.branch["round_ordinal"] == 9)


def fake_d_varied(task):
    """seedによって結果を変える（seed間でΔがばらつく）。"""
    index = plan_module.SEEDS.index(task.seed) if task.seed in plan_module.SEEDS else 0
    outcome = ("better", "same", "worse")[index % 3]
    return _d_played(task, outcome, resumed=task.branch["round_ordinal"] == 9)


def fake_d_riichi_at_branch(task):
    """分岐点の判断でリーチが適用された（分岐点の行動が宣言牌でない）。"""
    if task.seed == plan_module.SEEDS[5] and task.branch["round_ordinal"] == 0:
        return _d_played(task, "better", branch_action=encode_action(RIICHI))
    return fake_d_better(task)


def fake_d_count_mismatch(task):
    """wrapperと行動列で分岐後のリーチ数が違う。"""
    if task.seed == plan_module.SEEDS[5] and task.branch["round_ordinal"] == 9:
        return _d_played(task, "better", resumed=True, wrapper_riichi_after_branch=2)
    return fake_d_better(task)


def fake_d_same(task):
    return _d_played(task, "same", resumed=task.branch["round_ordinal"] == 9)


def fake_d_unstable(task):
    """最初のK9分岐（branch ordinal 1）だけ、2回目のworkerで分岐後のリーチの位置が変わる。"""
    if task.branch_ordinal == 1:
        marker = os.environ["RIICHI_FIRST_TEST_REPEAT_MARKER"]
        first = not os.path.exists(marker)
        if first:
            open(marker, "a", encoding="utf-8").close()  # noqa: SIM115
        else:
            return _d_played(
                task,
                "better",
                resumed=True,
                first_riichi_after_branch={
                    "decision_index": 45,
                    "seat_decisions_after_branch": 3,
                },
            )
    return fake_d_better(task)


read_lines = v1.read_lines
write_lines = v1.write_lines
