"""Artifact-only failure diagnosis for the #140 Q-vs-BC candidate pair (Issue #152).

Issue #140は`VALUE/Q OBJECTIVE NEGATIVE`で終了し、candidate-only Mahjong
diagnosticsでQ hybridのtenpai到達が`2 / 100`という記述的signalを残した。
Issue #152は、**新しいhanchan・seed・training・strength evidenceを一切作らず**、
#140でretain済みのartifactだけでその失敗機構をboundedに切り分ける。

```text
retained candidate-pair bundle        dataset artifact      replacement TEST artifact
  offlineq-candidate-freeze.json        69094c1b...            fe7a4455...
  bc-checkpoint  17a31fc8...
  q-checkpoint   31545d6b...
        |                                    |                        |
        +------------------ strict readback -+------------------------+
                                     |
                                     v
                    bind_diagnosis_inputs()   identity / digest / schema binding
                                     |
                                     v
                    eligible ordinary-discard choice rows
                    （forced / non-discard / support-incompleteを除外）
                                     |
        +----------------+-----------+-----------+----------------+
        v                v                       v                v
  Measurement A     Measurement B          Measurement C     Measurement D
  same-state        Q ranking              reward /          hand progression
  disagreement      stability              bootstrap         （導出可能な場合のみ）
        |                |                       |                |
        +----------------+-----------+-----------+----------------+
                                     v
                        build_diagnosis_result()  versioned JSON document
                                     v
                        validate_diagnosis_result()
```

## このmoduleが意図的に**しない**こと

- 新しいgame生成、seed割り当て、retraining、replacement TESTの再生成
- resultを見てからのthreshold導入・classification rule変更
- offline disagreementをstrength regressionのcausal proofとして扱うこと
- rollout distribution shiftの主張（A-Cはbehavior distribution上のdiagnostic）
- artifactが手元に無いことを`DIAGNOSTIC EVIDENCE INSUFFICIENT`と判定すること

exhaustive outcomeは`DiagnosisOutcome`が列挙するが、**このmoduleは数値から
outcomeを自動生成しない**。Issue #152のladderはqualitativeな判断（「明確な
descriptive patternがあるか」）であり、閾値を後付けで発明しないことがIssueの
明示要件だからである。`record_classification()`は「実artifactをstrict
readbackした実行結果に対してのみ、exhaustiveな1件を記録できる」という
機械的な境界だけを強制する。
"""

from dataclasses import dataclass
from enum import Enum
from math import ceil, floor, isfinite

from .artifact import LoadedOfflineQDataset, feature_block, vocabulary_block
from .errors import OfflineQAmbiguousStateError, OfflineQDiagnosisError
from .hand_progression import (
    UKEIRE_UNAVAILABLE_REASON,
    HandProgression,
    MeasurementAvailability,
    hand_progression_for_row,
    is_discard_index,
)
from .protocol import (
    GAMMA,
    MINIMUM_CHOICE_LEGAL_ACTION_COUNT,
    PROTOCOL_ID,
    VOCABULARY_SIZE,
    Split,
    verify_contract_identity,
)
from .replacement_test import LoadedReplacementTest
from .support import LEGAL_ACTION_COUNT_BUCKETS, support_set_identity

DIAGNOSIS_SCHEMA_VERSION = "arena-learned-policy-offlineq-diagnosis-v1"
DIAGNOSIS_ID = "arena-learned-policy-offlineq-failure-diagnosis-152"
SOURCE_ISSUE = "lisbun/lisjong-arena#152"
PREDECESSOR_ISSUE = "lisbun/lisjong-arena#140"

FIXED_QUANTILES = (0.0, 0.05, 0.25, 0.5, 0.75, 0.95, 1.0)
"""結果を見てから足し引きしない固定quantile集合。"""

DECISION_DEPTH_BAND_EDGES = (4, 8, 12, 16)
"""`decision_ordinal`のstratification band境界（upper-exclusive）。

`0-3 / 4-7 / 8-11 / 12-15 / 16+`。resultを見てから変更しない。
"""

TD_TARGET_MODEL = "final_q_checkpoint_as_its_own_target"
"""TD targetに使うtarget networkの正体。

retained artifactはfitted-Q最終iterationのonline networkだけを含み、
そのepochで使われたtarget network snapshotは含まない。したがってこの
diagnosticのTD targetは「最終checkpointを自分自身のtarget networkとして
評価したもの」であり、training中に実際に使われたtargetの再現ではない。
"""


class DiagnosisOutcome(Enum):
    """Issue #152がresult exposure前に固定したexhaustive outcome。"""

    HAND_PROGRESSION_DEGRADATION_IDENTIFIED = "HAND-PROGRESSION DEGRADATION IDENTIFIED"
    Q_RANKING_INSTABILITY_IDENTIFIED = "Q-RANKING INSTABILITY IDENTIFIED"
    FAILURE_MECHANISM_INCONCLUSIVE = "FAILURE MECHANISM INCONCLUSIVE"
    DIAGNOSTIC_EVIDENCE_INSUFFICIENT = "DIAGNOSTIC EVIDENCE INSUFFICIENT"
    STOP_INVALID = "STOP / INVALID"


class DiagnosisRole(Enum):
    """diagnosticを走らせるrow populationのrole。"""

    DATASET_TRAIN = "dataset-train"
    DATASET_VALIDATION = "dataset-validation"
    DATASET_TEST = "dataset-test"
    REPLACEMENT_TEST = "replacement-test"


