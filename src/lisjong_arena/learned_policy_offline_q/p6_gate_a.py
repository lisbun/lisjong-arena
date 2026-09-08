"""Issue #181 P6 Gate A orchestration and retained-evidence evaluation.

The scientific run is intentionally separate from implementation/CI. A real run
must be performed from reviewed merged ``main`` after an Issue #181 pre-result lock
has been posted. No game, seed, hanchan, or fresh trajectory is generated here.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
from enum import Enum
from pathlib import Path
from shutil import rmtree
from tempfile import mkdtemp

from lisjong_arena._artifact_io import (
    canonical_json_text,
    read_json_document,
    write_new_artifact_file,
)
from lisjong_arena.learned_policy_stage4a.candidate import resolve_retention_target

from .artifact import load_dataset, vocabulary_block
from .bc_training import load_checkpoint as load_bc_checkpoint
from .diagnosis import (
    LOCKED_SOURCE_IDENTITIES,
    bind_diagnosis_inputs,
    hand_progression_arm_summary,
    hand_progression_pair_summary,
    rate,
    require_finite,
    select_eligible_rows,
)
from .errors import (
    OfflineQAmbiguousStateError,
    OfflineQArtifactError,
    OfflineQProtocolError,
)
from .hand_progression import MeasurementAvailability, hand_progression_for_row
from .p1_candidate import (
    LOCKED_P1_CANDIDATE,
    candidate_binding_document,
    candidate_logical_identity,
    load_p1_serving_checkpoint,
)
from .p1_features import (
    derive_all_split_tensors,
    derive_replacement_test_tensors,
    p1_feature_block,
)
from .p1_gate_a import P1GateARole, P1RolePopulation, verify_derived_alignment
from .p1_q_training import p1_model_block, p1_training_block
from .p6_conservative_q import (
    CQL_ALPHA,
    CQL_TEMPERATURE,
    P6TrainingRun,
    cql_gap,
    load_p6_checkpoint,
    p6_candidate_binding,
    p6_candidate_identity,
    p6_model_block,
    p6_training_block,
    save_p6_checkpoint,
    train_p6_conservative_q,
    verify_p6_protocol_delta,
)
from .protocol import BATCH_SIZE, MAXIMUM_EPOCHS, Split, VOCABULARY_SIZE
from .q_network import masked_argmax_q, q_value_at
from .q_training import load_checkpoint as load_q_checkpoint
from .replacement_test import (
    load_replacement_test,
    load_replacement_test_tensors,
    support_mask_from_checkpoint,
)
from .split_tensors import load_split_tensors
from .support import support_set_identity

P6_GATE_A_LOCK_SCHEMA_VERSION = "arena-learned-policy-p6-gate-a-lock-v1"
P6_GATE_A_RESULT_SCHEMA_VERSION = "arena-learned-policy-p6-gate-a-result-v1"
P6_GATE_A_CLASSIFIED_SCHEMA_VERSION = "arena-learned-policy-p6-gate-a-classified-v1"
P6_GATE_A_ID = "arena-learned-policy-p6-conservative-q-gate-a-181"
SOURCE_ISSUE = "lisbun/lisjong-arena#181"
PARENT_ISSUE = "lisbun/lisjong-project#45"
LOCK_COMMENT_PATTERN = re.compile(
    r"https://github\.com/lisbun/lisjong-arena/issues/181#issuecomment-\d+\Z"
)

PRIMARY_ROLES = (P1GateARole.DATASET_TEST, P1GateARole.REPLACEMENT_TEST)
ALL_ROLES = (
    P1GateARole.DATASET_TRAIN,
    P1GateARole.DATASET_VALIDATION,
    P1GateARole.DATASET_TEST,
    P1GateARole.REPLACEMENT_TEST,
)
ROLE_SOURCE = {
    P1GateARole.DATASET_TRAIN: "dataset",
    P1GateARole.DATASET_VALIDATION: "dataset",
    P1GateARole.DATASET_TEST: "dataset",
    P1GateARole.REPLACEMENT_TEST: "replacement-test",
}
ROLE_SPLIT = {
    P1GateARole.DATASET_TRAIN: Split.TRAIN,
    P1GateARole.DATASET_VALIDATION: Split.VALIDATION,
    P1GateARole.DATASET_TEST: Split.TEST,
    P1GateARole.REPLACEMENT_TEST: Split.TEST,
}

GENERATION_BUDGET = {
    "new_game_generation": 0,
    "new_seed_allocation": 0,
    "new_hanchan": 0,
    "new_trajectory_data": 0,
    "formal_holdout_exposure": 0,
}

LIMITATIONS = (
    "Gate A reuses already-exposed retained dataset TEST and replacement TEST rows; "
    "it is development evidence, not a fresh generalization or strength result.",
    "alpha=0.1 and temperature=1.0 are one pre-registered formulation and are not "
    "claimed to be optimized values.",
    "The conservative penalty may move the candidate toward recorded behavior; "
    "behavior agreement is therefore a mechanism diagnostic and not a strength metric.",
    "Rows within a hanchan are not treated as independent strength samples and no "
    "hanchan-performance claim is made.",
)


class P6GateAOutcome(Enum):
    SIGNAL = "P6 CONSERVATIVE-Q GATE A SIGNAL"
    REGRESSION = "P6 CONSERVATIVE-Q GATE A REGRESSION"
    INCONCLUSIVE = "P6 CONSERVATIVE-Q GATE A INCONCLUSIVE"
    EVIDENCE_INSUFFICIENT = "P6 EVIDENCE INSUFFICIENT"
    STOP_INVALID = "STOP / INVALID"


def _error(message: str) -> OfflineQProtocolError:
    return OfflineQProtocolError(message)


def _sha256_document(document: dict[str, object]) -> str:
    return hashlib.sha256(canonical_json_text(document).encode("utf-8")).hexdigest()


def _git_output(*arguments: str) -> str:
    try:
        completed = subprocess.run(
            ("git", *arguments),
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise _error("Arena Git provenance could not be read") from error
    return completed.stdout.strip()


def require_clean_merged_main() -> str:
    """Return HEAD only when worktree is clean and HEAD equals fetched origin/main."""
    if _git_output("status", "--porcelain"):
        raise _error("P6 scientific execution requires a clean Arena worktree")
    head = _git_output("rev-parse", "HEAD^{commit}")
    origin_main = _git_output("rev-parse", "refs/remotes/origin/main^{commit}")
    if head != origin_main:
        raise _error(
            "P6 scientific execution requires HEAD to equal fetched "
            "refs/remotes/origin/main"
        )
    return head


def _runtime_block() -> dict[str, object]:
    import platform

    import torch

    return {
        "python": platform.python_version(),
        "torch": torch.__version__,
        "device": "cpu",
    }


def _load_inputs(dataset_path, bc_path, q_path, replacement_path, p1_control_path):
    dataset = load_dataset(dataset_path)
    bc = load_bc_checkpoint(bc_path)
    q = load_q_checkpoint(q_path)
    replacement = load_replacement_test(replacement_path)
    binding = bind_diagnosis_inputs(
        dataset=dataset,
        bc_checkpoint=bc,
        q_checkpoint=q,
        replacement_test=replacement,
        expected=LOCKED_SOURCE_IDENTITIES,
    )
    if not binding.real_artifact_execution:
        raise _error("Issue #181 requires the exact locked #140 retained artifacts")
    p1_control = load_p1_serving_checkpoint(p1_control_path)
    if not p1_control.real_candidate_materialization:
        raise _error("Issue #181 requires the exact retained #158 P1 control candidate")
    if p1_control.manifest["canonical_model_weights_digest"] != (
        LOCKED_P1_CANDIDATE.canonical_model_weights_digest
    ):
        raise _error("the P1 control weights digest is not the locked #158 value")
    if p1_control.manifest["supported_indices_digest"] != (
        LOCKED_SOURCE_IDENTITIES.supported_indices_digest
    ):
        raise _error(
            "the P1 control support set is not the exact retained TRAIN support"
        )
    return dataset, bc, q, replacement, binding, p1_control


def _expected_p1_control_binding() -> dict[str, object]:
    """Return the exact #158 serving-control binding required by Issue #181."""
    logical_binding = candidate_binding_document(
        canonical_model_weights_digest=(
            LOCKED_P1_CANDIDATE.canonical_model_weights_digest
        ),
        support_set_digest=LOCKED_P1_CANDIDATE.support_set_digest,
    )
    return {
        "candidate_identity": candidate_logical_identity(logical_binding),
        "canonical_model_weights_digest": (
            LOCKED_P1_CANDIDATE.canonical_model_weights_digest
        ),
        "source_dataset_identity": LOCKED_P1_CANDIDATE.source_dataset_identity,
        "p1_feature": p1_feature_block(),
        "action_vocabulary": vocabulary_block(),
        "supported_indices_digest": LOCKED_P1_CANDIDATE.support_set_digest,
        "model": p1_model_block(),
        "training": p1_training_block(),
    }


