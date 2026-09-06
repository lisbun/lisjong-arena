"""P1 Gate B — exact #158 candidate vs passive tsumogiri x3 (Issue #162).

`lisbun/lisjong-arena #158`のGate Aは、retained same-state rows上でP1 Q-v2が
hand progressionを方向として改善することを示した（`P1 HAND-PROGRESSION
SIGNAL`）。Gate Bはそこから**同じcandidateを一切変更せずserveし**、fresh
development seedのsingle-round rollout上でweakなpassive tsumogiri x3に対して
最低限のoffensive capabilityを示すかだけを見る。

```text
Gate A                              Gate B
retained same-state rows      ->    fresh single-round rollout
hand progression                    candidate vs passive tsumogiri x3
                                    4p-red-single / 25 seeds / 4 rotations
```

```text
exact #158 P1 serving checkpoint          passive tsumogiri v1
        |                                          |
        +----------- SingleRoundEvaluationPlan ----+
                             |
                  run_single_round_evaluation()   serial / workers = 1
                             |
                  save_single_round_artifact()    immutable
                             |
                  load_single_round_artifact()    strict readback
                             |
                  summarize_single_round_strength()  canonical re-derivation
                             |
                  build_gate_b_result() -> validate -> derive_classification()
```

## このmoduleが意図的に**しない**こと

- 新しいgame runner / seat rotation / score aggregationの実装
- Q-v1 / BC / Development Championとのcomparison
- hanchan、formal holdout、追加seed、retraining
- diagnostic metricからのclassification rule変更
- resultを見たあとのseed extension / comparator変更

Gate Bはstrength confirmationではなく、one-way viability filterである。
"""

import hashlib
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from lisjong_arena._artifact_io import canonical_json_text
from lisjong_arena.model import (
    SINGLE_ROUND_GAME_MODE,
    SINGLE_ROUND_ROTATION_COUNT,
    PolicySpec,
    SingleRoundEvaluationPlan,
)
from lisjong_arena.single_round_artifact import (
    SINGLE_ROUND_ARTIFACT_SCHEMA_VERSION,
    SINGLE_ROUND_EVALUATION_PROTOCOL,
    SingleRoundStrengthArtifact,
    collect_execution_provenance,
    execution_provenance_to_dict,
    load_single_round_artifact,
    parse_execution_provenance,
    save_single_round_artifact,
    summary_to_dict,
)
from lisjong_arena.single_round_evaluation import (
    aggregate_candidate_metrics,
    run_single_round_evaluation,
    summarize_single_round_strength,
)
from lisjong_arena.stage3_scale_learning_curve.protocol import (
    HISTORICALLY_CONSUMED_SEEDS,
)
from lisjong_arena.stage3_scale_learning_curve.protocol import (
    ORDERED_SEEDS as SCALE_LEARNING_CURVE_SEEDS,
)

from .errors import OfflineQError
from .p1_candidate import (
    LOCKED_P1_CANDIDATE,
    LOCKED_SELECTED_EPOCH,
    MATERIALIZATION_SOURCES,
    SERVING_CHECKPOINT_SCHEMA_VERSION,
    LoadedP1ServingCheckpoint,
    candidate_binding_document,
    require_candidate_identity,
    verify_locked_candidate_contract,
)
from .p1_gate_b_comparator import (
    PASSIVE_TSUMOGIRI_IDENTITY,
    comparator_block,
    passive_tsumogiri_spec,
)
from .p1_serving import create_p1_hybrid_runtime
from .protocol import PROTOCOL_ID
from .strength import (
    ActivationDiagnostics,
    PolicyInstanceRegistry,
    collect_activation_diagnostics,
)

P1_GATE_B_SCHEMA_VERSION = "arena-learned-policy-offlineq-p1-gate-b-v1"
P1_GATE_B_ID = "arena-learned-policy-offlineq-p1-keep-shanten-gate-b-162"
SOURCE_ISSUE = "lisbun/lisjong-arena#162"
PREDECESSOR_ISSUES = (
    "lisbun/lisjong-arena#140",
    "lisbun/lisjong-arena#152",
    "lisbun/lisjong-arena#158",
)
PARENT_ISSUE = "lisbun/lisjong-project#45"

RETENTION_BACKEND = "operator-local-durable"
CANDIDATE_RETENTION_KEY = "offlineq-162-p1-gate-b/candidate"
ARTIFACT_RETENTION_KEY = "offlineq-162-p1-gate-b/strength-artifact"

# --- Locked Gate B population ---------------------------------------------
#
# Issue #162がresult exposure前の第一候補としてlockしたfresh development
# population。`100..359`はhistorical prefix、`360..439`はPhase 10 scale
# learning curveが取得済みであり、Gate Bはその直後のcontiguous rangeを使う。

