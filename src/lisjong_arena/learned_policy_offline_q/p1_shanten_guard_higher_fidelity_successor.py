"""Fresh successor to the invalid Issue #175 higher-fidelity screen (Issue #179).

Issue #175 consumed seeds 547..571 but failed before durable evidence was
persisted.  This module keeps that historical protocol untouched and defines a
new execution identity, fresh population, lock comment namespace, and retention
keys while reusing the exact #162/#173 candidate and Stage 6 yakuhai-call
comparator semantics.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from enum import Enum
from math import isfinite
from pathlib import Path

from lisjong_arena._artifact_io import canonical_json_text, write_new_artifact_file
from lisjong_arena.model import (
    SINGLE_ROUND_GAME_MODE,
    SINGLE_ROUND_ROTATION_COUNT,
    PolicySpec,
    SingleRoundEvaluationPlan,
)
from lisjong_arena.policy_catalog import create_yakuhai_call
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

from .errors import OfflineQError
from .p1_candidate import LoadedP1ServingCheckpoint, load_p1_serving_checkpoint
from .p1_serving import create_p1_hybrid_runtime
from .p1_shanten_guard import (
    GuardDiagnostics,
    collect_guard_diagnostics,
    guarded_policy_factory,
)
from .strength import (
    ActivationDiagnostics,
    PolicyInstanceRegistry,
    collect_activation_diagnostics,
)
from . import p1_shanten_guard_higher_fidelity as historical

LOCK_SCHEMA_VERSION = (
    "arena-learned-policy-offlineq-p1-guarded-higher-fidelity-successor-lock-v1"
)
RESULT_SCHEMA_VERSION = (
    "arena-learned-policy-offlineq-p1-guarded-higher-fidelity-successor-result-v1"
)
EXPERIMENT_ID = "arena-learned-policy-offlineq-p1-guarded-higher-fidelity-179"
SOURCE_ISSUE = "lisbun/lisjong-arena#179"
PARENT_ISSUE = "lisbun/lisjong-project#45"
PREDECESSOR_ISSUES = (
    "lisbun/lisjong-arena#173",
    "lisbun/lisjong-arena#175",
    "lisbun/lisjong-arena#177",
)

DEFAULT_ORDERED_SEEDS = tuple(range(572, 597))
CONSUMED_INVALID_PREDECESSOR_SEEDS = historical.DEFAULT_ORDERED_SEEDS
SEED_BLOCK_COUNT = historical.SEED_BLOCK_COUNT
ROTATIONS_PER_SEED = SINGLE_ROUND_ROTATION_COUNT
GAME_COUNT = SEED_BLOCK_COUNT * ROTATIONS_PER_SEED
GAME_MODE = SINGLE_ROUND_GAME_MODE
MAX_WORKERS = 1
FORMAL_TEST = False
ROLE = "DEVELOPMENT HIGHER-FIDELITY SUCCESSOR SCREEN"
EXECUTION_TARGET_REF = historical.EXECUTION_TARGET_REF

BASELINE_IDENTITY = historical.BASELINE_IDENTITY
BASELINE_FACTORY = historical.BASELINE_FACTORY
BASELINE_IMPLEMENTATION = historical.BASELINE_IMPLEMENTATION
BASELINE_IMPLEMENTATION_SOURCE_REVISION = (
    historical.BASELINE_IMPLEMENTATION_SOURCE_REVISION
)
BASELINE_SELECTED_LISJONG_REVISION = historical.BASELINE_SELECTED_LISJONG_REVISION
LOCKED_ENGINE_REVISION = historical.LOCKED_ENGINE_REVISION
EXPECTED_BASE_CANDIDATE_IDENTITY = historical.EXPECTED_BASE_CANDIDATE_IDENTITY
EXPECTED_GUARDED_CANDIDATE_IDENTITY = historical.EXPECTED_GUARDED_CANDIDATE_IDENTITY

RETENTION_BACKEND = historical.RETENTION_BACKEND
CANDIDATE_RETENTION_KEY = historical.CANDIDATE_RETENTION_KEY
ARTIFACT_RETENTION_KEY = "offlineq-179-guarded-higher-fidelity/strength-artifact"
RESULT_RETENTION_KEY = "offlineq-179-guarded-higher-fidelity/result"
CLASSIFIED_RESULT_RETENTION_KEY = (
    "offlineq-179-guarded-higher-fidelity/classified-result"
)

PRIMARY_CHANGED_AXIS = "fresh evaluation population and successor execution identity only"
LOCKED_UNCHANGED_AXES = historical.LOCKED_UNCHANGED_AXES
CLASSIFICATION_RULE = dict(historical.CLASSIFICATION_RULE)
NO_RESCUE_BOUNDARY = (
    "Issue #175 transient scores and seeds 547..571 are never reused. After Issue "
    "#179 game execution begins there is no seed extension, replacement seed, rescue "
    "rerun, comparator substitution, guard change, or candidate change."
)

_LOCK_COMMENT_URL = re.compile(
    r"https://github\.com/lisbun/lisjong-arena/issues/179#issuecomment-\d+\Z"
)
_OUTPUT_LOCATION_NAMES = ("strength_artifact", "result", "classified_result")

candidate_block = historical.candidate_block
baseline_block = historical.baseline_block
require_baseline_provenance = historical.require_baseline_provenance
runtime_block = historical.runtime_block
execution_target_block = historical.execution_target_block
_require_clean_arena_head = historical._require_clean_arena_head


class SuccessorHigherFidelityError(OfflineQError):
    """Issue #179 lock, execution, artifact, or result contract violation."""


