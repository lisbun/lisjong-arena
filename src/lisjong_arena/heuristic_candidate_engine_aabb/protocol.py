"""protocol ``arena-heuristic-candidate-engine-aabb-half-v1`` の固定値（#514、仕様§1〜§5）。

旧RiichiEnv版（``heuristic_candidate_aabb``）へのbackend option追加ではない独立protocol
である。ここで固定する値はcaller-configurableなdefaultを持たない。

```text
engine        lisjong-engine 91af75e3aa11520c3b0543719dc74bb4c517ee06
rules         RuleSet.default() の全field値（§5.2）。rules_digest で拘束
game mode     hanchan / 4 players、東1局・起家seat 0・25000点、MatchState(seed, rules)
schedule      lockのordered seeds の順 -> placement 0..5（§2.2）
主指標        FinalPlayerScore.final_points（内部単位 1 = 0.1 pt）
block統計     D(s) = (S_A - S_B) / 12 / 10、mean ± 1.96 × SD(K-1) / sqrt(K)
classification heuristic-candidate-engine-seed-block-final-points-normal-approx-95-v1
```

digestは既存artifactと同じcanonical JSON（key sort、indent 2、enumは``.value``、集合は
sortしたlist、tupleはlist、``None``は``null``）のSHA-256である（§5.6）。
"""

import dataclasses
import enum
import importlib
import inspect
import re
from collections.abc import Sequence

from lisjong_engine.rules import RuleSet

from lisjong_arena.overall_champion_aabb.lock import document_identity

from .errors import EngineAabbRecordError
from .kyoku_record import (
    PLACEMENT_CANDIDATE_SEATS,
    PROTOCOL_ID,
    ROLE_CANDIDATE,
    ROLE_INCUMBENT,
    ExpectedHanchan,
    SeatAssignment,
    placement_roles,
)

PROTOCOL_VERSION = 1
ENGINE_REVISION = "91af75e3aa11520c3b0543719dc74bb4c517ee06"
INTERVAL_Z = 1.96
SEAT_RESULTS_PER_ROLE_PER_BLOCK = 12
INTERNAL_UNITS_PER_POINT = 10
CLASSIFICATION_RULE_ID = (
    "heuristic-candidate-engine-seed-block-final-points-normal-approx-95-v1"
)
CANDIDATE_SUPERIOR = "CANDIDATE SUPERIOR"
INCUMBENT_SUPERIOR = "CHAMPION SUPERIOR"
INCONCLUSIVE = "INCONCLUSIVE"

SEED_DOMAIN = "lisjong-engine-project-standard-v1-hanchan-v1"
FORMAL_SPLIT = "FORMAL-EVAL"
POPULATION_PREFIX = "heuristic-candidate-engine-aabb-"
MINIMUM_BLOCKS = 2
"""SDが定義できる算術上の下限。正式評価の最小Kではない（J1は校正後に決める）。"""

REGISTRY_EXTERNAL_SEEDS = {
    "lisbun/lisjong-arena#472": tuple(range(472_000, 472_007)),
    "lisbun/lisjong-arena#513": (513_000,),
}
"""Seed Registryのallocation外で使ったengine domainの既知seed。正式populationから除く。

#472: 開発用試行（§14）。#513: 実engineの1半荘を回すtest（#514のtestも同じseedを使う）。
"""

PARTICIPANT_FAMILY = "heuristic"
FACTORY_MODULE = "lisjong_arena.policy_catalog"
BACKEND = "rust"

RULES_DIGEST = "1393f5021bd332ca6efe733c12fd79e011b55706ae559db894b0619cd0fa9626"
PROTOCOL_DIGEST = "e60f75d7c32695e1c79225cad409334c3233dc8dd8b1cd0c6c7995789c9adec2"
"""§5.6のdigest。``rules_document()`` / ``protocol_document()`` との一致をimport時に確かめる。"""

