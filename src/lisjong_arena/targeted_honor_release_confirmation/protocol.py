"""Issue #270 independent targeted honor-release confirmation protocol."""

from __future__ import annotations

from lisjong_arena.model import SINGLE_ROUND_GAME_MODE, SINGLE_ROUND_ROTATION_COUNT
from lisjong_arena.targeted_honor_release_development.protocol import (
    CANDIDATE_CLASS_NAME,
    CANDIDATE_IDENTITY,
    CANDIDATE_SOURCE_MODULE,
    COMPARATOR_IDENTITY,
    COMPARATOR_SEMANTICS,
    LISJONG_REVISION,
    MAX_STEPS,
    PARENT_CLASS_NAME,
    PARENT_IDENTITY,
    candidate_spec,
    comparator_spec,
    parent_spec,
    require_exact_candidate_semantics,
    require_exact_comparator,
)
PROTOCOL_ID = "targeted-honor-release-confirmation-v1"
CLASSIFICATION_RULE_ID = "paired-seed-block-normal-approx-95-confirmation-v1"
ROLE = "INDEPENDENT CONFIRMATION"
FORMAL_TEST = False
GAME_MODE = SINGLE_ROUND_GAME_MODE
ROTATION_COUNT = SINGLE_ROUND_ROTATION_COUNT
SEED_BLOCK_COUNT = 2_200
GAMES_PER_ARM = SEED_BLOCK_COUNT * ROTATION_COUNT
TOTAL_GAMES = 2 * GAMES_PER_ARM
EXECUTION_BRANCH = "main"

CONFIRMED_POSITIVE_LABEL = "TARGETED HONOR-RELEASE CONFIRMED POSITIVE"
CONFIRMED_NEGATIVE_LABEL = "TARGETED HONOR-RELEASE CONFIRMED NEGATIVE"
INCONCLUSIVE_LABEL = "TARGETED HONOR-RELEASE CONFIRMATION INCONCLUSIVE"
STOP_INVALID_LABEL = "STOP / INVALID"

# Historical #263 development population. It is explicitly excluded from #270.
ISSUE_263_DEVELOPMENT_SEEDS = tuple(range(751, 851))


class TargetedHonorReleaseConfirmationProtocolError(ValueError):
    """Issue #270 locked confirmation protocol cannot be satisfied."""


def require_confirmation_population(seeds: object) -> tuple[int, ...]:
    if isinstance(seeds, (str, bytes, bytearray)):
        raise TypeError("confirmation seeds must be an ordered collection of ints")
    try:
        ordered = tuple(seeds)  # type: ignore[arg-type]
    except TypeError:
        raise TypeError(
            "confirmation seeds must be an ordered collection of ints"
        ) from None
    if any(type(seed) is not int for seed in ordered):
        raise TypeError("confirmation seeds must contain only exact ints")
    if len(ordered) != SEED_BLOCK_COUNT:
        raise TargetedHonorReleaseConfirmationProtocolError(
            f"confirmation must contain exactly {SEED_BLOCK_COUNT} seed blocks"
        )
    if not ordered:
        raise TargetedHonorReleaseConfirmationProtocolError(
            "confirmation seed population must not be empty"
        )
    expected = tuple(range(ordered[0], ordered[0] + SEED_BLOCK_COUNT))
    if ordered != expected:
        raise TargetedHonorReleaseConfirmationProtocolError(
            "confirmation seeds must be one contiguous increasing range"
        )
    return ordered


def repository_declared_allocated_seeds() -> frozenset[int]:
    """Return Arena allocations other than #270's own population."""

    from lisjong_arena.seed_registry import allocated_seeds

    return allocated_seeds(
        exclude_owner_issues={"lisbun/lisjong-arena#270"}
    )


def seed_freshness_block(
    ordered_seeds: object,
    *,
    external_freshness_confirmed: bool,
    additional_allocated_seeds: object = (),
) -> dict[str, object]:
    seeds = require_confirmation_population(ordered_seeds)
    if type(external_freshness_confirmed) is not bool:
        raise TypeError("external_freshness_confirmed must be an exact bool")
    if not external_freshness_confirmed:
        raise TargetedHonorReleaseConfirmationProtocolError(
            "relevant open/closed Issues and known local/private allocations "
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
        raise TargetedHonorReleaseConfirmationProtocolError(
            "SEED PLAN REFORMULATE: confirmation seed collision found before "
            "result exposure "
            f"(repository={repository_collisions!r}, "
            f"external={external_collisions!r})"
        )
    return {
        "additional_allocated_seeds": sorted(additional),
        "external_freshness_confirmed": True,
        "external_audit_scope": (
            "relevant open/closed Issues + local/private allocations"
        ),
        "external_collisions": [],
        "fresh": True,
        "ordered_seeds": list(seeds),
        "repository_collisions": [],
    }


def protocol_document(seeds: object) -> dict[str, object]:
    ordered = require_confirmation_population(seeds)
    return {
        "classification_rule_id": CLASSIFICATION_RULE_ID,
        "estimand": "mean paired focal-score delta H_s - C_s over locked seed blocks",
        "formal_test": FORMAL_TEST,
        "game_mode": GAME_MODE,
        "games_per_arm": GAMES_PER_ARM,
        "interval_method": "two-sided normal-approximation 95% interval",
        "max_steps": MAX_STEPS,
        "ordered_seeds": list(ordered),
        "primary_unit": "paired seed block",
        "protocol_id": PROTOCOL_ID,
        "role": ROLE,
        "rotation_count": ROTATION_COUNT,
        "seed_block_count": SEED_BLOCK_COUNT,
        "total_games": TOTAL_GAMES,
    }


__all__ = [
    "CANDIDATE_CLASS_NAME",
    "CANDIDATE_IDENTITY",
    "CANDIDATE_SOURCE_MODULE",
    "CLASSIFICATION_RULE_ID",
    "COMPARATOR_IDENTITY",
    "COMPARATOR_SEMANTICS",
    "CONFIRMED_NEGATIVE_LABEL",
    "CONFIRMED_POSITIVE_LABEL",
    "EXECUTION_BRANCH",
    "FORMAL_TEST",
    "GAME_MODE",
    "GAMES_PER_ARM",
    "INCONCLUSIVE_LABEL",
    "ISSUE_263_DEVELOPMENT_SEEDS",
    "LISJONG_REVISION",
    "MAX_STEPS",
    "PARENT_CLASS_NAME",
    "PARENT_IDENTITY",
    "PROTOCOL_ID",
    "ROLE",
    "ROTATION_COUNT",
    "SEED_BLOCK_COUNT",
    "STOP_INVALID_LABEL",
    "TOTAL_GAMES",
    "TargetedHonorReleaseConfirmationProtocolError",
    "candidate_spec",
    "comparator_spec",
    "parent_spec",
    "protocol_document",
    "repository_declared_allocated_seeds",
    "require_confirmation_population",
    "require_exact_candidate_semantics",
    "require_exact_comparator",
    "seed_freshness_block",
]
