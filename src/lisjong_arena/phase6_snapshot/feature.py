"""Pure player-safe Phase 6 history-conditioned snapshot features."""

from dataclasses import dataclass

from lisjong.belief import derive_remaining_tile_inventory, tile_type_index
from lisjong_engine.observation import SeatObservation
from lisjong_engine.public_state import PublicMeldType, PublicRiichiStatus
from lisjong_engine.round_event import DrawSource
from lisjong_engine.round_evidence import (
    DiscardEvidence,
    DoraIndicatorRevealedEvidence,
    DrawEvidence,
    KanConfirmedEvidence,
    KanDeclaredEvidence,
    MeldCalledEvidence,
    ResponseEpochClosedEvidence,
    ResponseEpochOpenedEvidence,
    ResponseOutcome,
    ResponseTrigger,
    RiichiDeclaredEvidence,
    RiichiEstablishedEvidence,
    RiichiFailedEvidence,
    RoundEndedEvidence,
    RoundStartedEvidence,
)
from lisjong_engine.seat import Seat
from lisjong_engine.wind import Wind

from lisjong_arena.lisjong_engine.domain_conversion import tile_from_public_tile
from lisjong_arena.lisjong_engine.policy_input import build_policy_input
from lisjong_arena.phase2_training_anchor.player_safe_anchor import (
    FrozenPlayerSafeAnchor,
)

FEATURE_SEMANTICS_ID = "phase6-history-snapshot-v1"
TILE_TYPE_COUNT = 34

_SEATS = tuple(Seat)
_WINDS = tuple(Wind)
_DRAW_SOURCES = tuple(DrawSource)
_MELD_TYPES = tuple(PublicMeldType)
_RIICHI_STATUSES = tuple(PublicRiichiStatus)
_RESPONSE_TRIGGERS = tuple(ResponseTrigger)
_RESPONSE_OUTCOMES = tuple(ResponseOutcome)


def _zeros() -> list[int]:
    return [0] * TILE_TYPE_COUNT


def _tile_index(tile) -> int:
    return tile_type_index(tile_from_public_tile(tile).tile_type)


def _seat_index(seat: Seat) -> int:
    return _SEATS.index(seat)


def _wind_for_seat(seat: Seat, dealer: Seat) -> Wind:
    return _WINDS[(_seat_index(seat) - _seat_index(dealer)) % len(_SEATS)]


def _seat_for_wind(wind: Wind, dealer: Seat) -> Seat:
    return _SEATS[(_seat_index(dealer) + _WINDS.index(wind)) % len(_SEATS)]


@dataclass(frozen=True, slots=True)
class OpponentSnapshotFeature:
    wind: Wind
    concealed_slot_count: int
    riichi_status: PublicRiichiStatus
    public_meld_tile_counts: tuple[int, ...]
    meld_kind_counts: tuple[int, ...]
    discard_counts: tuple[int, ...]
    tedashi_counts: tuple[int, ...]
    tsumogiri_counts: tuple[int, ...]
    last_discard_orders: tuple[int, ...]
    last_discard_present: tuple[int, ...]
    public_draw_source_counts: tuple[int, ...]
    riichi_declaration_present: int
    riichi_declaration_discard_order: int
    last_call_evidence_position: int
    last_call_present: int
    last_kan_evidence_position: int
    last_kan_present: int
    discard_no_public_response_counts: tuple[int, ...]
    kakan_no_public_response_count: int
    ankan_no_public_response_count: int


@dataclass(frozen=True, slots=True)
class Phase6SnapshotFeature:
    """Immutable raw feature value with no label or provenance identity."""

    viewer_wind: Wind
    prevailing_wind: Wind
    dealer_relation: int
    hand_number: int
    honba: int
    riichi_sticks: int
    remaining_live_wall_count: int
    global_public_discard_count: int
    evidence_prefix_length: int
    scores_by_wind: tuple[int, ...]
    own_base_tile_counts: tuple[int, ...]
    remaining_tile_counts: tuple[int, ...]
    visible_dora_indicator_counts: tuple[int, ...]
    opponents: tuple[OpponentSnapshotFeature, ...]
    response_history_counts: tuple[int, ...]


@dataclass(slots=True)
class _History:
    discard_counts: list[int]
    tedashi_counts: list[int]
    tsumogiri_counts: list[int]
    last_discard_orders: list[int]
    last_discard_present: list[int]
    draw_source_counts: list[int]
    riichi_declaration_order: int | None
    riichi_established: bool
    last_call_position: int | None
    last_kan_position: int | None
    discard_no_response: list[int]
    kakan_no_response: int
    ankan_no_response: int