_ROLE_SPLIT = {
    DiagnosisRole.DATASET_TRAIN: Split.TRAIN,
    DiagnosisRole.DATASET_VALIDATION: Split.VALIDATION,
    DiagnosisRole.DATASET_TEST: Split.TEST,
    DiagnosisRole.REPLACEMENT_TEST: Split.TEST,
}
_ROLE_SOURCE_ARTIFACT = {
    DiagnosisRole.DATASET_TRAIN: "dataset",
    DiagnosisRole.DATASET_VALIDATION: "dataset",
    DiagnosisRole.DATASET_TEST: "dataset",
    DiagnosisRole.REPLACEMENT_TEST: "replacement-test",
}
_ROLE_GENERALIZATION_EVIDENCE = {
    DiagnosisRole.DATASET_TRAIN: False,
    DiagnosisRole.DATASET_VALIDATION: False,
    DiagnosisRole.DATASET_TEST: False,
    DiagnosisRole.REPLACEMENT_TEST: False,
}
"""どのroleも新しいgeneralization / strength claimを構成しない。

TRAIN / VALIDATIONはbehavior distributionそのもの、dataset TEST `271..276`は
#140以前にexposure済み、replacement TEST `354..359`は#140で一度だけ
exposureしたoffline diagnostic populationである。本Issueは既存artifactの
記述的診断だけを行い、いずれもnew TEST claimへ昇格させない。
"""

DIAGNOSIS_LIMITATIONS = (
    "A-C are computed on offline behavior-distribution states and do not observe "
    "the rollout distribution that the Q hybrid itself induces; they cannot prove "
    "ROLLOUT DISTRIBUTION SHIFT.",
    "This diagnosis is descriptive, not causal: an offline decision disagreement "
    "is not proof of the #140 strength regression.",
    "The #140 candidate-only Mahjong metrics (tenpai reached 2/100, mean first "
    "tenpai turn 19.0) are Q hybrid's own values and are not a difference against "
    "the BC hybrid baseline.",
    "TD targets are evaluated with the final Q checkpoint acting as its own target "
    "network; the retained artifact does not contain the per-epoch target network "
    "snapshot that fitted-Q actually regressed against.",
    "Measurement D reconstructs only the acting seat's concealed hand from the "
    "recorded player-safe feature row; ukeire is not derived because no first-party "
    "public ukeire contract can be reused exactly.",
    "TRAIN and VALIDATION agreement is behavior-distribution agreement and is not "
    "generalization evidence; dataset TEST 271..276 and replacement TEST 354..359 "
    "were already exposed in #140 and are not re-used here as a new TEST claim.",
    "No new hanchan, seeds, training, or strength evidence were produced; every "
    "number is derived from artifacts retained by #140.",
)

RETAINED_STRENGTH_CONTEXT = {
    "source_issue": PREDECESSOR_ISSUE,
    "recomputed": False,
    "candidate": "q-hybrid",
    "baseline": "bc-hybrid",
    "mean_candidate_game_delta": -488.67,
    "normal_approx_95_interval_lower": -844.19,
    "normal_approx_95_interval_upper": -133.15,
    "final_outcome": "VALUE/Q OBJECTIVE NEGATIVE",
    "candidate_only_mahjong_metrics": {
        "is_baseline_difference": False,
        "tenpai_reached_count": 2,
        "game_count": 100,
        "mean_first_tenpai_turn": 19.0,
    },
}
"""#140のcanonical strength resultをcontextとしてだけ持つ（Measurement E）。

再計算・再samplingせず、candidate-only metricsがbaseline差ではないことを
schema上で明示する。
"""


def _error(message: str) -> OfflineQDiagnosisError:
    return OfflineQDiagnosisError(message)


# --- Input binding --------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExpectedArtifactIdentities:
    """diagnosticが対象にするartifactのexact identity。"""

    dataset_identity: str
    bc_checkpoint_identity: str
    q_checkpoint_identity: str
    replacement_test_artifact_identity: str
    supported_indices_digest: str

    def __post_init__(self) -> None:
        for name in (
            "dataset_identity",
            "bc_checkpoint_identity",
            "q_checkpoint_identity",
            "replacement_test_artifact_identity",
            "supported_indices_digest",
        ):
            value = getattr(self, name)
            if type(value) is not str or len(value) != 64:
                raise _error(f"{name} must be a 64 character sha256 digest")

    def to_document(self) -> dict[str, object]:
        return {
            "dataset_identity": self.dataset_identity,
            "bc_checkpoint_identity": self.bc_checkpoint_identity,
            "q_checkpoint_identity": self.q_checkpoint_identity,
            "replacement_test_artifact_identity": (
                self.replacement_test_artifact_identity
            ),
            "supported_indices_digest": self.supported_indices_digest,
        }


LOCKED_SOURCE_IDENTITIES = ExpectedArtifactIdentities(
    dataset_identity=(
        "69094c1b82f2aaedfed57cb3021b90d44642c3978a2368d4d1e2d927c5a7b2f4"
    ),
    bc_checkpoint_identity=(
        "17a31fc8aa0edcdd3834da7075abe37bd9554d47f4efe94afb31050bad20ac3b"
    ),
    q_checkpoint_identity=(
        "31545d6bde3da4fd7ee6152bf3183e5be82302d8a5cee70ccf35923781382b94"
    ),
    replacement_test_artifact_identity=(
        "fe7a4455b775cbc23568b0d9c7489593c0859bce28e0529e3e400a816cf7fccd"
    ),
    supported_indices_digest=(
        "230b2f07dc95d169ebfb85b9deb6174f22909b52025979ae64da541bd9481d9e"
    ),
)
"""Issue #152がsource of truthとしてlockした#140 retained artifactのidentity。"""

RETENTION_BACKEND = "operator-local-durable"
RETENTION_KEY = "offlineq-140-rebuild/candidate-pair"


@dataclass(frozen=True, slots=True)
class DiagnosisInputBinding:
    """strict readback済みinputのidentity binding。"""

    dataset_identity: str
    bc_checkpoint_identity: str
    q_checkpoint_identity: str
    replacement_test_artifact_identity: str
    supported_indices_digest: str
    real_artifact_execution: bool

    def to_document(self) -> dict[str, object]:
        return {
            "dataset_identity": self.dataset_identity,
            "bc_checkpoint_identity": self.bc_checkpoint_identity,
            "q_checkpoint_identity": self.q_checkpoint_identity,
            "replacement_test_artifact_identity": (
                self.replacement_test_artifact_identity
            ),
            "supported_indices_digest": self.supported_indices_digest,
            "real_artifact_execution": self.real_artifact_execution,
        }