def _input_binding_document(binding, p1_control) -> dict[str, object]:
    return {
        "retained_artifacts": binding.to_document(),
        "p1_control": {
            "candidate_identity": p1_control.manifest["candidate_identity"],
            "canonical_model_weights_digest": p1_control.manifest[
                "canonical_model_weights_digest"
            ],
            "source_dataset_identity": p1_control.manifest["source_dataset_identity"],
            "p1_feature": p1_control.manifest["p1_feature"],
            "action_vocabulary": p1_control.manifest["action_vocabulary"],
            "supported_indices_digest": p1_control.manifest["supported_indices_digest"],
            "model": p1_control.manifest["model"],
            "training": p1_control.manifest["training"],
        },
    }


def build_pre_result_lock(
    *,
    dataset_path,
    bc_checkpoint_path,
    q_checkpoint_path,
    replacement_test_path,
    p1_control_path,
    retention_backend: str,
    retention_root,
    retention_key: str,
) -> dict[str, object]:
    """Build a pre-training lock from live exact inputs on merged main."""
    verify_p6_protocol_delta()
    arena_revision = require_clean_merged_main()
    target = resolve_retention_target(
        backend=retention_backend, root=retention_root, key=retention_key
    )
    dataset, _bc, _q, _replacement, binding, p1_control = _load_inputs(
        dataset_path,
        bc_checkpoint_path,
        q_checkpoint_path,
        replacement_test_path,
        p1_control_path,
    )
    document: dict[str, object] = {
        "lock_schema_version": P6_GATE_A_LOCK_SCHEMA_VERSION,
        "experiment_id": P6_GATE_A_ID,
        "source_issue": SOURCE_ISSUE,
        "parent_issue": PARENT_ISSUE,
        "result_exposed": False,
        "arena_revision": arena_revision,
        "runtime": _runtime_block(),
        "inputs": _input_binding_document(binding, p1_control),
        "source_dataset_identity": dataset.identity,
        "p1_feature": p1_feature_block(),
        "action_vocabulary": vocabulary_block(),
        "model": p6_model_block(),
        "base_p1_model": p1_model_block(),
        "base_training": p1_training_block(),
        "p6_training": p6_training_block(),
        "cql_alpha": CQL_ALPHA,
        "cql_temperature": CQL_TEMPERATURE,
        "primary_roles": [role.value for role in PRIMARY_ROLES],
        "classification_rule": {
            "signal": (
                "both primary roles: candidate worsen rate < P1 control; candidate "
                "lower-shanten paired count > higher; candidate behavior top1 "
                "agreement > P1 control"
            ),
            "regression": "both primary roles satisfy the exact reverse directions",
            "otherwise": P6GateAOutcome.INCONCLUSIVE.value,
        },
        "generation_budget": dict(GENERATION_BUDGET),
        "retention": {
            "backend": target.backend,
            "key": target.key,
        },
    }
    document["lock_identity"] = _sha256_document(document)
    return document


