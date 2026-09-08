"""Issue #185 exact P6 conservative-Q vs revision-bound yakuhai-call x3.

This purpose-specific orchestration combines the exact Issue #183 P6 serving
family with the Issue #179 higher-fidelity comparator semantics.  Historical
schemas and provenance validators remain unchanged: in particular, this module
does not import Issue #179's later-package provenance validator.
"""

from __future__ import annotations

import argparse
import hashlib
import math
import os
import re
import sys
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from lisjong.policies import (
    YakuhaiCallGenbutsuDefenseFiniteHorizonHandValueAwarePolicy,
)

from lisjong_arena._artifact_io import (
    canonical_json_text,
    read_json_document,
    write_new_artifact_file,
)
from lisjong_arena.model import (
    SINGLE_ROUND_GAME_MODE,
    SINGLE_ROUND_ROTATION_COUNT,
    PolicySpec,
    SingleRoundEvaluationPlan,
)
from lisjong_arena.policy_catalog import POLICY_CATALOG, create_yakuhai_call
from lisjong_arena.single_round_artifact import (
    SINGLE_ROUND_ARTIFACT_SCHEMA_VERSION,
    SINGLE_ROUND_EVALUATION_PROTOCOL,
    SingleRoundExecutionProvenance,
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
    aggregate_seat_round_stats_metrics,
    run_single_round_evaluation,
    summarize_single_round_strength,
)

from .errors import OfflineQError
from .p1_serving import (
    create_p1_hybrid_runtime,
    fallback_policy_block,
    hybrid_activation_block,
)
from .p1_shanten_guard_higher_fidelity import (
    _git_output,
    _mahjong_metrics,
    _require_clean_arena_head,
    _resolve_execution_target_revision,
    _validate_mahjong_metrics,
)
from .p1_shanten_guard_higher_fidelity_successor import (
    DEFAULT_ORDERED_SEEDS as ISSUE_179_ORDERED_SEEDS,
)
from .p6_conservative_q import LoadedP6Checkpoint, p6_training_block
from .p6_gate_b import (
    DEFAULT_ORDERED_SEEDS as ISSUE_183_ORDERED_SEEDS,
)
from .p6_gate_b import (
    EXPECTED_CANDIDATE_IDENTITY,
    EXPECTED_GATE_A_CLASSIFICATION,
    EXPECTED_GATE_A_CLASSIFIED_IDENTITY,
    EXPECTED_GATE_A_RESULT_IDENTITY,
    GateAEvidence,
    _expected_candidate_block,
    load_gate_a_evidence,
)
from .p6_gate_b import (
    declared_allocated_seeds as declared_before_185,
)
from .p6_gate_b import (
    derive_classification as derive_gate_b_classification,
)
from .p6_gate_b import (
    record_classification as record_gate_b_classification,
)
from .p6_gate_b import (
    validate_result as validate_gate_b_result,
)
from .strength import (
    ActivationDiagnostics,
    PolicyInstanceRegistry,
    collect_activation_diagnostics,
)

LOCK_SCHEMA_VERSION = (
    "arena-learned-policy-p6-conservative-q-higher-fidelity-185-lock-v1"
)
RESULT_SCHEMA_VERSION = (
    "arena-learned-policy-p6-conservative-q-higher-fidelity-185-result-v1"
)
CLASSIFIED_RESULT_SCHEMA_VERSION = (
    "arena-learned-policy-p6-conservative-q-higher-fidelity-185-classified-v1"
)
EXPERIMENT_ID = "arena-learned-policy-p6-conservative-q-higher-fidelity-185"
SOURCE_ISSUE = "lisbun/lisjong-arena#185"
SOURCE_PR = "lisbun/lisjong-arena#187"
PARENT_ISSUE = "lisbun/lisjong-project#45"
PREDECESSOR_ISSUES = (
    "lisbun/lisjong-arena#181",
    "lisbun/lisjong-arena#183",
    "lisbun/lisjong-arena#179",
    "lisbun/lisjong-arena#177",
)

P6_LISJONG_REVISION = "99a30c267a3c3e301e132c8799726eb10e012a95"
P6_ENGINE_REVISION = "8735e89e1aea000ab59368d0368d476787827741"
PYTHON_VERSION = "3.14.6"
TORCH_VERSION = "2.13.0+cpu"
RIICHIENV_VERSION = "0.4.8"

EXPECTED_GATE_B_LOCK_IDENTITY = (
    "2dcb63b7b086147a8c293e5702a671fd3097289d8a5beedb481651e9aa429993"
)
EXPECTED_GATE_B_RESULT_IDENTITY = (
    "5f051385f88853d14cb9a38e7434e3537203d4110460f9c24149e6e6ecc33923"
)
EXPECTED_GATE_B_ARTIFACT_SHA256 = (
    "4b116043745a87e032d807889969797dcdc0e8032db2c1633d7e3f9b882467b6"
)
EXPECTED_GATE_B_CLASSIFICATION = "P6 CONSERVATIVE-Q GATE B POSITIVE SIGNAL"

BASELINE_IDENTITY = "yakuhai-call"
BASELINE_FACTORY = "lisjong_arena.policy_catalog.create_yakuhai_call"
BASELINE_IMPLEMENTATION_CLASS = (
    "YakuhaiCallGenbutsuDefenseFiniteHorizonHandValueAwarePolicy"
)
BASELINE_IMPLEMENTATION_MODULE = (
    "lisjong.policies.yakuhai_call_genbutsu_defense_finite_horizon_hand_value_aware"
)
BASELINE_IMPLEMENTATION_SOURCE_REVISION = "a0666d24e66179a45fd6e231a3cbd489b492d162"

DEFAULT_ORDERED_SEEDS = tuple(range(622, 647))
SEED_BLOCK_COUNT = 25
ROTATIONS_PER_SEED = SINGLE_ROUND_ROTATION_COUNT
GAME_COUNT = SEED_BLOCK_COUNT * ROTATIONS_PER_SEED
GAME_MODE = SINGLE_ROUND_GAME_MODE
MAX_STEPS = 10_000
MAX_WORKERS = 1
FORMAL_TEST = False
ROLE = "DEVELOPMENT P6 HIGHER-FIDELITY SCREEN"

