"""Portable, non-interactive orchestration for one ABBB strength run.

The runner composes existing Arena contracts. It does not implement Mahjong
execution, seat rotation, strength aggregation, seed allocation, retry/resume,
candidate generation, or promotion logic.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lisjong_arena._artifact_io import (
    ArtifactValidationError,
    canonical_json_text,
    expect_int,
    expect_list,
    expect_object,
    expect_str,
    read_json_document,
    write_new_artifact_file,
)
from lisjong_arena._execution_safety import (
    ExecutionSafetyError,
    require_clean_arena_head,
    require_new_artifact_destinations,
)
from lisjong_arena.model import (
    SINGLE_ROUND_ROTATION_COUNT,
    PolicySpec,
    SingleRoundEvaluationPlan,
)
from lisjong_arena.policy_reference import (
    PolicyReferenceError,
    resolve_policy_reference,
)
from lisjong_arena.single_round_artifact import (
    SINGLE_ROUND_ARTIFACT_SCHEMA_VERSION,
    SINGLE_ROUND_EVALUATION_PROTOCOL,
    SingleRoundArtifactError,
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
    SingleRoundStrengthSummary,
    run_single_round_evaluation,
    run_single_round_evaluation_parallel,
)

SPEC_VERSION = 1
RESULT_VERSION = 1
LOCK_VERSION = 1
CLASSIFICATION_RULE_TYPE = "normal-approx-95-interval-threshold-v1"
UNCLASSIFIED_LABEL = "MEASURED / UNCLASSIFIED"
EXECUTION_STATUS_COMPLETED = "COMPLETED"
EXECUTION_TARGET_TYPE = "clean-arena-head-v1"

_SPEC_REQUIRED_FIELDS = {
    "artifact_output",
    "baseline",
    "candidate",
    "classification_rule",
    "execution_options",
    "expected_provenance_constraints",
    "ordered_seeds",
    "protocol",
    "result_output",
    "spec_version",
}
_SEMANTIC_SPEC_FIELDS = _SPEC_REQUIRED_FIELDS - {"artifact_output", "result_output"}
_PROVENANCE_FIELDS = frozenset(
    {
        "execution_environment",
        "lisjong_arena_revision",
        "lisjong_arena_version",
        "lisjong_engine_revision",
        "lisjong_engine_version",
        "lisjong_revision",
        "lisjong_version",
        "python_version",
        "riichienv_version",
    }
)
_RESULT_FIELDS = {
    "canonical_summary",
    "classification",
    "classification_rule",
    "execution_options",
    "execution_status",
    "execution_target",
    "game_count",
    "lock_identity",
    "ordered_seeds",
    "protocol",
    "provenance",
    "resolved_baseline",
    "resolved_candidate",
    "result_identity",
    "result_version",
    "spec",
    "spec_identity",
    "strength_artifact",
    "lock",
}


class StrengthEvaluationError(ValueError):
    """Base error for portable strength-evaluation orchestration."""


class StrengthEvaluationSpecError(StrengthEvaluationError):
    """The machine-readable input cannot describe a supported v0 run."""


class StrengthEvaluationPreflightError(StrengthEvaluationError):
    """A run was rejected before game 1."""


class StrengthEvaluationExecutionError(StrengthEvaluationError):
    """Execution or durable evidence acceptance failed."""


class StrengthEvaluationResultError(StrengthEvaluationError):
    """A result artifact is malformed or inconsistent."""


@dataclass(frozen=True, slots=True)
class PolicyReference:
    reference: str
    identity: str | None = None

    def __post_init__(self) -> None:
        if type(self.reference) is not str or not self.reference:
            raise StrengthEvaluationSpecError(
                "policy reference must be a non-empty string"
            )
        if self.identity is not None and (
            type(self.identity) is not str or not self.identity.strip()
        ):
            raise StrengthEvaluationSpecError(
                "explicit policy identity must be null or a non-empty string"
            )

    def to_document(self) -> dict[str, object]:
        return {"identity": self.identity, "reference": self.reference}


@dataclass(frozen=True, slots=True)
class IntervalClassificationRule:
    threshold: float
    positive_label: str
    negative_label: str
    inconclusive_label: str
    rule_type: str = CLASSIFICATION_RULE_TYPE

    def __post_init__(self) -> None:
        if self.rule_type != CLASSIFICATION_RULE_TYPE:
            raise StrengthEvaluationSpecError(
                f"unsupported classification rule: {self.rule_type!r}"
            )
        if type(self.threshold) is not float or not math.isfinite(self.threshold):
            raise StrengthEvaluationSpecError(
                "classification threshold must be a finite number"
            )
        for name in ("positive_label", "negative_label", "inconclusive_label"):
            value = getattr(self, name)
            if type(value) is not str or not value.strip():
                raise StrengthEvaluationSpecError(
                    f"classification {name} must be a non-empty string"
                )
        if (
            len({self.positive_label, self.negative_label, self.inconclusive_label})
            != 3
        ):
            raise StrengthEvaluationSpecError("classification labels must be distinct")

    def to_document(self) -> dict[str, object]:
        return {
            "inconclusive_label": self.inconclusive_label,
            "negative_label": self.negative_label,
            "positive_label": self.positive_label,
            "rule_type": self.rule_type,
            "threshold": self.threshold,
        }

    @property
    def identity(self) -> str:
        return _document_identity(self.to_document())


@dataclass(frozen=True, slots=True)
class StrengthEvaluationSpec:
    candidate: PolicyReference
    baseline: PolicyReference
    ordered_seeds: tuple[int, ...]
    artifact_output: Path
    result_output: Path
    workers: int
    max_steps: int
    classification_rule: IntervalClassificationRule | None = None
    expected_provenance_constraints: tuple[tuple[str, str], ...] = ()
    spec_version: int = SPEC_VERSION
    protocol: str = SINGLE_ROUND_EVALUATION_PROTOCOL

    def __post_init__(self) -> None:
        if self.spec_version != SPEC_VERSION:
            raise StrengthEvaluationSpecError(
                f"unsupported spec version: {self.spec_version!r}"
            )
        if self.protocol != SINGLE_ROUND_EVALUATION_PROTOCOL:
            raise StrengthEvaluationSpecError(
                f"unsupported protocol: {self.protocol!r}"
            )
        if not isinstance(self.candidate, PolicyReference):
            raise TypeError("candidate must be a PolicyReference")
        if not isinstance(self.baseline, PolicyReference):
            raise TypeError("baseline must be a PolicyReference")
        if type(self.workers) is not int or self.workers <= 0:
            raise StrengthEvaluationSpecError("workers must be a positive integer")
        if type(self.max_steps) is not int or self.max_steps <= 0:
            raise StrengthEvaluationSpecError("max_steps must be a positive integer")
        if self.classification_rule is not None and not isinstance(
            self.classification_rule, IntervalClassificationRule
        ):
            raise TypeError(
                "classification_rule must be an IntervalClassificationRule or None"
            )
        try:
            SingleRoundEvaluationPlan(
                candidate=PolicySpec("candidate-preflight", lambda: None),
                baseline=PolicySpec("baseline-preflight", lambda: None),
                seeds=self.ordered_seeds,
                max_steps=self.max_steps,
            )
        except (TypeError, ValueError) as exc:
            raise StrengthEvaluationSpecError(str(exc)) from exc
        if self.classification_rule is not None and len(self.ordered_seeds) < 2:
            raise StrengthEvaluationSpecError(
                "interval classification requires at least two seed blocks"
            )
        constraints = tuple(self.expected_provenance_constraints)
        if tuple(sorted(constraints)) != constraints:
            raise StrengthEvaluationSpecError(
                "provenance constraints must be sorted by field name"
            )
        if len({name for name, _ in constraints}) != len(constraints):
            raise StrengthEvaluationSpecError(
                "provenance constraints must not repeat fields"
            )
        for name, value in constraints:
            if name not in _PROVENANCE_FIELDS:
                raise StrengthEvaluationSpecError(
                    f"unsupported provenance constraint: {name!r}"
                )
            if type(value) is not str or not value:
                raise StrengthEvaluationSpecError(
                    f"provenance constraint {name!r} must be a non-empty string"
                )
        object.__setattr__(self, "ordered_seeds", tuple(self.ordered_seeds))
        object.__setattr__(self, "artifact_output", Path(self.artifact_output))
        object.__setattr__(self, "result_output", Path(self.result_output))
        object.__setattr__(self, "expected_provenance_constraints", constraints)

    def semantic_document(self) -> dict[str, object]:
        """Return identity-bearing semantics, intentionally excluding paths."""
        return {
            "baseline": self.baseline.to_document(),
            "candidate": self.candidate.to_document(),
            "classification_rule": (
                None
                if self.classification_rule is None
                else self.classification_rule.to_document()
            ),
            "execution_options": {
                "max_steps": self.max_steps,
                "workers": self.workers,
            },
            "expected_provenance_constraints": dict(
                self.expected_provenance_constraints
            ),
            "ordered_seeds": list(self.ordered_seeds),
            "protocol": self.protocol,
            "spec_version": self.spec_version,
        }

    @property
    def identity(self) -> str:
        return _document_identity(self.semantic_document())

    @property
    def destinations(self) -> dict[str, Path]:
        return {
            "strength_artifact": self.artifact_output,
            "result": self.result_output,
        }


@dataclass(frozen=True, slots=True)
class LockedStrengthEvaluationPlan:
    spec: StrengthEvaluationSpec
    candidate: PolicySpec
    baseline: PolicySpec
    evaluation_plan: SingleRoundEvaluationPlan
    provenance: SingleRoundExecutionProvenance
    execution_target_revision: str
    document: dict[str, object]

    @property
    def identity(self) -> str:
        return expect_str(self.document["lock_identity"], "lock_identity")


def _document_identity(document: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json_text(document).encode("utf-8")).hexdigest()


def _same_canonical_json_value(left: object, right: object) -> bool:
    """Compare JSON values without Python's bool/int/float equality coercion."""
    return canonical_json_text({"value": left}) == canonical_json_text({"value": right})


