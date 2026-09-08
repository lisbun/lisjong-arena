"""Issue #183 P6 Gate B — exact #181 conservative-Q candidate vs passive tsumogiri x3.

This module intentionally leaves historical #162 and #181 code paths untouched.
It reuses the exact P1 8241 hybrid serving boundary and passive tsumogiri
comparator, changing only the served model/checkpoint to the strict-read #181 P6
candidate. The real 100-game run is allowed only after a reviewed merged-main
pre-execution lock has been posted to Issue #183.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

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
    run_single_round_evaluation,
    summarize_single_round_strength,
)

from .artifact import vocabulary_block
from .errors import OfflineQError
from .p1_features import p1_feature_block
from .p1_gate_b_comparator import (
    PASSIVE_TSUMOGIRI_IDENTITY,
    comparator_block,
    passive_tsumogiri_spec,
)
from .p1_serving import (
    create_p1_hybrid_runtime,
    fallback_policy_block,
    hybrid_activation_block,
)
from .p1_shanten_guard_higher_fidelity import (
    _require_clean_arena_head,
    execution_target_block,
    runtime_block,
)
from .p1_shanten_guard_higher_fidelity_successor import (
    DEFAULT_ORDERED_SEEDS as ISSUE_179_ORDERED_SEEDS,
)
from .p1_shanten_guard_higher_fidelity_successor import (
    declared_allocated_seeds as declared_before_183,
)
from .p6_conservative_q import (
    P6_CHECKPOINT_SCHEMA_VERSION,
    LoadedP6Checkpoint,
    load_p6_checkpoint,
    p6_model_block,
    p6_training_block,
)
from .p6_gate_a import (
    P6GateAOutcome,
    validate_gate_a_result,
)
from .p6_gate_a import (
    classified_document as gate_a_classified_document,
)
from .strength import (
    ActivationDiagnostics,
    PolicyInstanceRegistry,
    collect_activation_diagnostics,
)

LOCK_SCHEMA_VERSION = "arena-learned-policy-p6-gate-b-lock-v1"
RESULT_SCHEMA_VERSION = "arena-learned-policy-p6-gate-b-result-v1"
EXPERIMENT_ID = "arena-learned-policy-p6-conservative-q-gate-b-183"
SOURCE_ISSUE = "lisbun/lisjong-arena#183"
PARENT_ISSUE = "lisbun/lisjong-project#45"
PREDECESSOR_ISSUES = (
    "lisbun/lisjong-arena#181",
    "lisbun/lisjong-arena#162",
    "lisbun/lisjong-arena#179",
)

EXPECTED_CANDIDATE_IDENTITY = (
    "learned-p6-conservative-q:"
    "9d5bf5afc164dd49314fbb6cca3338426c7cf35384f36401cfb6b3a329b22ac7"
)
EXPECTED_WEIGHTS_DIGEST = (
    "679366f11c30e9dda39ca6fcf6e745da5a561e65a0d32b1576a39bd059627499"
)
EXPECTED_SOURCE_DATASET_IDENTITY = (
    "69094c1b82f2aaedfed57cb3021b90d44642c3978a2368d4d1e2d927c5a7b2f4"
)
EXPECTED_SUPPORT_DIGEST = (
    "230b2f07dc95d169ebfb85b9deb6174f22909b52025979ae64da541bd9481d9e"
)
EXPECTED_GATE_A_RESULT_IDENTITY = (
    "01fa261422517868949a58dba717eafc722aa4b84d85ed1282ebdca0c49a90e3"
)
EXPECTED_GATE_A_CLASSIFIED_IDENTITY = (
    "1ed17d6984bc96347b2461f70a034e55474d3d2f44802f9461198f100801d051"
)
EXPECTED_GATE_A_CLASSIFICATION = P6GateAOutcome.SIGNAL.value
EXPECTED_SELECTED_EPOCH = 20
GATE_B_LISJONG_REVISION = "99a30c267a3c3e301e132c8799726eb10e012a95"
GATE_B_ENGINE_REVISION = "8735e89e1aea000ab59368d0368d476787827741"

DEFAULT_ORDERED_SEEDS = tuple(range(597, 622))
SEED_BLOCK_COUNT = 25
ROTATIONS_PER_SEED = SINGLE_ROUND_ROTATION_COUNT
GAME_COUNT = SEED_BLOCK_COUNT * ROTATIONS_PER_SEED
GAME_MODE = SINGLE_ROUND_GAME_MODE
MAX_WORKERS = 1
FORMAL_TEST = False
ROLE = "DEVELOPMENT-ONLY P6 GATE B"

RETENTION_BACKEND = "operator-local-durable"
ARTIFACT_RETENTION_KEY = "offlineq-183-p6-gate-b/strength-artifact"
RESULT_RETENTION_KEY = "offlineq-183-p6-gate-b/result"
CLASSIFIED_RESULT_RETENTION_KEY = "offlineq-183-p6-gate-b/classified-result"

CLASSIFICATION_RULE = {
    "primary_metric": "seed-block candidate-vs-baseline score delta",
    "interval": "normal-approx 95% interval over seed blocks",
    "positive": "normal_approx_95_interval_lower > 0",
    "negative": "normal_approx_95_interval_upper < 0",
    "inconclusive": "the interval crosses zero",
    "secondary_metrics_may_alter_classification": False,
    "serving_diagnostics_may_alter_classification": False,
}
NO_RESCUE_BOUNDARY = (
    "After Issue #183 result exposure there is no seed extension or replacement, "
    "candidate retraining, alpha/temperature change, #173 guard insertion, support/"
    "feature/reward/gamma change, comparator/fallback substitution, P2/P3 addition, "
    "or rescue rerun."
)
LIMITATIONS = (
    "Gate B is a one-way viability filter against a deliberately weak passive "
    "tsumogiri x3 comparator; it is not current-strength-baseline evidence.",
    "The exact #181 P6 candidate is served without the #173 selection-time shanten "
    "guard, so the run tests whether learner ranking improvement survives rollout.",
    "A positive result does not establish P6-vs-P1 causality, yakuhai-call superiority, "
    "hanchan strength, CQL optimality, or production readiness.",
    "Seeds are development-only and not a formal holdout.",
)
_LOCK_COMMENT_URL = re.compile(
    r"https://github\.com/lisbun/lisjong-arena/issues/183#issuecomment-\d+\Z"
)
_OUTPUT_NAMES = ("strength_artifact", "result", "classified_result")


class P6GateBError(OfflineQError):
    """Issue #183 lock, execution, artifact, or result contract violation."""


