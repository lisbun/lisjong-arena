"""Issue #252 progression development evaluationのlocked protocol定数とidentity。

`lisbun/lisjong #169 / PR #170`でmergeされたexact adaptive candidate

```text
TerminalShantenProgressionMechanismRiichiDefensePolicy
```

を、そのexact parent

```text
MechanismRiichiDefenseYakuhaiCallPolicy   (Arena identity: mechanism-riichi-defense)
```

と同じfresh seeds / seat rotations / passive tsumogiri x3条件で比較するための、
Issue-scopedなprotocol値だけをこのmoduleが所有する。

## このmoduleが所有しないもの

Policy semantics、game execution、seat rotation、strength aggregation、artifact
schema、統計の定義はすべて既存Arena contract(``single_round_evaluation`` /
``single_round_artifact`` / ``_execution_safety``)が所有する。ここはその上へ
Issue #252が事前登録した固定条件を載せるだけであり、新しいevaluation
frameworkもgeneric comparator frameworkも導入しない。

## candidate semanticsを変更しない

candidate Pはlisjong側のexact adaptive implementationをそのまま使う。Arenaは
approximation、Monte Carlo、beam search、horizon変更、pruning追加、
`lisjong #171`のclairvoyant / draw-multiset reductionを一切導入しない。#171は
adaptive objectiveと等価でないことが反例付きで確定してcloseされており、本Issue
では採用しない。``require_exact_candidate_semantics()``がbound revision上の
identityとその不在をfail closedで確認する。
"""

from __future__ import annotations

import hashlib
import pkgutil
from dataclasses import dataclass
from typing import Any

from lisjong.policies.terminal_shanten_progression_mechanism_riichi_defense import (
    DEFAULT_HORIZON,
    ProgressionDecisionAnalysis,
    TerminalShantenProgressionMechanismRiichiDefensePolicy,
)

from lisjong_arena._artifact_io import canonical_json_text
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

PROTOCOL_ID = "progression-development-passive-x3-v1"
"""Issue #252 two-phase evaluation protocolのversioned identity。"""

GAME_MODE = SINGLE_ROUND_GAME_MODE
"""protocol invariantのgame mode。callerが選べるoptionではない。"""

ROTATION_COUNT = SINGLE_ROUND_ROTATION_COUNT
"""1 seedあたりのcandidate seat rotation数。"""

FORMAL_TEST = False
"""本Issueはdevelopment screenであり、formal holdout testではない。"""

MAX_STEPS = 10_000
"""1 gameのstep上限。protocol invariantであり、phaseごとに変えられない。

``SingleRoundArtifactPlan``が記録するreproducibility条件の1つなので、Phase A
とPhase Bで別の値を使えると同じlockの下で条件が変わってしまう。CLIからも
指定できないようにし、protocol document経由でlock / record / resultへbindする。
"""

EXECUTION_BRANCH = "main"
"""real executionを許すlong-lived branch。callerが選べるoptionではない。

Issue #252はreviewed merged mainからの実行だけを許す。branchをcallerが
変えられると、PR branchをexecution targetにしたlockでreal executionへ
進めてしまう。
"""

PHASE_A_SEEDS: tuple[int, ...] = tuple(range(647, 651))
"""Phase A technical feasibility population(647..650)。strength evidenceではない。"""

PHASE_B_SEEDS: tuple[int, ...] = tuple(range(651, 751))
"""Phase B development population(651..750)。paired primary unitはこの各seed。"""

PHASE_A_GAME_COUNT = ROTATION_COUNT * len(PHASE_A_SEEDS)
"""Phase Aのcandidate game数(16)。"""

PHASE_B_GAMES_PER_ARM = ROTATION_COUNT * len(PHASE_B_SEEDS)
"""1 armあたりのPhase B game数(400)。"""

PHASE_B_TOTAL_GAMES = 2 * PHASE_B_GAMES_PER_ARM
"""P arm + C armの合計game数(800)。independent sample数ではない。"""

FEASIBILITY_WALL_CLOCK_LIMIT_HOURS = 8.0
"""事前登録したP-arm projected wall-clockの実務上限。strength thresholdではない。"""

WORKER_SWEEP: tuple[int, ...] = (1, 4, 8, 16)
"""Phase Aのworker sweep。16はmachineが対応する場合のみ(下記参照)。"""

CANDIDATE_IDENTITY = "terminal-shanten-progression-mechanism-riichi-defense"
"""candidate P(#169/#170 exact adaptive generation)のArena identity。"""

CANDIDATE_SOURCE_MODULE = (
    "lisjong.policies.terminal_shanten_progression_mechanism_riichi_defense"
)
"""candidate Pのsource implementation module path。"""

