"""#173 exact guarded candidate vs locked yakuhai-call screen (Issue #175).

This purpose-specific module changes evaluation fidelity only.  It reuses the
exact #162 serving checkpoint, the #173 selection guard, and the existing ABBB
single-round evaluator/artifact.  It deliberately contains no training path.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from enum import Enum
from math import isfinite
from pathlib import Path

from lisjong.policies import (
    YakuhaiCallGenbutsuDefenseFiniteHorizonHandValueAwarePolicy,
)

from lisjong_arena._artifact_io import canonical_json_text, write_new_artifact_file
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
from .p1_candidate import (
    LOCKED_P1_CANDIDATE,
    LOCKED_SELECTED_EPOCH,
    MATERIALIZATION_SOURCES,
    SERVING_CHECKPOINT_SCHEMA_VERSION,
    LoadedP1ServingCheckpoint,
    candidate_binding_document,
    load_p1_serving_checkpoint,
    verify_locked_candidate_contract,
)
from .p1_serving import create_p1_hybrid_runtime
from .p1_shanten_guard import (
    GUARD_CANDIDATE_BINDING_SCHEMA_VERSION,
    GUARD_SEMANTICS_ID,
    GuardDiagnostics,
    collect_guard_diagnostics,
    guard_binding_document,
    guard_candidate_identity,
    guarded_policy_factory,
    require_guard_candidate_identity,
)
from .p1_shanten_guard_diagnostic import (
    ORDERED_SEEDS as ISSUE_173_ORDERED_SEEDS,
)
from .p1_shanten_guard_diagnostic import declared_allocated_seeds as declared_before_173
from .strength import (
    ActivationDiagnostics,
    PolicyInstanceRegistry,
    collect_activation_diagnostics,
)

LOCK_SCHEMA_VERSION = "arena-learned-policy-offlineq-p1-guarded-higher-fidelity-lock-v1"
RESULT_SCHEMA_VERSION = (
    "arena-learned-policy-offlineq-p1-guarded-higher-fidelity-result-v1"
)
EXPERIMENT_ID = "arena-learned-policy-offlineq-p1-guarded-higher-fidelity-175"
SOURCE_ISSUE = "lisbun/lisjong-arena#175"
PARENT_ISSUE = "lisbun/lisjong-project#45"
PREDECESSOR_ISSUE = "lisbun/lisjong-arena#173"

DEFAULT_ORDERED_SEEDS = tuple(range(547, 572))
SEED_BLOCK_COUNT = 25
ROTATIONS_PER_SEED = SINGLE_ROUND_ROTATION_COUNT
GAME_COUNT = SEED_BLOCK_COUNT * ROTATIONS_PER_SEED
GAME_MODE = SINGLE_ROUND_GAME_MODE
MAX_WORKERS = 1
FORMAL_TEST = False
ROLE = "DEVELOPMENT HIGHER-FIDELITY SCREEN"
EXECUTION_TARGET_REF = "refs/remotes/origin/main"

BASELINE_IDENTITY = "yakuhai-call"
BASELINE_FACTORY = "lisjong_arena.policy_catalog.create_yakuhai_call"
BASELINE_IMPLEMENTATION = "YakuhaiCallGenbutsuDefenseFiniteHorizonHandValueAwarePolicy"
BASELINE_IMPLEMENTATION_MODULE = (
    "lisjong.policies.yakuhai_call_genbutsu_defense_finite_horizon_hand_value_aware"
)
BASELINE_IMPLEMENTATION_SOURCE_REVISION = "a0666d24e66179a45fd6e231a3cbd489b492d162"
BASELINE_SELECTED_LISJONG_REVISION = "4a5c1c724739882eb33a6915b278afb3697162ad"
LOCKED_ENGINE_REVISION = "11e83d03fe06f9277eb5d6c6b9b41cc25a142bba"

EXPECTED_BASE_CANDIDATE_IDENTITY = (
    "learned-offlineq-p1-gateb:"
    "a779aea609aef0cbb0982f3f3d67b0095c7eab9ee8ef4414af488a295468eb5e"
)
EXPECTED_GUARDED_CANDIDATE_IDENTITY = (
    "learned-offlineq-p1-shanten-guard:"
    "b2daad85781aafb53dc7364a89fb458b3e7174f7a86e5a8258688f757b0fdc78"
)

RETENTION_BACKEND = "operator-local-durable"
CANDIDATE_RETENTION_KEY = "offlineq-162-p1-gate-b/candidate"
ARTIFACT_RETENTION_KEY = "offlineq-175-guarded-higher-fidelity/strength-artifact"
RESULT_RETENTION_KEY = "offlineq-175-guarded-higher-fidelity/result"
CLASSIFIED_RESULT_RETENTION_KEY = (
    "offlineq-175-guarded-higher-fidelity/classified-result"
)

PRIMARY_CHANGED_AXIS = "evaluation fidelity only"
LOCKED_UNCHANGED_AXES = (
    "exact #162 checkpoint and model weights",
    "P1 feature and TRAIN support",
    "action vocabulary and Q argmax semantics",
    "hybrid activation and yakuhai-call fallback",
    "exact #173 shanten guard semantics",
    "training, teacher, reward, gamma, and architecture",
)
CLASSIFICATION_RULE = {
    "primary_metric": "seed-block G-vs-C score delta",
    "interval": "normal-approx 95% interval over seed blocks",
    "positive": "normal_approx_95_interval_lower > 0",
    "negative": "normal_approx_95_interval_upper < 0",
    "inconclusive": "otherwise",
    "secondary_metrics_may_alter_classification": False,
    "serving_diagnostics_may_alter_classification": False,
}
NO_RESCUE_BOUNDARY = (
    "After result exposure there is no seed extension, replacement seed, rescue "
    "rerun, comparator substitution, guard change, or candidate change."
)

_LOCK_COMMENT_URL = re.compile(
    r"https://github\.com/lisbun/lisjong-arena/issues/175#issuecomment-\d+\Z"
)
_FULL_COMMIT_ID = re.compile(r"[0-9a-f]{40}\Z").fullmatch
_ARENA_SOURCE_DIRECTORY = Path(__file__).resolve().parent
_GIT_TIMEOUT_SECONDS = 30


class HigherFidelityError(OfflineQError):
    """Issue #175 lock, execution, artifact, or result contract violation."""