EXPECTED_RULES = {
    "bankrupt_player_penalty_points": -10,
    "bankruptcy_bonus_points": 10,
    "bankruptcy_enabled": True,
    "bankruptcy_threshold": 0,
    "counted_yakuman_enabled": True,
    "dealer_tenpai_end_enabled": True,
    "dealer_win_end_enabled": True,
    "double_ron_enabled": True,
    "double_wind_pair_fu": 4,
    "double_yakuman_variants": [],
    "final_points_rounding": "toward_zero_remainder_to_first",
    "final_rank_tie_policy": "seat_order",
    "first_place_target_points": 30000,
    "four_kans_abortive_draw_enabled": True,
    "four_riichi_abortive_draw_enabled": True,
    "four_winds_abortive_draw_enabled": True,
    "kan_dora_reveal_policy": "delay_open_kan_dora",
    "kokushi_ankan_chankan_enabled": False,
    "match_format": "hanchan",
    "multiple_ron_honba_policy": "nearest_winner_to_discarder",
    "multiple_ron_riichi_stick_policy": "nearest_winner_to_discarder",
    "multiple_yakuman_enabled": True,
    "nagashi_mangan_enabled": True,
    "name": "project-standard-v1",
    "nine_terminals_abortive_draw_enabled": True,
    "noten_penalty_total": 3000,
    "pao_compound_yakuman_policy": "full_hand",
    "pao_enabled": True,
    "pao_yaku": ["daisangen", "daisuushii"],
    "player_count": 4,
    "return_points": 30000,
    "riichi_ankan_policy": "preserve_wait_and_decomposition",
    "riichi_minimum_live_wall_tiles": 4,
    "riichi_minimum_points": 1000,
    "riichi_stick_points": 1000,
    "ron_honba_points": 300,
    "ron_resolution_policy": "multiple_ron",
    "rounded_mangan_enabled": False,
    "starting_points": 25000,
    "triple_ron_abortive_draw": True,
    "tsumo_honba_points_per_payer": 100,
    "uma": [30, 10, -10, -30],
    "version": 1,
    "west_round_enabled": True,
}
"""§5.2の表をそのまま書いた値。``dataclasses.fields(RuleSet)`` の集合とも一致を要求する。"""

MATCH_END_CONDITIONS = (
    "bankruptcy: a seat below 0 after round settlement (highest priority)",
    "final_round: West 4 always ends",
    "dealer_win / dealer_tenpai: South 4 or West 1-3, dealer continues and is "
    "first with >= 30000",
    "target_reached: South 4 or West 1-3, dealer does not continue and a seat has "
    ">= 30000",
    "otherwise continue (South 4 dealer flow below 30000 enters West)",
)

NO_RESCUE_BOUNDARY = (
    "no seed is added, replaced or dropped after result exposure",
    "K, the primary statistic, interval, classification rule, RuleSet and "
    "participants are never changed after result exposure",
    "partial or failed execution is never partially adopted",
    "secondary metrics never override the primary classification",
    "INCONCLUSIVE is a valid terminal outcome, not permission to add seeds",
    "an aborted run is repeated only for the whole locked schedule with the same "
    "lock, seeds and destinations",
)

_HEX40 = re.compile(r"[0-9a-f]{40}")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_IDENTITY = re.compile(r"[a-z0-9][a-z0-9._-]*")


class EngineAabbProtocolError(EngineAabbRecordError):
    """protocol / lock / resultの値が契約を満たさない場合。"""


# ---------------------------------------------------------------------------
# rules / protocol / schedule documents
# ---------------------------------------------------------------------------


def canonical_value(value: object) -> object:
    """§5.6の変換（enumは``.value``、集合はsortしたlist、tupleはlist）。"""
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, (set, frozenset)):
        return sorted(canonical_value(item) for item in value)
    if isinstance(value, (tuple, list)):
        return [canonical_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): canonical_value(item) for key, item in value.items()}
    return value


def rules_document(rules: RuleSet) -> dict[str, object]:
    """``dataclasses.fields(RuleSet)`` を列挙したrules document（§5.2）。"""
    if not isinstance(rules, RuleSet):
        raise EngineAabbProtocolError("rules must be a lisjong-engine RuleSet")
    return {
        field.name: canonical_value(getattr(rules, field.name))
        for field in dataclasses.fields(RuleSet)
    }


def locked_rules() -> RuleSet:
    """lockするRuleSet。全field値が§5.2と一致しなければfail closedする。"""
    rules = RuleSet.default()
    document = rules_document(rules)
    if set(document) != set(EXPECTED_RULES):
        raise EngineAabbProtocolError(
            "RuleSet fields differ from the locked field set: "
            f"{sorted(set(document) ^ set(EXPECTED_RULES))}"
        )
    if document != EXPECTED_RULES:
        raise EngineAabbProtocolError("RuleSet.default() values differ from §5.2")
    return rules