RETENTION_BACKEND = "operator-local-durable"
ARTIFACT_RETENTION_KEY = "offlineq-185-p6-higher-fidelity/strength-artifact"
RESULT_RETENTION_KEY = "offlineq-185-p6-higher-fidelity/result"
CLASSIFIED_RESULT_RETENTION_KEY = "offlineq-185-p6-higher-fidelity/classified-result"
PRIMARY_CHANGED_AXIS = "passive tsumogiri x3 to exact yakuhai-call x3"
LOCKED_UNCHANGED_AXES = (
    "exact #181/#183 P6 checkpoint and model weights",
    "P1 8241 feature and exact TRAIN support",
    "action vocabulary and legal-masked Q argmax semantics",
    "unguarded hybrid activation and exact yakuhai-call fallback",
    "#183 dependency family",
    "training, reward, gamma, architecture, and selected epoch",
)
CLASSIFICATION_RULE = {
    "primary_metric": "seed-block P6-vs-yakuhai-call score delta",
    "interval": "normal-approx 95% interval over 25 seed blocks",
    "positive": "normal_approx_95_interval_lower > 0",
    "negative": "normal_approx_95_interval_upper < 0",
    "inconclusive": "otherwise",
    "secondary_metrics_may_alter_classification": False,
    "serving_diagnostics_may_alter_classification": False,
}
NO_RESCUE_BOUNDARY = (
    "After Issue #185 result exposure there is no seed extension or replacement, "
    "rerun, retraining, alpha/temperature change, #173 guard insertion, feature/"
    "support/reward/gamma change, candidate/fallback/baseline/runtime substitution, "
    "additional arm, or hanchan escalation."
)
LIMITATIONS = (
    "This is a fresh development single-round screen, not a formal holdout.",
    "The primary samples are 25 paired four-rotation seed blocks, not 100 candidate "
    "and 300 baseline seat-rounds treated as independent samples.",
    "A signal does not establish hanchan strength, production readiness, P6-vs-P1 "
    "causality, CQL optimality, or superiority over every existing Policy.",
)

_LOCK_COMMENT_URL = re.compile(
    r"https://github\.com/lisbun/lisjong-arena/issues/185#issuecomment-\d+\Z"
)
_OUTPUT_NAMES = ("strength_artifact", "result", "classified_result")


class P6HigherFidelityError(OfflineQError):
    """Issue #185 lock, execution, artifact, or result contract violation."""


class P6HigherFidelityOutcome(Enum):
    SIGNAL = "P6 HIGHER-FIDELITY SIGNAL"
    NEGATIVE = "P6 HIGHER-FIDELITY NEGATIVE"
    INCONCLUSIVE = "P6 HIGHER-FIDELITY INCONCLUSIVE"
    EVIDENCE_BLOCKED = "P6 HIGHER-FIDELITY EVIDENCE BLOCKED"
    STOP_INVALID = "STOP / INVALID"


_RESULT_OUTCOMES = frozenset(
    {
        P6HigherFidelityOutcome.SIGNAL,
        P6HigherFidelityOutcome.NEGATIVE,
        P6HigherFidelityOutcome.INCONCLUSIVE,
    }
)


def _error(message: str) -> P6HigherFidelityError:
    return P6HigherFidelityError(message)


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sha256_document(document: dict[str, object]) -> str:
    return hashlib.sha256(canonical_json_text(document).encode()).hexdigest()


def runtime_block() -> dict[str, object]:
    from .p1_shanten_guard_higher_fidelity import runtime_block as live_runtime_block

    return live_runtime_block()


def require_runtime(actual: object) -> None:
    expected = {
        "python_version": PYTHON_VERSION,
        "torch_version": TORCH_VERSION,
        "riichienv_version": RIICHIENV_VERSION,
    }
    if actual != expected:
        raise _error("runtime versions differ from the exact #183 dependency family")


def baseline_block() -> dict[str, object]:
    """Bind yakuhai-call semantics without importing #179 package provenance."""
    spec = POLICY_CATALOG.get(BASELINE_IDENTITY)
    if spec is None or spec.factory is not create_yakuhai_call:
        raise _error("the curated yakuhai-call factory binding drifted")
    policy = create_yakuhai_call()
    if type(policy) is not YakuhaiCallGenbutsuDefenseFiniteHorizonHandValueAwarePolicy:
        raise _error("the yakuhai-call implementation class drifted")
    if type(policy).__module__ != BASELINE_IMPLEMENTATION_MODULE:
        raise _error("the yakuhai-call implementation module drifted")
    return {
        "identity": BASELINE_IDENTITY,
        "factory": BASELINE_FACTORY,
        "implementation_class": BASELINE_IMPLEMENTATION_CLASS,
        "implementation_module": BASELINE_IMPLEMENTATION_MODULE,
        "implementation_source_revision": BASELINE_IMPLEMENTATION_SOURCE_REVISION,
        "selected_lisjong_revision": P6_LISJONG_REVISION,
        "mutable_alias": False,
    }


def require_p6_higher_fidelity_provenance(
    provenance: SingleRoundExecutionProvenance,
) -> None:
    if not isinstance(provenance, SingleRoundExecutionProvenance):
        raise TypeError("provenance must be a SingleRoundExecutionProvenance")
    baseline_block()
    if provenance.lisjong_revision != P6_LISJONG_REVISION:
        raise _error("installed lisjong revision is not the exact #183 revision")
    if provenance.lisjong_engine_revision != P6_ENGINE_REVISION:
        raise _error("installed lisjong-engine revision is not the exact #183 revision")
    if provenance.python_version != PYTHON_VERSION:
        raise _error("provenance Python version is not the exact #183 version")
    if provenance.riichienv_version != RIICHIENV_VERSION:
        raise _error("provenance RiichiEnv version is not the exact #183 version")


def declared_allocated_seeds() -> frozenset[int]:
    return (
        frozenset(declared_before_185())
        | frozenset(ISSUE_179_ORDERED_SEEDS)
        | frozenset(ISSUE_183_ORDERED_SEEDS)
    )


def require_seed_plan(ordered_seeds) -> tuple[int, ...]:
    if isinstance(ordered_seeds, (str, bytes, bytearray)):
        raise TypeError("ordered_seeds must be an ordered collection of ints")
    try:
        seeds = tuple(ordered_seeds)
    except TypeError:
        raise TypeError("ordered_seeds must be an ordered collection of ints") from None
    if any(type(seed) is not int for seed in seeds):
        raise TypeError("ordered_seeds must contain only exact ints")
    if seeds != DEFAULT_ORDERED_SEEDS:
        raise _error("the seed plan must be exactly the ordered seeds 622..646")
    return seeds


def seed_freshness_block(
    ordered_seeds=DEFAULT_ORDERED_SEEDS,
    *,
    external_freshness_confirmed: bool,
    additional_allocated_seeds=(),
) -> dict[str, object]:
    seeds = require_seed_plan(ordered_seeds)
    if type(external_freshness_confirmed) is not bool:
        raise TypeError("external_freshness_confirmed must be an exact bool")
    if not external_freshness_confirmed:
        raise _error(
            "open/closed Issues plus known local/private allocations must be "
            "reviewed immediately before the lock"
        )
    try:
        external = frozenset(additional_allocated_seeds)
    except TypeError:
        raise TypeError("additional_allocated_seeds must be an iterable") from None
    if any(type(seed) is not int for seed in external):
        raise TypeError("additional_allocated_seeds must contain only exact ints")
    repository_collisions = sorted(declared_allocated_seeds().intersection(seeds))
    external_collisions = sorted(external.intersection(seeds))
    if repository_collisions or external_collisions:
        raise _error("SEED PLAN REFORMULATE: the provisional population collided")
    return {
        "ordered_seeds": list(seeds),
        "repository_declared_collisions": repository_collisions,
        "external_review_collisions": external_collisions,
        "external_open_closed_issue_review_confirmed": True,
        "known_local_private_allocation_review_confirmed": True,
        "fresh": True,
        "result_exposed": False,
    }


