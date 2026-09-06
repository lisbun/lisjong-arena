"""P1 Gate A — keep-shanten discard feature bounded evaluation (Issue #158).

#152は`HAND-PROGRESSION DEGRADATION IDENTIFIED`として、同一observable stateで
current QがBC / behaviorよりpost-discard shantenを悪化させるdiscardへ系統的に
偏ることを確認した。Issue #158はそれに対して、**deterministic Mahjong featureを
ちょうど1 family**（keep-shanten discard 37-tile mask）だけ追加したQ-v2を
retained artifact上で学習し、hand-progression degradationが改善するかを
boundedに検証する。

```text
retained candidate-pair bundle     dataset artifact       replacement TEST artifact
  bc-checkpoint  17a31fc8...         69094c1b...             fe7a4455...
  q-checkpoint   31545d6b...
        |                                 |                          |
        +---------------- strict readback (#152 binding) ------------+
                                     |
                                     v
                     P1 derived view (8241 = 8204 + 37)
                     row order / split membership / behavior /
                     reward / terminal / legal maskは不変
                                     |
                     +---------------+---------------+
                     v                               v
             P1 Q-v2 training                 Gate A evaluation
             （#140と同一のtraining          dataset TEST / replacement TEST
               semantics、input dim         を primary role として、
               だけが8204 -> 8241）          Q-v2 / retained Q-v1 / BC /
                                             behaviorを同一row上で比較
                                     |
                                     v
                       build_gate_a_result()  versioned JSON document
                                     v
                       validate_gate_a_result()
                                     v
                       derive_classification()  exhaustive outcome
```

## このmoduleが意図的に**しない**こと

- 新しいgame生成、seed割り当て、hanchan、新しいTEST exposure
- reward / gamma / target cadence / support semantics / architecture familyの変更
- strength / hanchan improvement claim
- 結果を見てからのthreshold / role / feature familyの変更

Issue #158のoutcome ladderは#152と違い**数値からdeterministicに決まる**ため、
`derive_classification()`が事前lockされた条件から機械的に導出する。
`record_classification()`はその導出結果と一致するoutcomeだけを記録できる。
"""

from dataclasses import dataclass
from enum import Enum

from .artifact import LoadedOfflineQDataset, feature_block, vocabulary_block
from .diagnosis import (
    FIXED_QUANTILES,
    LOCKED_SOURCE_IDENTITIES,
    RETENTION_BACKEND,
    RETENTION_KEY,
    DiagnosisInputBinding,
    ExpectedArtifactIdentities,
    bind_diagnosis_inputs,
    hand_progression_arm_summary,
    hand_progression_pair_summary,
    rate,
    require_finite,
    select_eligible_rows,
)
from .errors import OfflineQAmbiguousStateError, OfflineQDiagnosisError
from .hand_progression import (
    HandProgression,
    MeasurementAvailability,
    hand_progression_for_row,
)
from .p1_features import P1_FEATURE_DIMENSION, P1DerivationCoverage, p1_feature_block
from .p1_q_training import (
    p1_model_block,
    p1_training_block,
    verify_locked_q_protocol_delta,
)
from .protocol import (
    FEATURE_DIMENSION,
    PROTOCOL_ID,
    VOCABULARY_SIZE,
    Split,
    verify_contract_identity,
)

P1_GATE_A_SCHEMA_VERSION = "arena-learned-policy-offlineq-p1-gate-a-v1"
P1_GATE_A_ID = "arena-learned-policy-offlineq-p1-keep-shanten-gate-a-158"
SOURCE_ISSUE = "lisbun/lisjong-arena#158"
PREDECESSOR_ISSUES = ("lisbun/lisjong-arena#140", "lisbun/lisjong-arena#152")
PARENT_ISSUE = "lisbun/lisjong-project#45"

CHANGED_AXIS = (
    "deterministic keep-shanten discard 37-tile feature family",
    "the first-layer parameter increase that follows from the input dimension",
)
"""Issue #158が変更を許したprimary axis。これ以外は#140 / #152から固定する。"""

GENERATION_BUDGET = {
    "new_game_generation": 0,
    "new_seed_allocation": 0,
    "new_hanchan": 0,
    "new_test_exposure": 0,
}
"""本Issueのcost guardrail。derived viewはretained rowsからのみ作る。"""

P1_GATE_A_LIMITATIONS = (
    "Gate A is an offline decision comparison on retained behavior-distribution "
    "rows; it observes no rollout distribution and is not a strength or hanchan "
    "improvement claim.",
    "dataset TEST 271..276 and replacement TEST 354..359 were already exposed in "
    "#140 / #152; they are reused here as the locked Gate A rows and are not a new "
    "TEST claim or new generalization evidence.",
    "TRAIN and VALIDATION roles are training-distribution diagnostics and are never "
    "the source of a positive claim.",
    "Only the keep-shanten discard feature family changed; no ukeire, waits, value, "
    "defense, HandBelief, or current-shanten feature was added, and reward, gamma, "
    "target cadence, support restriction, loss, optimizer, hidden width, activation, "
    "seeds, deterministic settings, and checkpoint selection are the #140 values.",
    "Rows are not independent samples: several rows come from the same hanchan and "
    "the same hand, so per-seed blocks are reported descriptively and no p-value or "
    "confidence interval is claimed.",
    "No new hanchan, seeds, training data, or TEST exposure were produced; every row "
    "is derived from artifacts retained by #140.",
)

INTERPRETATION_BOUNDARY = {
    "positive_claim_limit": (
        "explicit keep-shanten structure helped this bounded simple Offline Q "
        "formulation preserve hand progression on retained Gate A evidence"
    ),
    "forbidden_claims": [
        "P1 is universally required",
        "Q is now strong",
        "hanchan strength improved",
        "Mortal-like representation is proven superior",
    ],
    "negative_claim_limit": (
        "negative evidence applies to the keep-shanten-only formulation, not to P1 "
        "as a whole"
    ),
}