def validate_pre_result_lock(document: object) -> dict[str, object]:
    if type(document) is not dict:
        raise _error("P6 pre-result lock must be an object")
    required = {
        "lock_schema_version",
        "experiment_id",
        "source_issue",
        "parent_issue",
        "result_exposed",
        "arena_revision",
        "runtime",
        "inputs",
        "source_dataset_identity",
        "p1_feature",
        "action_vocabulary",
        "model",
        "base_p1_model",
        "base_training",
        "p6_training",
        "cql_alpha",
        "cql_temperature",
        "primary_roles",
        "classification_rule",
        "generation_budget",
        "retention",
        "lock_identity",
    }
    if set(document) != required:
        raise _error("P6 pre-result lock fields are invalid")
    identity = document["lock_identity"]
    logical = {key: value for key, value in document.items() if key != "lock_identity"}
    if type(identity) is not str or identity != _sha256_document(logical):
        raise _error("P6 pre-result lock identity does not match its content")
    if document["lock_schema_version"] != P6_GATE_A_LOCK_SCHEMA_VERSION:
        raise _error("unsupported P6 pre-result lock schema")
    if (
        document["experiment_id"] != P6_GATE_A_ID
        or document["source_issue"] != SOURCE_ISSUE
    ):
        raise _error("P6 pre-result lock does not belong to Issue #181")
    if (
        document["parent_issue"] != PARENT_ISSUE
        or document["result_exposed"] is not False
    ):
        raise _error("P6 pre-result lock parent/result exposure state is invalid")
    if document["p1_feature"] != p1_feature_block():
        raise _error("P6 pre-result lock P1 feature drifted")
    if document["action_vocabulary"] != vocabulary_block():
        raise _error("P6 pre-result lock vocabulary drifted")
    if (
        document["model"] != p6_model_block()
        or document["base_p1_model"] != p1_model_block()
    ):
        raise _error("P6 pre-result lock model block drifted")
    if document["base_training"] != p1_training_block():
        raise _error("P6 pre-result lock base training drifted")
    if document["p6_training"] != p6_training_block():
        raise _error("P6 pre-result lock conservative training block drifted")
    if document["cql_alpha"] != 0.1 or document["cql_temperature"] != 1.0:
        raise _error("P6 pre-result lock alpha/temperature drifted")
    if document["generation_budget"] != GENERATION_BUDGET:
        raise _error("P6 Gate A must not allocate games, seeds, or trajectories")
    if document["primary_roles"] != [role.value for role in PRIMARY_ROLES]:
        raise _error("P6 pre-result lock primary roles drifted")
    inputs = document["inputs"]
    if type(inputs) is not dict or set(inputs) != {"retained_artifacts", "p1_control"}:
        raise _error("P6 pre-result lock input binding is invalid")
    retained = inputs["retained_artifacts"]
    if type(retained) is not dict:
        raise _error("P6 retained input binding is invalid")
    expected_retained = {
        **LOCKED_SOURCE_IDENTITIES.to_document(),
        "real_artifact_execution": True,
    }
    if retained != expected_retained:
        raise _error("P6 pre-result lock does not bind the exact retained artifacts")
    control = inputs["p1_control"]
    if control != _expected_p1_control_binding():
        raise _error(
            "P6 pre-result lock does not bind the exact #158 P1 serving control"
        )
    retention = document["retention"]
    if type(retention) is not dict or set(retention) != {"backend", "key"}:
        raise _error("P6 pre-result lock retention reference is invalid")
    return document


def require_lock_comment_url(url: object) -> str:
    if type(url) is not str or LOCK_COMMENT_PATTERN.fullmatch(url) is None:
        raise _error(
            "P6 run requires the posted Issue #181 pre-result lock comment URL"
        )
    return url


def _model_outputs(model, features, batch_size: int = BATCH_SIZE):
    import torch

    outputs = []
    with torch.inference_mode():
        for start in range(0, int(features.shape[0]), batch_size):
            outputs.append(model(features[start : start + batch_size]).clone())
    if not outputs:
        return torch.zeros((0, VOCABULARY_SIZE), dtype=torch.float32)
    return torch.cat(outputs, dim=0)


def _agreement_count(left: list[int], right: list[int]) -> int:
    return sum(1 for one, other in zip(left, right, strict=True) if one == other)


