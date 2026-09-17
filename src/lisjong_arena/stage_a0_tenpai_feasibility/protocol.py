"""Locked Stage A0 Tenpai label-path feasibility protocol (Issue #258).

`lisbun/lisjong-arena #258`は、current flat-BC decision rowに対応するhidden
opponent stateから、canonicalなnon-riichi structural-Tenpai targetを正確かつ
再現可能に生成できるかだけをfeasibility-onlyで資格確認する。modelは学習せず、
Stage A0のscientific TRAIN / VALIDATION corpusも生成しない。

このmoduleはArena-local feasibility protocol identityだけを所有する。

```text
feature / vocabulary / teacher / game mode
    -> lisjong_arena.learned_policy_offline_q.protocol (retained flat-BC corpus契約)
canonical Tenpai / wait semantics
    -> lisjong.belief.exact_wait_ground_truth (Arena側へ再実装しない)
```
"""

import hashlib
import inspect

from lisjong.belief import exact_wait_ground_truth

from lisjong_arena.learned_policy_offline_q import protocol as offline_q
from lisjong_arena.learned_policy_offline_q.artifact import PROVENANCE_FIELDS

from .errors import StageA0ProtocolError

PROTOCOL_ID = "arena-stage-a0-tenpai-feasibility-v1"
ISSUE_IDENTITY = "lisbun/lisjong-arena#258"
PARENT_ISSUE_IDENTITIES = (
    "lisbun/lisjong-arena#255",
    "lisbun/lisjong-project#57",
)

# --- Reused current flat-BC contract identity (unchanged) ----------------
#
# retained #140/#190 flat-BC corpusと同じfeature / vocabulary / teacher /
# game mode契約をそのまま参照する。Stage A0 feasibilityはこれらを再定義しない。

FEATURE_DIMENSION = offline_q.FEATURE_DIMENSION
VOCABULARY_SIZE = offline_q.VOCABULARY_SIZE
GAME_MODE = offline_q.GAME_MODE
TEACHER_IDENTITY = offline_q.TEACHER_IDENTITY
TEACHER_POLICY_CLASS = offline_q.TEACHER_POLICY_CLASS
TEACHER_POPULATION = offline_q.TEACHER_POPULATION
TEACHER_SOURCE_REVISION = offline_q.TEACHER_SOURCE_REVISION
Split = offline_q.Split
action_family_of = offline_q.action_family
verify_contract_identity = offline_q.verify_contract_identity

# --- Provenance identity classes ------------------------------------------
#
# retained corpusのexact augmentationでは、2種類のprovenanceを区別する。
#
# source-semantic provenance
#     retained rowのpublic semanticsとhidden stateのexactnessを決める
#     dependency identityであり、operatorがhistorical execution environmentを
#     再現すれば一致させられる。1 fieldでも一致しなければfail closedする。
#
# instrumentation provenance
#     #258のobserver / qualification implementationを持つArena revisionである。
#     このcodeはretained corpusを生成したhistorical Arena revisionには存在
#     しないため、qualificationを実行しながらそのrevisionを名乗ることは
#     論理的に不可能である。したがって一致は要求せず、両側を記録したうえで、
#     retained public rowのexact alignment
#     （decision identity / actor seat / round identity / feature bytes /
#     legal mask bytes / teacher action / same-state binding）が、
#     instrumentationを含むreplayでも同じpublic row semanticsを再現したことを
#     実証する。
#
# feature schema fingerprintとaction vocabulary fingerprintは
# `load_dataset()`がinstalled contractに対して既にfail closedで検証しており、
# ここで重複検証しない。

SOURCE_SEMANTIC_PROVENANCE_FIELDS = (
    "execution_environment",
    "lisjong_version",
    "lisjong_revision",
    "lisjong_engine_version",
    "lisjong_engine_revision",
    "riichienv_version",
    "python_version",
)
INSTRUMENTATION_PROVENANCE_FIELDS = (
    "lisjong_arena_version",
    "lisjong_arena_revision",
)