class P1GateAOutcome(Enum):
    """Issue #158がresult exposure前に固定したexhaustive outcome。"""

    HAND_PROGRESSION_SIGNAL = "P1 HAND-PROGRESSION SIGNAL"
    HAND_PROGRESSION_REGRESSION = "P1 HAND-PROGRESSION REGRESSION"
    HAND_PROGRESSION_INCONCLUSIVE = "P1 HAND-PROGRESSION INCONCLUSIVE"
    EVIDENCE_INSUFFICIENT = "P1 EVIDENCE INSUFFICIENT"
    STOP_INVALID = "STOP / INVALID"


class P1GateARole(Enum):
    """Gate Aを走らせるrow populationのrole。"""

    DATASET_TRAIN = "dataset-train"
    DATASET_VALIDATION = "dataset-validation"
    DATASET_TEST = "dataset-test"
    REPLACEMENT_TEST = "replacement-test"


PRIMARY_ROLES = (P1GateARole.DATASET_TEST, P1GateARole.REPLACEMENT_TEST)
"""positive / negative claimの正本となるroleはこの2つだけである。"""

_ROLE_SPLIT = {
    P1GateARole.DATASET_TRAIN: Split.TRAIN,
    P1GateARole.DATASET_VALIDATION: Split.VALIDATION,
    P1GateARole.DATASET_TEST: Split.TEST,
    P1GateARole.REPLACEMENT_TEST: Split.TEST,
}
_ROLE_SOURCE_ARTIFACT = {
    P1GateARole.DATASET_TRAIN: "dataset",
    P1GateARole.DATASET_VALIDATION: "dataset",
    P1GateARole.DATASET_TEST: "dataset",
    P1GateARole.REPLACEMENT_TEST: "replacement-test",
}

ARMS = ("q_v2", "q_v1", "bc", "behavior")
"""同一row上で比較する4 arm。`q_v2`がP1 candidate、`q_v1`がretained Qである。"""

PAIRS = (
    "q_v2_vs_q_v1",
    "q_v2_vs_bc",
    "q_v2_vs_behavior",
    "q_v1_vs_bc",
    "q_v1_vs_behavior",
)
AGREEMENT_PAIRS = (*PAIRS, "bc_vs_behavior")


def _error(message: str) -> OfflineQDiagnosisError:
    return OfflineQDiagnosisError(message)


# --- Role populations -----------------------------------------------------


@dataclass(frozen=True, slots=True)
class P1RolePopulation:
    """1 roleのv1 tensor / P1 derived tensor / row recordの組。

    順序はartifact順で固定する。`tensors`と`p1_tensors`は同じrowを同じ順序で
    指し、featureのbase 8204列は完全に一致していなければならない。
    """

    role: P1GateARole
    tensors: object
    p1_tensors: object
    rows: tuple
    coverage: P1DerivationCoverage

    def __post_init__(self) -> None:
        if not isinstance(self.role, P1GateARole):
            raise TypeError("role must be a P1GateARole")
        if not isinstance(self.coverage, P1DerivationCoverage):
            raise TypeError("coverage must be a P1DerivationCoverage")
        row_count = self.tensors.row_count
        if len(self.rows) != row_count or self.p1_tensors.row_count != row_count:
            raise _error("row records and tensors describe different row counts")
        if self.coverage.row_count != row_count:
            raise _error("derived coverage describes a different row count")


def verify_derived_alignment(population: P1RolePopulation) -> None:
    """P1 tensorがbase v1 tensorをverbatimに保持していることを確認する。

    derived viewはbase valueを書き換えない。row identity / order / legal mask /
    behavior / reward / terminalも同一でなければならない。
    """
    import torch

    base = population.tensors
    derived = population.p1_tensors
    if int(derived.features.shape[1]) != P1_FEATURE_DIMENSION:
        raise _error("the derived features are not the P1 schema dimension")
    if int(base.features.shape[1]) != FEATURE_DIMENSION:
        raise _error("the base features are not the locked v1 schema dimension")
    if not torch.equal(derived.features[:, :FEATURE_DIMENSION], base.features):
        raise _error("the derived view rewrote base v1 source feature values")
    if not torch.equal(
        derived.next_features[:, :FEATURE_DIMENSION], base.next_features
    ):
        raise _error("the derived view rewrote base v1 next feature values")
    terminal = base.terminal
    if bool(terminal.any()) and bool(
        derived.next_features[terminal][:, FEATURE_DIMENSION:].any()
    ):
        raise _error(
            "a terminal next-state placeholder carries a non-zero derived block; "
            "the locked contract keeps that placeholder all-zero"
        )
    for name in ("legal_mask", "next_legal_mask", "behavior_action_index", "terminal"):
        if not torch.equal(getattr(derived, name), getattr(base, name)):
            raise _error(f"the derived view changed {name}")
    if not torch.equal(derived.reward, base.reward):
        raise _error("the derived view changed reward")


# --- Measurement ----------------------------------------------------------


def _model_outputs(model, features, batch_size: int):
    import torch

    outputs = []
    with torch.inference_mode():
        for start in range(0, int(features.shape[0]), batch_size):
            outputs.append(model(features[start : start + batch_size]).clone())
    if not outputs:
        return torch.zeros((0, VOCABULARY_SIZE), dtype=torch.float32)
    return torch.cat(outputs, dim=0)


def action_agreement(selections: dict[str, list[int]], row_count: int) -> dict:
    """4 arm間のtop-1 action disagreement countとrateを返す。"""
    document: dict[str, object] = {"eligible_row_count": row_count}
    for pair in AGREEMENT_PAIRS:
        left, right = pair.split("_vs_")
        count = sum(
            1
            for one, other in zip(selections[left], selections[right], strict=True)
            if one != other
        )
        document[f"{pair}_disagreement_count"] = count
        document[f"{pair}_disagreement_rate"] = rate(count, row_count)
    return document


