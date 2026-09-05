"""Arena #148 population-mix pilotのlocked protocol constants.

本pilotは Arena #131 (`ENTRY GATE REFORMULATE`) と Arena #146
(`KAN COVERAGE SOURCE QUALIFIED FOR MIX DESIGN`) のsuccessorである。どちらの
rescue runでもなく、historical protocol / seeds / population identity /
validatorsは一切変更せず、successor-specificなidentityだけを追加する。

```text
role              DEVELOPMENT-ONLY POPULATION-MIX SELECTION
ordered seeds     330..353            (24 hanchan / arm)
TRAIN-dev         330..347            (18 hanchan)
VALIDATION-dev    348..353            ( 6 hanchan)
formal TEST       none
arms              A (0%) / B (12.5%) / C (25%)
total generated   72 hanchan
```

3 armは同じordered seedsを **意図的に** 共有する。同じinitial game randomnessに
対してpopulation constructionだけを変えるdevelopment comparisonであり、seed reuse
事故ではない。armごとに独立したpopulation identity / artifactを持つ。

決めるのは`population construction / augmentation fraction`だけである。Policy
strength改善、kan戦略改善、HandBelief architecture変更は目的ではない。

```text
population-mix selection
!= Policy strength comparison
!= KanCoverage Policy adoption
!= new HandBelief architecture search
!= Phase 10 large-scale generation
```

結果を見てからseed、split、seat assignment、classification条件を変更しない。
`50%` / `100%`へのresult-drivenな救済extensionもしない。
"""

from lisjong_arena.phase5_belief_dataset.split import (
    MIX_PILOT_DEVELOPMENT_SEEDS,
    MIX_PILOT_TRAIN_SEEDS,
    MIX_PILOT_VALIDATION_SEEDS,
    FirstPartySplitPolicy,
)

PILOT_ROLE = "development-only-population-mix-selection"
"""このpilotのrole。caller optionにしないprotocol invariantである。"""

SPLIT_POLICY = FirstPartySplitPolicy.MIX_PILOT_DEVELOPMENT
ORDERED_SEEDS = MIX_PILOT_DEVELOPMENT_SEEDS
TRAIN_SEEDS = MIX_PILOT_TRAIN_SEEDS
VALIDATION_SEEDS = MIX_PILOT_VALIDATION_SEEDS
PILOT_HANCHAN_PER_ARM = len(ORDERED_SEEDS)
SEAT_COUNT = 4
SEAT_SLOTS_PER_ARM = PILOT_HANCHAN_PER_ARM * SEAT_COUNT

PRIMARY_IDENTITY = "yakuhai-call"
PRIMARY_REFERENCE = "yakuhai-call"
"""primary sourceはArena `POLICY_CATALOG`のcurated aliasで解決する。

`yakuhai-call`はlisjong-owned current strength baselineであり、本pilotで
semanticsを変更しない。
"""

AUGMENTATION_IDENTITY = "kan-coverage-yakuhai-call"
AUGMENTATION_REFERENCE = (
    "lisjong.policies.kan_coverage_yakuhai_call:KanCoverageYakuhaiCallPolicy"
)
"""augmentation sourceはArena #146と同じexplicit import referenceで解決する。

catalogへ登録しない。coverage sourceであり、strength baselineでも推奨gameplayでも
final training populationでもない。
"""

ARM_IDS = ("A", "B", "C")
CONTROL_ARM_ID = "A"

AUGMENTATION_SLOTS_BY_ARM = {"A": 0, "B": 12, "C": 24}
"""armごとのcoverage-source seat slot数。96 seat slots中の実数である。

```text
A     0 / 96 =  0.0%
B    12 / 96 = 12.5%
C    24 / 96 = 25.0%
```
"""

AUGMENTED_GAMES_BY_ARM = {"A": 0, "B": 12, "C": 24}
"""armごとの「coverage seatを1つ持つhanchan」数。

B / Cとも1 hanchanあたりのcoverage seatは高々1である。coverage sourceだけの
hanchanを混ぜるgame単位のclustered augmentationは採らない。
"""

