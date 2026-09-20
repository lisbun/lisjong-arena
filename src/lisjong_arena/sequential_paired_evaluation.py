"""Repeated-look-valid sequential mechanics for paired Arena evaluations.

This module is deliberately separate from paired_evaluation.py.  The latter owns
fixed-N paired aggregation mechanics; this module owns one bounded sequential
v1 protocol:

* a predeclared finite look schedule,
* equal Bonferroni alpha spending across those looks,
* normal-approximation intervals for the paired mean at each look,
* practical-effect boundaries at +/- delta_min, and
* no futility rule in v1.

The Bonferroni correction controls repeated-look error by allocating alpha / K
to each of K predeclared looks.  It does not make the underlying marginal
normal approximation exact; consumers must use sample sizes for which that
approximation is part of their locked scientific contract.

Scientific meaning remains caller-owned.  Callers bind an estimand id, sign
convention, paired-unit id, ordered seed population, and protocol id into the
immutable protocol identity before exposing the first result-bearing look.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from pathlib import Path
from statistics import NormalDist
from typing import Iterable, Iterator

from lisjong_arena._artifact_io import (
    ArtifactValidationError,
    canonical_json_text,
    expect_bool,
    expect_float,
    expect_int,
    expect_list,
    expect_object,
    expect_str,
    read_json_document,
    write_new_artifact_file,
)
from lisjong_arena.paired_evaluation import PairedSeedDelta

SCHEMA = "sequential-paired-evaluation-v1"
METHOD_ID = "bonferroni-normal-approx-alpha-spending-v1"
NUMERIC_IMPLEMENTATION_ID = (
    "cpython-3.14-statistics.NormalDist.inv_cdf+math.fsum+sample-sd-n-minus-1-v1"
)
FUTILITY_RULE_ID = "none-v1"
SIDEDNESS = "two-sided"
MIN_LOOK_BLOCK_COUNT = 20

DECISION_CONTINUE = "CONTINUE"
DECISION_POSITIVE = "STOP_POSITIVE_EFFECT_ESTABLISHED"
DECISION_NEGATIVE = "STOP_NEGATIVE_EFFECT_ESTABLISHED"
DECISION_FINAL_INCONCLUSIVE = "FINAL_INCONCLUSIVE"

STOPPING_REASON_POSITIVE = "POSITIVE_EFFECT_ESTABLISHED"
STOPPING_REASON_NEGATIVE = "NEGATIVE_EFFECT_ESTABLISHED"
STOPPING_REASON_MAXIMUM = "MAXIMUM_SAMPLE_REACHED"


class SequentialPairedEvaluationError(ValueError):
    """Sequential paired protocol or artifact is malformed or inconsistent."""


def _require_non_empty_string(value: object, context: str) -> str:
    if type(value) is not str or not value:
        raise SequentialPairedEvaluationError(f"{context} must be a non-empty string")
    return value


def _require_finite_float(value: object, context: str) -> float:
    if type(value) is not float or not math.isfinite(value):
        raise SequentialPairedEvaluationError(f"{context} must be a finite float")
    return value


@dataclass(frozen=True, slots=True)
class SequentialPairedProtocol:
    """Immutable protocol inputs for one sequential paired evaluation."""

    protocol_id: str
    estimand_id: str
    sign_convention: str
    paired_unit_id: str
    runtime_requirements_id: str
    provenance_requirements_id: str
    ordered_seeds: tuple[int, ...]
    look_block_counts: tuple[int, ...]
    familywise_alpha: float
    delta_min: float

    def __post_init__(self) -> None:
        for value, context in (
            (self.protocol_id, "protocol_id"),
            (self.estimand_id, "estimand_id"),
            (self.sign_convention, "sign_convention"),
            (self.paired_unit_id, "paired_unit_id"),
            (self.runtime_requirements_id, "runtime_requirements_id"),
            (self.provenance_requirements_id, "provenance_requirements_id"),
        ):
            _require_non_empty_string(value, context)

        if type(self.ordered_seeds) is not tuple or not self.ordered_seeds:
            raise SequentialPairedEvaluationError(
                "ordered_seeds must be a non-empty tuple"
            )
        if any(type(seed) is not int or seed < 0 for seed in self.ordered_seeds):
            raise SequentialPairedEvaluationError(
                "ordered_seeds must contain non-negative integers"
            )
        if len(set(self.ordered_seeds)) != len(self.ordered_seeds):
            raise SequentialPairedEvaluationError("ordered_seeds must be unique")

        if type(self.look_block_counts) is not tuple or not self.look_block_counts:
            raise SequentialPairedEvaluationError(
                "look_block_counts must be a non-empty tuple"
            )
        if any(type(count) is not int for count in self.look_block_counts):
            raise SequentialPairedEvaluationError(
                "look_block_counts must contain integers"
            )
        if self.look_block_counts[0] < MIN_LOOK_BLOCK_COUNT:
            raise SequentialPairedEvaluationError(
                f"the first look must contain at least {MIN_LOOK_BLOCK_COUNT} paired units"
            )
        if any(
            later <= earlier
            for earlier, later in zip(
                self.look_block_counts, self.look_block_counts[1:], strict=False
            )
        ):
            raise SequentialPairedEvaluationError(
                "look_block_counts must be strictly increasing"
            )
        if self.look_block_counts[-1] != len(self.ordered_seeds):
            raise SequentialPairedEvaluationError(
                "the final look must equal the locked maximum paired-unit count"
            )

        alpha = _require_finite_float(self.familywise_alpha, "familywise_alpha")
        if not 0.0 < alpha < 1.0:
            raise SequentialPairedEvaluationError(
                "familywise_alpha must be strictly between zero and one"
            )
        delta_min = _require_finite_float(self.delta_min, "delta_min")
        if delta_min <= 0.0:
            raise SequentialPairedEvaluationError("delta_min must be strictly positive")

    @property
    def maximum_block_count(self) -> int:
        return len(self.ordered_seeds)

    @property
    def per_look_alpha(self) -> float:
        return self.familywise_alpha / len(self.look_block_counts)

    @property
    def critical_value(self) -> float:
        return NormalDist().inv_cdf(1.0 - self.per_look_alpha / 2.0)

    def to_document(self) -> dict[str, object]:
        return {
            "critical_value": self.critical_value,
            "delta_min": self.delta_min,
            "estimand_id": self.estimand_id,
            "familywise_alpha": self.familywise_alpha,
            "futility_rule_id": FUTILITY_RULE_ID,
            "look_block_counts": list(self.look_block_counts),
            "maximum_block_count": self.maximum_block_count,
            "method_id": METHOD_ID,
            "numeric_implementation_id": NUMERIC_IMPLEMENTATION_ID,
            "ordered_seeds": list(self.ordered_seeds),
            "paired_unit_id": self.paired_unit_id,
            "per_look_alpha": self.per_look_alpha,
            "provenance_requirements_id": self.provenance_requirements_id,
            "runtime_requirements_id": self.runtime_requirements_id,
            "sidedness": SIDEDNESS,
            "protocol_id": self.protocol_id,
            "sign_convention": self.sign_convention,
        }

    @property
    def identity(self) -> str:
        return hashlib.sha256(
            canonical_json_text(self.to_document()).encode("utf-8")
        ).hexdigest()


@dataclass(frozen=True, slots=True)
class SequentialLookResult:
    """One deterministic result-bearing look."""

    look_index: int
    cumulative_paired_units: int
    mean_delta: float
    sample_standard_deviation: float
    standard_error: float
    look_alpha: float
    critical_value: float
    interval_lower: float
    interval_upper: float
    decision: str
    stopped: bool
    stopping_reason: str | None
    remaining_maximum_budget: int

    def to_document(self) -> dict[str, object]:
        return {
            "critical_value": self.critical_value,
            "cumulative_paired_units": self.cumulative_paired_units,
            "decision": self.decision,
            "interval_lower": self.interval_lower,
            "interval_upper": self.interval_upper,
            "look_alpha": self.look_alpha,
            "look_index": self.look_index,
            "mean_delta": self.mean_delta,
            "remaining_maximum_budget": self.remaining_maximum_budget,
            "sample_standard_deviation": self.sample_standard_deviation,
            "standard_error": self.standard_error,
            "stopped": self.stopped,
            "stopping_reason": self.stopping_reason,
        }


def _validate_paired_units(
    protocol: SequentialPairedProtocol,
    paired_units: tuple[PairedSeedDelta, ...],
) -> None:
    if type(paired_units) is not tuple or not paired_units:
        raise SequentialPairedEvaluationError(
            "paired_units must be a non-empty tuple"
        )
    if len(paired_units) > protocol.maximum_block_count:
        raise SequentialPairedEvaluationError(
            "paired_units exceed the locked maximum sample size"
        )
    expected_seeds = protocol.ordered_seeds[: len(paired_units)]
    actual_seeds: list[int] = []
    for index, item in enumerate(paired_units):
        if type(item) is not PairedSeedDelta:
            raise SequentialPairedEvaluationError(
                f"paired_units[{index}] must be a PairedSeedDelta"
            )
        actual_seeds.append(item.seed)
        for value, field in (
            (item.candidate_mean, "candidate_mean"),
            (item.parent_mean, "parent_mean"),
            (item.delta, "delta"),
        ):
            if type(value) is not float or not math.isfinite(value):
                raise SequentialPairedEvaluationError(
                    f"paired_units[{index}].{field} must be a finite float"
                )
        if item.delta != item.candidate_mean - item.parent_mean:
            raise SequentialPairedEvaluationError(
                f"paired_units[{index}].delta is not re-derived from the paired means"
            )
    if tuple(actual_seeds) != expected_seeds:
        raise SequentialPairedEvaluationError(
            "paired_units do not follow the locked ordered seed prefix"
        )


def evaluate_sequential_look(
    protocol: SequentialPairedProtocol,
    paired_units: tuple[PairedSeedDelta, ...],
) -> SequentialLookResult:
    """Evaluate exactly one allowed look without inspecting future paired units."""
    _validate_paired_units(protocol, paired_units)
    count = len(paired_units)
    try:
        zero_based_index = protocol.look_block_counts.index(count)
    except ValueError as exc:
        raise SequentialPairedEvaluationError(
            "paired-unit count is not an allowed predeclared look"
        ) from exc

    values = tuple(item.delta for item in paired_units)
    mean_delta = math.fsum(values) / count
    variance = math.fsum((value - mean_delta) ** 2 for value in values) / (count - 1)
    sample_standard_deviation = math.sqrt(variance)
    standard_error = sample_standard_deviation / math.sqrt(count)
    critical_value = protocol.critical_value
    interval_lower = mean_delta - critical_value * standard_error
    interval_upper = mean_delta + critical_value * standard_error

    if interval_lower > protocol.delta_min:
        decision = DECISION_POSITIVE
        stopped = True
        stopping_reason = STOPPING_REASON_POSITIVE
    elif interval_upper < -protocol.delta_min:
        decision = DECISION_NEGATIVE
        stopped = True
        stopping_reason = STOPPING_REASON_NEGATIVE
    elif count == protocol.maximum_block_count:
        decision = DECISION_FINAL_INCONCLUSIVE
        stopped = True
        stopping_reason = STOPPING_REASON_MAXIMUM
    else:
        decision = DECISION_CONTINUE
        stopped = False
        stopping_reason = None

    return SequentialLookResult(
        look_index=zero_based_index + 1,
        cumulative_paired_units=count,
        mean_delta=mean_delta,
        sample_standard_deviation=sample_standard_deviation,
        standard_error=standard_error,
        look_alpha=protocol.per_look_alpha,
        critical_value=critical_value,
        interval_lower=interval_lower,
        interval_upper=interval_upper,
        decision=decision,
        stopped=stopped,
        stopping_reason=stopping_reason,
        remaining_maximum_budget=protocol.maximum_block_count - count,
    )


def build_sequential_result(
    protocol: SequentialPairedProtocol,
    paired_units: tuple[PairedSeedDelta, ...],
) -> dict[str, object]:
    """Build one immutable cumulative snapshot at an allowed look."""
    _validate_paired_units(protocol, paired_units)
    current_count = len(paired_units)
    if current_count not in protocol.look_block_counts:
        raise SequentialPairedEvaluationError(
            "sequential result must end exactly at a predeclared look"
        )

    looks: list[SequentialLookResult] = []
    for look_count in protocol.look_block_counts:
        if look_count > current_count:
            break
        look = evaluate_sequential_look(protocol, paired_units[:look_count])
        if looks and looks[-1].stopped:
            raise SequentialPairedEvaluationError(
                "continued execution after a terminal sequential decision"
            )
        looks.append(look)

    if not looks:
        raise SequentialPairedEvaluationError("sequential result contains no looks")

    current = looks[-1]
    payload: dict[str, object] = {
        "current_look_index": current.look_index,
        "looks": [look.to_document() for look in looks],
        "paired_units": [item.to_document() for item in paired_units],
        "protocol": protocol.to_document(),
        "protocol_identity": protocol.identity,
        "schema": SCHEMA,
        "stopped": current.stopped,
        "terminal_decision": current.decision if current.stopped else None,
    }
    document = dict(payload)
    document["result_identity"] = hashlib.sha256(
        canonical_json_text(payload).encode("utf-8")
    ).hexdigest()
    return document


def _protocol_from_document(value: object) -> SequentialPairedProtocol:
    raw = expect_object(
        value,
        {
            "critical_value",
            "delta_min",
            "estimand_id",
            "familywise_alpha",
            "futility_rule_id",
            "look_block_counts",
            "maximum_block_count",
            "method_id",
            "numeric_implementation_id",
            "ordered_seeds",
            "paired_unit_id",
            "per_look_alpha",
            "provenance_requirements_id",
            "protocol_id",
            "runtime_requirements_id",
            "sidedness",
            "sign_convention",
        },
        "protocol",
    )
    if expect_str(raw["method_id"], "protocol.method_id") != METHOD_ID:
        raise SequentialPairedEvaluationError("sequential method id drifted")
    if (
        expect_str(raw["numeric_implementation_id"], "protocol.numeric_implementation_id")
        != NUMERIC_IMPLEMENTATION_ID
    ):
        raise SequentialPairedEvaluationError(
            "sequential numeric implementation id drifted"
        )
    if expect_str(raw["futility_rule_id"], "protocol.futility_rule_id") != FUTILITY_RULE_ID:
        raise SequentialPairedEvaluationError("sequential futility rule drifted")
    if expect_str(raw["sidedness"], "protocol.sidedness") != SIDEDNESS:
        raise SequentialPairedEvaluationError("sequential sidedness drifted")

    ordered_seeds = tuple(
        expect_int(seed, f"protocol.ordered_seeds[{index}]")
        for index, seed in enumerate(
            expect_list(raw["ordered_seeds"], "protocol.ordered_seeds")
        )
    )
    look_block_counts = tuple(
        expect_int(count, f"protocol.look_block_counts[{index}]")
        for index, count in enumerate(
            expect_list(raw["look_block_counts"], "protocol.look_block_counts")
        )
    )
    protocol = SequentialPairedProtocol(
        protocol_id=expect_str(raw["protocol_id"], "protocol.protocol_id"),
        estimand_id=expect_str(raw["estimand_id"], "protocol.estimand_id"),
        sign_convention=expect_str(
            raw["sign_convention"], "protocol.sign_convention"
        ),
        paired_unit_id=expect_str(raw["paired_unit_id"], "protocol.paired_unit_id"),
        runtime_requirements_id=expect_str(
            raw["runtime_requirements_id"], "protocol.runtime_requirements_id"
        ),
        provenance_requirements_id=expect_str(
            raw["provenance_requirements_id"], "protocol.provenance_requirements_id"
        ),
        ordered_seeds=ordered_seeds,
        look_block_counts=look_block_counts,
        familywise_alpha=expect_float(
            raw["familywise_alpha"], "protocol.familywise_alpha"
        ),
        delta_min=expect_float(raw["delta_min"], "protocol.delta_min"),
    )
    if expect_int(
        raw["maximum_block_count"], "protocol.maximum_block_count"
    ) != protocol.maximum_block_count:
        raise SequentialPairedEvaluationError("maximum sample size drifted")
    if expect_float(
        raw["per_look_alpha"], "protocol.per_look_alpha"
    ) != protocol.per_look_alpha:
        raise SequentialPairedEvaluationError("per-look alpha drifted")
    if expect_float(
        raw["critical_value"], "protocol.critical_value"
    ) != protocol.critical_value:
        raise SequentialPairedEvaluationError("critical boundary drifted")
    if raw != protocol.to_document():
        raise SequentialPairedEvaluationError("protocol document drifted")
    return protocol


def _paired_unit_from_document(value: object, index: int) -> PairedSeedDelta:
    context = f"paired_units[{index}]"
    raw = expect_object(
        value,
        {"candidate_mean", "delta", "parent_mean", "seed"},
        context,
    )
    return PairedSeedDelta(
        seed=expect_int(raw["seed"], f"{context}.seed"),
        candidate_mean=expect_float(
            raw["candidate_mean"], f"{context}.candidate_mean"
        ),
        parent_mean=expect_float(raw["parent_mean"], f"{context}.parent_mean"),
        delta=expect_float(raw["delta"], f"{context}.delta"),
    )


def parse_sequential_result(value: object) -> dict[str, object]:
    """Strictly replay a stored cumulative result and fail closed on drift."""
    try:
        raw = expect_object(
            value,
            {
                "current_look_index",
                "looks",
                "paired_units",
                "protocol",
                "protocol_identity",
                "result_identity",
                "schema",
                "stopped",
                "terminal_decision",
            },
            "sequential_result",
        )
        if expect_str(raw["schema"], "sequential_result.schema") != SCHEMA:
            raise SequentialPairedEvaluationError("unsupported sequential result schema")
        protocol = _protocol_from_document(raw["protocol"])
        if (
            expect_str(
                raw["protocol_identity"], "sequential_result.protocol_identity"
            )
            != protocol.identity
        ):
            raise SequentialPairedEvaluationError("protocol identity mismatch")

        paired_units = tuple(
            _paired_unit_from_document(item, index)
            for index, item in enumerate(
                expect_list(raw["paired_units"], "sequential_result.paired_units")
            )
        )
        expect_int(raw["current_look_index"], "sequential_result.current_look_index")
        expect_list(raw["looks"], "sequential_result.looks")
        expect_bool(raw["stopped"], "sequential_result.stopped")
        if raw["terminal_decision"] is not None:
            expect_str(
                raw["terminal_decision"], "sequential_result.terminal_decision"
            )
        expect_str(raw["result_identity"], "sequential_result.result_identity")

        expected = build_sequential_result(protocol, paired_units)
        if raw != expected:
            raise SequentialPairedEvaluationError(
                "sequential result does not match deterministic replay"
            )
        return dict(raw)
    except ArtifactValidationError as exc:
        raise SequentialPairedEvaluationError(str(exc)) from exc


def save_sequential_result(
    document: dict[str, object], path: str | Path
) -> Path:
    """Persist one immutable result-bearing look snapshot."""
    parsed = parse_sequential_result(document)
    destination = Path(path)
    write_new_artifact_file(destination, canonical_json_text(parsed))
    return destination


def load_sequential_result(path: str | Path) -> dict[str, object]:
    """Load and strictly replay one stored sequential look snapshot."""
    try:
        return parse_sequential_result(read_json_document(Path(path)))
    except ArtifactValidationError as exc:
        raise SequentialPairedEvaluationError(str(exc)) from exc


def iter_sequential_look_results(
    protocol: SequentialPairedProtocol,
    source: Iterable[PairedSeedDelta],
) -> Iterator[dict[str, object]]:
    """Consume a lazy paired-unit source only until the locked terminal decision."""
    iterator = iter(source)
    paired_units: list[PairedSeedDelta] = []
    look_counts = set(protocol.look_block_counts)

    for expected_seed in protocol.ordered_seeds:
        try:
            item = next(iterator)
        except StopIteration as exc:
            raise SequentialPairedEvaluationError(
                "paired-unit source ended before the next locked look"
            ) from exc
        paired_units.append(item)
        frozen = tuple(paired_units)
        _validate_paired_units(protocol, frozen)
        if item.seed != expected_seed:
            raise SequentialPairedEvaluationError(
                "paired-unit source drifted from the locked seed order"
            )

        if len(frozen) not in look_counts:
            continue
        document = build_sequential_result(protocol, frozen)
        yield document
        if document["stopped"]:
            return

    raise SequentialPairedEvaluationError(
        "sequential source exhausted the maximum sample without a terminal result"
    )


__all__ = [
    "DECISION_CONTINUE",
    "DECISION_FINAL_INCONCLUSIVE",
    "DECISION_NEGATIVE",
    "DECISION_POSITIVE",
    "FUTILITY_RULE_ID",
    "METHOD_ID",
    "MIN_LOOK_BLOCK_COUNT",
    "NUMERIC_IMPLEMENTATION_ID",
    "SCHEMA",
    "SIDEDNESS",
    "SequentialLookResult",
    "SequentialPairedEvaluationError",
    "SequentialPairedProtocol",
    "build_sequential_result",
    "evaluate_sequential_look",
    "iter_sequential_look_results",
    "load_sequential_result",
    "parse_sequential_result",
    "save_sequential_result",
]