class P6GateBOutcome(Enum):
    POSITIVE_SIGNAL = "P6 CONSERVATIVE-Q GATE B POSITIVE SIGNAL"
    NEGATIVE_SIGNAL = "P6 CONSERVATIVE-Q GATE B NEGATIVE SIGNAL"
    INCONCLUSIVE = "P6 CONSERVATIVE-Q GATE B INCONCLUSIVE"
    EVIDENCE_BLOCKED = "P6 EVIDENCE BLOCKED"
    STOP_INVALID = "STOP / INVALID"


_RECORDABLE_OUTCOMES = frozenset(
    {
        P6GateBOutcome.POSITIVE_SIGNAL,
        P6GateBOutcome.NEGATIVE_SIGNAL,
        P6GateBOutcome.INCONCLUSIVE,
    }
)


def _error(message: str) -> P6GateBError:
    return P6GateBError(message)


def require_gate_b_provenance(provenance: SingleRoundExecutionProvenance) -> None:
    if not isinstance(provenance, SingleRoundExecutionProvenance):
        raise TypeError("provenance must be a SingleRoundExecutionProvenance")
    if provenance.lisjong_revision != GATE_B_LISJONG_REVISION:
        raise _error("lisjong revision differs from the established #162 Gate B family")
    if provenance.lisjong_engine_revision != GATE_B_ENGINE_REVISION:
        raise _error(
            "lisjong-engine revision differs from the established #162 Gate B family"
        )


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sha256_document(document: dict[str, object]) -> str:
    return hashlib.sha256(canonical_json_text(document).encode("utf-8")).hexdigest()


def declared_allocated_seeds() -> frozenset[int]:
    """Repository-declared populations that precede Issue #183."""
    return frozenset(declared_before_183()) | frozenset(ISSUE_179_ORDERED_SEEDS)


def require_seed_plan(ordered_seeds) -> tuple[int, ...]:
    if isinstance(ordered_seeds, (str, bytes, bytearray)):
        raise TypeError("ordered_seeds must be an ordered collection of ints")
    try:
        seeds = tuple(ordered_seeds)
    except TypeError:
        raise TypeError("ordered_seeds must be an ordered collection of ints") from None
    if len(seeds) != SEED_BLOCK_COUNT:
        raise _error(f"the plan must contain exactly {SEED_BLOCK_COUNT} seeds")
    if any(type(seed) is not int for seed in seeds):
        raise TypeError("ordered_seeds must contain only exact ints")
    if seeds != tuple(range(seeds[0], seeds[0] + SEED_BLOCK_COUNT)):
        raise _error("the seed plan must be one contiguous increasing range")
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
            "open/closed relevant Issues plus known local/private allocations must "
            "be reviewed before the lock is built"
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
        raise _error(
            "SEED PLAN REFORMULATE: collision found before result exposure; choose "
            "a fresh contiguous 25-seed range before building the lock"
        )
    return {
        "ordered_seeds": list(seeds),
        "repository_declared_collisions": repository_collisions,
        "external_review_collisions": external_collisions,
        "external_open_closed_issue_review_confirmed": True,
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
        "max_workers": MAX_WORKERS,
        "formal_test": FORMAL_TEST,
        "role": ROLE,
        "seat_rotation": "ABBB",
        "candidate_arm": "P6",
        "baseline_arm": "T",
    }


