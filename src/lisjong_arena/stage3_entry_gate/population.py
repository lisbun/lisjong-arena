"""Stage 3 Entry Gate first-party population plans with exact seat identity.

Arena #131のdevelopment-only pilotは、3つのfirst-party populationを同じordered
seeds / same rulesで実行し、Phase 10へ渡すtraining populationを選定する。本module
はそのpopulationを **exact identity付きのimmutable value** として表現する。

```text
PopulationPlan
  ├─ ordered SeatPolicyReference   (identity + explicit import reference)
  └─ ordered GameSeatAssignment    (seed -> canonical Seat順のidentity)
```

population identityにはseat assignmentまで含める。同じPolicy集合でもseat配置が
異なればtrajectory distributionが変わるため、identityを policy集合だけから導出
しない。

## 作らないもの

generic Policy configuration framework、plugin framework、YAML/TOML registry、
production Policy registry、generic experiment frameworkは作らない。Policy
referenceの解決は既存の`lisjong_arena.policy_reference`だけを使い、catalogへ
未登録Policyを追加もしない。
"""

import hashlib
from collections.abc import Callable
from dataclasses import dataclass

from lisjong_engine.seat import Seat

from lisjong_arena.phase4_raw_corpus.codec import canonical_json_bytes
from lisjong_arena.phase5_belief_dataset.split import (
    STAGE3_DEVELOPMENT_SEEDS,
    STAGE3_TRAIN_SEEDS,
    STAGE3_VALIDATION_SEEDS,
)
from lisjong_arena.policy_reference import resolve_policy_reference

PLAN_SCHEMA_VERSION = "stage3-entry-gate-population-plan-v1"
PILOT_ROLE = "development-only"
PILOT_HANCHAN_PER_POPULATION = len(STAGE3_DEVELOPMENT_SEEDS)

FIXED_SEAT_ASSIGNMENT_ID = "fixed-single-policy-v1"
ROTATING_SEAT_ASSIGNMENT_ID = "base-order-rotated-one-seat-per-seed-v1"

TWO_STEP = "two-step"
GENBUTSU_DEFENSE_TWO_STEP = "genbutsu-defense-two-step"
HAND_VALUE_AWARE = "hand-value-aware"
YAKUHAI_CALL = "yakuhai-call"

GENBUTSU_DEFENSE_TWO_STEP_REFERENCE = (
    "lisjong.policies:GenbutsuDefenseTwoStepUkeirePolicy"
)


class Stage3PopulationError(ValueError):
    """Stage 3 population planのcontract violation。"""