def _hand_progression_block(features, rows, selections: dict[str, list[int]]) -> dict:
    arms = ("p6", "p1", "bc", "behavior")
    if not rows:
        return {
            "status": MeasurementAvailability.UNAVAILABLE.value,
            "unavailable_reason": "no eligible common-row population",
            "arms": None,
            "p6_vs_p1": None,
            "conditions": None,
        }
    progressions = {arm: [] for arm in arms}
    try:
        for position in range(len(rows)):
            values = hand_progression_for_row(
                features[position],
                tuple(selections[arm][position] for arm in arms),
            )
            for arm, value in zip(arms, values, strict=True):
                progressions[arm].append(value)
    except OfflineQAmbiguousStateError as error:
        return {
            "status": MeasurementAvailability.UNAVAILABLE.value,
            "unavailable_reason": str(error),
            "arms": None,
            "p6_vs_p1": None,
            "conditions": None,
        }
    summaries = {arm: hand_progression_arm_summary(progressions[arm]) for arm in arms}
    pair = hand_progression_pair_summary(progressions["p6"], progressions["p1"])
    p6_behavior_agree = _agreement_count(selections["p6"], selections["behavior"])
    p1_behavior_agree = _agreement_count(selections["p1"], selections["behavior"])
    row_count = len(rows)
    signal_conditions = {
        "worsen_shanten_rate_reduced": (
            summaries["p6"]["worsen_shanten_rate"]
            < summaries["p1"]["worsen_shanten_rate"]
        ),
        "paired_lower_exceeds_higher": (
            pair["lower_post_discard_shanten_count"]
            > pair["higher_post_discard_shanten_count"]
        ),
        "behavior_top1_agreement_increased": p6_behavior_agree > p1_behavior_agree,
    }
    regression_conditions = {
        "worsen_shanten_rate_increased": (
            summaries["p6"]["worsen_shanten_rate"]
            > summaries["p1"]["worsen_shanten_rate"]
        ),
        "paired_higher_exceeds_lower": (
            pair["higher_post_discard_shanten_count"]
            > pair["lower_post_discard_shanten_count"]
        ),
        "behavior_top1_agreement_decreased": p6_behavior_agree < p1_behavior_agree,
    }
    return {
        "status": MeasurementAvailability.AVAILABLE.value,
        "unavailable_reason": None,
        "arms": summaries,
        "p6_vs_p1": pair,
        "conditions": {
            "p6_behavior_top1_agreement_count": p6_behavior_agree,
            "p6_behavior_top1_agreement_rate": rate(p6_behavior_agree, row_count),
            "p1_behavior_top1_agreement_count": p1_behavior_agree,
            "p1_behavior_top1_agreement_rate": rate(p1_behavior_agree, row_count),
            "signal_conditions": signal_conditions,
            "regression_conditions": regression_conditions,
            "signal": all(signal_conditions.values()),
            "regression": all(regression_conditions.values()),
        },
    }


def _q_snapshot_diagnostics(
    model, p1_tensors, selector, support_mask
) -> dict[str, object]:
    """Read-only Q/CQL/TD diagnostics on the exact eligible common rows."""
    import torch

    if int(selector.numel()) == 0:
        return {"status": "UNAVAILABLE", "reason": "no eligible rows"}
    features = p1_tensors.features.index_select(0, selector)
    legal = p1_tensors.legal_mask.index_select(0, selector)
    behavior = p1_tensors.behavior_action_index.index_select(0, selector)
    q_values = _model_outputs(model, features)
    require_finite(q_values, "P6 diagnostic Q output")
    gaps = cql_gap(q_values, legal, behavior, support_mask)
    behavior_q = q_value_at(q_values, behavior)
    action_mask = legal & support_mask.unsqueeze(0)
    masked = q_values.masked_fill(~action_mask, float("-inf"))
    top2 = torch.topk(masked, k=2, dim=1).values
    margins = top2[:, 0] - top2[:, 1]

    reward = p1_tensors.reward.index_select(0, selector)
    terminal = p1_tensors.terminal.index_select(0, selector)
    targets = reward.clone()
    nonterminal = ~terminal
    if bool(nonterminal.any()):
        next_features = p1_tensors.next_features.index_select(0, selector)[nonterminal]
        next_legal = p1_tensors.next_legal_mask.index_select(0, selector)[nonterminal]
        unsupported = next_legal & ~support_mask.unsqueeze(0)
        if bool(unsupported.any()):
            raise _error(
                "an eligible Gate A row has an unsupported next legal action; the "
                "locked TD diagnostic cannot silently change support semantics"
            )
        next_q = _model_outputs(model, next_features)
        require_finite(next_q, "P6 diagnostic next Q output")
        next_masked = next_q.masked_fill(~next_legal, float("-inf"))
        targets[nonterminal] = reward[nonterminal] + next_masked.max(dim=1).values
    huber = torch.nn.functional.huber_loss(
        behavior_q, targets, delta=1.0, reduction="none"
    )

    def summary(values):
        return {
            "count": int(values.numel()),
            "mean": float(values.mean()),
            "minimum": float(values.min()),
            "maximum": float(values.max()),
        }

    alternative = masked.clone()
    alternative.scatter_(1, behavior.unsqueeze(1), float("-inf"))
    alt_gap = alternative.max(dim=1).values - behavior_q
    for name, values in (
        ("cql_gap", gaps),
        ("q_behavior", behavior_q),
        ("max_supported_legal_alternative_minus_behavior", alt_gap),
        ("top1_top2_margin", margins),
        ("selected_action_huber_to_fixed_self_target", huber),
        ("td_target", targets),
    ):
        if not bool(torch.isfinite(values).all()):
            raise _error(f"P6 diagnostic {name} contains a non-finite value")
    return {
        "status": "AVAILABLE",
        "finite_q_rate": 1.0,
        "cql_gap": summary(gaps),
        "q_behavior": summary(behavior_q),
        "max_supported_legal_alternative_minus_behavior": summary(alt_gap),
        "top1_top2_margin": summary(margins),
        "selected_action_huber_to_fixed_self_target": summary(huber),
        "td_target": summary(targets),
    }