def _per_seed_blocks(
    rows, progressions: dict[str, list[HandProgression]]
) -> list[dict]:
    """seed（hanchan）単位のpaired diagnostic。p-value claimは行わない。"""
    blocks: dict[int, dict[str, int]] = {}
    for position, row in enumerate(rows):
        entry = blocks.setdefault(
            row.seed,
            {
                "row_count": 0,
                "q_v2_worsen_shanten_count": 0,
                "q_v1_worsen_shanten_count": 0,
                "lower_post_discard_shanten_count": 0,
                "equal_post_discard_shanten_count": 0,
                "higher_post_discard_shanten_count": 0,
            },
        )
        entry["row_count"] += 1
        candidate = progressions["q_v2"][position]
        retained = progressions["q_v1"][position]
        entry["q_v2_worsen_shanten_count"] += int(candidate.worsens_shanten)
        entry["q_v1_worsen_shanten_count"] += int(retained.worsens_shanten)
        if candidate.post_discard_shanten < retained.post_discard_shanten:
            entry["lower_post_discard_shanten_count"] += 1
        elif candidate.post_discard_shanten == retained.post_discard_shanten:
            entry["equal_post_discard_shanten_count"] += 1
        else:
            entry["higher_post_discard_shanten_count"] += 1
    return [{"seed": seed, **counts} for seed, counts in sorted(blocks.items())]


def outcome_conditions(arms: dict, pairs: dict) -> dict[str, object]:
    """事前lockされたsignal / regression条件を、metricsから機械的に導出する。"""
    candidate_worsen = arms["q_v2"]["worsen_shanten_rate"]
    retained_worsen = arms["q_v1"]["worsen_shanten_rate"]
    paired = pairs["q_v2_vs_q_v1"]
    lower = paired["lower_post_discard_shanten_count"]
    higher = paired["higher_post_discard_shanten_count"]
    candidate_gap = abs(pairs["q_v2_vs_bc"]["worsen_shanten_rate_difference"])
    retained_gap = abs(pairs["q_v1_vs_bc"]["worsen_shanten_rate_difference"])

    signal = {
        "worsen_shanten_rate_reduced": candidate_worsen < retained_worsen,
        "paired_lower_exceeds_higher": lower > higher,
        "bc_worsen_rate_gap_narrowed": candidate_gap < retained_gap,
    }
    regression = {
        "worsen_shanten_rate_increased": candidate_worsen > retained_worsen,
        "paired_higher_exceeds_lower": higher > lower,
        "bc_worsen_rate_gap_widened": candidate_gap > retained_gap,
    }
    return {
        "q_v2_worsen_shanten_rate": candidate_worsen,
        "q_v1_worsen_shanten_rate": retained_worsen,
        "paired_lower_post_discard_shanten_count": lower,
        "paired_higher_post_discard_shanten_count": higher,
        "q_v2_vs_bc_worsen_rate_gap": candidate_gap,
        "q_v1_vs_bc_worsen_rate_gap": retained_gap,
        "signal_conditions": signal,
        "regression_conditions": regression,
        "signal": all(signal.values()),
        "regression": all(regression.values()),
    }


def hand_progression_block(features, rows, selections: dict[str, list[int]]) -> dict:
    """4 armのpost-discard shantenを、同じrowから同じcurrent stateで導出する。

    1 rowでもambiguousに復元される場合、推測で埋めずrole全体を`UNAVAILABLE`と
    する。featureはbase v1 rowを渡す（derived blockはここでは読まない）。

    eligible rowが1つも無いroleも`UNAVAILABLE`にする。rateも paired比較も母数0では
    定義できず、ladder条件をNoneから捏造しないためである。
    """
    if not rows:
        return {
            "status": MeasurementAvailability.UNAVAILABLE.value,
            "unavailable_reason": (
                "the role has no eligible ordinary-discard choice row; no rate or "
                "paired comparison is defined on an empty population"
            ),
            "arms": None,
            "pairs": None,
            "per_seed": None,
            "outcome_conditions": None,
        }
    progressions: dict[str, list[HandProgression]] = {arm: [] for arm in ARMS}
    try:
        for position in range(len(rows)):
            derived = hand_progression_for_row(
                features[position],
                tuple(selections[arm][position] for arm in ARMS),
            )
            for arm, entry in zip(ARMS, derived, strict=True):
                progressions[arm].append(entry)
    except OfflineQAmbiguousStateError as error:
        return {
            "status": MeasurementAvailability.UNAVAILABLE.value,
            "unavailable_reason": str(error),
            "arms": None,
            "pairs": None,
            "per_seed": None,
            "outcome_conditions": None,
        }

    arms = {arm: hand_progression_arm_summary(progressions[arm]) for arm in ARMS}
    pairs = {}
    for pair in PAIRS:
        left, right = pair.split("_vs_")
        pairs[pair] = hand_progression_pair_summary(
            progressions[left], progressions[right]
        )
    return {
        "status": MeasurementAvailability.AVAILABLE.value,
        "unavailable_reason": None,
        "arms": arms,
        "pairs": pairs,
        "per_seed": _per_seed_blocks(rows, progressions),
        "outcome_conditions": outcome_conditions(arms, pairs),
    }