def _new_history() -> _History:
    return _History(
        _zeros(),
        _zeros(),
        _zeros(),
        _zeros(),
        _zeros(),
        [0] * len(_DRAW_SOURCES),
        None,
        False,
        None,
        None,
        _zeros(),
        0,
        0,
    )


@dataclass(frozen=True, slots=True)
class _PendingTrigger:
    trigger: ResponseTrigger
    source_seat: Seat
    tile_index: int | None
    discard_order: int | None


@dataclass(frozen=True, slots=True)
class _OpenEpoch:
    trigger: ResponseTrigger
    source_seat: Seat
    responder_seats: tuple[Seat, ...]
    tile_index: int | None


def _parse_history(
    anchor: FrozenPlayerSafeAnchor,
) -> tuple[dict[Seat, _History], tuple[int, ...]]:
    observation = anchor.observation
    evidence = anchor.evidence
    if not evidence or not isinstance(evidence[0], RoundStartedEvidence):
        raise ValueError("evidence prefix must begin with RoundStartedEvidence")
    started = evidence[0]
    if (
        started.dealer_seat is not observation.dealer_seat
        or started.prevailing_wind is not observation.prevailing_wind
    ):
        raise ValueError("round-start evidence and observation differ")

    histories = {seat: _new_history() for seat in _SEATS}
    response_counts = [0] * (len(_RESPONSE_TRIGGERS) * len(_RESPONSE_OUTCOMES))
    pending: _PendingTrigger | None = None
    opened: _OpenEpoch | None = None
    evidence_discards: list[DiscardEvidence] = []
    called_orders: dict[int, Seat] = {}

    for position, item in enumerate(evidence):
        if position and isinstance(item, RoundStartedEvidence):
            raise ValueError("evidence prefix contains multiple round starts")
        if isinstance(item, RoundStartedEvidence):
            continue
        if isinstance(item, DrawEvidence):
            histories[item.seat].draw_source_counts[
                _DRAW_SOURCES.index(item.source)
            ] += 1
            continue
        if isinstance(item, DiscardEvidence):
            if pending is not None or opened is not None:
                raise ValueError(
                    "discard appears before the prior response epoch closes"
                )
            if item.order != len(evidence_discards):
                raise ValueError("discard evidence orders must be contiguous from zero")
            tile_index = _tile_index(item.tile)
            history = histories[item.seat]
            history.discard_counts[tile_index] += 1
            (history.tsumogiri_counts if item.is_tsumogiri else history.tedashi_counts)[
                tile_index
            ] += 1
            history.last_discard_orders[tile_index] = item.order
            history.last_discard_present[tile_index] = 1
            evidence_discards.append(item)
            pending = _PendingTrigger(
                ResponseTrigger.DISCARD, item.seat, tile_index, item.order
            )
            continue
        if isinstance(item, RiichiDeclaredEvidence):
            if (
                pending is None
                or pending.trigger is not ResponseTrigger.DISCARD
                or pending.source_seat is not item.seat
                or pending.discard_order != item.declaration_discard_order
            ):
                raise ValueError("riichi declaration is not paired with its discard")
            history = histories[item.seat]
            if history.riichi_declaration_order is not None:
                raise ValueError("seat contains multiple riichi declarations")
            history.riichi_declaration_order = item.declaration_discard_order
            continue
        if isinstance(item, RiichiEstablishedEvidence):
            history = histories[item.seat]
            if history.riichi_declaration_order is None or history.riichi_established:
                raise ValueError("riichi establishment lacks one prior declaration")
            history.riichi_established = True
            continue
        if isinstance(item, RiichiFailedEvidence):
            history = histories[item.seat]
            if history.riichi_declaration_order is None or history.riichi_established:
                raise ValueError("riichi failure lacks one pending declaration")
            continue
        if isinstance(item, KanDeclaredEvidence):
            if pending is not None or opened is not None:
                raise ValueError("kan declaration overlaps a response epoch")
            if item.meld.meld_type is PublicMeldType.KAKAN:
                trigger = ResponseTrigger.KAKAN
            elif item.meld.meld_type is PublicMeldType.ANKAN:
                trigger = ResponseTrigger.ANKAN
            else:
                raise ValueError("kan declaration must be kakan or ankan")
            histories[item.seat].last_kan_position = position
            pending = _PendingTrigger(trigger, item.seat, None, None)
            continue
        if isinstance(item, ResponseEpochOpenedEvidence):
            if opened is not None or pending is None:
                raise ValueError("response epoch opening lacks one public trigger")
            if (
                item.trigger is not pending.trigger
                or item.source_seat is not pending.source_seat
            ):
                raise ValueError("response epoch opening and trigger differ")
            opened = _OpenEpoch(
                item.trigger,
                item.source_seat,
                item.responder_seats,
                pending.tile_index,
            )
            pending = None
            continue
        if isinstance(item, ResponseEpochClosedEvidence):
            if opened is None:
                raise ValueError("response epoch closing lacks an opening")
            if (
                item.trigger is not opened.trigger
                or item.source_seat is not opened.source_seat
            ):
                raise ValueError("response epoch closing and opening differ")
            response_index = _RESPONSE_TRIGGERS.index(item.trigger) * len(
                _RESPONSE_OUTCOMES
            ) + _RESPONSE_OUTCOMES.index(item.outcome)
            response_counts[response_index] += 1
            if item.outcome is ResponseOutcome.NO_PUBLIC_RESPONSE:
                for responder in opened.responder_seats:
                    history = histories[responder]
                    if item.trigger is ResponseTrigger.DISCARD:
                        if opened.tile_index is None:
                            raise ValueError("discard response epoch lacks a tile")
                        history.discard_no_response[opened.tile_index] += 1
                    elif item.trigger is ResponseTrigger.KAKAN:
                        history.kakan_no_response += 1
                    else:
                        history.ankan_no_response += 1
            opened = None
            continue
        if isinstance(item, MeldCalledEvidence):
            if pending is not None or opened is not None:
                raise ValueError(
                    "meld call appears inside an unresolved response epoch"
                )
            if item.called_discard_order >= len(evidence_discards):
                raise ValueError("meld call refers to an unknown discard")
            discard = evidence_discards[item.called_discard_order]
            if item.meld.from_seat is not discard.seat:
                raise ValueError("meld call source differs from the called discard")
            if item.called_discard_order in called_orders:
                raise ValueError("one discard is called more than once")
            called_orders[item.called_discard_order] = item.seat
            histories[item.seat].last_call_position = position
            continue
        if isinstance(item, KanConfirmedEvidence):
            if pending is not None:
                if pending.trigger is not ResponseTrigger.ANKAN:
                    raise ValueError("kan confirmation precedes its response epoch")
                if pending.source_seat is not item.seat:
                    raise ValueError("ankan confirmation and declaration seats differ")
                pending = None
            if opened is not None:
                raise ValueError("kan confirmation precedes response epoch closure")
            continue
        if isinstance(item, DoraIndicatorRevealedEvidence):
            if pending is not None or opened is not None:
                raise ValueError("dora reveal precedes kan response resolution")
            continue
        if isinstance(item, RoundEndedEvidence):
            raise ValueError("a TURN anchor cannot include round-ended evidence")
        raise TypeError(f"unsupported RoundEvidence value: {type(item).__name__}")

    if pending is not None or opened is not None:
        raise ValueError("evidence prefix ends with an unresolved response epoch")

    observed_discards = sorted(
        (
            (seat_discards.seat, discard)
            for seat_discards in observation.discards
            for discard in seat_discards.discards
        ),
        key=lambda value: value[1].order,
    )
    if len(observed_discards) != len(evidence_discards):
        raise ValueError("observation and evidence discard counts differ")
    for (seat, observed), historical in zip(
        observed_discards, evidence_discards, strict=True
    ):
        if (
            seat is not historical.seat
            or observed.tile != historical.tile
            or observed.is_tsumogiri != historical.is_tsumogiri
            or observed.order != historical.order
            or observed.is_riichi_declaration != historical.is_riichi_declaration
            or observed.called_by != called_orders.get(observed.order)
        ):
            raise ValueError("observation and discard evidence differ")
    return histories, tuple(response_counts)