def bind_diagnosis_inputs(
    *,
    dataset: LoadedOfflineQDataset,
    bc_checkpoint,
    q_checkpoint,
    replacement_test: LoadedReplacementTest,
    expected: ExpectedArtifactIdentities,
) -> DiagnosisInputBinding:
    """4 artifactを1つのdiagnosticへbindし、identity不一致をfail closedする。

    `expected`は「どのartifactを診断しているか」を宣言する必須引数である。
    実行時に観測したidentityがそれと一致するかどうかを`real_artifact_execution`
    として記録し、`record_classification()`がそれを要求する。
    """
    verify_contract_identity()
    if not isinstance(dataset, LoadedOfflineQDataset):
        raise TypeError("dataset must be a LoadedOfflineQDataset")
    if not isinstance(replacement_test, LoadedReplacementTest):
        raise TypeError("replacement_test must be a LoadedReplacementTest")
    if not isinstance(expected, ExpectedArtifactIdentities):
        raise TypeError("expected must be an ExpectedArtifactIdentities")

    bc_dataset_identity = bc_checkpoint.manifest["dataset_identity"]
    q_dataset_identity = q_checkpoint.manifest["dataset_identity"]
    if bc_dataset_identity != q_dataset_identity:
        raise _error(
            "BC and Q checkpoints were not trained on the same dataset identity"
        )
    if bc_dataset_identity != dataset.identity:
        raise _error(
            "the supplied dataset is not the dataset the checkpoints were trained on"
        )

    for name, manifest in (
        ("dataset", dataset.manifest),
        ("BC checkpoint", bc_checkpoint.manifest),
        ("Q checkpoint", q_checkpoint.manifest),
        ("replacement TEST artifact", replacement_test.manifest),
    ):
        if manifest["feature"] != feature_block():
            raise _error(f"{name} feature schema identity is not the locked one")
        if manifest["vocabulary"] != vocabulary_block():
            raise _error(f"{name} action vocabulary identity is not the locked one")

    supported_indices = sorted(q_checkpoint.supported_indices)
    recomputed_digest = support_set_identity(supported_indices)
    if q_checkpoint.manifest["supported_indices_digest"] != recomputed_digest:
        raise _error(
            "Q checkpoint supported_indices does not match its recorded digest"
        )

    observed = (
        ("dataset_identity", dataset.identity, expected.dataset_identity),
        (
            "bc_checkpoint_identity",
            bc_checkpoint.identity,
            expected.bc_checkpoint_identity,
        ),
        (
            "q_checkpoint_identity",
            q_checkpoint.identity,
            expected.q_checkpoint_identity,
        ),
        (
            "replacement_test_artifact_identity",
            replacement_test.identity,
            expected.replacement_test_artifact_identity,
        ),
        (
            "supported_indices_digest",
            recomputed_digest,
            expected.supported_indices_digest,
        ),
    )
    for name, actual, wanted in observed:
        if actual != wanted:
            raise _error(
                f"{name} does not match the artifact this diagnosis is bound to: "
                f"expected {wanted}, got {actual}"
            )

    return DiagnosisInputBinding(
        dataset_identity=dataset.identity,
        bc_checkpoint_identity=bc_checkpoint.identity,
        q_checkpoint_identity=q_checkpoint.identity,
        replacement_test_artifact_identity=replacement_test.identity,
        supported_indices_digest=recomputed_digest,
        real_artifact_execution=expected == LOCKED_SOURCE_IDENTITIES,
    )


# --- Fixed summaries ------------------------------------------------------


def _quantile(sorted_values: list[float], quantile: float) -> float:
    position = quantile * (len(sorted_values) - 1)
    lower = floor(position)
    upper = ceil(position)
    if lower == upper:
        return sorted_values[lower]
    weight = position - lower
    return sorted_values[lower] * (1.0 - weight) + sorted_values[upper] * weight


def fixed_summary(values) -> dict[str, object]:
    """母数と平均と固定quantileだけを持つ、result非依存のsummaryを返す。"""
    items = [float(value) for value in values]
    if any(not isfinite(value) for value in items):
        raise _error("a diagnostic summary cannot contain non-finite values")
    if not items:
        return {"count": 0, "mean": None, "quantiles": None}
    ordered = sorted(items)
    return {
        "count": len(ordered),
        "mean": sum(ordered) / len(ordered),
        "quantiles": {
            format(quantile, "g"): _quantile(ordered, quantile)
            for quantile in FIXED_QUANTILES
        },
    }


def _rate(numerator: int, denominator: int) -> float | None:
    if denominator == 0:
        return None
    return numerator / denominator


def legal_action_count_bucket(legal_action_count: int) -> str:
    """`LEGAL_ACTION_COUNT_BUCKETS`と同じ境界でstratification labelを作る。"""
    for bucket in LEGAL_ACTION_COUNT_BUCKETS:
        if legal_action_count <= bucket:
            return str(bucket)
    return f"{LEGAL_ACTION_COUNT_BUCKETS[-1] + 1}+"


def decision_depth_band(decision_ordinal: int) -> str:
    """`DECISION_DEPTH_BAND_EDGES`で固定したdecision depth bandを返す。"""
    lower = 0
    for edge in DECISION_DEPTH_BAND_EDGES:
        if decision_ordinal < edge:
            return f"{lower}-{edge - 1}"
        lower = edge
    return f"{lower}+"


# --- Measurement execution ------------------------------------------------


def require_finite(values, context: str) -> int:
    """non-finiteを数え、1件でもあればfail closedする。返り値は常に0。"""
    import torch

    non_finite = int((~torch.isfinite(values)).sum())
    if non_finite:
        raise _error(f"{context} contains {non_finite} non-finite values")
    return non_finite