def evaluate_role(
    population: P1RolePopulation,
    *,
    q_v1_model,
    q_v2_model,
    bc_model,
    support_mask,
    batch_size: int = 256,
) -> dict[str, object]:
    """1 roleについて4 armを同一row上で比較し、role documentを返す。"""
    import torch

    from lisjong_arena.learned_policy_stage2.network import masked_argmax

    from .q_network import masked_argmax_q

    verify_derived_alignment(population)
    tensors = population.tensors
    eligible, counts = select_eligible_rows(tensors, support_mask)
    selector = torch.nonzero(eligible).flatten()
    rows = tuple(
        row
        for row, keep in zip(population.rows, eligible.tolist(), strict=True)
        if keep
    )

    features = tensors.features.index_select(0, selector)
    p1_features = population.p1_tensors.features.index_select(0, selector)
    legal_mask = tensors.legal_mask.index_select(0, selector)
    behavior_index = tensors.behavior_action_index.index_select(0, selector)

    q_v1_values = _model_outputs(q_v1_model, features, batch_size)
    q_v2_values = _model_outputs(q_v2_model, p1_features, batch_size)
    bc_logits = _model_outputs(bc_model, features, batch_size)
    require_finite(q_v1_values, "retained Q-v1 output")
    require_finite(q_v2_values, "P1 Q-v2 output")
    require_finite(bc_logits, "retained BC output")

    selections = {
        "q_v2": masked_argmax_q(q_v2_values, legal_mask).tolist(),
        "q_v1": masked_argmax_q(q_v1_values, legal_mask).tolist(),
        "bc": masked_argmax(bc_logits, legal_mask).tolist(),
        "behavior": behavior_index.tolist(),
    }
    row_count = len(rows)
    return {
        "role": population.role.value,
        "source_artifact": _ROLE_SOURCE_ARTIFACT[population.role],
        "split": _ROLE_SPLIT[population.role].value,
        "is_primary_role": population.role in PRIMARY_ROLES,
        "is_generalization_evidence": False,
        "row_counts": counts.to_document(),
        "derived_coverage": population.coverage.to_document(),
        "action_agreement": action_agreement(selections, row_count),
        "hand_progression": hand_progression_block(features, rows, selections),
    }


# --- Result artifact ------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CandidateModelRecord:
    """学習したP1 Q-v2 modelのidentityとtraining history。"""

    weights_digest: str
    selected_epoch: int
    final_validation_huber_loss: float
    epoch_history: tuple
    supported_indices_digest: str

    def to_document(self) -> dict[str, object]:
        return {
            "model": p1_model_block(),
            "training": p1_training_block(),
            "weights_digest": self.weights_digest,
            "selected_epoch": self.selected_epoch,
            "final_validation_huber_loss": self.final_validation_huber_loss,
            "epoch_history": [record.to_document() for record in self.epoch_history],
            "supported_indices_digest": self.supported_indices_digest,
        }


def build_gate_a_result(
    *,
    binding: DiagnosisInputBinding,
    candidate: CandidateModelRecord,
    roles: list[dict],
) -> dict[str, object]:
    """versioned Gate A result documentを組み立てる。

    `classification`は常に`None`で作られる。outcomeはvalidation後に
    `record_classification()`でだけ付与する。
    """
    if not isinstance(binding, DiagnosisInputBinding):
        raise TypeError("binding must be a DiagnosisInputBinding")
    if not isinstance(candidate, CandidateModelRecord):
        raise TypeError("candidate must be a CandidateModelRecord")
    if not roles:
        raise _error("a Gate A result must contain at least one role")
    verify_locked_q_protocol_delta()
    return {
        "p1_gate_a_schema_version": P1_GATE_A_SCHEMA_VERSION,
        "p1_gate_a_id": P1_GATE_A_ID,
        "source_issue": SOURCE_ISSUE,
        "predecessor_issues": list(PREDECESSOR_ISSUES),
        "parent_issue": PARENT_ISSUE,
        "protocol_id": PROTOCOL_ID,
        "retention": {"backend": RETENTION_BACKEND, "key": RETENTION_KEY},
        "input_artifact_identities": binding.to_document(),
        "locked_source_identities": LOCKED_SOURCE_IDENTITIES.to_document(),
        "feature": feature_block(),
        "derived_feature": p1_feature_block(),
        "vocabulary": vocabulary_block(),
        "candidate": candidate.to_document(),
        "changed_axis": list(CHANGED_AXIS),
        "generation_budget": dict(GENERATION_BUDGET),
        "fixed_quantiles": [format(value, "g") for value in FIXED_QUANTILES],
        "primary_roles": [role.value for role in PRIMARY_ROLES],
        "roles": list(roles),
        "limitations": list(P1_GATE_A_LIMITATIONS),
        "interpretation_boundary": {
            "positive_claim_limit": INTERPRETATION_BOUNDARY["positive_claim_limit"],
            "forbidden_claims": list(INTERPRETATION_BOUNDARY["forbidden_claims"]),
            "negative_claim_limit": INTERPRETATION_BOUNDARY["negative_claim_limit"],
        },
        "classification": None,
    }