CONTROL_SEAT_ASSIGNMENT_ID = "fixed-single-policy-v1"
AUGMENTED_SEAT_ASSIGNMENT_ID = "deterministic-balanced-seat-slot-augmentation-v1"
"""augmented armのseat assignment semantics identity。

seed index `i = seed - 330` からdeterministicに導出し、PRNGを使わない。

```text
B   coverage present iff i % 2 == 0     coverage seat index = (i // 2) % 4
C   coverage present for every i        coverage seat index = i % 4
```

いずれもcoverage actor seatがE/S/W/Nへexact balancedになる（B: 各seat 3回、
C: 各seat 6回）。
"""

GENERATION_SEMANTICS_ID = "phase4-first-party-recording-with-phase2-equality-v1"
"""generation semanticsのidentity。

既存Phase 4 protocol（recording + persistence + strict readback + TURN derivation
+ Phase 2 equality re-run）をそのまま使うことを明示するidentityであり、新しい
generation pathを作らない。
"""

PLAN_SCHEMA_VERSION = "stage3-mix-pilot-population-plan-v1"
MANIFEST_SCHEMA_VERSION = "stage3-mix-pilot-population-manifest-v1"
MODEL_ARTIFACT_SCHEMA_VERSION = "stage3-mix-pilot-sequential-model-v1"
RESULT_SCHEMA_VERSION = "stage3-mix-pilot-result-v1"
COMPARISON_SCHEMA_VERSION = "stage3-mix-pilot-paired-comparison-v1"

KAN_KINDS = ("daiminkan", "ankan", "kakan")
"""public `PublicMeldType`と同じkan kind名。新しいvocabularyを作らない。"""

BOOTSTRAP_SEED = 148
BOOTSTRAP_REPLICATES = 10000
BOOTSTRAP_LOWER_INDEX = 249
BOOTSTRAP_UPPER_INDEX = 9750
"""paired per-hanchan bootstrapのlocked constants。

Phase 9 confirmatory bootstrapはformal holdout `160..179` / 20 clustersへ
hard lockされたvalidatorを持つため再利用しない。本pilotは6 VALIDATION hanchanの
paired clusterに対して同じpercentile semanticsを独立に固定する。

`BOOTSTRAP_LOWER_INDEX` / `BOOTSTRAP_UPPER_INDEX`は10,000 replicateのsorted
distributionにおける2.5% / 97.5%のindexである。
"""

CLEAR_REGRESSION = "CLEAR MODEL-QUALITY REGRESSION"
NO_CLEAR_REGRESSION = "NO CLEAR MODEL-QUALITY REGRESSION"
"""paired comparisonのexhaustive classification。

`NO CLEAR MODEL-QUALITY REGRESSION`は`equivalent`を意味しない。本pilotはformal
TESTではないため、`no significant difference == equivalent`とは解釈しない。
"""

UNMEASURED = "UNMEASURED / ABSENT IN PILOT"
OBSERVED = "OBSERVED"
OPPORTUNITY_OBSERVED = "OPPORTUNITY OBSERVED / NOT SELECTED"
CONTRACT_VIOLATION = "SOURCE CONTRACT VIOLATION"
KIND_INTERPRETATIONS = (
    UNMEASURED,
    OBSERVED,
    OPPORTUNITY_OBSERVED,
    CONTRACT_VIOLATION,
)
"""kan kindごとのzero-count解釈。Arena #146と同じ意味論をそのまま使う。"""

MIX_LOCKED_LOW = "MIX LOCKED — 12.5% AUGMENTATION"
MIX_LOCKED_MEDIUM = "MIX LOCKED — 25% AUGMENTATION"
COVERAGE_INSUFFICIENT = "MIX REFORMULATE — COVERAGE INSUFFICIENT"
QUALITY_TRADEOFF = "MIX REFORMULATE — QUALITY / DISTRIBUTION TRADEOFF"
INCONCLUSIVE = "MIX REFORMULATE — INCONCLUSIVE"
SEED_PLAN_REFORMULATE = "SEED PLAN REFORMULATE"
STOP_INVALID = "STOP / INVALID"