class SuccessorOutcome(Enum):
    SIGNAL = "GUARDED CANDIDATE HIGHER-FIDELITY SIGNAL"
    NEGATIVE = "GUARDED CANDIDATE HIGHER-FIDELITY NEGATIVE"
    INCONCLUSIVE = "GUARDED CANDIDATE HIGHER-FIDELITY INCONCLUSIVE"
    EVIDENCE_BLOCKED = "GUARDED CANDIDATE EVIDENCE BLOCKED"
    STOP_INVALID = "STOP / INVALID"


_RESULT_OUTCOMES = frozenset(
    {SuccessorOutcome.SIGNAL, SuccessorOutcome.NEGATIVE, SuccessorOutcome.INCONCLUSIVE}
)


def _error(message: str) -> SuccessorHigherFidelityError:
    return SuccessorHigherFidelityError(message)


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def declared_allocated_seeds() -> frozenset[int]:
    """Return all repository-declared populations consumed before Issue #179."""
    return frozenset(historical.declared_allocated_seeds()) | frozenset(
        CONSUMED_INVALID_PREDECESSOR_SEEDS
    )


def require_seed_plan(ordered_seeds) -> tuple[int, ...]:
    """Accept exactly one contiguous 25-seed development population."""
    try:
        seeds = historical.require_seed_plan(ordered_seeds)
    except historical.HigherFidelityError as exc:
        raise _error(str(exc)) from exc
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
            "open and closed relevant Issues plus known local/private allocations "
            "must be checked before the lock is built"
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


@dataclass(frozen=True, slots=True)
class SuccessorArtifactLocations:
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
            raise _error("artifact locations must be non-empty strings")
        outputs = (self.strength_artifact, self.result, self.classified_result)
        if len(set(outputs)) != len(outputs):
            raise _error("each output must have a distinct location")

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


def lock_identity(document: dict) -> str:
    payload = {
        name: value for name, value in document.items() if name != "lock_identity"
    }
    return hashlib.sha256(canonical_json_text(payload).encode()).hexdigest()


def _require_output_destinations_ready(locations: object) -> None:
    """Reject deterministic write-once destination defects without creating files."""
    if type(locations) is not dict:
        raise _error("locked artifact locations are invalid")
    for name in _OUTPUT_LOCATION_NAMES:
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


