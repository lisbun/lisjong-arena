"""Issue #263 targeted honor-release development protocol.

This package evaluates the exact lisjong #174 candidate without recreating its
selection semantics in Arena.  Phase A replays the retained #252 parent
population for diagnostic-only observation.  Phase B, when authorized by the
Phase-A gate, uses one prelocked fresh contiguous 100-seed development
population under the same passive-tsumogiri x3 ABBB shape.
"""

from __future__ import annotations

from dataclasses import dataclass

from lisjong.policies.mechanism_riichi_defense_yakuhai_call import (
    MechanismRiichiDefenseYakuhaiCallPolicy,
)
from lisjong.policies.targeted_honor_release_terminal_progression import (
    TargetedHonorReleaseAnalysis,
    TargetedHonorReleaseTerminalProgressionPolicy,
)
from lisjong_arena.learned_policy_offline_q.p1_gate_b_comparator import (
    PASSIVE_TSUMOGIRI_IDENTITY,
    PASSIVE_TSUMOGIRI_SEMANTICS,
    PassiveTsumogiriPolicy,
    create_passive_tsumogiri,
)
from lisjong_arena.model import (
    SINGLE_ROUND_GAME_MODE,
    SINGLE_ROUND_ROTATION_COUNT,
    PolicySpec,
)
from lisjong_arena.policy_catalog import POLICY_CATALOG
from lisjong_arena.progression_development.protocol import (
    PHASE_B_SEEDS as ISSUE_252_PARENT_SEEDS,
)

PROTOCOL_ID = "targeted-honor-release-development-v1"
LISJONG_REVISION = "f29d129c67e5232d06563c6e457754377734ed14"

CANDIDATE_IDENTITY = "targeted-honor-release-terminal-progression"
CANDIDATE_CLASS_NAME = "TargetedHonorReleaseTerminalProgressionPolicy"
CANDIDATE_SOURCE_MODULE = (
    "lisjong.policies.targeted_honor_release_terminal_progression"
)
ANALYSIS_CLASS_NAME = "TargetedHonorReleaseAnalysis"

PARENT_IDENTITY = "mechanism-riichi-defense"
PARENT_CLASS_NAME = "MechanismRiichiDefenseYakuhaiCallPolicy"
COMPARATOR_IDENTITY = PASSIVE_TSUMOGIRI_IDENTITY
COMPARATOR_SEMANTICS = PASSIVE_TSUMOGIRI_SEMANTICS

GAME_MODE = SINGLE_ROUND_GAME_MODE
ROTATION_COUNT = SINGLE_ROUND_ROTATION_COUNT
MAX_STEPS = 10_000
FORMAL_TEST = False
EXECUTION_BRANCH = "main"

PHASE_A_SEEDS = ISSUE_252_PARENT_SEEDS
PHASE_A_GAME_COUNT = len(PHASE_A_SEEDS) * ROTATION_COUNT
PHASE_B_SEED_BLOCK_COUNT = 100
PHASE_B_GAMES_PER_ARM = PHASE_B_SEED_BLOCK_COUNT * ROTATION_COUNT
PHASE_B_TOTAL_GAMES = 2 * PHASE_B_GAMES_PER_ARM
FEASIBILITY_WALL_CLOCK_LIMIT_HOURS = 8.0

DIAGNOSTIC_COMPLETE_LABEL = "TARGETED HONOR-RELEASE DIAGNOSTIC COMPLETE — PROCEED"
OPPORTUNITY_NOT_OBSERVED_LABEL = "TARGETED HONOR-RELEASE OPPORTUNITY NOT OBSERVED"
INFEASIBLE_LABEL = "TARGETED HONOR-RELEASE DEVELOPMENT EVALUATION INFEASIBLE"
TRAJECTORY_FAILURE_LABEL = "TRAJECTORY IDENTITY FAILURE"
STOP_INVALID_LABEL = "STOP / INVALID"

SIGNAL_LABEL = "TARGETED HONOR-RELEASE DEVELOPMENT SIGNAL"
NEGATIVE_LABEL = "TARGETED HONOR-RELEASE DEVELOPMENT NEGATIVE"
INCONCLUSIVE_LABEL = "TARGETED HONOR-RELEASE DEVELOPMENT INCONCLUSIVE"
CLASSIFICATION_RULE_ID = "paired-seed-block-normal-approx-95-interval-v1"


class TargetedHonorReleaseProtocolError(ValueError):
    """Issue #263 locked protocol cannot be satisfied."""


@dataclass(frozen=True, slots=True)
class CandidateBinding:
    identity: str
    source_module: str
    class_name: str
    analysis_class_name: str
    parent_class_name: str

    def to_document(self) -> dict[str, object]:
        return {
            "analysis_class_name": self.analysis_class_name,
            "class_name": self.class_name,
            "identity": self.identity,
            "parent_class_name": self.parent_class_name,
            "source_module": self.source_module,
        }