OUTCOMES = (
    MIX_LOCKED_LOW,
    MIX_LOCKED_MEDIUM,
    COVERAGE_INSUFFICIENT,
    QUALITY_TRADEOFF,
    INCONCLUSIVE,
    SEED_PLAN_REFORMULATE,
    STOP_INVALID,
)
"""exhaustive outcome集合。結果を見てからoutcome定義を変更しない。

`SEED PLAN REFORMULATE`はresult exposure前のfreshness preflightでしか選べず、
measurementからは導出しない。
"""

SELECTION_RULE = (
    "1. hard validity fails -> STOP / INVALID; "
    "2. neither B nor C satisfies coverage-source accounting -> MIX REFORMULATE "
    "— COVERAGE INSUFFICIENT; "
    "3. coverage holds but both B and C carry a clear model-quality regression "
    "-> MIX REFORMULATE — QUALITY / DISTRIBUTION TRADEOFF; "
    "4. B satisfies candidate eligibility -> MIX LOCKED — 12.5% AUGMENTATION; "
    "5. B does not and C does -> MIX LOCKED — 25% AUGMENTATION; "
    "6. otherwise -> MIX REFORMULATE — INCONCLUSIVE. "
    "When both B and C are eligible the lower augmentation fraction wins: "
    "minimising the intervention into the training distribution that still "
    "closes the kan / rinshan coverage hole is fixed as the selection priority "
    "before any result is exposed"
)

RETRY_RULE = (
    "no retry: a failed generation is reported as a failure and neither seeds nor "
    "the ordered seed population are replaced or extended after execution; a "
    "deterministic infrastructure failure may re-run the same seeds under the "
    "same plan with the reason recorded"
)

REGRESSION_RULE = (
    "Delta = MAE(Model A) - MAE(candidate model) per VALIDATION hanchan, so a "
    "positive Delta means the candidate is better; a candidate carries CLEAR "
    "MODEL-QUALITY REGRESSION on an evaluation population when the 95% paired "
    "hanchan bootstrap interval of the pooled Delta has an upper bound below "
    "zero. This pilot is not a formal TEST, so the absence of a clear regression "
    "is never read as equivalence"
)

__all__ = [
    "ARM_IDS",
    "AUGMENTATION_IDENTITY",
    "AUGMENTATION_REFERENCE",
    "AUGMENTATION_SLOTS_BY_ARM",
    "AUGMENTED_GAMES_BY_ARM",
    "AUGMENTED_SEAT_ASSIGNMENT_ID",
    "BOOTSTRAP_LOWER_INDEX",
    "BOOTSTRAP_REPLICATES",
    "BOOTSTRAP_SEED",
    "BOOTSTRAP_UPPER_INDEX",
    "CLEAR_REGRESSION",
    "COMPARISON_SCHEMA_VERSION",
    "CONTRACT_VIOLATION",
    "CONTROL_ARM_ID",
    "CONTROL_SEAT_ASSIGNMENT_ID",
    "COVERAGE_INSUFFICIENT",
    "GENERATION_SEMANTICS_ID",
    "INCONCLUSIVE",
    "KAN_KINDS",
    "KIND_INTERPRETATIONS",
    "MANIFEST_SCHEMA_VERSION",
    "MIX_LOCKED_LOW",
    "MIX_LOCKED_MEDIUM",
    "MODEL_ARTIFACT_SCHEMA_VERSION",
    "NO_CLEAR_REGRESSION",
    "OBSERVED",
    "OPPORTUNITY_OBSERVED",
    "ORDERED_SEEDS",
    "OUTCOMES",
    "PILOT_HANCHAN_PER_ARM",
    "PILOT_ROLE",
    "PLAN_SCHEMA_VERSION",
    "PRIMARY_IDENTITY",
    "PRIMARY_REFERENCE",
    "QUALITY_TRADEOFF",
    "REGRESSION_RULE",
    "RESULT_SCHEMA_VERSION",
    "RETRY_RULE",
    "SEAT_COUNT",
    "SEAT_SLOTS_PER_ARM",
    "SEED_PLAN_REFORMULATE",
    "SELECTION_RULE",
    "SPLIT_POLICY",
    "STOP_INVALID",
    "TRAIN_SEEDS",
    "UNMEASURED",
    "VALIDATION_SEEDS",
]