def plan_block(ordered_seeds=DEFAULT_ORDERED_SEEDS) -> dict[str, object]:
    seeds = require_seed_plan(ordered_seeds)
    return {
        "ordered_seeds": list(seeds),
        "seed_block_count": SEED_BLOCK_COUNT,
        "rotation_count": ROTATIONS_PER_SEED,
        "game_count": GAME_COUNT,
        "game_mode": GAME_MODE,
        "max_steps": MAX_STEPS,
        "max_workers": MAX_WORKERS,
        "formal_test": FORMAL_TEST,
        "role": ROLE,
        "seat_rotation": "ABBB",
        "candidate_arm": "P6",
        "baseline_arm": "C",
    }


@dataclass(frozen=True, slots=True)
class PriorEvidence:
    gate_a: GateAEvidence
    gate_b_result: dict[str, object]
    gate_b_classified: dict[str, object]

    def candidate_block(self) -> dict[str, object]:
        return self.gate_a.candidate_block()

    def prior_binding(self) -> dict[str, object]:
        return prior_evidence_block()


def prior_evidence_block() -> dict[str, object]:
    return {
        "gate_a_unclassified_result_identity": EXPECTED_GATE_A_RESULT_IDENTITY,
        "gate_a_classified_result_identity": EXPECTED_GATE_A_CLASSIFIED_IDENTITY,
        "gate_a_classification": EXPECTED_GATE_A_CLASSIFICATION,
        "gate_b_lock_identity": EXPECTED_GATE_B_LOCK_IDENTITY,
        "gate_b_result_identity": EXPECTED_GATE_B_RESULT_IDENTITY,
        "gate_b_strength_artifact_sha256": EXPECTED_GATE_B_ARTIFACT_SHA256,
        "gate_b_classification": EXPECTED_GATE_B_CLASSIFICATION,
    }


def load_prior_evidence(
    checkpoint_path: str | Path,
    gate_a_result_path: str | Path,
    gate_a_classified_path: str | Path,
    gate_b_result_path: str | Path,
    gate_b_classified_path: str | Path,
) -> PriorEvidence:
    gate_a = load_gate_a_evidence(
        checkpoint_path, gate_a_result_path, gate_a_classified_path
    )
    gate_b_result = read_json_document(Path(gate_b_result_path))
    gate_b_classified = read_json_document(Path(gate_b_classified_path))
    validate_gate_b_result(gate_b_result)
    validate_gate_b_result(gate_b_classified, allow_classified=True)
    expected_classified = record_gate_b_classification(
        gate_b_result,
        derive_gate_b_classification(gate_b_result),
    )
    if gate_b_classified != expected_classified:
        raise _error("the retained #183 classified result differs from re-derivation")
    checks = (
        (gate_b_result["lock_identity"], EXPECTED_GATE_B_LOCK_IDENTITY, "lock"),
        (
            gate_b_result["result_identity"],
            EXPECTED_GATE_B_RESULT_IDENTITY,
            "result",
        ),
        (
            gate_b_result["strength_artifact"]["sha256"],
            EXPECTED_GATE_B_ARTIFACT_SHA256,
            "strength artifact",
        ),
        (
            gate_b_classified["classification"],
            EXPECTED_GATE_B_CLASSIFICATION,
            "classification",
        ),
    )
    for actual, expected, name in checks:
        if actual != expected:
            raise _error(f"the retained #183 {name} binding drifted")
    if gate_b_result["candidate"] != gate_a.candidate_block():
        raise _error("the retained #181 and #183 candidate bindings differ")
    return PriorEvidence(gate_a, gate_b_result, gate_b_classified)


@dataclass(frozen=True, slots=True)
class P6HigherFidelityArtifactLocations:
    candidate_checkpoint: str
    gate_a_result: str
    gate_a_classified: str
    gate_b_result: str
    gate_b_classified: str
    strength_artifact: str
    result: str
    classified_result: str
    retention_backend: str = RETENTION_BACKEND

    def __post_init__(self) -> None:
        values = tuple(getattr(self, field) for field in self.__dataclass_fields__)
        if any(type(value) is not str or not value.strip() for value in values):
            raise _error("artifact locations must be non-empty strings")
        outputs = (self.strength_artifact, self.result, self.classified_result)
        if len(set(outputs)) != len(outputs):
            raise _error("each output must have a distinct location")

    def to_document(self) -> dict[str, object]:
        return {
            "retention_backend": self.retention_backend,
            "candidate_checkpoint": self.candidate_checkpoint,
            "gate_a_result": self.gate_a_result,
            "gate_a_classified": self.gate_a_classified,
            "gate_b_result": self.gate_b_result,
            "gate_b_classified": self.gate_b_classified,
            "strength_artifact": self.strength_artifact,
            "result": self.result,
            "classified_result": self.classified_result,
            "retention_keys": {
                "strength_artifact": ARTIFACT_RETENTION_KEY,
                "result": RESULT_RETENTION_KEY,
                "classified_result": CLASSIFIED_RESULT_RETENTION_KEY,
            },
        }


def _require_output_destinations_ready(locations: object) -> None:
    if type(locations) is not dict:
        raise _error("locked artifact locations are invalid")
    resolved: list[Path] = []
    for name in _OUTPUT_NAMES:
        value = locations.get(name)
        if type(value) is not str or not value or "\x00" in value:
            raise _error(f"locked output {name} path is unusable")
        path = Path(value)
        resolved.append(path.resolve(strict=False))
        if path.exists():
            raise _error(f"locked output {name} already exists; outputs are write-once")
        parent = path.parent
        if not parent.exists():
            raise _error(f"locked output {name} parent directory does not exist")
        if not parent.is_dir():
            raise _error(f"locked output {name} parent is not a directory")
        if not os.access(parent, os.W_OK):
            raise _error(f"locked output {name} parent directory is not writable")
    if len(set(resolved)) != len(resolved):
        raise _error("each output must resolve to a distinct location")


def execution_target_block(
    provenance: SingleRoundExecutionProvenance,
) -> dict[str, object]:
    if not isinstance(provenance, SingleRoundExecutionProvenance):
        raise TypeError("provenance must be a SingleRoundExecutionProvenance")
    branch = _git_output("symbolic-ref", "--short", "HEAD").strip()
    if branch != "main":
        raise _error("real lock/run requires the checked-out Arena main branch")
    head = _require_clean_arena_head()
    origin_main = _resolve_execution_target_revision()
    if head != provenance.lisjong_arena_revision:
        raise _error("Arena HEAD differs from collected execution provenance")
    if head != origin_main:
        raise _error("Arena HEAD must be the fetched merged origin/main revision")
    return {
        "branch": "main",
        "head_equals_origin_main_at_lock": True,
        "merge_status": "merged-main",
        "merged_main_revision": head,
        "source_issue": SOURCE_ISSUE,
        "source_pr": SOURCE_PR,
    }


def lock_identity(document: dict[str, object]) -> str:
    return _sha256_document(
        {name: value for name, value in document.items() if name != "lock_identity"}
    )


