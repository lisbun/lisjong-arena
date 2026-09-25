"""Issue #375 Heuristic candidate vs Heuristic Champion AABB half-game protocol v1.

cross-family Overall判定(``arena-overall-champion-aabb-half-v1``)とは別の、
family-internalなcandidate評価protocolである。matchupの意味・primary metric・
classification labelが異なるため、Overall protocolへoptionを足して流用せず、
独立したprotocol identityを持つ。

```text
A = candidate  (lock時にbindするHeuristic candidate)
B = incumbent  (lock時にbindするcurrent Heuristic Champion)
game mode      4p-red-half
matchup        AABB, 4 rotations / seed
seed blocks    100 (statistical N)
primary        uma/oka込みfinal scoreのseed-block mean delta (A - B)
```

実行substrate(``ComparisonPlan`` / ``run_comparison()`` / ``ComparisonArtifact``)
とparticipant binding primitive(``ParticipantBinding``等)は既存実装をreuseし、
新しいhalf-game runnerやgeneric evaluation frameworkは作らない。
"""

from __future__ import annotations

from collections.abc import Sequence

from lisjong_arena._artifact_io import ArtifactValidationError, expect_object
from lisjong_arena.overall_champion_aabb.protocol import (
    HEURISTIC_FAMILY,
    OverallChampionProtocolError,
    ParticipantBinding,
    parse_participant_binding,
)
from lisjong_arena.paired_evaluation import INTERVAL_Z
from lisjong_arena.seed_registry import RIICHIENV_HALF_HANCHAN_SEED_DOMAIN

PROTOCOL_ID = "arena-heuristic-candidate-aabb-half-v1"
PROTOCOL_VERSION = 1

OWNER_ISSUE = "lisbun/lisjong-arena#375"
"""seed allocationのowner issue。lockはこのownerのallocationだけを受理する。"""

SEED_DOMAIN = RIICHIENV_HALF_HANCHAN_SEED_DOMAIN
ALLOCATION_POPULATION = "heuristic-candidate-aabb-375"
ALLOCATION_SPLIT = "FORMAL-EVAL"

GAME_MODE = "4p-red-half"
ROTATION_COUNT = 4
SEAT_COUNT = 4
SEED_BLOCK_COUNT = 100
HANCHAN_COUNT = SEED_BLOCK_COUNT * ROTATION_COUNT
SEAT_RESULTS_PER_BLOCK = ROTATION_COUNT * SEAT_COUNT
SEAT_RESULT_COUNT = SEED_BLOCK_COUNT * SEAT_RESULTS_PER_BLOCK
ROLE_SEAT_RESULTS_PER_BLOCK = 8
ROLE_SEAT_EXPOSURE_PER_BLOCK = 2
MAX_STEPS = 10_000

CANDIDATE_ROLE = "candidate"
INCUMBENT_ROLE = "incumbent"
ROLES = (CANDIDATE_ROLE, INCUMBENT_ROLE)
CANDIDATE_SLOT = "A"
INCUMBENT_SLOT = "B"

ROTATION_PLAN = (
    (CANDIDATE_SLOT, CANDIDATE_SLOT, INCUMBENT_SLOT, INCUMBENT_SLOT),
    (INCUMBENT_SLOT, CANDIDATE_SLOT, CANDIDATE_SLOT, INCUMBENT_SLOT),
    (INCUMBENT_SLOT, INCUMBENT_SLOT, CANDIDATE_SLOT, CANDIDATE_SLOT),
    (CANDIDATE_SLOT, INCUMBENT_SLOT, INCUMBENT_SLOT, CANDIDATE_SLOT),
)
"""v1 AABB rotation contract。既存generic comparisonの4 cyclic rotationsと一致する
ことはregression testで拘束する。"""

STARTING_POINTS = 25_000
RETURN_POINTS = 30_000
UMA = (30, 10, -10, -30)
OKA = (20, 0, 0, 0)
"""uma/oka final scoreのprotocol invariant(caller-configurableにしない)。

    final_score = (final_points - RETURN_POINTS) / 1000 + UMA[rank] + OKA[rank]

``OKA[0] = (RETURN_POINTS - STARTING_POINTS) * 4 / 1000``。rankと最終素点は
``LocalGameResult``(engine)の結果をそのまま使い、同点処理や供託の扱いを
protocol側で再定義しない。
"""

FINAL_SCORE_UNITS_PER_POINT = 1000
"""final scoreを整数で扱うためのscale(1 unit = 1素点)。"""

CLASSIFICATION_RULE_ID = "heuristic-candidate-seed-block-uma-oka-normal-approx-95-v1"

CANDIDATE_SUPERIOR_LABEL = "CANDIDATE SUPERIOR"
INCUMBENT_SUPERIOR_LABEL = "CHAMPION SUPERIOR"
INCONCLUSIVE_LABEL = "INCONCLUSIVE"
STOP_INVALID_LABEL = "STOP / INVALID"

