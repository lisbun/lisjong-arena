"""#553 pon-first-pass-branch-v1のtest用builder・fake play。

R1（#544）のfixture（#513のscript X、D armの局の差し替え）を再利用し、行動記録とdigestが整合する
Rと、P1 wrapperの記録を持つD armを作る。

```text
分岐点の局面  E（seat 0）が門前で、W（seat 2）の9m打牌にChampionがPonを選ぶ（Honitsu 1向聴）。
              contextはlisjongのPolicyInputで合成し、鳴き解析は実lisjongで計算する
R の行動記録  48判断（各席12）。K0（局0）: index 3でEがPon -> SがWからロン（script X）なので
              CANCELLED-RON。K9（局9）: index 40でEがPon -> ESTABLISHED（分岐）
D arm         status branched、分岐点の行動 = Pass、Passの適用あり。結果はscript Xの対象局を
              差し替えた局completion fact
```
"""

import functools
import json
import os
from dataclasses import replace

import _engine_aabb_riichi_dama_branch_fixtures as v1
from _engine_aabb_protocol_fixtures import (
    ARENA_REVISION,
    ENVIRONMENT,
    LISJONG_REVISION,
    WHEEL_PATH,
)
from _engine_aabb_record_fixtures import SCRIPT_X, play
from lisjong.policy_contract import DecisionContext
from lisjong.policy_contract.action import DiscardAction, PassAction, PonAction
from lisjong.policy_contract.discard import Discard
from lisjong.policy_contract.seat import Seat
from lisjong.policy_contract.wind import Wind

from lisjong_arena import seed_registry
from lisjong_arena.heuristic_candidate_engine_aabb import (
    pon_first_pass_plan as plan_module,
)
from lisjong_arena.heuristic_candidate_engine_aabb.call_value_shadow_worker import (
    CANCELLED_RON,
    ESTABLISHED,
    action_sequence_digest,
    canonical_digest,
    encode_action,
    encode_context,
)
from lisjong_arena.heuristic_candidate_engine_aabb.executor import PlayedHanchan
from lisjong_arena.heuristic_candidate_engine_aabb.kyoku_record import SeatAssignment
from lisjong_arena.heuristic_candidate_engine_aabb.lock import EXECUTION_BRANCH
from lisjong_arena.heuristic_candidate_engine_aabb.pon_first_pass_worker import (
    call_analysis,
)
from lisjong_arena.heuristic_candidate_engine_aabb.record_store import HanchanCapture
from lisjong_arena.heuristic_candidate_engine_aabb.riichi_dama_branch_worker import (
    ArmCapture,
    settlement_of_fact,
)
from lisjong_arena.overall_champion_aabb.lock import document_identity

__all__ = ["ARENA_REVISION", "ENVIRONMENT", "LISJONG_REVISION", "WHEEL_PATH"]

OWNER = "lisbun/lisjong-arena#554"
DECISIONS = v1.DECISIONS
CHAMPION = v1.CHAMPION
HONITSU = "1235799m112233z"
"""9mのPonでHonitsuの1向聴になり、ChampionはPonを選ぶ（lisjong #363のtestと同じ手）。"""
TARGET_SEAT = Seat.SEAT_2
CALLED_ORDER = 20
PASS = PassAction(actor=Seat.SEAT_0)


def pon_action() -> PonAction:
    tile = v1.tile("9m")
    return PonAction(
        actor=Seat.SEAT_0,
        target=TARGET_SEAT,
        called_tile=tile,
        consumed_tiles=(tile, tile),
    )


PON = pon_action()


def pon_decision(*, called_order=CALLED_ORDER, **kwargs) -> DecisionContext:
    """Eが門前で、Wの最後の打牌9mへPon / Passを選ぶ判断。"""
    context = v1.policy_input(HONITSU, own_river="", **kwargs)
    players = list(context.players)
    players[int(TARGET_SEAT)] = replace(
        players[int(TARGET_SEAT)],
        discards=(
            Discard(
                tile=v1.tile("9m"), tsumogiri=False, order=called_order, called_by=None
            ),
        ),
    )
    context = replace(context, players=tuple(players))
    return DecisionContext(input=context, legal_actions=(PASS, PON))


K0_DECISION = pon_decision()
"""K0 E1-0（親E）。"""
K9_DECISION = pon_decision(
    round_wind=Wind.SOUTH, hand_number=4, honba=4, dealer=Seat.SEAT_3
)
"""K9 S4-4（親N）。"""
CANDIDATE_POINTS = {
    0: (3, K0_DECISION, CANCELLED_RON),
    9: (40, K9_DECISION, ESTABLISHED),
}
"""round ordinal -> (decision index, context, 解決結果)。席はE（seat 0）。"""

# ---------------------------------------------------------------------------
# Rの行動記録（digestが整合する48判断）
# ---------------------------------------------------------------------------

_TOTAL = 4 * DECISIONS
_FIXED = {3: (0, PON), 40: (0, PON)}


def _ordinal(index: int) -> int:
    if index < 5:
        return 0
    if index >= 40:
        return 9
    return 1 + (index - 5) * 8 // 35


@functools.cache
def _plan_entries() -> tuple[tuple[int, int, object], ...]:
    remaining = [DECISIONS - len(_FIXED), DECISIONS, DECISIONS, DECISIONS]
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