def _discard_block_mask():
    import torch

    return torch.tensor(
        [is_discard_index(index) for index in range(VOCABULARY_SIZE)],
        dtype=torch.bool,
    )


@dataclass(frozen=True, slots=True)
class EligibilityCounts:
    """diagnostic対象rowを絞り込む各段階の母数。"""

    total_row_count: int
    choice_row_count: int
    ordinary_discard_row_count: int
    support_complete_row_count: int
    eligible_row_count: int

    def to_document(self) -> dict[str, object]:
        return {
            "total_row_count": self.total_row_count,
            "choice_row_count": self.choice_row_count,
            "ordinary_discard_row_count": self.ordinary_discard_row_count,
            "support_complete_row_count": self.support_complete_row_count,
            "eligible_row_count": self.eligible_row_count,
            "excluded_row_count": self.total_row_count - self.eligible_row_count,
        }


def select_eligible_rows(tensors, support_mask):
    """serving activationと同じ条件でeligible rowのbool maskと母数を返す。

    `HybridPolicy.choose_action()`は
    `is_eligible_ordinary_discard_choice()`と`is_support_complete()`の両方が
    成立した場合だけlearned modelを使う。forced decision、非discardを含む
    decision、support-incomplete decisionはscaffoldへfallbackするため、
    Q / BCのdecision差を測る母数へ混ぜない。
    """
    legal_mask = tensors.legal_mask
    discard_block = _discard_block_mask().unsqueeze(0)
    support = support_mask.unsqueeze(0)

    choice = legal_mask.sum(dim=1) >= MINIMUM_CHOICE_LEGAL_ACTION_COUNT
    ordinary_discard = ~(legal_mask & ~discard_block).any(dim=1)
    support_complete = ~(legal_mask & ~support).any(dim=1)
    eligible = choice & ordinary_discard & support_complete

    counts = EligibilityCounts(
        total_row_count=int(legal_mask.shape[0]),
        choice_row_count=int(choice.sum()),
        ordinary_discard_row_count=int((choice & ordinary_discard).sum()),
        support_complete_row_count=int((choice & support_complete).sum()),
        eligible_row_count=int(eligible.sum()),
    )
    return eligible, counts


def _model_outputs(model, features, batch_size: int):
    import torch

    outputs = []
    with torch.inference_mode():
        for start in range(0, int(features.shape[0]), batch_size):
            outputs.append(model(features[start : start + batch_size]).clone())
    if not outputs:
        return torch.zeros((0, VOCABULARY_SIZE), dtype=torch.float32)
    return torch.cat(outputs, dim=0)


@dataclass(frozen=True, slots=True)
class RolePopulation:
    """1 roleのtensorとrow recordの組。順序はartifact順で固定する。"""

    role: DiagnosisRole
    tensors: object
    rows: tuple

    def __post_init__(self) -> None:
        if not isinstance(self.role, DiagnosisRole):
            raise TypeError("role must be a DiagnosisRole")
        if len(self.rows) != self.tensors.row_count:
            raise _error("row records and tensors describe different row counts")


def dataset_role_populations(dataset: LoadedOfflineQDataset, split_tensors) -> tuple:
    """dataset artifactをsplit単位のroleへ分ける（TRAIN / TEST roleの分離）。"""
    populations = []
    for role in (
        DiagnosisRole.DATASET_TRAIN,
        DiagnosisRole.DATASET_VALIDATION,
        DiagnosisRole.DATASET_TEST,
    ):
        split = _ROLE_SPLIT[role]
        tensors = split_tensors[split]
        populations.append(
            RolePopulation(
                role=role,
                tensors=tensors,
                rows=tuple(dataset.rows[index] for index in tensors.row_indices),
            )
        )
    return tuple(populations)


def _stratified_disagreement(labels, q_top, bc_top, behavior) -> list[dict]:
    strata: dict[str, dict[str, int]] = {}
    for position, label in enumerate(labels):
        entry = strata.setdefault(
            label,
            {
                "row_count": 0,
                "q_vs_bc_disagreement_count": 0,
                "q_vs_behavior_disagreement_count": 0,
                "bc_vs_behavior_disagreement_count": 0,
            },
        )
        entry["row_count"] += 1
        if q_top[position] != bc_top[position]:
            entry["q_vs_bc_disagreement_count"] += 1
        if q_top[position] != behavior[position]:
            entry["q_vs_behavior_disagreement_count"] += 1
        if bc_top[position] != behavior[position]:
            entry["bc_vs_behavior_disagreement_count"] += 1
    return [
        {
            "stratum": label,
            **counts,
            "q_vs_bc_disagreement_rate": _rate(
                counts["q_vs_bc_disagreement_count"], counts["row_count"]
            ),
            "q_vs_behavior_disagreement_rate": _rate(
                counts["q_vs_behavior_disagreement_count"], counts["row_count"]
            ),
            "bc_vs_behavior_disagreement_rate": _rate(
                counts["bc_vs_behavior_disagreement_count"], counts["row_count"]
            ),
        }
        for label, counts in sorted(strata.items())
    ]