def create_candidate() -> TargetedHonorReleaseTerminalProgressionPolicy:
    return TargetedHonorReleaseTerminalProgressionPolicy()


def candidate_spec() -> PolicySpec:
    return PolicySpec(identity=CANDIDATE_IDENTITY, factory=create_candidate)


def parent_spec() -> PolicySpec:
    return POLICY_CATALOG[PARENT_IDENTITY]


def comparator_spec() -> PolicySpec:
    return PolicySpec(identity=COMPARATOR_IDENTITY, factory=create_passive_tsumogiri)


def require_exact_candidate_semantics() -> CandidateBinding:
    candidate_class = TargetedHonorReleaseTerminalProgressionPolicy
    if candidate_class.__name__ != CANDIDATE_CLASS_NAME:
        raise TargetedHonorReleaseProtocolError("candidate class identity drifted")
    if candidate_class.__module__ != CANDIDATE_SOURCE_MODULE:
        raise TargetedHonorReleaseProtocolError("candidate source module drifted")
    if TargetedHonorReleaseAnalysis.__name__ != ANALYSIS_CLASS_NAME:
        raise TargetedHonorReleaseProtocolError("candidate analysis identity drifted")

    parent = parent_spec().factory()
    if type(parent) is not MechanismRiichiDefenseYakuhaiCallPolicy:
        raise TargetedHonorReleaseProtocolError("exact parent class identity drifted")
    if type(parent).__name__ != PARENT_CLASS_NAME:
        raise TargetedHonorReleaseProtocolError("exact parent class name drifted")
    if not issubclass(candidate_class, type(parent)):
        raise TargetedHonorReleaseProtocolError(
            "candidate is not a generation of the exact parent"
        )
    return CandidateBinding(
        identity=CANDIDATE_IDENTITY,
        source_module=CANDIDATE_SOURCE_MODULE,
        class_name=CANDIDATE_CLASS_NAME,
        analysis_class_name=ANALYSIS_CLASS_NAME,
        parent_class_name=PARENT_CLASS_NAME,
    )


def require_exact_comparator() -> dict[str, object]:
    comparator = create_passive_tsumogiri()
    if type(comparator) is not PassiveTsumogiriPolicy:
        raise TargetedHonorReleaseProtocolError(
            "passive comparator implementation identity drifted"
        )
    return {
        "identity": COMPARATOR_IDENTITY,
        "semantics": list(COMPARATOR_SEMANTICS),
        "implementation": (
            "lisjong_arena.learned_policy_offline_q.p1_gate_b_comparator:"
            "PassiveTsumogiriPolicy"
        ),
    }


def require_phase_a_population(seeds: object) -> tuple[int, ...]:
    if not isinstance(seeds, tuple) or seeds != PHASE_A_SEEDS:
        raise TargetedHonorReleaseProtocolError(
            "Phase A must use exact retained #252 parent seeds 651..750"
        )
    return seeds


def require_phase_b_population(seeds: object) -> tuple[int, ...]:
    if isinstance(seeds, (str, bytes, bytearray)):
        raise TypeError("Phase B seeds must be an ordered collection of ints")
    try:
        ordered = tuple(seeds)  # type: ignore[arg-type]
    except TypeError:
        raise TypeError(
            "Phase B seeds must be an ordered collection of ints"
        ) from None
    if any(type(seed) is not int for seed in ordered):
        raise TypeError("Phase B seeds must contain only exact ints")
    if len(ordered) != PHASE_B_SEED_BLOCK_COUNT:
        raise TargetedHonorReleaseProtocolError(
            f"Phase B must contain exactly {PHASE_B_SEED_BLOCK_COUNT} seed blocks"
        )
    if ordered != tuple(range(ordered[0], ordered[0] + PHASE_B_SEED_BLOCK_COUNT)):
        raise TargetedHonorReleaseProtocolError(
            "Phase B seeds must be one contiguous increasing range"
        )
    if set(ordered).intersection(PHASE_A_SEEDS):
        raise TargetedHonorReleaseProtocolError(
            "Phase B fresh seeds must not overlap the #252 diagnostic population"
        )
    return ordered


def repository_declared_allocated_seeds() -> frozenset[int]:
    """Return repository-declared allocations known before #263.

    This intentionally reuses the latest existing allocation audit and adds the
    completed #252 populations, which are not part of the older learned-policy
    allocation chain.
    """
    from lisjong_arena.learned_policy_offline_q.p6_higher_fidelity import (
        DEFAULT_ORDERED_SEEDS as ISSUE_185_SEEDS,
    )
    from lisjong_arena.learned_policy_offline_q.p6_higher_fidelity import (
        declared_allocated_seeds as declared_before_263,
    )
    from lisjong_arena.progression_development.protocol import (
        PHASE_A_SEEDS as ISSUE_252_TECHNICAL_SEEDS,
    )

    return (
        frozenset(declared_before_263())
        | frozenset(ISSUE_185_SEEDS)
        | frozenset(ISSUE_252_TECHNICAL_SEEDS)
        | frozenset(ISSUE_252_PARENT_SEEDS)
    )


