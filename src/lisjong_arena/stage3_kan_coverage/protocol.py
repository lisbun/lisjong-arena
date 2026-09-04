"""Arena #146 kan coverage-source qualification pilotのlocked protocol constants.

本pilotは Arena #131 (`ENTRY GATE REFORMULATE`) のsuccessorであり、rescue runでは
ない。#131のhistorical protocol / seeds / population identity / validatorsは
一切変更せず、successor-specificなidentityだけを追加する。

```text
role            DEVELOPMENT-ONLY COVERAGE-SOURCE QUALIFICATION
population      KanCoverageYakuhaiCallPolicy x4
ordered seeds   306..329            (24 hanchan)
TRAIN-dev       306..323            (18 hanchan)
VALIDATION-dev  324..329            (6 hanchan)
formal TEST     none
```

`KanCoverageYakuhaiCallPolicy`はHandBelief training coverage sourceであり、
strength baseline / recommended gameplay / final training populationではない。
本pilotのpositive outcomeは「次のpopulation-mix designへ進んでよい」という
意味だけで、Phase 10 entryやfinal population lockではない。

結果を見てからseedを追加・置換しない。rare kan kindが0件でも0件として報告する。
"""

from lisjong_arena.phase5_belief_dataset.split import (
    KAN_COVERAGE_DEVELOPMENT_SEEDS,
    KAN_COVERAGE_TRAIN_SEEDS,
    KAN_COVERAGE_VALIDATION_SEEDS,
    FirstPartySplitPolicy,
)

PILOT_ROLE = "development-only-coverage-source-qualification"
"""このpilotのrole。caller optionにしないprotocol invariantである。"""

POPULATION_ID = "kan-coverage"
POLICY_IDENTITY = "kan-coverage-yakuhai-call"
POLICY_IMPORT_REFERENCE = (
    "lisjong.policies.kan_coverage_yakuhai_call:KanCoverageYakuhaiCallPolicy"
)
"""Arena `POLICY_CATALOG`へ登録せず、既存explicit import referenceで解決する。"""

SEAT_ASSIGNMENT_SEMANTICS_ID = "fixed-single-policy-v1"
GENERATION_SEMANTICS_ID = "phase4-first-party-recording-with-phase2-equality-v1"
"""generation semanticsのidentity。

既存Phase 4 protocol（recording + persistence + strict readback + TURN derivation
+ Phase 2 equality re-run）をそのまま使うことを明示するidentityであり、新しい
generation pathを作らない。
"""

SPLIT_POLICY = FirstPartySplitPolicy.KAN_COVERAGE_DEVELOPMENT
ORDERED_SEEDS = KAN_COVERAGE_DEVELOPMENT_SEEDS
TRAIN_SEEDS = KAN_COVERAGE_TRAIN_SEEDS
VALIDATION_SEEDS = KAN_COVERAGE_VALIDATION_SEEDS
PILOT_HANCHAN = len(ORDERED_SEEDS)

PLAN_SCHEMA_VERSION = "stage3-kan-coverage-population-plan-v1"
MANIFEST_SCHEMA_VERSION = "stage3-kan-coverage-population-manifest-v1"
DIAGNOSTIC_SCHEMA_VERSION = "stage3-kan-coverage-diagnostic-v1"
ACCOUNTING_SCHEMA_VERSION = "stage3-kan-coverage-accounting-v1"
RESULT_SCHEMA_VERSION = "stage3-kan-coverage-result-v1"

KAN_KINDS = ("daiminkan", "ankan", "kakan")
"""public `PublicMeldType`と同じkan kind名。新しいvocabularyを作らない。"""

QUALIFIED = "KAN COVERAGE SOURCE QUALIFIED FOR MIX DESIGN"
INSUFFICIENT = "KAN COVERAGE SOURCE EMPIRICALLY INSUFFICIENT"
ACCOUNTING_REFORMULATE = "KAN COVERAGE ACCOUNTING REFORMULATE"
SEED_PLAN_REFORMULATE = "SEED PLAN REFORMULATE"
STOP_INVALID = "STOP / INVALID"

OUTCOMES = (
    QUALIFIED,
    INSUFFICIENT,
    ACCOUNTING_REFORMULATE,
    SEED_PLAN_REFORMULATE,
    STOP_INVALID,
)
"""exhaustive outcome集合。結果を見てからoutcome定義を変更しない。"""

RETRY_RULE = (
    "no retry: a failed generation is reported as a failure and neither seeds nor "
    "the ordered seed population are replaced or extended after execution"
)
CLASSIFICATION_RULE = (
    "QUALIFIED requires every hard validity gate, zero selection-contract "
    "violations, zero unaccounted selected kan, zero missing rinshan on confirmed "
    "kan with an expected continuation, and at least one eligible no-win kan "
    "opportunity that materializes as a confirmed kan with an observed rinshan "
    "draw retained by the dataset; a kan kind with zero eligible no-win "
    "opportunities is UNMEASURED / ABSENT IN PILOT, not a failure"
)

__all__ = [
    "ACCOUNTING_REFORMULATE",
    "ACCOUNTING_SCHEMA_VERSION",
    "CLASSIFICATION_RULE",
    "DIAGNOSTIC_SCHEMA_VERSION",
    "GENERATION_SEMANTICS_ID",
    "INSUFFICIENT",
    "KAN_KINDS",
    "MANIFEST_SCHEMA_VERSION",
    "ORDERED_SEEDS",
    "OUTCOMES",
    "PILOT_HANCHAN",
    "PILOT_ROLE",
    "PLAN_SCHEMA_VERSION",
    "POLICY_IDENTITY",
    "POLICY_IMPORT_REFERENCE",
    "POPULATION_ID",
    "QUALIFIED",
    "RESULT_SCHEMA_VERSION",
    "RETRY_RULE",
    "SEAT_ASSIGNMENT_SEMANTICS_ID",
    "SEED_PLAN_REFORMULATE",
    "SPLIT_POLICY",
    "STOP_INVALID",
    "TRAIN_SEEDS",
    "VALIDATION_SEEDS",
]