def measurement_a(rows, q_top, bc_top, behavior) -> dict[str, object]:
    """Measurement A — same-state Q / BC / behavior top-1 disagreement。"""
    row_count = len(rows)
    q_vs_bc = sum(1 for left, right in zip(q_top, bc_top, strict=True) if left != right)
    q_vs_behavior = sum(
        1 for left, right in zip(q_top, behavior, strict=True) if left != right
    )
    bc_vs_behavior = sum(
        1 for left, right in zip(bc_top, behavior, strict=True) if left != right
    )
    return {
        "eligible_row_count": row_count,
        "q_vs_bc_disagreement_count": q_vs_bc,
        "q_vs_bc_disagreement_rate": _rate(q_vs_bc, row_count),
        "q_vs_behavior_disagreement_count": q_vs_behavior,
        "q_vs_behavior_disagreement_rate": _rate(q_vs_behavior, row_count),
        "bc_vs_behavior_disagreement_count": bc_vs_behavior,
        "bc_vs_behavior_disagreement_rate": _rate(bc_vs_behavior, row_count),
        "stratifications": {
            "legal_action_count": _stratified_disagreement(
                [legal_action_count_bucket(row.legal_action_count) for row in rows],
                q_top,
                bc_top,
                behavior,
            ),
            "transition_terminality": _stratified_disagreement(
                ["terminal" if row.terminal else "nonterminal" for row in rows],
                q_top,
                bc_top,
                behavior,
            ),
            "round_ordinal": _stratified_disagreement(
                [str(row.round_ordinal) for row in rows], q_top, bc_top, behavior
            ),
            "decision_depth": _stratified_disagreement(
                [decision_depth_band(row.decision_ordinal) for row in rows],
                q_top,
                bc_top,
                behavior,
            ),
        },
    }


@dataclass(frozen=True, slots=True)
class QRanking:
    """1 roleのeligible rowについてのQ ranking観測値。"""

    top1_value: list[float]
    top2_value: list[float]
    margin: list[float]
    bc_action_value: list[float]
    behavior_action_value: list[float]
    q_vs_bc_gap: list[float]
    q_vs_behavior_gap: list[float]


def build_q_ranking(q_values, legal_mask, bc_top, behavior) -> QRanking:
    """masked Q valueからtop1 / top2 / margin / selected-action gapを導出する。"""
    import torch

    from .q_network import masked_q_values, q_value_at

    masked = masked_q_values(q_values, legal_mask)
    top = masked.topk(2, dim=-1).values
    top1 = top[:, 0]
    top2 = top[:, 1]
    require_finite(top1, "Q top-1 value")
    require_finite(top2, "Q top-2 value")

    bc_index = torch.tensor(bc_top, dtype=torch.long)
    behavior_index = torch.tensor(behavior, dtype=torch.long)
    bc_value = q_value_at(q_values, bc_index)
    behavior_value = q_value_at(q_values, behavior_index)
    require_finite(bc_value, "Q value of the BC-selected action")
    require_finite(behavior_value, "Q value of the behavior action")

    return QRanking(
        top1_value=top1.tolist(),
        top2_value=top2.tolist(),
        margin=(top1 - top2).tolist(),
        bc_action_value=bc_value.tolist(),
        behavior_action_value=behavior_value.tolist(),
        q_vs_bc_gap=(top1 - bc_value).tolist(),
        q_vs_behavior_gap=(top1 - behavior_value).tolist(),
    )


def _subset(values: list, selector: list[bool]) -> list:
    """`selector`がTrueの位置だけを、同じ順序で取り出す。"""
    return [value for value, keep in zip(values, selector, strict=True) if keep]


def measurement_b(ranking: QRanking, agree: list[bool]) -> dict[str, object]:
    """Measurement B — Q ranking stabilityの固定summary。"""
    disagree = [not value for value in agree]
    fields = {
        "q_top1_value": ranking.top1_value,
        "q_top2_value": ranking.top2_value,
        "q_margin": ranking.margin,
        "q_value_of_bc_action": ranking.bc_action_value,
        "q_value_of_behavior_action": ranking.behavior_action_value,
        "q_selected_vs_bc_selected_gap": ranking.q_vs_bc_gap,
        "q_selected_vs_behavior_gap": ranking.q_vs_behavior_gap,
    }
    return {
        "all_eligible_rows": {
            name: fixed_summary(values) for name, values in fields.items()
        },
        "q_bc_agree_rows": {
            name: fixed_summary(_subset(values, agree))
            for name, values in fields.items()
        },
        "q_bc_disagree_rows": {
            name: fixed_summary(_subset(values, disagree))
            for name, values in fields.items()
        },
    }


def measurement_c(
    rows,
    reward: list[float],
    terminal: list[bool],
    bootstrap_eligible: list[bool],
    td_target: list[float],
    predicted_selected_q: list[float],
    absolute_bellman_residual: list[float],
    agree: list[bool],
) -> dict[str, object]:
    """Measurement C — reward / bootstrap structureの記述的summary。

    TD targetとBellman residualは、next legal actionがすべてTRAIN-supported
    なrow（`bootstrap_eligible`）だけで定義される。terminal rowはbootstrap
    しないため常にeligibleである。
    """
    agree_selector = _subset(agree, bootstrap_eligible)
    disagree_selector = [not value for value in agree_selector]
    terminal_selector = _subset(terminal, bootstrap_eligible)
    nonterminal_selector = [not value for value in terminal_selector]

    def strata(values: list[float]) -> dict[str, object]:
        return {
            "all_bootstrap_eligible_rows": fixed_summary(values),
            "q_bc_agree_rows": fixed_summary(_subset(values, agree_selector)),
            "q_bc_disagree_rows": fixed_summary(_subset(values, disagree_selector)),
            "terminal_rows": fixed_summary(_subset(values, terminal_selector)),
            "nonterminal_rows": fixed_summary(_subset(values, nonterminal_selector)),
        }

    terminal_count = sum(1 for value in terminal if value)
    return {
        "eligible_row_count": len(rows),
        "terminal_row_count": terminal_count,
        "nonterminal_row_count": len(rows) - terminal_count,
        "bootstrap_eligible_row_count": sum(1 for value in bootstrap_eligible if value),
        "unsupported_bootstrap_row_count": sum(
            1 for value in bootstrap_eligible if not value
        ),
        "gamma": GAMMA,
        "td_target_model": TD_TARGET_MODEL,
        "immediate_reward": {
            "all_eligible_rows": fixed_summary(reward),
            "terminal_rows": fixed_summary(
                [value for value, flag in zip(reward, terminal, strict=True) if flag]
            ),
            "nonterminal_rows": fixed_summary(
                [
                    value
                    for value, flag in zip(reward, terminal, strict=True)
                    if not flag
                ]
            ),
            "q_bc_agree_rows": fixed_summary(_subset(reward, agree)),
            "q_bc_disagree_rows": fixed_summary(
                _subset(reward, [not value for value in agree])
            ),
        },
        "td_target": strata(td_target),
        "predicted_selected_q": strata(predicted_selected_q),
        "absolute_bellman_residual": strata(absolute_bellman_residual),
    }