CANDIDATE_SUPERIOR_KIND = "CANDIDATE_SUPERIOR"
INCUMBENT_SUPERIOR_KIND = "CHAMPION_SUPERIOR"
INCONCLUSIVE_KIND = "INCONCLUSIVE"

EXECUTION_BRANCH = "main"

if OKA[0] * FINAL_SCORE_UNITS_PER_POINT != (RETURN_POINTS - STARTING_POINTS) * 4:
    raise AssertionError("oka must equal the return-point difference of four seats")
if sum(UMA) != 0 or sum(OKA[1:]) != 0:
    raise AssertionError("uma must be zero-sum and oka must go only to first place")


class HeuristicCandidateProtocolError(ValueError):
    """Issue #375 protocol v1を満たせない場合。"""


def final_score_units(points: int, rank: int) -> int:
    """1 seat-resultのuma/oka込みfinal scoreを``1/1000``単位の整数で返す。"""
    if type(points) is not int:
        raise TypeError("points must be an int")
    if type(rank) is not int:
        raise TypeError("rank must be an int")
    if not 1 <= rank <= SEAT_COUNT:
        raise HeuristicCandidateProtocolError("rank must be between 1 and 4")
    bonus = UMA[rank - 1] + OKA[rank - 1]
    return points - RETURN_POINTS + bonus * FINAL_SCORE_UNITS_PER_POINT


def require_participants(
    candidate: object, incumbent: object
) -> tuple[ParticipantBinding, ParticipantBinding]:
    """candidate / incumbentのexact Heuristic participant pairを検証する。"""
    for role, binding in ((CANDIDATE_ROLE, candidate), (INCUMBENT_ROLE, incumbent)):
        if not isinstance(binding, ParticipantBinding):
            raise HeuristicCandidateProtocolError(
                f"{role} participant must be a ParticipantBinding"
            )
        if binding.family != HEURISTIC_FAMILY:
            raise HeuristicCandidateProtocolError(
                f"{role} participant must be bound to the heuristic family"
            )
        if binding.has_checkpoint:
            raise HeuristicCandidateProtocolError(
                f"{role} participant must not declare a checkpoint in protocol v1"
            )
    assert isinstance(candidate, ParticipantBinding)
    assert isinstance(incumbent, ParticipantBinding)
    if candidate.policy_identity == incumbent.policy_identity:
        raise HeuristicCandidateProtocolError(
            "candidate and incumbent identities must be distinct"
        )
    if candidate.factory_binding == incumbent.factory_binding:
        raise HeuristicCandidateProtocolError(
            "candidate and incumbent factory bindings must be distinct"
        )
    return candidate, incumbent


def parse_participant(value: object, context: str) -> ParticipantBinding:
    try:
        return parse_participant_binding(value, context)
    except OverallChampionProtocolError as exc:
        raise HeuristicCandidateProtocolError(str(exc)) from exc


def require_population(seeds: object) -> tuple[int, ...]:
    """formal v1 populationがexactly 100 unique ordered seedsであることを要求する。"""
    if isinstance(seeds, (str, bytes, bytearray)) or not isinstance(seeds, Sequence):
        raise TypeError("seeds must be an ordered collection of ints")
    ordered = tuple(seeds)
    if any(type(seed) is not int or seed < 0 for seed in ordered):
        raise TypeError("seeds must contain only non-negative exact ints")
    if len(ordered) != SEED_BLOCK_COUNT:
        raise HeuristicCandidateProtocolError(
            f"protocol v1 requires exactly {SEED_BLOCK_COUNT} seed blocks "
            f"but got {len(ordered)}"
        )
    if len(set(ordered)) != len(ordered):
        raise HeuristicCandidateProtocolError(
            "seed population must not contain duplicates"
        )
    return ordered


def classification_document() -> dict[str, object]:
    """lockとresultが共有するprimary statistic / classification rule block。"""
    return {
        "final_score": {
            "formula": (
                "(final_points - return_points) / 1000 + uma[rank] + oka[rank]"
            ),
            "oka": list(OKA),
            "rank_source": "engine LocalGameResult ranks",
            "return_points": RETURN_POINTS,
            "starting_points": STARTING_POINTS,
            "uma": list(UMA),
        },
        "interval_method": (
            "two-sided normal-approximation 95% interval, "
            f"mean +/- {INTERVAL_Z} * standard error"
        ),
        "interval_z": INTERVAL_Z,
        "primary_statistic": (
            "for each locked seed s, D(s) = mean uma/oka final score of the eight "
            "candidate seat-results minus mean uma/oka final score of the eight "
            "incumbent seat-results across the four AABB rotations"
        ),
        "primary_unit": "seed block",
        "rule": (
            "interval lower > 0 -> CANDIDATE SUPERIOR; "
            "interval upper < 0 -> CHAMPION SUPERIOR; "
            "otherwise INCONCLUSIVE; "
            "invalid protocol / provenance / artifact evidence -> STOP / INVALID"
        ),
        "rule_id": CLASSIFICATION_RULE_ID,
        "secondary_metrics_override_primary": False,
        "sign_interpretation": (
            "D > 0 -> candidate advantage; D < 0 -> incumbent advantage"
        ),
        "statistical_n": (
            f"{SEED_BLOCK_COUNT} seed blocks, never {HANCHAN_COUNT} hanchan or "
            f"{SEED_BLOCK_COUNT * ROLE_SEAT_RESULTS_PER_BLOCK} seat-results per role"
        ),
    }