def build_pre_execution_lock(
    evidence: PriorEvidence,
    *,
    locations: P6HigherFidelityArtifactLocations,
    ordered_seeds=DEFAULT_ORDERED_SEEDS,
    external_freshness_confirmed: bool,
    additional_allocated_seeds=(),
) -> dict[str, object]:
    if not isinstance(evidence, PriorEvidence):
        raise TypeError("evidence must be PriorEvidence")
    if not isinstance(locations, P6HigherFidelityArtifactLocations):
        raise TypeError("locations must be P6HigherFidelityArtifactLocations")
    if str(evidence.gate_a.checkpoint.path) != locations.candidate_checkpoint:
        raise _error("the lock candidate location differs from the loaded checkpoint")
    location_document = locations.to_document()
    _require_output_destinations_ready(location_document)
    rebound = load_prior_evidence(
        locations.candidate_checkpoint,
        locations.gate_a_result,
        locations.gate_a_classified,
        locations.gate_b_result,
        locations.gate_b_classified,
    )
    if rebound.candidate_block() != evidence.candidate_block():
        raise _error("strict prior-evidence readback differs")
    provenance = collect_execution_provenance()
    require_p6_higher_fidelity_provenance(provenance)
    runtime = runtime_block()
    require_runtime(runtime)
    target = execution_target_block(provenance)
    seeds = require_seed_plan(ordered_seeds)
    freshness = seed_freshness_block(
        seeds,
        external_freshness_confirmed=external_freshness_confirmed,
        additional_allocated_seeds=additional_allocated_seeds,
    )
    document: dict[str, object] = {
        "lock_schema_version": LOCK_SCHEMA_VERSION,
        "experiment_id": EXPERIMENT_ID,
        "source_issue": SOURCE_ISSUE,
        "source_pr": SOURCE_PR,
        "parent_issue": PARENT_ISSUE,
        "predecessor_issues": list(PREDECESSOR_ISSUES),
        "primary_changed_axis": PRIMARY_CHANGED_AXIS,
        "locked_unchanged_axes": list(LOCKED_UNCHANGED_AXES),
        "candidate": rebound.candidate_block(),
        "prior_evidence": rebound.prior_binding(),
        "serving": hybrid_activation_block(),
        "selection_guard": None,
        "fallback_policy": fallback_policy_block(),
        "baseline": baseline_block(),
        "p6_training": p6_training_block(),
        "plan": plan_block(seeds),
        "classification_rule": dict(CLASSIFICATION_RULE),
        "outcomes": [outcome.value for outcome in P6HigherFidelityOutcome],
        "artifact_locations": location_document,
        "seed_freshness": freshness,
        "provenance": execution_provenance_to_dict(provenance),
        "runtime": runtime,
        "execution_target": target,
        "no_rescue_boundary": NO_RESCUE_BOUNDARY,
        "result_exposed": False,
        "lock_identity": None,
    }
    document["lock_identity"] = lock_identity(document)
    return validate_pre_execution_lock(document)


def validate_pre_execution_lock(document: object) -> dict[str, object]:
    if type(document) is not dict:
        raise _error("pre-execution lock must be an object")
    required = {
        "lock_schema_version",
        "experiment_id",
        "source_issue",
        "source_pr",
        "parent_issue",
        "predecessor_issues",
        "primary_changed_axis",
        "locked_unchanged_axes",
        "candidate",
        "prior_evidence",
        "serving",
        "selection_guard",
        "fallback_policy",
        "baseline",
        "p6_training",
        "plan",
        "classification_rule",
        "outcomes",
        "artifact_locations",
        "seed_freshness",
        "provenance",
        "runtime",
        "execution_target",
        "no_rescue_boundary",
        "result_exposed",
        "lock_identity",
    }
    if set(document) != required:
        raise _error("pre-execution lock fields are invalid")
    expected = {
        "lock_schema_version": LOCK_SCHEMA_VERSION,
        "experiment_id": EXPERIMENT_ID,
        "source_issue": SOURCE_ISSUE,
        "source_pr": SOURCE_PR,
        "parent_issue": PARENT_ISSUE,
        "predecessor_issues": list(PREDECESSOR_ISSUES),
        "primary_changed_axis": PRIMARY_CHANGED_AXIS,
        "locked_unchanged_axes": list(LOCKED_UNCHANGED_AXES),
        "candidate": _expected_candidate_block(),
        "prior_evidence": prior_evidence_block(),
        "serving": hybrid_activation_block(),
        "selection_guard": None,
        "fallback_policy": fallback_policy_block(),
        "baseline": baseline_block(),
        "p6_training": p6_training_block(),
        "classification_rule": dict(CLASSIFICATION_RULE),
        "outcomes": [outcome.value for outcome in P6HigherFidelityOutcome],
        "no_rescue_boundary": NO_RESCUE_BOUNDARY,
        "result_exposed": False,
    }
    for name, value in expected.items():
        if document[name] != value:
            raise _error(f"pre-execution lock {name} drifted")
    plan = document["plan"]
    if type(plan) is not dict or plan != plan_block(plan.get("ordered_seeds", ())):
        raise _error("pre-execution lock plan is invalid")
    freshness = document["seed_freshness"]
    if type(freshness) is not dict or freshness != {
        "ordered_seeds": plan["ordered_seeds"],
        "repository_declared_collisions": [],
        "external_review_collisions": [],
        "external_open_closed_issue_review_confirmed": True,
        "known_local_private_allocation_review_confirmed": True,
        "fresh": True,
        "result_exposed": False,
    }:
        raise _error("pre-execution lock seed freshness evidence is invalid")
    if declared_allocated_seeds().intersection(plan["ordered_seeds"]):
        raise _error("pre-execution lock seeds collide with consumed evidence")
    locations = document["artifact_locations"]
    location_fields = {
        "retention_backend",
        "candidate_checkpoint",
        "gate_a_result",
        "gate_a_classified",
        "gate_b_result",
        "gate_b_classified",
        "strength_artifact",
        "result",
        "classified_result",
        "retention_keys",
    }
    if type(locations) is not dict or set(locations) != location_fields:
        raise _error("pre-execution lock artifact locations are invalid")
    if any(
        type(locations[name]) is not str or not locations[name]
        for name in location_fields - {"retention_keys"}
    ):
        raise _error("pre-execution lock artifact location is invalid")
    if locations["retention_backend"] != RETENTION_BACKEND or locations[
        "retention_keys"
    ] != {
        "strength_artifact": ARTIFACT_RETENTION_KEY,
        "result": RESULT_RETENTION_KEY,
        "classified_result": CLASSIFIED_RESULT_RETENTION_KEY,
    }:
        raise _error("pre-execution lock retention binding drifted")
    outputs = [Path(locations[name]).resolve(strict=False) for name in _OUTPUT_NAMES]
    if len(set(outputs)) != len(outputs):
        raise _error("pre-execution lock outputs must be distinct")
    provenance = parse_execution_provenance(document["provenance"])
    require_p6_higher_fidelity_provenance(provenance)
    require_runtime(document["runtime"])
    target = document["execution_target"]
    if target != {
        "branch": "main",
        "head_equals_origin_main_at_lock": True,
        "merge_status": "merged-main",
        "merged_main_revision": provenance.lisjong_arena_revision,
        "source_issue": SOURCE_ISSUE,
        "source_pr": SOURCE_PR,
    }:
        raise _error("pre-execution lock execution target is invalid")
    identity = document["lock_identity"]
    if (
        type(identity) is not str
        or len(identity) != 64
        or identity != lock_identity(document)
    ):
        raise _error("pre-execution lock identity is invalid")
    return document


