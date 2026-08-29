"""Strict Phase 3 JSON values -> existing player-safe domain values。"""

import re

from lisjong.policy_contract import Seat as LisjongSeat
from lisjong.policy_contract import Wind as LisjongWind
from lisjong_engine.observation import ObservationDecisionKind, SeatObservation
from lisjong_engine.public_state import (
    PublicDiscard,
    PublicMeld,
    PublicMeldType,
    PublicRiichiStatus,
    PublicTile,
    SeatDiscards,
    SeatMelds,
    SeatRiichiState,
    SeatScore,
)
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
    RoundEndKind,
    RoundEvidence,
    RoundStartedEvidence,
)
from lisjong_engine.round_result import AbortiveDrawReason
from lisjong_engine.seat import Seat as EngineSeat
from lisjong_engine.tile import STANDARD_TILE_TYPES
from lisjong_engine.win_context import WinMethod
from lisjong_engine.wind import Wind as EngineWind

from lisjong_arena.phase2_training_anchor.pipeline_provenance import (
    SourceRevisions,
    TrainingPipelineProvenance,
)
from lisjong_arena.phase2_training_anchor.rule_provenance import EffectiveRuleProvenance
from lisjong_arena.phase2_training_anchor.training_labels import OpponentIdentity
from lisjong_arena.phase3_bootstrap_corpus.model import Phase3BootstrapArtifactError

_FULL_COMMIT_ID = re.compile(r"[0-9a-f]{40}").fullmatch
_SHA256 = re.compile(r"[0-9a-f]{64}").fullmatch


def expect_object(value: object, keys: set[str], context: str) -> dict[str, object]:
    if type(value) is not dict:
        raise Phase3BootstrapArtifactError(f"{context} must be an object")
    if set(value) != keys:
        raise Phase3BootstrapArtifactError(f"{context} fields are invalid")
    return value


def expect_list(value: object, context: str) -> list[object]:
    if type(value) is not list:
        raise Phase3BootstrapArtifactError(f"{context} must be an array")
    return value


def expect_str(value: object, context: str) -> str:
    if type(value) is not str:
        raise Phase3BootstrapArtifactError(f"{context} must be a string")
    return value


def expect_nonempty_str(value: object, context: str) -> str:
    result = expect_str(value, context)
    if not result:
        raise Phase3BootstrapArtifactError(f"{context} must not be empty")
    return result


def expect_int(value: object, context: str) -> int:
    if type(value) is not int:
        raise Phase3BootstrapArtifactError(f"{context} must be an integer")
    return value


def expect_bool(value: object, context: str) -> bool:
    if type(value) is not bool:
        raise Phase3BootstrapArtifactError(f"{context} must be a boolean")
    return value


def parse_enum(enum_type, value: object, context: str):
    raw = expect_str(value, context)
    try:
        return enum_type(raw)
    except ValueError as exc:
        raise Phase3BootstrapArtifactError(f"{context} enum value is invalid") from exc


def parse_nullable_enum(enum_type, value: object, context: str):
    return None if value is None else parse_enum(enum_type, value, context)


def parse_rule_provenance(value: object, context: str) -> EffectiveRuleProvenance:
    raw = expect_object(value, {"fingerprint", "name", "version"}, context)
    fingerprint = expect_str(raw["fingerprint"], f"{context}.fingerprint")
    if _SHA256(fingerprint) is None:
        raise Phase3BootstrapArtifactError(f"{context}.fingerprint is invalid")
    try:
        return EffectiveRuleProvenance(
            name=expect_nonempty_str(raw["name"], f"{context}.name"),
            version=expect_int(raw["version"], f"{context}.version"),
            fingerprint=fingerprint,
        )
    except (TypeError, ValueError) as exc:
        raise Phase3BootstrapArtifactError(f"{context} is invalid") from exc