def _expected_candidate_block() -> dict[str, object]:
    return {
        "checkpoint_schema_version": P6_CHECKPOINT_SCHEMA_VERSION,
        "candidate_identity": EXPECTED_CANDIDATE_IDENTITY,
        "canonical_model_weights_digest": EXPECTED_WEIGHTS_DIGEST,
        "source_dataset_identity": EXPECTED_SOURCE_DATASET_IDENTITY,
        "supported_indices_digest": EXPECTED_SUPPORT_DIGEST,
        "selected_epoch": EXPECTED_SELECTED_EPOCH,
        "p1_feature": p1_feature_block(),
        "action_vocabulary": vocabulary_block(),
        "model": p6_model_block(),
        "training": p6_training_block(),
    }


@dataclass(frozen=True, slots=True)
class GateAEvidence:
    checkpoint: LoadedP6Checkpoint
    gate_a_result: dict[str, object]
    gate_a_classified: dict[str, object]

    def candidate_block(self) -> dict[str, object]:
        manifest = self.checkpoint.manifest
        return {
            "checkpoint_schema_version": manifest["checkpoint_schema_version"],
            "candidate_identity": self.checkpoint.candidate_identity,
            "canonical_model_weights_digest": manifest[
                "canonical_model_weights_digest"
            ],
            "source_dataset_identity": manifest["source_dataset_identity"],
            "supported_indices_digest": manifest["supported_indices_digest"],
            "selected_epoch": manifest["selected_epoch"],
            "p1_feature": manifest["p1_feature"],
            "action_vocabulary": manifest["action_vocabulary"],
            "model": manifest["model"],
            "training": manifest["training"],
        }

    def gate_a_binding(self) -> dict[str, object]:
        return {
            "unclassified_result_identity": self.gate_a_result["result_identity"],
            "classified_result_identity": self.gate_a_classified["result_identity"],
            "classification": self.gate_a_classified["classification"],
        }


def load_gate_a_evidence(
    checkpoint_path: str | Path,
    gate_a_result_path: str | Path,
    gate_a_classified_path: str | Path,
) -> GateAEvidence:
    checkpoint = load_p6_checkpoint(checkpoint_path)
    result = read_json_document(Path(gate_a_result_path))
    validate_gate_a_result(result)
    classified = read_json_document(Path(gate_a_classified_path))
    expected_classified = gate_a_classified_document(result)
    if classified != expected_classified:
        raise _error(
            "the retained #181 classified result differs from strict re-derivation"
        )
    manifest = checkpoint.manifest
    checks = (
        (
            checkpoint.candidate_identity,
            EXPECTED_CANDIDATE_IDENTITY,
            "candidate identity",
        ),
        (
            manifest["canonical_model_weights_digest"],
            EXPECTED_WEIGHTS_DIGEST,
            "candidate weights",
        ),
        (
            manifest["source_dataset_identity"],
            EXPECTED_SOURCE_DATASET_IDENTITY,
            "source dataset",
        ),
        (
            manifest["supported_indices_digest"],
            EXPECTED_SUPPORT_DIGEST,
            "TRAIN support",
        ),
        (manifest["selected_epoch"], EXPECTED_SELECTED_EPOCH, "selected epoch"),
        (
            result["result_identity"],
            EXPECTED_GATE_A_RESULT_IDENTITY,
            "Gate A result identity",
        ),
        (
            classified["result_identity"],
            EXPECTED_GATE_A_CLASSIFIED_IDENTITY,
            "Gate A classified identity",
        ),
        (
            classified["classification"],
            EXPECTED_GATE_A_CLASSIFICATION,
            "Gate A classification",
        ),
    )
    for actual, expected, name in checks:
        if actual != expected:
            raise _error(f"the retained #181 {name} is not the locked value")
    result_candidate = result["candidate"]
    if (
        result_candidate["candidate_identity"] != checkpoint.candidate_identity
        or result_candidate["canonical_model_weights_digest"]
        != manifest["canonical_model_weights_digest"]
        or result_candidate["supported_indices_digest"]
        != manifest["supported_indices_digest"]
    ):
        raise _error("the #181 result and checkpoint do not bind the same candidate")
    if manifest["checkpoint_schema_version"] != P6_CHECKPOINT_SCHEMA_VERSION:
        raise _error("the #181 checkpoint schema version drifted")
    if manifest["training"] != p6_training_block():
        raise _error("the #181 P6 training/objective binding drifted")
    evidence = GateAEvidence(checkpoint, result, classified)
    if evidence.candidate_block() != _expected_candidate_block():
        raise _error("the retained #181 candidate block drifted from its exact binding")
    return evidence