def render_pre_execution_lock(document: object) -> str:
    lock = validate_pre_execution_lock(document)
    plan = lock["plan"]
    locations = lock["artifact_locations"]
    provenance = lock["provenance"]
    lines = [
        "## Issue #185 pre-execution lock",
        "",
        "```text",
        f"experiment             {lock['experiment_id']}",
        f"lock identity          {lock['lock_identity']}",
        f"result exposed         {lock['result_exposed']}",
        f"arena merged main      {lock['execution_target']['merged_main_revision']}",
        f"source Issue / PR      {SOURCE_ISSUE} / {SOURCE_PR}",
        f"lisjong revision       {provenance['lisjong_revision']}",
        f"engine revision        {provenance['lisjong_engine_revision']}",
        f"python / torch         {lock['runtime']['python_version']} / {lock['runtime']['torch_version']}",
        f"riichienv              {lock['runtime']['riichienv_version']}",
        f"candidate              {lock['candidate']['candidate_identity']}",
        f"baseline               {lock['baseline']['identity']}",
        "selection guard        none",
        f"seeds                  {plan['ordered_seeds'][0]}..{plan['ordered_seeds'][-1]}",
        f"blocks / games         {plan['seed_block_count']} / {plan['game_count']}",
        f"rotations / workers    {plan['rotation_count']} / {plan['max_workers']}",
        f"formal TEST            {plan['formal_test']}",
        f"strength artifact      {locations['strength_artifact']}",
        f"result                 {locations['result']}",
        f"classified result      {locations['classified_result']}",
        "```",
        "",
        "Machine-readable lock:",
        "",
        "```json",
        canonical_json_text(lock),
        "```",
        "",
        NO_RESCUE_BOUNDARY,
    ]
    return "\n".join(lines) + "\n"


def require_pre_execution_comment_url(value: object) -> str:
    if type(value) is not str or _LOCK_COMMENT_URL.fullmatch(value) is None:
        raise _error("real execution requires an exact Issue #185 lock-comment URL")
    return value


def build_evaluation_plan(
    checkpoint: LoadedP6Checkpoint, lock_document: object
) -> tuple[SingleRoundEvaluationPlan, PolicyInstanceRegistry, PolicyInstanceRegistry]:
    lock = validate_pre_execution_lock(lock_document)
    if not isinstance(checkpoint, LoadedP6Checkpoint):
        raise TypeError("checkpoint must be a LoadedP6Checkpoint")
    if checkpoint.candidate_identity != EXPECTED_CANDIDATE_IDENTITY:
        raise _error("the served checkpoint is not the exact #181/#183 P6 candidate")
    if lock["candidate"] != _expected_candidate_block():
        raise _error("the locked P6 candidate binding drifted")
    runtime = create_p1_hybrid_runtime(
        checkpoint.model, supported_indices=checkpoint.supported_indices
    )
    candidate_registry = PolicyInstanceRegistry(runtime.create_policy)
    baseline_registry = PolicyInstanceRegistry(create_yakuhai_call)
    plan = SingleRoundEvaluationPlan(
        candidate=PolicySpec(
            identity=checkpoint.candidate_identity,
            factory=candidate_registry.create_policy,
        ),
        baseline=PolicySpec(
            identity=BASELINE_IDENTITY,
            factory=baseline_registry.create_policy,
        ),
        seeds=tuple(lock["plan"]["ordered_seeds"]),
        max_steps=MAX_STEPS,
    )
    return plan, candidate_registry, baseline_registry


def require_higher_fidelity_artifact(
    artifact: SingleRoundStrengthArtifact, lock_document: object
) -> SingleRoundStrengthArtifact:
    lock = validate_pre_execution_lock(lock_document)
    if not isinstance(artifact, SingleRoundStrengthArtifact):
        raise TypeError("artifact must be a SingleRoundStrengthArtifact")
    plan = artifact.plan
    if (
        plan.candidate_identity != lock["candidate"]["candidate_identity"]
        or plan.baseline_identity != BASELINE_IDENTITY
        or plan.seeds != tuple(lock["plan"]["ordered_seeds"])
        or plan.game_mode != GAME_MODE
        or plan.rotation_count != ROTATIONS_PER_SEED
        or plan.max_steps != MAX_STEPS
    ):
        raise _error("artifact candidate, baseline, population, or protocol drifted")
    if len(artifact.game_results) != GAME_COUNT:
        raise _error("partial evaluation artifacts are never accepted")
    if artifact.provenance != parse_execution_provenance(lock["provenance"]):
        raise _error("artifact provenance differs from the posted lock")
    expected_pairs = [
        (seed, rotation)
        for seed in lock["plan"]["ordered_seeds"]
        for rotation in range(ROTATIONS_PER_SEED)
    ]
    observed_pairs = [
        (result.seed, result.rotation) for result in artifact.game_results
    ]
    if observed_pairs != expected_pairs:
        raise _error("artifact does not preserve the exact seed/rotation order")
    counts = {seat: 0 for seat in range(4)}
    for result in artifact.game_results:
        counts[int(result.candidate_seat)] += 1
    if set(counts.values()) != {SEED_BLOCK_COUNT}:
        raise _error("candidate did not occupy every seat once per seed block")
    return artifact


def _artifact_block(
    artifact: SingleRoundStrengthArtifact, path: Path
) -> dict[str, object]:
    return {
        "schema_version": artifact.schema_version,
        "evaluation_protocol": artifact.evaluation_protocol,
        "filename": path.name,
        "sha256": _sha256_file(path),
        "game_count": len(artifact.game_results),
        "retention": {"backend": RETENTION_BACKEND, "key": ARTIFACT_RETENTION_KEY},
    }


def secondary_diagnostics_block(artifact, summary) -> dict[str, object]:
    baseline_stats = [
        result.seat_round_stats[seat]
        for result in artifact.game_results
        for seat in range(4)
        if seat != int(result.candidate_seat)
    ]
    return {
        "p6_candidate": _mahjong_metrics(
            summary.candidate_metrics.mahjong_metrics,
            "P6 candidate (1 seat per game)",
        ),
        "yakuhai_call_baseline": _mahjong_metrics(
            aggregate_seat_round_stats_metrics(baseline_stats),
            "yakuhai-call baseline (3 seats per game)",
        ),
    }


def serving_diagnostics_block(diagnostics: ActivationDiagnostics) -> dict[str, object]:
    return {
        **diagnostics.to_document(),
        "illegal_selection_count": 0,
        "non_finite_q_output_count": 0,
        "resolve_failure_count": 0,
        "fail_closed_at_decision_time": True,
    }


def result_identity(document: dict[str, object]) -> str:
    payload = {**document, "classification": None, "result_identity": None}
    return _sha256_document(payload)