class HigherFidelityOutcome(Enum):
    SIGNAL = "GUARDED CANDIDATE HIGHER-FIDELITY SIGNAL"
    NEGATIVE = "GUARDED CANDIDATE HIGHER-FIDELITY NEGATIVE"
    INCONCLUSIVE = "GUARDED CANDIDATE HIGHER-FIDELITY INCONCLUSIVE"
    EVIDENCE_BLOCKED = "GUARDED CANDIDATE EVIDENCE BLOCKED"
    STOP_INVALID = "STOP / INVALID"


_RESULT_OUTCOMES = frozenset(
    {
        HigherFidelityOutcome.SIGNAL,
        HigherFidelityOutcome.NEGATIVE,
        HigherFidelityOutcome.INCONCLUSIVE,
    }
)


def _error(message: str) -> HigherFidelityError:
    return HigherFidelityError(message)


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _locked_base_binding() -> dict[str, object]:
    return candidate_binding_document(
        canonical_model_weights_digest=(
            LOCKED_P1_CANDIDATE.canonical_model_weights_digest
        ),
        support_set_digest=LOCKED_P1_CANDIDATE.support_set_digest,
    )


if guard_candidate_identity(_locked_base_binding()) != (
    EXPECTED_GUARDED_CANDIDATE_IDENTITY
):
    raise RuntimeError("the exact #173 guarded candidate identity drifted")


def declared_allocated_seeds() -> frozenset[int]:
    """Return repository-declared populations preceding Issue #175."""
    return frozenset(declared_before_173()) | frozenset(ISSUE_173_ORDERED_SEEDS)


def require_seed_plan(ordered_seeds) -> tuple[int, ...]:
    """Accept only a pre-result contiguous 25-seed development population."""
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
    """Validate repository constants plus the operator's live Issue recheck."""
    seeds = require_seed_plan(ordered_seeds)
    if type(external_freshness_confirmed) is not bool:
        raise TypeError("external_freshness_confirmed must be an exact bool")
    if not external_freshness_confirmed:
        raise _error(
            "open and closed relevant Issues must be checked before the lock is built"
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
            "SEED PLAN REFORMULATE: collision found before result exposure; "
            "choose a fresh contiguous 25-seed range before building the lock"
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
        "candidate_arm": "G",
        "baseline_arm": "C",
    }


def candidate_block(checkpoint: LoadedP1ServingCheckpoint) -> dict[str, object]:
    """Bind the exact #162 bytes and exact #173 guard semantics."""
    if not isinstance(checkpoint, LoadedP1ServingCheckpoint):
        raise TypeError("checkpoint must be a LoadedP1ServingCheckpoint")
    manifest = checkpoint.manifest
    base_binding = manifest["candidate_binding"]
    block = {
        "identity": guard_candidate_identity(base_binding),
        "binding": guard_binding_document(base_binding),
        "base_candidate_identity": checkpoint.candidate_identity,
        "checkpoint_schema_version": manifest["checkpoint_schema_version"],
        "canonical_model_weights_digest": checkpoint.canonical_model_weights_digest,
        "source_dataset_identity": manifest["source_dataset_identity"],
        "p1_feature_fingerprint": base_binding["p1_feature"]["schema_fingerprint"],
        "supported_indices_digest": manifest["supported_indices_digest"],
        "action_vocabulary_version": base_binding["action_vocabulary"]["version"],
        "action_vocabulary_fingerprint": base_binding["action_vocabulary"][
            "fingerprint"
        ],
        "guard_semantics_id": GUARD_SEMANTICS_ID,
        "guard_binding_schema_version": GUARD_CANDIDATE_BINDING_SCHEMA_VERSION,
        "selected_epoch": manifest["selected_epoch"],
        "materialization_source": manifest["materialization_source"],
        "real_candidate_materialization": manifest["real_candidate_materialization"],
        "retention": {
            "backend": RETENTION_BACKEND,
            "key": CANDIDATE_RETENTION_KEY,
        },
    }
    _validate_candidate_block(block)
    return block


def _validate_candidate_block(block: object) -> dict:
    fields = {
        "identity",
        "binding",
        "base_candidate_identity",
        "checkpoint_schema_version",
        "canonical_model_weights_digest",
        "source_dataset_identity",
        "p1_feature_fingerprint",
        "supported_indices_digest",
        "action_vocabulary_version",
        "action_vocabulary_fingerprint",
        "guard_semantics_id",
        "guard_binding_schema_version",
        "selected_epoch",
        "materialization_source",
        "real_candidate_materialization",
        "retention",
    }
    if type(block) is not dict or set(block) != fields:
        raise _error("candidate block fields are invalid")
    base = _locked_base_binding()
    expected = {
        "identity": EXPECTED_GUARDED_CANDIDATE_IDENTITY,
        "binding": guard_binding_document(base),
        "base_candidate_identity": EXPECTED_BASE_CANDIDATE_IDENTITY,
        "checkpoint_schema_version": SERVING_CHECKPOINT_SCHEMA_VERSION,
        "canonical_model_weights_digest": (
            LOCKED_P1_CANDIDATE.canonical_model_weights_digest
        ),
        "source_dataset_identity": LOCKED_P1_CANDIDATE.source_dataset_identity,
        "p1_feature_fingerprint": base["p1_feature"]["schema_fingerprint"],
        "supported_indices_digest": LOCKED_P1_CANDIDATE.support_set_digest,
        "action_vocabulary_version": base["action_vocabulary"]["version"],
        "action_vocabulary_fingerprint": base["action_vocabulary"]["fingerprint"],
        "guard_semantics_id": GUARD_SEMANTICS_ID,
        "guard_binding_schema_version": GUARD_CANDIDATE_BINDING_SCHEMA_VERSION,
        "selected_epoch": LOCKED_SELECTED_EPOCH,
        "real_candidate_materialization": True,
        "retention": {
            "backend": RETENTION_BACKEND,
            "key": CANDIDATE_RETENTION_KEY,
        },
    }
    for name, value in expected.items():
        if block[name] != value:
            raise _error(f"candidate {name} is not the exact #173 candidate value")
    if block["materialization_source"] not in MATERIALIZATION_SOURCES:
        raise _error("candidate materialization_source is invalid")
    require_guard_candidate_identity(block["identity"], base)
    return block