GATE_B_ORDERED_SEEDS = tuple(range(440, 465))
GATE_B_SEED_BLOCK_COUNT = 25
GATE_B_ROTATIONS_PER_SEED = SINGLE_ROUND_ROTATION_COUNT
GATE_B_GAME_COUNT = GATE_B_SEED_BLOCK_COUNT * GATE_B_ROTATIONS_PER_SEED
GATE_B_GAME_MODE = SINGLE_ROUND_GAME_MODE
GATE_B_MAX_WORKERS = 1
"""Learned runtimeのprocess serializationを本Issueへ持ち込まないためserial固定。"""

GATE_B_ROLE = "DEVELOPMENT-ONLY P1 GATE B"

_KNOWN_ALLOCATED_SEEDS = frozenset(
    (*HISTORICALLY_CONSUMED_SEEDS, *SCALE_LEARNING_CURVE_SEEDS)
)

if (
    len(GATE_B_ORDERED_SEEDS) != GATE_B_SEED_BLOCK_COUNT
    or GATE_B_ORDERED_SEEDS
    != tuple(range(GATE_B_ORDERED_SEEDS[0], GATE_B_ORDERED_SEEDS[0] + 25))
    or GATE_B_GAME_COUNT != 100
    or GATE_B_ROTATIONS_PER_SEED != 4
    or GATE_B_GAME_MODE != "4p-red-single"
):
    raise RuntimeError("the locked Gate B seed plan shape drifted")
if _KNOWN_ALLOCATED_SEEDS.intersection(GATE_B_ORDERED_SEEDS):
    raise RuntimeError(
        "the Gate B seed population collides with an already allocated seed "
        "range; a collision must be resolved as SEED PLAN REFORMULATE before "
        "any result is exposed, never after"
    )

GATE_B_LIMITATIONS = (
    "Gate B is a one-way viability filter against a deliberately weak passive "
    "comparator; it is not a strength confirmation and never promotes a Policy.",
    "The comparator is passive tsumogiri x3, not Q-v1, not BC, and not the "
    "Development Champion, so no comparison against any of those is claimed.",
    "Only the exact #158 P1 hybrid candidate is served; Q-v1 is not run as a "
    "paired ablation on this population, so no P1 causal effect is claimed.",
    "The candidate is a hybrid: decisions outside the eligible ordinary-discard "
    "and TRAIN-support-complete region fall back to the yakuhai-call scaffold, so "
    "a Gate B outcome describes the whole hybrid candidate, not the P1 feature.",
    "Single-round 4p-red-single games are not hanchan; no hanchan strength "
    "improvement is claimed.",
    "The seeds are a development-only population; this is not a formal holdout "
    "and not generalization evidence.",
    "Secondary Mahjong and serving diagnostics are reported for interpretation "
    "only and never enter the primary classification.",
)

INTERPRETATION_BOUNDARY = {
    "positive_claim_limit": (
        "the exact #158 P1 hybrid candidate showed a clear positive single-round "
        "score signal against the locked passive tsumogiri x3 Gate B baseline on "
        "fresh development seeds"
    ),
    "forbidden_claims": [
        "P1 is universally required",
        "P1 is better than Q-v1 in rollout",
        "P1 caused the Gate B advantage",
        "Q-v2 is strong",
        "the candidate beats the Development Champion",
        "hanchan strength improved",
        "formal generalization is established",
    ],
    "negative_claim_limit": (
        "negative evidence applies to this exact hybrid candidate against this "
        "locked weak comparator, not to P1 as a whole"
    ),
}

CLASSIFICATION_RULE = {
    "primary_metric": "seed-block candidate-vs-baseline score delta",
    "interval": "normal-approx 95% interval over seed blocks",
    "positive": "normal_approx_95_interval_lower > 0",
    "negative": "normal_approx_95_interval_upper < 0",
    "inconclusive": "the interval crosses zero",
    "secondary_metrics_may_alter_classification": False,
    "serving_diagnostics_may_alter_classification": False,
}


class P1GateBOutcome(Enum):
    """Issue #162がresult exposure前に固定したexhaustive outcome。"""

    POSITIVE_SIGNAL = "P1 GATE B POSITIVE SIGNAL"
    NEGATIVE_SIGNAL = "P1 GATE B NEGATIVE SIGNAL"
    INCONCLUSIVE = "P1 GATE B INCONCLUSIVE"
    EVIDENCE_BLOCKED = "P1 GATE B EVIDENCE BLOCKED"
    STOP_INVALID = "STOP / INVALID"