@dataclass(frozen=True, slots=True)
class P6GateBArtifactLocations:
    candidate_checkpoint: str
    gate_a_result: str
    gate_a_classified: str
    strength_artifact: str
    result: str
    classified_result: str
    retention_backend: str = RETENTION_BACKEND

    def __post_init__(self) -> None:
        values = (
            self.candidate_checkpoint,
            self.gate_a_result,
            self.gate_a_classified,
            self.strength_artifact,
            self.result,
            self.classified_result,
            self.retention_backend,
        )
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
    for name in _OUTPUT_NAMES:
        value = locations.get(name)
        if type(value) is not str or not value or "\x00" in value:
            raise _error(f"locked output {name} path is unusable")
        path = Path(value)
        if path.exists():
            raise _error(f"locked output {name} already exists; outputs are write-once")
        parent = path.parent
        if not parent.exists():
            raise _error(f"locked output {name} parent directory does not exist")
        if not parent.is_dir():
            raise _error(f"locked output {name} parent is not a directory")
        if not os.access(parent, os.W_OK):
            raise _error(f"locked output {name} parent directory is not writable")


def lock_identity(document: dict[str, object]) -> str:
    payload = {
        name: value for name, value in document.items() if name != "lock_identity"
    }
    return _sha256_document(payload)


def build_pre_execution_lock(
    evidence: GateAEvidence,
    *,
    locations: P6GateBArtifactLocations,
    ordered_seeds=DEFAULT_ORDERED_SEEDS,
    external_freshness_confirmed: bool,
    additional_allocated_seeds=(),
) -> dict[str, object]:
    if not isinstance(evidence, GateAEvidence):
        raise TypeError("evidence must be GateAEvidence")
    if not isinstance(locations, P6GateBArtifactLocations):
        raise TypeError("locations must be P6GateBArtifactLocations")
    if str(evidence.checkpoint.path) != locations.candidate_checkpoint:
        raise _error("the lock candidate location differs from the loaded checkpoint")
    rebound = load_gate_a_evidence(
        locations.candidate_checkpoint,
        locations.gate_a_result,
        locations.gate_a_classified,
    )
    if rebound.candidate_block() != evidence.candidate_block():
        raise _error("strict #181 checkpoint readback differs from supplied evidence")
    location_document = locations.to_document()
    _require_output_destinations_ready(location_document)
    provenance = collect_execution_provenance()
    require_gate_b_provenance(provenance)
    execution_target = execution_target_block(provenance)
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
        "parent_issue": PARENT_ISSUE,
        "predecessor_issues": list(PREDECESSOR_ISSUES),
        "candidate": rebound.candidate_block(),
        "gate_a_binding": rebound.gate_a_binding(),
        "serving": hybrid_activation_block(),
        "fallback_policy": fallback_policy_block(),
        "comparator": comparator_block(),
        "p6_training": p6_training_block(),
        "plan": plan_block(seeds),
        "classification_rule": dict(CLASSIFICATION_RULE),
        "outcomes": [outcome.value for outcome in P6GateBOutcome],
        "artifact_locations": location_document,
        "seed_freshness": freshness,
        "provenance": execution_provenance_to_dict(provenance),
        "runtime": runtime_block(),
        "execution_target": execution_target,
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
        "parent_issue",
        "predecessor_issues",
        "candidate",
        "gate_a_binding",
        "serving",
        "fallback_policy",
        "comparator",
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
    expected_scalars = {
        "lock_schema_version": LOCK_SCHEMA_VERSION,
        "experiment_id": EXPERIMENT_ID,
        "source_issue": SOURCE_ISSUE,
        "parent_issue": PARENT_ISSUE,
        "predecessor_issues": list(PREDECESSOR_ISSUES),
        "serving": hybrid_activation_block(),
        "fallback_policy": fallback_policy_block(),
        "comparator": comparator_block(),
        "p6_training": p6_training_block(),
        "classification_rule": dict(CLASSIFICATION_RULE),
        "outcomes": [outcome.value for outcome in P6GateBOutcome],
        "no_rescue_boundary": NO_RESCUE_BOUNDARY,
        "result_exposed": False,
    }
    for name, expected in expected_scalars.items():
        if document[name] != expected:
            raise _error(f"pre-execution lock {name} is not the locked value")
    candidate = document["candidate"]
    if candidate != _expected_candidate_block():
        raise _error("pre-execution lock candidate binding drifted")
    gate_a = document["gate_a_binding"]
    if gate_a != {
        "unclassified_result_identity": EXPECTED_GATE_A_RESULT_IDENTITY,
        "classified_result_identity": EXPECTED_GATE_A_CLASSIFIED_IDENTITY,
        "classification": EXPECTED_GATE_A_CLASSIFICATION,
    }:
        raise _error("pre-execution lock Gate A binding drifted")
    plan = document["plan"]
    if type(plan) is not dict or plan != plan_block(plan.get("ordered_seeds", ())):
        raise _error("pre-execution lock plan is invalid")
    freshness = document["seed_freshness"]
    if type(freshness) is not dict or freshness != {
        "ordered_seeds": plan["ordered_seeds"],
        "repository_declared_collisions": [],
        "external_review_collisions": [],
        "external_open_closed_issue_review_confirmed": True,
        "fresh": True,
        "result_exposed": False,
    }:
        raise _error("pre-execution lock seed freshness evidence is invalid")
    if declared_allocated_seeds().intersection(plan["ordered_seeds"]):
        raise _error("pre-execution lock seeds now collide with repository allocations")
    locations = document["artifact_locations"]
    expected_location_fields = {
        "retention_backend",
        "candidate_checkpoint",
        "gate_a_result",
        "gate_a_classified",
        "strength_artifact",
        "result",
        "classified_result",
        "retention_keys",
    }
    if type(locations) is not dict or set(locations) != expected_location_fields:
        raise _error("pre-execution lock artifact locations are invalid")
    if any(
        type(locations[name]) is not str or not locations[name]
        for name in expected_location_fields - {"retention_keys"}
    ):
        raise _error("pre-execution lock artifact location value is invalid")
    if locations.get("retention_backend") != RETENTION_BACKEND:
        raise _error("pre-execution lock retention backend drifted")
    keys = locations.get("retention_keys")
    if keys != {
        "strength_artifact": ARTIFACT_RETENTION_KEY,
        "result": RESULT_RETENTION_KEY,
        "classified_result": CLASSIFIED_RESULT_RETENTION_KEY,
    }:
        raise _error("pre-execution lock retention keys drifted")
    provenance = parse_execution_provenance(document["provenance"])
    require_gate_b_provenance(provenance)
    if document["runtime"] != runtime_block():
        raise _error("pre-execution lock runtime drifted")
    target = document["execution_target"]
    if (
        type(target) is not dict
        or target.get("merged_main_revision") != provenance.lisjong_arena_revision
        or target.get("head_matches_merged_main") is not True
    ):
        raise _error("pre-execution lock execution target is invalid")
    identity = document["lock_identity"]
    if type(identity) is not str or len(identity) != 64:
        raise _error("pre-execution lock identity is invalid")
    if identity != lock_identity(document):
        raise _error("pre-execution lock identity does not match its content")
    return document


