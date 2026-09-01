"""Mortal same-state decision diagnostic専用のopt-in offline analysis artifact。

このmoduleが所有するのは、``lisjong_arena.mortal_decision_evaluation``が返す
successful ``MortalDecisionEvaluationResult``を、後からoffline analysisできる
local fileへprojectionするpersistence contractだけである。

**このartifactはMortal same-state disagreementのexploratory offline analysis専用
である。** Mortalはground truthではなく、disagreementはerrorでもlisjong Policyの
誤りでもない。training dataset、supervised label corpus、canonical GameRecord、
project-wide decision corpus、generic replay/event schemaではない。

そのため次を導入しない。

- generic ``PolicyInput`` / ``DecisionTrace`` / ``AnalysisTrace`` persistence API
- lisjong Policy contract側の``to_json()`` / ``serialize()`` / ``schema_version``
- cross-environment Action schema、artifact repository、database、dashboard、viewer

保持するのはArena側のconcrete consumer専用projectionであり、schema identityも
このdiagnostic専用の``lisjong-arena-mortal-decision-analysis-v1``である。
project-wide schema versionへ昇格させない。

## 情報境界

各rowへ書くのは、そのdecision時点でlisjong Policyが実際に観測していた
``PolicyInput``のplayer-safe snapshotと、``DecisionTrace``のlegal actions /
selected actionだけである。opponentのconcealed hand、wall / 王牌、未来のevent、
oracle / observer-only state、credential、Docker configuration、machine-local
model pathは書かない。shanten、ukeire、danger、hand value、push/fold label等を
Arena側で新規計算もしない。これはPolicyが見ていた情報のprojectionであって、
Arenaによるsemantic enrichmentではない。

``DecisionTrace.analysis``のgeneric serializationはinitial artifactのscope外で
ある。current codebaseはlisjong-owned ``AnalysisTrace``のexplicit / safe /
versioned serializerを持たず、このartifactのためだけにarbitrary dataclass
serializer、``repr()`` / ``__dict__`` serializer、pickle、generic registryを
新設しない。analysis payloadはrowへ書かず、``analysis = 0``や
``analysis failed``のようなsemanticへも変換しない(fieldごと存在しない)。

## Aggregateの正本

manifestのaggregateは、in-memory ``MortalDecisionComparisonSummary``からの
projectionだけである。artifact側で別semanticのaggregateを実装せず、
action-kind pair集計も``mortal_decision_comparison``のcanonical実装を共有する。

## Completion

completeなartifactはsuccessful diagnostic runに対してだけ公開する。全rowを
serializeし、manifestとrowsの整合を検証してから、staging directoryをfinal
pathへrenameする。途中で失敗した場合はpartialな``decisions.jsonl``や
completeに見える``manifest.json``を残さない。既存pathはdefaultで上書きしない。
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from math import isfinite
from pathlib import Path
from types import MappingProxyType
from typing import Any

from lisjong.policy_contract import (
    AnkanAction,
    ChiAction,
    DaiminkanAction,
    DecisionTrace,
    Discard,
    DiscardAction,
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
    expect_float,
    expect_int,
    expect_list,
    expect_object,
    expect_str,
    read_json_document,
    write_new_artifact_file,
)
from lisjong_arena.model import SINGLE_ROUND_GAME_MODE, SINGLE_ROUND_ROTATION_COUNT
from lisjong_arena.mortal_decision_comparison import (
    ActionKindPairCount,
    NormalizedRiichiEnvAction,
    RiichiEnvActionKind,
    count_action_kind_pairs,
)
from lisjong_arena.mortal_decision_evaluation import MortalDecisionEvaluationResult
from lisjong_arena.single_round_artifact import (
    SingleRoundExecutionProvenance,
    collect_execution_provenance,
    execution_provenance_to_dict,
    parse_execution_provenance,
)

MORTAL_DECISION_ANALYSIS_SCHEMA = "lisjong-arena-mortal-decision-analysis-v1"
"""このdiagnostic export専用のschema identity。unknown値はfail closedする。