_RECORDABLE_OUTCOMES = (
    P1GateBOutcome.POSITIVE_SIGNAL,
    P1GateBOutcome.NEGATIVE_SIGNAL,
    P1GateBOutcome.INCONCLUSIVE,
)
_OUTCOME_VALUES = frozenset(outcome.value for outcome in P1GateBOutcome)


class P1GateBError(OfflineQError):
    """Gate Bのplan / execution / artifact / result契約の違反。"""


def _error(message: str) -> P1GateBError:
    return P1GateBError(message)


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --- Plan -----------------------------------------------------------------


def require_gate_b_seed(seed: int) -> int:
    """Gate Bで許されたfresh seedだけをfail closedで通す。"""
    if type(seed) is not int:
        raise TypeError("seed must be an int")
    if seed not in GATE_B_ORDERED_SEEDS:
        raise _error(f"seed {seed} is not part of the locked Gate B population")
    return seed


def plan_block() -> dict[str, object]:
    """result documentへ記録するlocked plan block。"""
    return {
        "ordered_seeds": list(GATE_B_ORDERED_SEEDS),
        "seed_block_count": GATE_B_SEED_BLOCK_COUNT,
        "rotation_count": GATE_B_ROTATIONS_PER_SEED,
        "game_count": GATE_B_GAME_COUNT,
        "game_mode": GATE_B_GAME_MODE,
        "max_workers": GATE_B_MAX_WORKERS,
        "role": GATE_B_ROLE,
        "formal_test": False,
    }


def build_gate_b_plan(
    checkpoint: LoadedP1ServingCheckpoint,
) -> tuple[SingleRoundEvaluationPlan, PolicyInstanceRegistry]:
    """exact P1 candidateとlocked comparatorでGate B planを組み立てる。

    candidate `PolicySpec.identity`はcheckpointのlogical candidate identityで
    あり、weights digest単独でもfree-form aliasでもない。
    """
    if not isinstance(checkpoint, LoadedP1ServingCheckpoint):
        raise TypeError("checkpoint must be a LoadedP1ServingCheckpoint")
    verify_locked_candidate_contract()
    for seed in GATE_B_ORDERED_SEEDS:
        require_gate_b_seed(seed)
    runtime = create_p1_hybrid_runtime(
        checkpoint.model, supported_indices=checkpoint.supported_indices
    )
    registry = PolicyInstanceRegistry(runtime.create_policy)
    candidate = PolicySpec(
        identity=checkpoint.candidate_identity, factory=registry.create_policy
    )
    plan = SingleRoundEvaluationPlan(
        candidate=candidate,
        baseline=passive_tsumogiri_spec(),
        seeds=GATE_B_ORDERED_SEEDS,
    )
    return plan, registry


# --- Artifact binding -----------------------------------------------------


def require_gate_b_artifact(
    artifact: SingleRoundStrengthArtifact, *, candidate_identity: str
) -> SingleRoundStrengthArtifact:
    """readbackしたartifactがGate B protocol条件を満たすことを確認する。

    partial run、seed mismatch、rotation mismatch、comparator mismatchは
    ここでfail closedする。
    """
    if not isinstance(artifact, SingleRoundStrengthArtifact):
        raise TypeError("artifact must be a SingleRoundStrengthArtifact")
    plan = artifact.plan
    if plan.seeds != GATE_B_ORDERED_SEEDS:
        raise _error("the artifact was not produced on the locked Gate B ordered seeds")
    if plan.game_mode != GATE_B_GAME_MODE:
        raise _error("the artifact game mode is not the locked Gate B game mode")
    if plan.rotation_count != GATE_B_ROTATIONS_PER_SEED:
        raise _error("the artifact rotation count is not the locked Gate B one")
    if len(artifact.game_results) != GATE_B_GAME_COUNT:
        raise _error(
            f"a Gate B artifact must contain exactly {GATE_B_GAME_COUNT} games; "
            f"got {len(artifact.game_results)} -- a partial run is never accepted"
        )
    if plan.candidate_identity != candidate_identity:
        raise _error("the artifact candidate identity is not the served candidate")
    if plan.baseline_identity != PASSIVE_TSUMOGIRI_IDENTITY:
        raise _error("the artifact baseline is not the locked Gate B comparator")
    seat_counts = {}
    for game_result in artifact.game_results:
        seat_counts[game_result.candidate_seat] = (
            seat_counts.get(game_result.candidate_seat, 0) + 1
        )
    if sorted(int(seat) for seat in seat_counts) != [0, 1, 2, 3] or set(
        seat_counts.values()
    ) != {GATE_B_SEED_BLOCK_COUNT}:
        raise _error(
            "the candidate did not occupy each seat exactly once per seed block"
        )
    return artifact