def _policy_reference(value: object, context: str) -> PolicyReference:
    raw = expect_object(value, {"identity", "reference"}, context)
    identity = raw["identity"]
    if identity is not None and type(identity) is not str:
        raise ArtifactValidationError(f"{context}.identity must be a string or null")
    return PolicyReference(
        reference=expect_str(raw["reference"], f"{context}.reference"),
        identity=identity,
    )


def _number(value: object, context: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ArtifactValidationError(f"{context} must be a JSON number")
    normalized = float(value)
    if not math.isfinite(normalized):
        raise ArtifactValidationError(f"{context} must be finite")
    return normalized


def _classification_rule(value: object) -> IntervalClassificationRule | None:
    if value is None:
        return None
    raw = expect_object(
        value,
        {
            "inconclusive_label",
            "negative_label",
            "positive_label",
            "rule_type",
            "threshold",
        },
        "classification_rule",
    )
    return IntervalClassificationRule(
        rule_type=expect_str(raw["rule_type"], "classification_rule.rule_type"),
        threshold=_number(raw["threshold"], "classification_rule.threshold"),
        positive_label=expect_str(
            raw["positive_label"], "classification_rule.positive_label"
        ),
        negative_label=expect_str(
            raw["negative_label"], "classification_rule.negative_label"
        ),
        inconclusive_label=expect_str(
            raw["inconclusive_label"], "classification_rule.inconclusive_label"
        ),
    )


def parse_strength_evaluation_spec(value: object) -> StrengthEvaluationSpec:
    """Parse one strict v0 spec document."""
    try:
        raw = expect_object(value, _SPEC_REQUIRED_FIELDS, "spec")
        options = expect_object(
            raw["execution_options"], {"max_steps", "workers"}, "execution_options"
        )
        constraints_raw = raw["expected_provenance_constraints"]
        if type(constraints_raw) is not dict:
            raise ArtifactValidationError(
                "expected_provenance_constraints must be an object"
            )
        constraints: list[tuple[str, str]] = []
        for name in sorted(constraints_raw):
            if type(name) is not str:
                raise ArtifactValidationError(
                    "provenance constraint names must be strings"
                )
            constraints.append(
                (
                    name,
                    expect_str(
                        constraints_raw[name],
                        f"expected_provenance_constraints.{name}",
                    ),
                )
            )
        seeds = tuple(
            expect_int(seed, f"ordered_seeds[{index}]")
            for index, seed in enumerate(
                expect_list(raw["ordered_seeds"], "ordered_seeds")
            )
        )
        artifact_output = expect_str(raw["artifact_output"], "artifact_output")
        result_output = expect_str(raw["result_output"], "result_output")
        if not artifact_output or not result_output:
            raise ArtifactValidationError("output paths must be non-empty strings")
        return StrengthEvaluationSpec(
            spec_version=expect_int(raw["spec_version"], "spec_version"),
            candidate=_policy_reference(raw["candidate"], "candidate"),
            baseline=_policy_reference(raw["baseline"], "baseline"),
            protocol=expect_str(raw["protocol"], "protocol"),
            ordered_seeds=seeds,
            artifact_output=Path(artifact_output),
            result_output=Path(result_output),
            classification_rule=_classification_rule(raw["classification_rule"]),
            workers=expect_int(options["workers"], "execution_options.workers"),
            max_steps=expect_int(options["max_steps"], "execution_options.max_steps"),
            expected_provenance_constraints=tuple(constraints),
        )
    except StrengthEvaluationSpecError:
        raise
    except (ArtifactValidationError, TypeError, ValueError) as exc:
        raise StrengthEvaluationSpecError(str(exc)) from exc


def load_strength_evaluation_spec(path: str | Path) -> StrengthEvaluationSpec:
    try:
        return parse_strength_evaluation_spec(read_json_document(Path(path)))
    except StrengthEvaluationSpecError:
        raise
    except (ArtifactValidationError, json.JSONDecodeError, OSError) as exc:
        raise StrengthEvaluationSpecError("spec is unreadable or malformed") from exc


def _resolved_policy_document(
    reference: PolicyReference, resolved: PolicySpec
) -> dict[str, object]:
    return {"identity": resolved.identity, "reference": reference.reference}


def _require_provenance_constraints(
    provenance: SingleRoundExecutionProvenance,
    constraints: tuple[tuple[str, str], ...],
) -> None:
    actual = execution_provenance_to_dict(provenance)
    for name, expected in constraints:
        if actual[name] != expected:
            raise StrengthEvaluationPreflightError(
                f"provenance constraint {name!r} does not match the live value"
            )


def _lock_document(
    spec: StrengthEvaluationSpec,
    candidate: PolicySpec,
    baseline: PolicySpec,
    provenance: SingleRoundExecutionProvenance,
    target_revision: str,
) -> dict[str, object]:
    document: dict[str, object] = {
        "classification_rule": (
            None
            if spec.classification_rule is None
            else spec.classification_rule.to_document()
        ),
        "execution_options": {
            "max_steps": spec.max_steps,
            "workers": spec.workers,
        },
        "execution_target": {
            "revision": target_revision,
            "target_type": EXECUTION_TARGET_TYPE,
        },
        "expected_provenance_constraints": dict(spec.expected_provenance_constraints),
        "lock_identity": None,
        "lock_version": LOCK_VERSION,
        "ordered_seeds": list(spec.ordered_seeds),
        "protocol": spec.protocol,
        "provenance": execution_provenance_to_dict(provenance),
        "resolved_baseline": _resolved_policy_document(spec.baseline, baseline),
        "resolved_candidate": _resolved_policy_document(spec.candidate, candidate),
        "spec_identity": spec.identity,
    }
    payload = {
        name: value for name, value in document.items() if name != "lock_identity"
    }
    document["lock_identity"] = _document_identity(payload)
    return document


def prepare_strength_evaluation(
    spec: StrengthEvaluationSpec,
) -> LockedStrengthEvaluationPlan:
    """Resolve and lock a run after all deterministic preflight checks pass."""
    if not isinstance(spec, StrengthEvaluationSpec):
        raise TypeError("spec must be a StrengthEvaluationSpec")
    try:
        candidate = resolve_policy_reference(
            spec.candidate.reference, explicit_identity=spec.candidate.identity
        )
        baseline = resolve_policy_reference(
            spec.baseline.reference, explicit_identity=spec.baseline.identity
        )
        evaluation_plan = SingleRoundEvaluationPlan(
            candidate=candidate,
            baseline=baseline,
            seeds=spec.ordered_seeds,
            max_steps=spec.max_steps,
        )
        provenance = collect_execution_provenance()
        _require_provenance_constraints(
            provenance, spec.expected_provenance_constraints
        )
        target_revision = require_clean_arena_head()
        if target_revision != provenance.lisjong_arena_revision:
            raise StrengthEvaluationPreflightError(
                "Arena HEAD differs from collected execution provenance"
            )
        require_new_artifact_destinations(
            spec.destinations,
            required_names=("strength_artifact", "result"),
        )
    except StrengthEvaluationPreflightError:
        raise
    except (
        ExecutionSafetyError,
        PolicyReferenceError,
        SingleRoundArtifactError,
        TypeError,
        ValueError,
    ) as exc:
        raise StrengthEvaluationPreflightError(str(exc)) from exc

    document = _lock_document(spec, candidate, baseline, provenance, target_revision)
    return LockedStrengthEvaluationPlan(
        spec=spec,
        candidate=candidate,
        baseline=baseline,
        evaluation_plan=evaluation_plan,
        provenance=provenance,
        execution_target_revision=target_revision,
        document=document,
    )


def classify_strength_summary(
    summary: SingleRoundStrengthSummary,
    rule: IntervalClassificationRule | None,
) -> dict[str, object]:
    """Apply only the predeclared primary interval rule."""
    if not isinstance(summary, SingleRoundStrengthSummary):
        raise TypeError("summary must be a SingleRoundStrengthSummary")
    if rule is None:
        return {
            "kind": "UNCLASSIFIED",
            "label": UNCLASSIFIED_LABEL,
            "rule_identity": None,
        }
    if not isinstance(rule, IntervalClassificationRule):
        raise TypeError("rule must be an IntervalClassificationRule or None")
    statistics = summary.seed_block_statistics
    lower = statistics.normal_approx_95_interval_lower
    upper = statistics.normal_approx_95_interval_upper
    if lower is None or upper is None:
        return {"kind": "INVALID", "label": "INVALID", "rule_identity": rule.identity}
    if not math.isfinite(lower) or not math.isfinite(upper) or lower > upper:
        return {"kind": "INVALID", "label": "INVALID", "rule_identity": rule.identity}
    if lower > rule.threshold:
        kind, label = "POSITIVE", rule.positive_label
    elif upper < rule.threshold:
        kind, label = "NEGATIVE", rule.negative_label
    else:
        kind, label = "INCONCLUSIVE", rule.inconclusive_label
    return {"kind": kind, "label": label, "rule_identity": rule.identity}


def _validate_locked_execution(lock: LockedStrengthEvaluationPlan) -> None:
    require_new_artifact_destinations(
        lock.spec.destinations,
        required_names=("strength_artifact", "result"),
    )
    if require_clean_arena_head() != lock.execution_target_revision:
        raise StrengthEvaluationPreflightError(
            "live Arena HEAD differs from the locked execution target"
        )
    live_provenance = collect_execution_provenance()
    if live_provenance != lock.provenance:
        raise StrengthEvaluationPreflightError(
            "live execution provenance differs from the locked provenance"
        )
    _require_provenance_constraints(
        live_provenance, lock.spec.expected_provenance_constraints
    )
    payload = {
        name: value for name, value in lock.document.items() if name != "lock_identity"
    }
    if lock.identity != _document_identity(payload):
        raise StrengthEvaluationPreflightError("lock identity is inconsistent")


def _artifact_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _require_artifact_matches_lock(
    artifact: SingleRoundStrengthArtifact,
    lock: LockedStrengthEvaluationPlan,
) -> None:
    plan = artifact.plan
    if plan.candidate_identity != lock.candidate.identity:
        raise StrengthEvaluationExecutionError(
            "strength artifact candidate differs from the lock"
        )
    if plan.baseline_identity != lock.baseline.identity:
        raise StrengthEvaluationExecutionError(
            "strength artifact baseline differs from the lock"
        )
    if plan.seeds != lock.spec.ordered_seeds:
        raise StrengthEvaluationExecutionError(
            "strength artifact ordered seeds differ from the lock"
        )
    if plan.max_steps != lock.spec.max_steps:
        raise StrengthEvaluationExecutionError(
            "strength artifact max_steps differs from the lock"
        )
    if artifact.provenance != lock.provenance:
        raise StrengthEvaluationExecutionError(
            "strength artifact provenance differs from the lock"
        )


def _result_document(
    lock: LockedStrengthEvaluationPlan,
    artifact: SingleRoundStrengthArtifact,
    artifact_digest: str,
) -> dict[str, object]:
    classification = classify_strength_summary(
        artifact.summary, lock.spec.classification_rule
    )
    if classification["kind"] == "INVALID":
        raise StrengthEvaluationExecutionError(
            "the predeclared classification rule cannot classify this artifact"
        )
    document: dict[str, object] = {
        "canonical_summary": summary_to_dict(artifact.summary),
        "classification": classification,
        "classification_rule": (
            None
            if lock.spec.classification_rule is None
            else lock.spec.classification_rule.to_document()
        ),
        "execution_options": {
            "max_steps": lock.spec.max_steps,
            "workers": lock.spec.workers,
        },
        "execution_status": EXECUTION_STATUS_COMPLETED,
        "execution_target": lock.document["execution_target"],
        "game_count": len(artifact.game_results),
        "lock_identity": lock.identity,
        "lock": lock.document,
        "ordered_seeds": list(lock.spec.ordered_seeds),
        "protocol": lock.spec.protocol,
        "provenance": execution_provenance_to_dict(artifact.provenance),
        "resolved_baseline": lock.document["resolved_baseline"],
        "resolved_candidate": lock.document["resolved_candidate"],
        "result_identity": None,
        "result_version": RESULT_VERSION,
        "spec": lock.spec.semantic_document(),
        "spec_identity": lock.spec.identity,
        "strength_artifact": {
            "evaluation_protocol": artifact.evaluation_protocol,
            "reference": "strength-artifact",
            "schema_version": artifact.schema_version,
            "sha256": artifact_digest,
        },
    }
    payload = {
        name: value for name, value in document.items() if name != "result_identity"
    }
    document["result_identity"] = _document_identity(payload)
    return document


def _validate_result_document(
    value: object,
    strength_artifact_path: Path,
) -> dict[str, object]:
    raw = expect_object(value, _RESULT_FIELDS, "result")
    if expect_int(raw["result_version"], "result_version") != RESULT_VERSION:
        raise StrengthEvaluationResultError("unsupported result version")
    if raw["execution_status"] != EXECUTION_STATUS_COMPLETED:
        raise StrengthEvaluationResultError("result is not a completed evaluation")
    for name in ("spec_identity", "lock_identity", "result_identity"):
        value = expect_str(raw[name], name)
        if len(value) != 64 or any(
            character not in "0123456789abcdef" for character in value
        ):
            raise StrengthEvaluationResultError(f"{name} is not a SHA-256 identity")

    spec_raw = expect_object(raw["spec"], _SEMANTIC_SPEC_FIELDS, "spec")
    semantic_spec = parse_strength_evaluation_spec(
        {
            **spec_raw,
            "artifact_output": "unused-strength-artifact.json",
            "result_output": "unused-strength-result.json",
        }
    )
    if raw["spec_identity"] != semantic_spec.identity:
        raise StrengthEvaluationResultError("spec identity does not match contents")
    if not _same_canonical_json_value(spec_raw, semantic_spec.semantic_document()):
        raise StrengthEvaluationResultError("spec semantics are not canonical")

    lock_raw = raw["lock"]
    if type(lock_raw) is not dict:
        raise StrengthEvaluationResultError("lock must be an object")
    expected_lock_fields = {
        "classification_rule",
        "execution_options",
        "execution_target",
        "expected_provenance_constraints",
        "lock_identity",
        "lock_version",
        "ordered_seeds",
        "protocol",
        "provenance",
        "resolved_baseline",
        "resolved_candidate",
        "spec_identity",
    }
    if set(lock_raw) != expected_lock_fields:
        raise StrengthEvaluationResultError("lock fields are invalid")
    lock_version = expect_int(lock_raw["lock_version"], "lock.lock_version")
    if lock_version != LOCK_VERSION:
        raise StrengthEvaluationResultError("lock version is unsupported")
    lock_payload = {
        name: item for name, item in lock_raw.items() if name != "lock_identity"
    }
    if lock_raw["lock_identity"] != _document_identity(lock_payload):
        raise StrengthEvaluationResultError("lock identity does not match contents")
    if raw["lock_identity"] != lock_raw["lock_identity"]:
        raise StrengthEvaluationResultError("result lock identity differs from lock")
    if lock_raw["spec_identity"] != semantic_spec.identity:
        raise StrengthEvaluationResultError("lock spec identity differs from spec")
    for name in (
        "classification_rule",
        "execution_options",
        "expected_provenance_constraints",
        "ordered_seeds",
        "protocol",
    ):
        if not _same_canonical_json_value(lock_raw[name], spec_raw[name]):
            raise StrengthEvaluationResultError(
                f"lock {name} differs from spec semantics"
            )
    if raw["protocol"] != SINGLE_ROUND_EVALUATION_PROTOCOL:
        raise StrengthEvaluationResultError("result protocol is unsupported")
    if raw["protocol"] != spec_raw["protocol"]:
        raise StrengthEvaluationResultError("result protocol differs from spec")
    options = expect_object(
        raw["execution_options"], {"max_steps", "workers"}, "execution_options"
    )
    max_steps = expect_int(options["max_steps"], "execution_options.max_steps")
    workers = expect_int(options["workers"], "execution_options.workers")
    if max_steps <= 0 or workers <= 0:
        raise StrengthEvaluationResultError("execution options must be positive")
    if not _same_canonical_json_value(
        raw["execution_options"], spec_raw["execution_options"]
    ):
        raise StrengthEvaluationResultError("result execution options differ from spec")
    seeds = tuple(
        expect_int(seed, f"ordered_seeds[{index}]")
        for index, seed in enumerate(expect_list(raw["ordered_seeds"], "ordered_seeds"))
    )
    if not seeds or len(set(seeds)) != len(seeds):
        raise StrengthEvaluationResultError("ordered seeds are invalid")
    if not _same_canonical_json_value(raw["ordered_seeds"], spec_raw["ordered_seeds"]):
        raise StrengthEvaluationResultError("result ordered seeds differ from spec")
    game_count = expect_int(raw["game_count"], "game_count")
    if game_count != SINGLE_ROUND_ROTATION_COUNT * len(seeds):
        raise StrengthEvaluationResultError("game count does not match ordered seeds")

    candidate = expect_object(
        raw["resolved_candidate"], {"identity", "reference"}, "resolved_candidate"
    )
    baseline = expect_object(
        raw["resolved_baseline"], {"identity", "reference"}, "resolved_baseline"
    )
    candidate_identity = expect_str(
        candidate["identity"], "resolved_candidate.identity"
    )
    baseline_identity = expect_str(baseline["identity"], "resolved_baseline.identity")
    if (
        not candidate_identity
        or not baseline_identity
        or candidate_identity == baseline_identity
    ):
        raise StrengthEvaluationResultError("resolved Policy identities are invalid")
    expect_str(candidate["reference"], "resolved_candidate.reference")
    expect_str(baseline["reference"], "resolved_baseline.reference")
    if candidate["reference"] != semantic_spec.candidate.reference:
        raise StrengthEvaluationResultError(
            "resolved candidate reference differs from spec"
        )
    if baseline["reference"] != semantic_spec.baseline.reference:
        raise StrengthEvaluationResultError(
            "resolved baseline reference differs from spec"
        )
    if (
        semantic_spec.candidate.identity is not None
        and candidate_identity != semantic_spec.candidate.identity
    ):
        raise StrengthEvaluationResultError(
            "resolved candidate identity differs from explicit spec identity"
        )
    if (
        semantic_spec.baseline.identity is not None
        and baseline_identity != semantic_spec.baseline.identity
    ):
        raise StrengthEvaluationResultError(
            "resolved baseline identity differs from explicit spec identity"
        )
    if not _same_canonical_json_value(
        raw["resolved_candidate"], lock_raw["resolved_candidate"]
    ):
        raise StrengthEvaluationResultError("result candidate differs from lock")
    if not _same_canonical_json_value(
        raw["resolved_baseline"], lock_raw["resolved_baseline"]
    ):
        raise StrengthEvaluationResultError("result baseline differs from lock")

    provenance = parse_execution_provenance(raw["provenance"])
    actual_provenance = execution_provenance_to_dict(provenance)
    for name, expected in semantic_spec.expected_provenance_constraints:
        if actual_provenance[name] != expected:
            raise StrengthEvaluationResultError(
                f"result provenance violates constraint {name!r}"
            )
    target = expect_object(
        raw["execution_target"], {"revision", "target_type"}, "execution_target"
    )
    if target["target_type"] != EXECUTION_TARGET_TYPE:
        raise StrengthEvaluationResultError("execution target type is unsupported")
    if target["revision"] != provenance.lisjong_arena_revision:
        raise StrengthEvaluationResultError(
            "execution target differs from result provenance"
        )
    if not _same_canonical_json_value(
        raw["execution_target"], lock_raw["execution_target"]
    ):
        raise StrengthEvaluationResultError("result execution target differs from lock")
    if not _same_canonical_json_value(raw["provenance"], lock_raw["provenance"]):
        raise StrengthEvaluationResultError("result provenance differs from lock")

    artifact_ref = expect_object(
        raw["strength_artifact"],
        {"evaluation_protocol", "reference", "schema_version", "sha256"},
        "strength_artifact",
    )
    if artifact_ref["reference"] != "strength-artifact":
        raise StrengthEvaluationResultError("strength artifact reference is invalid")
    if artifact_ref["evaluation_protocol"] != SINGLE_ROUND_EVALUATION_PROTOCOL:
        raise StrengthEvaluationResultError("strength artifact protocol is invalid")
    artifact_schema_version = expect_int(
        artifact_ref["schema_version"], "strength_artifact.schema_version"
    )
    if artifact_schema_version != SINGLE_ROUND_ARTIFACT_SCHEMA_VERSION:
        raise StrengthEvaluationResultError("strength artifact schema is invalid")
    digest = expect_str(artifact_ref["sha256"], "strength_artifact.sha256")
    if digest != _artifact_digest(strength_artifact_path):
        raise StrengthEvaluationResultError("strength artifact digest does not match")
    artifact = load_single_round_artifact(strength_artifact_path)
    if artifact.plan.candidate_identity != candidate_identity:
        raise StrengthEvaluationResultError("artifact candidate differs from result")
    if artifact.plan.baseline_identity != baseline_identity:
        raise StrengthEvaluationResultError("artifact baseline differs from result")
    if artifact.plan.seeds != seeds or artifact.plan.max_steps != max_steps:
        raise StrengthEvaluationResultError("artifact plan differs from result")
    if artifact.provenance != provenance:
        raise StrengthEvaluationResultError("artifact provenance differs from result")
    if len(artifact.game_results) != game_count:
        raise StrengthEvaluationResultError("artifact game count differs from result")
    if not _same_canonical_json_value(
        raw["canonical_summary"], summary_to_dict(artifact.summary)
    ):
        raise StrengthEvaluationResultError(
            "canonical summary is not derived from the strength artifact"
        )

    rule = _classification_rule(raw["classification_rule"])
    if not _same_canonical_json_value(
        raw["classification_rule"], spec_raw["classification_rule"]
    ):
        raise StrengthEvaluationResultError(
            "result classification rule differs from spec"
        )
    expected_classification = classify_strength_summary(artifact.summary, rule)
    if expected_classification["kind"] == "INVALID":
        raise StrengthEvaluationResultError("result classification is invalid")
    if not _same_canonical_json_value(raw["classification"], expected_classification):
        raise StrengthEvaluationResultError(
            "classification is not derived from the strength artifact"
        )
    payload = {name: item for name, item in raw.items() if name != "result_identity"}
    if raw["result_identity"] != _document_identity(payload):
        raise StrengthEvaluationResultError("result identity does not match contents")
    return raw


def load_strength_evaluation_result(
    path: str | Path,
    *,
    strength_artifact_path: str | Path,
) -> dict[str, object]:
    """Strictly read a result and rebind it to its measurement artifact."""
    try:
        return _validate_result_document(
            read_json_document(Path(path)), Path(strength_artifact_path)
        )
    except StrengthEvaluationResultError:
        raise
    except (
        ArtifactValidationError,
        SingleRoundArtifactError,
        json.JSONDecodeError,
        OSError,
        TypeError,
        ValueError,
    ) as exc:
        raise StrengthEvaluationResultError(
            "result is unreadable, malformed, or inconsistent"
        ) from exc


def run_strength_evaluation(
    spec: StrengthEvaluationSpec,
) -> dict[str, object]:
    """Run one locked evaluation and return its strict durable result."""
    lock = prepare_strength_evaluation(spec)
    try:
        _validate_locked_execution(lock)
    except (ExecutionSafetyError, SingleRoundArtifactError) as exc:
        raise StrengthEvaluationPreflightError(str(exc)) from exc

    try:
        if spec.workers == 1:
            evaluation = run_single_round_evaluation(lock.evaluation_plan)
        else:
            evaluation = run_single_round_evaluation_parallel(
                lock.evaluation_plan, max_workers=spec.workers
            )
        save_single_round_artifact(evaluation, spec.artifact_output)
        artifact = load_single_round_artifact(spec.artifact_output)
        _require_artifact_matches_lock(artifact, lock)
        document = _result_document(
            lock, artifact, _artifact_digest(spec.artifact_output)
        )
        write_new_artifact_file(spec.result_output, canonical_json_text(document))
        return load_strength_evaluation_result(
            spec.result_output, strength_artifact_path=spec.artifact_output
        )
    except StrengthEvaluationError:
        raise
    except Exception as exc:
        raise StrengthEvaluationExecutionError(
            "evaluation or durable evidence acceptance failed"
        ) from exc


def format_compact_result_summary(result: dict[str, object]) -> str:
    """Format validated fields without reimplementing strength aggregation."""
    candidate = result["resolved_candidate"]["identity"]
    baseline = result["resolved_baseline"]["identity"]
    statistics = result["canonical_summary"]["seed_block_statistics"]
    lower = statistics["normal_approx_95_interval_lower"]
    upper = statistics["normal_approx_95_interval_upper"]
    interval = "N/A" if lower is None else f"[{lower:.3f}, {upper:.3f}]"
    return (
        f"{result['execution_status']} candidate={candidate} baseline={baseline} "
        f"games={result['game_count']} "
        f"mean_seed_block_delta={statistics['mean_seed_block_delta']:.3f} "
        f"interval95={interval} classification={result['classification']['label']}"
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run one portable locked Policy-vs-Policy ABBB evaluation."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    run_parser = subparsers.add_parser("run", help="execute one JSON spec")
    run_parser.add_argument("spec", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        spec = load_strength_evaluation_spec(args.spec)
        result = run_strength_evaluation(spec)
    except StrengthEvaluationError as exc:
        error = {
            "error_type": type(exc).__name__,
            "message": str(exc),
            "status": "FAILED",
        }
        print(canonical_json_text(error), file=sys.stderr, end="")
        return 1
    print(canonical_json_text(result), end="")
    print(format_compact_result_summary(result), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CLASSIFICATION_RULE_TYPE",
    "IntervalClassificationRule",
    "LockedStrengthEvaluationPlan",
    "PolicyReference",
    "StrengthEvaluationError",
    "StrengthEvaluationExecutionError",
    "StrengthEvaluationPreflightError",
    "StrengthEvaluationResultError",
    "StrengthEvaluationSpec",
    "StrengthEvaluationSpecError",
    "classify_strength_summary",
    "format_compact_result_summary",
    "load_strength_evaluation_result",
    "load_strength_evaluation_spec",
    "main",
    "parse_strength_evaluation_spec",
    "prepare_strength_evaluation",
    "run_strength_evaluation",
]