@functools.cache
def _analysis_text(round_ordinal: int) -> str:
    _, decision, _ = CANDIDATE_POINTS[round_ordinal]
    return json.dumps(call_analysis(decision, PON))


def candidate(round_ordinal: int, *, resolution=None) -> dict:
    """fake Rの候補記録（workerの ``PonRObserver`` と同じ形。鳴き解析は実lisjong）。"""
    index, decision, default = CANDIDATE_POINTS[round_ordinal]
    context = encode_context(decision)
    return {
        "decision_index": index,
        "decision_input": {
            "context": context,
            "context_sha256": canonical_digest(context),
            "pon": encode_action(PON),
            **json.loads(_analysis_text(round_ordinal)),
        },
        "invalid": [],
        "post_sha256": entries_digest(index, _round_stop(round_ordinal)),
        "prefix_sha256": entries_digest(0, index),
        "round": v1.context_round(decision),
        "round_ordinal": round_ordinal,
        "seat": 0,
        "selection": {
            "called_discard_order": CALLED_ORDER,
            "resolution": resolution or default,
        },
    }


# ---------------------------------------------------------------------------
# D arm
# ---------------------------------------------------------------------------


def d_record(seed: int, ordinal: int, branch: dict, outcome: str, **changes) -> dict:
    """fake D armの記録（workerの ``d_arm`` と同じ形）。"""
    round_ordinal = branch["round_ordinal"]
    index, decision, _ = CANDIDATE_POINTS[round_ordinal]
    fact, final = v1.d_facts(seed, round_ordinal, outcome)
    record = {
        "branch_action": encode_action(PASS),
        "branch_context_sha256": canonical_digest(encode_context(decision)),
        "branch_decision_index": index,
        "branch_ordinal": ordinal,
        "calls_after_branch": [],
        "decisions": index + 6,
        "invalid": [],
        "pass_applied": True,
        "post_sha256": canonical_digest(["d-post", seed, index, outcome]),
        "prefix_sha256": entries_digest(0, index),
        "round": branch["round"],
        "round_ordinal": round_ordinal,
        "seat": branch["seat"],
        "seed": seed,
        "settlement": settlement_of_fact(fact, final),
        "status": "branched",
        "wrapper_calls_after_branch": 0,
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


def time_estimate(
    *, r_wall=50.0, d_wall=20.0, branches=8, r_hanchans=2, elapsed=1400.0, **changes
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


def _r_played(task, rounds, *, observed=True, digests=None, resolutions=None):
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
        resolutions = resolutions or {}
        analysis |= {
            "actions": actions_record(),
            "candidates": [
                candidate(ordinal, resolution=resolutions.get(ordinal))
                for ordinal in rounds
            ],
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
    """全seedでK0（ロン取消）とK9（分岐）の2候補。"""
    return _r_played(task, (0, 9))


def fake_r_cancelled_only(task):
    """全seedでK0のロン取消だけ（成立分岐0件）。"""
    return _r_played(task, (0,))


def fake_r_priority(task):
    """1 seedでK9の候補が優先鳴きで取り消された（起こりえないのでINVALID）。"""
    if task.seed == plan_module.SEEDS[3]:
        return _r_played(task, (0, 9), resolutions={9: "CANCELLED-PRIORITY-CALL"})
    return fake_r_both(task)


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
    return _d_played(task, "better")


def fake_d_varied(task):
    """seedによって結果を変える（seed間でΔがばらつく）。"""
    index = plan_module.SEEDS.index(task.seed) if task.seed in plan_module.SEEDS else 0
    return _d_played(task, ("better", "same", "worse")[index % 3])


def fake_d_recall(task):
    """分岐後に分岐席が鳴きを2回選び、1回成立・1回ロン取消。"""
    calls = [
        {
            "decision_index": 42,
            "kind": "pon",
            "resolution": CANCELLED_RON,
            "seat_decisions_after_branch": 1,
        },
        {
            "decision_index": 44,
            "kind": "chi",
            "resolution": ESTABLISHED,
            "seat_decisions_after_branch": 2,
        },
    ]
    return _d_played(
        task, "better", calls_after_branch=calls, wrapper_calls_after_branch=2
    )


def fake_d_not_passed(task):
    """1分岐で、Passした打牌に分岐席の副露がある。"""
    if task.seed == plan_module.SEEDS[5]:
        return _d_played(task, "better", pass_applied=False)
    return fake_d_better(task)


def fake_d_count_mismatch(task):
    """wrapperの選択回数と行動列の鳴きの数が違う。"""
    if task.seed == plan_module.SEEDS[5]:
        return _d_played(task, "better", wrapper_calls_after_branch=1)
    return fake_d_better(task)


def fake_d_unstable(task):
    """最初の分岐だけ、2回目のworkerでPassの適用が変わる。"""
    if task.branch_ordinal == 1:
        marker = os.environ["PON_FIRST_TEST_REPEAT_MARKER"]
        if os.path.exists(marker):
            return _d_played(task, "better", pass_applied=False)
        open(marker, "a", encoding="utf-8").close()  # noqa: SIM115
    return fake_d_better(task)


read_lines = v1.read_lines
write_lines = v1.write_lines