def baseline_block() -> dict[str, object]:
    """Bind the Stage 6 comparator without a mutable champion alias."""
    if POLICY_CATALOG.get(BASELINE_IDENTITY) is None:
        raise _error("the curated yakuhai-call baseline is unavailable")
    if POLICY_CATALOG[BASELINE_IDENTITY].factory is not create_yakuhai_call:
        raise _error("the yakuhai-call catalog factory drifted")
    policy = create_yakuhai_call()
    if type(policy) is not YakuhaiCallGenbutsuDefenseFiniteHorizonHandValueAwarePolicy:
        raise _error("the yakuhai-call implementation class drifted")
    return {
        "identity": BASELINE_IDENTITY,
        "factory": BASELINE_FACTORY,
        "implementation_class": BASELINE_IMPLEMENTATION,
        "implementation_module": BASELINE_IMPLEMENTATION_MODULE,
        "implementation_source_revision": BASELINE_IMPLEMENTATION_SOURCE_REVISION,
        "stage6_selected_lisjong_revision": BASELINE_SELECTED_LISJONG_REVISION,
        "mutable_alias": False,
    }


def require_baseline_provenance(provenance: SingleRoundExecutionProvenance) -> None:
    baseline_block()
    if provenance.lisjong_revision != BASELINE_SELECTED_LISJONG_REVISION:
        raise _error(
            "BASELINE PLAN REFORMULATE: installed lisjong revision differs from "
            "the Stage 6 selected baseline revision"
        )
    if provenance.lisjong_engine_revision != LOCKED_ENGINE_REVISION:
        raise _error(
            "the installed lisjong-engine revision differs from the locked dependency"
        )


@dataclass(frozen=True, slots=True)
class HigherFidelityArtifactLocations:
    candidate_checkpoint: str
    strength_artifact: str
    result: str
    classified_result: str
    retention_backend: str = RETENTION_BACKEND

    def __post_init__(self) -> None:
        values = (
            self.candidate_checkpoint,
            self.strength_artifact,
            self.result,
            self.classified_result,
            self.retention_backend,
        )
        if any(type(value) is not str or not value.strip() for value in values):
            raise HigherFidelityError("artifact locations must be non-empty strings")
        outputs = (self.strength_artifact, self.result, self.classified_result)
        if len(set(outputs)) != len(outputs):
            raise HigherFidelityError("each output must have a distinct location")

    def to_document(self) -> dict[str, object]:
        return {
            "retention_backend": self.retention_backend,
            "candidate_checkpoint": self.candidate_checkpoint,
            "strength_artifact": self.strength_artifact,
            "result": self.result,
            "classified_result": self.classified_result,
            "retention_keys": {
                "candidate_checkpoint": CANDIDATE_RETENTION_KEY,
                "strength_artifact": ARTIFACT_RETENTION_KEY,
                "result": RESULT_RETENTION_KEY,
                "classified_result": CLASSIFIED_RESULT_RETENTION_KEY,
            },
        }


def runtime_block() -> dict[str, object]:
    import importlib.metadata
    import platform

    import torch

    return {
        "python_version": platform.python_version(),
        "torch_version": str(torch.__version__),
        "riichienv_version": importlib.metadata.version("riichienv"),
    }


def _git_output(*arguments: str) -> str:
    try:
        completed = subprocess.run(
            ("git", "-C", str(_ARENA_SOURCE_DIRECTORY), *arguments),
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise _error(
            f"Arena execution target cannot be verified: git {arguments[0]} "
            "could not be executed"
        ) from exc
    if completed.returncode != 0:
        raise _error(
            f"Arena execution target cannot be verified: git {arguments[0]} failed"
        )
    return completed.stdout


def _require_clean_arena_head() -> str:
    """Return HEAD only when the complete Arena worktree is clean."""
    if _git_output("status", "--porcelain").strip():
        raise _error(
            "Arena worktree must be clean before the pre-execution lock or run"
        )
    revision = _git_output("rev-parse", "--verify", "HEAD^{commit}").strip()
    if _FULL_COMMIT_ID(revision) is None:
        raise _error("Arena HEAD is not a lowercase full commit ID")
    return revision


def _resolve_execution_target_revision() -> str:
    revision = _git_output(
        "rev-parse", "--verify", f"{EXECUTION_TARGET_REF}^{{commit}}"
    ).strip()
    if _FULL_COMMIT_ID(revision) is None:
        raise _error("merged main target is not a lowercase full commit ID")
    return revision


def execution_target_block(
    provenance: SingleRoundExecutionProvenance,
) -> dict[str, object]:
    """Bind the live clean HEAD to the locally fetched merged main ref."""
    if not isinstance(provenance, SingleRoundExecutionProvenance):
        raise TypeError("provenance must be SingleRoundExecutionProvenance")
    head_revision = _require_clean_arena_head()
    merged_main_revision = _resolve_execution_target_revision()
    if head_revision != provenance.lisjong_arena_revision:
        raise _error("Arena HEAD differs from collected execution provenance")
    if head_revision != merged_main_revision:
        raise _error(
            "PRE-EXECUTION LOCK REJECTED: Arena HEAD is not the fetched merged "
            "origin/main revision"
        )
    return {
        "reference": EXECUTION_TARGET_REF,
        "merged_main_revision": merged_main_revision,
        "head_matches_merged_main": True,
    }


def lock_identity(document: dict) -> str:
    payload = {
        name: value for name, value in document.items() if name != "lock_identity"
    }
    return hashlib.sha256(canonical_json_text(payload).encode()).hexdigest()


def build_pre_execution_lock(
    checkpoint: LoadedP1ServingCheckpoint,
    *,
    locations: HigherFidelityArtifactLocations,
    ordered_seeds=DEFAULT_ORDERED_SEEDS,
    external_freshness_confirmed: bool,
    additional_allocated_seeds=(),
) -> dict[str, object]:
    """Build the exact document that must be posted before real execution."""
    verify_locked_candidate_contract()
    if not isinstance(locations, HigherFidelityArtifactLocations):
        raise TypeError("locations must be HigherFidelityArtifactLocations")
    if str(checkpoint.path) != locations.candidate_checkpoint:
        raise _error("the lock candidate location differs from the loaded checkpoint")
    reloaded = load_p1_serving_checkpoint(checkpoint.path)
    if candidate_block(reloaded) != candidate_block(checkpoint):
        raise _error("strict checkpoint readback differs from the supplied candidate")
    checkpoint = reloaded
    candidate = candidate_block(checkpoint)
    actual_provenance = collect_execution_provenance()
    require_baseline_provenance(actual_provenance)
    execution_target = execution_target_block(actual_provenance)
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
        "predecessor_issue": PREDECESSOR_ISSUE,
        "parent_issue": PARENT_ISSUE,
        "primary_changed_axis": PRIMARY_CHANGED_AXIS,
        "locked_unchanged_axes": list(LOCKED_UNCHANGED_AXES),
        "candidate": candidate,
        "baseline": baseline_block(),
        "plan": plan_block(seeds),
        "classification_rule": dict(CLASSIFICATION_RULE),
        "outcomes": [outcome.value for outcome in HigherFidelityOutcome],
        "artifact_locations": locations.to_document(),
        "seed_freshness": freshness,
        "provenance": execution_provenance_to_dict(actual_provenance),
        "runtime": runtime_block(),
        "execution_target": execution_target,
        "no_rescue_boundary": NO_RESCUE_BOUNDARY,
        "result_exposed": False,
        "lock_identity": None,
    }
    return validate_pre_execution_lock(
        {**document, "lock_identity": lock_identity(document)}
    )