if set(SOURCE_SEMANTIC_PROVENANCE_FIELDS).intersection(
    INSTRUMENTATION_PROVENANCE_FIELDS
):
    raise RuntimeError("a provenance field cannot be both source and instrumentation")
if set(SOURCE_SEMANTIC_PROVENANCE_FIELDS) | set(
    INSTRUMENTATION_PROVENANCE_FIELDS
) != set(PROVENANCE_FIELDS):
    raise RuntimeError(
        "every retained provenance field must be classified as either "
        "source-semantic or instrumentation; an unclassified field must fail "
        "closed instead of being silently ignored"
    )


RETAINED_DATASET_IDENTITY = (
    "69094c1b82f2aaedfed57cb3021b90d44642c3978a2368d4d1e2d927c5a7b2f4"
)
"""#258本文が示すretained first-party flat-BC corpusのdataset identity。"""

RETAINED_TRAIN_SEEDS = offline_q.DATASET_TRAIN_SEEDS
"""retained augmentation qualificationで参照してよいTRAIN-side seed（245..264）。"""

PROTECTED_TEST_SEEDS = offline_q.DATASET_TEST_SEEDS
"""protected TEST（271..276）。qualification pathはこのseedを実行・参照しない。"""

EXCLUDED_QUALIFICATION_SEEDS = (
    offline_q.DATASET_VALIDATION_SEEDS + offline_q.DATASET_TEST_SEEDS
)
"""#258 technical qualificationで扱わないsplit（VALIDATION / protected TEST）。

VALIDATION targetのbehaviorを評価・要約しないという#258のexposure boundaryを
codeとして固定する。
"""

# --- Bounded technical smoke population ----------------------------------
#
# #258は、retained augmentationが資格化できない場合のfresh live-label pathを
# 証明するためのbounded technical smokeだけを許す。実行前にfixしたこの
# populationは、development / technical専用でありscientific evidenceではない。
#
# repository内で既にlock済みのlocal-game seed populationは、確認時点で次の
# とおりであり、そのいずれとも重複しない直後のcontiguous rangeを取る。
#
#     100..199  phase05 / phase5 / phase8 / phase9 historical
#     200..215  Stage 2 dataset
#     216..219  Stage 3 serving smoke
#     220..244  Stage 4a screening
#     245..276  Issue #140 dataset (retained flat-BC corpus)
#     277..280  Issue #140 serving smoke
#     281..305  Issue #140 strength screening
#     306..329  Arena #146 kan coverage
#     330..353  Arena #148 mix pilot
#     354..359  Issue #140 replacement TEST
#     360..439  Phase 10 scale learning curve
#     440..464  P1 Gate B
#     465..521  FH curriculum dataset / rollout
#     522..646  P1 shanten guard / P6 gate / higher fidelity families
#     647..750  Issue #252 progression development (Phase A / Phase B)

SMOKE_SEEDS = (751, 752)
SMOKE_POPULATION_IDENTITY = "arena-stage-a0-tenpai-feasibility-smoke-751-752"
SMOKE_POPULATION_ROLE = "DEVELOPMENT-ONLY TECHNICAL SMOKE"
SMOKE_POPULATION_LIMITATIONS = (
    "この population は fresh live-label path の技術成立だけを示す。"
    "label prevalence / model behavior を見て選んでいない。"
    "後続 Stage A0 の scientific TRAIN / VALIDATION / TEST へ再利用しない。",
)

_KNOWN_ALLOCATED_SEEDS = frozenset(range(100, 751))

if frozenset(SMOKE_SEEDS).intersection(_KNOWN_ALLOCATED_SEEDS):
    raise RuntimeError(
        "the Stage A0 technical smoke population collides with an already "
        "allocated seed range; resolve it as a seed plan reformulation before "
        "any execution, never after"
    )