def artifact_block(
    artifact: SingleRoundStrengthArtifact, path: Path
) -> dict[str, object]:
    """immutable strength artifactへのidentity / referenceだけを記録する。

    strength measurementの正本はartifact fileそのものであり、この blockは
    その所在とexact bytes digestを指すだけである。
    """
    return {
        "schema_version": artifact.schema_version,
        "evaluation_protocol": artifact.evaluation_protocol,
        "filename": path.name,
        "sha256": _sha256_file(path),
        "game_count": len(artifact.game_results),
        "retention": {"backend": RETENTION_BACKEND, "key": ARTIFACT_RETENTION_KEY},
    }


# --- Result document ------------------------------------------------------


def candidate_block(checkpoint: LoadedP1ServingCheckpoint) -> dict[str, object]:
    """candidate logical identityとcanonical weights digestの両方を保持する。"""
    manifest = checkpoint.manifest
    return {
        "identity": checkpoint.candidate_identity,
        "binding": manifest["candidate_binding"],
        "canonical_model_weights_digest": (checkpoint.canonical_model_weights_digest),
        "checkpoint_schema_version": manifest["checkpoint_schema_version"],
        "materialization_source": manifest["materialization_source"],
        "selected_epoch": manifest["selected_epoch"],
        "source_dataset_identity": manifest["source_dataset_identity"],
        "supported_indices_digest": manifest["supported_indices_digest"],
        "expected_identities": manifest["expected_identities"],
        "real_candidate_materialization": (manifest["real_candidate_materialization"]),
        "retention": {"backend": RETENTION_BACKEND, "key": CANDIDATE_RETENTION_KEY},
    }


def serving_diagnostics_block(
    diagnostics: ActivationDiagnostics,
) -> dict[str, object]:
    """serving activation / fallback診断。classificationには使わない。

    illegal selection / non-finite output / resolve failureはserving path上で
    fail closedであり、1件でも起きればrunがabortしてresult documentは作られ
    ない。したがってvalid resultではこれらは常に0である。
    """
    return {
        **diagnostics.to_document(),
        "illegal_selection_count": 0,
        "non_finite_model_output_count": 0,
        "resolve_failure_count": 0,
        "fail_closed_at_decision_time": True,
    }


def result_identity(document: dict) -> str:
    """classificationとidentity自身を除いたcanonical bytesのdigest。

    outcomeを記録してもresult identityは変わらない（同じ実測の同じdocument
    である）。
    """
    payload = {**document, "classification": None, "result_identity": None}
    return hashlib.sha256(canonical_json_text(payload).encode("utf-8")).hexdigest()


def build_gate_b_result(
    *,
    checkpoint: LoadedP1ServingCheckpoint,
    artifact: SingleRoundStrengthArtifact,
    artifact_path: Path,
    summary,
    diagnostics: ActivationDiagnostics,
) -> dict[str, object]:
    """versioned Gate B result documentを組み立てる。

    `classification`は常に`None`で作られる。outcomeはvalidation後に
    `record_classification()`でだけ付与する。
    """
    if not isinstance(diagnostics, ActivationDiagnostics):
        raise TypeError("diagnostics must be an ActivationDiagnostics")
    document = {
        "p1_gate_b_schema_version": P1_GATE_B_SCHEMA_VERSION,
        "p1_gate_b_id": P1_GATE_B_ID,
        "source_issue": SOURCE_ISSUE,
        "predecessor_issues": list(PREDECESSOR_ISSUES),
        "parent_issue": PARENT_ISSUE,
        "protocol_id": PROTOCOL_ID,
        "candidate": candidate_block(checkpoint),
        "comparator": comparator_block(),
        "plan": plan_block(),
        "strength_artifact": artifact_block(artifact, artifact_path),
        "canonical_summary": summary_to_dict(summary),
        "serving_diagnostics": serving_diagnostics_block(diagnostics),
        "classification_rule": dict(CLASSIFICATION_RULE),
        "limitations": list(GATE_B_LIMITATIONS),
        "interpretation_boundary": {
            "positive_claim_limit": INTERPRETATION_BOUNDARY["positive_claim_limit"],
            "forbidden_claims": list(INTERPRETATION_BOUNDARY["forbidden_claims"]),
            "negative_claim_limit": INTERPRETATION_BOUNDARY["negative_claim_limit"],
        },
        "provenance": execution_provenance_to_dict(collect_execution_provenance()),
        "result_identity": None,
        "classification": None,
    }
    return {**document, "result_identity": result_identity(document)}