def protocol_document() -> dict[str, object]:
    """protocol identityを構成する値（§5.6 protocol_digestの対象）。"""
    return {
        "classification": {
            "interval": "mean D +/- z * SD(K-1) / sqrt(K), two-sided normal approx",
            "labels": {
                "lower_above_zero": CANDIDATE_SUPERIOR,
                "otherwise": INCONCLUSIVE,
                "upper_below_zero": INCUMBENT_SUPERIOR,
            },
            "rule_id": CLASSIFICATION_RULE_ID,
            "z": INTERVAL_Z,
        },
        "engine_revision": ENGINE_REVISION,
        "game_mode": {
            "dealer_start_seat": 0,
            "match_format": "hanchan",
            "player_count": 4,
            "starting_points": 25000,
            "start": "MatchState(seed, rules) at East 1, no starting_scores",
        },
        "match_end_conditions": list(MATCH_END_CONDITIONS),
        "placements": [list(seats) for seats in PLACEMENT_CANDIDATE_SEATS],
        "primary_metric": {
            "block_difference": "D(s) = (S_A(s) - S_B(s)) / 12 / 10",
            "source": "CompletedMatch.final_score.for_seat(seat).final_points",
            "unit": "internal, 1 = 0.1 pt",
        },
        "protocol_id": PROTOCOL_ID,
        "protocol_version": PROTOCOL_VERSION,
        "rules_digest": document_identity(EXPECTED_RULES),
    }


def placement_schedule(seeds: Sequence[int]) -> list[dict[str, object]]:
    """seed順 -> placement 0..5の実行順（§2.2）。seedの妥当性はcallerが検査する。"""
    return [
        {
            "placement": placement,
            "roles": list(placement_roles(placement)),
            "seed": seed,
        }
        for seed in seeds
        for placement in range(len(PLACEMENT_CANDIDATE_SEATS))
    ]


def schedule_document(ordered_seeds: Sequence[int]) -> dict[str, object]:
    """正式populationのschedule（§5.5 schedule_digestの対象）。"""
    return {
        "hanchans": placement_schedule(require_seeds(ordered_seeds)),
        "protocol_id": PROTOCOL_ID,
    }


def require_seeds(values: object) -> tuple[int, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise EngineAabbProtocolError("ordered seeds must be a sequence of ints")
    seeds = tuple(values)
    if any(type(seed) is not int or not 0 <= seed < 2**32 for seed in seeds):
        raise EngineAabbProtocolError("seeds must be unsigned 32-bit ints")
    if len(set(seeds)) != len(seeds):
        raise EngineAabbProtocolError("ordered seeds must not repeat")
    if len(seeds) < MINIMUM_BLOCKS:
        raise EngineAabbProtocolError(f"K must be at least {MINIMUM_BLOCKS}")
    external = sorted(
        seed
        for seeds_used in REGISTRY_EXTERNAL_SEEDS.values()
        for seed in seeds_used
        if seed in seeds
    )
    if external:
        raise EngineAabbProtocolError(
            f"seeds used outside the registry must not be reused: {external}"
        )
    return seeds


# ---------------------------------------------------------------------------
# participants
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True, slots=True)
class Participant:
    """lockがbindする1 participant（§5.4）。factory自体は保存しない。"""

    policy_identity: str
    factory_binding: str
    lisjong_revision: str
    behavior_configuration: dict

    def to_document(self) -> dict[str, object]:
        return {
            "artifact": None,
            "backend": BACKEND,
            "behavior_configuration": self.behavior_configuration,
            "behavior_configuration_digest": document_identity(
                {"behavior_configuration": self.behavior_configuration}
            ),
            "factory_binding": self.factory_binding,
            "family": PARTICIPANT_FAMILY,
            "lisjong_revision": self.lisjong_revision,
            "policy_identity": self.policy_identity,
        }


_PARTICIPANT_FIELDS = frozenset(
    {
        "artifact",
        "backend",
        "behavior_configuration",
        "behavior_configuration_digest",
        "factory_binding",
        "family",
        "lisjong_revision",
        "policy_identity",
    }
)


def resolve_factory(participant: Participant):
    """factory bindingを解決する。``policy_catalog``のheuristic factoryだけを受理する。"""
    module_name, _, name = participant.factory_binding.partition(":")
    if module_name != FACTORY_MODULE or not name.startswith("create_"):
        raise EngineAabbProtocolError(
            f"factory must be {FACTORY_MODULE}:create_*; learned artifacts and "
            "other factories are not accepted"
        )
    module = importlib.import_module(module_name)
    factory = getattr(module, name, None)
    catalog = getattr(module, "POLICY_CATALOG")
    spec = catalog.get(participant.policy_identity)
    if factory is None or spec is None or spec.factory is not factory:
        raise EngineAabbProtocolError(
            f"{participant.policy_identity!r} is not bound to {participant.factory_binding}"
        )
    try:
        inspect.signature(factory).bind(**participant.behavior_configuration)
    except TypeError as error:
        raise EngineAabbProtocolError(
            f"behavior_configuration does not fit the factory: {error}"
        ) from error
    return factory


