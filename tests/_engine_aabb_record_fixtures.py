"""#513 engine版AABB局単位recordのtest用builder。

半荘は実engineの``MatchState``で東1局0本場・25000点から進める。各局は
#472のfixture（``test_engine_aabb_half_settlement_fixtures``）と同じ方法で
終局factを直接与えて``settle_active_round()``を呼ぶ。和了の手も#472と同じ
七対子（ロン 子1600 / 親2400、ツモ 子800/1600 / 親1600オール）を使う。
立直供託は``RoundState.riichi_contributions``を差し替えて与える。
private fieldへの依存はtestだけに閉じる。

script X（10局、TARGET_REACHED、終局供託あり）

```text
K0 E1-0  E立直。S が W からロン 1600、供託1本は S へ
K1 E2-0  N立直。荒牌、聴牌 S N（親S続行）。ノーテン罰符 E W -> S N 各750
K2 E2-1  供託1本。W から E と N のダブロン 各1600。本場300と供託は下家側の N
K3 E3-0  途中流局（四風連打）、親続行
K4 E3-1  S（子）ツモ 800/1600 + 本場各100
K5〜K8   全員ノーテンの荒牌（E4-0 -> S1-1 -> S2-2 -> S3-3）
K9 S4-4  E立直。荒牌、聴牌 E のみ（親N流れ）。S W N -> E 各1000。供託1本残り
終局      S 31600 が首位で30000以上 -> 残供託1000は S、最終素点 25200/32600/15700/26500
```

script Y（8局、BANKRUPTCY）: 東1局で親Eが N から連続ロン（2400 + 本場）。
8回目で N が -2600 になり飛ぶ。
"""

from unittest import mock

from lisjong_engine.match_state import MatchPhase, MatchState
from lisjong_engine.public_state import PublicMeld, PublicMeldType, PublicTile
from lisjong_engine.riichi_event import RiichiContribution
from lisjong_engine.round_event import DrawSource
from lisjong_engine.round_evidence import (
    DiscardEvidence,
    DrawEvidence,
    KanConfirmedEvidence,
    KanDeclaredEvidence,
    MeldCalledEvidence,
    RiichiDeclaredEvidence,
    RiichiEstablishedEvidence,
    RoundStartedEvidence,
)
from lisjong_engine.round_evidence_completion import (
    RoundEvidenceCompletion,
    SeatRoundEvidence,
)
from lisjong_engine.round_phase import RoundPhase
from lisjong_engine.round_result import (
    AbortiveDrawReason,
    AbortiveDrawResult,
    ExhaustiveDrawResult,
)
from lisjong_engine.round_state import RoundState
from lisjong_engine.rules import RuleSet
from lisjong_engine.seat import Seat
from lisjong_engine.tile import TileCategory, TileType
from test_engine_aabb_half_settlement_fixtures import _win

from lisjong_arena.heuristic_candidate_engine_aabb import (
    HanchanCapture,
    SeatAssignment,
    placement_roles,
)

E, S, W, N = Seat.EAST, Seat.SOUTH, Seat.WEST, Seat.NORTH
RULES = RuleSet.default()
CANDIDATE_IDENTITY = "candidate-policy@test"
INCUMBENT_IDENTITY = "incumbent-policy@test"
PRODUCER_REVISION = "a" * 40


def _ron(*winners, source):
    return lambda position: _win(position, winners, source=source)


def _tsumo(winner):
    return lambda position: _win(position, (winner,))


def _draw(*tenpai):
    return lambda position: ExhaustiveDrawResult(tenpai_seats=tenpai)


def _abortive(reason=AbortiveDrawReason.FOUR_WINDS):
    return lambda position: AbortiveDrawResult(reason)


SCRIPT_X = (
    (_ron(S, source=W), (E,)),
    (_draw(S, N), (N,)),
    (_ron(E, N, source=W), ()),
    (_abortive(), ()),
    (_tsumo(S), ()),
    (_draw(), ()),
    (_draw(), ()),
    (_draw(), ()),
    (_draw(), ()),
    (_draw(E), (E,)),
)
SCRIPT_Y = tuple((_ron(E, source=N), ()) for _ in range(8))


def play(seed, script):
    """``script``の各局を順に精算し、終局した``CompletedMatch``を返す。"""
    match = MatchState(seed=seed, rules=RULES)
    for index, (make_result, riichi_seats) in enumerate(script):
        round_state = match.start_round()
        result = make_result(match.position)
        round_state._phase = RoundPhase.FINISHED
        round_state._result = result
        contributions = tuple(
            RiichiContribution(seat, RULES.riichi_stick_points) for seat in riichi_seats
        )
        with mock.patch.object(
            RoundState,
            "riichi_contributions",
            new_callable=mock.PropertyMock,
            return_value=contributions,
        ):
            match.settle_active_round()
        finished = match.phase is MatchPhase.FINISHED
        if finished is not (index == len(script) - 1):
            raise AssertionError(f"script ended at kyoku {index} unexpectedly")
    return match.completed_match


