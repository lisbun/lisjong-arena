"""Arena #146 successor coverage-source populationのexact identity。

#131の`stage3_entry_gate.population.PopulationPlan`はordered seeds `180..191`へ
lockされたhistorical protocol invariantを持つ。本pilotのためにそのlockを緩めず、
successor-specific planをここへ独立に持つ。`SeatPolicyReference` /
`GameSeatAssignment`というidentity valueだけを再利用する。

```text
identity          kan-coverage-yakuhai-call
reference         lisjong.policies.kan_coverage_yakuhai_call:KanCoverageYakuhaiCallPolicy
seat assignment   same Policy x4 / fixed uniform
ordered seeds     306..329
```

Policy instanceはgame・seatごとにfactoryから新規生成し、seat間・game間で
共有しない。Arena `POLICY_CATALOG`へは登録せず、既存explicit import reference
だけで解決する。
"""

import hashlib
from collections.abc import Callable
from dataclasses import dataclass

from lisjong_engine.seat import Seat

from lisjong_arena.phase4_raw_corpus.codec import canonical_json_bytes
from lisjong_arena.phase5_belief_dataset.split import (
    QUANTITATIVE_SEEDS,
    STAGE3_DEVELOPMENT_SEEDS,
)
from lisjong_arena.phase9_confirmatory.protocol import HOLDOUT_SEEDS
from lisjong_arena.stage3_entry_gate.population import (
    GameSeatAssignment,
    SeatPolicyReference,
)
from lisjong_arena.stage3_kan_coverage.protocol import (
    GENERATION_SEMANTICS_ID,
    ORDERED_SEEDS,
    PILOT_ROLE,
    PLAN_SCHEMA_VERSION,
    POLICY_IDENTITY,
    POLICY_IMPORT_REFERENCE,
    POPULATION_ID,
    SEAT_ASSIGNMENT_SEMANTICS_ID,
    SPLIT_POLICY,
    TRAIN_SEEDS,
    VALIDATION_SEEDS,
)

_HISTORICAL_SEEDS = frozenset(
    QUANTITATIVE_SEEDS + HOLDOUT_SEEDS + STAGE3_DEVELOPMENT_SEEDS
)
"""本pilotが再利用してはならないhistorical population seeds。

Stage 1/2 formal split (`100..159`)、Phase 9 confirmatory holdout (`160..179`)、
#131 historical development population (`180..191`) をsuccessor pilotへ混入
させないためのfail closed guardである。
"""


class KanCoveragePopulationError(ValueError):
    """kan coverage population planのcontract violation。"""


@dataclass(frozen=True, slots=True)
class KanCoveragePopulationPlan:
    """Arena #146で実行する単一populationのexact identity。

    seedsとroleはprotocol invariantであり、caller optionにしない。
    """

    policy: SeatPolicyReference
    assignments: tuple[GameSeatAssignment, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.policy, SeatPolicyReference):
            raise KanCoveragePopulationError("policy must be a SeatPolicyReference")
        if self.policy.identity != POLICY_IDENTITY:
            raise KanCoveragePopulationError(
                "the coverage-source population uses one locked Policy identity"
            )
        if self.policy.reference != POLICY_IMPORT_REFERENCE:
            raise KanCoveragePopulationError(
                "the coverage-source population uses one locked import reference"
            )
        assignments = tuple(self.assignments)
        if any(not isinstance(value, GameSeatAssignment) for value in assignments):
            raise KanCoveragePopulationError(
                "assignments must contain GameSeatAssignment values"
            )
        if tuple(value.game_seed for value in assignments) != ORDERED_SEEDS:
            raise KanCoveragePopulationError(
                "the kan coverage population is locked to ordered seeds 306..329"
            )
        if any(
            value.seat_identities != (POLICY_IDENTITY,) * len(tuple(Seat))
            for value in assignments
        ):
            raise KanCoveragePopulationError(
                "every seat of every game must run the same coverage-source Policy"
            )
        object.__setattr__(self, "assignments", assignments)

    @property
    def population_id(self) -> str:
        return POPULATION_ID

    @property
    def ordered_seeds(self) -> tuple[int, ...]:
        return ORDERED_SEEDS

    @property
    def train_seeds(self) -> tuple[int, ...]:
        return TRAIN_SEEDS

    @property
    def validation_seeds(self) -> tuple[int, ...]:
        return VALIDATION_SEEDS

    def seat_policy_factories_by_seed(
        self,
    ) -> dict[int, dict[Seat, Callable[[], object]]]:
        """Phase 4 generationへ渡すseed別 / seat別factory mapping。

        factoryはcallableのままで、Policy instanceはgame・seatごとに
        Phase 4側で新規生成される。instanceをここで共有しない。
        """
        factory = self.policy.factory()
        return {
            assignment.game_seed: dict.fromkeys(Seat, factory)
            for assignment in self.assignments
        }

    def plan_value(self) -> dict[str, object]:
        return {
            "plan_schema_version": PLAN_SCHEMA_VERSION,
            "pilot_role": PILOT_ROLE,
            "population_id": POPULATION_ID,
            "seat_assignment_semantics_id": SEAT_ASSIGNMENT_SEMANTICS_ID,
            "generation_semantics_id": GENERATION_SEMANTICS_ID,
            "split_policy_id": SPLIT_POLICY.value,
            "policies": [
                {"identity": self.policy.identity, "reference": self.policy.reference}
            ],
            "ordered_seeds": list(ORDERED_SEEDS),
            "train_seeds": list(TRAIN_SEEDS),
            "validation_seeds": list(VALIDATION_SEEDS),
            "seat_order": [seat.value for seat in Seat],
            "seat_assignments": [
                {
                    "game_seed": value.game_seed,
                    "seat_identities": list(value.seat_identities),
                }
                for value in self.assignments
            ],
            "test_partition_present": False,
        }

    @property
    def population_identity(self) -> str:
        """seat assignmentまで含むsuccessor population identity。"""
        return hashlib.sha256(canonical_json_bytes(self.plan_value())).hexdigest()


def kan_coverage_population_plan() -> KanCoveragePopulationPlan:
    """Arena #146のlocked coverage-source population。

    historical population seedsとの重なりはprotocol violationとしてfail closed
    する。#131のseedsを再利用しないことはこのpilotのidentityの一部である。
    """
    overlap = _HISTORICAL_SEEDS.intersection(ORDERED_SEEDS)
    if overlap:
        raise KanCoveragePopulationError(
            "the successor coverage-source population must not reuse historical "
            f"population seeds: {sorted(overlap)}"
        )
    policy = SeatPolicyReference(
        identity=POLICY_IDENTITY, reference=POLICY_IMPORT_REFERENCE
    )
    return KanCoveragePopulationPlan(
        policy=policy,
        assignments=tuple(
            GameSeatAssignment(seed, (POLICY_IDENTITY,) * len(tuple(Seat)))
            for seed in ORDERED_SEEDS
        ),
    )


__all__ = [
    "KanCoveragePopulationError",
    "KanCoveragePopulationPlan",
    "kan_coverage_population_plan",
]