CANDIDATE_CLASS_NAME = "TerminalShantenProgressionMechanismRiichiDefensePolicy"
"""candidate Pのexact class名。"""

CANDIDATE_HORIZON = 3
"""#170 adaptive candidateが使うhorizon。Arenaはこれを変更しない。"""

PARENT_IDENTITY = "mechanism-riichi-defense"
"""candidateのexact parent generation(current promoted heuristic baseline)。"""

PARENT_CLASS_NAME = "MechanismRiichiDefenseYakuhaiCallPolicy"
"""parent Cのexact class名。"""

COMPARATOR_IDENTITY = PASSIVE_TSUMOGIRI_IDENTITY
"""passive comparator Tのidentity。

既存`PassiveTsumogiriPolicy`実装をそのまま再利用するため、identityも既存の
ものをそのまま使う。同じ挙動に別名を与えて2つ目のcomparatorがあるように
見せない。historical名にP1 Gate Bが含まれるのは初出experimentの記録であり、
semanticsは本Issueが要求するpassive tsumogiri x3と同一である。
"""

COMPARATOR_SEMANTICS = PASSIVE_TSUMOGIRI_SEMANTICS
"""comparator semanticsの列(result documentへそのまま記録する)。"""

FORBIDDEN_APPROXIMATION_MARKERS: tuple[str, ...] = (
    "clairvoyant",
    "draw_multiset",
    "drawmultiset",
)
"""bound revisionへ混入していないことを確認する#171系approximationのmarker。"""

SIGNAL_LABEL = "PROGRESSION DEVELOPMENT SIGNAL"
NEGATIVE_LABEL = "PROGRESSION DEVELOPMENT NEGATIVE"
INCONCLUSIVE_LABEL = "PROGRESSION DEVELOPMENT INCONCLUSIVE"
INFEASIBLE_LABEL = "PROGRESSION DEVELOPMENT EVALUATION INFEASIBLE"

CLASSIFICATION_RULE_ID = "paired-seed-block-normal-approx-95-interval-v1"
"""primary classification ruleのversioned identity。"""


class ProgressionProtocolError(ValueError):
    """Issue #252のlocked protocol条件を満たさない場合。"""


def create_terminal_shanten_progression() -> (
    TerminalShantenProgressionMechanismRiichiDefensePolicy
):
    """candidate Pのfresh instanceを1つ返す。

    factoryはこのmodule top-levelのimport可能なcallableであり、spawn worker
    からserialize / importできる(``check_policy_spec_serializable()``が確認する)。
    """
    return TerminalShantenProgressionMechanismRiichiDefensePolicy()


def candidate_spec() -> PolicySpec:
    """candidate P(#170 exact adaptive)の``PolicySpec``。"""
    return PolicySpec(
        identity=CANDIDATE_IDENTITY, factory=create_terminal_shanten_progression
    )


def parent_spec() -> PolicySpec:
    """parent C(``mechanism-riichi-defense``)の``PolicySpec``。

    curated catalogのentryをそのまま使い、同じPolicyへ別identityを与えない。
    """
    return POLICY_CATALOG[PARENT_IDENTITY]


def comparator_spec() -> PolicySpec:
    """passive comparator Tの``PolicySpec``。

    既存Gate B comparator実装をthin reuseする。Arenaは同じsemanticsを
    再実装しない。
    """
    return PolicySpec(identity=COMPARATOR_IDENTITY, factory=create_passive_tsumogiri)


@dataclass(frozen=True, slots=True)
class CandidateSemanticBinding:
    """bound lisjong revision上で確認できたcandidate identityのfact。"""

    identity: str
    source_module: str
    class_name: str
    parent_class_name: str
    horizon: int
    adaptive_analysis_type: str
    clairvoyant_semantics_present: bool

    def to_document(self) -> dict[str, object]:
        return {
            "adaptive_analysis_type": self.adaptive_analysis_type,
            "class_name": self.class_name,
            "clairvoyant_semantics_present": self.clairvoyant_semantics_present,
            "horizon": self.horizon,
            "identity": self.identity,
            "parent_class_name": self.parent_class_name,
            "source_module": self.source_module,
        }


def _forbidden_approximation_modules() -> tuple[str, ...]:
    """bound revisionの``lisjong.policies``にある#171系module名を返す。"""
    import lisjong.policies as policies_package

    return tuple(
        sorted(
            module.name
            for module in pkgutil.iter_modules(policies_package.__path__)
            if any(marker in module.name for marker in FORBIDDEN_APPROXIMATION_MARKERS)
        )
    )


