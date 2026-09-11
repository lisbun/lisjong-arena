"""Successful standard ``LocalGameRunner`` executionsのdurable local record。

Issue #55のsame-process ``LocalGameInspection``を、process終了後にもstrictに
readbackできるArena-owned bundleへ保存する。これはproject-wide canonical
``GameRecord``でもtraining datasetでもなく、standard RiichiEnv local execution
専用のraw execution / decision recordである。

Objective ``GameTrace``、lisjong-owned player-safe ``PolicyInput`` / typed
``DecisionTrace``、existing ``LocalGameResult`` semanticsは別payloadへ保ち、互いの
semanticsを再計算・混在させない。schema v1で対応する``AnalysisTrace``はcurrent
lisjong pinが提供する明示的な4 subtypeだけであり、未知typeをgeneric dataclassや
``repr``へfallbackせずfail closedする。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lisjong.policies.finite_horizon_completion import (
    FiniteHorizonCandidateEvaluation,
    FiniteHorizonCompletionAnalysis,
)
from lisjong.policies.hand_value_aware_two_step_ukeire import (
    HandValueAwareTwoStepUkeireAnalysis,
    HandValueCandidateEvaluation,
)
from lisjong.policies.two_step_ukeire import (
    TwoStepUkeireAnalysis,
    TwoStepUkeireCandidateEvaluation,
)
from lisjong.policies.value_aware_two_step_ukeire import (
    ValueAwareTwoStepUkeireAnalysis,
    ValueAwareTwoStepUkeireCandidateEvaluation,
)
from lisjong.policy_contract import (
    AnalysisTrace,
    AnkanAction,
    ChiAction,
    DaiminkanAction,
    DecisionTrace,
    Discard,
    DiscardAction,
    InternalAction,
    KakanAction,
    KyuushuKyuuhaiAction,
    MeldKind,
    OwnHandState,
    PassAction,
    PlayerPublicState,
    PolicyInput,
    PonAction,
    PublicMeld,
    RiichiAction,
    RiichiState,
    RonAction,
    RoundState,
    Seat,
    Tile,
    TileCategory,
    TileType,
    TsumoAction,
    Wind,
)

from lisjong_arena._artifact_io import (
    ArtifactValidationError,
    canonical_json_text,
    expect_bool,
    expect_int,
    expect_list,
    expect_object,
    expect_optional_bool,
    expect_optional_int,
    expect_str,
    read_json_document,
    write_new_artifact_file,
)
from lisjong_arena.game_trace import GameTrace, GameTraceEvent
from lisjong_arena.model import PolicySpec
from lisjong_arena.riichienv.local_game_runner import (
    LocalGameInspection,
    LocalGameInspectionRecorder,
    LocalGameResult,
    LocalGameRunner,
    SeatDecisionObservation,
    StepDecisionObservation,
)
from lisjong_arena.riichienv.round_result import (
    RoundDrawFact,
    RoundResult,
    RoundWinFact,
    RoundWinScoring,
    RoundYaku,
)
from lisjong_arena.riichienv.round_stats import SeatRoundStats
from lisjong_arena.single_round_artifact import (
    SingleRoundExecutionProvenance,
    collect_execution_provenance,
    execution_provenance_to_dict,
    parse_execution_provenance,
)

LOCAL_GAME_RECORD_SCHEMA_ID = "lisjong-arena-durable-local-game-record"
LOCAL_GAME_RECORD_SCHEMA_VERSION = 2
LOCAL_GAME_RECORD_SCHEMA_VERSION_WITHOUT_ROUND_RESULTS = 1
LOCAL_GAME_RECORD_BACKEND = "riichienv-local-game-runner"

MANIFEST_FILENAME = "manifest.json"
OBJECTIVE_TRACE_FILENAME = "objective_trace.json"
DECISIONS_FILENAME = "decisions.json"
RESULT_FILENAME = "result.json"
ROUND_RESULTS_FILENAME = "round_results.json"

_EXPECTED_FILES = frozenset(
    {
        MANIFEST_FILENAME,
        OBJECTIVE_TRACE_FILENAME,
        DECISIONS_FILENAME,
        RESULT_FILENAME,
        ROUND_RESULTS_FILENAME,
    }
)
_PAYLOAD_NAMES = ("objective_trace", "decisions", "result", "round_results")
_SHA256_LENGTH = 64
_HEX_DIGITS = frozenset("0123456789abcdef")


class DurableLocalGameRecordError(ArtifactValidationError):
    """durable local game recordを生成・検証・readbackできない場合。"""


@dataclass(frozen=True, slots=True)
class PayloadReference:
    """manifestが固定する1 payloadのphysical nameとcontent identity。"""

    filename: str
    sha256: str
    byte_count: int

    def __post_init__(self) -> None:
        if type(self.filename) is not str or not self.filename:
            raise ValueError("filename must be a non-empty str")
        if (
            type(self.sha256) is not str
            or len(self.sha256) != _SHA256_LENGTH
            or not _HEX_DIGITS.issuperset(self.sha256)
        ):
            raise ValueError("sha256 must be a lowercase SHA-256 digest")
        if type(self.byte_count) is not int:
            raise TypeError("byte_count must be an int")
        if self.byte_count <= 0:
            raise ValueError("byte_count must be positive")


@dataclass(frozen=True, slots=True)
class DurableLocalGameRecord:
    """strict loaderが復元・検証したcompleted durable record。"""

    record_identity: str
    policy_identities: tuple[str, str, str, str]
    max_steps: int | None
    provenance: SingleRoundExecutionProvenance
    inspection: LocalGameInspection
    payloads: Mapping[str, PayloadReference]

    def __post_init__(self) -> None:
        if (
            type(self.record_identity) is not str
            or len(self.record_identity) != _SHA256_LENGTH
            or not _HEX_DIGITS.issuperset(self.record_identity)
        ):
            raise ValueError("record_identity must be a lowercase SHA-256 digest")
        try:
            identities = tuple(self.policy_identities)
        except TypeError:
            raise TypeError("policy_identities must be an iterable") from None
        if len(identities) != 4:
            raise ValueError("policy_identities must contain exactly four values")
        if any(type(identity) is not str or not identity for identity in identities):
            raise ValueError("policy identities must be non-empty strings")
        if self.max_steps is not None and type(self.max_steps) is not int:
            raise TypeError("max_steps must be an int or None")
        if self.max_steps is not None and self.max_steps <= 0:
            raise ValueError("max_steps must be positive")
        if not isinstance(self.provenance, SingleRoundExecutionProvenance):
            raise TypeError("provenance must be a SingleRoundExecutionProvenance")
        if self.provenance.execution_environment != "riichienv":
            raise ValueError("provenance must identify the RiichiEnv execution path")
        if not isinstance(self.inspection, LocalGameInspection):
            raise TypeError("inspection must be a LocalGameInspection")
        if not isinstance(self.payloads, Mapping) or set(self.payloads) != set(
            _PAYLOAD_NAMES
        ):
            raise ValueError("payloads must contain the three record payloads")
        if any(
            not isinstance(reference, PayloadReference)
            for reference in self.payloads.values()
        ):
            raise TypeError("payloads must contain only PayloadReference values")
        object.__setattr__(self, "policy_identities", identities)
        object.__setattr__(self, "payloads", dict(self.payloads))


@dataclass(frozen=True, slots=True)
class LocalGameRecordSummary:
    """consumer utility smoke向けのsmall deterministic summary。"""

    record_identity: str
    seed: int
    game_mode: str
    steps: int
    decisions: int
    decisions_with_analysis: int
    rounds: int
    wins: int
    wins_with_backend_scoring: int
    draws: int


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_bytes(document: dict[str, Any]) -> bytes:
    return canonical_json_text(document).encode("utf-8")


def _strict_document(path: Path, context: str) -> dict[str, object]:
    try:
        value = read_json_document(path)
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        ArtifactValidationError,
    ) as exc:
        raise DurableLocalGameRecordError(
            f"{context} is not valid strict JSON"
        ) from exc
    if type(value) is not dict:
        raise DurableLocalGameRecordError(f"{context} must be a JSON object")
    try:
        serialized = path.read_bytes()
    except OSError as exc:
        raise DurableLocalGameRecordError(f"{context} cannot be read") from exc
    if _canonical_bytes(value) != serialized:
        raise DurableLocalGameRecordError(f"{context} is not canonical JSON")
    return value


def _construct(factory, context: str, **values):
    try:
        return factory(**values)
    except (TypeError, ValueError) as exc:
        raise DurableLocalGameRecordError(f"{context} is invalid: {exc}") from exc


def _parse_seat(value: object, context: str) -> Seat:
    raw = expect_int(value, context)
    try:
        return Seat(raw)
    except ValueError:
        raise DurableLocalGameRecordError(f"{context} is not a valid seat") from None


def _parse_enum(enum_type: type, value: object, context: str):
    raw = expect_str(value, context)
    try:
        return enum_type(raw)
    except ValueError:
        raise DurableLocalGameRecordError(
            f"{context} has an unsupported value: {raw!r}"
        ) from None


def _tile_to_value(tile: object, context: str) -> dict[str, object]:
    if not isinstance(tile, Tile):
        raise DurableLocalGameRecordError(f"{context} must be a Tile")
    return {
        "category": tile.tile_type.category.value,
        "is_red": tile.is_red,
        "rank": tile.tile_type.rank,
    }


_TILE_KEYS = {"category", "is_red", "rank"}


def _parse_tile(value: object, context: str) -> Tile:
    raw = expect_object(value, _TILE_KEYS, context)
    category = _parse_enum(TileCategory, raw["category"], f"{context}.category")
    return _construct(
        Tile,
        context,
        tile_type=_construct(
            TileType,
            f"{context}.tile_type",
            category=category,
            rank=expect_int(raw["rank"], f"{context}.rank"),
        ),
        is_red=expect_bool(raw["is_red"], f"{context}.is_red"),
    )


def _tiles_to_value(values: Sequence[Tile], context: str) -> list[dict[str, object]]:
    return [
        _tile_to_value(tile, f"{context}[{index}]") for index, tile in enumerate(values)
    ]


def _parse_tiles(value: object, context: str) -> tuple[Tile, ...]:
    return tuple(
        _parse_tile(item, f"{context}[{index}]")
        for index, item in enumerate(expect_list(value, context))
    )


_ACTION_TYPES: dict[type, str] = {
    DiscardAction: "discard",
    RiichiAction: "riichi",
    ChiAction: "chi",
    PonAction: "pon",
    DaiminkanAction: "daiminkan",
    AnkanAction: "ankan",
    KakanAction: "kakan",
    RonAction: "ron",
    TsumoAction: "tsumo",
    PassAction: "pass",
    KyuushuKyuuhaiAction: "kyuushu-kyuuhai",
}


def _action_to_value(action: object, context: str) -> dict[str, object]:
    kind = _ACTION_TYPES.get(type(action))
    if kind is None:
        raise DurableLocalGameRecordError(
            f"{context} has unsupported InternalAction type {type(action).__name__}"
        )
    value: dict[str, object] = {"actor": int(action.actor), "kind": kind}
    if isinstance(action, DiscardAction):
        value.update(
            tile=_tile_to_value(action.tile, f"{context}.tile"),
            tsumogiri=action.tsumogiri,
        )
    elif isinstance(action, (ChiAction, PonAction, DaiminkanAction)):
        value.update(
            target=int(action.target),
            called_tile=_tile_to_value(action.called_tile, f"{context}.called_tile"),
            consumed_tiles=_tiles_to_value(
                action.consumed_tiles, f"{context}.consumed_tiles"
            ),
        )
    elif isinstance(action, AnkanAction):
        value["tiles"] = _tiles_to_value(action.tiles, f"{context}.tiles")
    elif isinstance(action, KakanAction):
        value.update(
            added_tile=_tile_to_value(action.added_tile, f"{context}.added_tile"),
            from_seat=int(action.from_seat),
            called_tile=_tile_to_value(action.called_tile, f"{context}.called_tile"),
        )
    elif isinstance(action, RonAction):
        value.update(
            target=int(action.target),
            winning_tile=_tile_to_value(action.winning_tile, f"{context}.winning_tile"),
        )
    elif isinstance(action, TsumoAction):
        value["winning_tile"] = _tile_to_value(
            action.winning_tile, f"{context}.winning_tile"
        )
    return value


_ACTION_KEYS = {
    "discard": {"actor", "kind", "tile", "tsumogiri"},
    "riichi": {"actor", "kind"},
    "chi": {"actor", "kind", "target", "called_tile", "consumed_tiles"},
    "pon": {"actor", "kind", "target", "called_tile", "consumed_tiles"},
    "daiminkan": {"actor", "kind", "target", "called_tile", "consumed_tiles"},
    "ankan": {"actor", "kind", "tiles"},
    "kakan": {"actor", "kind", "added_tile", "from_seat", "called_tile"},
    "ron": {"actor", "kind", "target", "winning_tile"},
    "tsumo": {"actor", "kind", "winning_tile"},
    "pass": {"actor", "kind"},
    "kyuushu-kyuuhai": {"actor", "kind"},
}


def _parse_action(value: object, context: str) -> InternalAction:
    if type(value) is not dict:
        raise DurableLocalGameRecordError(f"{context} must be an object")
    kind = expect_str(value.get("kind"), f"{context}.kind")
    keys = _ACTION_KEYS.get(kind)
    if keys is None:
        raise DurableLocalGameRecordError(f"{context}.kind is unsupported: {kind!r}")
    raw = expect_object(value, keys, context)
    actor = _parse_seat(raw["actor"], f"{context}.actor")
    common = {"actor": actor}
    if kind == "discard":
        return _construct(
            DiscardAction,
            context,
            **common,
            tile=_parse_tile(raw["tile"], f"{context}.tile"),
            tsumogiri=expect_bool(raw["tsumogiri"], f"{context}.tsumogiri"),
        )
    if kind in ("chi", "pon", "daiminkan"):
        cls = {"chi": ChiAction, "pon": PonAction, "daiminkan": DaiminkanAction}[kind]
        return _construct(
            cls,
            context,
            **common,
            target=_parse_seat(raw["target"], f"{context}.target"),
            called_tile=_parse_tile(raw["called_tile"], f"{context}.called_tile"),
            consumed_tiles=_parse_tiles(
                raw["consumed_tiles"], f"{context}.consumed_tiles"
            ),
        )
    if kind == "ankan":
        return _construct(
            AnkanAction,
            context,
            **common,
            tiles=_parse_tiles(raw["tiles"], f"{context}.tiles"),
        )
    if kind == "kakan":
        return _construct(
            KakanAction,
            context,
            **common,
            added_tile=_parse_tile(raw["added_tile"], f"{context}.added_tile"),
            from_seat=_parse_seat(raw["from_seat"], f"{context}.from_seat"),
            called_tile=_parse_tile(raw["called_tile"], f"{context}.called_tile"),
        )
    if kind == "ron":
        return _construct(
            RonAction,
            context,
            **common,
            target=_parse_seat(raw["target"], f"{context}.target"),
            winning_tile=_parse_tile(raw["winning_tile"], f"{context}.winning_tile"),
        )
    if kind == "tsumo":
        return _construct(
            TsumoAction,
            context,
            **common,
            winning_tile=_parse_tile(raw["winning_tile"], f"{context}.winning_tile"),
        )
    cls = {
        "riichi": RiichiAction,
        "pass": PassAction,
        "kyuushu-kyuuhai": KyuushuKyuuhaiAction,
    }[kind]
    return _construct(cls, context, **common)


def _discard_to_value(discard: Discard, context: str) -> dict[str, object]:
    return {
        "called_by": None if discard.called_by is None else int(discard.called_by),
        "order": discard.order,
        "tile": _tile_to_value(discard.tile, f"{context}.tile"),
        "tsumogiri": discard.tsumogiri,
    }


_DISCARD_KEYS = {"called_by", "order", "tile", "tsumogiri"}


def _parse_discard(value: object, context: str) -> Discard:
    raw = expect_object(value, _DISCARD_KEYS, context)
    return _construct(
        Discard,
        context,
        tile=_parse_tile(raw["tile"], f"{context}.tile"),
        tsumogiri=expect_bool(raw["tsumogiri"], f"{context}.tsumogiri"),
        order=expect_int(raw["order"], f"{context}.order"),
        called_by=None
        if raw["called_by"] is None
        else _parse_seat(raw["called_by"], f"{context}.called_by"),
    )


def _meld_to_value(meld: PublicMeld, context: str) -> dict[str, object]:
    return {
        "called_tile": None
        if meld.called_tile is None
        else _tile_to_value(meld.called_tile, f"{context}.called_tile"),
        "from_seat": None if meld.from_seat is None else int(meld.from_seat),
        "kind": meld.kind.value,
        "tiles": _tiles_to_value(meld.tiles, f"{context}.tiles"),
    }


_MELD_KEYS = {"called_tile", "from_seat", "kind", "tiles"}


def _parse_meld(value: object, context: str) -> PublicMeld:
    raw = expect_object(value, _MELD_KEYS, context)
    return _construct(
        PublicMeld,
        context,
        kind=_parse_enum(MeldKind, raw["kind"], f"{context}.kind"),
        tiles=_parse_tiles(raw["tiles"], f"{context}.tiles"),
        from_seat=None
        if raw["from_seat"] is None
        else _parse_seat(raw["from_seat"], f"{context}.from_seat"),
        called_tile=None
        if raw["called_tile"] is None
        else _parse_tile(raw["called_tile"], f"{context}.called_tile"),
    )


def _policy_input_to_value(policy_input: PolicyInput) -> dict[str, object]:
    if not isinstance(policy_input, PolicyInput):
        raise DurableLocalGameRecordError("policy_input must be a PolicyInput")
    round_state = policy_input.round
    return {
        "own_hand": {
            "concealed_tiles": _tiles_to_value(
                policy_input.own_hand.concealed_tiles,
                "policy_input.own_hand.concealed_tiles",
            ),
            "drawn_tile": None
            if policy_input.own_hand.drawn_tile is None
            else _tile_to_value(
                policy_input.own_hand.drawn_tile, "policy_input.own_hand.drawn_tile"
            ),
        },
        "players": [
            {
                "discards": [
                    _discard_to_value(
                        item, f"policy_input.players[{seat}].discards[{index}]"
                    )
                    for index, item in enumerate(player.discards)
                ],
                "melds": [
                    _meld_to_value(item, f"policy_input.players[{seat}].melds[{index}]")
                    for index, item in enumerate(player.melds)
                ],
                "riichi": player.riichi.value,
                "score": player.score,
            }
            for seat, player in enumerate(policy_input.players)
        ],
        "round": {
            "dealer_seat": int(round_state.dealer_seat),
            "dora_indicators": _tiles_to_value(
                round_state.dora_indicators, "policy_input.round.dora_indicators"
            ),
            "hand_number": round_state.hand_number,
            "honba": round_state.honba,
            "live_wall_tiles_remaining": round_state.live_wall_tiles_remaining,
            "riichi_sticks": round_state.riichi_sticks,
            "round_wind": round_state.round_wind.value,
        },
        "self_seat": int(policy_input.self_seat),
    }


_POLICY_INPUT_KEYS = {"own_hand", "players", "round", "self_seat"}
_OWN_HAND_KEYS = {"concealed_tiles", "drawn_tile"}
_PLAYER_KEYS = {"discards", "melds", "riichi", "score"}
_ROUND_KEYS = {
    "dealer_seat",
    "dora_indicators",
    "hand_number",
    "honba",
    "live_wall_tiles_remaining",
    "riichi_sticks",
    "round_wind",
}


def _parse_policy_input(value: object, context: str) -> PolicyInput:
    raw = expect_object(value, _POLICY_INPUT_KEYS, context)
    round_raw = expect_object(raw["round"], _ROUND_KEYS, f"{context}.round")
    own_raw = expect_object(raw["own_hand"], _OWN_HAND_KEYS, f"{context}.own_hand")
    players_raw = expect_list(raw["players"], f"{context}.players")
    players = []
    for index, item in enumerate(players_raw):
        player_context = f"{context}.players[{index}]"
        player = expect_object(item, _PLAYER_KEYS, player_context)
        players.append(
            _construct(
                PlayerPublicState,
                player_context,
                score=expect_int(player["score"], f"{player_context}.score"),
                discards=tuple(
                    _parse_discard(row, f"{player_context}.discards[{row_index}]")
                    for row_index, row in enumerate(
                        expect_list(player["discards"], f"{player_context}.discards")
                    )
                ),
                melds=tuple(
                    _parse_meld(row, f"{player_context}.melds[{row_index}]")
                    for row_index, row in enumerate(
                        expect_list(player["melds"], f"{player_context}.melds")
                    )
                ),
                riichi=_parse_enum(
                    RiichiState, player["riichi"], f"{player_context}.riichi"
                ),
            )
        )
    drawn = own_raw["drawn_tile"]
    return _construct(
        PolicyInput,
        context,
        self_seat=_parse_seat(raw["self_seat"], f"{context}.self_seat"),
        round=_construct(
            RoundState,
            f"{context}.round",
            round_wind=_parse_enum(
                Wind, round_raw["round_wind"], f"{context}.round.round_wind"
            ),
            hand_number=expect_int(
                round_raw["hand_number"], f"{context}.round.hand_number"
            ),
            dealer_seat=_parse_seat(
                round_raw["dealer_seat"], f"{context}.round.dealer_seat"
            ),
            honba=expect_int(round_raw["honba"], f"{context}.round.honba"),
            riichi_sticks=expect_int(
                round_raw["riichi_sticks"], f"{context}.round.riichi_sticks"
            ),
            dora_indicators=_parse_tiles(
                round_raw["dora_indicators"], f"{context}.round.dora_indicators"
            ),
            live_wall_tiles_remaining=expect_int(
                round_raw["live_wall_tiles_remaining"],
                f"{context}.round.live_wall_tiles_remaining",
            ),
        ),
        players=tuple(players),
        own_hand=_construct(
            OwnHandState,
            f"{context}.own_hand",
            concealed_tiles=_parse_tiles(
                own_raw["concealed_tiles"], f"{context}.own_hand.concealed_tiles"
            ),
            drawn_tile=None
            if drawn is None
            else _parse_tile(drawn, f"{context}.own_hand.drawn_tile"),
        ),
    )


_ANALYSIS_TYPE = "type"
_TWO_STEP_TYPE = "lisjong.policies.two_step_ukeire:TwoStepUkeireAnalysis"
_VALUE_AWARE_TYPE = (
    "lisjong.policies.value_aware_two_step_ukeire:ValueAwareTwoStepUkeireAnalysis"
)
_HAND_VALUE_TYPE = "lisjong.policies.hand_value_aware_two_step_ukeire:HandValueAwareTwoStepUkeireAnalysis"
_FINITE_HORIZON_TYPE = (
    "lisjong.policies.finite_horizon_completion:FiniteHorizonCompletionAnalysis"
)


def _candidate_to_value(candidate: object, context: str) -> dict[str, object]:
    value: dict[str, object] = {
        "action": _action_to_value(candidate.action, f"{context}.action")
    }
    if type(candidate) is TwoStepUkeireCandidateEvaluation:
        value.update(
            post_discard_shanten=candidate.post_discard_shanten,
            current_ukeire_count=candidate.current_ukeire_count,
            second_step_ukeire_score=candidate.second_step_ukeire_score,
        )
    elif type(candidate) is ValueAwareTwoStepUkeireCandidateEvaluation:
        value.update(
            post_discard_shanten=candidate.post_discard_shanten,
            current_ukeire_count=candidate.current_ukeire_count,
            retained_concealed_dora_count=candidate.retained_concealed_dora_count,
            second_step_ukeire_score=candidate.second_step_ukeire_score,
        )
    elif type(candidate) is HandValueCandidateEvaluation:
        value.update(
            post_discard_shanten=candidate.post_discard_shanten,
            current_ukeire_count=candidate.current_ukeire_count,
            retained_real_value=candidate.retained_real_value,
            yaku_route_value=candidate.yaku_route_value,
            second_step_ukeire_score=candidate.second_step_ukeire_score,
        )
    elif type(candidate) is FiniteHorizonCandidateEvaluation:
        value["completion_mass"] = candidate.completion_mass
    else:
        raise DurableLocalGameRecordError(
            f"{context} has unsupported analysis candidate type {type(candidate).__name__}"
        )
    return value


def _analysis_to_value(
    analysis: AnalysisTrace | None, context: str
) -> dict[str, object] | None:
    if analysis is None:
        return None
    if type(analysis) is TwoStepUkeireAnalysis:
        return {
            _ANALYSIS_TYPE: _TWO_STEP_TYPE,
            "candidate_evaluations": [
                _candidate_to_value(item, f"{context}.candidate_evaluations[{index}]")
                for index, item in enumerate(analysis.candidate_evaluations)
            ],
        }
    if type(analysis) is ValueAwareTwoStepUkeireAnalysis:
        return {
            _ANALYSIS_TYPE: _VALUE_AWARE_TYPE,
            "candidate_evaluations": [
                _candidate_to_value(item, f"{context}.candidate_evaluations[{index}]")
                for index, item in enumerate(analysis.candidate_evaluations)
            ],
        }
    if type(analysis) is HandValueAwareTwoStepUkeireAnalysis:
        return {
            _ANALYSIS_TYPE: _HAND_VALUE_TYPE,
            "candidate_evaluations": [
                _candidate_to_value(item, f"{context}.candidate_evaluations[{index}]")
                for index, item in enumerate(analysis.candidate_evaluations)
            ],
        }
    if type(analysis) is FiniteHorizonCompletionAnalysis:
        return {
            _ANALYSIS_TYPE: _FINITE_HORIZON_TYPE,
            "candidate_evaluations": [
                _candidate_to_value(item, f"{context}.candidate_evaluations[{index}]")
                for index, item in enumerate(analysis.candidate_evaluations)
            ],
            "hidden_tile_count": analysis.hidden_tile_count,
            "horizon": analysis.horizon,
            "sequence_denominator": analysis.sequence_denominator,
            "two_step_tiebreak_analysis": _analysis_to_value(
                analysis.two_step_tiebreak_analysis,
                f"{context}.two_step_tiebreak_analysis",
            ),
        }
    raise DurableLocalGameRecordError(
        f"{context} has unsupported AnalysisTrace type {type(analysis).__name__}"
    )


_TWO_STEP_CANDIDATE_KEYS = {
    "action",
    "post_discard_shanten",
    "current_ukeire_count",
    "second_step_ukeire_score",
}
_VALUE_AWARE_CANDIDATE_KEYS = _TWO_STEP_CANDIDATE_KEYS | {
    "retained_concealed_dora_count"
}
_HAND_VALUE_CANDIDATE_KEYS = _TWO_STEP_CANDIDATE_KEYS | {
    "retained_real_value",
    "yaku_route_value",
}
_FINITE_CANDIDATE_KEYS = {"action", "completion_mass"}


def _parse_candidates(
    value: object, analysis_type: str, context: str
) -> tuple[object, ...]:
    result = []
    for index, item in enumerate(expect_list(value, context)):
        candidate_context = f"{context}[{index}]"
        if analysis_type == _TWO_STEP_TYPE:
            raw = expect_object(item, _TWO_STEP_CANDIDATE_KEYS, candidate_context)
            result.append(
                _construct(
                    TwoStepUkeireCandidateEvaluation,
                    candidate_context,
                    action=_parse_action(raw["action"], f"{candidate_context}.action"),
                    post_discard_shanten=expect_int(
                        raw["post_discard_shanten"],
                        f"{candidate_context}.post_discard_shanten",
                    ),
                    current_ukeire_count=expect_optional_int(
                        raw["current_ukeire_count"],
                        f"{candidate_context}.current_ukeire_count",
                    ),
                    second_step_ukeire_score=expect_optional_int(
                        raw["second_step_ukeire_score"],
                        f"{candidate_context}.second_step_ukeire_score",
                    ),
                )
            )
        elif analysis_type == _VALUE_AWARE_TYPE:
            raw = expect_object(item, _VALUE_AWARE_CANDIDATE_KEYS, candidate_context)
            result.append(
                _construct(
                    ValueAwareTwoStepUkeireCandidateEvaluation,
                    candidate_context,
                    action=_parse_action(raw["action"], f"{candidate_context}.action"),
                    post_discard_shanten=expect_int(
                        raw["post_discard_shanten"],
                        f"{candidate_context}.post_discard_shanten",
                    ),
                    current_ukeire_count=expect_optional_int(
                        raw["current_ukeire_count"],
                        f"{candidate_context}.current_ukeire_count",
                    ),
                    retained_concealed_dora_count=expect_optional_int(
                        raw["retained_concealed_dora_count"],
                        f"{candidate_context}.retained_concealed_dora_count",
                    ),
                    second_step_ukeire_score=expect_optional_int(
                        raw["second_step_ukeire_score"],
                        f"{candidate_context}.second_step_ukeire_score",
                    ),
                )
            )
        elif analysis_type == _HAND_VALUE_TYPE:
            raw = expect_object(item, _HAND_VALUE_CANDIDATE_KEYS, candidate_context)
            result.append(
                _construct(
                    HandValueCandidateEvaluation,
                    candidate_context,
                    action=_parse_action(raw["action"], f"{candidate_context}.action"),
                    post_discard_shanten=expect_int(
                        raw["post_discard_shanten"],
                        f"{candidate_context}.post_discard_shanten",
                    ),
                    current_ukeire_count=expect_optional_int(
                        raw["current_ukeire_count"],
                        f"{candidate_context}.current_ukeire_count",
                    ),
                    retained_real_value=expect_optional_int(
                        raw["retained_real_value"],
                        f"{candidate_context}.retained_real_value",
                    ),
                    yaku_route_value=expect_optional_int(
                        raw["yaku_route_value"], f"{candidate_context}.yaku_route_value"
                    ),
                    second_step_ukeire_score=expect_optional_int(
                        raw["second_step_ukeire_score"],
                        f"{candidate_context}.second_step_ukeire_score",
                    ),
                )
            )
        else:
            raw = expect_object(item, _FINITE_CANDIDATE_KEYS, candidate_context)
            result.append(
                _construct(
                    FiniteHorizonCandidateEvaluation,
                    candidate_context,
                    action=_parse_action(raw["action"], f"{candidate_context}.action"),
                    completion_mass=expect_int(
                        raw["completion_mass"], f"{candidate_context}.completion_mass"
                    ),
                )
            )
    return tuple(result)


def _parse_analysis(value: object, context: str) -> AnalysisTrace | None:
    if value is None:
        return None
    if type(value) is not dict:
        raise DurableLocalGameRecordError(f"{context} must be null or an object")
    analysis_type = expect_str(value.get(_ANALYSIS_TYPE), f"{context}.type")
    if analysis_type in (_TWO_STEP_TYPE, _VALUE_AWARE_TYPE, _HAND_VALUE_TYPE):
        raw = expect_object(value, {_ANALYSIS_TYPE, "candidate_evaluations"}, context)
        candidates = _parse_candidates(
            raw["candidate_evaluations"],
            analysis_type,
            f"{context}.candidate_evaluations",
        )
        cls = {
            _TWO_STEP_TYPE: TwoStepUkeireAnalysis,
            _VALUE_AWARE_TYPE: ValueAwareTwoStepUkeireAnalysis,
            _HAND_VALUE_TYPE: HandValueAwareTwoStepUkeireAnalysis,
        }[analysis_type]
        return _construct(cls, context, candidate_evaluations=candidates)
    if analysis_type == _FINITE_HORIZON_TYPE:
        raw = expect_object(
            value,
            {
                _ANALYSIS_TYPE,
                "candidate_evaluations",
                "hidden_tile_count",
                "horizon",
                "sequence_denominator",
                "two_step_tiebreak_analysis",
            },
            context,
        )
        tiebreak = _parse_analysis(
            raw["two_step_tiebreak_analysis"], f"{context}.two_step_tiebreak_analysis"
        )
        if tiebreak is not None and not isinstance(tiebreak, TwoStepUkeireAnalysis):
            raise DurableLocalGameRecordError(
                f"{context}.two_step_tiebreak_analysis must be TwoStepUkeireAnalysis or null"
            )
        return _construct(
            FiniteHorizonCompletionAnalysis,
            context,
            horizon=expect_int(raw["horizon"], f"{context}.horizon"),
            hidden_tile_count=expect_int(
                raw["hidden_tile_count"], f"{context}.hidden_tile_count"
            ),
            sequence_denominator=expect_int(
                raw["sequence_denominator"], f"{context}.sequence_denominator"
            ),
            candidate_evaluations=_parse_candidates(
                raw["candidate_evaluations"],
                analysis_type,
                f"{context}.candidate_evaluations",
            ),
            two_step_tiebreak_analysis=tiebreak,
        )
    raise DurableLocalGameRecordError(
        f"{context}.type is unsupported: {analysis_type!r}"
    )


def _decision_trace_to_value(trace: DecisionTrace, context: str) -> dict[str, object]:
    if not isinstance(trace, DecisionTrace):
        raise DurableLocalGameRecordError(f"{context} must be a DecisionTrace")
    return {
        "analysis": _analysis_to_value(trace.analysis, f"{context}.analysis"),
        "legal_actions": [
            _action_to_value(action, f"{context}.legal_actions[{index}]")
            for index, action in enumerate(trace.legal_actions)
        ],
        "selected_action": _action_to_value(
            trace.selected_action, f"{context}.selected_action"
        ),
    }


_DECISION_TRACE_KEYS = {"analysis", "legal_actions", "selected_action"}


def _parse_decision_trace(value: object, context: str) -> DecisionTrace:
    raw = expect_object(value, _DECISION_TRACE_KEYS, context)
    return _construct(
        DecisionTrace,
        context,
        legal_actions=tuple(
            _parse_action(item, f"{context}.legal_actions[{index}]")
            for index, item in enumerate(
                expect_list(raw["legal_actions"], f"{context}.legal_actions")
            )
        ),
        selected_action=_parse_action(
            raw["selected_action"], f"{context}.selected_action"
        ),
        analysis=_parse_analysis(raw["analysis"], f"{context}.analysis"),
    )


def _trace_document(trace: GameTrace) -> dict[str, Any]:
    return {
        "events": [
            {"event": event.event, "sequence": event.sequence} for event in trace.events
        ],
        "game_mode": trace.game_mode,
        "seed": trace.seed,
    }


_TRACE_KEYS = {"events", "game_mode", "seed"}
_EVENT_KEYS = {"event", "sequence"}


def _parse_trace(value: object) -> GameTrace:
    raw = expect_object(value, _TRACE_KEYS, "objective_trace")
    events = []
    for index, item in enumerate(expect_list(raw["events"], "objective_trace.events")):
        context = f"objective_trace.events[{index}]"
        event = expect_object(item, _EVENT_KEYS, context)
        events.append(
            _construct(
                GameTraceEvent,
                context,
                sequence=expect_int(event["sequence"], f"{context}.sequence"),
                event=expect_str(event["event"], f"{context}.event"),
            )
        )
    return _construct(
        GameTrace,
        "objective_trace",
        seed=expect_int(raw["seed"], "objective_trace.seed"),
        game_mode=expect_str(raw["game_mode"], "objective_trace.game_mode"),
        events=tuple(events),
    )


def _decisions_document(inspection: LocalGameInspection) -> dict[str, Any]:
    return {
        "decision_count": inspection.result.decisions,
        "game_mode": inspection.result.game_mode,
        "seed": inspection.result.seed,
        "step_count": inspection.result.steps,
        "steps": [
            {
                "event_sequence_end": step.event_sequence_end,
                "event_sequence_start": step.event_sequence_start,
                "seat_decisions": [
                    {
                        "decision_trace": _decision_trace_to_value(
                            decision.decision_trace,
                            f"steps[{step.step_ordinal}].seat_decisions[{index}].decision_trace",
                        ),
                        "policy_input": _policy_input_to_value(decision.policy_input),
                        "seat": int(decision.seat),
                    }
                    for index, decision in enumerate(step.seat_decisions)
                ],
                "step_ordinal": step.step_ordinal,
            }
            for step in inspection.step_observations
        ],
    }


_DECISIONS_KEYS = {"decision_count", "game_mode", "seed", "step_count", "steps"}
_STEP_KEYS = {
    "event_sequence_end",
    "event_sequence_start",
    "seat_decisions",
    "step_ordinal",
}
_SEAT_DECISION_KEYS = {"decision_trace", "policy_input", "seat"}


def _parse_decisions(
    value: object,
) -> tuple[int, str, int, int, tuple[StepDecisionObservation, ...]]:
    raw = expect_object(value, _DECISIONS_KEYS, "decisions")
    steps = []
    for index, item in enumerate(expect_list(raw["steps"], "decisions.steps")):
        context = f"decisions.steps[{index}]"
        step = expect_object(item, _STEP_KEYS, context)
        seat_decisions = []
        for seat_index, row in enumerate(
            expect_list(step["seat_decisions"], f"{context}.seat_decisions")
        ):
            seat_context = f"{context}.seat_decisions[{seat_index}]"
            seat_raw = expect_object(row, _SEAT_DECISION_KEYS, seat_context)
            seat_decisions.append(
                _construct(
                    SeatDecisionObservation,
                    seat_context,
                    seat=_parse_seat(seat_raw["seat"], f"{seat_context}.seat"),
                    policy_input=_parse_policy_input(
                        seat_raw["policy_input"], f"{seat_context}.policy_input"
                    ),
                    decision_trace=_parse_decision_trace(
                        seat_raw["decision_trace"], f"{seat_context}.decision_trace"
                    ),
                )
            )
        if tuple(decision.seat for decision in seat_decisions) != tuple(
            sorted((decision.seat for decision in seat_decisions), key=int)
        ):
            raise DurableLocalGameRecordError(
                f"{context}.seat_decisions must be in canonical Seat order"
            )
        steps.append(
            _construct(
                StepDecisionObservation,
                context,
                step_ordinal=expect_int(
                    step["step_ordinal"], f"{context}.step_ordinal"
                ),
                event_sequence_start=expect_int(
                    step["event_sequence_start"], f"{context}.event_sequence_start"
                ),
                event_sequence_end=expect_int(
                    step["event_sequence_end"], f"{context}.event_sequence_end"
                ),
                seat_decisions=tuple(seat_decisions),
            )
        )
    return (
        expect_int(raw["seed"], "decisions.seed"),
        expect_str(raw["game_mode"], "decisions.game_mode"),
        expect_int(raw["step_count"], "decisions.step_count"),
        expect_int(raw["decision_count"], "decisions.decision_count"),
        tuple(steps),
    )


def _stats_to_value(stats: SeatRoundStats) -> dict[str, object]:
    return {
        "deal_in_loss": stats.deal_in_loss,
        "dealt_in": stats.dealt_in,
        "end_score": stats.end_score,
        "exhaustive_draw": stats.exhaustive_draw,
        "first_tenpai_turn": stats.first_tenpai_turn,
        "start_score": stats.start_score,
        "tenpai_at_exhaustive_draw": stats.tenpai_at_exhaustive_draw,
        "win_points": stats.win_points,
        "won": stats.won,
    }


def _result_document(result: LocalGameResult) -> dict[str, Any]:
    return {
        "decisions": result.decisions,
        "game_mode": result.game_mode,
        "ranks": list(result.ranks),
        "scores": list(result.scores),
        "seat_round_stats": [
            _stats_to_value(stats) for stats in result.seat_round_stats
        ],
        "seed": result.seed,
        "steps": result.steps,
    }


_RESULT_KEYS = {
    "decisions",
    "game_mode",
    "ranks",
    "scores",
    "seat_round_stats",
    "seed",
    "steps",
}
_STATS_KEYS = {
    "deal_in_loss",
    "dealt_in",
    "end_score",
    "exhaustive_draw",
    "first_tenpai_turn",
    "start_score",
    "tenpai_at_exhaustive_draw",
    "win_points",
    "won",
}


def _parse_result(value: object) -> LocalGameResult:
    raw = expect_object(value, _RESULT_KEYS, "result")
    stats = []
    for index, item in enumerate(
        expect_list(raw["seat_round_stats"], "result.seat_round_stats")
    ):
        context = f"result.seat_round_stats[{index}]"
        row = expect_object(item, _STATS_KEYS, context)
        stats.append(
            _construct(
                SeatRoundStats,
                context,
                start_score=expect_int(row["start_score"], f"{context}.start_score"),
                end_score=expect_int(row["end_score"], f"{context}.end_score"),
                won=expect_bool(row["won"], f"{context}.won"),
                win_points=expect_optional_int(
                    row["win_points"], f"{context}.win_points"
                ),
                dealt_in=expect_bool(row["dealt_in"], f"{context}.dealt_in"),
                deal_in_loss=expect_optional_int(
                    row["deal_in_loss"], f"{context}.deal_in_loss"
                ),
                exhaustive_draw=expect_bool(
                    row["exhaustive_draw"], f"{context}.exhaustive_draw"
                ),
                tenpai_at_exhaustive_draw=expect_optional_bool(
                    row["tenpai_at_exhaustive_draw"],
                    f"{context}.tenpai_at_exhaustive_draw",
                ),
                first_tenpai_turn=expect_optional_int(
                    row["first_tenpai_turn"], f"{context}.first_tenpai_turn"
                ),
            )
        )
    return _construct(
        LocalGameResult,
        "result",
        seed=expect_int(raw["seed"], "result.seed"),
        game_mode=expect_str(raw["game_mode"], "result.game_mode"),
        scores=tuple(
            expect_int(item, f"result.scores[{index}]")
            for index, item in enumerate(expect_list(raw["scores"], "result.scores"))
        ),
        ranks=tuple(
            expect_int(item, f"result.ranks[{index}]")
            for index, item in enumerate(expect_list(raw["ranks"], "result.ranks"))
        ),
        steps=expect_int(raw["steps"], "result.steps"),
        decisions=expect_int(raw["decisions"], "result.decisions"),
        seat_round_stats=tuple(stats),
    )


def _yaku_to_value(yaku: RoundYaku) -> dict[str, object]:
    return {"name": yaku.name, "name_en": yaku.name_en, "yaku_id": yaku.yaku_id}


def _scoring_to_value(scoring: RoundWinScoring | None) -> dict[str, object] | None:
    if scoring is None:
        return None
    return {
        "fu": scoring.fu,
        "han": scoring.han,
        "pao_payer": None if scoring.pao_payer is None else int(scoring.pao_payer),
        "ron_points": scoring.ron_points,
        "tsumo_points_ko": scoring.tsumo_points_ko,
        "tsumo_points_oya": scoring.tsumo_points_oya,
        "yaku": [_yaku_to_value(item) for item in scoring.yaku],
        "yakuman": scoring.yakuman,
    }


def _win_to_value(win: RoundWinFact, context: str) -> dict[str, object]:
    return {
        "deltas": list(win.deltas),
        "event_sequence": win.event_sequence,
        "loser_seat": None if win.loser_seat is None else int(win.loser_seat),
        "scoring": _scoring_to_value(win.scoring),
        "tsumo": win.tsumo,
        "ura_indicators": _tiles_to_value(
            win.ura_indicators, f"{context}.ura_indicators"
        ),
        "winner_seat": int(win.winner_seat),
    }


def _draw_to_value(draw: RoundDrawFact | None) -> dict[str, object] | None:
    if draw is None:
        return None
    return {
        "deltas": list(draw.deltas),
        "event_sequence": draw.event_sequence,
        "exhaustive": draw.exhaustive,
        "reason": draw.reason,
    }


def _round_result_to_value(
    round_result: RoundResult, context: str
) -> dict[str, object]:
    return {
        "dealer_seat": int(round_result.dealer_seat),
        "dora_indicators": _tiles_to_value(
            round_result.dora_indicators, f"{context}.dora_indicators"
        ),
        "draw": _draw_to_value(round_result.draw),
        "end_scores": list(round_result.end_scores),
        "hand_number": round_result.hand_number,
        "honba": round_result.honba,
        "riichi_seats": [int(seat) for seat in round_result.riichi_seats],
        "riichi_sticks_after": round_result.riichi_sticks_after,
        "riichi_sticks_before": round_result.riichi_sticks_before,
        "round_wind": round_result.round_wind.value,
        "start_event_sequence": round_result.start_event_sequence,
        "start_scores": list(round_result.start_scores),
        "wins": [
            _win_to_value(win, f"{context}.wins[{index}]")
            for index, win in enumerate(round_result.wins)
        ],
    }


def _round_results_document(inspection: LocalGameInspection) -> dict[str, Any]:
    return {
        "game_mode": inspection.result.game_mode,
        "round_count": len(inspection.round_results),
        "rounds": [
            _round_result_to_value(round_result, f"rounds[{index}]")
            for index, round_result in enumerate(inspection.round_results)
        ],
        "seed": inspection.result.seed,
    }


_ROUND_RESULTS_DOCUMENT_KEYS = {"game_mode", "round_count", "rounds", "seed"}
_ROUND_RESULT_KEYS = {
    "dealer_seat",
    "dora_indicators",
    "draw",
    "end_scores",
    "hand_number",
    "honba",
    "riichi_seats",
    "riichi_sticks_after",
    "riichi_sticks_before",
    "round_wind",
    "start_event_sequence",
    "start_scores",
    "wins",
}
_WIN_KEYS = {
    "deltas",
    "event_sequence",
    "loser_seat",
    "scoring",
    "tsumo",
    "ura_indicators",
    "winner_seat",
}
_SCORING_KEYS = {
    "fu",
    "han",
    "pao_payer",
    "ron_points",
    "tsumo_points_ko",
    "tsumo_points_oya",
    "yaku",
    "yakuman",
}
_YAKU_KEYS = {"name", "name_en", "yaku_id"}
_DRAW_KEYS = {"deltas", "event_sequence", "exhaustive", "reason"}


def _parse_four_ints(value: object, context: str) -> tuple[int, int, int, int]:
    items = tuple(
        expect_int(item, f"{context}[{index}]")
        for index, item in enumerate(expect_list(value, context))
    )
    if len(items) != 4:
        raise DurableLocalGameRecordError(f"{context} must contain four values")
    return items


def _parse_scoring(value: object, context: str) -> RoundWinScoring | None:
    if value is None:
        return None
    raw = expect_object(value, _SCORING_KEYS, context)
    yaku = []
    for index, item in enumerate(expect_list(raw["yaku"], f"{context}.yaku")):
        yaku_context = f"{context}.yaku[{index}]"
        row = expect_object(item, _YAKU_KEYS, yaku_context)
        yaku.append(
            _construct(
                RoundYaku,
                yaku_context,
                yaku_id=expect_int(row["yaku_id"], f"{yaku_context}.yaku_id"),
                name=expect_str(row["name"], f"{yaku_context}.name"),
                name_en=expect_str(row["name_en"], f"{yaku_context}.name_en"),
            )
        )
    yaku = tuple(yaku)
    return _construct(
        RoundWinScoring,
        context,
        han=expect_int(raw["han"], f"{context}.han"),
        fu=expect_int(raw["fu"], f"{context}.fu"),
        yakuman=expect_bool(raw["yakuman"], f"{context}.yakuman"),
        yaku=yaku,
        ron_points=expect_int(raw["ron_points"], f"{context}.ron_points"),
        tsumo_points_oya=expect_int(
            raw["tsumo_points_oya"], f"{context}.tsumo_points_oya"
        ),
        tsumo_points_ko=expect_int(
            raw["tsumo_points_ko"], f"{context}.tsumo_points_ko"
        ),
        pao_payer=None
        if raw["pao_payer"] is None
        else _parse_seat(raw["pao_payer"], f"{context}.pao_payer"),
    )


def _parse_win(value: object, context: str) -> RoundWinFact:
    raw = expect_object(value, _WIN_KEYS, context)
    return _construct(
        RoundWinFact,
        context,
        winner_seat=_parse_seat(raw["winner_seat"], f"{context}.winner_seat"),
        tsumo=expect_bool(raw["tsumo"], f"{context}.tsumo"),
        loser_seat=None
        if raw["loser_seat"] is None
        else _parse_seat(raw["loser_seat"], f"{context}.loser_seat"),
        deltas=_parse_four_ints(raw["deltas"], f"{context}.deltas"),
        ura_indicators=_parse_tiles(raw["ura_indicators"], f"{context}.ura_indicators"),
        event_sequence=expect_int(raw["event_sequence"], f"{context}.event_sequence"),
        scoring=_parse_scoring(raw["scoring"], f"{context}.scoring"),
    )


def _parse_draw(value: object, context: str) -> RoundDrawFact | None:
    if value is None:
        return None
    raw = expect_object(value, _DRAW_KEYS, context)
    return _construct(
        RoundDrawFact,
        context,
        reason=expect_str(raw["reason"], f"{context}.reason"),
        exhaustive=expect_bool(raw["exhaustive"], f"{context}.exhaustive"),
        deltas=_parse_four_ints(raw["deltas"], f"{context}.deltas"),
        event_sequence=expect_int(raw["event_sequence"], f"{context}.event_sequence"),
    )


def _parse_round_result(value: object, context: str) -> RoundResult:
    raw = expect_object(value, _ROUND_RESULT_KEYS, context)
    return _construct(
        RoundResult,
        context,
        round_wind=_parse_enum(Wind, raw["round_wind"], f"{context}.round_wind"),
        hand_number=expect_int(raw["hand_number"], f"{context}.hand_number"),
        honba=expect_int(raw["honba"], f"{context}.honba"),
        dealer_seat=_parse_seat(raw["dealer_seat"], f"{context}.dealer_seat"),
        riichi_sticks_before=expect_int(
            raw["riichi_sticks_before"], f"{context}.riichi_sticks_before"
        ),
        riichi_sticks_after=expect_int(
            raw["riichi_sticks_after"], f"{context}.riichi_sticks_after"
        ),
        start_scores=_parse_four_ints(raw["start_scores"], f"{context}.start_scores"),
        end_scores=_parse_four_ints(raw["end_scores"], f"{context}.end_scores"),
        dora_indicators=_parse_tiles(
            raw["dora_indicators"], f"{context}.dora_indicators"
        ),
        riichi_seats=tuple(
            _parse_seat(item, f"{context}.riichi_seats[{index}]")
            for index, item in enumerate(
                expect_list(raw["riichi_seats"], f"{context}.riichi_seats")
            )
        ),
        start_event_sequence=expect_int(
            raw["start_event_sequence"], f"{context}.start_event_sequence"
        ),
        wins=tuple(
            _parse_win(item, f"{context}.wins[{index}]")
            for index, item in enumerate(expect_list(raw["wins"], f"{context}.wins"))
        ),
        draw=_parse_draw(raw["draw"], f"{context}.draw"),
    )


def _parse_round_results(value: object) -> tuple[int, str, tuple[RoundResult, ...]]:
    raw = expect_object(value, _ROUND_RESULTS_DOCUMENT_KEYS, "round_results")
    rounds = tuple(
        _parse_round_result(item, f"round_results.rounds[{index}]")
        for index, item in enumerate(expect_list(raw["rounds"], "round_results.rounds"))
    )
    if expect_int(raw["round_count"], "round_results.round_count") != len(rounds):
        raise DurableLocalGameRecordError(
            "round_results.round_count does not match the recorded rounds"
        )
    for previous, current in zip(rounds, rounds[1:]):
        if current.start_scores != previous.end_scores:
            raise DurableLocalGameRecordError(
                "recorded round start scores do not continue the previous round"
            )
        if current.riichi_sticks_before != previous.riichi_sticks_after:
            raise DurableLocalGameRecordError(
                "recorded round riichi sticks do not continue the previous round"
            )
    return (
        expect_int(raw["seed"], "round_results.seed"),
        expect_str(raw["game_mode"], "round_results.game_mode"),
        rounds,
    )


def _validate_round_identities(inspection: LocalGameInspection) -> None:
    """recorded round identityがdecision observation側と同じ局を指すか確認する。

    ``PolicyInput.round``はlisjongが所有するplayer-safe decision semanticsで
    あり、round-result factはArenaが所有するobjective terminal truthである。
    ここでは両者のsemanticsを混ぜず、同一runの同じ局を指しているかどうかだけを
    突き合わせる。

    ある``env.step()``で選ばれたdecisionは、そのstepが生成したeventより前の局に
    属する。したがって``event_sequence_start``以下で最後に始まった局が、その
    stepのdecisionが属する局である。
    """
    boundaries = [
        round_result.start_event_sequence for round_result in inspection.round_results
    ]
    for step in inspection.step_observations:
        index = -1
        for boundary in boundaries:
            if boundary > step.event_sequence_start:
                break
            index += 1
        if index < 0:
            raise DurableLocalGameRecordError(
                "decision step precedes the first recorded round"
            )
        round_result = inspection.round_results[index]
        for decision in step.seat_decisions:
            observed = decision.policy_input.round
            if (
                observed.round_wind is not round_result.round_wind
                or observed.hand_number != round_result.hand_number
                or observed.honba != round_result.honba
                or observed.dealer_seat != round_result.dealer_seat
            ):
                raise DurableLocalGameRecordError(
                    "decision round identity does not match the recorded round result"
                )


def _normalize_policy_identities(
    value: Mapping[Seat, str],
) -> tuple[str, str, str, str]:
    if not isinstance(value, Mapping):
        raise TypeError("policy_identities must be a mapping")
    if set(value) != set(Seat):
        raise ValueError("policy_identities must contain exactly one identity per Seat")
    identities = tuple(value[seat] for seat in Seat)
    if any(type(identity) is not str or not identity for identity in identities):
        raise ValueError("policy identities must be non-empty strings")
    return identities


def _payload_reference(filename: str, data: bytes) -> PayloadReference:
    return PayloadReference(
        filename=filename, sha256=_sha256(data), byte_count=len(data)
    )


def _payload_reference_value(reference: PayloadReference) -> dict[str, object]:
    return {
        "byte_count": reference.byte_count,
        "filename": reference.filename,
        "sha256": reference.sha256,
    }


def _manifest_without_identity(
    *,
    inspection: LocalGameInspection,
    policy_identities: tuple[str, str, str, str],
    max_steps: int | None,
    provenance: SingleRoundExecutionProvenance,
    payloads: Mapping[str, PayloadReference],
) -> dict[str, Any]:
    return {
        "effective_game_config": {
            "game_mode": inspection.result.game_mode,
            "max_steps": max_steps,
        },
        "execution_backend": LOCAL_GAME_RECORD_BACKEND,
        "game_mode": inspection.result.game_mode,
        "payloads": {
            name: _payload_reference_value(payloads[name]) for name in _PAYLOAD_NAMES
        },
        "policy_assignments": [
            {"identity": policy_identities[int(seat)], "seat": int(seat)}
            for seat in Seat
        ],
        "provenance": execution_provenance_to_dict(provenance),
        "schema_id": LOCAL_GAME_RECORD_SCHEMA_ID,
        "schema_version": LOCAL_GAME_RECORD_SCHEMA_VERSION,
        "seed": inspection.result.seed,
    }


def _record_identity(manifest_without_identity: dict[str, Any]) -> str:
    return _sha256(_canonical_bytes(manifest_without_identity))


def _manifest_document(**kwargs) -> dict[str, Any]:
    value = _manifest_without_identity(**kwargs)
    return {**value, "record_identity": _record_identity(value)}


_MANIFEST_KEYS = {
    "effective_game_config",
    "execution_backend",
    "game_mode",
    "payloads",
    "policy_assignments",
    "provenance",
    "record_identity",
    "schema_id",
    "schema_version",
    "seed",
}
_CONFIG_KEYS = {"game_mode", "max_steps"}
_PAYLOAD_REFERENCE_KEYS = {"byte_count", "filename", "sha256"}
_POLICY_ASSIGNMENT_KEYS = {"identity", "seat"}


def save_local_game_record(
    inspection: LocalGameInspection,
    path: str | Path,
    *,
    policy_identities: Mapping[Seat, str],
    max_steps: int | None,
    provenance: SingleRoundExecutionProvenance | None = None,
) -> DurableLocalGameRecord:
    """completed inspectionをnew immutable bundleへstrict readback後にfinalizeする。"""
    if not isinstance(inspection, LocalGameInspection):
        raise TypeError("inspection must be a completed LocalGameInspection")
    identities = _normalize_policy_identities(policy_identities)
    if max_steps is not None and type(max_steps) is not int:
        raise TypeError("max_steps must be an int or None")
    if max_steps is not None and max_steps <= 0:
        raise ValueError("max_steps must be positive")
    provenance = collect_execution_provenance() if provenance is None else provenance
    if not isinstance(provenance, SingleRoundExecutionProvenance):
        raise TypeError("provenance must be a SingleRoundExecutionProvenance")

    destination = Path(path)
    if destination.exists():
        raise FileExistsError(f"record path already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)

    documents = {
        "objective_trace": _trace_document(inspection.game_trace),
        "decisions": _decisions_document(inspection),
        "result": _result_document(inspection.result),
        "round_results": _round_results_document(inspection),
    }
    data = {name: _canonical_bytes(document) for name, document in documents.items()}
    filenames = {
        "objective_trace": OBJECTIVE_TRACE_FILENAME,
        "decisions": DECISIONS_FILENAME,
        "result": RESULT_FILENAME,
        "round_results": ROUND_RESULTS_FILENAME,
    }
    payloads = {
        name: _payload_reference(filenames[name], data[name]) for name in _PAYLOAD_NAMES
    }
    manifest_data = _canonical_bytes(
        _manifest_document(
            inspection=inspection,
            policy_identities=identities,
            max_steps=max_steps,
            provenance=provenance,
            payloads=payloads,
        )
    )

    staging = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}.staging-", dir=destination.parent)
    )
    published = False
    try:
        for name in _PAYLOAD_NAMES:
            write_new_artifact_file(
                staging / filenames[name], data[name].decode("utf-8")
            )
        write_new_artifact_file(
            staging / MANIFEST_FILENAME, manifest_data.decode("utf-8")
        )
        validated = load_local_game_record(staging)

        destination.mkdir()
        published = True
        for name in _PAYLOAD_NAMES:
            os.rename(staging / filenames[name], destination / filenames[name])
        os.rename(staging / MANIFEST_FILENAME, destination / MANIFEST_FILENAME)
        return validated
    except BaseException:
        if published:
            shutil.rmtree(destination, ignore_errors=True)
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _parse_payload_reference(value: object, context: str) -> PayloadReference:
    raw = expect_object(value, _PAYLOAD_REFERENCE_KEYS, context)
    return _construct(
        PayloadReference,
        context,
        filename=expect_str(raw["filename"], f"{context}.filename"),
        sha256=expect_str(raw["sha256"], f"{context}.sha256"),
        byte_count=expect_int(raw["byte_count"], f"{context}.byte_count"),
    )


def _load_local_game_record(path: str | Path) -> DurableLocalGameRecord:
    directory = Path(path)
    if not directory.is_dir():
        raise DurableLocalGameRecordError("record directory is missing")
    try:
        filenames = {entry.name for entry in directory.iterdir()}
    except OSError as exc:
        raise DurableLocalGameRecordError("record directory cannot be read") from exc
    if filenames != _EXPECTED_FILES:
        raise DurableLocalGameRecordError("record contains missing or extra files")
    if any(
        (directory / filename).is_symlink() or not (directory / filename).is_file()
        for filename in _EXPECTED_FILES
    ):
        raise DurableLocalGameRecordError(
            "record entries must be regular files inside the bundle"
        )

    manifest = expect_object(
        _strict_document(directory / MANIFEST_FILENAME, "manifest"),
        _MANIFEST_KEYS,
        "manifest",
    )
    if (
        expect_str(manifest["schema_id"], "manifest.schema_id")
        != LOCAL_GAME_RECORD_SCHEMA_ID
    ):
        raise DurableLocalGameRecordError("unsupported record schema id")
    schema_version = expect_int(manifest["schema_version"], "manifest.schema_version")
    if schema_version != LOCAL_GAME_RECORD_SCHEMA_VERSION:
        if schema_version == LOCAL_GAME_RECORD_SCHEMA_VERSION_WITHOUT_ROUND_RESULTS:
            raise DurableLocalGameRecordError(
                "schema version 1 records do not contain per-round result facts "
                "and are not readable by this version 2 only loader"
            )
        raise DurableLocalGameRecordError("unsupported record schema version")
    if (
        expect_str(manifest["execution_backend"], "manifest.execution_backend")
        != LOCAL_GAME_RECORD_BACKEND
    ):
        raise DurableLocalGameRecordError("unsupported execution backend")

    seed = expect_int(manifest["seed"], "manifest.seed")
    game_mode = expect_str(manifest["game_mode"], "manifest.game_mode")
    config = expect_object(
        manifest["effective_game_config"],
        _CONFIG_KEYS,
        "manifest.effective_game_config",
    )
    if (
        expect_str(config["game_mode"], "manifest.effective_game_config.game_mode")
        != game_mode
    ):
        raise DurableLocalGameRecordError(
            "effective game config does not match game mode"
        )
    max_steps = expect_optional_int(
        config["max_steps"], "manifest.effective_game_config.max_steps"
    )
    if max_steps is not None and max_steps <= 0:
        raise DurableLocalGameRecordError(
            "manifest.effective_game_config.max_steps must be positive"
        )

    assignments_raw = expect_list(
        manifest["policy_assignments"], "manifest.policy_assignments"
    )
    if len(assignments_raw) != 4:
        raise DurableLocalGameRecordError(
            "manifest must contain four policy assignments"
        )
    identities = []
    for index, item in enumerate(assignments_raw):
        context = f"manifest.policy_assignments[{index}]"
        assignment = expect_object(item, _POLICY_ASSIGNMENT_KEYS, context)
        if _parse_seat(assignment["seat"], f"{context}.seat") != Seat(index):
            raise DurableLocalGameRecordError(
                "policy assignments must be in Seat order"
            )
        identity = expect_str(assignment["identity"], f"{context}.identity")
        if not identity:
            raise DurableLocalGameRecordError("policy identity must not be empty")
        identities.append(identity)

    payloads_raw = expect_object(
        manifest["payloads"], set(_PAYLOAD_NAMES), "manifest.payloads"
    )
    payloads = {
        name: _parse_payload_reference(payloads_raw[name], f"manifest.payloads.{name}")
        for name in _PAYLOAD_NAMES
    }
    expected_names = {payloads[name].filename for name in _PAYLOAD_NAMES} | {
        MANIFEST_FILENAME
    }
    if expected_names != _EXPECTED_FILES or len(expected_names) != len(_EXPECTED_FILES):
        raise DurableLocalGameRecordError("manifest payload filenames are invalid")

    payload_documents = {}
    for name in _PAYLOAD_NAMES:
        reference = payloads[name]
        payload_path = directory / reference.filename
        try:
            payload_data = payload_path.read_bytes()
        except OSError as exc:
            raise DurableLocalGameRecordError(f"{name} payload cannot be read") from exc
        if len(payload_data) != reference.byte_count:
            raise DurableLocalGameRecordError(f"{name} payload byte count mismatch")
        if _sha256(payload_data) != reference.sha256:
            raise DurableLocalGameRecordError(f"{name} payload digest mismatch")
        payload_documents[name] = _strict_document(payload_path, name)

    recorded_identity = expect_str(
        manifest["record_identity"], "manifest.record_identity"
    )
    without_identity = dict(manifest)
    without_identity.pop("record_identity")
    if recorded_identity != _record_identity(without_identity):
        raise DurableLocalGameRecordError("record identity mismatch")

    try:
        provenance = parse_execution_provenance(manifest["provenance"])
    except (TypeError, ValueError, ArtifactValidationError) as exc:
        raise DurableLocalGameRecordError("manifest provenance is invalid") from exc
    trace = _parse_trace(payload_documents["objective_trace"])
    result = _parse_result(payload_documents["result"])
    decision_seed, decision_mode, step_count, decision_count, steps = _parse_decisions(
        payload_documents["decisions"]
    )
    round_seed, round_mode, round_results = _parse_round_results(
        payload_documents["round_results"]
    )
    if not (seed == trace.seed == result.seed == decision_seed == round_seed):
        raise DurableLocalGameRecordError("record seed fields do not identify one run")
    if not (
        game_mode == trace.game_mode == result.game_mode == decision_mode == round_mode
    ):
        raise DurableLocalGameRecordError(
            "record game mode fields do not identify one run"
        )
    if step_count != result.steps or decision_count != result.decisions:
        raise DurableLocalGameRecordError("decision payload counts do not match result")
    inspection = _construct(
        LocalGameInspection,
        "record inspection",
        result=result,
        game_trace=trace,
        step_observations=steps,
        round_results=round_results,
    )
    _validate_round_identities(inspection)
    return _construct(
        DurableLocalGameRecord,
        "record",
        record_identity=recorded_identity,
        policy_identities=tuple(identities),
        max_steps=max_steps,
        provenance=provenance,
        inspection=inspection,
        payloads=payloads,
    )


def load_local_game_record(path: str | Path) -> DurableLocalGameRecord:
    """completed bundleをstrictに検証し、existing semantic valuesを復元する。"""
    try:
        return _load_local_game_record(path)
    except DurableLocalGameRecordError:
        raise
    except (ArtifactValidationError, TypeError, ValueError) as exc:
        raise DurableLocalGameRecordError(
            "record is malformed or semantically inconsistent"
        ) from exc


def summarize_local_game_record(
    record: DurableLocalGameRecord,
) -> LocalGameRecordSummary:
    """PolicyInput / legal action / selected action / analysisをiterateするsmoke。"""
    if not isinstance(record, DurableLocalGameRecord):
        raise TypeError("record must be a DurableLocalGameRecord")
    decisions = tuple(
        decision
        for step in record.inspection.step_observations
        for decision in step.seat_decisions
    )
    for decision in decisions:
        # Access自体がconsumer seam。construction済みDecisionTraceがlegal/selectedの
        # consistencyを保証し、PolicyInputはplayer-safe original snapshotである。
        decision.policy_input.self_seat
        decision.decision_trace.legal_actions
        decision.decision_trace.selected_action
    rounds = record.inspection.round_results
    for round_result in rounds:
        # Access自体がconsumer seam。round identity / settlement / win facts は
        # construction時点で検証済みであり、Mahjong ruleを評価せずに読める。
        round_result.round_wind
        round_result.end_scores
        round_result.riichi_seats
        for win in round_result.wins:
            win.winner_seat
            win.tsumo
            win.ura_indicators
            win.scoring
        if round_result.draw is not None:
            round_result.draw.reason
            round_result.draw.exhaustive
    result = record.inspection.result
    return LocalGameRecordSummary(
        record_identity=record.record_identity,
        seed=result.seed,
        game_mode=result.game_mode,
        steps=result.steps,
        decisions=len(decisions),
        decisions_with_analysis=sum(
            decision.decision_trace.analysis is not None for decision in decisions
        ),
        rounds=len(rounds),
        wins=sum(len(round_result.wins) for round_result in rounds),
        wins_with_backend_scoring=sum(
            win.scoring is not None
            for round_result in rounds
            for win in round_result.wins
        ),
        draws=sum(round_result.draw is not None for round_result in rounds),
    )


def run_and_save_local_game_record(
    policies: Mapping[Seat, PolicySpec],
    *,
    seed: int,
    path: str | Path,
    game_mode: str = "4p-red-half",
    max_steps: int | None = None,
) -> DurableLocalGameRecord:
    """4 seat assignmentで1 successful gameだけを実行・保存するthin acquisition。"""
    if not isinstance(policies, Mapping) or set(policies) != set(Seat):
        raise ValueError("policies must contain exactly one PolicySpec per Seat")
    if any(not isinstance(spec, PolicySpec) for spec in policies.values()):
        raise TypeError("policies must contain only PolicySpec values")
    recorder = LocalGameInspectionRecorder()
    runner = LocalGameRunner(
        {seat: policies[seat].factory() for seat in Seat},
        seed=seed,
        game_mode=game_mode,
        max_steps=max_steps,
        inspection_recorder=recorder,
    )
    runner.run()
    return save_local_game_record(
        recorder.snapshot(),
        path,
        policy_identities={seat: policies[seat].identity for seat in Seat},
        max_steps=max_steps,
    )


__all__ = [
    "DECISIONS_FILENAME",
    "LOCAL_GAME_RECORD_BACKEND",
    "LOCAL_GAME_RECORD_SCHEMA_ID",
    "LOCAL_GAME_RECORD_SCHEMA_VERSION",
    "LOCAL_GAME_RECORD_SCHEMA_VERSION_WITHOUT_ROUND_RESULTS",
    "MANIFEST_FILENAME",
    "OBJECTIVE_TRACE_FILENAME",
    "RESULT_FILENAME",
    "ROUND_RESULTS_FILENAME",
    "DurableLocalGameRecord",
    "DurableLocalGameRecordError",
    "LocalGameRecordSummary",
    "PayloadReference",
    "load_local_game_record",
    "run_and_save_local_game_record",
    "save_local_game_record",
    "summarize_local_game_record",
]
