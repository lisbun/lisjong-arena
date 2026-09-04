"""Arena #148 population-mix armのexact identity。

`stage3_entry_gate.population.PopulationPlan`はordered seeds `180..191`へ、
`stage3_kan_coverage.population.KanCoveragePopulationPlan`はordered seeds
`306..329`へlockされたhistorical protocol invariantを持つ。どちらのlockも
緩めず、successor-specific planをここへ独立に持つ。`SeatPolicyReference` /
`GameSeatAssignment`というidentity valueだけを再利用する。

```text
arm   augmentation slots   games with a coverage seat   coverage seat per seat
A      0 / 96 =  0.0%       0 / 24                       0
B     12 / 96 = 12.5%      12 / 24                       3
C     24 / 96 = 25.0%      24 / 24                       6
```

seat assignmentはseed index `i = seed - 330`からdeterministicに導出し、PRNGを
使わない。coverage actor seatはE/S/W/Nへexact balancedであり、balanceは
protocol invariantとしてfail closedで検証する。

Policy instanceはgame・seatごとにfactoryから新規生成し、seat間・game間で
共有しない。augmentation sourceはArena `POLICY_CATALOG`へ登録せず、既存
explicit import referenceだけで解決する。

## 作らないもの

generic population DSL、generic Policy configuration framework、plugin
framework、registry、generic experiment frameworkは作らない。
"""

import hashlib
from collections.abc import Callable
from dataclasses import dataclass

from lisjong_engine.seat import Seat

from lisjong_arena.phase4_raw_corpus.codec import canonical_json_bytes
from lisjong_arena.phase5_belief_dataset.split import (
    KAN_COVERAGE_DEVELOPMENT_SEEDS,
    QUANTITATIVE_SEEDS,
    STAGE3_DEVELOPMENT_SEEDS,
)
from lisjong_arena.phase9_confirmatory.protocol import HOLDOUT_SEEDS
from lisjong_arena.stage3_entry_gate.population import (
    GameSeatAssignment,
    SeatPolicyReference,
)
from lisjong_arena.stage3_mix_pilot.protocol import (
    ARM_IDS,
    AUGMENTATION_IDENTITY,
    AUGMENTATION_REFERENCE,
    AUGMENTATION_SLOTS_BY_ARM,
    AUGMENTED_GAMES_BY_ARM,
    AUGMENTED_SEAT_ASSIGNMENT_ID,
    CONTROL_ARM_ID,
    CONTROL_SEAT_ASSIGNMENT_ID,
    GENERATION_SEMANTICS_ID,
    ORDERED_SEEDS,
    PILOT_ROLE,
    PLAN_SCHEMA_VERSION,
    PRIMARY_IDENTITY,
    PRIMARY_REFERENCE,
    SEAT_COUNT,
    SEAT_SLOTS_PER_ARM,
    SPLIT_POLICY,
    TRAIN_SEEDS,
    VALIDATION_SEEDS,
)

_HISTORICAL_SEEDS = frozenset(
    QUANTITATIVE_SEEDS
    + HOLDOUT_SEEDS
    + STAGE3_DEVELOPMENT_SEEDS
    + KAN_COVERAGE_DEVELOPMENT_SEEDS
)
"""本pilotが再利用してはならないhistorical population seeds。

Stage 1/2 formal split (`100..159`)、Phase 9 confirmatory holdout (`160..179`)、
#131 historical development population (`180..191`)、#146 coverage-source
qualification population (`306..329`) をsuccessor pilotへ混入させないための
fail closed guardである。
"""

_SEAT_ORDER = tuple(Seat)


class MixPopulationError(ValueError):
    """population-mix armのcontract violation。"""


