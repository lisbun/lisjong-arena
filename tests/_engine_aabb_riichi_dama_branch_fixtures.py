"""#540 riichi-dama-branch-v1のtest用builder・fake play。

実engineの対局・Rust wheel・実Policyの半荘実行を使わない（特徴APIとwrapperは実lisjongを使う）。

```text
半荘       #513のscript X（実engineの``MatchState``へ局の結果を直接与えて精算する。
           Policyは呼ばない）。K0 E1-0でEが立直してSがWからロン、K9 S4-4でEが立直して
           Eだけ聴牌の流局（供託1本が残り、終局時にSへ）
分岐       Eの2局（K0・K9）。contextはlisjongのPolicyInputで合成し、特徴は実lisjongで計算する
           K0: 嵌張2s待ち、他家リーチ1人、見えない残り4枚 -> S3
           K9: 三色・ドラ1のダマ満貫（5s・8s待ち）-> S1
D arm      script Xの対象局からEの立直供託を除き、結果を差し替えた半荘の局completion fact
           （engineの``project_round_completion``）
fake_*     spawn workerへ渡すmodule-levelのplay
```
"""

import functools
import json
import os
from dataclasses import replace
from unittest import mock

from _engine_aabb_protocol_fixtures import (
    ARENA_REVISION,
    ENVIRONMENT,
    LISJONG_REVISION,
    WHEEL_PATH,
)
from _engine_aabb_record_fixtures import (
    RULES,
    SCRIPT_X,
    E,
    S,
    W,
    _draw,
    _ron,
    _tsumo,
    play,
)
from lisjong.policy_contract import DecisionContext
from lisjong.policy_contract.action import DiscardAction, RiichiAction
from lisjong.policy_contract.discard import Discard
from lisjong.policy_contract.own_hand_state import OwnHandState
from lisjong.policy_contract.player_state import PlayerPublicState
from lisjong.policy_contract.policy_input import PolicyInput
from lisjong.policy_contract.riichi import RiichiState
from lisjong.policy_contract.round_state import RoundState
from lisjong.policy_contract.seat import Seat
from lisjong.policy_contract.tile import Tile, TileCategory, TileType
from lisjong.policy_contract.wind import Wind
from lisjong_engine.match_state import MatchState
from lisjong_engine.riichi_event import RiichiContribution
from lisjong_engine.round_completion import (
    project_match_completion,
    project_round_completion,
)
from lisjong_engine.round_phase import RoundPhase
from lisjong_engine.round_state import RoundState as EngineRoundState

from lisjong_arena import seed_registry
from lisjong_arena.heuristic_candidate_engine_aabb import (
    riichi_dama_branch_plan as plan_module,
)
from lisjong_arena.heuristic_candidate_engine_aabb.call_value_shadow_worker import (
    canonical_digest,
    encode_action,
    encode_context,
)
from lisjong_arena.heuristic_candidate_engine_aabb.executor import PlayedHanchan
from lisjong_arena.heuristic_candidate_engine_aabb.kyoku_record import SeatAssignment
from lisjong_arena.heuristic_candidate_engine_aabb.lock import EXECUTION_BRANCH
from lisjong_arena.heuristic_candidate_engine_aabb.protocol import SEED_DOMAIN
from lisjong_arena.heuristic_candidate_engine_aabb.record_store import HanchanCapture
from lisjong_arena.heuristic_candidate_engine_aabb.riichi_dama_branch_worker import (
    ArmCapture,
    branch_features,
    settlement_of_fact,
)
from lisjong_arena.overall_champion_aabb.lock import document_identity

__all__ = ["ARENA_REVISION", "ENVIRONMENT", "LISJONG_REVISION", "WHEEL_PATH"]

OWNER = "lisbun/lisjong-arena#541"
DECISIONS = 12
"""1席あたりの判断数（4席で48）。"""
CHAMPION = "placement-aware-speed-call"

# ---------------------------------------------------------------------------
# lisjongのPolicyInput（判断context）
# ---------------------------------------------------------------------------