def parse_provenance(value: object) -> TrainingPipelineProvenance:
    raw = expect_object(
        value,
        {
            "anchor_semantics_id",
            "effective_rules",
            "evidence_cutoff_semantics_id",
            "label_semantics_id",
            "source_revisions",
        },
        "provenance",
    )
    revisions = expect_object(
        raw["source_revisions"],
        {"lisjong", "lisjong_arena", "lisjong_engine"},
        "provenance.source_revisions",
    )
    parsed_revisions: dict[str, str] = {}
    for name in ("lisjong", "lisjong_engine", "lisjong_arena"):
        revision = expect_str(revisions[name], f"provenance.source_revisions.{name}")
        if _FULL_COMMIT_ID(revision) is None:
            raise Phase3BootstrapArtifactError(
                f"provenance.source_revisions.{name} is not a full commit ID"
            )
        parsed_revisions[name] = revision
    try:
        return TrainingPipelineProvenance(
            source_revisions=SourceRevisions(**parsed_revisions),
            anchor_semantics_id=expect_nonempty_str(
                raw["anchor_semantics_id"], "provenance.anchor_semantics_id"
            ),
            evidence_cutoff_semantics_id=expect_nonempty_str(
                raw["evidence_cutoff_semantics_id"],
                "provenance.evidence_cutoff_semantics_id",
            ),
            label_semantics_id=expect_nonempty_str(
                raw["label_semantics_id"], "provenance.label_semantics_id"
            ),
            effective_rules=parse_rule_provenance(
                raw["effective_rules"], "provenance.effective_rules"
            ),
        )
    except (TypeError, ValueError) as exc:
        raise Phase3BootstrapArtifactError("provenance is invalid") from exc


def parse_tile(value: object, context: str) -> PublicTile:
    raw = expect_object(value, {"is_red", "tile_type_id"}, context)
    tile_type_id = expect_int(raw["tile_type_id"], f"{context}.tile_type_id")
    if not 0 <= tile_type_id < len(STANDARD_TILE_TYPES):
        raise Phase3BootstrapArtifactError(f"{context}.tile_type_id is invalid")
    try:
        return PublicTile(
            STANDARD_TILE_TYPES[tile_type_id],
            expect_bool(raw["is_red"], f"{context}.is_red"),
        )
    except (TypeError, ValueError) as exc:
        raise Phase3BootstrapArtifactError(f"{context} is invalid") from exc


def parse_discard(value: object, context: str) -> PublicDiscard:
    raw = expect_object(
        value,
        {"called_by", "is_riichi_declaration", "is_tsumogiri", "order", "tile"},
        context,
    )
    try:
        return PublicDiscard(
            tile=parse_tile(raw["tile"], f"{context}.tile"),
            is_tsumogiri=expect_bool(raw["is_tsumogiri"], f"{context}.is_tsumogiri"),
            order=expect_int(raw["order"], f"{context}.order"),
            is_riichi_declaration=expect_bool(
                raw["is_riichi_declaration"], f"{context}.is_riichi_declaration"
            ),
            called_by=parse_nullable_enum(
                EngineSeat, raw["called_by"], f"{context}.called_by"
            ),
        )
    except (TypeError, ValueError) as exc:
        if isinstance(exc, Phase3BootstrapArtifactError):
            raise
        raise Phase3BootstrapArtifactError(f"{context} is invalid") from exc


def parse_meld(value: object, context: str) -> PublicMeld:
    raw = expect_object(
        value, {"called_tile", "from_seat", "meld_type", "tiles"}, context
    )
    try:
        return PublicMeld(
            meld_type=parse_enum(
                PublicMeldType, raw["meld_type"], f"{context}.meld_type"
            ),
            tiles=tuple(
                parse_tile(tile, f"{context}.tiles[{index}]")
                for index, tile in enumerate(
                    expect_list(raw["tiles"], f"{context}.tiles")
                )
            ),
            from_seat=parse_nullable_enum(
                EngineSeat, raw["from_seat"], f"{context}.from_seat"
            ),
            called_tile=(
                None
                if raw["called_tile"] is None
                else parse_tile(raw["called_tile"], f"{context}.called_tile")
            ),
        )
    except (TypeError, ValueError) as exc:
        if isinstance(exc, Phase3BootstrapArtifactError):
            raise
        raise Phase3BootstrapArtifactError(f"{context} is invalid") from exc