def require_pre_execution_comment_url(value: str) -> str:
    if type(value) is not str or _LOCK_COMMENT_URL.fullmatch(value) is None:
        raise _error(
            "pre-execution comment URL must be an exact Issue #183 comment URL"
        )
    return value


def build_gate_b_plan(
    checkpoint: LoadedP6Checkpoint, ordered_seeds=DEFAULT_ORDERED_SEEDS
) -> tuple[SingleRoundEvaluationPlan, PolicyInstanceRegistry]:
    if not isinstance(checkpoint, LoadedP6Checkpoint):
        raise TypeError("checkpoint must be a LoadedP6Checkpoint")
    if checkpoint.candidate_identity != EXPECTED_CANDIDATE_IDENTITY:
        raise _error("the served checkpoint is not the exact #181 P6 candidate")
    seeds = require_seed_plan(ordered_seeds)
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
        seeds=seeds,
    )
    return plan, registry


def require_gate_b_artifact(
    artifact: SingleRoundStrengthArtifact, *, candidate_identity: str, ordered_seeds
) -> SingleRoundStrengthArtifact:
    if not isinstance(artifact, SingleRoundStrengthArtifact):
        raise TypeError("artifact must be a SingleRoundStrengthArtifact")
    seeds = require_seed_plan(ordered_seeds)
    plan = artifact.plan
    if plan.seeds != seeds:
        raise _error("artifact seeds differ from the locked Gate B population")
    if plan.game_mode != GAME_MODE or plan.rotation_count != ROTATIONS_PER_SEED:
        raise _error("artifact game mode or rotation count drifted")
    if len(artifact.game_results) != GAME_COUNT:
        raise _error(f"artifact must contain exactly {GAME_COUNT} games")
    if plan.candidate_identity != candidate_identity:
        raise _error("artifact candidate identity differs from the served candidate")
    if plan.baseline_identity != PASSIVE_TSUMOGIRI_IDENTITY:
        raise _error("artifact baseline is not the locked passive comparator")
    seat_counts: dict[int, int] = {}
    for game_result in artifact.game_results:
        seat_counts[game_result.candidate_seat] = (
            seat_counts.get(game_result.candidate_seat, 0) + 1
        )
    if sorted(int(seat) for seat in seat_counts) != [0, 1, 2, 3] or set(
        seat_counts.values()
    ) != {SEED_BLOCK_COUNT}:
        raise _error("candidate did not occupy each seat exactly once per seed block")
    return artifact