_CATEGORIES = {
    "m": TileCategory.MANZU,
    "p": TileCategory.PINZU,
    "s": TileCategory.SOUZU,
    "z": TileCategory.HONOR,
}


def tile(spec: str) -> Tile:
    rank = int(spec[0])
    return Tile(
        TileType(_CATEGORIES[spec[1]], 5 if rank == 0 else rank), is_red=rank == 0
    )


def hand(spec: str) -> tuple[Tile, ...]:
    tiles, ranks = [], ""
    for character in spec:
        if character.isdigit():
            ranks += character
            continue
        tiles.extend(tile(rank + character) for rank in ranks)
        ranks = ""
    return tuple(tiles)


def _river(spec: str, start: int) -> tuple[Discard, ...]:
    return tuple(
        Discard(tile=item, tsumogiri=False, order=start + index, called_by=None)
        for index, item in enumerate(hand(spec))
    )


def policy_input(
    concealed: str,
    *,
    round_wind=Wind.EAST,
    hand_number=1,
    honba=0,
    dealer=Seat.SEAT_0,
    dora="",
    own_river="9m",
    opponent_riichi=False,
    seat=Seat.SEAT_0,
) -> PolicyInput:
    players = []
    for index in range(4):
        riichi = RiichiState.NONE
        river = ""
        if index == int(seat):
            river = own_river
        elif opponent_riichi and index == (int(seat) + 1) % 4:
            riichi, river = RiichiState.ACCEPTED, "1p"
        players.append(
            PlayerPublicState(
                score=25_000,
                discards=_river(river, 10 * index),
                melds=(),
                riichi=riichi,
            )
        )
    return PolicyInput(
        self_seat=seat,
        round=RoundState(
            round_wind=round_wind,
            hand_number=hand_number,
            dealer_seat=dealer,
            honba=honba,
            riichi_sticks=0,
            dora_indicators=hand(dora),
            live_wall_tiles_remaining=40,
        ),
        players=tuple(players),
        own_hand=OwnHandState(concealed_tiles=hand(concealed), drawn_tile=None),
    )


def discards(context: PolicyInput) -> tuple[DiscardAction, ...]:
    seen = []
    for item in context.own_hand.concealed_tiles:
        if item not in seen:
            seen.append(item)
    return tuple(
        DiscardAction(actor=context.self_seat, tile=item, tsumogiri=False)
        for item in seen
    )


def riichi_decision(concealed: str, **kwargs) -> DecisionContext:
    context = policy_input(concealed, **kwargs)
    return DecisionContext(
        input=context,
        legal_actions=(RiichiAction(actor=context.self_seat), *discards(context)),
    )


def discard_decision(concealed: str, **kwargs) -> DecisionContext:
    context = policy_input(concealed, **kwargs)
    return DecisionContext(input=context, legal_actions=discards(context))


def declaration_decision(concealed: str, **kwargs) -> DecisionContext:
    """宣言後の打牌判断（RiichiActionなし、自席は宣言中）。"""
    context = policy_input(concealed, **kwargs)
    players = list(context.players)
    own = int(context.self_seat)
    players[own] = replace(players[own], riichi=RiichiState.DECLARED)
    context = replace(context, players=tuple(players))
    return DecisionContext(input=context, legal_actions=discards(context))


KANCHAN = "123m456p789s1355s1z"
"""打1zで嵌張2s待ち（見えない残り4枚）。ダマのロンは役なし。"""
SANSHOKU = "234m234p234s55m67s1z"
"""打1zでタンヤオ・平和・三色（5s・8s待ち）。ドラ表示牌1mでダマ満貫。"""

K0_DECISION = riichi_decision(KANCHAN, opponent_riichi=True)
"""K0 E1-0（親E）: 他家リーチ1人 -> S3。"""
K9_DECISION = riichi_decision(
    SANSHOKU,
    round_wind=Wind.SOUTH,
    hand_number=4,
    honba=4,
    dealer=Seat.SEAT_3,
    dora="1m",
)
"""K9 S4-4（親N）: ダマ満貫 -> S1。"""
DECLARATION = DiscardAction(actor=Seat.SEAT_0, tile=tile("1z"), tsumogiri=False)
BRANCH_POINTS = {0: (3, K0_DECISION), 9: (40, K9_DECISION)}
"""round ordinal -> (decision index, 分岐点のcontext)。席はE（seat 0）。"""