def _parse_seat_discards(value: object, context: str) -> SeatDiscards:
    raw = expect_object(value, {"discards", "seat"}, context)
    return SeatDiscards(
        seat=parse_enum(EngineSeat, raw["seat"], f"{context}.seat"),
        discards=tuple(
            parse_discard(item, f"{context}.discards[{index}]")
            for index, item in enumerate(
                expect_list(raw["discards"], f"{context}.discards")
            )
        ),
    )


def _parse_seat_melds(value: object, context: str) -> SeatMelds:
    raw = expect_object(value, {"melds", "seat"}, context)
    return SeatMelds(
        seat=parse_enum(EngineSeat, raw["seat"], f"{context}.seat"),
        melds=tuple(
            parse_meld(item, f"{context}.melds[{index}]")
            for index, item in enumerate(expect_list(raw["melds"], f"{context}.melds"))
        ),
    )


def _parse_score(value: object, context: str) -> SeatScore:
    raw = expect_object(value, {"points", "seat"}, context)
    return SeatScore(
        seat=parse_enum(EngineSeat, raw["seat"], f"{context}.seat"),
        points=expect_int(raw["points"], f"{context}.points"),
    )


def _parse_riichi_state(value: object, context: str) -> SeatRiichiState:
    raw = expect_object(value, {"seat", "status"}, context)
    return SeatRiichiState(
        seat=parse_enum(EngineSeat, raw["seat"], f"{context}.seat"),
        status=parse_enum(PublicRiichiStatus, raw["status"], f"{context}.status"),
    )


def parse_observation(value: object, context: str) -> SeatObservation:
    raw = expect_object(
        value,
        {
            "dealer_seat",
            "decision_kind",
            "discards",
            "dora_indicators",
            "drawn_tile",
            "hand_number",
            "hand_tiles",
            "honba",
            "melds",
            "prevailing_wind",
            "remaining_live_wall_count",
            "riichi_states",
            "riichi_sticks",
            "scores",
            "viewer_seat",
        },
        context,
    )
    try:
        return SeatObservation(
            viewer_seat=parse_enum(
                EngineSeat, raw["viewer_seat"], f"{context}.viewer_seat"
            ),
            decision_kind=parse_enum(
                ObservationDecisionKind,
                raw["decision_kind"],
                f"{context}.decision_kind",
            ),
            hand_number=expect_int(raw["hand_number"], f"{context}.hand_number"),
            honba=expect_int(raw["honba"], f"{context}.honba"),
            riichi_sticks=expect_int(raw["riichi_sticks"], f"{context}.riichi_sticks"),
            hand_tiles=tuple(
                parse_tile(item, f"{context}.hand_tiles[{index}]")
                for index, item in enumerate(
                    expect_list(raw["hand_tiles"], f"{context}.hand_tiles")
                )
            ),
            drawn_tile=(
                None
                if raw["drawn_tile"] is None
                else parse_tile(raw["drawn_tile"], f"{context}.drawn_tile")
            ),
            discards=tuple(
                _parse_seat_discards(item, f"{context}.discards[{index}]")
                for index, item in enumerate(
                    expect_list(raw["discards"], f"{context}.discards")
                )
            ),
            melds=tuple(
                _parse_seat_melds(item, f"{context}.melds[{index}]")
                for index, item in enumerate(
                    expect_list(raw["melds"], f"{context}.melds")
                )
            ),
            dora_indicators=tuple(
                parse_tile(item, f"{context}.dora_indicators[{index}]")
                for index, item in enumerate(
                    expect_list(raw["dora_indicators"], f"{context}.dora_indicators")
                )
            ),
            remaining_live_wall_count=expect_int(
                raw["remaining_live_wall_count"],
                f"{context}.remaining_live_wall_count",
            ),
            scores=tuple(
                _parse_score(item, f"{context}.scores[{index}]")
                for index, item in enumerate(
                    expect_list(raw["scores"], f"{context}.scores")
                )
            ),
            dealer_seat=parse_enum(
                EngineSeat, raw["dealer_seat"], f"{context}.dealer_seat"
            ),
            prevailing_wind=parse_enum(
                EngineWind, raw["prevailing_wind"], f"{context}.prevailing_wind"
            ),
            riichi_states=tuple(
                _parse_riichi_state(item, f"{context}.riichi_states[{index}]")
                for index, item in enumerate(
                    expect_list(raw["riichi_states"], f"{context}.riichi_states")
                )
            ),
        )
    except (TypeError, ValueError) as exc:
        if isinstance(exc, Phase3BootstrapArtifactError):
            raise
        raise Phase3BootstrapArtifactError(f"{context} is invalid") from exc