def evaluate_p6_role(
    population: P1RolePopulation,
    *,
    p6_model,
    p1_control_model,
    bc_model,
    support_mask,
) -> dict[str, object]:
    """Evaluate P6, exact P1 control, BC and behavior on exactly the same rows."""
    import torch

    from lisjong_arena.learned_policy_stage2.network import masked_argmax

    verify_derived_alignment(population)
    eligible, counts = select_eligible_rows(population.tensors, support_mask)
    selector = torch.nonzero(eligible).flatten()
    rows = tuple(
        row
        for row, keep in zip(population.rows, eligible.tolist(), strict=True)
        if keep
    )
    base_features = population.tensors.features.index_select(0, selector)
    p1_features = population.p1_tensors.features.index_select(0, selector)
    legal = population.tensors.legal_mask.index_select(0, selector)
    behavior = population.tensors.behavior_action_index.index_select(0, selector)

    p6_values = _model_outputs(p6_model, p1_features)
    p1_values = _model_outputs(p1_control_model, p1_features)
    bc_logits = _model_outputs(bc_model, base_features)
    require_finite(p6_values, "P6 Q output")
    require_finite(p1_values, "P1 control Q output")
    require_finite(bc_logits, "retained BC output")
    selections = {
        "p6": masked_argmax_q(p6_values, legal).tolist(),
        "p1": masked_argmax_q(p1_values, legal).tolist(),
        "bc": masked_argmax(bc_logits, legal).tolist(),
        "behavior": behavior.tolist(),
    }
    row_count = len(rows)
    p6_p1_agree = _agreement_count(selections["p6"], selections["p1"])
    return {
        "role": population.role.value,
        "source_artifact": ROLE_SOURCE[population.role],
        "split": ROLE_SPLIT[population.role].value,
        "is_primary_role": population.role in PRIMARY_ROLES,
        "is_generalization_evidence": False,
        "row_counts": counts.to_document(),
        "derived_coverage": population.coverage.to_document(),
        "common_row_identity": True,
        "action_comparison": {
            "eligible_row_count": row_count,
            "p6_vs_p1_disagreement_count": row_count - p6_p1_agree,
            "p6_vs_p1_disagreement_rate": rate(row_count - p6_p1_agree, row_count),
            "p6_vs_behavior_agreement_count": _agreement_count(
                selections["p6"], selections["behavior"]
            ),
            "p1_vs_behavior_agreement_count": _agreement_count(
                selections["p1"], selections["behavior"]
            ),
        },
        "hand_progression": _hand_progression_block(base_features, rows, selections),
        "conservative_q_diagnostics": _q_snapshot_diagnostics(
            p6_model, population.p1_tensors, selector, support_mask
        ),
    }


def _build_populations(dataset, replacement):
    split_tensors = load_split_tensors(dataset)
    p1_splits, coverage = derive_all_split_tensors(split_tensors)
    populations = [
        P1RolePopulation(
            role=role,
            tensors=split_tensors[split],
            p1_tensors=p1_splits[split],
            rows=tuple(
                dataset.rows[index] for index in split_tensors[split].row_indices
            ),
            coverage=coverage[split],
        )
        for role, split in (
            (P1GateARole.DATASET_TRAIN, Split.TRAIN),
            (P1GateARole.DATASET_VALIDATION, Split.VALIDATION),
            (P1GateARole.DATASET_TEST, Split.TEST),
        )
    ]
    replacement_tensors = load_replacement_test_tensors(replacement)
    p1_replacement, replacement_coverage = derive_replacement_test_tensors(
        replacement_tensors
    )
    populations.append(
        P1RolePopulation(
            role=P1GateARole.REPLACEMENT_TEST,
            tensors=replacement_tensors,
            p1_tensors=p1_replacement,
            rows=replacement.rows,
            coverage=replacement_coverage,
        )
    )
    return p1_splits, populations


def _candidate_document(checkpoint) -> dict[str, object]:
    return {
        "candidate_identity": checkpoint.manifest["candidate_identity"],
        "canonical_model_weights_digest": checkpoint.manifest[
            "canonical_model_weights_digest"
        ],
        "source_dataset_identity": checkpoint.manifest["source_dataset_identity"],
        "supported_indices_digest": checkpoint.manifest["supported_indices_digest"],
        "model": checkpoint.manifest["model"],
        "training": checkpoint.manifest["training"],
        "selected_epoch": checkpoint.manifest["selected_epoch"],
    }


def build_gate_a_result(
    *, lock: dict[str, object], lock_comment_url: str, p6_checkpoint, roles
) -> dict[str, object]:
    validate_pre_result_lock(lock)
    require_lock_comment_url(lock_comment_url)
    document = {
        "result_schema_version": P6_GATE_A_RESULT_SCHEMA_VERSION,
        "experiment_id": P6_GATE_A_ID,
        "source_issue": SOURCE_ISSUE,
        "parent_issue": PARENT_ISSUE,
        "lock_identity": lock["lock_identity"],
        "lock_comment_url": lock_comment_url,
        "input_binding": lock["inputs"],
        "p1_feature": p1_feature_block(),
        "action_vocabulary": vocabulary_block(),
        "base_training": p1_training_block(),
        "p6_training": p6_training_block(),
        "candidate": _candidate_document(p6_checkpoint),
        "generation_budget": dict(GENERATION_BUDGET),
        "primary_roles": [role.value for role in PRIMARY_ROLES],
        "roles": list(roles),
        "limitations": list(LIMITATIONS),
        "classification": None,
    }
    document["result_identity"] = _sha256_document(document)
    return document