_RESULT_FIELDS = {
    "p1_gate_a_schema_version",
    "p1_gate_a_id",
    "source_issue",
    "predecessor_issues",
    "parent_issue",
    "protocol_id",
    "retention",
    "input_artifact_identities",
    "locked_source_identities",
    "feature",
    "derived_feature",
    "vocabulary",
    "candidate",
    "changed_axis",
    "generation_budget",
    "fixed_quantiles",
    "primary_roles",
    "roles",
    "limitations",
    "interpretation_boundary",
    "classification",
}
_ROLE_FIELDS = {
    "role",
    "source_artifact",
    "split",
    "is_primary_role",
    "is_generalization_evidence",
    "row_counts",
    "derived_coverage",
    "action_agreement",
    "hand_progression",
}
_ROW_COUNT_FIELDS = {
    "total_row_count",
    "choice_row_count",
    "ordinary_discard_row_count",
    "support_complete_row_count",
    "eligible_row_count",
    "excluded_row_count",
}
_COVERAGE_FIELDS = {
    "row_count",
    "source_rows_derived",
    "nonterminal_next_rows_derived",
    "terminal_next_rows_zero_padded",
    "imputed_row_count",
}
_AGREEMENT_FIELDS = {
    "eligible_row_count",
    *(
        f"{pair}_disagreement_{suffix}"
        for pair in AGREEMENT_PAIRS
        for suffix in ("count", "rate")
    ),
}
_HAND_PROGRESSION_FIELDS = {
    "status",
    "unavailable_reason",
    "arms",
    "pairs",
    "per_seed",
    "outcome_conditions",
}
_ARM_FIELDS = {
    "row_count",
    "post_discard_shanten",
    "keep_shanten_count",
    "keep_shanten_rate",
    "worsen_shanten_count",
    "worsen_shanten_rate",
}
_PAIR_FIELDS = {
    "row_count",
    "lower_post_discard_shanten_count",
    "equal_post_discard_shanten_count",
    "higher_post_discard_shanten_count",
    "higher_post_discard_shanten_rate",
    "worsen_shanten_rate_difference",
}
_PER_SEED_FIELDS = {
    "seed",
    "row_count",
    "q_v2_worsen_shanten_count",
    "q_v1_worsen_shanten_count",
    "lower_post_discard_shanten_count",
    "equal_post_discard_shanten_count",
    "higher_post_discard_shanten_count",
}
_CONDITION_FIELDS = {
    "q_v2_worsen_shanten_rate",
    "q_v1_worsen_shanten_rate",
    "paired_lower_post_discard_shanten_count",
    "paired_higher_post_discard_shanten_count",
    "q_v2_vs_bc_worsen_rate_gap",
    "q_v1_vs_bc_worsen_rate_gap",
    "signal_conditions",
    "regression_conditions",
    "signal",
    "regression",
}
_SUMMARY_FIELDS = {"count", "mean", "quantiles"}
_QUANTILE_KEYS = frozenset(format(value, "g") for value in FIXED_QUANTILES)
_CANDIDATE_FIELDS = {
    "model",
    "training",
    "weights_digest",
    "selected_epoch",
    "final_validation_huber_loss",
    "epoch_history",
    "supported_indices_digest",
}
_IDENTITY_FIELDS = tuple(LOCKED_SOURCE_IDENTITIES.to_document())
_INPUT_IDENTITY_FIELDS = {*_IDENTITY_FIELDS, "real_artifact_execution"}
_AVAILABILITY_VALUES = frozenset(status.value for status in MeasurementAvailability)
_OUTCOME_VALUES = frozenset(outcome.value for outcome in P1GateAOutcome)


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


def _validate_summary(value: object, context: str) -> int:
    summary = _require_fields(value, _SUMMARY_FIELDS, context)
    count = _require_count(summary["count"], f"{context}.count")
    if count == 0:
        if summary["mean"] is not None or summary["quantiles"] is not None:
            raise _error(f"{context} has no rows but carries fabricated values")
        return 0
    if type(summary["mean"]) not in (int, float):
        raise _error(f"{context}.mean must be a number")
    quantiles = summary["quantiles"]
    if type(quantiles) is not dict or set(quantiles) != _QUANTILE_KEYS:
        raise _error(f"{context}.quantiles is not the locked quantile set")
    for key, item in quantiles.items():
        if type(item) not in (int, float):
            raise _error(f"{context}.quantiles[{key!r}] must be a number")
    return count


def _validate_input_identities(document: dict) -> None:
    """observed identityをlocked constantと突き合わせ、bool自己申告を無効化する。"""
    identities = _require_fields(
        document["input_artifact_identities"],
        _INPUT_IDENTITY_FIELDS,
        "input_artifact_identities",
    )
    declared = _require_bool(
        identities["real_artifact_execution"], "real_artifact_execution"
    )
    observed = {name: identities[name] for name in _IDENTITY_FIELDS}
    for name, value in observed.items():
        if type(value) is not str or len(value) != 64:
            raise _error(f"input_artifact_identities.{name} is not a sha256 digest")
    if declared is not (observed == LOCKED_SOURCE_IDENTITIES.to_document()):
        raise _error(
            "real_artifact_execution does not follow from the recorded input "
            "artifact identities; it is derived from an exact comparison against "
            "the locked Issue #158 identities, never self-declared"
        )
    if document["locked_source_identities"] != LOCKED_SOURCE_IDENTITIES.to_document():
        raise _error(
            "locked_source_identities does not match the locked retained artifacts"
        )


def _validate_candidate(block: object, *, real_artifact_execution: bool) -> None:
    candidate = _require_fields(block, _CANDIDATE_FIELDS, "candidate")
    if candidate["model"] != p1_model_block():
        raise _error("the candidate model block is not the locked P1 model")
    if candidate["training"] != p1_training_block():
        raise _error("the candidate training block is not the locked #140 semantics")
    for name in ("weights_digest", "supported_indices_digest"):
        value = candidate[name]
        if type(value) is not str or len(value) != 64:
            raise _error(f"candidate.{name} must be a 64 character sha256 digest")
    # 実retained artifactに対する実行だけがlocked support setをsource of truthに
    # できる。合成candidate pairはTRAIN behaviorが違うためsupport setも違う。
    if real_artifact_execution and candidate["supported_indices_digest"] != (
        LOCKED_SOURCE_IDENTITIES.supported_indices_digest
    ):
        raise _error(
            "the candidate was not trained under the retained TRAIN support set; "
            "the support restriction semantics are locked"
        )
    epochs = candidate["epoch_history"]
    if type(epochs) is not list or not epochs:
        raise _error("candidate.epoch_history must be a non-empty list")
    if candidate["training"]["maximum_epochs"] != len(epochs):
        raise _error("candidate.epoch_history does not cover every locked epoch")
    selected = _require_count(candidate["selected_epoch"], "candidate.selected_epoch")
    if selected != len(epochs):
        raise _error(
            "checkpoint selection must stay fixed_final_iteration; the selected "
            "epoch is not the final outer iteration"
        )
    if type(candidate["final_validation_huber_loss"]) not in (int, float):
        raise _error("candidate.final_validation_huber_loss must be a number")