if SMOKE_SEEDS != tuple(range(SMOKE_SEEDS[0], SMOKE_SEEDS[0] + len(SMOKE_SEEDS))):
    raise RuntimeError("the Stage A0 technical smoke population must be contiguous")

MAXIMUM_SMOKE_GAME_COUNT = len(SMOKE_SEEDS)
"""full Stage A0 corpus生成を防ぐhard upper bound。"""

# --- Canonical Tenpai semantics identity ---------------------------------

CANONICAL_WAIT_BUILDER = "lisjong.belief.exact_wait_ground_truth"
CANONICAL_WAIT_ENTRY_POINT = "exact_hand_belief_with_waits"
TILE_KIND_COUNT = 34
OPPONENT_TARGET_COUNT = 3
RELATIVE_OPPONENT_OFFSETS = (1, 2, 3)


def exact_wait_implementation_identity() -> str:
    """installedなcanonical exact-wait implementationのsource fingerprintを返す。

    Tenpai semanticsの正本はArenaではなくlisjong側であるため、Arenaはその
    implementationを再実装せずidentityだけを記録する。artifact / sidecarへ
    このfingerprintを焼き込むことで、labelを生成したcanonical実装が後から
    差し替わったことを検出できる。
    """
    source = inspect.getsource(exact_wait_ground_truth)
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def require_qualification_seed(seed: int) -> int:
    """retained qualificationで読んでよいTRAIN-side seedだけをfail closedで通す。"""
    if type(seed) is not int:
        raise TypeError("seed must be an int")
    if seed in PROTECTED_TEST_SEEDS:
        raise StageA0ProtocolError(
            f"seed {seed} belongs to the protected TEST split and must not be "
            "read by the #258 qualification path"
        )
    if seed not in RETAINED_TRAIN_SEEDS:
        raise StageA0ProtocolError(
            f"seed {seed} is not part of the retained TRAIN-side qualification "
            "population"
        )
    return seed


def require_smoke_seed(seed: int) -> int:
    """bounded technical smokeで許されたfresh seedだけをfail closedで通す。"""
    if type(seed) is not int:
        raise TypeError("seed must be an int")
    if seed not in SMOKE_SEEDS:
        raise StageA0ProtocolError(
            f"seed {seed} is not part of the locked Stage A0 technical smoke population"
        )
    return seed


__all__ = [
    "CANONICAL_WAIT_BUILDER",
    "INSTRUMENTATION_PROVENANCE_FIELDS",
    "CANONICAL_WAIT_ENTRY_POINT",
    "EXCLUDED_QUALIFICATION_SEEDS",
    "FEATURE_DIMENSION",
    "GAME_MODE",
    "ISSUE_IDENTITY",
    "MAXIMUM_SMOKE_GAME_COUNT",
    "OPPONENT_TARGET_COUNT",
    "PARENT_ISSUE_IDENTITIES",
    "PROTECTED_TEST_SEEDS",
    "PROTOCOL_ID",
    "RELATIVE_OPPONENT_OFFSETS",
    "RETAINED_DATASET_IDENTITY",
    "RETAINED_TRAIN_SEEDS",
    "SMOKE_POPULATION_IDENTITY",
    "SMOKE_POPULATION_LIMITATIONS",
    "SMOKE_POPULATION_ROLE",
    "SMOKE_SEEDS",
    "SOURCE_SEMANTIC_PROVENANCE_FIELDS",
    "TEACHER_IDENTITY",
    "TEACHER_POLICY_CLASS",
    "TEACHER_POPULATION",
    "TEACHER_SOURCE_REVISION",
    "TILE_KIND_COUNT",
    "VOCABULARY_SIZE",
    "Split",
    "action_family_of",
    "exact_wait_implementation_identity",
    "require_qualification_seed",
    "require_smoke_seed",
    "verify_contract_identity",
]