def artifact_block(
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


def serving_diagnostics_block(
    diagnostics: ActivationDiagnostics,
) -> dict[str, object]:
    return {
        **diagnostics.to_document(),
        "illegal_selection_count": 0,
        "non_finite_q_output_count": 0,
        "resolve_failure_count": 0,
        "fail_closed_at_decision_time": True,
    }


def _result_identity(document: dict[str, object]) -> str:
    payload = {**document, "classification": None, "result_identity": None}
    return _sha256_document(payload)


def build_result(
    *,
    lock: dict[str, object],
    pre_execution_comment_url: str,
    checkpoint: LoadedP6Checkpoint,
    artifact: SingleRoundStrengthArtifact,
    artifact_path: Path,
    summary,
    diagnostics: ActivationDiagnostics,
) -> dict[str, object]:
    validate_pre_execution_lock(lock)
    require_pre_execution_comment_url(pre_execution_comment_url)
    document: dict[str, object] = {
        "result_schema_version": RESULT_SCHEMA_VERSION,
        "experiment_id": EXPERIMENT_ID,
        "source_issue": SOURCE_ISSUE,
        "parent_issue": PARENT_ISSUE,
        "lock_identity": lock["lock_identity"],
        "lock_comment_url": pre_execution_comment_url,
        "candidate": lock["candidate"],
        "gate_a_binding": lock["gate_a_binding"],
        "serving": lock["serving"],
        "fallback_policy": lock["fallback_policy"],
        "comparator": comparator_block(),
        "plan": lock["plan"],
        "strength_artifact": artifact_block(artifact, artifact_path),
        "canonical_summary": summary_to_dict(summary),
        "serving_diagnostics": serving_diagnostics_block(diagnostics),
        "classification_rule": dict(CLASSIFICATION_RULE),
        "limitations": list(LIMITATIONS),
        "no_rescue_boundary": NO_RESCUE_BOUNDARY,
        "provenance": execution_provenance_to_dict(collect_execution_provenance()),
        "classification": None,
        "result_identity": None,
    }
    if checkpoint.candidate_identity != document["candidate"]["candidate_identity"]:
        raise _error("result checkpoint identity differs from the locked candidate")
    if artifact.provenance != parse_execution_provenance(lock["provenance"]):
        raise _error("result artifact provenance differs from the posted lock")
    document["provenance"] = execution_provenance_to_dict(artifact.provenance)
    document["result_identity"] = _result_identity(document)
    return validate_result(document)


def _summary_statistics(document: dict[str, object]) -> dict[str, object]:
    summary = document.get("canonical_summary")
    if type(summary) is not dict:
        raise _error("canonical_summary must be an object")
    statistics = summary.get("seed_block_statistics")
    if type(statistics) is not dict:
        raise _error("seed_block_statistics must be an object")
    if statistics.get("seed_block_count") != SEED_BLOCK_COUNT:
        raise _error("seed_block_statistics do not cover the locked 25 blocks")
    lower = statistics.get("normal_approx_95_interval_lower")
    upper = statistics.get("normal_approx_95_interval_upper")
    if type(lower) not in (int, float) or type(upper) not in (int, float):
        raise _error("Gate B classification interval is undefined")
    return statistics


def derive_classification(document: object) -> P6GateBOutcome:
    validated = validate_result(document, allow_classified=True)
    statistics = _summary_statistics(validated)
    lower = float(statistics["normal_approx_95_interval_lower"])
    upper = float(statistics["normal_approx_95_interval_upper"])
    if lower > 0:
        return P6GateBOutcome.POSITIVE_SIGNAL
    if upper < 0:
        return P6GateBOutcome.NEGATIVE_SIGNAL
    return P6GateBOutcome.INCONCLUSIVE


def validate_result(
    document: object, *, allow_classified: bool = False
) -> dict[str, object]:
    if type(document) is not dict:
        raise _error("P6 Gate B result must be an object")
    required = {
        "result_schema_version",
        "experiment_id",
        "source_issue",
        "parent_issue",
        "lock_identity",
        "lock_comment_url",
        "candidate",
        "gate_a_binding",
        "serving",
        "fallback_policy",
        "comparator",
        "plan",
        "strength_artifact",
        "canonical_summary",
        "serving_diagnostics",
        "classification_rule",
        "limitations",
        "no_rescue_boundary",
        "provenance",
        "classification",
        "result_identity",
    }
    if set(document) != required:
        raise _error("P6 Gate B result fields are invalid")
    if (
        document["result_schema_version"] != RESULT_SCHEMA_VERSION
        or document["experiment_id"] != EXPERIMENT_ID
        or document["source_issue"] != SOURCE_ISSUE
        or document["parent_issue"] != PARENT_ISSUE
    ):
        raise _error("P6 Gate B result identity fields drifted")
    require_pre_execution_comment_url(document["lock_comment_url"])
    candidate = document["candidate"]
    if candidate != _expected_candidate_block():
        raise _error("P6 Gate B result candidate binding drifted")
    if document["gate_a_binding"] != {
        "unclassified_result_identity": EXPECTED_GATE_A_RESULT_IDENTITY,
        "classified_result_identity": EXPECTED_GATE_A_CLASSIFIED_IDENTITY,
        "classification": EXPECTED_GATE_A_CLASSIFICATION,
    }:
        raise _error("P6 Gate B result Gate A binding drifted")
    if (
        document["serving"] != hybrid_activation_block()
        or document["fallback_policy"] != fallback_policy_block()
        or document["comparator"] != comparator_block()
    ):
        raise _error("P6 Gate B serving/comparator binding drifted")
    plan = document["plan"]
    if type(plan) is not dict or plan != plan_block(plan.get("ordered_seeds", ())):
        raise _error("P6 Gate B result plan drifted")
    artifact = document["strength_artifact"]
    if (
        type(artifact) is not dict
        or artifact.get("schema_version") != SINGLE_ROUND_ARTIFACT_SCHEMA_VERSION
        or artifact.get("evaluation_protocol") != SINGLE_ROUND_EVALUATION_PROTOCOL
        or artifact.get("game_count") != GAME_COUNT
        or artifact.get("retention")
        != {"backend": RETENTION_BACKEND, "key": ARTIFACT_RETENTION_KEY}
    ):
        raise _error("P6 Gate B strength artifact binding is invalid")
    digest = artifact.get("sha256")
    if type(digest) is not str or len(digest) != 64:
        raise _error("P6 Gate B strength artifact digest is invalid")
    filename = artifact.get("filename")
    if type(filename) is not str or not filename or "/" in filename or "\\" in filename:
        raise _error("P6 Gate B strength artifact filename must be a bare name")
    _summary_statistics(document)
    diagnostics = document["serving_diagnostics"]
    if type(diagnostics) is not dict:
        raise _error("P6 Gate B serving diagnostics are invalid")
    for name in (
        "policy_instance_count",
        "total_decisions",
        "total_activations",
        "total_scaffold_fallbacks",
        "total_support_fallbacks",
        "illegal_selection_count",
        "non_finite_q_output_count",
        "resolve_failure_count",
    ):
        if type(diagnostics.get(name)) is not int or diagnostics[name] < 0:
            raise _error(f"P6 Gate B diagnostic {name} is invalid")
    if (
        diagnostics["illegal_selection_count"] != 0
        or diagnostics["non_finite_q_output_count"] != 0
        or diagnostics["resolve_failure_count"] != 0
        or diagnostics.get("fail_closed_at_decision_time") is not True
    ):
        raise _error("P6 Gate B serving failure counters are not zero")
    if (
        document["classification_rule"] != CLASSIFICATION_RULE
        or document["limitations"] != list(LIMITATIONS)
        or document["no_rescue_boundary"] != NO_RESCUE_BOUNDARY
    ):
        raise _error("P6 Gate B scientific boundary drifted")
    provenance = parse_execution_provenance(document["provenance"])
    require_gate_b_provenance(provenance)
    classification = document["classification"]
    if classification is not None:
        if not allow_classified:
            raise _error(
                "unclassified P6 Gate B result unexpectedly has a classification"
            )
        if classification not in {outcome.value for outcome in _RECORDABLE_OUTCOMES}:
            raise _error("P6 Gate B classification is invalid")
    identity = document["result_identity"]
    if type(identity) is not str or len(identity) != 64:
        raise _error("P6 Gate B result identity is invalid")
    if identity != _result_identity(document):
        raise _error("P6 Gate B result identity does not match its content")
    if classification is not None:
        derived = derive_classification({**document, "classification": None})
        if classification != derived.value:
            raise _error("recorded P6 Gate B classification is not derivable")
    return document


def record_classification(
    document: object, outcome: P6GateBOutcome
) -> dict[str, object]:
    validated = validate_result(document)
    if not isinstance(outcome, P6GateBOutcome):
        raise TypeError("outcome must be a P6GateBOutcome")
    if outcome not in _RECORDABLE_OUTCOMES:
        raise _error("blocked/invalid outcomes are not recorded as valid measurements")
    expected = derive_classification(validated)
    if outcome is not expected:
        raise _error("requested classification differs from the locked interval rule")
    classified = {**validated, "classification": outcome.value}
    return validate_result(classified, allow_classified=True)


@dataclass(frozen=True, slots=True)
class P6GateBMeasurement:
    artifact: SingleRoundStrengthArtifact
    document: dict[str, object]
    classified: dict[str, object]
    outcome: P6GateBOutcome
    wall_clock_seconds: float
    cpu_seconds: float


def run_gate_b(
    lock_document: object,
    *,
    pre_execution_comment_url: str,
    progress_callback=None,
) -> P6GateBMeasurement:
    lock = validate_pre_execution_lock(lock_document)
    require_pre_execution_comment_url(pre_execution_comment_url)
    target_revision = lock["execution_target"]["merged_main_revision"]
    if _require_clean_arena_head() != target_revision:
        raise _error("live Arena HEAD differs from the locked merged main revision")
    live_provenance = collect_execution_provenance()
    require_gate_b_provenance(live_provenance)
    if execution_provenance_to_dict(live_provenance) != lock["provenance"]:
        raise _error("live execution provenance differs from the posted lock")
    if runtime_block() != lock["runtime"]:
        raise _error("live runtime differs from the posted lock")
    locations = lock["artifact_locations"]
    _require_output_destinations_ready(locations)
    evidence = load_gate_a_evidence(
        locations["candidate_checkpoint"],
        locations["gate_a_result"],
        locations["gate_a_classified"],
    )
    if evidence.candidate_block() != lock["candidate"]:
        raise _error("live #181 candidate binding differs from the posted lock")
    plan, registry = build_gate_b_plan(
        evidence.checkpoint, lock["plan"]["ordered_seeds"]
    )

    wall_started = time.perf_counter()
    cpu_started = time.process_time()
    evaluation = run_single_round_evaluation(plan, progress_callback=progress_callback)
    wall_clock_seconds = time.perf_counter() - wall_started
    cpu_seconds = time.process_time() - cpu_started

    strength_path = Path(locations["strength_artifact"])
    save_single_round_artifact(evaluation, strength_path)
    artifact = load_single_round_artifact(strength_path)
    require_gate_b_artifact(
        artifact,
        candidate_identity=evidence.checkpoint.candidate_identity,
        ordered_seeds=lock["plan"]["ordered_seeds"],
    )
    if artifact.provenance != parse_execution_provenance(lock["provenance"]):
        raise _error("artifact execution provenance does not match the posted lock")
    summary = summarize_single_round_strength(
        aggregate_candidate_metrics(
            artifact.plan.candidate_identity, artifact.game_results
        ),
        artifact.game_results,
    )
    if summary != artifact.summary:
        raise _error(
            "canonical summary regenerated from raw games differs from the artifact"
        )
    diagnostics = collect_activation_diagnostics(registry.instances)
    result = build_result(
        lock=lock,
        pre_execution_comment_url=pre_execution_comment_url,
        checkpoint=evidence.checkpoint,
        artifact=artifact,
        artifact_path=strength_path,
        summary=summary,
        diagnostics=diagnostics,
    )
    result_path = Path(locations["result"])
    write_new_artifact_file(result_path, canonical_json_text(result))
    strict_result = read_json_document(result_path)
    validate_result(strict_result)
    outcome = derive_classification(strict_result)
    classified = record_classification(strict_result, outcome)
    classified_path = Path(locations["classified_result"])
    write_new_artifact_file(classified_path, canonical_json_text(classified))
    strict_classified = read_json_document(classified_path)
    if strict_classified != classified:
        raise _error("classified result strict readback differs")
    validate_result(strict_classified, allow_classified=True)
    return P6GateBMeasurement(
        artifact=artifact,
        document=strict_result,
        classified=strict_classified,
        outcome=outcome,
        wall_clock_seconds=wall_clock_seconds,
        cpu_seconds=cpu_seconds,
    )


def _write_lock(path: Path, document: dict[str, object]) -> None:
    if not path.parent.is_dir():
        raise _error("lock output parent directory must already exist")
    write_new_artifact_file(path, canonical_json_text(document))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Issue #183 P6 Gate B protocol")
    subparsers = parser.add_subparsers(dest="command", required=True)

    lock = subparsers.add_parser("lock")
    lock.add_argument("--candidate-checkpoint", required=True)
    lock.add_argument("--gate-a-result", required=True)
    lock.add_argument("--gate-a-classified", required=True)
    lock.add_argument("--strength-artifact", required=True)
    lock.add_argument("--result", required=True)
    lock.add_argument("--classified-result", required=True)
    lock.add_argument("--lock-output", required=True)
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
            evidence = load_gate_a_evidence(
                arguments.candidate_checkpoint,
                arguments.gate_a_result,
                arguments.gate_a_classified,
            )
            locations = P6GateBArtifactLocations(
                candidate_checkpoint=arguments.candidate_checkpoint,
                gate_a_result=arguments.gate_a_result,
                gate_a_classified=arguments.gate_a_classified,
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
            print(canonical_json_text(document), end="")
            return 0

        lock_document = read_json_document(Path(arguments.lock_file))
        measurement = run_gate_b(
            lock_document,
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
    "GateAEvidence",
    "P6GateBArtifactLocations",
    "P6GateBMeasurement",
    "P6GateBOutcome",
    "build_gate_b_plan",
    "build_pre_execution_lock",
    "derive_classification",
    "load_gate_a_evidence",
    "main",
    "record_classification",
    "require_gate_b_artifact",
    "require_pre_execution_comment_url",
    "run_gate_b",
    "seed_freshness_block",
    "validate_pre_execution_lock",
    "validate_result",
]