def parse_participant(value: object, context: str) -> Participant:
    if type(value) is not dict or set(value) != _PARTICIPANT_FIELDS:
        raise EngineAabbProtocolError(f"{context} fields do not match the schema")
    identity = value["policy_identity"]
    if type(identity) is not str or not _IDENTITY.fullmatch(identity):
        raise EngineAabbProtocolError(f"{context}.policy_identity is invalid")
    if type(value["factory_binding"]) is not str:
        raise EngineAabbProtocolError(f"{context}.factory_binding must be a string")
    revision = value["lisjong_revision"]
    if type(revision) is not str or not _HEX40.fullmatch(revision):
        raise EngineAabbProtocolError(f"{context}.lisjong_revision must be 40-hex")
    if value["family"] != PARTICIPANT_FAMILY:
        raise EngineAabbProtocolError(f"{context}.family must be heuristic")
    if value["backend"] != BACKEND:
        raise EngineAabbProtocolError(f"{context}.backend must be rust")
    if value["artifact"] is not None:
        raise EngineAabbProtocolError(f"{context}: learned artifacts are not accepted")
    configuration = value["behavior_configuration"]
    if type(configuration) is not dict:
        raise EngineAabbProtocolError(
            f"{context}.behavior_configuration must be an object"
        )
    participant = Participant(
        identity, value["factory_binding"], revision, configuration
    )
    if participant.to_document() != value:
        raise EngineAabbProtocolError(
            f"{context}.behavior_configuration_digest mismatch"
        )
    resolve_factory(participant)
    return participant


def require_pair(candidate: Participant, incumbent: Participant) -> None:
    """A / Bはidentityとfactory bindingが異なり、同じlisjong revisionから作る。"""
    if candidate.policy_identity == incumbent.policy_identity:
        raise EngineAabbProtocolError("candidate and incumbent identities must differ")
    if candidate.factory_binding == incumbent.factory_binding:
        raise EngineAabbProtocolError("candidate and incumbent factories must differ")
    if candidate.lisjong_revision != incumbent.lisjong_revision:
        raise EngineAabbProtocolError(
            "candidate and incumbent must come from the same lisjong revision"
        )


def expected_schedule(
    hanchans: Sequence[dict], candidate: Participant, incumbent: Participant
) -> list[ExpectedHanchan]:
    """schedule itemからrecordのK1 / K2照合に渡す期待値を作る。"""
    identities = {
        ROLE_CANDIDATE: candidate.policy_identity,
        ROLE_INCUMBENT: incumbent.policy_identity,
    }
    return [
        ExpectedHanchan(
            seed=item["seed"],
            placement=item["placement"],
            seats=tuple(
                SeatAssignment(role, identities[role]) for role in item["roles"]
            ),
        )
        for item in hanchans
    ]


def require_hex(value: object, length: int, context: str) -> str:
    pattern = _HEX40 if length == 40 else _HEX64
    if type(value) is not str or not pattern.fullmatch(value):
        raise EngineAabbProtocolError(f"{context} must be {length}-hex")
    return value


def _self_check() -> None:
    if document_identity(EXPECTED_RULES) != RULES_DIGEST:
        raise EngineAabbProtocolError("RULES_DIGEST does not match EXPECTED_RULES")
    if document_identity(protocol_document()) != PROTOCOL_DIGEST:
        raise EngineAabbProtocolError("PROTOCOL_DIGEST does not match the document")


_self_check()

__all__ = [
    "BACKEND",
    "CANDIDATE_SUPERIOR",
    "CLASSIFICATION_RULE_ID",
    "ENGINE_REVISION",
    "EXPECTED_RULES",
    "FORMAL_SPLIT",
    "INCONCLUSIVE",
    "INCUMBENT_SUPERIOR",
    "INTERVAL_Z",
    "MINIMUM_BLOCKS",
    "NO_RESCUE_BOUNDARY",
    "POPULATION_PREFIX",
    "PROTOCOL_DIGEST",
    "PROTOCOL_VERSION",
    "REGISTRY_EXTERNAL_SEEDS",
    "RULES_DIGEST",
    "SEED_DOMAIN",
    "EngineAabbProtocolError",
    "Participant",
    "canonical_value",
    "expected_schedule",
    "locked_rules",
    "parse_participant",
    "placement_schedule",
    "protocol_document",
    "require_pair",
    "require_seeds",
    "resolve_factory",
    "rules_document",
    "schedule_document",
]