_RESULT_FIELDS = {
    "p1_gate_b_schema_version",
    "p1_gate_b_id",
    "source_issue",
    "predecessor_issues",
    "parent_issue",
    "protocol_id",
    "candidate",
    "comparator",
    "plan",
    "strength_artifact",
    "canonical_summary",
    "serving_diagnostics",
    "classification_rule",
    "limitations",
    "interpretation_boundary",
    "provenance",
    "result_identity",
    "classification",
}
_CANDIDATE_FIELDS = {
    "identity",
    "binding",
    "canonical_model_weights_digest",
    "checkpoint_schema_version",
    "materialization_source",
    "selected_epoch",
    "source_dataset_identity",
    "supported_indices_digest",
    "expected_identities",
    "real_candidate_materialization",
    "retention",
}
_EXPECTED_IDENTITY_FIELDS = set(LOCKED_P1_CANDIDATE.to_document())
_ARTIFACT_FIELDS = {
    "schema_version",
    "evaluation_protocol",
    "filename",
    "sha256",
    "game_count",
    "retention",
}
_DIAGNOSTIC_FIELDS = {
    "policy_instance_count",
    "total_decisions",
    "total_activations",
    "activation_rate",
    "total_scaffold_fallbacks",
    "scaffold_fallback_rate",
    "total_support_fallbacks",
    "support_fallback_rate",
    "illegal_selection_count",
    "non_finite_model_output_count",
    "resolve_failure_count",
    "fail_closed_at_decision_time",
}
_SEED_BLOCK_FIELDS = {
    "seed_block_count",
    "mean_seed_block_delta",
    "sample_standard_deviation",
    "standard_error",
    "normal_approx_95_interval_lower",
    "normal_approx_95_interval_upper",
    "positive_seed_block_count",
    "zero_seed_block_count",
    "negative_seed_block_count",
}


def _require_fields(value: object, fields: set[str], context: str) -> dict:
    if type(value) is not dict:
        raise _error(f"{context} must be an object")
    if set(value) != fields:
        missing = sorted(fields - set(value))
        extra = sorted(set(value) - fields)
        raise _error(f"{context} has missing {missing!r} or extra {extra!r} fields")
    return value


def _require_count(value: object, context: str) -> int:
    if type(value) is not int or value < 0:
        raise _error(f"{context} must be a non-negative int")
    return value


def _require_bool(value: object, context: str) -> bool:
    if type(value) is not bool:
        raise _error(f"{context} must be an exact bool")
    return value


def _require_number(value: object, context: str) -> float:
    if type(value) not in (int, float):
        raise _error(f"{context} must be a number")
    return float(value)


def _validate_real_candidate_materialization(candidate: dict) -> None:
    """`real_candidate_materialization`をidentityから再導出して照合する。

    checkpoint manifest（`p1_candidate.load_p1_serving_checkpoint()`）と同じ
    再導出規則をresult validation boundaryでも適用する。documentが宣言した
    flagをauthorityにしないため、checkpoint loaderを経由せず組み立てた
    result documentでも、fixture / substitute candidateを
    `real_candidate_materialization = true`として通すことはできない。

    ```text
    expected_identities == LOCKED_P1_CANDIDATE  ->  true
    otherwise                                   ->  false
    ```

    加えて、candidateが実際に記録しているdigestが`expected_identities`と
    exact一致することを要求する。したがって`true`は必ず「exact #158
    candidate identitiesである」ことを含意する。
    """
    expected = _require_fields(
        candidate["expected_identities"],
        _EXPECTED_IDENTITY_FIELDS,
        "candidate.expected_identities",
    )
    for name, value in expected.items():
        if type(value) is not str or len(value) != 64:
            raise _error(f"candidate.expected_identities.{name} is not a sha256 digest")
    for recorded, declared in (
        ("canonical_model_weights_digest", "canonical_model_weights_digest"),
        ("source_dataset_identity", "source_dataset_identity"),
        ("supported_indices_digest", "support_set_digest"),
    ):
        if candidate[recorded] != expected[declared]:
            raise _error(
                f"candidate.{recorded} does not match the expected identity it "
                "declares; the served candidate and the identity it claims must "
                "be the same candidate"
            )
    declared_real = _require_bool(
        candidate["real_candidate_materialization"],
        "candidate.real_candidate_materialization",
    )
    if declared_real is not (expected == LOCKED_P1_CANDIDATE.to_document()):
        raise _error(
            "real_candidate_materialization does not follow from the recorded "
            "candidate identities; it is derived from an exact comparison "
            "against the locked Issue #158 candidate, never self-declared -- a "
            "fixture or substitute candidate is never real Gate B evidence"
        )