def _hand_progression_arm(progressions: list[HandProgression]) -> dict[str, object]:
    keep = sum(1 for item in progressions if item.keeps_shanten)
    worsen = sum(1 for item in progressions if item.worsens_shanten)
    return {
        "row_count": len(progressions),
        "post_discard_shanten": fixed_summary(
            [float(item.post_discard_shanten) for item in progressions]
        ),
        "keep_shanten_count": keep,
        "keep_shanten_rate": _rate(keep, len(progressions)),
        "worsen_shanten_count": worsen,
        "worsen_shanten_rate": _rate(worsen, len(progressions)),
    }


def _hand_progression_pair(
    left: list[HandProgression], right: list[HandProgression]
) -> dict[str, object]:
    lower = equal = higher = 0
    for one, other in zip(left, right, strict=True):
        if one.post_discard_shanten < other.post_discard_shanten:
            lower += 1
        elif one.post_discard_shanten == other.post_discard_shanten:
            equal += 1
        else:
            higher += 1
    total = len(left)
    return {
        "row_count": total,
        "lower_post_discard_shanten_count": lower,
        "equal_post_discard_shanten_count": equal,
        "higher_post_discard_shanten_count": higher,
        "higher_post_discard_shanten_rate": _rate(higher, total),
        "worsen_shanten_rate_difference": (
            None
            if total == 0
            else (
                sum(1 for item in left if item.worsens_shanten) / total
                - sum(1 for item in right if item.worsens_shanten) / total
            )
        ),
    }


def measurement_d(features, q_top, bc_top, behavior) -> dict[str, object]:
    """Measurement D — player-safe hand progression（導出できる場合だけ）。

    1 rowでもambiguousに復元される場合、推測で埋めずrole全体を`UNAVAILABLE`と
    する。A-Cはこの結果に依存しない。
    """
    arms: dict[str, list[HandProgression]] = {"q": [], "bc": [], "behavior": []}
    try:
        for position in range(len(q_top)):
            progressions = hand_progression_for_row(
                features[position],
                (q_top[position], bc_top[position], behavior[position]),
            )
            q_progression, bc_progression, behavior_progression = progressions
            arms["q"].append(q_progression)
            arms["bc"].append(bc_progression)
            arms["behavior"].append(behavior_progression)
    except OfflineQAmbiguousStateError as error:
        return {
            "status": MeasurementAvailability.UNAVAILABLE.value,
            "unavailable_reason": str(error),
            "post_discard_shanten": None,
            "ukeire": {
                "status": MeasurementAvailability.UNAVAILABLE.value,
                "unavailable_reason": UKEIRE_UNAVAILABLE_REASON,
            },
        }

    return {
        "status": MeasurementAvailability.AVAILABLE.value,
        "unavailable_reason": None,
        "post_discard_shanten": {
            "q": _hand_progression_arm(arms["q"]),
            "bc": _hand_progression_arm(arms["bc"]),
            "behavior": _hand_progression_arm(arms["behavior"]),
            "q_vs_bc": _hand_progression_pair(arms["q"], arms["bc"]),
            "q_vs_behavior": _hand_progression_pair(arms["q"], arms["behavior"]),
        },
        "ukeire": {
            "status": MeasurementAvailability.UNAVAILABLE.value,
            "unavailable_reason": UKEIRE_UNAVAILABLE_REASON,
        },
    }


def diagnose_role(
    population: RolePopulation,
    *,
    bc_model,
    q_model,
    support_mask,
    batch_size: int = 256,
) -> dict[str, object]:
    """1 roleについてMeasurement A-Dを実行し、role documentを返す。"""
    import torch

    from lisjong_arena.learned_policy_stage2.network import masked_argmax

    from .q_network import masked_argmax_q, q_value_at
    from .q_training import compute_td_targets
    from .split_tensors import OfflineQSplitTensors

    tensors = population.tensors
    eligible, counts = select_eligible_rows(tensors, support_mask)
    selector = torch.nonzero(eligible).flatten()
    rows = tuple(
        row
        for row, keep in zip(population.rows, eligible.tolist(), strict=True)
        if keep
    )

    features = tensors.features.index_select(0, selector)
    legal_mask = tensors.legal_mask.index_select(0, selector)
    behavior_index = tensors.behavior_action_index.index_select(0, selector)
    reward = tensors.reward.index_select(0, selector)
    terminal = tensors.terminal.index_select(0, selector)
    next_features = tensors.next_features.index_select(0, selector)
    next_legal_mask = tensors.next_legal_mask.index_select(0, selector)

    q_values = _model_outputs(q_model, features, batch_size)
    bc_logits = _model_outputs(bc_model, features, batch_size)
    require_finite(q_values, "Q model output")
    require_finite(bc_logits, "BC model output")

    q_top = masked_argmax_q(q_values, legal_mask).tolist()
    bc_top = masked_argmax(bc_logits, legal_mask).tolist()
    behavior = behavior_index.tolist()
    agree = [left == right for left, right in zip(q_top, bc_top, strict=True)]

    ranking = build_q_ranking(q_values, legal_mask, bc_top, behavior)

    unsupported_next = (next_legal_mask & ~support_mask.unsqueeze(0)).any(dim=1)
    bootstrap_eligible = terminal | ~unsupported_next
    bootstrap_selector = torch.nonzero(bootstrap_eligible).flatten()
    bootstrap_tensors = OfflineQSplitTensors(
        split=_ROLE_SPLIT[population.role],
        features=features.index_select(0, bootstrap_selector),
        legal_mask=legal_mask.index_select(0, bootstrap_selector),
        behavior_action_index=behavior_index.index_select(0, bootstrap_selector),
        reward=reward.index_select(0, bootstrap_selector),
        terminal=terminal.index_select(0, bootstrap_selector),
        next_features=next_features.index_select(0, bootstrap_selector),
        next_legal_mask=next_legal_mask.index_select(0, bootstrap_selector),
        row_indices=tuple(bootstrap_selector.tolist()),
    )
    td_target = compute_td_targets(q_model, bootstrap_tensors, support_mask)
    require_finite(td_target, "TD target")
    predicted = q_value_at(
        q_values.index_select(0, bootstrap_selector),
        bootstrap_tensors.behavior_action_index,
    )
    require_finite(predicted, "predicted selected-action Q value")
    residual = (predicted - td_target).abs()

    document = {
        "role": population.role.value,
        "source_artifact": _ROLE_SOURCE_ARTIFACT[population.role],
        "split": _ROLE_SPLIT[population.role].value,
        "is_generalization_evidence": _ROLE_GENERALIZATION_EVIDENCE[population.role],
        "row_counts": counts.to_document(),
        "measurement_a": measurement_a(rows, q_top, bc_top, behavior),
        "measurement_b": measurement_b(ranking, agree),
        "measurement_c": measurement_c(
            rows,
            reward.tolist(),
            terminal.tolist(),
            bootstrap_eligible.tolist(),
            td_target.tolist(),
            predicted.tolist(),
            residual.tolist(),
            agree,
        ),
        "measurement_d": measurement_d(features, q_top, bc_top, behavior),
    }
    return document