def build_result(
    *,
    lock_document: object,
    pre_execution_comment_url: str,
    artifact: SingleRoundStrengthArtifact,
    artifact_path: str | Path,
    summary,
    activation_diagnostics: ActivationDiagnostics,
    baseline_policy_instance_count: int,
) -> dict[str, object]:
    lock = validate_pre_execution_lock(lock_document)
    comment_url = require_pre_execution_comment_url(pre_execution_comment_url)
    path = Path(artifact_path)
    require_higher_fidelity_artifact(artifact, lock)
    regenerated = summarize_single_round_strength(
        aggregate_candidate_metrics(
            artifact.plan.candidate_identity, artifact.game_results
        ),
        artifact.game_results,
    )
    if summary != artifact.summary or summary != regenerated:
        raise _error("result summary is not the canonical artifact summary")
    if (
        path.resolve()
        != Path(lock["artifact_locations"]["strength_artifact"]).resolve()
    ):
        raise _error("artifact path differs from the pre-execution lock")
    if baseline_policy_instance_count != 3 * GAME_COUNT:
        raise _error("every baseline seat must receive a fresh Policy instance")
    document: dict[str, object] = {
        "result_schema_version": RESULT_SCHEMA_VERSION,
        "classified_result_schema_version": CLASSIFIED_RESULT_SCHEMA_VERSION,
        "experiment_id": EXPERIMENT_ID,
        "source_issue": SOURCE_ISSUE,
        "source_pr": SOURCE_PR,
        "lock_identity": lock["lock_identity"],
        "lock_comment_url": comment_url,
        "candidate": lock["candidate"],
        "prior_evidence": lock["prior_evidence"],
        "serving": lock["serving"],
        "selection_guard": None,
        "fallback_policy": lock["fallback_policy"],
        "baseline": lock["baseline"],
        "plan": lock["plan"],
        "runtime": lock["runtime"],
        "execution_target": lock["execution_target"],
        "strength_artifact": _artifact_block(artifact, path),
        "canonical_summary": summary_to_dict(summary),
        "secondary_diagnostics": secondary_diagnostics_block(artifact, summary),
        "serving_diagnostics": serving_diagnostics_block(activation_diagnostics),
        "baseline_policy_instance_count": baseline_policy_instance_count,
        "classification_rule": dict(CLASSIFICATION_RULE),
        "limitations": list(LIMITATIONS),
        "no_rescue_boundary": NO_RESCUE_BOUNDARY,
        "provenance": execution_provenance_to_dict(artifact.provenance),
        "classification": None,
        "result_identity": None,
    }
    document["result_identity"] = result_identity(document)
    return validate_result(document)


def _summary_statistics(document: dict[str, object]) -> dict[str, object]:
    summary = document.get("canonical_summary")
    if type(summary) is not dict:
        raise _error("canonical_summary must be an object")
    statistics = summary.get("seed_block_statistics")
    if (
        type(statistics) is not dict
        or statistics.get("seed_block_count") != SEED_BLOCK_COUNT
    ):
        raise _error("classification statistics must cover the exact 25 blocks")
    lower = statistics.get("normal_approx_95_interval_lower")
    upper = statistics.get("normal_approx_95_interval_upper")
    if type(lower) not in (int, float) or type(upper) not in (int, float):
        raise _error("classification interval is undefined")
    if not math.isfinite(lower) or not math.isfinite(upper) or lower > upper:
        raise _error("classification interval is invalid")
    return statistics


def classify_interval(lower: object, upper: object) -> P6HigherFidelityOutcome:
    if type(lower) not in (int, float) or type(upper) not in (int, float):
        raise _error("classification requires a numeric interval")
    lower = float(lower)
    upper = float(upper)
    if not math.isfinite(lower) or not math.isfinite(upper) or lower > upper:
        raise _error("classification interval is invalid")
    if lower > 0:
        return P6HigherFidelityOutcome.SIGNAL
    if upper < 0:
        return P6HigherFidelityOutcome.NEGATIVE
    return P6HigherFidelityOutcome.INCONCLUSIVE


def classify_pre_result_state(
    *, evidence_available: bool, protocol_valid: bool
) -> P6HigherFidelityOutcome | None:
    if type(evidence_available) is not bool or type(protocol_valid) is not bool:
        raise TypeError("pre-result state flags must be exact bools")
    if not protocol_valid:
        return P6HigherFidelityOutcome.STOP_INVALID
    if not evidence_available:
        return P6HigherFidelityOutcome.EVIDENCE_BLOCKED
    return None


def derive_classification(document: object) -> P6HigherFidelityOutcome:
    validated = validate_result(document, allow_classified=True)
    statistics = _summary_statistics(validated)
    return classify_interval(
        statistics["normal_approx_95_interval_lower"],
        statistics["normal_approx_95_interval_upper"],
    )


def _validate_serving_diagnostics(diagnostics: object) -> None:
    fields = {
        "policy_instance_count",
        "total_decisions",
        "total_activations",
        "activation_rate",
        "total_scaffold_fallbacks",
        "scaffold_fallback_rate",
        "total_support_fallbacks",
        "support_fallback_rate",
        "illegal_selection_count",
        "non_finite_q_output_count",
        "resolve_failure_count",
        "fail_closed_at_decision_time",
    }
    if type(diagnostics) is not dict or set(diagnostics) != fields:
        raise _error("serving diagnostics fields are invalid")
    count_names = (
        "policy_instance_count",
        "total_decisions",
        "total_activations",
        "total_scaffold_fallbacks",
        "total_support_fallbacks",
        "illegal_selection_count",
        "non_finite_q_output_count",
        "resolve_failure_count",
    )
    if any(
        type(diagnostics[name]) is not int or diagnostics[name] < 0
        for name in count_names
    ):
        raise _error("serving diagnostic counts are invalid")
    if diagnostics["policy_instance_count"] != GAME_COUNT:
        raise _error("candidate must receive one fresh Policy instance per game")
    decisions = diagnostics["total_decisions"]
    activations = diagnostics["total_activations"]
    scaffold = diagnostics["total_scaffold_fallbacks"]
    support = diagnostics["total_support_fallbacks"]
    if decisions <= 0 or activations + scaffold != decisions or support > scaffold:
        raise _error("serving diagnostic path counts are inconsistent")
    for name, numerator in (
        ("activation_rate", activations),
        ("scaffold_fallback_rate", scaffold),
        ("support_fallback_rate", support),
    ):
        value = diagnostics[name]
        if (
            type(value) is not float
            or not math.isfinite(value)
            or value != numerator / decisions
        ):
            raise _error(f"serving diagnostic {name} is inconsistent")
    if (
        any(
            diagnostics[name] != 0
            for name in (
                "illegal_selection_count",
                "non_finite_q_output_count",
                "resolve_failure_count",
            )
        )
        or diagnostics["fail_closed_at_decision_time"] is not True
    ):
        raise _error("serving failure diagnostics must remain zero and fail closed")