def require_exact_candidate_semantics() -> CandidateSemanticBinding:
    """bound revisionのcandidateが#170 adaptive generationであることを確認する。

    ここで確認するのはidentity bindingだけである。parent-equivalent behavior
    (positive completion decisionでparentと同じactionを返すこと)はlisjong側の
    contractであり、Arenaはそのtestを複製しない。

    #171 clairvoyant / draw-multiset approximationがbound revisionへ入って
    いないことも、module名markerとadaptive typeの存在で確認する。
    """
    candidate_class = TerminalShantenProgressionMechanismRiichiDefensePolicy
    if candidate_class.__name__ != CANDIDATE_CLASS_NAME:
        raise ProgressionProtocolError(
            "bound candidate class name is not the locked #170 implementation"
        )
    if candidate_class.__module__ != CANDIDATE_SOURCE_MODULE:
        raise ProgressionProtocolError(
            "bound candidate module is not the locked #170 source implementation"
        )
    parent_class = POLICY_CATALOG[PARENT_IDENTITY].factory().__class__
    if parent_class.__name__ != PARENT_CLASS_NAME:
        raise ProgressionProtocolError(
            "bound parent class name is not the locked mechanism-riichi-defense "
            "generation"
        )
    if not issubclass(candidate_class, parent_class):
        raise ProgressionProtocolError(
            "bound candidate is not a generation of its exact parent"
        )
    if DEFAULT_HORIZON != CANDIDATE_HORIZON:
        raise ProgressionProtocolError(
            f"bound candidate horizon is {DEFAULT_HORIZON!r}, not the locked "
            f"{CANDIDATE_HORIZON!r}"
        )
    forbidden = _forbidden_approximation_modules()
    if forbidden:
        raise ProgressionProtocolError(
            "bound revision exposes forbidden approximation modules: "
            f"{', '.join(forbidden)}"
        )
    return CandidateSemanticBinding(
        identity=CANDIDATE_IDENTITY,
        source_module=CANDIDATE_SOURCE_MODULE,
        class_name=candidate_class.__name__,
        parent_class_name=parent_class.__name__,
        horizon=DEFAULT_HORIZON,
        adaptive_analysis_type=ProgressionDecisionAnalysis.__name__,
        clairvoyant_semantics_present=False,
    )


def require_exact_comparator() -> dict[str, object]:
    """passive comparator Tが既存実装そのものであることを確認する。"""
    comparator = create_passive_tsumogiri()
    if type(comparator) is not PassiveTsumogiriPolicy:
        raise ProgressionProtocolError(
            "passive comparator is not the reused PassiveTsumogiriPolicy"
        )
    return {
        "identity": COMPARATOR_IDENTITY,
        "reused_implementation": (
            "lisjong_arena.learned_policy_offline_q.p1_gate_b_comparator"
            ":PassiveTsumogiriPolicy"
        ),
        "role": "weak-fixed-development-comparator",
        "semantics": list(COMPARATOR_SEMANTICS),
    }


def _require_exact_population(
    seeds: object, expected: tuple[int, ...], phase: str
) -> tuple[int, ...]:
    if not isinstance(seeds, tuple):
        raise ProgressionProtocolError(f"{phase} seeds must be a tuple")
    if seeds != expected:
        raise ProgressionProtocolError(
            f"{phase} seeds must be exactly the locked population "
            f"{expected[0]}..{expected[-1]} in order"
        )
    return seeds


def require_phase_a_population(seeds: object) -> tuple[int, ...]:
    """Phase A technical populationがlocked 647..650そのものかを確認する。"""
    return _require_exact_population(seeds, PHASE_A_SEEDS, "phase A")


def require_phase_b_population(seeds: object) -> tuple[int, ...]:
    """Phase B development populationがlocked 651..750そのものかを確認する。"""
    return _require_exact_population(seeds, PHASE_B_SEEDS, "phase B")


def require_disjoint_populations() -> None:
    """technical seedsがdevelopment evidenceへ混ざらないことを確認する。"""
    overlap = sorted(set(PHASE_A_SEEDS) & set(PHASE_B_SEEDS))
    if overlap:
        raise ProgressionProtocolError(
            f"technical and development populations overlap at {overlap}"
        )


def supported_worker_sweep(logical_cpu_count: int) -> tuple[int, ...]:
    """machineが実際に出せるworker sweepを返す。

    `WORKER_SWEEP`のうちlogical CPU数を超える設定は実行前に落とし、16を
    出せないmachineでは対応可能な最大値で置き換える。scoreやgame outcomeは
    一切参照しない。
    """
    if type(logical_cpu_count) is not int:
        raise ProgressionProtocolError("logical_cpu_count must be an int")
    if logical_cpu_count <= 0:
        raise ProgressionProtocolError("logical_cpu_count must be positive")
    supported = tuple(worker for worker in WORKER_SWEEP if worker <= logical_cpu_count)
    if not supported:
        return (1,)
    if supported[-1] != logical_cpu_count and logical_cpu_count < max(WORKER_SWEEP):
        supported = (*supported, logical_cpu_count)
    return tuple(sorted(set(supported)))


