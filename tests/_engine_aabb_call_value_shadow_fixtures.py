"""#534 call-value-shadow-v1のtest用builder・fake play。

実engineの対局・Rust wheel・実Policyの半荘実行を使わない（解析APIは実lisjongを使う）。

```text
_call_value_shadow_fixtures.json   実callの記録9件（観測smokeの実engine半荘4件 +
                                   lisjong #348 / #350の局面fixture 5件。生成元は
                                   docs/call-value-shadow-534.md）
engine evidence builder           公開evidenceの列（打牌 -> epoch -> 結果）を合成する
fake_*                            spawn workerへ渡すmodule-levelのplay。半荘は#513の
                                  scriptで、Policy側の観測は上の記録を並べたもの
```
"""

import json
import os
from pathlib import Path
from types import SimpleNamespace

from _engine_aabb_protocol_fixtures import (
    ARENA_REVISION,
    ENVIRONMENT,
    LISJONG_REVISION,
    WHEEL_PATH,
)
from _engine_aabb_record_fixtures import SCRIPT_Y, play
from lisjong_engine.public_state import PublicMeld, PublicMeldType, PublicTile
from lisjong_engine.reaction import reaction_seat_order
from lisjong_engine.round_evidence import (
    DiscardEvidence,
    MeldCalledEvidence,
    ResponseEpochClosedEvidence,
    ResponseEpochOpenedEvidence,
    ResponseOutcome,
    ResponseTrigger,
)
from lisjong_engine.seat import Seat as EngineSeat
from lisjong_engine.tile import TileCategory as EngineCategory
from lisjong_engine.tile import TileType as EngineTileType
from lisjong_engine.wind import Wind as EngineWind

from lisjong_arena import seed_registry
from lisjong_arena.heuristic_candidate_engine_aabb import (
    call_value_shadow_plan as plan_module,
)
from lisjong_arena.heuristic_candidate_engine_aabb.call_value_shadow_worker import (
    decode_action,
    decode_context,
)
from lisjong_arena.heuristic_candidate_engine_aabb.executor import PlayedHanchan
from lisjong_arena.heuristic_candidate_engine_aabb.kyoku_record import SeatAssignment
from lisjong_arena.heuristic_candidate_engine_aabb.protocol import SEED_DOMAIN
from lisjong_arena.heuristic_candidate_engine_aabb.record_store import HanchanCapture
from lisjong_arena.lisjong_engine.domain_conversion import (
    seat_from_engine_seat,
    wind_from_engine_wind,
)

__all__ = ["ARENA_REVISION", "ENVIRONMENT", "LISJONG_REVISION", "WHEEL_PATH"]

FIXTURES = json.loads(
    (Path(__file__).with_name("_call_value_shadow_fixtures.json")).read_text(
        encoding="utf-8"
    )
)
OWNER = "lisbun/lisjong-arena#535"
DECISIONS = 12
OPPORTUNITIES = 6


def record(name: str, **changes) -> dict:
    """記録1件のcopy（ordinal等を差し替える）。"""
    value = json.loads(json.dumps(FIXTURES[name]))
    post = changes.pop("post_ordinal", None)
    value.update(changes)
    if post is not None:
        value["post"]["decision_ordinal"] = post
    return value


def contexts(name: str):
    """記録の (pre context, call, post context, discard)（postがなければNone）。"""
    value = FIXTURES[name]
    post = value["post"]
    return (
        decode_context(value["pre_context"]),
        decode_action(value["call"]),
        None if post is None else decode_context(post["context"]),
        None if post is None else decode_action(post["discard"]),
    )


# ---------------------------------------------------------------------------
# ledger・plan
# ---------------------------------------------------------------------------