# ---------------------------------------------------------------------------
# synthetic public evidence（契約試験用。実candidate経由の槓適用の根拠ではない）
# ---------------------------------------------------------------------------


def tile(category, rank, *, red=False):
    return PublicTile(TileType(category, rank), red)


M = TileCategory.MANZU
P = TileCategory.PINZU
Z = TileCategory.HONOR


def riichi_evidence(seat, *, own_discards, first_order):
    """``seat``が``own_discards``枚目の打牌で立直を宣言し、成立する公開evidence。"""
    items = []
    order = first_order
    for count in range(1, own_discards + 1):
        items.append(
            DiscardEvidence(
                seat=seat,
                tile=tile(M, 1 + count % 9),
                is_tsumogiri=False,
                order=order,
                is_riichi_declaration=count == own_discards,
            )
        )
        order += 4
    declaration = order - 4
    items.append(
        RiichiDeclaredEvidence(seat, tile(M, 1 + own_discards % 9), declaration)
    )
    items.append(RiichiEstablishedEvidence(seat))
    return items


def pon(seat, *, from_seat, order):
    called = tile(Z, 5)
    return MeldCalledEvidence(
        seat=seat,
        meld=PublicMeld(
            PublicMeldType.PON, (called, called, called), from_seat, called
        ),
        called_discard_order=order,
    )


def daiminkan(seat, *, from_seat, order):
    called = tile(P, 5, red=True)
    plain = tile(P, 5)
    return MeldCalledEvidence(
        seat=seat,
        meld=PublicMeld(
            PublicMeldType.DAIMINKAN, (plain, plain, plain, called), from_seat, called
        ),
        called_discard_order=order,
    )


def ankan(seat):
    tiles = (tile(Z, 1),) * 4
    meld = PublicMeld(PublicMeldType.ANKAN, tiles, None, None)
    return [KanDeclaredEvidence(seat, meld), KanConfirmedEvidence(seat, meld)]


def completion(completed_round, public=(), *, private_draws=True, viewer_extra=None):
    """1局の``RoundEvidenceCompletion``。各viewerに自分のツモ牌（private）を加える。"""
    position = completed_round.position_before
    projections = []
    for viewer in Seat:
        evidence = [
            RoundStartedEvidence(position.dealer_seat, position.prevailing_wind)
        ]
        if private_draws:
            for seat in Seat:
                evidence.append(
                    DrawEvidence(
                        seat=seat,
                        source=DrawSource.LIVE_WALL,
                        tile=tile(P, 9) if seat is viewer else None,
                    )
                )
        evidence.extend(public)
        if viewer_extra is not None:
            evidence.extend(viewer_extra.get(viewer, ()))
        projections.append(SeatRoundEvidence(viewer, tuple(evidence)))
    return RoundEvidenceCompletion(
        prevailing_wind=position.prevailing_wind,
        hand_number=position.hand_number,
        dealer_seat=position.dealer_seat,
        honba=position.honba,
        projections=tuple(projections),
    )


def script_x_public_evidence():
    """script Xの局ごとの公開evidence（立直は第1層の供託と一致させる）。"""
    by_kyoku = {
        0: riichi_evidence(E, own_discards=3, first_order=0),
        1: riichi_evidence(N, own_discards=7, first_order=3),
        2: [daiminkan(N, from_seat=W, order=6), *ankan(E)],
        4: [pon(S, from_seat=W, order=2)],
        9: riichi_evidence(E, own_discards=10, first_order=0),
    }
    return by_kyoku


def completions_for(match, *, script_x: bool):
    public = script_x_public_evidence() if script_x else {}
    return tuple(
        completion(completed, public.get(index, ()))
        for index, completed in enumerate(match.history)
    )


def seats_for(placement):
    return tuple(
        SeatAssignment(
            role,
            CANDIDATE_IDENTITY if role == "A" else INCUMBENT_IDENTITY,
        )
        for role in placement_roles(placement)
    )


def capture(seed, placement, script, *, round_evidence="auto", calls=(40, 41, 42, 43)):
    match = play(seed, script)
    if round_evidence == "auto":
        round_evidence = completions_for(match, script_x=script is SCRIPT_X)
    return HanchanCapture(
        match=match,
        seed=seed,
        placement=placement,
        seats=seats_for(placement),
        selector_call_counts=tuple(calls),
        policy_call_counts=tuple(calls),
        round_evidence=round_evidence,
    )


SEED_MIXED = 513001
SEED_PLAIN = 513002


def two_block_captures():
    """seed 513001: placement 0 が script X、1..5 が Y。seed 513002: 全配置 Y。"""
    first = [
        capture(SEED_MIXED, placement, SCRIPT_X if placement == 0 else SCRIPT_Y)
        for placement in range(6)
    ]
    second = [capture(SEED_PLAIN, placement, SCRIPT_Y) for placement in range(6)]
    return first + second