def _conditions_from_role(role: dict[str, object]) -> tuple[bool, bool] | None:
    hand = role.get("hand_progression")
    if (
        type(hand) is not dict
        or hand.get("status") != MeasurementAvailability.AVAILABLE.value
    ):
        return None
    arms = hand.get("arms")
    pair = hand.get("p6_vs_p1")
    conditions = hand.get("conditions")
    if type(arms) is not dict or type(pair) is not dict or type(conditions) is not dict:
        raise _error("P6 role hand-progression structure is invalid")
    p6 = arms.get("p6")
    p1 = arms.get("p1")
    if type(p6) is not dict or type(p1) is not dict:
        raise _error("P6 role arm summaries are invalid")
    signal = (
        p6["worsen_shanten_rate"] < p1["worsen_shanten_rate"]
        and pair["lower_post_discard_shanten_count"]
        > pair["higher_post_discard_shanten_count"]
        and conditions["p6_behavior_top1_agreement_count"]
        > conditions["p1_behavior_top1_agreement_count"]
    )
    regression = (
        p6["worsen_shanten_rate"] > p1["worsen_shanten_rate"]
        and pair["higher_post_discard_shanten_count"]
        > pair["lower_post_discard_shanten_count"]
        and conditions["p6_behavior_top1_agreement_count"]
        < conditions["p1_behavior_top1_agreement_count"]
    )
    if (
        conditions.get("signal") is not signal
        or conditions.get("regression") is not regression
    ):
        raise _error("P6 role recorded conditions are not derivable from its metrics")
    return signal, regression


def validate_gate_a_result(document: object) -> dict[str, object]:
    if type(document) is not dict:
        raise _error("P6 Gate A result must be an object")
    required = {
        "result_schema_version",
        "experiment_id",
        "source_issue",
        "parent_issue",
        "lock_identity",
        "lock_comment_url",
        "input_binding",
        "p1_feature",
        "action_vocabulary",
        "base_training",
        "p6_training",
        "candidate",
        "generation_budget",
        "primary_roles",
        "roles",
        "limitations",
        "classification",
        "result_identity",
    }
    if set(document) != required:
        raise _error("P6 Gate A result fields are invalid")
    logical = {
        key: value for key, value in document.items() if key != "result_identity"
    }
    if document["result_identity"] != _sha256_document(logical):
        raise _error("P6 Gate A result identity does not match its content")
    if document["result_schema_version"] != P6_GATE_A_RESULT_SCHEMA_VERSION:
        raise _error("unsupported P6 Gate A result schema")
    if (
        document["experiment_id"] != P6_GATE_A_ID
        or document["source_issue"] != SOURCE_ISSUE
    ):
        raise _error("P6 Gate A result does not belong to Issue #181")
    require_lock_comment_url(document["lock_comment_url"])
    if document["parent_issue"] != PARENT_ISSUE:
        raise _error("P6 Gate A result parent issue drifted")
    lock_identity = document["lock_identity"]
    if type(lock_identity) is not str or len(lock_identity) != 64:
        raise _error("P6 Gate A result lock identity is not a sha256 digest")
    input_binding = document["input_binding"]
    if type(input_binding) is not dict or set(input_binding) != {
        "retained_artifacts",
        "p1_control",
    }:
        raise _error("P6 Gate A result input binding is invalid")
    expected_retained = {
        **LOCKED_SOURCE_IDENTITIES.to_document(),
        "real_artifact_execution": True,
    }
    if input_binding["retained_artifacts"] != expected_retained:
        raise _error("P6 Gate A result retained artifact binding drifted")
    if input_binding["p1_control"] != _expected_p1_control_binding():
        raise _error("P6 Gate A result P1 control binding drifted")
    if (
        document["p1_feature"] != p1_feature_block()
        or document["action_vocabulary"] != vocabulary_block()
    ):
        raise _error("P6 Gate A result feature/vocabulary drifted")
    if (
        document["base_training"] != p1_training_block()
        or document["p6_training"] != p6_training_block()
    ):
        raise _error("P6 Gate A result training contract drifted")
    if document["generation_budget"] != GENERATION_BUDGET:
        raise _error("P6 Gate A result generation budget is not zero")
    if document["primary_roles"] != [role.value for role in PRIMARY_ROLES]:
        raise _error("P6 Gate A result primary roles drifted")
    if document["limitations"] != list(LIMITATIONS):
        raise _error("P6 Gate A result limitations drifted")
    candidate = document["candidate"]
    if type(candidate) is not dict:
        raise _error("P6 candidate binding is invalid")
    if (
        candidate.get("model") != p6_model_block()
        or candidate.get("training") != p6_training_block()
    ):
        raise _error("P6 candidate model/training binding drifted")
    if (
        candidate.get("source_dataset_identity")
        != LOCKED_SOURCE_IDENTITIES.dataset_identity
    ):
        raise _error("P6 candidate source dataset is not the locked retained dataset")
    if (
        candidate.get("supported_indices_digest")
        != LOCKED_SOURCE_IDENTITIES.supported_indices_digest
    ):
        raise _error("P6 candidate support digest is not the locked retained support")
    weights_digest = candidate.get("canonical_model_weights_digest")
    if type(weights_digest) is not str or len(weights_digest) != 64:
        raise _error("P6 candidate weights digest is invalid")
    expected_candidate_identity = p6_candidate_identity(
        p6_candidate_binding(
            source_dataset_identity=LOCKED_SOURCE_IDENTITIES.dataset_identity,
            supported_indices_digest=(
                LOCKED_SOURCE_IDENTITIES.supported_indices_digest
            ),
            weights_digest=weights_digest,
        )
    )
    if candidate.get("candidate_identity") != expected_candidate_identity:
        raise _error("P6 candidate identity is not derivable from its locked binding")
    if candidate.get("selected_epoch") != MAXIMUM_EPOCHS:
        raise _error("P6 candidate selected epoch is not the fixed final iteration")
    roles = document["roles"]
    if type(roles) is not list or len(roles) != len(ALL_ROLES):
        raise _error("P6 Gate A result must contain all four roles")
    seen = set()
    for role in roles:
        if type(role) is not dict or role.get("role") in seen:
            raise _error("P6 Gate A role is malformed or duplicated")
        seen.add(role.get("role"))
        try:
            role_name = P1GateARole(role["role"])
        except (KeyError, ValueError) as error:
            raise _error("P6 Gate A role name is invalid") from error
        if (
            role.get("source_artifact") != ROLE_SOURCE[role_name]
            or role.get("split") != ROLE_SPLIT[role_name].value
        ):
            raise _error("P6 Gate A role source/split drifted")
        if role.get("is_primary_role") is not (role_name in PRIMARY_ROLES):
            raise _error("P6 Gate A role primary flag drifted")
        if (
            role.get("is_generalization_evidence") is not False
            or role.get("common_row_identity") is not True
        ):
            raise _error("P6 Gate A role evidence/common-row flags are invalid")
        comparison = role.get("action_comparison")
        if type(comparison) is not dict:
            raise _error("P6 Gate A action comparison is invalid")
        count = comparison.get("eligible_row_count")
        if type(count) is not int or count < 0:
            raise _error("P6 Gate A eligible row count is invalid")
        disagreements = comparison.get("p6_vs_p1_disagreement_count")
        if type(disagreements) is not int or not 0 <= disagreements <= count:
            raise _error("P6 Gate A disagreement count is invalid")
        if comparison.get("p6_vs_p1_disagreement_rate") != rate(disagreements, count):
            raise _error("P6 Gate A disagreement rate is not derivable")
        for key in (
            "p6_vs_behavior_agreement_count",
            "p1_vs_behavior_agreement_count",
        ):
            value = comparison.get(key)
            if type(value) is not int or not 0 <= value <= count:
                raise _error(f"P6 Gate A {key} is invalid")
        _conditions_from_role(role)
    if seen != {role.value for role in ALL_ROLES}:
        raise _error("P6 Gate A role set is incomplete")
    classification = document["classification"]
    if classification is not None and classification not in {
        outcome.value for outcome in P6GateAOutcome
    }:
        raise _error("P6 Gate A classification is invalid")
    return document


