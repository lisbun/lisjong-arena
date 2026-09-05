"""Phase 10のseed非依存recipeと、seed-boundなlocked population realization。

`recipe_value()`は#148がlockした **population recipe** そのものであり、seedを
含まない。`plan_value()`はそのrecipeを`360..439`へ適用したPhase 10固有の
realizationであり、seedを含む。

```text
recipe        carry-forward可能 / seed-free
plan          Phase 10固有 / seed-bound
```

carry-forward側へdevelopment seedやseed-bound split policy idを漏らさないことが
information-flow boundaryであり、`assert_recipe_is_seed_free()`がそれをfail
closedで固定する。

seat assignmentはPRNGを使わず、seed indexからdeterministicに導出する。

```text
i = seed - 360
i % 2 == 0   coverage seat index = (i // 2) % 4
i % 2 == 1   coverage source なし
```

`360..439`では80 hanchan / 320 seat slots中40 slotsがcoverage sourceであり、
augmentation fractionはexactに12.5%、coverage seatはE/S/W/Nへ`[10,10,10,10]`で
balanceする。nested subsetもそれぞれexact balanceを保つ。
"""

from lisjong_engine.seat import Seat

from lisjong_arena.stage3_entry_gate.population import SeatPolicyReference
from lisjong_arena.stage3_mix_pilot.protocol import (
    AUGMENTATION_IDENTITY,
    AUGMENTATION_REFERENCE,
    AUGMENTED_SEAT_ASSIGNMENT_ID,
    GENERATION_SEMANTICS_ID,
    PRIMARY_IDENTITY,
    PRIMARY_REFERENCE,
)

from .protocol import (
    AUGMENTATION_FRACTION,
    COVERAGE_SLOTS,
    ORDERED_SEEDS,
    ROLE,
    SCALES,
    SCHEMA,
    SEAT_SLOTS,
    SPLIT_POLICY,
    TRAIN_SEEDS,
    VALIDATION_SEEDS,
    ScaleError,
    check_freshness,
    identity,
    train_seeds,
)


def recipe_value() -> dict[str, object]:
    """#148でlockされたseed-freeなfirst-party population recipe。

    Phase 10はこのrecipeを再選択も再解釈もせず、そのまま適用する。
    """
    return {
        "primary": {"identity": PRIMARY_IDENTITY, "reference": PRIMARY_REFERENCE},
        "augmentation": {
            "identity": AUGMENTATION_IDENTITY,
            "reference": AUGMENTATION_REFERENCE,
        },
        "augmentation_fraction": AUGMENTATION_FRACTION,
        "seat_assignment_semantics": AUGMENTED_SEAT_ASSIGNMENT_ID,
        "generation_semantics": GENERATION_SEMANTICS_ID,
        "split_semantics": "whole hanchan / TRAIN + VALIDATION / no formal TEST",
    }


def assert_recipe_is_seed_free(recipe: dict[str, object]) -> dict[str, object]:
    """carry-forward recipeがdevelopment seedもseed-bound split idも含まないこと。

    recipeはPhase 10より先へ持ち出せるvalueである。ここへ`360..439`や
    `first-party-seeds-360-439-...`が混ざると、後続のpopulationがdevelopment
    seedを暗黙に引き継いでしまう。canonical bytes上で実際に探索して拒否する。
    """
    from lisjong_arena.phase4_raw_corpus.codec import canonical_json_bytes

    encoded = canonical_json_bytes(recipe).decode("utf-8")
    if SPLIT_POLICY.value in encoded:
        raise ScaleError("carry-forward recipe leaks the seed-bound split policy id")
    for seed in ORDERED_SEEDS:
        if str(seed) in encoded:
            raise ScaleError(f"carry-forward recipe leaks the development seed {seed}")
    return recipe


def coverage_seat_index(seed: int) -> int | None:
    """1 hanchanのcoverage-source seat index。PRNGを使わない。"""
    if type(seed) is not int or seed not in ORDERED_SEEDS:
        raise ScaleError("seed is outside the locked Phase 10 population")
    index = seed - ORDERED_SEEDS[0]
    return (index // 2) % 4 if index % 2 == 0 else None


def assignments() -> list[dict[str, object]]:
    return [
        {
            "game_seed": seed,
            "seat_identities": [
                AUGMENTATION_IDENTITY
                if index == coverage_seat_index(seed)
                else PRIMARY_IDENTITY
                for index in range(len(tuple(Seat)))
            ],
        }
        for seed in ORDERED_SEEDS
    ]


def occupancy(seeds: tuple[int, ...]) -> list[int]:
    """seat indexごとのcoverage-source occupancy。"""
    return [
        sum(coverage_seat_index(seed) == index for seed in seeds) for index in range(4)
    ]


def plan_value() -> dict[str, object]:
    """locked Phase 10 population plan。

    plan構成時にfreshness preflightを再実行する。`360..439`へ新しいcollisionが
    入った場合はplanを黙って作らずfail closedする。
    """
    outcome, overlap = check_freshness(ORDERED_SEEDS)
    if outcome is not None:
        raise ScaleError(f"{outcome}: {overlap}")
    return {
        "schema": SCHEMA + "/population-plan",
        "role": ROLE,
        "recipe": assert_recipe_is_seed_free(recipe_value()),
        "ordered_seeds": list(ORDERED_SEEDS),
        "train_seeds": list(TRAIN_SEEDS),
        "validation_seeds": list(VALIDATION_SEEDS),
        "subsets": {scale: list(train_seeds(scale)) for scale in SCALES},
        "split_policy_id": SPLIT_POLICY.value,
        "test_partition_present": False,
        "seat_order": [seat.value for seat in Seat],
        "assignments": assignments(),
        "seat_slots": SEAT_SLOTS,
        "coverage_slots": COVERAGE_SLOTS,
        "coverage_occupancy": occupancy(ORDERED_SEEDS),
    }


def population_identity() -> str:
    return identity(plan_value())


def seat_policy_factories_by_seed() -> dict[int, dict[object, object]]:
    """seed / seatごとのPolicy factory。

    factoryはPhase 4 generationがgame・seatごとにfresh instanceを作るための
    callableであり、Policy instanceそのものをseat間やgame間で共有しない。
    """
    recipe = recipe_value()
    factories = {
        row["identity"]: SeatPolicyReference(**row).factory()
        for row in (recipe["primary"], recipe["augmentation"])
    }
    return {
        row["game_seed"]: {
            seat: factories[row["seat_identities"][index]]
            for index, seat in enumerate(Seat)
        }
        for row in assignments()
    }


def subset_binding(
    scale: str,
    *,
    raw_corpus_identity: str,
    dataset_identity: str,
    provenance: dict[str, object],
) -> dict[str, object]:
    """1 scaleのTRAIN subsetを、population / corpus / dataset / sourceへbindする。

    modelがどのexact TRAIN subsetから来たかをartifact自身が証明できるようにする。
    """
    return {
        "schema": SCHEMA + "/train-subset",
        "scale": scale,
        "population_identity": population_identity(),
        "raw_corpus_identity": raw_corpus_identity,
        "dataset_identity": dataset_identity,
        "provenance": provenance,
        "train_seeds": list(train_seeds(scale)),
        "validation_seeds": list(VALIDATION_SEEDS),
        "test_partition_present": False,
    }


__all__ = [
    "assert_recipe_is_seed_free",
    "assignments",
    "coverage_seat_index",
    "occupancy",
    "plan_value",
    "population_identity",
    "recipe_value",
    "seat_policy_factories_by_seed",
    "subset_binding",
]