def _validate_candidate(block: object) -> None:
    candidate = _require_fields(block, _CANDIDATE_FIELDS, "candidate")
    if candidate["checkpoint_schema_version"] != SERVING_CHECKPOINT_SCHEMA_VERSION:
        raise _error("the candidate checkpoint schema version is not the locked one")
    if candidate["selected_epoch"] != LOCKED_SELECTED_EPOCH:
        raise _error(
            "the served candidate is not the locked fixed_final_iteration epoch "
            f"{LOCKED_SELECTED_EPOCH} checkpoint"
        )
    if candidate["materialization_source"] not in MATERIALIZATION_SOURCES:
        raise _error("unknown candidate materialization source")
    digest = candidate["canonical_model_weights_digest"]
    support_digest = candidate["supported_indices_digest"]
    for name, value in (
        ("canonical_model_weights_digest", digest),
        ("supported_indices_digest", support_digest),
        ("source_dataset_identity", candidate["source_dataset_identity"]),
    ):
        if type(value) is not str or len(value) != 64:
            raise _error(f"candidate.{name} must be a 64 character sha256 digest")
    binding = candidate_binding_document(
        canonical_model_weights_digest=digest, support_set_digest=support_digest
    )
    if candidate["binding"] != binding:
        raise _error(
            "the recorded candidate binding document is not the one this "
            "candidate's weights, feature, vocabulary, support set, activation "
            "semantics and fallback Policy derive"
        )
    require_candidate_identity(candidate["identity"], binding)
    _validate_real_candidate_materialization(candidate)
    if candidate["retention"] != {
        "backend": RETENTION_BACKEND,
        "key": CANDIDATE_RETENTION_KEY,
    }:
        raise _error("the candidate retention target is not the locked one")


def _validate_artifact(block: object) -> None:
    artifact = _require_fields(block, _ARTIFACT_FIELDS, "strength_artifact")
    if artifact["schema_version"] != SINGLE_ROUND_ARTIFACT_SCHEMA_VERSION:
        raise _error("the strength artifact schema version is not the locked one")
    if artifact["evaluation_protocol"] != SINGLE_ROUND_EVALUATION_PROTOCOL:
        raise _error("the strength artifact evaluation protocol is not ABBB")
    if artifact["game_count"] != GATE_B_GAME_COUNT:
        raise _error("the strength artifact does not record the locked game count")
    sha256 = artifact["sha256"]
    if type(sha256) is not str or len(sha256) != 64:
        raise _error("strength_artifact.sha256 must be a 64 character sha256 digest")
    filename = artifact["filename"]
    if type(filename) is not str or not filename or "/" in filename:
        raise _error("strength_artifact.filename must be a bare file name")
    if artifact["retention"] != {
        "backend": RETENTION_BACKEND,
        "key": ARTIFACT_RETENTION_KEY,
    }:
        raise _error("the strength artifact retention target is not the locked one")


def _validate_summary(block: object) -> dict:
    if type(block) is not dict:
        raise _error("canonical_summary must be an object")
    if set(block) != {
        "candidate_metrics",
        "mean_baseline_score",
        "mean_candidate_game_delta",
        "seed_block_statistics",
    }:
        raise _error("canonical_summary is not the canonical aggregation shape")
    for name in ("mean_baseline_score", "mean_candidate_game_delta"):
        _require_number(block[name], f"canonical_summary.{name}")
    metrics = block["candidate_metrics"]
    if type(metrics) is not dict:
        raise _error("canonical_summary.candidate_metrics must be an object")
    if metrics.get("game_count") != GATE_B_GAME_COUNT:
        raise _error(
            "the canonical summary was not aggregated over the locked game count"
        )
    statistics = _require_fields(
        block["seed_block_statistics"], _SEED_BLOCK_FIELDS, "seed_block_statistics"
    )
    if statistics["seed_block_count"] != GATE_B_SEED_BLOCK_COUNT:
        raise _error(
            "the canonical summary was not aggregated over the locked seed blocks"
        )
    for name in (
        "normal_approx_95_interval_lower",
        "normal_approx_95_interval_upper",
    ):
        if statistics[name] is None:
            raise _error(
                "the Gate B primary metric requires a defined normal-approx 95% "
                "interval"
            )
        _require_number(statistics[name], f"seed_block_statistics.{name}")
    ordered = [
        _require_count(statistics[f"{name}_seed_block_count"], name)
        for name in ("positive", "zero", "negative")
    ]
    if sum(ordered) != GATE_B_SEED_BLOCK_COUNT:
        raise _error("the seed block sign counts do not partition the seed blocks")
    return statistics