# --- Result artifact ------------------------------------------------------


def build_diagnosis_result(
    *, binding: DiagnosisInputBinding, roles: list[dict]
) -> dict[str, object]:
    """versioned diagnosis result documentを組み立てる。

    `classification`は常に`None`で作られる。actual outcomeは実artifactに対する
    real executionの後にだけ`record_classification()`で付与する。
    """
    if not isinstance(binding, DiagnosisInputBinding):
        raise TypeError("binding must be a DiagnosisInputBinding")
    if not roles:
        raise _error("a diagnosis result must contain at least one role")
    return {
        "diagnosis_schema_version": DIAGNOSIS_SCHEMA_VERSION,
        "diagnosis_id": DIAGNOSIS_ID,
        "source_issue": SOURCE_ISSUE,
        "predecessor_issue": PREDECESSOR_ISSUE,
        "protocol_id": PROTOCOL_ID,
        "retention": {"backend": RETENTION_BACKEND, "key": RETENTION_KEY},
        "input_artifact_identities": binding.to_document(),
        "locked_source_identities": LOCKED_SOURCE_IDENTITIES.to_document(),
        "feature": feature_block(),
        "vocabulary": vocabulary_block(),
        "fixed_quantiles": [format(value, "g") for value in FIXED_QUANTILES],
        "roles": list(roles),
        "retained_strength_context": RETAINED_STRENGTH_CONTEXT,
        "limitations": list(DIAGNOSIS_LIMITATIONS),
        "classification": None,
    }


_ROLE_FIELDS = {
    "role",
    "source_artifact",
    "split",
    "is_generalization_evidence",
    "row_counts",
    "measurement_a",
    "measurement_b",
    "measurement_c",
    "measurement_d",
}
_RESULT_FIELDS = {
    "diagnosis_schema_version",
    "diagnosis_id",
    "source_issue",
    "predecessor_issue",
    "protocol_id",
    "retention",
    "input_artifact_identities",
    "locked_source_identities",
    "feature",
    "vocabulary",
    "fixed_quantiles",
    "roles",
    "retained_strength_context",
    "limitations",
    "classification",
}
_OUTCOME_VALUES = frozenset(outcome.value for outcome in DiagnosisOutcome)


def _validate_disagreement_block(block: dict, expected_row_count: int) -> None:
    if block["eligible_row_count"] != expected_row_count:
        raise _error("measurement A row count differs from the eligible row count")
    for name in ("q_vs_bc", "q_vs_behavior", "bc_vs_behavior"):
        count = block[f"{name}_disagreement_count"]
        if not 0 <= count <= expected_row_count:
            raise _error(f"measurement A {name} count is out of range")
        if block[f"{name}_disagreement_rate"] != _rate(count, expected_row_count):
            raise _error(f"measurement A {name} rate is not derivable from its counts")
    for name, strata in block["stratifications"].items():
        total = sum(entry["row_count"] for entry in strata)
        if total != expected_row_count:
            raise _error(
                f"measurement A {name} stratification does not partition the "
                "eligible rows"
            )
        for entry in strata:
            for pair in ("q_vs_bc", "q_vs_behavior", "bc_vs_behavior"):
                if entry[f"{pair}_disagreement_rate"] != _rate(
                    entry[f"{pair}_disagreement_count"], entry["row_count"]
                ):
                    raise _error(
                        f"measurement A {name} stratum rate is not derivable from "
                        "its counts"
                    )