def _validate_agreement(block: object, expected_row_count: int) -> None:
    agreement = _require_fields(block, _AGREEMENT_FIELDS, "action_agreement")
    if agreement["eligible_row_count"] != expected_row_count:
        raise _error("action_agreement row count differs from the eligible row count")
    for pair in AGREEMENT_PAIRS:
        count = _require_count(
            agreement[f"{pair}_disagreement_count"], f"action_agreement {pair} count"
        )
        if count > expected_row_count:
            raise _error(f"action_agreement {pair} count is out of range")
        if agreement[f"{pair}_disagreement_rate"] != rate(count, expected_row_count):
            raise _error(
                f"action_agreement {pair} rate is not derivable from its counts"
            )


def _validate_conditions(block: object, arms: dict, pairs: dict) -> None:
    conditions = _require_fields(block, _CONDITION_FIELDS, "outcome_conditions")
    expected = outcome_conditions(arms, pairs)
    for name in sorted(_CONDITION_FIELDS):
        if conditions[name] != expected[name]:
            raise _error(
                f"outcome_conditions.{name} is not derivable from the recorded "
                "hand-progression metrics"
            )
    for name in ("signal_conditions", "regression_conditions"):
        for key, value in conditions[name].items():
            _require_bool(value, f"outcome_conditions.{name}.{key}")
    _require_bool(conditions["signal"], "outcome_conditions.signal")
    _require_bool(conditions["regression"], "outcome_conditions.regression")


def _validate_hand_progression(block: object, expected_row_count: int) -> str:
    measurement = _require_fields(block, _HAND_PROGRESSION_FIELDS, "hand_progression")
    status = measurement["status"]
    if status not in _AVAILABILITY_VALUES:
        raise _error(f"unknown hand_progression status: {status!r}")

    if status == MeasurementAvailability.UNAVAILABLE.value:
        if not measurement["unavailable_reason"]:
            raise _error("an unavailable hand_progression must record its reason")
        for name in ("arms", "pairs", "per_seed", "outcome_conditions"):
            if measurement[name] is not None:
                raise _error("an unavailable hand_progression must not carry summaries")
        return status

    if measurement["unavailable_reason"] is not None:
        raise _error("an available hand_progression must not record a reason")
    arms = _require_fields(measurement["arms"], set(ARMS), "hand_progression arms")
    for arm in ARMS:
        entry = _require_fields(arms[arm], _ARM_FIELDS, f"hand_progression {arm}")
        row_count = _require_count(entry["row_count"], f"{arm} row count")
        if row_count != expected_row_count:
            raise _error(f"hand_progression {arm} was not measured on every row")
        if (
            _validate_summary(entry["post_discard_shanten"], f"{arm} shanten")
            != row_count
        ):
            raise _error(f"hand_progression {arm} shanten summary count differs")
        for name in ("keep_shanten", "worsen_shanten"):
            count = _require_count(entry[f"{name}_count"], f"{arm} {name} count")
            if count > row_count:
                raise _error(f"hand_progression {arm} {name} count exceeds its rows")
            if entry[f"{name}_rate"] != rate(count, row_count):
                raise _error(
                    f"hand_progression {arm} {name} rate is not derivable from counts"
                )

    pairs = _require_fields(measurement["pairs"], set(PAIRS), "hand_progression pairs")
    for pair in PAIRS:
        entry = _require_fields(pairs[pair], _PAIR_FIELDS, f"hand_progression {pair}")
        row_count = _require_count(entry["row_count"], f"{pair} row count")
        if row_count != expected_row_count:
            raise _error(f"hand_progression {pair} was not measured on every row")
        ordered = [
            _require_count(
                entry[f"{name}_post_discard_shanten_count"], f"{pair} {name} count"
            )
            for name in ("lower", "equal", "higher")
        ]
        if sum(ordered) != row_count:
            raise _error(f"hand_progression {pair} counts do not partition its rows")
        if entry["higher_post_discard_shanten_rate"] != rate(ordered[2], row_count):
            raise _error(f"hand_progression {pair} higher rate is not derivable")
        left, right = pair.split("_vs_")
        expected_difference = (
            None
            if row_count == 0
            else arms[left]["worsen_shanten_rate"] - arms[right]["worsen_shanten_rate"]
        )
        if entry["worsen_shanten_rate_difference"] != expected_difference:
            raise _error(
                f"hand_progression {pair} worsening rate difference is not derivable "
                "from the per-arm rates"
            )

    per_seed = measurement["per_seed"]
    if type(per_seed) is not list:
        raise _error("hand_progression per_seed must be a list")
    seeds: set[int] = set()
    total = 0
    for entry in per_seed:
        block_ = _require_fields(entry, _PER_SEED_FIELDS, "hand_progression per_seed")
        seed = block_["seed"]
        if type(seed) is not int or seed in seeds:
            raise _error("hand_progression per_seed repeats or malforms a seed")
        seeds.add(seed)
        row_count = _require_count(block_["row_count"], "per_seed row count")
        total += row_count
        ordered = [
            _require_count(
                block_[f"{name}_post_discard_shanten_count"], f"per_seed {name} count"
            )
            for name in ("lower", "equal", "higher")
        ]
        if sum(ordered) != row_count:
            raise _error("hand_progression per_seed counts do not partition its rows")
        for name in ("q_v2", "q_v1"):
            if (
                _require_count(
                    block_[f"{name}_worsen_shanten_count"], f"per_seed {name} count"
                )
                > row_count
            ):
                raise _error(f"per_seed {name} worsening count exceeds its rows")
    if total != expected_row_count:
        raise _error("hand_progression per_seed does not partition the eligible rows")

    _validate_conditions(measurement["outcome_conditions"], arms, pairs)
    return status