def seed_freshness_block(
    ordered_seeds: object,
    *,
    external_freshness_confirmed: bool,
    additional_allocated_seeds: object = (),
) -> dict[str, object]:
    seeds = require_phase_b_population(ordered_seeds)
    if type(external_freshness_confirmed) is not bool:
        raise TypeError("external_freshness_confirmed must be an exact bool")
    if not external_freshness_confirmed:
        raise TargetedHonorReleaseProtocolError(
            "open/closed relevant Issues plus known local/private allocations "
            "must be reviewed before the pre-execution lock"
        )
    if isinstance(additional_allocated_seeds, (str, bytes, bytearray)):
        raise TypeError("additional_allocated_seeds must be an iterable of ints")
    try:
        additional = frozenset(additional_allocated_seeds)  # type: ignore[arg-type]
    except TypeError:
        raise TypeError(
            "additional_allocated_seeds must be an iterable of ints"
        ) from None
    if any(type(seed) is not int for seed in additional):
        raise TypeError("additional_allocated_seeds must contain only exact ints")

    repository_collisions = sorted(
        repository_declared_allocated_seeds().intersection(seeds)
    )
    external_collisions = sorted(additional.intersection(seeds))
    if repository_collisions or external_collisions:
        raise TargetedHonorReleaseProtocolError(
            "SEED PLAN REFORMULATE: Phase B seed collision found before result "
            f"exposure (repository={repository_collisions!r}, "
            f"external={external_collisions!r})"
        )
    return {
        "additional_allocated_seeds": sorted(additional),
        "external_freshness_confirmed": True,
        "fresh": True,
        "ordered_seeds": list(seeds),
        "repository_collisions": [],
        "external_collisions": [],
    }


def protocol_document(phase_b_seeds: object) -> dict[str, object]:
    seeds = require_phase_b_population(phase_b_seeds)
    return {
        "classification_rule_id": CLASSIFICATION_RULE_ID,
        "execution_branch": EXECUTION_BRANCH,
        "feasibility_wall_clock_limit_hours": FEASIBILITY_WALL_CLOCK_LIMIT_HOURS,
        "formal_test": FORMAL_TEST,
        "game_mode": GAME_MODE,
        "max_steps": MAX_STEPS,
        "phase_a_game_count": PHASE_A_GAME_COUNT,
        "phase_a_role": "DIAGNOSTIC REUSE ONLY",
        "phase_a_seeds": list(PHASE_A_SEEDS),
        "phase_b_games_per_arm": PHASE_B_GAMES_PER_ARM,
        "phase_b_role": "DEVELOPMENT SCREEN",
        "phase_b_seeds": list(seeds),
        "phase_b_total_games": PHASE_B_TOTAL_GAMES,
        "protocol_id": PROTOCOL_ID,
        "rotation_count": ROTATION_COUNT,
    }


__all__ = [
    "ANALYSIS_CLASS_NAME",
    "CANDIDATE_CLASS_NAME",
    "CANDIDATE_IDENTITY",
    "CANDIDATE_SOURCE_MODULE",
    "CLASSIFICATION_RULE_ID",
    "COMPARATOR_IDENTITY",
    "COMPARATOR_SEMANTICS",
    "DIAGNOSTIC_COMPLETE_LABEL",
    "EXECUTION_BRANCH",
    "FEASIBILITY_WALL_CLOCK_LIMIT_HOURS",
    "FORMAL_TEST",
    "GAME_MODE",
    "INCONCLUSIVE_LABEL",
    "INFEASIBLE_LABEL",
    "LISJONG_REVISION",
    "MAX_STEPS",
    "NEGATIVE_LABEL",
    "OPPORTUNITY_NOT_OBSERVED_LABEL",
    "PARENT_CLASS_NAME",
    "PARENT_IDENTITY",
    "PHASE_A_GAME_COUNT",
    "PHASE_A_SEEDS",
    "PHASE_B_GAMES_PER_ARM",
    "PHASE_B_SEED_BLOCK_COUNT",
    "PHASE_B_TOTAL_GAMES",
    "PROTOCOL_ID",
    "ROTATION_COUNT",
    "SIGNAL_LABEL",
    "STOP_INVALID_LABEL",
    "TRAJECTORY_FAILURE_LABEL",
    "CandidateBinding",
    "TargetedHonorReleaseProtocolError",
    "candidate_spec",
    "comparator_spec",
    "create_candidate",
    "parent_spec",
    "protocol_document",
    "repository_declared_allocated_seeds",
    "require_exact_candidate_semantics",
    "require_exact_comparator",
    "require_phase_a_population",
    "require_phase_b_population",
    "seed_freshness_block",
]