def validate_result(
    document: object, *, allow_classified: bool = False
) -> dict[str, object]:
    if type(document) is not dict:
        raise _error("P6 higher-fidelity result must be an object")
    required = {
        "result_schema_version",
        "classified_result_schema_version",
        "experiment_id",
        "source_issue",
        "source_pr",
        "lock_identity",
        "lock_comment_url",
        "candidate",
        "prior_evidence",
        "serving",
        "selection_guard",
        "fallback_policy",
        "baseline",
        "plan",
        "runtime",
        "execution_target",
        "strength_artifact",
        "canonical_summary",
        "secondary_diagnostics",
        "serving_diagnostics",
        "baseline_policy_instance_count",
        "classification_rule",
        "limitations",
        "no_rescue_boundary",
        "provenance",
        "classification",
        "result_identity",
    }
    if set(document) != required:
        raise _error("P6 higher-fidelity result fields are invalid")
    expected = {
        "result_schema_version": RESULT_SCHEMA_VERSION,
        "classified_result_schema_version": CLASSIFIED_RESULT_SCHEMA_VERSION,
        "experiment_id": EXPERIMENT_ID,
        "source_issue": SOURCE_ISSUE,
        "source_pr": SOURCE_PR,
        "candidate": _expected_candidate_block(),
        "prior_evidence": prior_evidence_block(),
        "serving": hybrid_activation_block(),
        "selection_guard": None,
        "fallback_policy": fallback_policy_block(),
        "baseline": baseline_block(),
        "classification_rule": dict(CLASSIFICATION_RULE),
        "limitations": list(LIMITATIONS),
        "no_rescue_boundary": NO_RESCUE_BOUNDARY,
    }
    for name, value in expected.items():
        if document[name] != value:
            raise _error(f"P6 higher-fidelity result {name} drifted")
    require_runtime(document["runtime"])
    provenance = parse_execution_provenance(document["provenance"])
    require_p6_higher_fidelity_provenance(provenance)
    target = document["execution_target"]
    if type(target) is not dict or target != {
        "branch": "main",
        "head_equals_origin_main_at_lock": True,
        "merge_status": "merged-main",
        "merged_main_revision": document["provenance"]["lisjong_arena_revision"],
        "source_issue": SOURCE_ISSUE,
        "source_pr": SOURCE_PR,
    }:
        raise _error("P6 higher-fidelity result execution target drifted")
    require_pre_execution_comment_url(document["lock_comment_url"])
    identity = document["lock_identity"]
    if type(identity) is not str or len(identity) != 64:
        raise _error("result lock identity is invalid")
    plan = document["plan"]
    if type(plan) is not dict or plan != plan_block(plan.get("ordered_seeds", ())):
        raise _error("result plan is invalid")
    artifact = document["strength_artifact"]
    if (
        type(artifact) is not dict
        or artifact.get("schema_version") != SINGLE_ROUND_ARTIFACT_SCHEMA_VERSION
        or artifact.get("evaluation_protocol") != SINGLE_ROUND_EVALUATION_PROTOCOL
        or artifact.get("game_count") != GAME_COUNT
        or artifact.get("retention")
        != {"backend": RETENTION_BACKEND, "key": ARTIFACT_RETENTION_KEY}
    ):
        raise _error("result strength artifact binding is invalid")
    if type(artifact.get("sha256")) is not str or len(artifact["sha256"]) != 64:
        raise _error("result strength artifact digest is invalid")
    filename = artifact.get("filename")
    if type(filename) is not str or not filename or "/" in filename or "\\" in filename:
        raise _error("result strength artifact filename is invalid")
    _summary_statistics(document)
    summary = document["canonical_summary"]
    if type(summary) is not dict:
        raise _error("canonical summary must be an object")
    candidate_metrics = summary.get("candidate_metrics")
    if (
        type(candidate_metrics) is not dict
        or candidate_metrics.get("game_count") != GAME_COUNT
    ):
        raise _error("canonical candidate metrics do not cover 100 games")
    secondary = document["secondary_diagnostics"]
    if type(secondary) is not dict or set(secondary) != {
        "p6_candidate",
        "yakuhai_call_baseline",
    }:
        raise _error("secondary diagnostic arms are invalid")
    try:
        _validate_mahjong_metrics(secondary["p6_candidate"], GAME_COUNT, "p6_candidate")
        _validate_mahjong_metrics(
            secondary["yakuhai_call_baseline"], 3 * GAME_COUNT, "yakuhai_call_baseline"
        )
    except OfflineQError as exc:
        raise _error(str(exc)) from exc
    _validate_serving_diagnostics(document["serving_diagnostics"])
    if document["baseline_policy_instance_count"] != 3 * GAME_COUNT:
        raise _error("baseline Policy instance count is invalid")
    classification = document["classification"]
    if classification is not None:
        if not allow_classified:
            raise _error("unclassified result unexpectedly contains a classification")
        if classification not in {outcome.value for outcome in _RESULT_OUTCOMES}:
            raise _error("recorded classification is invalid")
    result_id = document["result_identity"]
    if (
        type(result_id) is not str
        or len(result_id) != 64
        or result_id != result_identity(document)
    ):
        raise _error("result identity does not match its contents")
    if classification is not None:
        derived = derive_classification({**document, "classification": None})
        if classification != derived.value:
            raise _error("recorded classification is not derivable")
    return document


def bind_result_artifact(
    document: object,
    *,
    artifact_path: str | Path,
    lock_document: object,
    allow_classified: bool = False,
) -> SingleRoundStrengthArtifact:
    result = validate_result(document, allow_classified=allow_classified)
    lock = validate_pre_execution_lock(lock_document)
    path = Path(artifact_path)
    if (
        path.resolve()
        != Path(lock["artifact_locations"]["strength_artifact"]).resolve()
    ):
        raise _error("retained artifact path differs from the posted lock")
    for name in (
        "candidate",
        "prior_evidence",
        "serving",
        "selection_guard",
        "fallback_policy",
        "baseline",
        "plan",
        "runtime",
        "execution_target",
        "provenance",
    ):
        if result[name] != lock[name]:
            raise _error(f"result {name} differs from the posted lock")
    if result["lock_identity"] != lock["lock_identity"]:
        raise _error("result lock identity differs from the posted lock")
    artifact = load_single_round_artifact(path)
    require_higher_fidelity_artifact(artifact, lock)
    if result["strength_artifact"] != _artifact_block(artifact, path):
        raise _error("retained artifact bytes differ from the result binding")
    regenerated = summarize_single_round_strength(
        aggregate_candidate_metrics(
            artifact.plan.candidate_identity, artifact.game_results
        ),
        artifact.game_results,
    )
    if (
        regenerated != artifact.summary
        or summary_to_dict(regenerated) != result["canonical_summary"]
    ):
        raise _error("retained raw games do not regenerate the canonical summary")
    if (
        secondary_diagnostics_block(artifact, regenerated)
        != result["secondary_diagnostics"]
    ):
        raise _error("retained raw games do not regenerate secondary diagnostics")
    return artifact


def record_classification(
    document: object, outcome: P6HigherFidelityOutcome
) -> dict[str, object]:
    validated = validate_result(document)
    if not isinstance(outcome, P6HigherFidelityOutcome):
        raise TypeError("outcome must be a P6HigherFidelityOutcome")
    if outcome not in _RESULT_OUTCOMES:
        raise _error("blocked/invalid are pre-result states, not recorded outcomes")
    if outcome is not derive_classification(validated):
        raise _error("requested classification contradicts the locked interval")
    return validate_result(
        {**validated, "classification": outcome.value}, allow_classified=True
    )