def validate_pre_execution_lock(document: object) -> dict:
    fields = {
        "lock_schema_version",
        "experiment_id",
        "source_issue",
        "predecessor_issue",
        "parent_issue",
        "primary_changed_axis",
        "locked_unchanged_axes",
        "candidate",
        "baseline",
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
    if type(document) is not dict or set(document) != fields:
        raise _error("pre-execution lock fields are invalid")
    expected = {
        "lock_schema_version": LOCK_SCHEMA_VERSION,
        "experiment_id": EXPERIMENT_ID,
        "source_issue": SOURCE_ISSUE,
        "predecessor_issue": PREDECESSOR_ISSUE,
        "parent_issue": PARENT_ISSUE,
        "primary_changed_axis": PRIMARY_CHANGED_AXIS,
        "locked_unchanged_axes": list(LOCKED_UNCHANGED_AXES),
        "baseline": baseline_block(),
        "classification_rule": dict(CLASSIFICATION_RULE),
        "outcomes": [outcome.value for outcome in HigherFidelityOutcome],
        "no_rescue_boundary": NO_RESCUE_BOUNDARY,
        "result_exposed": False,
    }
    for name, value in expected.items():
        if document[name] != value:
            raise _error(f"pre-execution lock {name} is not the locked value")
    _validate_candidate_block(document["candidate"])
    plan = document["plan"]
    if type(plan) is not dict:
        raise _error("pre-execution lock plan is invalid")
    if plan != plan_block(plan.get("ordered_seeds", ())):
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
        raise _error("pre-execution lock seed plan collides with repository evidence")
    provenance = parse_execution_provenance(document["provenance"])
    require_baseline_provenance(provenance)
    if document["execution_target"] != {
        "reference": EXECUTION_TARGET_REF,
        "merged_main_revision": provenance.lisjong_arena_revision,
        "head_matches_merged_main": True,
    }:
        raise _error("pre-execution lock merged main target is invalid")
    runtime = document["runtime"]
    if type(runtime) is not dict or set(runtime) != {
        "python_version",
        "torch_version",
        "riichienv_version",
    }:
        raise _error("pre-execution lock runtime fields are invalid")
    if any(type(runtime[name]) is not str or not runtime[name] for name in runtime):
        raise _error("pre-execution lock runtime values must be non-empty strings")
    locations = document["artifact_locations"]
    if type(locations) is not dict or set(locations) != {
        "retention_backend",
        "candidate_checkpoint",
        "strength_artifact",
        "result",
        "classified_result",
        "retention_keys",
    }:
        raise _error("pre-execution lock artifact locations are invalid")
    HigherFidelityArtifactLocations(
        candidate_checkpoint=locations.get("candidate_checkpoint"),
        strength_artifact=locations.get("strength_artifact"),
        result=locations.get("result"),
        classified_result=locations.get("classified_result"),
        retention_backend=locations.get("retention_backend"),
    )
    if locations.get("retention_keys") != {
        "candidate_checkpoint": CANDIDATE_RETENTION_KEY,
        "strength_artifact": ARTIFACT_RETENTION_KEY,
        "result": RESULT_RETENTION_KEY,
        "classified_result": CLASSIFIED_RESULT_RETENTION_KEY,
    }:
        raise _error("pre-execution lock retention keys are invalid")
    if document["lock_identity"] != lock_identity(document):
        raise _error("pre-execution lock identity does not match its contents")
    return document


def render_pre_execution_lock(document: dict) -> str:
    """Render reviewable Markdown with the canonical machine-readable lock."""
    lock = validate_pre_execution_lock(document)
    candidate = lock["candidate"]
    baseline = lock["baseline"]
    plan = lock["plan"]
    provenance = lock["provenance"]
    lines = [
        "## Issue #175 pre-execution lock",
        "",
        "```text",
        f"lock identity          {lock['lock_identity']}",
        f"result exposed         {lock['result_exposed']}",
        f"arena revision         {provenance['lisjong_arena_revision']}",
        f"execution target       {lock['execution_target']['reference']}",
        f"merged main revision   {lock['execution_target']['merged_main_revision']}",
        f"lisjong revision       {provenance['lisjong_revision']}",
        f"engine revision        {provenance['lisjong_engine_revision']}",
        f"python                 {lock['runtime']['python_version']}",
        f"pytorch                {lock['runtime']['torch_version']}",
        f"riichienv              {lock['runtime']['riichienv_version']}",
        f"candidate              {candidate['identity']}",
        f"base candidate         {candidate['base_candidate_identity']}",
        f"weights digest         {candidate['canonical_model_weights_digest']}",
        f"P1 fingerprint         {candidate['p1_feature_fingerprint']}",
        f"support digest         {candidate['supported_indices_digest']}",
        f"vocabulary fingerprint {candidate['action_vocabulary_fingerprint']}",
        f"guard semantics        {candidate['guard_semantics_id']}",
        f"guard binding          {candidate['guard_binding_schema_version']}",
        f"baseline               {baseline['identity']}",
        f"baseline factory       {baseline['factory']}",
        f"baseline class         {baseline['implementation_class']}",
        f"seeds                  {plan['ordered_seeds'][0]}..{plan['ordered_seeds'][-1]}",
        f"rotations / games      {plan['rotation_count']} / {plan['game_count']}",
        f"mode / workers         {plan['game_mode']} / {plan['max_workers']}",
        f"formal TEST            {plan['formal_test']}",
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
        raise _error(
            "real execution requires the Issue #175 pre-execution lock comment URL"
        )
    return value


def _require_checkpoint_matches_lock(
    checkpoint: LoadedP1ServingCheckpoint, lock: dict
) -> None:
    if candidate_block(checkpoint) != lock["candidate"]:
        raise _error("loaded checkpoint does not match the pre-execution lock")
    if str(checkpoint.path) != lock["artifact_locations"]["candidate_checkpoint"]:
        raise _error("loaded checkpoint path does not match the pre-execution lock")


def build_evaluation_plan(
    checkpoint: LoadedP1ServingCheckpoint, lock_document: dict
) -> tuple[SingleRoundEvaluationPlan, PolicyInstanceRegistry, PolicyInstanceRegistry]:
    lock = validate_pre_execution_lock(lock_document)
    _require_checkpoint_matches_lock(checkpoint, lock)
    runtime = create_p1_hybrid_runtime(
        checkpoint.model, supported_indices=checkpoint.supported_indices
    )
    candidate_registry = PolicyInstanceRegistry(guarded_policy_factory(runtime))
    baseline_registry = PolicyInstanceRegistry(create_yakuhai_call)
    plan = SingleRoundEvaluationPlan(
        candidate=PolicySpec(
            identity=lock["candidate"]["identity"],
            factory=candidate_registry.create_policy,
        ),
        baseline=PolicySpec(
            identity=BASELINE_IDENTITY,
            factory=baseline_registry.create_policy,
        ),
        seeds=tuple(lock["plan"]["ordered_seeds"]),
    )
    return plan, candidate_registry, baseline_registry


def require_higher_fidelity_artifact(
    artifact: SingleRoundStrengthArtifact, lock_document: dict
) -> SingleRoundStrengthArtifact:
    lock = validate_pre_execution_lock(lock_document)
    if not isinstance(artifact, SingleRoundStrengthArtifact):
        raise TypeError("artifact must be SingleRoundStrengthArtifact")
    plan = artifact.plan
    expected_plan = lock["plan"]
    if plan.candidate_identity != lock["candidate"]["identity"]:
        raise _error("artifact candidate identity does not match the lock")
    if plan.baseline_identity != BASELINE_IDENTITY:
        raise _error("artifact baseline identity does not match the lock")
    if list(plan.seeds) != expected_plan["ordered_seeds"]:
        raise _error("artifact seeds do not match the lock")
    if plan.game_mode != GAME_MODE or plan.rotation_count != ROTATIONS_PER_SEED:
        raise _error("artifact protocol shape does not match the lock")
    if len(artifact.game_results) != GAME_COUNT:
        raise _error("partial evaluation artifacts are never accepted")
    if artifact.provenance != parse_execution_provenance(lock["provenance"]):
        raise _error("artifact execution provenance does not match the lock")
    counts = {seat: 0 for seat in range(4)}
    for result in artifact.game_results:
        counts[int(result.candidate_seat)] += 1
    if set(counts.values()) != {SEED_BLOCK_COUNT}:
        raise _error("candidate did not occupy every seat once per seed block")
    return artifact


def _mahjong_metrics(metrics, population: str) -> dict[str, object]:
    return {
        "round_count": metrics.round_count,
        "population": population,
        "mean_round_score_delta": metrics.mean_round_score_delta,
        "win_count": metrics.win_count,
        "win_rate": metrics.win_rate,
        "mean_win_points": metrics.mean_win_points,
        "tenpai_reached_count": metrics.tenpai_reached_count,
        "tenpai_reached_rate": metrics.tenpai_reached_count / metrics.round_count,
        "mean_first_tenpai_turn": metrics.mean_first_tenpai_turn,
        "exhaustive_draw_count": metrics.exhaustive_draw_count,
        "exhaustive_draw_tenpai_count": metrics.exhaustive_draw_tenpai_count,
        "exhaustive_draw_tenpai_rate": metrics.exhaustive_draw_tenpai_rate,
        "deal_in_count": metrics.deal_in_count,
        "deal_in_rate": metrics.deal_in_rate,
        "mean_deal_in_loss": metrics.mean_deal_in_loss,
    }


def secondary_diagnostics_block(artifact, summary) -> dict[str, object]:
    baseline_stats = [
        result.seat_round_stats[seat]
        for result in artifact.game_results
        for seat in range(4)
        if seat != int(result.candidate_seat)
    ]
    return {
        "guarded_candidate": _mahjong_metrics(
            summary.candidate_metrics.mahjong_metrics,
            "guarded candidate G (1 seat per game)",
        ),
        "yakuhai_call_baseline": _mahjong_metrics(
            aggregate_seat_round_stats_metrics(baseline_stats),
            "yakuhai-call baseline C (3 seats per game)",
        ),
    }


def serving_diagnostics_block(
    activation: ActivationDiagnostics, guard: GuardDiagnostics
) -> dict[str, object]:
    if activation.total_activations != guard.learned_decision_count:
        raise _error("guard decisions must equal learned activations")
    return {
        **activation.to_document(),
        "keep_shanten_guard_opportunities": guard.keep_shanten_available_count,
        "guard_induced_action_changes": guard.action_change_count,
        "unguarded_would_worsen_count": guard.unguarded_worsen_count,
        "guarded_selected_worsen_count": (guard.guarded_worsen_among_available_count),
        "illegal_selection_count": 0,
        "non_finite_q_output_count": 0,
        "resolve_failure_count": 0,
    }


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


def result_identity(document: dict) -> str:
    payload = {**document, "classification": None, "result_identity": None}
    return hashlib.sha256(canonical_json_text(payload).encode()).hexdigest()


def build_result(
    *,
    lock_document: dict,
    pre_execution_comment_url: str,
    artifact: SingleRoundStrengthArtifact,
    artifact_path: str | Path,
    summary,
    guard_diagnostics: GuardDiagnostics,
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
    if str(path) != lock["artifact_locations"]["strength_artifact"]:
        raise _error("artifact path does not match the pre-execution lock")
    if baseline_policy_instance_count != GAME_COUNT * 3:
        raise _error("every baseline seat must receive a fresh Policy instance")
    document = {
        "schema_version": RESULT_SCHEMA_VERSION,
        "experiment_id": EXPERIMENT_ID,
        "source_issue": SOURCE_ISSUE,
        "execution_lock": lock,
        "pre_execution_lock_comment_url": comment_url,
        "strength_artifact": artifact_block(artifact, path),
        "canonical_summary": summary_to_dict(summary),
        "secondary_diagnostics": secondary_diagnostics_block(artifact, summary),
        "candidate_serving_diagnostics": serving_diagnostics_block(
            activation_diagnostics, guard_diagnostics
        ),
        "baseline_policy_instance_count": baseline_policy_instance_count,
        "classification_rule": dict(CLASSIFICATION_RULE),
        "no_rescue_boundary": NO_RESCUE_BOUNDARY,
        "result_identity": None,
        "classification": None,
    }
    return validate_result({**document, "result_identity": result_identity(document)})


def _require_nonnegative_int(value: object, name: str) -> int:
    if type(value) is not int or value < 0:
        raise _error(f"{name} must be a non-negative int")
    return value


def _require_rate(value: object, numerator: int, denominator: int, name: str) -> None:
    if type(value) is not float or not isfinite(value):
        raise _error(f"{name} must be a finite float")
    if value != numerator / denominator:
        raise _error(f"{name} is not derivable from its counts")


def _validate_mahjong_metrics(block: object, expected_rounds: int, name: str) -> None:
    fields = {
        "round_count",
        "population",
        "mean_round_score_delta",
        "win_count",
        "win_rate",
        "mean_win_points",
        "tenpai_reached_count",
        "tenpai_reached_rate",
        "mean_first_tenpai_turn",
        "exhaustive_draw_count",
        "exhaustive_draw_tenpai_count",
        "exhaustive_draw_tenpai_rate",
        "deal_in_count",
        "deal_in_rate",
        "mean_deal_in_loss",
    }
    if type(block) is not dict or set(block) != fields:
        raise _error(f"{name} fields are invalid")
    if block["round_count"] != expected_rounds:
        raise _error(f"{name} population is invalid")
    if type(block["population"]) is not str or not block["population"]:
        raise _error(f"{name} population label is invalid")
    wins = _require_nonnegative_int(block["win_count"], f"{name}.win_count")
    tenpai = _require_nonnegative_int(
        block["tenpai_reached_count"], f"{name}.tenpai_reached_count"
    )
    draws = _require_nonnegative_int(
        block["exhaustive_draw_count"], f"{name}.exhaustive_draw_count"
    )
    draw_tenpai = _require_nonnegative_int(
        block["exhaustive_draw_tenpai_count"],
        f"{name}.exhaustive_draw_tenpai_count",
    )
    deal_ins = _require_nonnegative_int(block["deal_in_count"], f"{name}.deal_in_count")
    if any(count > expected_rounds for count in (wins, tenpai, draws, deal_ins)):
        raise _error(f"{name} count exceeds its population")
    if draw_tenpai > draws:
        raise _error(f"{name} exhaustive-draw tenpai count exceeds draws")
    _require_rate(block["win_rate"], wins, expected_rounds, f"{name}.win_rate")
    _require_rate(
        block["tenpai_reached_rate"],
        tenpai,
        expected_rounds,
        f"{name}.tenpai_reached_rate",
    )
    _require_rate(
        block["deal_in_rate"], deal_ins, expected_rounds, f"{name}.deal_in_rate"
    )
    draw_rate = block["exhaustive_draw_tenpai_rate"]
    if draws == 0:
        if draw_rate is not None:
            raise _error(
                f"{name} draw-tenpai rate must be null when there are no draws"
            )
    else:
        _require_rate(
            draw_rate,
            draw_tenpai,
            draws,
            f"{name}.exhaustive_draw_tenpai_rate",
        )


def validate_result(document: object) -> dict:
    fields = {
        "schema_version",
        "experiment_id",
        "source_issue",
        "execution_lock",
        "pre_execution_lock_comment_url",
        "strength_artifact",
        "canonical_summary",
        "secondary_diagnostics",
        "candidate_serving_diagnostics",
        "baseline_policy_instance_count",
        "classification_rule",
        "no_rescue_boundary",
        "result_identity",
        "classification",
    }
    if type(document) is not dict or set(document) != fields:
        raise _error("higher-fidelity result fields are invalid")
    for name, value in (
        ("schema_version", RESULT_SCHEMA_VERSION),
        ("experiment_id", EXPERIMENT_ID),
        ("source_issue", SOURCE_ISSUE),
        ("classification_rule", dict(CLASSIFICATION_RULE)),
        ("no_rescue_boundary", NO_RESCUE_BOUNDARY),
    ):
        if document[name] != value:
            raise _error(f"higher-fidelity result {name} is invalid")
    lock = validate_pre_execution_lock(document["execution_lock"])
    require_pre_execution_comment_url(document["pre_execution_lock_comment_url"])
    artifact = document["strength_artifact"]
    if type(artifact) is not dict or set(artifact) != {
        "schema_version",
        "evaluation_protocol",
        "filename",
        "sha256",
        "game_count",
        "retention",
    }:
        raise _error("higher-fidelity result artifact fields are invalid")
    if artifact["schema_version"] != SINGLE_ROUND_ARTIFACT_SCHEMA_VERSION:
        raise _error("higher-fidelity result artifact schema is invalid")
    if artifact["evaluation_protocol"] != SINGLE_ROUND_EVALUATION_PROTOCOL:
        raise _error("higher-fidelity result artifact protocol is invalid")
    if artifact["game_count"] != GAME_COUNT:
        raise _error("higher-fidelity result artifact game count is invalid")
    if artifact["retention"] != {
        "backend": RETENTION_BACKEND,
        "key": ARTIFACT_RETENTION_KEY,
    }:
        raise _error("higher-fidelity result artifact retention is invalid")
    if type(artifact["sha256"]) is not str or len(artifact["sha256"]) != 64:
        raise _error("higher-fidelity result artifact digest is invalid")
    if (
        Path(lock["artifact_locations"]["strength_artifact"]).name
        != artifact["filename"]
    ):
        raise _error("higher-fidelity result artifact filename differs from the lock")
    summary = document["canonical_summary"]
    if type(summary) is not dict or set(summary) != {
        "candidate_metrics",
        "mean_baseline_score",
        "mean_candidate_game_delta",
        "seed_block_statistics",
    }:
        raise _error("canonical summary fields are invalid")
    stats = summary["seed_block_statistics"]
    if type(stats) is not dict or stats.get("seed_block_count") != SEED_BLOCK_COUNT:
        raise _error("canonical summary seed-block count is invalid")
    lower = stats.get("normal_approx_95_interval_lower")
    upper = stats.get("normal_approx_95_interval_upper")
    classify_interval(lower, upper)
    candidate_metrics = summary["candidate_metrics"]
    if (
        type(candidate_metrics) is not dict
        or candidate_metrics.get("game_count") != GAME_COUNT
    ):
        raise _error("canonical candidate metrics game count is invalid")
    secondary = document["secondary_diagnostics"]
    if type(secondary) is not dict or set(secondary) != {
        "guarded_candidate",
        "yakuhai_call_baseline",
    }:
        raise _error("secondary diagnostics arms are invalid")
    _validate_mahjong_metrics(
        secondary["guarded_candidate"], GAME_COUNT, "guarded_candidate"
    )
    _validate_mahjong_metrics(
        secondary["yakuhai_call_baseline"],
        3 * GAME_COUNT,
        "yakuhai_call_baseline",
    )
    serving = document["candidate_serving_diagnostics"]
    count_fields = (
        "policy_instance_count",
        "total_decisions",
        "total_activations",
        "total_scaffold_fallbacks",
        "total_support_fallbacks",
        "keep_shanten_guard_opportunities",
        "guard_induced_action_changes",
        "unguarded_would_worsen_count",
        "guarded_selected_worsen_count",
        "illegal_selection_count",
        "non_finite_q_output_count",
        "resolve_failure_count",
    )
    rate_fields = (
        "activation_rate",
        "scaffold_fallback_rate",
        "support_fallback_rate",
    )
    if type(serving) is not dict or set(serving) != set(count_fields + rate_fields):
        raise _error("candidate serving diagnostics fields are invalid")
    for name in count_fields:
        _require_nonnegative_int(serving[name], name)
    if serving["policy_instance_count"] != GAME_COUNT:
        raise _error("candidate must receive one fresh Policy instance per game")
    decisions = serving["total_decisions"]
    activations = serving["total_activations"]
    scaffold = serving["total_scaffold_fallbacks"]
    support = serving["total_support_fallbacks"]
    if decisions == 0 or activations + scaffold + support != decisions:
        raise _error("candidate serving paths do not partition decisions")
    _require_rate(serving["activation_rate"], activations, decisions, "activation_rate")
    _require_rate(
        serving["scaffold_fallback_rate"],
        scaffold,
        decisions,
        "scaffold_fallback_rate",
    )
    _require_rate(
        serving["support_fallback_rate"],
        support,
        decisions,
        "support_fallback_rate",
    )
    opportunities = serving["keep_shanten_guard_opportunities"]
    changes = serving["guard_induced_action_changes"]
    unguarded_worsen = serving["unguarded_would_worsen_count"]
    if opportunities > activations or changes > opportunities:
        raise _error("candidate guard opportunity/action-change counts are invalid")
    if unguarded_worsen > activations:
        raise _error("candidate unguarded-worsen count exceeds learned activations")
    if any(
        serving[name] != 0
        for name in (
            "guarded_selected_worsen_count",
            "illegal_selection_count",
            "non_finite_q_output_count",
            "resolve_failure_count",
        )
    ):
        raise _error("candidate serving failure diagnostics must all be zero")
    if document["baseline_policy_instance_count"] != 3 * GAME_COUNT:
        raise _error("baseline Policy instance count is invalid")
    if document["result_identity"] != result_identity(document):
        raise _error("higher-fidelity result identity does not match its contents")
    classification = document["classification"]
    if classification is not None:
        try:
            outcome = HigherFidelityOutcome(classification)
        except ValueError as exc:
            raise _error("higher-fidelity result classification is unknown") from exc
        if outcome not in _RESULT_OUTCOMES or outcome is not derive_classification(
            document
        ):
            raise _error("higher-fidelity result classification contradicts the rule")
    return document


def classify_interval(lower: object, upper: object) -> HigherFidelityOutcome:
    if type(lower) not in (int, float) or type(upper) not in (int, float):
        raise _error("classification requires a defined numeric interval")
    lower = float(lower)
    upper = float(upper)
    if not isfinite(lower) or not isfinite(upper) or lower > upper:
        raise _error("classification interval is invalid")
    if lower > 0:
        return HigherFidelityOutcome.SIGNAL
    if upper < 0:
        return HigherFidelityOutcome.NEGATIVE
    return HigherFidelityOutcome.INCONCLUSIVE


def classify_pre_result_state(
    *, evidence_available: bool, protocol_valid: bool
) -> HigherFidelityOutcome | None:
    """Keep blocked/invalid pre-result states distinct from strength evidence."""
    if type(evidence_available) is not bool or type(protocol_valid) is not bool:
        raise TypeError("pre-result state flags must be exact bools")
    if not protocol_valid:
        return HigherFidelityOutcome.STOP_INVALID
    if not evidence_available:
        return HigherFidelityOutcome.EVIDENCE_BLOCKED
    return None


def derive_classification(document: dict) -> HigherFidelityOutcome:
    stats = document["canonical_summary"]["seed_block_statistics"]
    return classify_interval(
        stats["normal_approx_95_interval_lower"],
        stats["normal_approx_95_interval_upper"],
    )


def save_result(path: str | Path, document: dict) -> dict:
    path = Path(path)
    validated = validate_result(document)
    write_new_artifact_file(path, canonical_json_text(validated))
    return load_result(path)


def load_result(path: str | Path) -> dict:
    path = Path(path)
    if not path.is_file():
        raise _error("higher-fidelity result path does not exist")
    text = path.read_text(encoding="utf-8")
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise _error("higher-fidelity result is not valid JSON") from exc
    if canonical_json_text(document) != text:
        raise _error("higher-fidelity result is not canonical JSON")
    return validate_result(document)


def bind_recorded_artifact(document: dict) -> SingleRoundStrengthArtifact:
    validated = validate_result(document)
    path = Path(validated["execution_lock"]["artifact_locations"]["strength_artifact"])
    if not path.is_file():
        raise _error("the strength artifact bound by the result does not exist")
    recorded = validated["strength_artifact"]
    if _sha256_file(path) != recorded["sha256"]:
        raise _error("the retained strength artifact digest differs from the result")
    artifact = load_single_round_artifact(path)
    require_higher_fidelity_artifact(artifact, validated["execution_lock"])
    summary = summarize_single_round_strength(
        aggregate_candidate_metrics(
            artifact.plan.candidate_identity, artifact.game_results
        ),
        artifact.game_results,
    )
    if (
        summary != artifact.summary
        or summary_to_dict(summary) != validated["canonical_summary"]
    ):
        raise _error("the retained raw games do not regenerate the recorded summary")
    return artifact


def bind_recorded_candidate(
    document: dict, checkpoint: LoadedP1ServingCheckpoint
) -> LoadedP1ServingCheckpoint:
    validated = validate_result(document)
    reloaded = load_p1_serving_checkpoint(checkpoint.path)
    _require_checkpoint_matches_lock(reloaded, validated["execution_lock"])
    return reloaded


def record_classification(
    document: dict,
    outcome: HigherFidelityOutcome,
    *,
    checkpoint: LoadedP1ServingCheckpoint,
) -> dict:
    validated = validate_result(document)
    if not isinstance(outcome, HigherFidelityOutcome):
        raise TypeError("outcome must be HigherFidelityOutcome")
    if outcome not in _RESULT_OUTCOMES:
        raise _error("blocked/invalid are pre-result states, not strength outcomes")
    if validated["classification"] is not None:
        raise _error("the result already records a classification")
    derived = derive_classification(validated)
    if outcome is not derived:
        raise _error("the requested classification contradicts the locked interval")
    bind_recorded_candidate(validated, checkpoint)
    bind_recorded_artifact(validated)
    return validate_result({**validated, "classification": outcome.value})


@dataclass(frozen=True, slots=True)
class HigherFidelityMeasurement:
    artifact: SingleRoundStrengthArtifact
    document: dict
    derived_outcome: HigherFidelityOutcome
    wall_clock_seconds: float
    cpu_seconds: float


def run_higher_fidelity_evaluation(
    checkpoint: LoadedP1ServingCheckpoint,
    lock_document: dict,
    *,
    pre_execution_comment_url: str,
    progress_callback=None,
) -> HigherFidelityMeasurement:
    """Execute exactly once after the reviewed lock comment has been posted."""
    lock = validate_pre_execution_lock(lock_document)
    require_pre_execution_comment_url(pre_execution_comment_url)
    if _require_clean_arena_head() != lock["execution_target"]["merged_main_revision"]:
        raise _error("live Arena HEAD differs from the locked merged main revision")
    live_provenance = collect_execution_provenance()
    if execution_provenance_to_dict(live_provenance) != lock["provenance"]:
        raise _error("live execution provenance differs from the posted lock")
    if runtime_block() != lock["runtime"]:
        raise _error("live runtime differs from the posted lock")
    locations = lock["artifact_locations"]
    for name in ("strength_artifact", "result", "classified_result"):
        if Path(locations[name]).exists():
            raise _error(f"locked output {name} already exists; outputs are write-once")
    checkpoint = load_p1_serving_checkpoint(checkpoint.path)
    _require_checkpoint_matches_lock(checkpoint, lock)
    plan, candidate_registry, baseline_registry = build_evaluation_plan(
        checkpoint, lock
    )
    wall_started = time.perf_counter()
    cpu_started = time.process_time()
    result = run_single_round_evaluation(plan, progress_callback=progress_callback)
    wall_clock_seconds = time.perf_counter() - wall_started
    cpu_seconds = time.process_time() - cpu_started
    artifact_path = Path(locations["strength_artifact"])
    save_single_round_artifact(result, artifact_path)
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
        raise _error("strict artifact readback did not regenerate the same summary")
    guard = collect_guard_diagnostics(candidate_registry.instances)
    activation = collect_activation_diagnostics(candidate_registry.instances)
    document = save_result(
        locations["result"],
        build_result(
            lock_document=lock,
            pre_execution_comment_url=pre_execution_comment_url,
            artifact=artifact,
            artifact_path=artifact_path,
            summary=summary,
            guard_diagnostics=guard,
            activation_diagnostics=activation,
            baseline_policy_instance_count=len(baseline_registry.instances),
        ),
    )
    return HigherFidelityMeasurement(
        artifact=artifact,
        document=document,
        derived_outcome=derive_classification(document),
        wall_clock_seconds=wall_clock_seconds,
        cpu_seconds=cpu_seconds,
    )


__all__ = [
    "ARTIFACT_RETENTION_KEY",
    "BASELINE_IDENTITY",
    "BASELINE_IMPLEMENTATION",
    "BASELINE_SELECTED_LISJONG_REVISION",
    "CLASSIFICATION_RULE",
    "DEFAULT_ORDERED_SEEDS",
    "EXPECTED_BASE_CANDIDATE_IDENTITY",
    "EXPECTED_GUARDED_CANDIDATE_IDENTITY",
    "EXECUTION_TARGET_REF",
    "FORMAL_TEST",
    "GAME_COUNT",
    "GAME_MODE",
    "HigherFidelityArtifactLocations",
    "HigherFidelityError",
    "HigherFidelityMeasurement",
    "HigherFidelityOutcome",
    "LOCK_SCHEMA_VERSION",
    "MAX_WORKERS",
    "NO_RESCUE_BOUNDARY",
    "RESULT_SCHEMA_VERSION",
    "ROTATIONS_PER_SEED",
    "SEED_BLOCK_COUNT",
    "artifact_block",
    "baseline_block",
    "bind_recorded_artifact",
    "bind_recorded_candidate",
    "build_evaluation_plan",
    "build_pre_execution_lock",
    "build_result",
    "candidate_block",
    "classify_interval",
    "classify_pre_result_state",
    "declared_allocated_seeds",
    "derive_classification",
    "execution_target_block",
    "load_result",
    "lock_identity",
    "plan_block",
    "record_classification",
    "render_pre_execution_lock",
    "require_higher_fidelity_artifact",
    "require_pre_execution_comment_url",
    "require_seed_plan",
    "run_higher_fidelity_evaluation",
    "save_result",
    "seed_freshness_block",
    "validate_pre_execution_lock",
    "validate_result",
]