def context_round(decision) -> dict:
    state = decision.input.round
    return {
        "dealer_seat": int(state.dealer_seat),
        "hand_number": state.hand_number,
        "honba": state.honba,
        "prevailing_wind": state.round_wind.value,
    }


def prefix_digest(seed: int, index: int) -> str:
    return canonical_digest(["prefix", seed, index])


def r_record(seed: int, round_ordinal: int) -> dict:
    """fake Rの分岐記録（workerの ``RObserver`` と同じ形。特徴は実lisjong）。"""
    index, decision = BRANCH_POINTS[round_ordinal]
    context = encode_context(decision)
    record = {
        "context": context,
        "context_sha256": canonical_digest(context),
        "decision_index": index,
        "declaration": encode_action(DECLARATION),
        "declaration_decision_index": index + 1,
        "invalid": [],
        "post_sha256": canonical_digest(["post", seed, index]),
        "prefix_sha256": prefix_digest(seed, index),
        "riichi": encode_action(RiichiAction(actor=Seat.SEAT_0)),
        "round": context_round(decision),
        "round_ordinal": round_ordinal,
        "seat": 0,
    }
    record["features"] = json.loads(_features_text(round_ordinal))
    return record


@functools.cache
def _features_text(round_ordinal: int) -> str:
    """分岐点の特徴（局面はseedによらず同じなので1 processで1回だけ計算する）。"""
    index, decision = BRANCH_POINTS[round_ordinal]
    context = encode_context(decision)
    return json.dumps(
        branch_features({"context": context, "declaration": encode_action(DECLARATION)})
    )


# ---------------------------------------------------------------------------
# D armの局（script Xの対象局からEの供託を除いて結果を差し替える）
# ---------------------------------------------------------------------------

D_RESULTS = {
    "same": {0: _ron(S, source=W), 9: _draw(E)},
    "better": {0: _tsumo(E), 9: _draw(E)},
    "worse": {0: _ron(S, source=E), 9: _draw()},
}
"""D armの対象局の結果。better: Δ > 0、same: Rの供託分だけ +10、worse: Δ < 0。"""


def d_script(round_ordinal: int, outcome: str):
    """対象局までのscript（D armは対象局の精算で打ち切るので、その後の局は進めない）。"""
    script = list(SCRIPT_X[: round_ordinal + 1])
    _, riichi_seats = script[round_ordinal]
    script[round_ordinal] = (
        D_RESULTS[outcome][round_ordinal],
        tuple(item for item in riichi_seats if item is not E),
    )
    return tuple(script)


def play_prefix(seed: int, script):
    """``play`` と同じ方法で局を精算し、終局を要求せずに (match, rounds) を返す。"""
    match = MatchState(seed=seed, rules=RULES)
    rounds = []
    for make_result, riichi_seats in script:
        round_state = match.start_round()
        result = make_result(match.position)
        round_state._phase = RoundPhase.FINISHED
        round_state._result = result
        contributions = tuple(
            RiichiContribution(seat, RULES.riichi_stick_points) for seat in riichi_seats
        )
        with mock.patch.object(
            EngineRoundState,
            "riichi_contributions",
            new_callable=mock.PropertyMock,
            return_value=contributions,
        ):
            rounds.append(match.settle_active_round())
    return match, rounds


def d_facts(seed: int, round_ordinal: int, outcome: str):
    match, rounds = play_prefix(seed, d_script(round_ordinal, outcome))
    fact = project_round_completion(rounds[round_ordinal])
    final = (
        project_match_completion(match.completed_match)
        if match.completed_match is not None
        else None
    )
    return fact, final


