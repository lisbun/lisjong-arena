"""Issue #297 locked Champion + HandValue v2 direct-ABBB protocol."""

from __future__ import annotations

from lisjong.policies.hand_value_tradeoff_targeted_honor_release import (
    HandValueTradeoffTargetedHonorReleasePolicy,
)
from lisjong.policies.targeted_honor_release_terminal_progression import (
    TargetedHonorReleaseTerminalProgressionPolicy,
)

from lisjong_arena.model import (
    SINGLE_ROUND_GAME_MODE,
    SINGLE_ROUND_ROTATION_COUNT,
    PolicySpec,
    SingleRoundEvaluationPlan,
)
from lisjong_arena.targeted_honor_release_confirmation.protocol import (
    repository_declared_allocated_seeds as prior_declared_allocated_seeds,
)

PROTOCOL_ID = "champion-hand-value-v2-bounded-screen-v1"
ROLE = "BOUNDED DEVELOPMENT SCREEN"
FORMAL_TEST = False
GAME_MODE = SINGLE_ROUND_GAME_MODE
ROTATION_COUNT = SINGLE_ROUND_ROTATION_COUNT
SEED_BLOCK_COUNT = 100
TOTAL_GAMES = SEED_BLOCK_COUNT * ROTATION_COUNT
MAX_STEPS = 10_000

LISJONG_REVISION = "15799e5f0fe47f2e2b2c39060de804d99c51492d"
LISJONG_ENGINE_REVISION = "8735e89e1aea000ab59368d0368d476787827741"
RIICHIENV_VERSION = "0.4.10"

CANDIDATE_IDENTITY = "champion-plus-hand-value-v2"
CANDIDATE_CLASS_NAME = "HandValueTradeoffTargetedHonorReleasePolicy"
CANDIDATE_SOURCE_MODULE = "lisjong.policies.hand_value_tradeoff_targeted_honor_release"

BASELINE_IDENTITY = "targeted-honor-release-terminal-progression"
BASELINE_CLASS_NAME = "TargetedHonorReleaseTerminalProgressionPolicy"
BASELINE_SOURCE_MODULE = "lisjong.policies.targeted_honor_release_terminal_progression"

CLASSIFICATION_RULE_ID = "seed-block-normal-approx-95-direct-abbb-v1"
POSITIVE_LABEL = "CHAMPION + HAND VALUE V2 POSITIVE SIGNAL"
NEGATIVE_LABEL = "CHAMPION + HAND VALUE V2 NEGATIVE SIGNAL"
INCONCLUSIVE_LABEL = "CHAMPION + HAND VALUE V2 INCONCLUSIVE"
STOP_INVALID_LABEL = "STOP / INVALID"

# #270 consumed these seeds under a write-once confirmation lock.  They are
# not repository-declared in its protocol module because the range was chosen
# post-merge, so #297 records the allocation explicitly.
ISSUE_270_CONFIRMATION_SEEDS = tuple(range(50_000, 52_200))


class ChampionHandValueV2ProtocolError(ValueError):
    """The fixed Issue #297 scientific protocol cannot be satisfied."""


def candidate_factory() -> HandValueTradeoffTargetedHonorReleasePolicy:
    return HandValueTradeoffTargetedHonorReleasePolicy()


def baseline_factory() -> TargetedHonorReleaseTerminalProgressionPolicy:
    return TargetedHonorReleaseTerminalProgressionPolicy()


def candidate_spec() -> PolicySpec:
    return PolicySpec(identity=CANDIDATE_IDENTITY, factory=candidate_factory)


def baseline_spec() -> PolicySpec:
    return PolicySpec(identity=BASELINE_IDENTITY, factory=baseline_factory)


def require_exact_policy_semantics() -> None:
    candidate = candidate_factory()
    baseline = baseline_factory()
    if type(candidate).__name__ != CANDIDATE_CLASS_NAME:
        raise ChampionHandValueV2ProtocolError("candidate class identity drifted")
    if type(candidate).__module__ != CANDIDATE_SOURCE_MODULE:
        raise ChampionHandValueV2ProtocolError("candidate source module drifted")
    if type(baseline).__name__ != BASELINE_CLASS_NAME:
        raise ChampionHandValueV2ProtocolError("baseline class identity drifted")
    if type(baseline).__module__ != BASELINE_SOURCE_MODULE:
        raise ChampionHandValueV2ProtocolError("baseline source module drifted")