def _validate_role(role: object) -> None:
    entry = _require_fields(role, _ROLE_FIELDS, "Gate A role")
    try:
        name = P1GateARole(entry["role"])
    except ValueError:
        raise _error(f"unknown Gate A role: {entry['role']!r}") from None
    if entry["source_artifact"] != _ROLE_SOURCE_ARTIFACT[name]:
        raise _error(f"{name.value} does not declare its locked source artifact")
    if entry["split"] != _ROLE_SPLIT[name].value:
        raise _error(f"{name.value} does not declare its locked split")
    if _require_bool(entry["is_primary_role"], "is_primary_role") is not (
        name in PRIMARY_ROLES
    ):
        raise _error(f"{name.value} does not declare its locked primary-role status")
    if _require_bool(entry["is_generalization_evidence"], "is_generalization_evidence"):
        raise _error(
            "no Gate A role is generalization evidence; every population was "
            "already exposed in #140 / #152"
        )

    counts = _require_fields(entry["row_counts"], _ROW_COUNT_FIELDS, "row_counts")
    total = _require_count(counts["total_row_count"], "total_row_count")
    eligible = _require_count(counts["eligible_row_count"], "eligible_row_count")
    if eligible > total:
        raise _error("eligible row count is out of range")
    for field in (
        "choice_row_count",
        "ordinary_discard_row_count",
        "support_complete_row_count",
    ):
        if _require_count(counts[field], field) > total:
            raise _error(f"{field} exceeds the total row count")
    if counts["excluded_row_count"] != total - eligible:
        raise _error("excluded row count is not derivable from the row counts")

    coverage = _require_fields(
        entry["derived_coverage"], _COVERAGE_FIELDS, "derived_coverage"
    )
    covered = _require_count(coverage["row_count"], "derived_coverage row count")
    if covered != total:
        raise _error("derived coverage does not cover every retained row")
    if coverage["source_rows_derived"] != covered:
        raise _error("derived coverage does not cover every source state")
    if (
        _require_count(coverage["nonterminal_next_rows_derived"], "next rows")
        + _require_count(coverage["terminal_next_rows_zero_padded"], "terminal rows")
        != covered
    ):
        raise _error("derived next-state coverage does not partition the rows")
    if coverage["imputed_row_count"] != 0:
        raise _error("P1 derivation never imputes an ambiguous row")

    _validate_agreement(entry["action_agreement"], eligible)
    _validate_hand_progression(entry["hand_progression"], eligible)


def validate_gate_a_result(document: object) -> dict:
    """Gate A result documentを、aggregateをcountsから再導出しながら検証する。

    schema、locked constant、role集合、metricsの母数、countsからのrate再導出、
    そしてoutcome conditionの再導出までをfail closedで確認する。documentが
    自己申告するflagやlabelをauthorityにしない。
    """
    validated = _require_fields(document, _RESULT_FIELDS, "Gate A result")
    if validated["p1_gate_a_schema_version"] != P1_GATE_A_SCHEMA_VERSION:
        raise _error("unsupported Gate A schema version")
    if validated["p1_gate_a_id"] != P1_GATE_A_ID:
        raise _error("this result is not the Issue #158 Gate A result")
    if validated["source_issue"] != SOURCE_ISSUE:
        raise _error("Gate A result source_issue is not the locked one")
    if validated["predecessor_issues"] != list(PREDECESSOR_ISSUES):
        raise _error("Gate A result predecessor_issues are not the locked ones")
    if validated["parent_issue"] != PARENT_ISSUE:
        raise _error("Gate A result parent_issue is not the locked one")
    if validated["protocol_id"] != PROTOCOL_ID:
        raise _error("Gate A result protocol_id is not the locked one")
    if validated["retention"] != {
        "backend": RETENTION_BACKEND,
        "key": RETENTION_KEY,
    }:
        raise _error("Gate A result retention target is not the locked one")
    if validated["feature"] != feature_block():
        raise _error("Gate A result v1 feature identity is not the locked one")
    if validated["derived_feature"] != p1_feature_block():
        raise _error("Gate A result derived feature identity is not the locked one")
    if validated["vocabulary"] != vocabulary_block():
        raise _error("Gate A result vocabulary identity is not the locked one")
    if validated["changed_axis"] != list(CHANGED_AXIS):
        raise _error("Gate A result changed_axis is not the locked single axis")
    if validated["generation_budget"] != dict(GENERATION_BUDGET):
        raise _error(
            "Gate A produced no new game, seed, hanchan, or TEST exposure; the "
            "recorded generation budget must stay zero"
        )
    if validated["fixed_quantiles"] != [
        format(value, "g") for value in FIXED_QUANTILES
    ]:
        raise _error("Gate A result quantile set is not the locked one")
    if validated["primary_roles"] != [role.value for role in PRIMARY_ROLES]:
        raise _error("Gate A result primary roles are not the locked ones")
    if validated["limitations"] != list(P1_GATE_A_LIMITATIONS):
        raise _error("Gate A result limitations are not the locked ones")
    if validated["interpretation_boundary"] != {
        "positive_claim_limit": INTERPRETATION_BOUNDARY["positive_claim_limit"],
        "forbidden_claims": list(INTERPRETATION_BOUNDARY["forbidden_claims"]),
        "negative_claim_limit": INTERPRETATION_BOUNDARY["negative_claim_limit"],
    }:
        raise _error("Gate A result interpretation boundary is not the locked one")
    _validate_input_identities(validated)
    _validate_candidate(
        validated["candidate"],
        real_artifact_execution=validated["input_artifact_identities"][
            "real_artifact_execution"
        ],
    )

    roles = validated["roles"]
    if type(roles) is not list:
        raise _error("Gate A roles must be a list")
    seen: set[str] = set()
    for role in roles:
        _validate_role(role)
        if role["role"] in seen:
            raise _error("a Gate A role appears more than once")
        seen.add(role["role"])
    if seen != {item.value for item in P1GateARole}:
        raise _error(
            "a Gate A result must measure every locked role: TRAIN, VALIDATION, "
            "dataset TEST and the replacement TEST"
        )

    classification = validated["classification"]
    if classification is not None:
        if classification not in _OUTCOME_VALUES:
            raise _error(f"unknown Gate A outcome: {classification!r}")
        if classification != derive_classification(validated).value:
            raise _error(
                "the recorded classification is not the outcome that the locked "
                "Issue #158 ladder derives from these metrics"
            )
    return validated