def derive_classification(document: object) -> P6GateAOutcome:
    validated = validate_gate_a_result(document)
    by_role = {role["role"]: role for role in validated["roles"]}
    directions = []
    for role in PRIMARY_ROLES:
        condition = _conditions_from_role(by_role[role.value])
        if condition is None:
            return P6GateAOutcome.EVIDENCE_INSUFFICIENT
        directions.append(condition)
    if all(signal for signal, _regression in directions):
        return P6GateAOutcome.SIGNAL
    if all(regression for _signal, regression in directions):
        return P6GateAOutcome.REGRESSION
    return P6GateAOutcome.INCONCLUSIVE


def classified_document(document: object) -> dict[str, object]:
    validated = validate_gate_a_result(document)
    if validated["classification"] is not None:
        raise _error("P6 Gate A result is already classified")
    outcome = derive_classification(validated)
    classified = dict(validated)
    classified["result_schema_version"] = P6_GATE_A_CLASSIFIED_SCHEMA_VERSION
    classified["classification"] = outcome.value
    classified.pop("result_identity")
    classified["result_identity"] = _sha256_document(classified)
    return classified


def _validate_live_lock_against_inputs(lock, binding, p1_control, arena_revision):
    if lock["arena_revision"] != arena_revision:
        raise _error("Arena HEAD no longer matches the posted P6 pre-result lock")
    if lock["runtime"] != _runtime_block():
        raise _error("runtime no longer matches the posted P6 pre-result lock")
    if lock["inputs"] != _input_binding_document(binding, p1_control):
        raise _error("input artifacts no longer match the posted P6 pre-result lock")