def _validate_diagnostics(block: object) -> None:
    diagnostics = _require_fields(block, _DIAGNOSTIC_FIELDS, "serving_diagnostics")
    total = _require_count(
        diagnostics["total_decisions"], "serving_diagnostics.total_decisions"
    )
    if total == 0:
        raise _error("a Gate B run must produce at least one candidate decision")
    _require_count(
        diagnostics["policy_instance_count"],
        "serving_diagnostics.policy_instance_count",
    )
    parts = [
        _require_count(diagnostics[name], f"serving_diagnostics.{name}")
        for name in (
            "total_activations",
            "total_scaffold_fallbacks",
            "total_support_fallbacks",
        )
    ]
    if sum(parts) != total:
        raise _error(
            "activation, scaffold fallback and support fallback counts do not "
            "partition the candidate decisions"
        )
    for name, count in zip(
        ("activation_rate", "scaffold_fallback_rate", "support_fallback_rate"),
        parts,
        strict=True,
    ):
        if diagnostics[name] != count / total:
            raise _error(f"serving_diagnostics.{name} is not derivable from counts")
    for name in (
        "illegal_selection_count",
        "non_finite_model_output_count",
        "resolve_failure_count",
    ):
        if diagnostics[name] != 0:
            raise _error(
                f"serving_diagnostics.{name} must be 0; any occurrence fails the "
                "run closed before a Gate B result exists"
            )
    if (
        _require_bool(
            diagnostics["fail_closed_at_decision_time"],
            "serving_diagnostics.fail_closed_at_decision_time",
        )
        is not True
    ):
        raise _error("Gate B serving is fail closed at decision time")


def validate_gate_b_result(document: object) -> dict:
    """Gate B result documentをlocked constantと再導出でfail closedに検証する。"""
    validated = _require_fields(document, _RESULT_FIELDS, "Gate B result")
    for name, expected in (
        ("p1_gate_b_schema_version", P1_GATE_B_SCHEMA_VERSION),
        ("p1_gate_b_id", P1_GATE_B_ID),
        ("source_issue", SOURCE_ISSUE),
        ("predecessor_issues", list(PREDECESSOR_ISSUES)),
        ("parent_issue", PARENT_ISSUE),
        ("protocol_id", PROTOCOL_ID),
        ("comparator", comparator_block()),
        ("plan", plan_block()),
        ("classification_rule", dict(CLASSIFICATION_RULE)),
        ("limitations", list(GATE_B_LIMITATIONS)),
        (
            "interpretation_boundary",
            {
                "positive_claim_limit": INTERPRETATION_BOUNDARY["positive_claim_limit"],
                "forbidden_claims": list(INTERPRETATION_BOUNDARY["forbidden_claims"]),
                "negative_claim_limit": INTERPRETATION_BOUNDARY["negative_claim_limit"],
            },
        ),
    ):
        if validated[name] != expected:
            raise _error(f"Gate B result {name} is not the locked one")
    _validate_candidate(validated["candidate"])
    _validate_artifact(validated["strength_artifact"])
    _validate_summary(validated["canonical_summary"])
    _validate_diagnostics(validated["serving_diagnostics"])
    parse_execution_provenance(validated["provenance"])
    if validated["result_identity"] != result_identity(validated):
        raise _error(
            "result_identity is not the digest of this result document; the "
            "recorded measurement was edited after the fact"
        )
    classification = validated["classification"]
    if classification is not None:
        if classification not in _OUTCOME_VALUES:
            raise _error(f"unknown Gate B outcome: {classification!r}")
        if classification != derive_classification(validated).value:
            raise _error(
                "the recorded classification is not the outcome that the locked "
                "Issue #162 rule derives from the canonical interval"
            )
    return validated


def classify_interval(lower: float, upper: float) -> P1GateBOutcome:
    """lockedなone-way viability ruleをcanonical intervalへ適用する。"""
    if lower > 0:
        return P1GateBOutcome.POSITIVE_SIGNAL
    if upper < 0:
        return P1GateBOutcome.NEGATIVE_SIGNAL
    return P1GateBOutcome.INCONCLUSIVE


def derive_classification(document: dict) -> P1GateBOutcome:
    """result documentのcanonical intervalからoutcomeを機械的に導出する。

    secondary metricsとserving diagnosticsはここを一切通らない。
    `P1 GATE B EVIDENCE BLOCKED`と`STOP / INVALID`はresult documentが作られる
    前のpre-result stateであり、ここからは導出されない。
    """
    statistics = document["canonical_summary"]["seed_block_statistics"]
    return classify_interval(
        statistics["normal_approx_95_interval_lower"],
        statistics["normal_approx_95_interval_upper"],
    )