def d_record(seed: int, ordinal: int, branch: dict, outcome: str, **changes) -> dict:
    """fake D armの記録（workerの ``d_arm`` と同じ形）。"""
    round_ordinal = branch["round_ordinal"]
    index, decision = BRANCH_POINTS[round_ordinal]
    fact, final = d_facts(seed, round_ordinal, outcome)
    record = {
        "branch_context_sha256": canonical_digest(encode_context(decision)),
        "branch_decision_index": index,
        "branch_ordinal": ordinal,
        "decisions": index + 6,
        "invalid": [],
        "post_sha256": canonical_digest(["d-post", seed, index, outcome]),
        "prefix_sha256": prefix_digest(seed, index),
        "replacements": 1 if round_ordinal == 9 else 0,
        "riichi_after_branch": 0,
        "round": branch["round"],
        "round_ordinal": round_ordinal,
        "seat": branch["seat"],
        "seed": seed,
        "settlement": settlement_of_fact(fact, final),
        "status": "branched",
    }
    record.update(changes)
    return record


# ---------------------------------------------------------------------------
# ledger・plan・見積もり
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
        allocation_timestamp="2026-10-25T00:00:00Z",
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


TARGET = {"branch": EXECUTION_BRANCH, "revision": ARENA_REVISION}


def time_estimate(*, r_wall=50.0, d_wall=26.0, branches=28, r_hanchans=2, **changes):
    values = plan_module.estimate_values(
        r_walls=[r_wall] * r_hanchans,
        d_walls=[d_wall] * branches,
        branches=branches,
        rehearsal_seat_hanchans=4 * r_hanchans,
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
    if "allocation_binding" not in overrides:
        overrides["allocation_binding"] = (
            binding(document) if run_kind == "allocated" else None
        )
    if "time_estimate" not in overrides:
        overrides["time_estimate"] = (
            time_estimate() if run_kind == "allocated" else None
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


def _r_played(task, rounds, *, observed=True, digests=None):
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
        "action_sequence_sha256": digests
        or [canonical_digest(["seat", task.seed, seat]) for seat in range(4)],
        "decisions": 4 * DECISIONS,
    }
    if observed:
        analysis |= {
            "branches": [r_record(task.seed, ordinal) for ordinal in rounds],
            "riichi_declined": [1, 0, 0, 0],
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
    """全seedでK0（S3）とK9（S1）の2分岐 -> 各部分集合24件 / 24 seeds。"""
    return _r_played(task, (0, 9))


def fake_r_seven_seeds(task):
    """最初の7 seedsだけ分岐 -> 各部分集合7 seeds（seed数不足）。"""
    index = plan_module.SEEDS.index(task.seed)
    return _r_played(task, (0, 9) if index < 7 else ())


def fake_r_missing_declaration(task):
    played = fake_r_both(task)
    if task.seed == plan_module.SEEDS[1]:
        record = played.policy_analysis["branches"][0]
        record["declaration"] = None
        record["declaration_decision_index"] = None
        record["invalid"] = ["the round ended before the declaration discard"]
        record["features"] = branch_features(record)
    return played


def fake_r_rehearsal(task):
    return _r_played(task, (0, 9))


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


def fake_d_same(task):
    return _d_played(task, "same")


def fake_d_worse(task):
    return _d_played(task, "worse")


def fake_d_prefix_mismatch(task):
    if task.seed == plan_module.SEEDS[5] and task.branch["round_ordinal"] == 0:
        return _d_played(task, "better", prefix_sha256="0" * 64)
    return _d_played(task, "better")


def fake_d_unstable(task):
    """2回目のworker（別process）では差し替え回数が変わる（D armの再現性の崩れ）。"""
    marker = os.environ["RIICHI_DAMA_TEST_REPEAT_MARKER"]
    first = not os.path.exists(marker)
    if first:
        open(marker, "a", encoding="utf-8").close()  # noqa: SIM115
    if task.branch_ordinal == 0 and not first:
        return _d_played(task, "better", replacements=7)
    return _d_played(task, "better")


def read_lines(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def write_lines(path, lines):
    from lisjong_arena.heuristic_candidate_engine_aabb.riichi_dama_branch import (
        jsonl_text,
    )

    path.write_bytes(jsonl_text(lines).encode("utf-8"))