def coverage_seat_index(arm_id: str, seed_index: int) -> int | None:
    """arm / seed indexからcoverage seatのcanonical index、無ければ`None`。

    PRNGを使わないdeterministicなversioned construction ruleである。

    ```text
    A   coverage なし
    B   i % 2 == 0 のとき (i // 2) % 4
    C   常に i % 4
    ```
    """
    if arm_id not in ARM_IDS:
        raise MixPopulationError(f"unknown mix pilot arm id {arm_id!r}")
    if type(seed_index) is not int or not 0 <= seed_index < len(ORDERED_SEEDS):
        raise MixPopulationError("seed_index must index the locked ordered seeds")
    if arm_id == CONTROL_ARM_ID:
        return None
    if arm_id == "B":
        if seed_index % 2 != 0:
            return None
        return (seed_index // 2) % SEAT_COUNT
    return seed_index % SEAT_COUNT


@dataclass(frozen=True, slots=True)
class MixArmPlan:
    """Arena #148で実行する1 armのexact identity。

    seeds、role、armごとのaugmentation slot数はprotocol invariantであり、
    caller optionにしない。
    """

    arm_id: str
    seat_assignment_semantics_id: str
    policies: tuple[SeatPolicyReference, ...]
    assignments: tuple[GameSeatAssignment, ...]

    def __post_init__(self) -> None:
        if self.arm_id not in ARM_IDS:
            raise MixPopulationError(f"unknown mix pilot arm id {self.arm_id!r}")
        expected_semantics = (
            CONTROL_SEAT_ASSIGNMENT_ID
            if self.arm_id == CONTROL_ARM_ID
            else AUGMENTED_SEAT_ASSIGNMENT_ID
        )
        if self.seat_assignment_semantics_id != expected_semantics:
            raise MixPopulationError(
                f"arm {self.arm_id} must use {expected_semantics!r} seat assignment "
                "semantics"
            )
        policies = tuple(self.policies)
        if any(not isinstance(value, SeatPolicyReference) for value in policies):
            raise MixPopulationError("policies must contain SeatPolicyReference values")
        identities = tuple(value.identity for value in policies)
        if len(set(identities)) != len(identities):
            raise MixPopulationError("policy identities must be unique")
        if policies and policies[0].identity != PRIMARY_IDENTITY:
            raise MixPopulationError("the primary source must be declared first")
        expected_identities = (
            (PRIMARY_IDENTITY,)
            if self.arm_id == CONTROL_ARM_ID
            else (PRIMARY_IDENTITY, AUGMENTATION_IDENTITY)
        )
        if identities != expected_identities:
            raise MixPopulationError(
                f"arm {self.arm_id} declares the wrong source identities"
            )
        for value in policies:
            expected_reference = (
                PRIMARY_REFERENCE
                if value.identity == PRIMARY_IDENTITY
                else AUGMENTATION_REFERENCE
            )
            if value.reference != expected_reference:
                raise MixPopulationError(
                    f"source {value.identity!r} must use its locked reference"
                )
        assignments = tuple(self.assignments)
        if any(not isinstance(value, GameSeatAssignment) for value in assignments):
            raise MixPopulationError(
                "assignments must contain GameSeatAssignment values"
            )
        if tuple(value.game_seed for value in assignments) != ORDERED_SEEDS:
            raise MixPopulationError(
                "mix pilot arms are locked to ordered seeds 330..353"
            )
        declared = set(identities)
        if any(
            identity not in declared
            for value in assignments
            for identity in value.seat_identities
        ):
            raise MixPopulationError(
                "every seated identity must be declared by the plan"
            )
        if declared - {
            identity for value in assignments for identity in value.seat_identities
        }:
            raise MixPopulationError("every declared policy must be seated")
        object.__setattr__(self, "policies", policies)
        object.__setattr__(self, "assignments", assignments)
        self._validate_augmentation()

    def _validate_augmentation(self) -> None:
        slots = self.augmentation_slot_count
        if slots != AUGMENTATION_SLOTS_BY_ARM[self.arm_id]:
            raise MixPopulationError(
                f"arm {self.arm_id} must seat exactly "
                f"{AUGMENTATION_SLOTS_BY_ARM[self.arm_id]} coverage-source slots"
            )
        games = sum(
            1
            for value in self.assignments
            if AUGMENTATION_IDENTITY in value.seat_identities
        )
        if games != AUGMENTED_GAMES_BY_ARM[self.arm_id]:
            raise MixPopulationError(
                f"arm {self.arm_id} must place a coverage seat in exactly "
                f"{AUGMENTED_GAMES_BY_ARM[self.arm_id]} hanchan"
            )
        if any(
            sum(
                1
                for identity in value.seat_identities
                if identity == AUGMENTATION_IDENTITY
            )
            > 1
            for value in self.assignments
        ):
            raise MixPopulationError(
                "a mix pilot hanchan carries at most one coverage-source seat"
            )
        if not self.is_coverage_seat_balanced:
            raise MixPopulationError(
                "the coverage-source seat must be balanced across every canonical Seat"
            )

    @property
    def population_id(self) -> str:
        return self.arm_id

    @property
    def ordered_seeds(self) -> tuple[int, ...]:
        return ORDERED_SEEDS

    @property
    def train_seeds(self) -> tuple[int, ...]:
        return TRAIN_SEEDS

    @property
    def validation_seeds(self) -> tuple[int, ...]:
        return VALIDATION_SEEDS

    @property
    def augmentation_slot_count(self) -> int:
        return sum(
            1
            for value in self.assignments
            for identity in value.seat_identities
            if identity == AUGMENTATION_IDENTITY
        )

    @property
    def augmentation_seat_slot_fraction(self) -> float:
        return self.augmentation_slot_count / SEAT_SLOTS_PER_ARM

    def coverage_seat_occupancy(self) -> tuple[int, ...]:
        """canonical Seat順の、coverage sourceが担当した回数。"""
        counts = [0] * SEAT_COUNT
        for value in self.assignments:
            for index, identity in enumerate(value.seat_identities):
                if identity == AUGMENTATION_IDENTITY:
                    counts[index] += 1
        return tuple(counts)

    @property
    def is_coverage_seat_balanced(self) -> bool:
        """coverage sourceが全seatを同回数担当しているか。

        control armのように1件も配置しない場合もbalancedとして扱う。
        """
        counts = self.coverage_seat_occupancy()
        return len(set(counts)) == 1

    def coverage_slots(self) -> frozenset[tuple[int, Seat]]:
        """coverage sourceが座った`(game_seed, Seat)`の集合。

        Policy decisionのsource attributionに使う。
        """
        return frozenset(
            (value.game_seed, _SEAT_ORDER[index])
            for value in self.assignments
            for index, identity in enumerate(value.seat_identities)
            if identity == AUGMENTATION_IDENTITY
        )

    def seat_policy_factories_by_seed(
        self,
    ) -> dict[int, dict[Seat, Callable[[], object]]]:
        """Phase 4 generationへ渡すseed別 / seat別factory mapping。

        factoryはcallableのままで、Policy instanceはgame・seatごとにPhase 4側で
        新規生成される。instanceをここで共有しない。
        """
        factories = {value.identity: value.factory() for value in self.policies}
        return {
            assignment.game_seed: {
                seat: factories[assignment.seat_identities[index]]
                for index, seat in enumerate(_SEAT_ORDER)
            }
            for assignment in self.assignments
        }

    def plan_value(self) -> dict[str, object]:
        return {
            "plan_schema_version": PLAN_SCHEMA_VERSION,
            "pilot_role": PILOT_ROLE,
            "arm_id": self.arm_id,
            "population_id": self.arm_id,
            "seat_assignment_semantics_id": self.seat_assignment_semantics_id,
            "generation_semantics_id": GENERATION_SEMANTICS_ID,
            "split_policy_id": SPLIT_POLICY.value,
            "primary_source": {
                "identity": PRIMARY_IDENTITY,
                "reference": PRIMARY_REFERENCE,
            },
            "augmentation_source": {
                "identity": AUGMENTATION_IDENTITY,
                "reference": AUGMENTATION_REFERENCE,
            },
            "policies": [
                {"identity": value.identity, "reference": value.reference}
                for value in self.policies
            ],
            "ordered_seeds": list(ORDERED_SEEDS),
            "train_seeds": list(TRAIN_SEEDS),
            "validation_seeds": list(VALIDATION_SEEDS),
            "seat_order": [seat.value for seat in _SEAT_ORDER],
            "seat_assignments": [
                {
                    "game_seed": value.game_seed,
                    "seat_identities": list(value.seat_identities),
                }
                for value in self.assignments
            ],
            "seat_slots": SEAT_SLOTS_PER_ARM,
            "augmentation_seat_slots": self.augmentation_slot_count,
            "augmentation_seat_slot_fraction": self.augmentation_seat_slot_fraction,
            "augmented_hanchan": AUGMENTED_GAMES_BY_ARM[self.arm_id],
            "coverage_seat_occupancy": list(self.coverage_seat_occupancy()),
            "coverage_seat_balanced": self.is_coverage_seat_balanced,
            "test_partition_present": False,
        }

    @property
    def population_identity(self) -> str:
        """seat assignmentまで含むsuccessor population identity。"""
        return hashlib.sha256(canonical_json_bytes(self.plan_value())).hexdigest()


def _primary_reference() -> SeatPolicyReference:
    return SeatPolicyReference(identity=PRIMARY_IDENTITY, reference=PRIMARY_REFERENCE)


def _augmentation_reference() -> SeatPolicyReference:
    return SeatPolicyReference(
        identity=AUGMENTATION_IDENTITY, reference=AUGMENTATION_REFERENCE
    )


def mix_arm_plan(arm_id: str) -> MixArmPlan:
    """Arena #148のlocked arm plan。

    historical population seedsとの重なりはprotocol violationとしてfail closed
    する。historical seedsを再利用しないことはこのpilotのidentityの一部である。
    """
    if arm_id not in ARM_IDS:
        raise MixPopulationError(f"unknown mix pilot arm id {arm_id!r}")
    overlap = _HISTORICAL_SEEDS.intersection(ORDERED_SEEDS)
    if overlap:
        raise MixPopulationError(
            "the successor mix pilot must not reuse historical population seeds: "
            f"{sorted(overlap)}"
        )
    assignments = []
    for index, seed in enumerate(ORDERED_SEEDS):
        coverage_index = coverage_seat_index(arm_id, index)
        identities = tuple(
            AUGMENTATION_IDENTITY if seat_index == coverage_index else PRIMARY_IDENTITY
            for seat_index in range(SEAT_COUNT)
        )
        assignments.append(GameSeatAssignment(seed, identities))
    policies = (
        (_primary_reference(),)
        if arm_id == CONTROL_ARM_ID
        else (_primary_reference(), _augmentation_reference())
    )
    return MixArmPlan(
        arm_id=arm_id,
        seat_assignment_semantics_id=(
            CONTROL_SEAT_ASSIGNMENT_ID
            if arm_id == CONTROL_ARM_ID
            else AUGMENTED_SEAT_ASSIGNMENT_ID
        ),
        policies=policies,
        assignments=tuple(assignments),
    )


def mix_arm_plans() -> tuple[MixArmPlan, ...]:
    """Arena #148のlocked 3 arm。"""
    return tuple(mix_arm_plan(arm_id) for arm_id in ARM_IDS)


__all__ = [
    "MixArmPlan",
    "MixPopulationError",
    "coverage_seat_index",
    "mix_arm_plan",
    "mix_arm_plans",
]