field semanticsがincompatibleに変わる場合だけこのidentityを変更する。
project-wide schema versionではない。
"""

MORTAL_DECISION_DIAGNOSTIC = "mortal-same-state-decision-v1"
"""artifactが記録するdiagnostic identity。strength evaluation protocolではない。"""

MANIFEST_FILENAME = "manifest.json"
DECISIONS_FILENAME = "decisions.jsonl"

_STAGING_PREFIX = ".mortal-decision-analysis-staging-"
_SHA256_DIGEST_LENGTH = 64
_HEX_DIGITS = frozenset("0123456789abcdef")


class MortalDecisionAnalysisArtifactError(ArtifactValidationError):
    """Mortal診断artifactを生成、検証、または読み戻せない場合。"""


# ---------------------------------------------------------------------------
# lisjong-owned valueのprojection (write side)
# ---------------------------------------------------------------------------


def _enum_value(value: object, expected: type, context: str) -> str:
    if not isinstance(value, expected):
        raise MortalDecisionAnalysisArtifactError(
            f"{context} must be a {expected.__name__}"
        )
    return value.value


def _seat_value(value: object, context: str) -> int:
    if not isinstance(value, Seat):
        raise MortalDecisionAnalysisArtifactError(f"{context} must be a Seat")
    return int(value)


def _optional_seat_value(value: object, context: str) -> int | None:
    return None if value is None else _seat_value(value, context)


def _tile_to_dict(value: object, context: str) -> dict[str, Any]:
    if not isinstance(value, Tile):
        raise MortalDecisionAnalysisArtifactError(f"{context} must be a Tile")
    return {
        "category": _enum_value(
            value.tile_type.category, TileCategory, f"{context}.category"
        ),
        "rank": value.tile_type.rank,
        "is_red": value.is_red,
    }


def _optional_tile_to_dict(value: object, context: str) -> dict[str, Any] | None:
    return None if value is None else _tile_to_dict(value, context)


def _tiles_to_list(values: object, context: str) -> list[dict[str, Any]]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Iterable):
        raise MortalDecisionAnalysisArtifactError(f"{context} must be a tile sequence")
    return [_tile_to_dict(item, context) for item in values]


def _bool_value(value: object, context: str) -> bool:
    if type(value) is not bool:
        raise MortalDecisionAnalysisArtifactError(f"{context} must be a bool")
    return value


def _int_value(value: object, context: str) -> int:
    if type(value) is not int:
        raise MortalDecisionAnalysisArtifactError(f"{context} must be an int")
    return value


_INTERNAL_ACTION_KINDS: dict[type, str] = {
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


def _internal_action_to_dict(action: object, context: str) -> dict[str, Any]:
    """lisjong ``InternalAction``のvariant固有semantic fieldをexplicitへ写す。

    variantごとに必要fieldを明示dispatchし、未知typeは``repr()``等へfallback
    せずfail closedする。Arena側でAction semanticsを再定義しない。
    """
    kind = _INTERNAL_ACTION_KINDS.get(type(action))
    if kind is None:
        raise MortalDecisionAnalysisArtifactError(
            f"{context} is not a supported lisjong InternalAction variant: "
            f"{type(action).__name__}"
        )
    projected: dict[str, Any] = {
        "kind": kind,
        "actor": _seat_value(action.actor, f"{context}.actor"),
    }
    if kind == "discard":
        projected["tile"] = _tile_to_dict(action.tile, f"{context}.tile")
        projected["tsumogiri"] = _bool_value(action.tsumogiri, f"{context}.tsumogiri")
    elif kind in ("chi", "pon", "daiminkan"):
        projected["target"] = _seat_value(action.target, f"{context}.target")
        projected["called_tile"] = _tile_to_dict(
            action.called_tile, f"{context}.called_tile"
        )
        projected["consumed_tiles"] = _tiles_to_list(
            action.consumed_tiles, f"{context}.consumed_tiles"
        )
    elif kind == "ankan":
        projected["tiles"] = _tiles_to_list(action.tiles, f"{context}.tiles")
    elif kind == "kakan":
        projected["added_tile"] = _tile_to_dict(
            action.added_tile, f"{context}.added_tile"
        )
        projected["from_seat"] = _seat_value(action.from_seat, f"{context}.from_seat")
        projected["called_tile"] = _tile_to_dict(
            action.called_tile, f"{context}.called_tile"
        )
    elif kind in ("ron", "tsumo"):
        if kind == "ron":
            projected["target"] = _seat_value(action.target, f"{context}.target")
        projected["winning_tile"] = _tile_to_dict(
            action.winning_tile, f"{context}.winning_tile"
        )
    return projected


def _discard_to_dict(value: object, context: str) -> dict[str, Any]:
    if not isinstance(value, Discard):
        raise MortalDecisionAnalysisArtifactError(f"{context} must be a Discard")
    return {
        "tile": _tile_to_dict(value.tile, f"{context}.tile"),
        "tsumogiri": _bool_value(value.tsumogiri, f"{context}.tsumogiri"),
        "order": _int_value(value.order, f"{context}.order"),
        "called_by": _optional_seat_value(value.called_by, f"{context}.called_by"),
    }


def _meld_to_dict(value: object, context: str) -> dict[str, Any]:
    if not isinstance(value, PublicMeld):
        raise MortalDecisionAnalysisArtifactError(f"{context} must be a PublicMeld")
    return {
        "kind": _enum_value(value.kind, MeldKind, f"{context}.kind"),
        "tiles": _tiles_to_list(value.tiles, f"{context}.tiles"),
        "from_seat": _optional_seat_value(value.from_seat, f"{context}.from_seat"),
        "called_tile": _optional_tile_to_dict(
            value.called_tile, f"{context}.called_tile"
        ),
    }


def _player_state_to_dict(value: object, context: str) -> dict[str, Any]:
    if not isinstance(value, PlayerPublicState):
        raise MortalDecisionAnalysisArtifactError(
            f"{context} must be a PlayerPublicState"
        )
    return {
        "score": _int_value(value.score, f"{context}.score"),
        "discards": [
            _discard_to_dict(item, f"{context}.discards") for item in value.discards
        ],
        "melds": [_meld_to_dict(item, f"{context}.melds") for item in value.melds],
        "riichi": _enum_value(value.riichi, RiichiState, f"{context}.riichi"),
    }


def _round_state_to_dict(value: object, context: str) -> dict[str, Any]:
    if not isinstance(value, RoundState):
        raise MortalDecisionAnalysisArtifactError(f"{context} must be a RoundState")
    return {
        "round_wind": _enum_value(value.round_wind, Wind, f"{context}.round_wind"),
        "hand_number": _int_value(value.hand_number, f"{context}.hand_number"),
        "dealer_seat": _seat_value(value.dealer_seat, f"{context}.dealer_seat"),
        "honba": _int_value(value.honba, f"{context}.honba"),
        "riichi_sticks": _int_value(value.riichi_sticks, f"{context}.riichi_sticks"),
        "dora_indicators": _tiles_to_list(
            value.dora_indicators, f"{context}.dora_indicators"
        ),
        "live_wall_tiles_remaining": _int_value(
            value.live_wall_tiles_remaining, f"{context}.live_wall_tiles_remaining"
        ),
    }


def _own_hand_to_dict(value: object, context: str) -> dict[str, Any]:
    if not isinstance(value, OwnHandState):
        raise MortalDecisionAnalysisArtifactError(f"{context} must be an OwnHandState")
    return {
        "concealed_tiles": _tiles_to_list(
            value.concealed_tiles, f"{context}.concealed_tiles"
        ),
        "drawn_tile": _optional_tile_to_dict(value.drawn_tile, f"{context}.drawn_tile"),
    }


def _policy_input_to_dict(value: object) -> dict[str, Any]:
    """``PolicyInput``のplayer-safe snapshotをそのままprojectionする。

    ``PolicyInput``はlisjongが所有するseat-visible snapshotであり、ここでは
    fieldを足さず、hidden / oracle情報も新規取得しない。
    """
    if not isinstance(value, PolicyInput):
        raise MortalDecisionAnalysisArtifactError("policy_input must be a PolicyInput")
    return {
        "self_seat": _seat_value(value.self_seat, "policy_input.self_seat"),
        "round": _round_state_to_dict(value.round, "policy_input.round"),
        "players": [
            _player_state_to_dict(player, f"policy_input.players[{index}]")
            for index, player in enumerate(value.players)
        ],
        "own_hand": _own_hand_to_dict(value.own_hand, "policy_input.own_hand"),
    }


def _decision_trace_to_dict(value: object) -> dict[str, Any]:
    """``DecisionTrace``のlegal actions / selected actionだけをprojectionする。

    ``DecisionTrace.analysis``はinitial artifactのscope外であり、fieldとして
    出力しない(欠落を``analysis = 0``等のsemanticへ変換しない)。
    """
    if not isinstance(value, DecisionTrace):
        raise MortalDecisionAnalysisArtifactError(
            "decision_trace must be a DecisionTrace"
        )
    return {
        "legal_actions": [
            _internal_action_to_dict(action, "decision_trace.legal_actions")
            for action in value.legal_actions
        ],
        "selected_action": _internal_action_to_dict(
            value.selected_action, "decision_trace.selected_action"
        ),
    }


def _normalized_action_to_dict(value: object, context: str) -> dict[str, Any]:
    if not isinstance(value, NormalizedRiichiEnvAction):
        raise MortalDecisionAnalysisArtifactError(
            f"{context} must be a NormalizedRiichiEnvAction"
        )
    return {
        "kind": _enum_value(value.kind, RiichiEnvActionKind, f"{context}.kind"),
        "actor": _seat_value(value.actor, f"{context}.actor"),
        "tile": _optional_tile_to_dict(value.tile, f"{context}.tile"),
        "consume_tiles": _tiles_to_list(
            value.consume_tiles, f"{context}.consume_tiles"
        ),
        "tsumogiri": None
        if value.tsumogiri is None
        else _bool_value(value.tsumogiri, f"{context}.tsumogiri"),
    }


# ---------------------------------------------------------------------------
# artifact value types (read side)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MortalRuntimeProvenance:
    """再現に必要なMortal runtime identityだけを保持する。

    ``docker_executable``やmachine-localなmodel pathは再現性identityではなく
    machine-local informationのため保持しない。credentialも保持しない。
    """

    image: str
    implementation_revision: str
    model_sha256: str
    response_timeout_seconds: float

    def __post_init__(self) -> None:
        for name in ("image", "implementation_revision", "model_sha256"):
            value = getattr(self, name)
            if type(value) is not str:
                raise TypeError(f"{name} must be a str")
            if not value.strip():
                raise ValueError(f"{name} must not be empty")
        digest = self.model_sha256
        if len(digest) != _SHA256_DIGEST_LENGTH or not _HEX_DIGITS.issuperset(digest):
            raise ValueError("model_sha256 must be a lowercase SHA-256 digest")
        if type(self.response_timeout_seconds) is not float:
            raise TypeError("response_timeout_seconds must be a float")
        if (
            not isfinite(self.response_timeout_seconds)
            or self.response_timeout_seconds <= 0
        ):
            raise ValueError("response_timeout_seconds must be positive")


@dataclass(frozen=True, slots=True)
class MortalDecisionAnalysisManifest:
    """run全体のprovenanceと、in-memory summaryから写したaggregate。"""

    schema: str
    diagnostic: str
    game_mode: str
    shadow_policy_identity: str
    seeds: tuple[int, ...]
    game_count: int
    total_paired_decisions: int
    agreements: int
    disagreements: int
    agreement_rate: float
    action_kind_pairs: tuple[ActionKindPairCount, ...]
    mortal: MortalRuntimeProvenance
    execution: SingleRoundExecutionProvenance

    def __post_init__(self) -> None:
        if self.schema != MORTAL_DECISION_ANALYSIS_SCHEMA:
            raise ValueError(f"unsupported artifact schema: {self.schema!r}")
        if self.diagnostic != MORTAL_DECISION_DIAGNOSTIC:
            raise ValueError(f"unsupported diagnostic identity: {self.diagnostic!r}")
        if self.game_mode != SINGLE_ROUND_GAME_MODE:
            raise ValueError(f"unsupported game mode: {self.game_mode!r}")
        if type(self.shadow_policy_identity) is not str:
            raise TypeError("shadow_policy_identity must be a str")
        if not self.shadow_policy_identity:
            raise ValueError("shadow_policy_identity must not be empty")
        try:
            seeds = tuple(self.seeds)
        except TypeError:
            raise TypeError("seeds must be an iterable") from None
        if any(type(seed) is not int for seed in seeds):
            raise TypeError("seeds must contain only ints")
        if not seeds:
            raise ValueError("seeds must not be empty")
        if len(set(seeds)) != len(seeds):
            raise ValueError("seeds must not contain duplicates")
        for name in (
            "game_count",
            "total_paired_decisions",
            "agreements",
            "disagreements",
        ):
            value = getattr(self, name)
            if type(value) is not int:
                raise TypeError(f"{name} must be an int")
            if value < 0:
                raise ValueError(f"{name} must not be negative")
        if self.game_count != SINGLE_ROUND_ROTATION_COUNT * len(seeds):
            raise ValueError("game_count must equal 4 rotations per seed")
        if self.agreements + self.disagreements != self.total_paired_decisions:
            raise ValueError(
                "agreements and disagreements must sum to total_paired_decisions"
            )
        if type(self.agreement_rate) is not float:
            raise TypeError("agreement_rate must be a float")
        expected_rate = (
            0.0
            if self.total_paired_decisions == 0
            else self.agreements / self.total_paired_decisions
        )
        if self.agreement_rate != expected_rate:
            raise ValueError("agreement_rate must match agreements over total")
        try:
            pairs = tuple(self.action_kind_pairs)
        except TypeError:
            raise TypeError("action_kind_pairs must be an iterable") from None
        if any(not isinstance(item, ActionKindPairCount) for item in pairs):
            raise TypeError("action_kind_pairs must contain only ActionKindPairCount")
        if sum(pair.count for pair in pairs) != self.total_paired_decisions:
            raise ValueError("action_kind_pairs must cover every paired decision")
        if not isinstance(self.mortal, MortalRuntimeProvenance):
            raise TypeError("mortal must be a MortalRuntimeProvenance")
        if not isinstance(self.execution, SingleRoundExecutionProvenance):
            raise TypeError("execution must be a SingleRoundExecutionProvenance")
        object.__setattr__(self, "seeds", seeds)
        object.__setattr__(self, "action_kind_pairs", pairs)


@dataclass(frozen=True, slots=True)
class MortalDecisionAnalysisRow:
    """1 paired decisionのartifact row。

    ``policy_input`` / ``decision_trace``は、lisjong valueを再構築したもの
    ではなくprojectionされたJSON payloadである。artifactからの完全な
    round-trip復元はこのdiagnosticのrequirementではない。
    """

    seed: int
    rotation: int
    mortal_seat: Seat
    decision_ordinal: int
    shadow_policy_identity: str
    agreement: bool
    driver_mortal_action: NormalizedRiichiEnvAction
    shadow_policy_action: NormalizedRiichiEnvAction
    policy_input: Mapping[str, object]
    decision_trace: Mapping[str, object]

    def __post_init__(self) -> None:
        if type(self.seed) is not int:
            raise TypeError("seed must be an int")
        if type(self.rotation) is not int:
            raise TypeError("rotation must be an int")
        if not 0 <= self.rotation < SINGLE_ROUND_ROTATION_COUNT:
            raise ValueError("rotation must be between 0 and 3")
        if not isinstance(self.mortal_seat, Seat):
            raise TypeError("mortal_seat must be a Seat")
        if self.mortal_seat != Seat(self.rotation):
            raise ValueError("mortal_seat must equal Seat(rotation)")
        if type(self.decision_ordinal) is not int:
            raise TypeError("decision_ordinal must be an int")
        if self.decision_ordinal < 0:
            raise ValueError("decision_ordinal must not be negative")
        if type(self.shadow_policy_identity) is not str:
            raise TypeError("shadow_policy_identity must be a str")
        if not self.shadow_policy_identity:
            raise ValueError("shadow_policy_identity must not be empty")
        if type(self.agreement) is not bool:
            raise TypeError("agreement must be a bool")
        for name in ("driver_mortal_action", "shadow_policy_action"):
            value = getattr(self, name)
            if not isinstance(value, NormalizedRiichiEnvAction):
                raise TypeError(f"{name} must be a NormalizedRiichiEnvAction")
            if value.actor != self.mortal_seat:
                raise ValueError(f"{name} actor must match mortal_seat")
        if self.agreement != (self.driver_mortal_action == self.shadow_policy_action):
            raise ValueError("agreement must equal normalized Action equality")
        for name in ("policy_input", "decision_trace"):
            value = getattr(self, name)
            if not isinstance(value, Mapping):
                raise TypeError(f"{name} must be a mapping")
            object.__setattr__(self, name, MappingProxyType(dict(value)))


@dataclass(frozen=True, slots=True)
class MortalDecisionAnalysisArtifact:
    """readback済みartifactと、最小のoffline inspection seam。

    SQL、DataFrame、database、query languageは導入しない。
    """

    manifest: MortalDecisionAnalysisManifest
    decisions: tuple[MortalDecisionAnalysisRow, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.manifest, MortalDecisionAnalysisManifest):
            raise TypeError("manifest must be a MortalDecisionAnalysisManifest")
        try:
            decisions = tuple(self.decisions)
        except TypeError:
            raise TypeError("decisions must be an iterable") from None
        if any(not isinstance(item, MortalDecisionAnalysisRow) for item in decisions):
            raise TypeError("decisions must contain only MortalDecisionAnalysisRow")
        manifest = self.manifest
        if len(decisions) != manifest.total_paired_decisions:
            raise ValueError("decision rows must match manifest total_paired_decisions")
        agreements = sum(row.agreement for row in decisions)
        if agreements != manifest.agreements:
            raise ValueError("decision rows must match manifest agreements")
        if len(decisions) - agreements != manifest.disagreements:
            raise ValueError("decision rows must match manifest disagreements")
        if (
            count_action_kind_pairs(
                (row.driver_mortal_action.kind, row.shadow_policy_action.kind)
                for row in decisions
            )
            != manifest.action_kind_pairs
        ):
            raise ValueError("decision rows must match manifest action_kind_pairs")
        if any(
            row.shadow_policy_identity != manifest.shadow_policy_identity
            for row in decisions
        ):
            raise ValueError("decision rows must match manifest shadow policy identity")
        _validate_canonical_order(decisions, manifest.seeds)
        object.__setattr__(self, "decisions", decisions)

    def select(
        self,
        *,
        agreement: bool | None = None,
        first: int | None = None,
        driver_kind: RiichiEnvActionKind | None = None,
        shadow_kind: RiichiEnvActionKind | None = None,
    ) -> tuple[MortalDecisionAnalysisRow, ...]:
        """canonical orderを保ったままrowsをfilterする。

        引数を省略すれば全paired decisionsを返す。denominatorを維持するため、
        artifactはagreementもdisagreementも保持している。
        """
        if agreement is not None and type(agreement) is not bool:
            raise TypeError("agreement must be a bool or None")
        if first is not None and (type(first) is not int or first < 0):
            raise ValueError("first must be a non-negative int or None")
        if driver_kind is not None and not isinstance(driver_kind, RiichiEnvActionKind):
            raise TypeError("driver_kind must be a RiichiEnvActionKind or None")
        if shadow_kind is not None and not isinstance(shadow_kind, RiichiEnvActionKind):
            raise TypeError("shadow_kind must be a RiichiEnvActionKind or None")
        matches = tuple(
            row
            for row in self.decisions
            if (agreement is None or row.agreement is agreement)
            and (driver_kind is None or row.driver_mortal_action.kind is driver_kind)
            and (shadow_kind is None or row.shadow_policy_action.kind is shadow_kind)
        )
        return matches if first is None else matches[:first]

    def disagreements(
        self,
        *,
        first: int | None = None,
        driver_kind: RiichiEnvActionKind | None = None,
        shadow_kind: RiichiEnvActionKind | None = None,
    ) -> tuple[MortalDecisionAnalysisRow, ...]:
        """agreement == Falseのrowだけを、in-memory resultと同じsemanticで返す。"""
        return self.select(
            agreement=False,
            first=first,
            driver_kind=driver_kind,
            shadow_kind=shadow_kind,
        )


def _build_artifact(
    manifest: MortalDecisionAnalysisManifest,
    decisions: tuple[MortalDecisionAnalysisRow, ...],
) -> MortalDecisionAnalysisArtifact:
    """manifest / rows整合のfailureを、理由を保ったままartifact errorへ写す。"""
    try:
        return MortalDecisionAnalysisArtifact(manifest=manifest, decisions=decisions)
    except (TypeError, ValueError) as exc:
        if isinstance(exc, ArtifactValidationError):
            raise
        raise MortalDecisionAnalysisArtifactError(str(exc)) from exc


def _validate_canonical_order(
    decisions: tuple[MortalDecisionAnalysisRow, ...], seeds: tuple[int, ...]
) -> None:
    """``seed入力順 -> rotation 0..3 -> decision ordinal``順を検証する。"""
    expected_games = [
        (seed, rotation)
        for seed in seeds
        for rotation in range(SINGLE_ROUND_ROTATION_COUNT)
    ]
    game_index = 0
    ordinal = 0
    for row in decisions:
        while (
            game_index < len(expected_games)
            and (row.seed, row.rotation) != (expected_games[game_index])
        ):
            game_index += 1
            ordinal = 0
        if game_index >= len(expected_games):
            raise ValueError("decision rows are not in canonical seed/rotation order")
        if row.decision_ordinal != ordinal:
            raise ValueError("decision ordinals must be contiguous within a game")
        ordinal += 1


# ---------------------------------------------------------------------------
# write
# ---------------------------------------------------------------------------


def _build_manifest(
    result: MortalDecisionEvaluationResult,
    provenance: SingleRoundExecutionProvenance,
) -> MortalDecisionAnalysisManifest:
    """in-memory resultからmanifestをprojectionする(再集計はしない)。"""
    summary = result.summary
    config = result.plan.mortal_config
    return MortalDecisionAnalysisManifest(
        schema=MORTAL_DECISION_ANALYSIS_SCHEMA,
        diagnostic=MORTAL_DECISION_DIAGNOSTIC,
        game_mode=SINGLE_ROUND_GAME_MODE,
        shadow_policy_identity=result.plan.policy.identity,
        seeds=result.plan.seeds,
        game_count=len(result.game_results),
        total_paired_decisions=summary.total_paired_decisions,
        agreements=summary.agreements,
        disagreements=summary.disagreements_count,
        agreement_rate=summary.agreement_rate,
        action_kind_pairs=summary.action_kind_pairs,
        mortal=MortalRuntimeProvenance(
            image=config.image,
            implementation_revision=config.implementation_revision,
            model_sha256=config.model_sha256,
            response_timeout_seconds=config.response_timeout_seconds,
        ),
        execution=provenance,
    )


def _manifest_to_dict(manifest: MortalDecisionAnalysisManifest) -> dict[str, Any]:
    return {
        "action_kind_pairs": [
            {
                "count": pair.count,
                "driver_mortal_kind": pair.driver_mortal_kind.value,
                "shadow_policy_kind": pair.shadow_policy_kind.value,
            }
            for pair in manifest.action_kind_pairs
        ],
        "agreement_rate": manifest.agreement_rate,
        "agreements": manifest.agreements,
        "diagnostic": manifest.diagnostic,
        "disagreements": manifest.disagreements,
        "execution": execution_provenance_to_dict(manifest.execution),
        "game_count": manifest.game_count,
        "game_mode": manifest.game_mode,
        "mortal": {
            "image": manifest.mortal.image,
            "implementation_revision": manifest.mortal.implementation_revision,
            "model_sha256": manifest.mortal.model_sha256,
            "response_timeout_seconds": manifest.mortal.response_timeout_seconds,
        },
        "schema": manifest.schema,
        "seeds": list(manifest.seeds),
        "shadow_policy_identity": manifest.shadow_policy_identity,
        "total_paired_decisions": manifest.total_paired_decisions,
    }


def _record_to_dict(record: object) -> dict[str, Any]:
    return {
        "agreement": record.agreement,
        "decision_ordinal": record.decision_ordinal,
        "decision_trace": _decision_trace_to_dict(record.decision_trace),
        "driver_mortal_action": _normalized_action_to_dict(
            record.driver_mortal_action, "driver_mortal_action"
        ),
        "mortal_seat": _seat_value(record.mortal_seat, "mortal_seat"),
        "policy_input": _policy_input_to_dict(record.policy_input),
        "rotation": record.rotation,
        "seed": record.seed,
        "shadow_policy_action": _normalized_action_to_dict(
            record.shadow_policy_action, "shadow_policy_action"
        ),
        "shadow_policy_identity": record.shadow_policy_identity,
    }


def _canonical_row_text(document: dict[str, Any]) -> str:
    """1 row = 1行のstream-friendly canonical JSON textを返す。"""
    return (
        json.dumps(
            document,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )


def save_mortal_decision_analysis(
    result: MortalDecisionEvaluationResult, path: str | Path
) -> None:
    """成功したMortal診断を、新しいanalysis artifact directoryへ保存する。

    manifestと全decision rowsをserializeし、manifest / rows整合を検証して
    からstaging directoryをfinal pathへrenameする。既存pathは上書きせず
    ``FileExistsError``を送出する。途中で失敗した場合はcompleteに見える
    artifactもpartial fileも残さない。

    保存はdiagnosticの後段でのみ行い、実行semanticsへは影響しない。
    """
    if not isinstance(result, MortalDecisionEvaluationResult):
        raise TypeError("result must be a MortalDecisionEvaluationResult")
    directory = Path(path)
    if directory.exists():
        raise FileExistsError(f"analysis artifact path already exists: {directory}")
    parent = directory.parent
    if not parent.is_dir():
        raise MortalDecisionAnalysisArtifactError(
            f"analysis artifact directory does not exist: {parent}"
        )

    manifest = _build_manifest(result, collect_execution_provenance())
    manifest_text = canonical_json_text(_manifest_to_dict(manifest))
    rows = tuple(_record_to_dict(record) for record in result.summary.records)
    decisions_text = "".join(_canonical_row_text(row) for row in rows)
    # complete artifactとして公開する前に、readbackと同じ整合条件を検証する。
    _build_artifact(manifest, _parse_decision_rows(decisions_text))

    staging = Path(tempfile.mkdtemp(prefix=_STAGING_PREFIX, dir=parent))
    try:
        write_new_artifact_file(staging / DECISIONS_FILENAME, decisions_text)
        write_new_artifact_file(staging / MANIFEST_FILENAME, manifest_text)
        os.rename(staging, directory)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


# ---------------------------------------------------------------------------
# read
# ---------------------------------------------------------------------------

_MANIFEST_KEYS = {
    "action_kind_pairs",
    "agreement_rate",
    "agreements",
    "diagnostic",
    "disagreements",
    "execution",
    "game_count",
    "game_mode",
    "mortal",
    "schema",
    "seeds",
    "shadow_policy_identity",
    "total_paired_decisions",
}

_ROW_KEYS = {
    "agreement",
    "decision_ordinal",
    "decision_trace",
    "driver_mortal_action",
    "mortal_seat",
    "policy_input",
    "rotation",
    "seed",
    "shadow_policy_action",
    "shadow_policy_identity",
}

_NORMALIZED_ACTION_KEYS = {"actor", "consume_tiles", "kind", "tile", "tsumogiri"}
_TILE_KEYS = {"category", "is_red", "rank"}


def _parse_enum(enum_type: type, value: object, context: str):
    raw = expect_str(value, context)
    try:
        return enum_type(raw)
    except ValueError:
        raise MortalDecisionAnalysisArtifactError(
            f"{context} has an unsupported value: {raw!r}"
        ) from None


def _parse_seat(value: object, context: str) -> Seat:
    raw = expect_int(value, context)
    try:
        return Seat(raw)
    except ValueError:
        raise MortalDecisionAnalysisArtifactError(
            f"{context} is not a valid seat: {raw!r}"
        ) from None


def _parse_tile(value: object, context: str) -> Tile:
    raw = expect_object(value, _TILE_KEYS, context)
    category = _parse_enum(TileCategory, raw["category"], f"{context}.category")
    rank = expect_int(raw["rank"], f"{context}.rank")
    is_red = expect_bool(raw["is_red"], f"{context}.is_red")
    try:
        return Tile(tile_type=TileType(category, rank), is_red=is_red)
    except (TypeError, ValueError) as exc:
        raise MortalDecisionAnalysisArtifactError(
            f"{context} is not a valid tile"
        ) from exc


def _parse_optional_tile(value: object, context: str) -> Tile | None:
    return None if value is None else _parse_tile(value, context)


def _parse_normalized_action(value: object, context: str) -> NormalizedRiichiEnvAction:
    raw = expect_object(value, _NORMALIZED_ACTION_KEYS, context)
    tsumogiri = raw["tsumogiri"]
    if tsumogiri is not None:
        tsumogiri = expect_bool(tsumogiri, f"{context}.tsumogiri")
    try:
        return NormalizedRiichiEnvAction(
            kind=_parse_enum(RiichiEnvActionKind, raw["kind"], f"{context}.kind"),
            actor=_parse_seat(raw["actor"], f"{context}.actor"),
            tile=_parse_optional_tile(raw["tile"], f"{context}.tile"),
            consume_tiles=tuple(
                _parse_tile(item, f"{context}.consume_tiles")
                for item in expect_list(
                    raw["consume_tiles"], f"{context}.consume_tiles"
                )
            ),
            tsumogiri=tsumogiri,
        )
    except (TypeError, ValueError) as exc:
        if isinstance(exc, ArtifactValidationError):
            raise
        raise MortalDecisionAnalysisArtifactError(
            f"{context} is not a valid normalized action"
        ) from exc


def _parse_action_kind_pair(value: object, context: str) -> ActionKindPairCount:
    raw = expect_object(
        value, {"count", "driver_mortal_kind", "shadow_policy_kind"}, context
    )
    try:
        return ActionKindPairCount(
            driver_mortal_kind=_parse_enum(
                RiichiEnvActionKind,
                raw["driver_mortal_kind"],
                f"{context}.driver_mortal_kind",
            ),
            shadow_policy_kind=_parse_enum(
                RiichiEnvActionKind,
                raw["shadow_policy_kind"],
                f"{context}.shadow_policy_kind",
            ),
            count=expect_int(raw["count"], f"{context}.count"),
        )
    except (TypeError, ValueError) as exc:
        if isinstance(exc, ArtifactValidationError):
            raise
        raise MortalDecisionAnalysisArtifactError(
            f"{context} is not a valid action-kind pair"
        ) from exc


def _parse_mortal_provenance(value: object) -> MortalRuntimeProvenance:
    raw = expect_object(
        value,
        {
            "image",
            "implementation_revision",
            "model_sha256",
            "response_timeout_seconds",
        },
        "manifest.mortal",
    )
    try:
        return MortalRuntimeProvenance(
            image=expect_str(raw["image"], "manifest.mortal.image"),
            implementation_revision=expect_str(
                raw["implementation_revision"],
                "manifest.mortal.implementation_revision",
            ),
            model_sha256=expect_str(
                raw["model_sha256"], "manifest.mortal.model_sha256"
            ),
            response_timeout_seconds=expect_float(
                raw["response_timeout_seconds"],
                "manifest.mortal.response_timeout_seconds",
            ),
        )
    except (TypeError, ValueError) as exc:
        if isinstance(exc, ArtifactValidationError):
            raise
        raise MortalDecisionAnalysisArtifactError(
            "manifest.mortal is not valid Mortal provenance"
        ) from exc


def _parse_manifest(value: object) -> MortalDecisionAnalysisManifest:
    if type(value) is not dict:
        raise MortalDecisionAnalysisArtifactError("manifest must be an object")
    schema = value.get("schema")
    if schema != MORTAL_DECISION_ANALYSIS_SCHEMA:
        raise MortalDecisionAnalysisArtifactError(
            f"unsupported artifact schema: {schema!r}"
        )
    raw = expect_object(value, _MANIFEST_KEYS, "manifest")
    try:
        return MortalDecisionAnalysisManifest(
            schema=expect_str(raw["schema"], "manifest.schema"),
            diagnostic=expect_str(raw["diagnostic"], "manifest.diagnostic"),
            game_mode=expect_str(raw["game_mode"], "manifest.game_mode"),
            shadow_policy_identity=expect_str(
                raw["shadow_policy_identity"], "manifest.shadow_policy_identity"
            ),
            seeds=tuple(
                expect_int(seed, "manifest.seeds")
                for seed in expect_list(raw["seeds"], "manifest.seeds")
            ),
            game_count=expect_int(raw["game_count"], "manifest.game_count"),
            total_paired_decisions=expect_int(
                raw["total_paired_decisions"], "manifest.total_paired_decisions"
            ),
            agreements=expect_int(raw["agreements"], "manifest.agreements"),
            disagreements=expect_int(raw["disagreements"], "manifest.disagreements"),
            agreement_rate=expect_float(
                raw["agreement_rate"], "manifest.agreement_rate"
            ),
            action_kind_pairs=tuple(
                _parse_action_kind_pair(pair, "manifest.action_kind_pairs")
                for pair in expect_list(
                    raw["action_kind_pairs"], "manifest.action_kind_pairs"
                )
            ),
            mortal=_parse_mortal_provenance(raw["mortal"]),
            execution=parse_execution_provenance(raw["execution"]),
        )
    except (TypeError, ValueError) as exc:
        if isinstance(exc, ArtifactValidationError):
            raise
        raise MortalDecisionAnalysisArtifactError(
            "manifest is malformed or inconsistent"
        ) from exc


def _parse_row(value: object, index: int) -> MortalDecisionAnalysisRow:
    context = f"decisions[{index}]"
    raw = expect_object(value, _ROW_KEYS, context)
    policy_input = raw["policy_input"]
    decision_trace = raw["decision_trace"]
    if type(policy_input) is not dict:
        raise MortalDecisionAnalysisArtifactError(
            f"{context}.policy_input must be an object"
        )
    if type(decision_trace) is not dict:
        raise MortalDecisionAnalysisArtifactError(
            f"{context}.decision_trace must be an object"
        )
    try:
        return MortalDecisionAnalysisRow(
            seed=expect_int(raw["seed"], f"{context}.seed"),
            rotation=expect_int(raw["rotation"], f"{context}.rotation"),
            mortal_seat=_parse_seat(raw["mortal_seat"], f"{context}.mortal_seat"),
            decision_ordinal=expect_int(
                raw["decision_ordinal"], f"{context}.decision_ordinal"
            ),
            shadow_policy_identity=expect_str(
                raw["shadow_policy_identity"], f"{context}.shadow_policy_identity"
            ),
            agreement=expect_bool(raw["agreement"], f"{context}.agreement"),
            driver_mortal_action=_parse_normalized_action(
                raw["driver_mortal_action"], f"{context}.driver_mortal_action"
            ),
            shadow_policy_action=_parse_normalized_action(
                raw["shadow_policy_action"], f"{context}.shadow_policy_action"
            ),
            policy_input=policy_input,
            decision_trace=decision_trace,
        )
    except (TypeError, ValueError) as exc:
        if isinstance(exc, ArtifactValidationError):
            raise
        raise MortalDecisionAnalysisArtifactError(
            f"{context} is malformed or inconsistent"
        ) from exc


def _parse_decision_rows(text: str) -> tuple[MortalDecisionAnalysisRow, ...]:
    rows: list[MortalDecisionAnalysisRow] = []
    for index, line in enumerate(text.splitlines()):
        if not line.strip():
            raise MortalDecisionAnalysisArtifactError(
                f"decisions[{index}] is an empty line"
            )
        try:
            document = json.loads(line)
        except json.JSONDecodeError as exc:
            raise MortalDecisionAnalysisArtifactError(
                f"decisions[{index}] is not valid JSON"
            ) from exc
        rows.append(_parse_row(document, index))
    return tuple(rows)


def load_mortal_decision_analysis(
    path: str | Path,
) -> MortalDecisionAnalysisArtifact:
    """artifact directoryをfail-closedに検証してimmutable snapshotを返す。

    unknown schema、malformed row、manifestとdecision rowsのinconsistency
    (件数、agreement / disagreement、action-kind pair、shadow Policy identity、
    canonical order)をいずれもrejectする。
    """
    directory = Path(path)
    try:
        manifest = _parse_manifest(read_json_document(directory / MANIFEST_FILENAME))
        decisions_text = (directory / DECISIONS_FILENAME).read_text(encoding="utf-8")
        decisions = _parse_decision_rows(decisions_text)
    except MortalDecisionAnalysisArtifactError:
        raise
    except ArtifactValidationError as exc:
        raise MortalDecisionAnalysisArtifactError(str(exc)) from exc
    except (json.JSONDecodeError, UnicodeError, TypeError, ValueError) as exc:
        raise MortalDecisionAnalysisArtifactError(
            "analysis artifact is malformed or inconsistent"
        ) from exc
    return _build_artifact(manifest, decisions)


__all__ = [
    "DECISIONS_FILENAME",
    "MANIFEST_FILENAME",
    "MORTAL_DECISION_ANALYSIS_SCHEMA",
    "MORTAL_DECISION_DIAGNOSTIC",
    "MortalDecisionAnalysisArtifact",
    "MortalDecisionAnalysisArtifactError",
    "MortalDecisionAnalysisManifest",
    "MortalDecisionAnalysisRow",
    "MortalRuntimeProvenance",
    "load_mortal_decision_analysis",
    "save_mortal_decision_analysis",
]