def rotation_plan_document() -> list[list[str]]:
    return [list(assignment) for assignment in ROTATION_PLAN]


def protocol_document(seeds: object) -> dict[str, object]:
    ordered = require_population(seeds)
    return {
        "classification": classification_document(),
        "game_mode": GAME_MODE,
        "hanchan_count": HANCHAN_COUNT,
        "max_steps": MAX_STEPS,
        "ordered_seeds": list(ordered),
        "participant_slots": {
            CANDIDATE_SLOT: CANDIDATE_ROLE,
            INCUMBENT_SLOT: INCUMBENT_ROLE,
        },
        "protocol_id": PROTOCOL_ID,
        "protocol_version": PROTOCOL_VERSION,
        "role_seat_exposure_per_block": ROLE_SEAT_EXPOSURE_PER_BLOCK,
        "role_seat_results_per_block": ROLE_SEAT_RESULTS_PER_BLOCK,
        "rotation_count": ROTATION_COUNT,
        "rotation_plan": rotation_plan_document(),
        "seat_result_count": SEAT_RESULT_COUNT,
        "seed_allocation": {
            "owner_issue": OWNER_ISSUE,
            "population": ALLOCATION_POPULATION,
            "seed_domain": SEED_DOMAIN,
            "split": ALLOCATION_SPLIT,
        },
        "seed_block_count": SEED_BLOCK_COUNT,
    }


_PROTOCOL_FIELDS = set(protocol_document(tuple(range(SEED_BLOCK_COUNT))))


def require_protocol_document(value: object, context: str) -> tuple[int, ...]:
    """persisted protocol blockを読み戻し、v1 contractとの完全一致を要求する。"""
    try:
        raw = expect_object(value, _PROTOCOL_FIELDS, context)
    except ArtifactValidationError as exc:
        raise HeuristicCandidateProtocolError(str(exc)) from exc
    ordered_seeds = raw["ordered_seeds"]
    if type(ordered_seeds) is not list:
        raise HeuristicCandidateProtocolError(f"{context}.ordered_seeds must be a list")
    seeds = require_population(tuple(ordered_seeds))
    if dict(raw) != protocol_document(seeds):
        raise HeuristicCandidateProtocolError(
            f"{context} differs from locked protocol v1"
        )
    return seeds


__all__ = [
    "ALLOCATION_POPULATION",
    "ALLOCATION_SPLIT",
    "CANDIDATE_ROLE",
    "CANDIDATE_SLOT",
    "CANDIDATE_SUPERIOR_KIND",
    "CANDIDATE_SUPERIOR_LABEL",
    "CLASSIFICATION_RULE_ID",
    "EXECUTION_BRANCH",
    "FINAL_SCORE_UNITS_PER_POINT",
    "GAME_MODE",
    "HANCHAN_COUNT",
    "INCONCLUSIVE_KIND",
    "INCONCLUSIVE_LABEL",
    "INCUMBENT_ROLE",
    "INCUMBENT_SLOT",
    "INCUMBENT_SUPERIOR_KIND",
    "INCUMBENT_SUPERIOR_LABEL",
    "MAX_STEPS",
    "OKA",
    "OWNER_ISSUE",
    "PROTOCOL_ID",
    "PROTOCOL_VERSION",
    "RETURN_POINTS",
    "ROLES",
    "ROLE_SEAT_EXPOSURE_PER_BLOCK",
    "ROLE_SEAT_RESULTS_PER_BLOCK",
    "ROTATION_COUNT",
    "ROTATION_PLAN",
    "SEAT_COUNT",
    "SEAT_RESULTS_PER_BLOCK",
    "SEAT_RESULT_COUNT",
    "SEED_BLOCK_COUNT",
    "SEED_DOMAIN",
    "STARTING_POINTS",
    "STOP_INVALID_LABEL",
    "UMA",
    "HeuristicCandidateProtocolError",
    "classification_document",
    "final_score_units",
    "parse_participant",
    "protocol_document",
    "require_participants",
    "require_population",
    "require_protocol_document",
    "rotation_plan_document",
]