def document_identity(document: dict[str, Any]) -> str:
    """canonical JSON textのsha256を返すcontent-addressed identity。

    既存one-shot execution lock / resultと同じ考え方であり、identity field
    自身はcaller側でpayloadから除いてから渡す。
    """
    return hashlib.sha256(canonical_json_text(document).encode("utf-8")).hexdigest()


def progression_diagnostics_availability() -> dict[str, object]:
    """candidate側progression diagnosticsがstable seamで観測できるかを返す。

    bound revisionの``TerminalShantenProgressionMechanismRiichiDefensePolicy``は
    typed ``ProgressionDecisionAnalysis``をdecision内部で生成するが、
    ``_decide_discard()``は``PolicyDecision(action=selected, analysis=None)``を
    返し、この値をpublic decision contractへ載せない(#170の設計判断)。したがって
    activation / action-change / root post-discard shantenは、production
    selectionが返すdecisionからは観測できない。

    Arenaはこれを埋めるためにprogression DPを二度実行しない。lisjongのprivate
    DP internalsも再構築しない。optional diagnosticがunavailableであることは
    strength protocolのinvalidityを意味しない(Issue #252)。
    """
    return {
        "action_change_rate": None,
        "activation_rate": None,
        "available": False,
        "candidate_decisions": None,
        "reason": (
            "the bound #170 candidate returns PolicyDecision.analysis=None, so "
            "typed ProgressionDecisionAnalysis is not observable through the "
            "stable decision seam; Arena does not re-run the progression DP or "
            "reconstruct private internals to obtain diagnostics"
        ),
        "root_post_discard_shanten_distribution": None,
        "typed_analysis_type": ProgressionDecisionAnalysis.__name__,
    }


def protocol_document() -> dict[str, object]:
    """lock / resultへ埋め込むprotocol条件のplain document。"""
    return {
        "classification_rule_id": CLASSIFICATION_RULE_ID,
        "execution_branch": EXECUTION_BRANCH,
        "feasibility_wall_clock_limit_hours": FEASIBILITY_WALL_CLOCK_LIMIT_HOURS,
        "formal_test": FORMAL_TEST,
        "game_mode": GAME_MODE,
        "max_steps": MAX_STEPS,
        "phase_a_game_count": PHASE_A_GAME_COUNT,
        "phase_a_seeds": list(PHASE_A_SEEDS),
        "phase_b_games_per_arm": PHASE_B_GAMES_PER_ARM,
        "phase_b_seeds": list(PHASE_B_SEEDS),
        "phase_b_total_games": PHASE_B_TOTAL_GAMES,
        "protocol_id": PROTOCOL_ID,
        "rotation_count": ROTATION_COUNT,
        "worker_sweep": list(WORKER_SWEEP),
    }


__all__ = [
    "CANDIDATE_CLASS_NAME",
    "CANDIDATE_HORIZON",
    "CANDIDATE_IDENTITY",
    "CANDIDATE_SOURCE_MODULE",
    "CLASSIFICATION_RULE_ID",
    "COMPARATOR_IDENTITY",
    "COMPARATOR_SEMANTICS",
    "EXECUTION_BRANCH",
    "FEASIBILITY_WALL_CLOCK_LIMIT_HOURS",
    "FORMAL_TEST",
    "GAME_MODE",
    "INCONCLUSIVE_LABEL",
    "MAX_STEPS",
    "INFEASIBLE_LABEL",
    "NEGATIVE_LABEL",
    "PARENT_CLASS_NAME",
    "PARENT_IDENTITY",
    "PHASE_A_GAME_COUNT",
    "PHASE_A_SEEDS",
    "PHASE_B_GAMES_PER_ARM",
    "PHASE_B_SEEDS",
    "PHASE_B_TOTAL_GAMES",
    "PROTOCOL_ID",
    "ROTATION_COUNT",
    "SIGNAL_LABEL",
    "WORKER_SWEEP",
    "CandidateSemanticBinding",
    "ProgressionProtocolError",
    "candidate_spec",
    "comparator_spec",
    "create_terminal_shanten_progression",
    "document_identity",
    "parent_spec",
    "progression_diagnostics_availability",
    "protocol_document",
    "require_disjoint_populations",
    "require_exact_candidate_semantics",
    "require_exact_comparator",
    "require_phase_a_population",
    "require_phase_b_population",
    "supported_worker_sweep",
]