def allocation(
    seeds=plan_module.SEEDS,
    *,
    owner=OWNER,
    protocol=plan_module.ALLOCATION_PROTOCOL,
    population=plan_module.POPULATION,
    domain=SEED_DOMAIN,
):
    return seed_registry.build_allocation_record(
        owner_issue=owner,
        protocol=protocol,
        seed_domain=domain,
        purpose="test",
        population=population,
        split="DEVELOPMENT",
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


def binding(document, record_value=RECORD):
    return seed_registry.allocation_binding(
        document, record_value["allocation_identity"]
    )


def plan_document(
    tmp, *, run_kind="allocated", observation="observed", name="run", **overrides
):
    document = overrides.pop("ledger", None) or ledger()
    if "allocation_binding" not in overrides:
        overrides["allocation_binding"] = (
            binding(document) if run_kind == "allocated" else None
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


def gate(plan, *, ledger):
    plan_module.require_live_target(
        plan, ledger=ledger, environment=ENVIRONMENT, head=ARENA_REVISION
    )


# ---------------------------------------------------------------------------
# 公開evidence（engineの型で合成する）
# ---------------------------------------------------------------------------


def engine_seat(seat):
    return next(item for item in EngineSeat if seat_from_engine_seat(item) == seat)


def engine_tile(tile):
    return PublicTile(
        EngineTileType(
            EngineCategory[tile.tile_type.category.name], tile.tile_type.rank
        ),
        tile.is_red,
    )


def engine_meld(call, *, consumed=None):
    from lisjong.policy_contract.action import ChiAction

    tiles = consumed if consumed is not None else call.consumed_tiles
    return PublicMeld(
        meld_type=PublicMeldType.CHI
        if isinstance(call, ChiAction)
        else PublicMeldType.PON,
        tiles=tuple(engine_tile(tile) for tile in (*tiles, call.called_tile)),
        from_seat=engine_seat(call.target),
        called_tile=engine_tile(call.called_tile),
    )


def called_order(pre) -> int:
    from lisjong.policy_contract.action import ChiAction, PonAction

    call = next(a for a in pre.legal_actions if isinstance(a, (ChiAction, PonAction)))
    return pre.input.players[int(call.target)].discards[-1].order


def epoch(call, order, outcome, *after, tile=None, discard_seat=None):
    """打牌 -> epoch open -> close(outcome) -> ``after``。"""
    target = discard_seat or engine_seat(call.target)
    return (
        DiscardEvidence(
            seat=target,
            tile=tile or engine_tile(call.called_tile),
            is_tsumogiri=False,
            order=order,
            is_riichi_declaration=False,
        ),
        ResponseEpochOpenedEvidence(
            trigger=ResponseTrigger.DISCARD,
            source_seat=target,
            responder_seats=reaction_seat_order(target),
        ),
        ResponseEpochClosedEvidence(
            trigger=ResponseTrigger.DISCARD, source_seat=target, outcome=outcome
        ),
        *after,
    )


def established(call, order):
    return epoch(
        call,
        order,
        ResponseOutcome.CALL,
        MeldCalledEvidence(engine_seat(call.actor), engine_meld(call), order),
    )


def completion(pre, actor_evidence, *, actor=0, honba=None):
    """局の終了（actor席のevidenceだけを持ち、他席は空）。"""
    state = pre.input.round
    wind = next(
        item for item in EngineWind if wind_from_engine_wind(item) == state.round_wind
    )
    return SimpleNamespace(
        prevailing_wind=wind,
        hand_number=state.hand_number,
        dealer_seat=engine_seat(state.dealer_seat),
        honba=state.honba if honba is None else honba,
        projections=tuple(
            SimpleNamespace(
                viewer_seat=seat,
                evidence=actor_evidence
                if int(seat_from_engine_seat(seat)) == actor
                else (),
            )
            for seat in EngineSeat
        ),
    )


# ---------------------------------------------------------------------------
# fake play（spawn worker内）
# ---------------------------------------------------------------------------

PRIMARY = ("r1_primary_below_threshold", "kuikae_primary_below_threshold")
OTHERS = (
    "closed_tenpai_below_max",
    "red_five_consistent",
    "value_mode_below_threshold",
    "real_parent_decision",
    "real_primary_consistent",
    "real_priority_cancel",
    "real_speed_not_primary",
)


def calls_for(names) -> list[dict]:
    """記録を席ごとの判断ordinalへ並べる（call、直後打牌の順に2つずつ）。"""
    next_ordinal = [0, 0, 0, 0]
    calls = []
    for name in names:
        actor = FIXTURES[name]["actor"]
        ordinal = next_ordinal[actor]
        next_ordinal[actor] += 2
        calls.append(
            record(
                name,
                decision_ordinal=ordinal,
                response_ordinal=ordinal // 2,
                round_ordinal=0,
                post_ordinal=ordinal + 1 if FIXTURES[name]["post"] else None,
            )
        )
    return calls


def _played(task, names, *, digests=None, observed=True):
    match = play(task.seed, SCRIPT_Y)
    counts = (DECISIONS,) * 4
    capture = HanchanCapture(
        match=match,
        seed=task.seed,
        placement=task.placement,
        seats=(SeatAssignment("B", "placement-aware-speed-call"),) * 4,
        selector_call_counts=counts,
        policy_call_counts=counts,
        round_evidence=None,
    )
    analysis = {"action_sequence_sha256": digests or [f"{task.seed:064x}"] * 4}
    if observed:
        analysis |= {
            "calls": calls_for(names),
            "rounds": len(match.history),
            "seats": [
                {
                    "call_opportunities": OPPORTUNITIES,
                    "decisions": DECISIONS,
                    "seat": seat,
                }
                for seat in range(4)
            ],
        }
    return PlayedHanchan(capture, ENVIRONMENT, os.getpid(), 2048, None, analysis)


def fake_proceed(task):
    """最初の6 seedsで主対象の変更2件ずつ -> 12件 / 6 seeds（下限ちょうど）。"""
    index = plan_module.SEEDS.index(task.seed)
    names = PRIMARY if index < 6 else ()
    return _played(task, (*names, *OTHERS) if index == 0 else names)


def fake_low_seeds(task):
    """5 seedsで変更20件 -> seed数不足でSTOP-LOW-ACTIVATION。"""
    index = plan_module.SEEDS.index(task.seed)
    return _played(task, PRIMARY * 2 if index < 5 else ())


def fake_invalid(task):
    played = fake_proceed(task)
    if task.seed == plan_module.SEEDS[3]:
        call = played.policy_analysis["calls"][0]
        call["invalid"] = ["synthetic: unexplained cancellation"]
        call["resolution"] = {"detail": "synthetic", "status": "INVALID"}
        call["post"] = None
    return played


def fake_stop(task):
    if task.seed == plan_module.SEEDS[2]:
        raise RuntimeError("synthetic worker failure")
    return fake_proceed(task)


def fake_rehearsal_observed(task):
    return _played(task, PRIMARY)


def fake_rehearsal_unobserved(task):
    return _played(task, (), observed=False)


def fake_rehearsal_unobserved_other(task):
    digests = ["f" * 64] * 4 if task.seed == plan_module.REHEARSAL_SEEDS[1] else None
    return _played(task, (), digests=digests, observed=False)