def record_classification(document: dict, outcome: P1GateBOutcome) -> dict:
    """validated resultへexhaustive outcomeを1件だけ記録する。"""
    validated = validate_gate_b_result(document)
    if not isinstance(outcome, P1GateBOutcome):
        raise TypeError("outcome must be a P1GateBOutcome")
    if validated["classification"] is not None:
        raise _error("this Gate B result already records an outcome")
    if validated["candidate"]["real_candidate_materialization"] is not True:
        raise _error(
            "a Gate B outcome may only be recorded for the exact retained #158 "
            "candidate; a fixture or substitute candidate is never real Gate B "
            "evidence"
        )
    if outcome not in _RECORDABLE_OUTCOMES:
        raise _error(
            f"{outcome.value!r} is a pre-result state, not an outcome derived "
            "from a valid Gate B execution"
        )
    derived = derive_classification(validated)
    if outcome is not derived:
        raise _error(
            f"the locked Issue #162 rule derives {derived.value!r} from this "
            f"canonical interval, not {outcome.value!r}"
        )
    return validate_gate_b_result({**validated, "classification": outcome.value})


# --- Execution ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GateBMeasurement:
    """1回のGate B executionのimmutable artifactと再生成したcanonical summary。"""

    artifact: SingleRoundStrengthArtifact
    summary: object
    document: dict
    derived_outcome: P1GateBOutcome
    diagnostics: ActivationDiagnostics
    wall_clock_seconds: float
    cpu_seconds: float


def run_gate_b(
    checkpoint: LoadedP1ServingCheckpoint,
    artifact_path: str | Path,
    *,
    progress_callback=None,
) -> GateBMeasurement:
    """Gate Bを1回実行し、artifactを保存してから読み直して検証する。

    新しいgame runner / rotation / aggregationを作らず、既存
    `run_single_round_evaluation()`をserial（workers = 1）でthin reuseする。
    """
    artifact_path = Path(artifact_path)
    plan, registry = build_gate_b_plan(checkpoint)

    wall_started = time.perf_counter()
    cpu_started = time.process_time()
    result = run_single_round_evaluation(plan, progress_callback=progress_callback)
    wall_clock_seconds = time.perf_counter() - wall_started
    cpu_seconds = time.process_time() - cpu_started

    save_single_round_artifact(result, artifact_path)
    artifact = load_single_round_artifact(artifact_path)
    require_gate_b_artifact(artifact, candidate_identity=checkpoint.candidate_identity)
    summary = summarize_single_round_strength(
        aggregate_candidate_metrics(
            artifact.plan.candidate_identity, artifact.game_results
        ),
        artifact.game_results,
    )
    if summary != artifact.summary:
        raise _error(
            "the canonical summary regenerated from raw results differs from the "
            "stored artifact summary"
        )
    diagnostics = collect_activation_diagnostics(registry.instances)
    document = validate_gate_b_result(
        build_gate_b_result(
            checkpoint=checkpoint,
            artifact=artifact,
            artifact_path=artifact_path,
            summary=summary,
            diagnostics=diagnostics,
        )
    )
    return GateBMeasurement(
        artifact=artifact,
        summary=summary,
        document=document,
        derived_outcome=derive_classification(document),
        diagnostics=diagnostics,
        wall_clock_seconds=wall_clock_seconds,
        cpu_seconds=cpu_seconds,
    )


__all__ = [
    "ARTIFACT_RETENTION_KEY",
    "CANDIDATE_RETENTION_KEY",
    "CLASSIFICATION_RULE",
    "GATE_B_GAME_COUNT",
    "GATE_B_GAME_MODE",
    "GATE_B_LIMITATIONS",
    "GATE_B_MAX_WORKERS",
    "GATE_B_ORDERED_SEEDS",
    "GATE_B_ROLE",
    "GATE_B_ROTATIONS_PER_SEED",
    "GATE_B_SEED_BLOCK_COUNT",
    "INTERPRETATION_BOUNDARY",
    "PARENT_ISSUE",
    "PREDECESSOR_ISSUES",
    "P1_GATE_B_ID",
    "P1_GATE_B_SCHEMA_VERSION",
    "RETENTION_BACKEND",
    "SOURCE_ISSUE",
    "GateBMeasurement",
    "P1GateBError",
    "P1GateBOutcome",
    "artifact_block",
    "build_gate_b_plan",
    "build_gate_b_result",
    "candidate_block",
    "classify_interval",
    "derive_classification",
    "plan_block",
    "record_classification",
    "require_gate_b_artifact",
    "require_gate_b_seed",
    "result_identity",
    "run_gate_b",
    "serving_diagnostics_block",
    "validate_gate_b_result",
]