def validate_diagnosis_result(document: object) -> dict:
    """result documentを、aggregateをcountsから再導出しながら検証する。"""
    if type(document) is not dict:
        raise _error("a diagnosis result must be an object")
    if set(document) != _RESULT_FIELDS:
        raise _error("diagnosis result has missing or extra fields")
    if document["diagnosis_schema_version"] != DIAGNOSIS_SCHEMA_VERSION:
        raise _error("unsupported diagnosis schema version")
    if document["diagnosis_id"] != DIAGNOSIS_ID:
        raise _error("diagnosis result is not the Issue #152 diagnosis")
    if document["protocol_id"] != PROTOCOL_ID:
        raise _error("diagnosis result protocol_id is not the locked one")
    if document["feature"] != feature_block():
        raise _error("diagnosis result feature identity is not the locked one")
    if document["vocabulary"] != vocabulary_block():
        raise _error("diagnosis result vocabulary identity is not the locked one")
    if document["fixed_quantiles"] != [format(value, "g") for value in FIXED_QUANTILES]:
        raise _error("diagnosis result quantile set is not the locked one")
    if not document["limitations"]:
        raise _error("a diagnosis result must record its limitations")
    if document["retained_strength_context"]["recomputed"] is not False:
        raise _error("the retained strength context must not be recomputed")
    if (
        document["retained_strength_context"]["candidate_only_mahjong_metrics"][
            "is_baseline_difference"
        ]
        is not False
    ):
        raise _error(
            "candidate-only Mahjong metrics must not be labelled as a baseline "
            "difference"
        )

    roles = document["roles"]
    if type(roles) is not list or not roles:
        raise _error("a diagnosis result must contain at least one role")
    seen = set()
    for role in roles:
        if type(role) is not dict or set(role) != _ROLE_FIELDS:
            raise _error("a diagnosis role has missing or extra fields")
        if role["role"] not in {item.value for item in DiagnosisRole}:
            raise _error(f"unknown diagnosis role: {role['role']!r}")
        if role["role"] in seen:
            raise _error("a diagnosis role appears more than once")
        seen.add(role["role"])

        counts = role["row_counts"]
        eligible = counts["eligible_row_count"]
        if not 0 <= eligible <= counts["total_row_count"]:
            raise _error("eligible row count is out of range")
        if counts["excluded_row_count"] != counts["total_row_count"] - eligible:
            raise _error("excluded row count is not derivable from the row counts")
        _validate_disagreement_block(role["measurement_a"], eligible)

        measurement_c_block = role["measurement_c"]
        if measurement_c_block["eligible_row_count"] != eligible:
            raise _error("measurement C row count differs from the eligible row count")
        if (
            measurement_c_block["terminal_row_count"]
            + measurement_c_block["nonterminal_row_count"]
            != eligible
        ):
            raise _error("measurement C terminality counts do not partition the rows")
        if (
            measurement_c_block["bootstrap_eligible_row_count"]
            + measurement_c_block["unsupported_bootstrap_row_count"]
            != eligible
        ):
            raise _error("measurement C bootstrap counts do not partition the rows")

        measurement_d_block = role["measurement_d"]
        status = measurement_d_block["status"]
        if status not in {item.value for item in MeasurementAvailability}:
            raise _error(f"unknown measurement D status: {status!r}")
        if status == MeasurementAvailability.UNAVAILABLE.value:
            if not measurement_d_block["unavailable_reason"]:
                raise _error("an unavailable measurement D must record its reason")
            if measurement_d_block["post_discard_shanten"] is not None:
                raise _error("an unavailable measurement D must not carry summaries")
        elif measurement_d_block["post_discard_shanten"] is None:
            raise _error("an available measurement D must carry its summaries")
        if (
            measurement_d_block["ukeire"]["status"]
            != MeasurementAvailability.UNAVAILABLE.value
        ):
            raise _error(
                "ukeire has no reusable first-party contract and must stay UNAVAILABLE"
            )

    classification = document["classification"]
    if classification is not None and classification not in _OUTCOME_VALUES:
        raise _error(f"unknown diagnosis outcome: {classification!r}")
    return document


def record_classification(document: dict, outcome: DiagnosisOutcome) -> dict:
    """validated resultへexhaustive outcomeを1件だけ記録する。

    Issue #152のladderは「明確なdescriptive patternがあるか」というqualitative
    judgementであり、数値から自動生成しない。ここが機械的に強制するのは:

    - outcomeが`DiagnosisOutcome`のexhaustive集合に属すること
    - 実artifactをstrict readbackした実行結果にだけ付与できること
    - 一度記録したoutcomeを上書きできないこと

    の3点だけである。artifactへアクセスできないことは
    `DIAGNOSTIC EVIDENCE INSUFFICIENT`ではなく、単にoutcome未記録である。
    """
    validated = validate_diagnosis_result(document)
    if not isinstance(outcome, DiagnosisOutcome):
        raise TypeError("outcome must be a DiagnosisOutcome")
    if validated["classification"] is not None:
        raise _error("this diagnosis result already records an outcome")
    if validated["input_artifact_identities"]["real_artifact_execution"] is not True:
        raise _error(
            "a diagnosis outcome may only be recorded for a result produced by a "
            "real strict readback of the retained artifacts"
        )
    return validate_diagnosis_result({**validated, "classification": outcome.value})


__all__ = [
    "DECISION_DEPTH_BAND_EDGES",
    "DIAGNOSIS_ID",
    "DIAGNOSIS_LIMITATIONS",
    "DIAGNOSIS_SCHEMA_VERSION",
    "FIXED_QUANTILES",
    "LOCKED_SOURCE_IDENTITIES",
    "PREDECESSOR_ISSUE",
    "RETAINED_STRENGTH_CONTEXT",
    "RETENTION_BACKEND",
    "RETENTION_KEY",
    "SOURCE_ISSUE",
    "TD_TARGET_MODEL",
    "DiagnosisInputBinding",
    "DiagnosisOutcome",
    "DiagnosisRole",
    "EligibilityCounts",
    "ExpectedArtifactIdentities",
    "QRanking",
    "RolePopulation",
    "bind_diagnosis_inputs",
    "build_diagnosis_result",
    "build_q_ranking",
    "dataset_role_populations",
    "decision_depth_band",
    "diagnose_role",
    "fixed_summary",
    "legal_action_count_bucket",
    "measurement_a",
    "measurement_b",
    "measurement_c",
    "measurement_d",
    "record_classification",
    "require_finite",
    "select_eligible_rows",
    "validate_diagnosis_result",
]