def run_locked_gate_a(
    *,
    lock_document: object,
    lock_comment_url: str,
    dataset_path,
    bc_checkpoint_path,
    q_checkpoint_path,
    replacement_test_path,
    p1_control_path,
    retention_root,
) -> tuple[dict[str, object], dict[str, object]]:
    """Execute one locked P6 training/Gate A result without any game generation."""
    import torch

    lock = validate_pre_result_lock(lock_document)
    require_lock_comment_url(lock_comment_url)
    arena_revision = require_clean_merged_main()
    retention = lock["retention"]
    target = resolve_retention_target(
        backend=retention["backend"], root=retention_root, key=retention["key"]
    )
    dataset, bc, _q, replacement, binding, p1_control = _load_inputs(
        dataset_path,
        bc_checkpoint_path,
        q_checkpoint_path,
        replacement_test_path,
        p1_control_path,
    )
    _validate_live_lock_against_inputs(lock, binding, p1_control, arena_revision)
    p1_splits, populations = _build_populations(dataset, replacement)

    training_run: P6TrainingRun = train_p6_conservative_q(p1_splits)
    trained_support_digest = support_set_identity(
        sorted(
            int(index)
            for index in torch.nonzero(training_run.support_mask).flatten().tolist()
        )
    )
    if trained_support_digest != LOCKED_SOURCE_IDENTITIES.supported_indices_digest:
        raise _error(
            "P6 candidate TRAIN support differs from exact #158 control support"
        )

    target.bundle_path.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(mkdtemp(prefix=".p6-181-staging-", dir=target.bundle_path.parent))
    published = False
    try:
        checkpoint = save_p6_checkpoint(staging / "checkpoint", dataset, training_run)
        support_mask = support_mask_from_checkpoint(p1_control.supported_indices)
        roles = [
            evaluate_p6_role(
                population,
                p6_model=checkpoint.model,
                p1_control_model=p1_control.model,
                bc_model=bc.model,
                support_mask=support_mask,
            )
            for population in populations
        ]
        result = build_gate_a_result(
            lock=lock,
            lock_comment_url=lock_comment_url,
            p6_checkpoint=checkpoint,
            roles=roles,
        )
        validate_gate_a_result(result)
        write_new_artifact_file(
            staging / "gate-a-result.json", canonical_json_text(result)
        )
        staging.rename(target.bundle_path)
        published = True
    finally:
        if not published:
            rmtree(staging, ignore_errors=True)

    strict_checkpoint = load_p6_checkpoint(target.bundle_path / "checkpoint")
    if strict_checkpoint.candidate_identity != checkpoint.candidate_identity:
        raise OfflineQArtifactError("P6 checkpoint strict readback identity differs")
    strict_result = read_json_document(target.bundle_path / "gate-a-result.json")
    validate_gate_a_result(strict_result)
    classified = classified_document(strict_result)
    write_new_artifact_file(
        target.bundle_path / "gate-a-classified.json", canonical_json_text(classified)
    )
    strict_classified = read_json_document(
        target.bundle_path / "gate-a-classified.json"
    )
    if strict_classified != classified:
        raise OfflineQArtifactError("P6 classified result strict readback differs")
    return strict_result, classified


def _write_lock(path: Path, document: dict[str, object]) -> None:
    if not path.parent.is_dir():
        raise _error("lock output parent directory must already exist")
    write_new_artifact_file(path, canonical_json_text(document))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Issue #181 P6 Gate A protocol")
    subparsers = parser.add_subparsers(dest="command", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--dataset", required=True)
    common.add_argument("--bc-checkpoint", required=True)
    common.add_argument("--q-checkpoint", required=True)
    common.add_argument("--replacement-test", required=True)
    common.add_argument("--p1-control", required=True)
    common.add_argument("--retention-root", required=True)

    lock = subparsers.add_parser("lock", parents=[common])
    lock.add_argument("--retention-backend", default="operator-local-durable")
    lock.add_argument("--retention-key", default="offlineq-181-p6-gate-a")
    lock.add_argument("--lock-output", required=True)

    run = subparsers.add_parser("run", parents=[common])
    run.add_argument("--lock-file", required=True)
    run.add_argument("--lock-comment-url", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "lock":
            document = build_pre_result_lock(
                dataset_path=arguments.dataset,
                bc_checkpoint_path=arguments.bc_checkpoint,
                q_checkpoint_path=arguments.q_checkpoint,
                replacement_test_path=arguments.replacement_test,
                p1_control_path=arguments.p1_control,
                retention_backend=arguments.retention_backend,
                retention_root=arguments.retention_root,
                retention_key=arguments.retention_key,
            )
            _write_lock(Path(arguments.lock_output), document)
            print(canonical_json_text(document), end="")
            return 0
        lock_document = read_json_document(Path(arguments.lock_file))
        result, classified = run_locked_gate_a(
            lock_document=lock_document,
            lock_comment_url=arguments.lock_comment_url,
            dataset_path=arguments.dataset,
            bc_checkpoint_path=arguments.bc_checkpoint,
            q_checkpoint_path=arguments.q_checkpoint,
            replacement_test_path=arguments.replacement_test,
            p1_control_path=arguments.p1_control,
            retention_root=arguments.retention_root,
        )
        print(f"result identity: {result['result_identity']}")
        print(f"classification: {classified['classification']}")
        return 0
    except Exception as error:  # CLI boundary preserves the exact exception in stderr.
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ALL_ROLES",
    "GENERATION_BUDGET",
    "LIMITATIONS",
    "P6_GATE_A_CLASSIFIED_SCHEMA_VERSION",
    "P6_GATE_A_ID",
    "P6_GATE_A_LOCK_SCHEMA_VERSION",
    "P6_GATE_A_RESULT_SCHEMA_VERSION",
    "P6GateAOutcome",
    "PRIMARY_ROLES",
    "ROLE_SOURCE",
    "ROLE_SPLIT",
    "build_gate_a_result",
    "build_pre_result_lock",
    "classified_document",
    "derive_classification",
    "evaluate_p6_role",
    "main",
    "require_clean_merged_main",
    "require_lock_comment_url",
    "run_locked_gate_a",
    "validate_gate_a_result",
    "validate_pre_result_lock",
]