def parse_evidence(value: object, context: str) -> RoundEvidence:
    if type(value) is not dict or type(value.get("type")) is not str:
        raise Phase3BootstrapArtifactError(f"{context} must have a string type")
    kind = value["type"]
    try:
        if kind == "round_started":
            raw = expect_object(
                value, {"dealer_seat", "prevailing_wind", "type"}, context
            )
            return RoundStartedEvidence(
                dealer_seat=parse_enum(
                    EngineSeat, raw["dealer_seat"], f"{context}.dealer_seat"
                ),
                prevailing_wind=parse_enum(
                    EngineWind, raw["prevailing_wind"], f"{context}.prevailing_wind"
                ),
            )
        if kind == "draw":
            raw = expect_object(value, {"seat", "source", "tile", "type"}, context)
            return DrawEvidence(
                seat=parse_enum(EngineSeat, raw["seat"], f"{context}.seat"),
                source=parse_enum(DrawSource, raw["source"], f"{context}.source"),
                tile=(
                    None
                    if raw["tile"] is None
                    else parse_tile(raw["tile"], f"{context}.tile")
                ),
            )
        if kind == "discard":
            raw = expect_object(
                value,
                {
                    "is_riichi_declaration",
                    "is_tsumogiri",
                    "order",
                    "seat",
                    "tile",
                    "type",
                },
                context,
            )
            return DiscardEvidence(
                seat=parse_enum(EngineSeat, raw["seat"], f"{context}.seat"),
                tile=parse_tile(raw["tile"], f"{context}.tile"),
                is_tsumogiri=expect_bool(
                    raw["is_tsumogiri"], f"{context}.is_tsumogiri"
                ),
                order=expect_int(raw["order"], f"{context}.order"),
                is_riichi_declaration=expect_bool(
                    raw["is_riichi_declaration"], f"{context}.is_riichi_declaration"
                ),
            )
        if kind == "response_epoch_opened":
            raw = expect_object(
                value,
                {"responder_seats", "source_seat", "trigger", "type"},
                context,
            )
            return ResponseEpochOpenedEvidence(
                trigger=parse_enum(
                    ResponseTrigger, raw["trigger"], f"{context}.trigger"
                ),
                source_seat=parse_enum(
                    EngineSeat, raw["source_seat"], f"{context}.source_seat"
                ),
                responder_seats=tuple(
                    parse_enum(EngineSeat, seat, f"{context}.responder_seats[{index}]")
                    for index, seat in enumerate(
                        expect_list(
                            raw["responder_seats"], f"{context}.responder_seats"
                        )
                    )
                ),
            )
        if kind == "response_epoch_closed":
            raw = expect_object(
                value, {"outcome", "source_seat", "trigger", "type"}, context
            )
            return ResponseEpochClosedEvidence(
                trigger=parse_enum(
                    ResponseTrigger, raw["trigger"], f"{context}.trigger"
                ),
                source_seat=parse_enum(
                    EngineSeat, raw["source_seat"], f"{context}.source_seat"
                ),
                outcome=parse_enum(
                    ResponseOutcome, raw["outcome"], f"{context}.outcome"
                ),
            )
        if kind == "meld_called":
            raw = expect_object(
                value, {"called_discard_order", "meld", "seat", "type"}, context
            )
            return MeldCalledEvidence(
                seat=parse_enum(EngineSeat, raw["seat"], f"{context}.seat"),
                meld=parse_meld(raw["meld"], f"{context}.meld"),
                called_discard_order=expect_int(
                    raw["called_discard_order"], f"{context}.called_discard_order"
                ),
            )
        if kind in {"kan_declared", "kan_confirmed"}:
            raw = expect_object(value, {"meld", "seat", "type"}, context)
            cls = (
                KanDeclaredEvidence if kind == "kan_declared" else KanConfirmedEvidence
            )
            return cls(
                seat=parse_enum(EngineSeat, raw["seat"], f"{context}.seat"),
                meld=parse_meld(raw["meld"], f"{context}.meld"),
            )
        if kind == "riichi_declared":
            raw = expect_object(
                value,
                {"declaration_discard_order", "seat", "tile", "type"},
                context,
            )
            return RiichiDeclaredEvidence(
                seat=parse_enum(EngineSeat, raw["seat"], f"{context}.seat"),
                tile=parse_tile(raw["tile"], f"{context}.tile"),
                declaration_discard_order=expect_int(
                    raw["declaration_discard_order"],
                    f"{context}.declaration_discard_order",
                ),
            )
        if kind in {"riichi_established", "riichi_failed"}:
            raw = expect_object(value, {"seat", "type"}, context)
            cls = (
                RiichiEstablishedEvidence
                if kind == "riichi_established"
                else RiichiFailedEvidence
            )
            return cls(seat=parse_enum(EngineSeat, raw["seat"], f"{context}.seat"))
        if kind == "dora_indicator_revealed":
            raw = expect_object(value, {"indicator", "seat", "type"}, context)
            return DoraIndicatorRevealedEvidence(
                seat=parse_enum(EngineSeat, raw["seat"], f"{context}.seat"),
                indicator=parse_tile(raw["indicator"], f"{context}.indicator"),
            )
        if kind == "round_ended":
            raw = expect_object(
                value,
                {
                    "abortive_reason",
                    "kind",
                    "source_seat",
                    "type",
                    "win_method",
                    "winner_seats",
                },
                context,
            )
            return RoundEndedEvidence(
                kind=parse_enum(RoundEndKind, raw["kind"], f"{context}.kind"),
                win_method=parse_nullable_enum(
                    WinMethod, raw["win_method"], f"{context}.win_method"
                ),
                winner_seats=tuple(
                    parse_enum(EngineSeat, seat, f"{context}.winner_seats[{index}]")
                    for index, seat in enumerate(
                        expect_list(raw["winner_seats"], f"{context}.winner_seats")
                    )
                ),
                source_seat=parse_nullable_enum(
                    EngineSeat, raw["source_seat"], f"{context}.source_seat"
                ),
                abortive_reason=parse_nullable_enum(
                    AbortiveDrawReason,
                    raw["abortive_reason"],
                    f"{context}.abortive_reason",
                ),
            )
    except (TypeError, ValueError) as exc:
        if isinstance(exc, Phase3BootstrapArtifactError):
            raise
        raise Phase3BootstrapArtifactError(f"{context} is invalid") from exc
    raise Phase3BootstrapArtifactError(f"{context}.type is unsupported: {kind!r}")


def parse_opponent_identity(value: object, context: str) -> OpponentIdentity:
    raw = expect_object(value, {"seat", "viewer_relative_offset", "wind"}, context)
    try:
        return OpponentIdentity(
            seat=LisjongSeat(expect_int(raw["seat"], f"{context}.seat")),
            wind=parse_enum(LisjongWind, raw["wind"], f"{context}.wind"),
            viewer_relative_offset=expect_int(
                raw["viewer_relative_offset"], f"{context}.viewer_relative_offset"
            ),
        )
    except (TypeError, ValueError) as exc:
        if isinstance(exc, Phase3BootstrapArtifactError):
            raise
        raise Phase3BootstrapArtifactError(f"{context} is invalid") from exc