def require_population(seeds: object) -> tuple[int, ...]:
    if isinstance(seeds, (str, bytes, bytearray)):
        raise TypeError("seeds must be an ordered collection of ints")
    try:
        ordered = tuple(seeds)  # type: ignore[arg-type]
    except TypeError:
        raise TypeError("seeds must be an ordered collection of ints") from None
    if any(type(seed) is not int for seed in ordered):
        raise TypeError("seeds must contain only exact ints")
    if len(ordered) != SEED_BLOCK_COUNT:
        raise ChampionHandValueV2ProtocolError(
            f"screen must contain exactly {SEED_BLOCK_COUNT} seed blocks"
        )
    expected = tuple(range(ordered[0], ordered[0] + SEED_BLOCK_COUNT))
    if ordered != expected:
        raise ChampionHandValueV2ProtocolError(
            "screen seeds must be one contiguous increasing range"
        )
    return ordered


def repository_declared_allocated_seeds() -> frozenset[int]:
    return frozenset(prior_declared_allocated_seeds()) | frozenset(
        ISSUE_270_CONFIRMATION_SEEDS
    )


def seed_freshness_block(
    seeds: object,
    *,
    external_freshness_confirmed: bool,
    additional_allocated_seeds: object = (),
) -> dict[str, object]:
    ordered = require_population(seeds)
    if type(external_freshness_confirmed) is not bool:
        raise TypeError("external_freshness_confirmed must be an exact bool")
    if not external_freshness_confirmed:
        raise ChampionHandValueV2ProtocolError(
            "relevant open/closed Issues and local/private allocations must be "
            "reviewed immediately before the pre-execution lock"
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
        repository_declared_allocated_seeds().intersection(ordered)
    )
    external_collisions = sorted(additional.intersection(ordered))
    if repository_collisions or external_collisions:
        raise ChampionHandValueV2ProtocolError(
            "SEED PLAN REFORMULATE: collision found before result exposure "
            f"(repository={repository_collisions!r}, "
            f"external={external_collisions!r})"
        )
    return {
        "additional_allocated_seeds": sorted(additional),
        "external_freshness_confirmed": True,
        "external_audit_scope": (
            "relevant open/closed Issues + repository allocations + "
            "operator local/private allocations"
        ),
        "fresh": True,
        "ordered_seeds": list(ordered),
        "repository_collisions": [],
        "external_collisions": [],
    }


def build_plan(seeds: object) -> SingleRoundEvaluationPlan:
    ordered = require_population(seeds)
    return SingleRoundEvaluationPlan(
        candidate=candidate_spec(),
        baseline=baseline_spec(),
        seeds=ordered,
        max_steps=MAX_STEPS,
    )


def protocol_document(seeds: object) -> dict[str, object]:
    ordered = require_population(seeds)
    return {
        "protocol_id": PROTOCOL_ID,
        "role": ROLE,
        "formal_test": FORMAL_TEST,
        "game_mode": GAME_MODE,
        "seed_block_count": SEED_BLOCK_COUNT,
        "rotation_count": ROTATION_COUNT,
        "total_games": TOTAL_GAMES,
        "ordered_seeds": list(ordered),
        "primary_unit": "ordered seed block",
        "estimand": (
            "mean candidate score minus mean(other three baseline scores), "
            "aggregated over four rotations per seed"
        ),
        "interval_method": "two-sided normal-approximation 95% interval",
        "classification_rule_id": CLASSIFICATION_RULE_ID,
        "candidate": {
            "identity": CANDIDATE_IDENTITY,
            "class": CANDIDATE_CLASS_NAME,
            "module": CANDIDATE_SOURCE_MODULE,
            "lisjong_revision": LISJONG_REVISION,
        },
        "baseline": {
            "identity": BASELINE_IDENTITY,
            "class": BASELINE_CLASS_NAME,
            "module": BASELINE_SOURCE_MODULE,
            "lisjong_revision": LISJONG_REVISION,
        },
    }


__all__ = [
    "BASELINE_CLASS_NAME",
    "BASELINE_IDENTITY",
    "BASELINE_SOURCE_MODULE",
    "CANDIDATE_CLASS_NAME",
    "CANDIDATE_IDENTITY",
    "CANDIDATE_SOURCE_MODULE",
    "CLASSIFICATION_RULE_ID",
    "ChampionHandValueV2ProtocolError",
    "FORMAL_TEST",
    "GAME_MODE",
    "INCONCLUSIVE_LABEL",
    "ISSUE_270_CONFIRMATION_SEEDS",
    "LISJONG_ENGINE_REVISION",
    "LISJONG_REVISION",
    "MAX_STEPS",
    "NEGATIVE_LABEL",
    "POSITIVE_LABEL",
    "PROTOCOL_ID",
    "RIICHIENV_VERSION",
    "ROLE",
    "ROTATION_COUNT",
    "SEED_BLOCK_COUNT",
    "STOP_INVALID_LABEL",
    "TOTAL_GAMES",
    "baseline_factory",
    "baseline_spec",
    "build_plan",
    "candidate_factory",
    "candidate_spec",
    "protocol_document",
    "repository_declared_allocated_seeds",
    "require_exact_policy_semantics",
    "require_population",
    "seed_freshness_block",
]