def build_pre_execution_lock(
    checkpoint: LoadedP1ServingCheckpoint,
    *,
    locations: SuccessorArtifactLocations,
    ordered_seeds=DEFAULT_ORDERED_SEEDS,
    external_freshness_confirmed: bool,
    additional_allocated_seeds=(),
) -> dict[str, object]:
    """Build the exact Issue #179 lock after destination readiness is established."""
    historical.verify_locked_candidate_contract()
    if not isinstance(locations, SuccessorArtifactLocations):
        raise TypeError("locations must be SuccessorArtifactLocations")
    if str(checkpoint.path) != locations.candidate_checkpoint:
        raise _error("the lock candidate location differs from the loaded checkpoint")
    location_document = locations.to_document()
    _require_output_destinations_ready(location_document)
    reloaded = load_p1_serving_checkpoint(checkpoint.path)
    if candidate_block(reloaded) != candidate_block(checkpoint):
        raise _error("strict checkpoint readback differs from the supplied candidate")
    checkpoint = reloaded
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
        "parent_issue": PARENT_ISSUE,
        "predecessor_issues": list(PREDECESSOR_ISSUES),
        "primary_changed_axis": PRIMARY_CHANGED_AXIS,
        "locked_unchanged_axes": list(LOCKED_UNCHANGED_AXES),
        "candidate": candidate_block(checkpoint),
        "baseline": baseline_block(),
        "plan": plan_block(seeds),
        "classification_rule": dict(CLASSIFICATION_RULE),
        "outcomes": [outcome.value for outcome in SuccessorOutcome],
        "artifact_locations": location_document,
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
        "parent_issue",
        "predecessor_issues",
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
        "parent_issue": PARENT_ISSUE,
        "predecessor_issues": list(PREDECESSOR_ISSUES),
        "primary_changed_axis": PRIMARY_CHANGED_AXIS,
        "locked_unchanged_axes": list(LOCKED_UNCHANGED_AXES),
        "baseline": baseline_block(),
        "classification_rule": dict(CLASSIFICATION_RULE),
        "outcomes": [outcome.value for outcome in SuccessorOutcome],
        "no_rescue_boundary": NO_RESCUE_BOUNDARY,
        "result_exposed": False,
    }
    for name, value in expected.items():
        if document[name] != value:
            raise _error(f"pre-execution lock {name} is not the locked value")
    historical._validate_candidate_block(document["candidate"])
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
        raise _error("pre-execution lock seed plan collides with consumed evidence")
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
    SuccessorArtifactLocations(
        candidate_checkpoint=locations.get("candidate_checkpoint"),
        strength_artifact=locations.get("strength_artifact"),
        result=locations.get("result"),
        classified_result=locations.get("classified_result"),
        retention_backend=locations.get("retention_backend"),
    )
    if locations["retention_keys"] != {
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
    lock = validate_pre_execution_lock(document)
    candidate = lock["candidate"]
    baseline = lock["baseline"]
    plan = lock["plan"]
    provenance = lock["provenance"]
    locations = lock["artifact_locations"]
    lines = [
        "## Issue #179 pre-execution lock",
        "",
        "```text",
        f"experiment             {lock['experiment_id']}",
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
        raise _error(
            "real execution requires the Issue #179 pre-execution lock comment URL"
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


def require_successor_artifact(
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
    expected_pairs = {
        (seed, rotation)
        for seed in expected_plan["ordered_seeds"]
        for rotation in range(ROTATIONS_PER_SEED)
    }
    observed_pairs = {(result.seed, result.rotation) for result in artifact.game_results}
    if observed_pairs != expected_pairs:
        raise _error("artifact does not contain the exact seed/rotation population")
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
    require_successor_artifact(artifact, lock)
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
        "strength_artifact": _artifact_block(artifact, path),
        "canonical_summary": summary_to_dict(summary),
        "secondary_diagnostics": historical.secondary_diagnostics_block(
            artifact, summary
        ),
        "candidate_serving_diagnostics": historical.serving_diagnostics_block(
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
        raise _error(f"{name} must be a non-negative exact int")
    return value


def _require_rate(value: object, numerator: int, denominator: int, name: str) -> None:
    if type(value) not in (int, float) or not isfinite(float(value)):
        raise _error(f"{name} must be finite")
    expected = numerator / denominator
    if float(value) != expected:
        raise _error(f"{name} does not match its count ratio")


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
        raise _error("successor result fields are invalid")
    for name, value in (
        ("schema_version", RESULT_SCHEMA_VERSION),
        ("experiment_id", EXPERIMENT_ID),
        ("source_issue", SOURCE_ISSUE),
        ("classification_rule", dict(CLASSIFICATION_RULE)),
        ("no_rescue_boundary", NO_RESCUE_BOUNDARY),
    ):
        if document[name] != value:
            raise _error(f"successor result {name} is invalid")
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
        raise _error("successor result artifact fields are invalid")
    if artifact["schema_version"] != SINGLE_ROUND_ARTIFACT_SCHEMA_VERSION:
        raise _error("successor result artifact schema is invalid")
    if artifact["evaluation_protocol"] != SINGLE_ROUND_EVALUATION_PROTOCOL:
        raise _error("successor result artifact protocol is invalid")
    if artifact["game_count"] != GAME_COUNT:
        raise _error("successor result artifact game count is invalid")
    if artifact["retention"] != {
        "backend": RETENTION_BACKEND,
        "key": ARTIFACT_RETENTION_KEY,
    }:
        raise _error("successor result artifact retention is invalid")
    if type(artifact["sha256"]) is not str or len(artifact["sha256"]) != 64:
        raise _error("successor result artifact digest is invalid")
    if Path(lock["artifact_locations"]["strength_artifact"]).name != artifact["filename"]:
        raise _error("successor result artifact filename differs from the lock")
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
    classify_interval(
        stats.get("normal_approx_95_interval_lower"),
        stats.get("normal_approx_95_interval_upper"),
    )
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
    try:
        historical._validate_mahjong_metrics(
            secondary["guarded_candidate"], GAME_COUNT, "guarded_candidate"
        )
        historical._validate_mahjong_metrics(
            secondary["yakuhai_call_baseline"],
            3 * GAME_COUNT,
            "yakuhai_call_baseline",
        )
    except historical.HigherFidelityError as exc:
        raise _error(str(exc)) from exc
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
    rate_fields = ("activation_rate", "scaffold_fallback_rate", "support_fallback_rate")
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
        serving["scaffold_fallback_rate"], scaffold, decisions, "scaffold_fallback_rate"
    )
    _require_rate(serving["support_fallback_rate"], support, decisions, "support_fallback_rate")
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
        raise _error("successor result identity does not match its contents")
    classification = document["classification"]
    if classification is not None:
        try:
            outcome = SuccessorOutcome(classification)
        except ValueError as exc:
            raise _error("successor result classification is unknown") from exc
        if outcome not in _RESULT_OUTCOMES or outcome is not derive_classification(document):
            raise _error("successor result classification contradicts the rule")
    return document


def classify_interval(lower: object, upper: object) -> SuccessorOutcome:
    if type(lower) not in (int, float) or type(upper) not in (int, float):
        raise _error("classification requires a defined numeric interval")
    lower = float(lower)
    upper = float(upper)
    if not isfinite(lower) or not isfinite(upper) or lower > upper:
        raise _error("classification interval is invalid")
    if lower > 0:
        return SuccessorOutcome.SIGNAL
    if upper < 0:
        return SuccessorOutcome.NEGATIVE
    return SuccessorOutcome.INCONCLUSIVE


def classify_pre_result_state(
    *, evidence_available: bool, protocol_valid: bool
) -> SuccessorOutcome | None:
    if type(evidence_available) is not bool or type(protocol_valid) is not bool:
        raise TypeError("pre-result state flags must be exact bools")
    if not protocol_valid:
        return SuccessorOutcome.STOP_INVALID
    if not evidence_available:
        return SuccessorOutcome.EVIDENCE_BLOCKED
    return None


def derive_classification(document: dict) -> SuccessorOutcome:
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
        raise _error("successor result path does not exist")
    text = path.read_text(encoding="utf-8")
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise _error("successor result is not valid JSON") from exc
    if canonical_json_text(document) != text:
        raise _error("successor result is not canonical JSON")
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
    require_successor_artifact(artifact, validated["execution_lock"])
    summary = summarize_single_round_strength(
        aggregate_candidate_metrics(
            artifact.plan.candidate_identity, artifact.game_results
        ),
        artifact.game_results,
    )
    if summary != artifact.summary or summary_to_dict(summary) != validated["canonical_summary"]:
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
    outcome: SuccessorOutcome,
    *,
    checkpoint: LoadedP1ServingCheckpoint,
) -> dict:
    validated = validate_result(document)
    if not isinstance(outcome, SuccessorOutcome):
        raise TypeError("outcome must be SuccessorOutcome")
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
class SuccessorMeasurement:
    artifact: SingleRoundStrengthArtifact
    document: dict
    derived_outcome: SuccessorOutcome
    wall_clock_seconds: float
    cpu_seconds: float


def run_successor_evaluation(
    checkpoint: LoadedP1ServingCheckpoint,
    lock_document: dict,
    *,
    pre_execution_comment_url: str,
    progress_callback=None,
) -> SuccessorMeasurement:
    """Run the new Issue #179 one-shot population after its reviewed lock comment."""
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
    _require_output_destinations_ready(locations)
    checkpoint = load_p1_serving_checkpoint(checkpoint.path)
    _require_checkpoint_matches_lock(checkpoint, lock)
    plan, candidate_registry, baseline_registry = build_evaluation_plan(checkpoint, lock)
    wall_started = time.perf_counter()
    cpu_started = time.process_time()
    result = run_single_round_evaluation(plan, progress_callback=progress_callback)
    wall_clock_seconds = time.perf_counter() - wall_started
    cpu_seconds = time.process_time() - cpu_started
    artifact_path = Path(locations["strength_artifact"])
    save_single_round_artifact(result, artifact_path)
    artifact = require_successor_artifact(load_single_round_artifact(artifact_path), lock)
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
    return SuccessorMeasurement(
        artifact=artifact,
        document=document,
        derived_outcome=derive_classification(document),
        wall_clock_seconds=wall_clock_seconds,
        cpu_seconds=cpu_seconds,
    )