@dataclass(frozen=True, slots=True)
class SeatPolicyReference:
    """1 Policyのexplicit identityとreference。

    identityはclass名から暗黙導出せず、planが明示的に持つ。`reference`は既存
    `resolve_policy_reference()`が解決できるcurated aliasまたは
    `lisjong.<module>:<attribute>`のexplicit referenceである。
    """

    identity: str
    reference: str

    def __post_init__(self) -> None:
        for name in ("identity", "reference"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise Stage3PopulationError(f"{name} must be a non-empty str")

    def factory(self) -> Callable[[], object]:
        """既存Policy reference解決pathからfactory callableを得る。"""
        if ":" in self.reference:
            spec = resolve_policy_reference(
                self.reference, explicit_identity=self.identity
            )
        else:
            spec = resolve_policy_reference(self.reference)
            if spec.identity != self.identity:
                raise Stage3PopulationError(
                    f"catalog alias {self.reference!r} resolves to identity "
                    f"{spec.identity!r}, not {self.identity!r}"
                )
        return spec.factory


@dataclass(frozen=True, slots=True)
class GameSeatAssignment:
    """1 hanchanのseed と canonical `Seat`順のPolicy identity割り当て。"""

    game_seed: int
    seat_identities: tuple[str, str, str, str]

    def __post_init__(self) -> None:
        if type(self.game_seed) is not int:
            raise Stage3PopulationError("game_seed must be an int")
        identities = tuple(self.seat_identities)
        if len(identities) != len(tuple(Seat)):
            raise Stage3PopulationError(
                "seat_identities must cover every canonical Seat exactly once"
            )
        if any(not isinstance(value, str) or not value for value in identities):
            raise Stage3PopulationError("seat_identities must be non-empty strs")
        object.__setattr__(self, "seat_identities", identities)


@dataclass(frozen=True, slots=True)
class PopulationPlan:
    """Stage 3 pilotで実行する1 populationのexact identity。

    seedsは`180..191`にlockされている。development-only roleそのものがこの
    planのprotocol invariantであり、caller optionにしない。
    """

    population_id: str
    seat_assignment_semantics_id: str
    policies: tuple[SeatPolicyReference, ...]
    assignments: tuple[GameSeatAssignment, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.population_id, str) or not self.population_id.strip():
            raise Stage3PopulationError("population_id must be a non-empty str")
        if self.seat_assignment_semantics_id not in (
            FIXED_SEAT_ASSIGNMENT_ID,
            ROTATING_SEAT_ASSIGNMENT_ID,
        ):
            raise Stage3PopulationError("unknown seat assignment semantics identity")
        policies = tuple(self.policies)
        if not policies or any(
            not isinstance(value, SeatPolicyReference) for value in policies
        ):
            raise Stage3PopulationError(
                "policies must contain SeatPolicyReference values"
            )
        identities = tuple(value.identity for value in policies)
        if len(set(identities)) != len(identities):
            raise Stage3PopulationError("policy identities must be unique")
        assignments = tuple(self.assignments)
        if any(not isinstance(value, GameSeatAssignment) for value in assignments):
            raise Stage3PopulationError(
                "assignments must contain GameSeatAssignment values"
            )
        if tuple(value.game_seed for value in assignments) != STAGE3_DEVELOPMENT_SEEDS:
            raise Stage3PopulationError(
                "Stage 3 population plans are locked to ordered seeds 180..191"
            )
        declared = set(identities)
        if any(
            identity not in declared
            for value in assignments
            for identity in value.seat_identities
        ):
            raise Stage3PopulationError(
                "every seated identity must be declared by the plan"
            )
        if declared - {
            identity for value in assignments for identity in value.seat_identities
        }:
            raise Stage3PopulationError("every declared policy must be seated")
        object.__setattr__(self, "policies", policies)
        object.__setattr__(self, "assignments", assignments)

    @property
    def train_seeds(self) -> tuple[int, ...]:
        return STAGE3_TRAIN_SEEDS

    @property
    def validation_seeds(self) -> tuple[int, ...]:
        return STAGE3_VALIDATION_SEEDS

    def seat_occupancy(self) -> dict[str, tuple[int, ...]]:
        """Policy identityごとの、canonical Seat別担当回数。"""
        counts = {value.identity: [0] * len(tuple(Seat)) for value in self.policies}
        for assignment in self.assignments:
            for index, identity in enumerate(assignment.seat_identities):
                counts[identity][index] += 1
        return {identity: tuple(value) for identity, value in counts.items()}

    @property
    def is_seat_balanced(self) -> bool:
        """全Policyが全seatを同回数担当しているか。"""
        occupancy = self.seat_occupancy()
        expected = len(self.assignments) // len(occupancy)
        return all(
            all(count == expected for count in counts) for counts in occupancy.values()
        )

    def seat_policy_factories_by_seed(
        self,
    ) -> dict[int, dict[Seat, Callable[[], object]]]:
        """Phase 4 generationへ渡すseed別 / seat別factory mapping。

        Policy instanceはgame・seatごとにfactoryから新規生成されるため、
        ここではcallableだけを返しinstanceを共有しない。
        """
        factories = {value.identity: value.factory() for value in self.policies}
        return {
            assignment.game_seed: {
                seat: factories[assignment.seat_identities[index]]
                for index, seat in enumerate(Seat)
            }
            for assignment in self.assignments
        }

    def plan_value(self) -> dict[str, object]:
        return {
            "plan_schema_version": PLAN_SCHEMA_VERSION,
            "pilot_role": PILOT_ROLE,
            "population_id": self.population_id,
            "seat_assignment_semantics_id": self.seat_assignment_semantics_id,
            "policies": [
                {"identity": value.identity, "reference": value.reference}
                for value in self.policies
            ],
            "ordered_seeds": list(STAGE3_DEVELOPMENT_SEEDS),
            "train_seeds": list(STAGE3_TRAIN_SEEDS),
            "validation_seeds": list(STAGE3_VALIDATION_SEEDS),
            "seat_order": [seat.value for seat in Seat],
            "seat_assignments": [
                {
                    "game_seed": value.game_seed,
                    "seat_identities": list(value.seat_identities),
                }
                for value in self.assignments
            ],
            "seat_occupancy": {
                identity: list(counts)
                for identity, counts in sorted(self.seat_occupancy().items())
            },
            "seat_balanced": self.is_seat_balanced,
        }

    @property
    def population_identity(self) -> str:
        """seat assignmentまで含むpopulation identity。"""
        return hashlib.sha256(canonical_json_bytes(self.plan_value())).hexdigest()


def _uniform_plan(population_id: str, policy: SeatPolicyReference) -> PopulationPlan:
    return PopulationPlan(
        population_id=population_id,
        seat_assignment_semantics_id=FIXED_SEAT_ASSIGNMENT_ID,
        policies=(policy,),
        assignments=tuple(
            GameSeatAssignment(seed, (policy.identity,) * len(tuple(Seat)))
            for seed in STAGE3_DEVELOPMENT_SEEDS
        ),
    )


def population_a_plan() -> PopulationPlan:
    """Population A — historical continuity / cheap structural reference。"""
    return _uniform_plan(
        "A", SeatPolicyReference(identity=TWO_STEP, reference=TWO_STEP)
    )


def population_b_plan() -> PopulationPlan:
    """Population B — current strength / call-capable reference。"""
    return _uniform_plan(
        "B", SeatPolicyReference(identity=YAKUHAI_CALL, reference=YAKUHAI_CALL)
    )


MIXED_BASE_ORDER = (
    SeatPolicyReference(identity=TWO_STEP, reference=TWO_STEP),
    SeatPolicyReference(
        identity=GENBUTSU_DEFENSE_TWO_STEP,
        reference=GENBUTSU_DEFENSE_TWO_STEP_REFERENCE,
    ),
    SeatPolicyReference(identity=HAND_VALUE_AWARE, reference=HAND_VALUE_AWARE),
    SeatPolicyReference(identity=YAKUHAI_CALL, reference=YAKUHAI_CALL),
)
"""Population Cのbase order。

`GenbutsuDefenseTwoStepUkeirePolicy`は`POLICY_CATALOG`未登録のpublic exportで
あり、curated aliasを増やさず既存のexplicit import referenceで解決する。
"""


def population_c_plan() -> PopulationPlan:
    """Population C — seat-balanced mixed first-party population。

    seed index `i = seed - 180`でbase orderを1 seatずつrotateする。

    ```text
    seat_identity[s] = base[(s - i) mod 4]
    ```

    これによりbase policy `j`はseed `i`でseat `(j + i) mod 4`に座り、12 hanchan
    全体で各Policyが各seatをちょうど3回担当する。balanceはprotocol invariantで
    あり、成立しない場合はfail closedする。
    """
    seat_count = len(tuple(Seat))
    assignments = tuple(
        GameSeatAssignment(
            seed,
            tuple(
                MIXED_BASE_ORDER[(seat_index - index) % seat_count].identity
                for seat_index in range(seat_count)
            ),
        )
        for index, seed in enumerate(STAGE3_DEVELOPMENT_SEEDS)
    )
    plan = PopulationPlan(
        population_id="C",
        seat_assignment_semantics_id=ROTATING_SEAT_ASSIGNMENT_ID,
        policies=MIXED_BASE_ORDER,
        assignments=assignments,
    )
    if not plan.is_seat_balanced:
        raise Stage3PopulationError(
            "the mixed Stage 3 population must seat every Policy in every seat "
            "the same number of times"
        )
    return plan


def stage3_population_plans() -> tuple[PopulationPlan, ...]:
    """Stage 3 Entry Gateのlocked 3 population。"""
    return (population_a_plan(), population_b_plan(), population_c_plan())


def plan_for_population_id(population_id: str) -> PopulationPlan:
    for plan in stage3_population_plans():
        if plan.population_id == population_id:
            return plan
    raise Stage3PopulationError(f"unknown Stage 3 population id {population_id!r}")


__all__ = [
    "FIXED_SEAT_ASSIGNMENT_ID",
    "GENBUTSU_DEFENSE_TWO_STEP",
    "GENBUTSU_DEFENSE_TWO_STEP_REFERENCE",
    "HAND_VALUE_AWARE",
    "MIXED_BASE_ORDER",
    "PILOT_HANCHAN_PER_POPULATION",
    "PILOT_ROLE",
    "PLAN_SCHEMA_VERSION",
    "ROTATING_SEAT_ASSIGNMENT_ID",
    "TWO_STEP",
    "YAKUHAI_CALL",
    "GameSeatAssignment",
    "PopulationPlan",
    "SeatPolicyReference",
    "Stage3PopulationError",
    "plan_for_population_id",
    "population_a_plan",
    "population_b_plan",
    "population_c_plan",
    "stage3_population_plans",
]