def derive_classification(document: dict) -> P1GateAOutcome:
    """事前lockされたladderからexhaustive outcomeを機械的に導出する。

    ```text
    P1 HAND-PROGRESSION SIGNAL         両primary roleでsignal条件3つがすべて成立
    P1 HAND-PROGRESSION REGRESSION     両primary roleでregression条件3つがすべて成立
    P1 HAND-PROGRESSION INCONCLUSIVE   valid evidenceはあるがどちらでもない
    P1 EVIDENCE INSUFFICIENT           primary roleのvalid comparisonが成立しない
    ```

    `STOP / INVALID`はここから導出されない。identity mismatch、schema不整合、
    leakage、TEST discipline violationはresult documentが作られる前に
    strict bindingとvalidationがfail closedするためである。
    """
    primary = {
        role["role"]: role
        for role in document["roles"]
        if role["role"] in {item.value for item in PRIMARY_ROLES}
    }
    if set(primary) != {role.value for role in PRIMARY_ROLES}:
        return P1GateAOutcome.EVIDENCE_INSUFFICIENT
    conditions = []
    for role in primary.values():
        progression = role["hand_progression"]
        if progression["status"] != MeasurementAvailability.AVAILABLE.value:
            return P1GateAOutcome.EVIDENCE_INSUFFICIENT
        if role["row_counts"]["eligible_row_count"] == 0:
            return P1GateAOutcome.EVIDENCE_INSUFFICIENT
        conditions.append(progression["outcome_conditions"])
    if all(entry["signal"] for entry in conditions):
        return P1GateAOutcome.HAND_PROGRESSION_SIGNAL
    if all(entry["regression"] for entry in conditions):
        return P1GateAOutcome.HAND_PROGRESSION_REGRESSION
    return P1GateAOutcome.HAND_PROGRESSION_INCONCLUSIVE


def record_classification(document: dict, outcome: P1GateAOutcome) -> dict:
    """validated resultへexhaustive outcomeを1件だけ記録する。

    ここが機械的に強制するのは:

    - outcomeが`P1GateAOutcome`のexhaustive集合に属すること
    - 実artifactをstrict readbackした実行結果にだけ付与できること
    - 一度記録したoutcomeを上書きできないこと
    - 記録するoutcomeが`derive_classification()`の導出結果と一致すること
      （結果を見てthreshold / role / feature familyを変更できない）

    である。`STOP / INVALID`はresult documentへ記録しない。identity mismatch、
    leakage、schema / provenance不整合はbinding / validationがdocument生成前に
    fail closedするからである。
    """
    validated = validate_gate_a_result(document)
    if not isinstance(outcome, P1GateAOutcome):
        raise TypeError("outcome must be a P1GateAOutcome")
    if validated["classification"] is not None:
        raise _error("this Gate A result already records an outcome")
    if validated["input_artifact_identities"]["real_artifact_execution"] is not True:
        raise _error(
            "a Gate A outcome may only be recorded for a result produced by a real "
            "strict readback of the retained artifacts"
        )
    if outcome is P1GateAOutcome.STOP_INVALID:
        raise _error(
            "STOP / INVALID is a pre-result fail-closed state; an identity, "
            "schema, leakage, or TEST discipline violation aborts the run before a "
            "Gate A result document exists"
        )
    derived = derive_classification(validated)
    if outcome is not derived:
        raise _error(
            f"the locked Issue #158 ladder derives {derived.value!r} from these "
            f"metrics, not {outcome.value!r}"
        )
    return validate_gate_a_result({**validated, "classification": outcome.value})


# --- Input binding --------------------------------------------------------


def bind_gate_a_inputs(
    *,
    dataset: LoadedOfflineQDataset,
    bc_checkpoint,
    q_checkpoint,
    replacement_test,
    expected: ExpectedArtifactIdentities = LOCKED_SOURCE_IDENTITIES,
) -> DiagnosisInputBinding:
    """#152のstrict retained-artifact bindingをそのまま再利用する。

    Issue #158は#152と同じ4 artifactとsupport semanticsを対象にするため、
    binding / identity validationを再実装せず共有する。
    """
    verify_contract_identity()
    return bind_diagnosis_inputs(
        dataset=dataset,
        bc_checkpoint=bc_checkpoint,
        q_checkpoint=q_checkpoint,
        replacement_test=replacement_test,
        expected=expected,
    )


__all__ = [
    "ARMS",
    "AGREEMENT_PAIRS",
    "CHANGED_AXIS",
    "GENERATION_BUDGET",
    "INTERPRETATION_BOUNDARY",
    "PAIRS",
    "PARENT_ISSUE",
    "PREDECESSOR_ISSUES",
    "PRIMARY_ROLES",
    "P1_GATE_A_ID",
    "P1_GATE_A_LIMITATIONS",
    "P1_GATE_A_SCHEMA_VERSION",
    "SOURCE_ISSUE",
    "CandidateModelRecord",
    "P1GateAOutcome",
    "P1GateARole",
    "P1RolePopulation",
    "action_agreement",
    "bind_gate_a_inputs",
    "build_gate_a_result",
    "derive_classification",
    "evaluate_role",
    "hand_progression_block",
    "outcome_conditions",
    "record_classification",
    "validate_gate_a_result",
    "verify_derived_alignment",
]