@dataclass(frozen=True, slots=True)
class P6HigherFidelityMeasurement:
    artifact: SingleRoundStrengthArtifact
    document: dict[str, object]
    classified: dict[str, object]
    outcome: P6HigherFidelityOutcome
    wall_clock_seconds: float
    cpu_seconds: float


def run_higher_fidelity(
    lock_document: object,
    *,
    pre_execution_comment_url: str,
    progress_callback=None,
) -> P6HigherFidelityMeasurement:
    lock = validate_pre_execution_lock(lock_document)
    require_pre_execution_comment_url(pre_execution_comment_url)
    target_revision = lock["execution_target"]["merged_main_revision"]
    if _git_output("symbolic-ref", "--short", "HEAD").strip() != "main":
        raise _error("real lock/run requires the checked-out Arena main branch")
    if _require_clean_arena_head() != target_revision:
        raise _error("live Arena HEAD differs from the locked merged-main revision")
    live_provenance = collect_execution_provenance()
    require_p6_higher_fidelity_provenance(live_provenance)
    if execution_provenance_to_dict(live_provenance) != lock["provenance"]:
        raise _error("live execution provenance differs from the posted lock")
    live_runtime = runtime_block()
    require_runtime(live_runtime)
    if live_runtime != lock["runtime"]:
        raise _error("live runtime differs from the posted lock")
    locations = lock["artifact_locations"]
    _require_output_destinations_ready(locations)
    evidence = load_prior_evidence(
        locations["candidate_checkpoint"],
        locations["gate_a_result"],
        locations["gate_a_classified"],
        locations["gate_b_result"],
        locations["gate_b_classified"],
    )
    if (
        evidence.candidate_block() != lock["candidate"]
        or evidence.prior_binding() != lock["prior_evidence"]
    ):
        raise _error("live prior evidence differs from the posted lock")
    plan, candidate_registry, baseline_registry = build_evaluation_plan(
        evidence.gate_a.checkpoint, lock
    )
    wall_started = time.perf_counter()
    cpu_started = time.process_time()
    evaluation = run_single_round_evaluation(plan, progress_callback=progress_callback)
    wall_clock_seconds = time.perf_counter() - wall_started
    cpu_seconds = time.process_time() - cpu_started
    artifact_path = Path(locations["strength_artifact"])
    save_single_round_artifact(evaluation, artifact_path)
    artifact = require_higher_fidelity_artifact(
        load_single_round_artifact(artifact_path), lock
    )
    summary = summarize_single_round_strength(
        aggregate_candidate_metrics(
            artifact.plan.candidate_identity, artifact.game_results
        ),
        artifact.game_results,
    )
    if summary != artifact.summary:
        raise _error(
            "strict artifact readback did not regenerate the canonical summary"
        )
    result = build_result(
        lock_document=lock,
        pre_execution_comment_url=pre_execution_comment_url,
        artifact=artifact,
        artifact_path=artifact_path,
        summary=summary,
        activation_diagnostics=collect_activation_diagnostics(
            candidate_registry.instances
        ),
        baseline_policy_instance_count=len(baseline_registry.instances),
    )
    result_path = Path(locations["result"])
    write_new_artifact_file(result_path, canonical_json_text(result))
    strict_result = read_json_document(result_path)
    if strict_result != result:
        raise _error("unclassified result strict readback differs")
    bind_result_artifact(strict_result, artifact_path=artifact_path, lock_document=lock)
    outcome = derive_classification(strict_result)
    classified = record_classification(strict_result, outcome)
    classified_path = Path(locations["classified_result"])
    write_new_artifact_file(classified_path, canonical_json_text(classified))
    strict_classified = read_json_document(classified_path)
    if strict_classified != classified:
        raise _error("classified result strict readback differs")
    bind_result_artifact(
        strict_classified,
        artifact_path=artifact_path,
        lock_document=lock,
        allow_classified=True,
    )
    return P6HigherFidelityMeasurement(
        artifact,
        strict_result,
        strict_classified,
        outcome,
        wall_clock_seconds,
        cpu_seconds,
    )


def _write_lock(path: Path, document: dict[str, object]) -> None:
    if not path.parent.is_dir():
        raise _error("lock output parent directory must already exist")
    write_new_artifact_file(path, canonical_json_text(document))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Issue #185 P6 higher-fidelity protocol"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    lock = subparsers.add_parser("lock")
    for name in (
        "candidate-checkpoint",
        "gate-a-result",
        "gate-a-classified",
        "gate-b-result",
        "gate-b-classified",
        "strength-artifact",
        "result",
        "classified-result",
        "lock-output",
    ):
        lock.add_argument(f"--{name}", required=True)
    lock.add_argument("--external-freshness-confirmed", action="store_true")
    lock.add_argument(
        "--additional-allocated-seed", type=int, action="append", default=[]
    )
    run = subparsers.add_parser("run")
    run.add_argument("--lock-file", required=True)
    run.add_argument("--lock-comment-url", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "lock":
            evidence = load_prior_evidence(
                arguments.candidate_checkpoint,
                arguments.gate_a_result,
                arguments.gate_a_classified,
                arguments.gate_b_result,
                arguments.gate_b_classified,
            )
            locations = P6HigherFidelityArtifactLocations(
                candidate_checkpoint=arguments.candidate_checkpoint,
                gate_a_result=arguments.gate_a_result,
                gate_a_classified=arguments.gate_a_classified,
                gate_b_result=arguments.gate_b_result,
                gate_b_classified=arguments.gate_b_classified,
                strength_artifact=arguments.strength_artifact,
                result=arguments.result,
                classified_result=arguments.classified_result,
            )
            document = build_pre_execution_lock(
                evidence,
                locations=locations,
                external_freshness_confirmed=arguments.external_freshness_confirmed,
                additional_allocated_seeds=arguments.additional_allocated_seed,
            )
            _write_lock(Path(arguments.lock_output), document)
            print(render_pre_execution_lock(document), end="")
            return 0
        measurement = run_higher_fidelity(
            read_json_document(Path(arguments.lock_file)),
            pre_execution_comment_url=arguments.lock_comment_url,
        )
        print(f"result identity: {measurement.document['result_identity']}")
        print(f"classification: {measurement.outcome.value}")
        return 0
    except Exception as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CLASSIFICATION_RULE",
    "DEFAULT_ORDERED_SEEDS",
    "EXPERIMENT_ID",
    "P6HigherFidelityArtifactLocations",
    "P6HigherFidelityMeasurement",
    "P6HigherFidelityOutcome",
    "PriorEvidence",
    "baseline_block",
    "bind_result_artifact",
    "build_evaluation_plan",
    "build_pre_execution_lock",
    "classify_interval",
    "classify_pre_result_state",
    "derive_classification",
    "load_prior_evidence",
    "main",
    "record_classification",
    "render_pre_execution_lock",
    "require_higher_fidelity_artifact",
    "require_p6_higher_fidelity_provenance",
    "run_higher_fidelity",
    "seed_freshness_block",
    "validate_pre_execution_lock",
    "validate_result",
]