def _counts(tiles) -> tuple[int, ...]:
    counts = _zeros()
    for tile in tiles:
        counts[_tile_index(tile)] += 1
    return tuple(counts)


def build_phase6_snapshot_feature(
    anchor: FrozenPlayerSafeAnchor,
) -> Phase6SnapshotFeature:
    """Build the formal feature from the frozen player-safe anchor only."""
    if not isinstance(anchor, FrozenPlayerSafeAnchor):
        raise TypeError("anchor must be a FrozenPlayerSafeAnchor")
    observation: SeatObservation = anchor.observation
    histories, response_counts = _parse_history(anchor)
    dealer = observation.dealer_seat
    viewer_wind = _wind_for_seat(observation.viewer_seat, dealer)
    opponent_winds = tuple(wind for wind in _WINDS if wind is not viewer_wind)
    policy_input = build_policy_input(observation)
    remaining = derive_remaining_tile_inventory(policy_input).remaining_tile_counts
    if any(type(count) is not int or not 0 <= count <= 4 for count in remaining):
        raise ValueError("remaining tile inventory must contain integer counts in 0..4")

    scores_by_seat = {value.seat: value.points for value in observation.scores}
    scores_by_wind = tuple(
        scores_by_seat[_seat_for_wind(wind, dealer)] for wind in _WINDS
    )
    melds_by_seat = {value.seat: value.melds for value in observation.melds}
    riichi_by_seat = {value.seat: value.status for value in observation.riichi_states}
    for seat, status in riichi_by_seat.items():
        history = histories[seat]
        if (
            status is PublicRiichiStatus.NONE
            and history.riichi_declaration_order is not None
        ):
            raise ValueError("public riichi snapshot and declaration history differ")
        if status is PublicRiichiStatus.PENDING and (
            history.riichi_declaration_order is None or history.riichi_established
        ):
            raise ValueError("pending riichi snapshot and declaration history differ")
        if status is PublicRiichiStatus.ESTABLISHED and not history.riichi_established:
            raise ValueError("established riichi snapshot and history differ")
    opponents = []
    for wind in opponent_winds:
        seat = _seat_for_wind(wind, dealer)
        melds = melds_by_seat[seat]
        meld_tile_counts = _counts(tile for meld in melds for tile in meld.tiles)
        meld_kind_counts = tuple(
            sum(meld.meld_type is meld_type for meld in melds)
            for meld_type in _MELD_TYPES
        )
        history = histories[seat]
        declared = history.riichi_declaration_order
        opponents.append(
            OpponentSnapshotFeature(
                wind=wind,
                concealed_slot_count=13 - 3 * len(melds),
                riichi_status=riichi_by_seat[seat],
                public_meld_tile_counts=meld_tile_counts,
                meld_kind_counts=meld_kind_counts,
                discard_counts=tuple(history.discard_counts),
                tedashi_counts=tuple(history.tedashi_counts),
                tsumogiri_counts=tuple(history.tsumogiri_counts),
                last_discard_orders=tuple(history.last_discard_orders),
                last_discard_present=tuple(history.last_discard_present),
                public_draw_source_counts=tuple(history.draw_source_counts),
                riichi_declaration_present=int(declared is not None),
                riichi_declaration_discard_order=0 if declared is None else declared,
                last_call_evidence_position=(
                    0
                    if history.last_call_position is None
                    else history.last_call_position
                ),
                last_call_present=int(history.last_call_position is not None),
                last_kan_evidence_position=(
                    0
                    if history.last_kan_position is None
                    else history.last_kan_position
                ),
                last_kan_present=int(history.last_kan_position is not None),
                discard_no_public_response_counts=tuple(history.discard_no_response),
                kakan_no_public_response_count=history.kakan_no_response,
                ankan_no_public_response_count=history.ankan_no_response,
            )
        )
    if any(value.concealed_slot_count < 0 for value in opponents):
        raise ValueError("public meld count implies negative concealed slots")

    return Phase6SnapshotFeature(
        viewer_wind=viewer_wind,
        prevailing_wind=observation.prevailing_wind,
        dealer_relation=(_seat_index(dealer) - _seat_index(observation.viewer_seat))
        % 4,
        hand_number=observation.hand_number,
        honba=observation.honba,
        riichi_sticks=observation.riichi_sticks,
        remaining_live_wall_count=observation.remaining_live_wall_count,
        global_public_discard_count=sum(
            sum(value.discard_counts) for value in opponents
        )
        + sum(histories[observation.viewer_seat].discard_counts),
        evidence_prefix_length=len(anchor.evidence),
        scores_by_wind=scores_by_wind,
        own_base_tile_counts=_counts(observation.hand_tiles),
        remaining_tile_counts=tuple(remaining),
        visible_dora_indicator_counts=_counts(observation.dora_indicators),
        opponents=tuple(opponents),
        response_history_counts=response_counts,
    )


__all__ = [
    "FEATURE_SEMANTICS_ID",
    "OpponentSnapshotFeature",
    "Phase6SnapshotFeature",
    "build_phase6_snapshot_feature",
]
