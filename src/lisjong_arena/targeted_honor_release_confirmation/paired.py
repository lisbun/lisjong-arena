"""Issue #270 paired confirmation result and strict verification."""

from __future__ import annotations

import hashlib
from pathlib import Path

from lisjong_arena._artifact_io import (
    ArtifactValidationError,
    canonical_json_text,
    expect_float,
    expect_int,
    expect_list,
    expect_object,
    expect_str,
    read_json_document,
    write_new_artifact_file,
)
from lisjong_arena._execution_safety import require_new_artifact_destinations
from lisjong_arena.progression_development.paired import (
    PairedSeedDelta,
    PairedSummary,
    arm_diagnostics,
    artifact_file_digest,
    focal_seed_block_means,
    load_arm_artifact,
    summarize_paired_deltas,
)
from lisjong_arena.single_round_artifact import (
    SingleRoundStrengthArtifact,
    execution_provenance_to_dict,
)

from .protocol import (
    CANDIDATE_IDENTITY,
    CLASSIFICATION_RULE_ID,
    COMPARATOR_IDENTITY,
    CONFIRMED_NEGATIVE_LABEL,
    CONFIRMED_POSITIVE_LABEL,
    GAMES_PER_ARM,
    INCONCLUSIVE_LABEL,
    LISJONG_REVISION,
    MAX_STEPS,
    PARENT_IDENTITY,
    SEED_BLOCK_COUNT,
    protocol_document,
    require_confirmation_population,
    require_exact_candidate_semantics,
    require_exact_comparator,
)
from .trace import verify_trace_artifact

PAIRED_RESULT_VERSION = 1
CLASSIFIED_RESULT_VERSION = 1


class TargetedHonorReleaseConfirmationPairedError(ValueError):
    """Issue #270 paired evidence is malformed or inconsistent."""


def _identity(payload: dict[str, object]) -> str:
    return hashlib.sha256(canonical_json_text(payload).encode("utf-8")).hexdigest()


def _require_arm(
    artifact: SingleRoundStrengthArtifact,
    *,
    expected_identity: str,
    seeds: tuple[int, ...],
    arm: str,
) -> None:
    if artifact.plan.candidate_identity != expected_identity:
        raise TargetedHonorReleaseConfirmationPairedError(
            f"{arm} candidate identity differs from locked protocol"
        )
    if artifact.plan.baseline_identity != COMPARATOR_IDENTITY:
        raise TargetedHonorReleaseConfirmationPairedError(
            f"{arm} baseline identity differs from passive comparator"
        )
    if artifact.plan.seeds != seeds:
        raise TargetedHonorReleaseConfirmationPairedError(
            f"{arm} ordered seeds differ from locked population"
        )
    if artifact.plan.max_steps != MAX_STEPS:
        raise TargetedHonorReleaseConfirmationPairedError(f"{arm} max_steps drifted")
    if len(artifact.game_results) != GAMES_PER_ARM:
        raise TargetedHonorReleaseConfirmationPairedError(
            f"{arm} must contain exactly {GAMES_PER_ARM} games"
        )
    if artifact.provenance.lisjong_revision != LISJONG_REVISION:
        raise TargetedHonorReleaseConfirmationPairedError(
            f"{arm} lisjong revision is not exact #174 revision"
        )


def derive_paired_deltas(
    candidate: SingleRoundStrengthArtifact,
    parent: SingleRoundStrengthArtifact,
    *,
    seeds: tuple[int, ...],
) -> tuple[PairedSeedDelta, ...]:
    locked = require_confirmation_population(seeds)
    _require_arm(candidate, expected_identity=CANDIDATE_IDENTITY, seeds=locked, arm="H")
    _require_arm(parent, expected_identity=PARENT_IDENTITY, seeds=locked, arm="C")
    if candidate.provenance != parent.provenance:
        raise TargetedHonorReleaseConfirmationPairedError(
            "H and C arms must share exact execution provenance"
        )
    h_blocks = focal_seed_block_means(candidate.game_results)
    c_blocks = focal_seed_block_means(parent.game_results)
    if tuple(seed for seed, _ in h_blocks) != locked:
        raise TargetedHonorReleaseConfirmationPairedError("H seed-block order drifted")
    if tuple(seed for seed, _ in c_blocks) != locked:
        raise TargetedHonorReleaseConfirmationPairedError("C seed-block order drifted")
    deltas = tuple(
        PairedSeedDelta(
            seed=seed,
            candidate_mean=h_mean,
            parent_mean=c_mean,
            delta=h_mean - c_mean,
        )
        for (seed, h_mean), (_, c_mean) in zip(h_blocks, c_blocks, strict=True)
    )
    if len(deltas) != SEED_BLOCK_COUNT:
        raise TargetedHonorReleaseConfirmationPairedError(
            f"primary result must contain exactly {SEED_BLOCK_COUNT} paired deltas"
        )
    return deltas


def classify(summary: PairedSummary) -> dict[str, object]:
    if summary.block_count != SEED_BLOCK_COUNT:
        raise TargetedHonorReleaseConfirmationPairedError(
            f"classification requires exactly {SEED_BLOCK_COUNT} paired blocks"
        )
    if summary.interval_lower > 0.0:
        kind, label = "CONFIRMED_POSITIVE", CONFIRMED_POSITIVE_LABEL
    elif summary.interval_upper < 0.0:
        kind, label = "CONFIRMED_NEGATIVE", CONFIRMED_NEGATIVE_LABEL
    else:
        kind, label = "INCONCLUSIVE", INCONCLUSIVE_LABEL
    return {"kind": kind, "label": label, "rule_id": CLASSIFICATION_RULE_ID}


def _arm_document(
    artifact: SingleRoundStrengthArtifact, path: str | Path
) -> dict[str, object]:
    return {
        "artifact_digest": artifact_file_digest(path),
        "candidate_identity": artifact.plan.candidate_identity,
        "comparator_identity": artifact.plan.baseline_identity,
        "diagnostics": arm_diagnostics(artifact),
        "game_count": len(artifact.game_results),
        "ordered_seeds": list(artifact.plan.seeds),
    }


def _trace_reference(
    document: dict[str, object], path: str | Path
) -> dict[str, object]:
    return {
        "artifact_digest": artifact_file_digest(path),
        "result_identity": document["result_identity"],
        "summary": document["summary"],
    }


def build_paired_result(
    *,
    candidate_artifact: SingleRoundStrengthArtifact,
    candidate_artifact_path: str | Path,
    parent_artifact: SingleRoundStrengthArtifact,
    parent_artifact_path: str | Path,
    candidate_trace_artifact: dict[str, object],
    candidate_trace_artifact_path: str | Path,
    seeds: tuple[int, ...],
    worker_count: int,
) -> dict[str, object]:
    if type(worker_count) is not int or worker_count <= 0:
        raise TargetedHonorReleaseConfirmationPairedError(
            "worker_count must be a positive int"
        )
    locked = require_confirmation_population(seeds)
    deltas = derive_paired_deltas(candidate_artifact, parent_artifact, seeds=locked)
    summary = summarize_paired_deltas(deltas)
    payload: dict[str, object] = {
        "arms": {
            "candidate": _arm_document(candidate_artifact, candidate_artifact_path),
            "parent": _arm_document(parent_artifact, parent_artifact_path),
        },
        "candidate_binding": require_exact_candidate_semantics().to_document(),
        "candidate_trace": _trace_reference(
            candidate_trace_artifact, candidate_trace_artifact_path
        ),
        "classification": classify(summary),
        "comparator_binding": require_exact_comparator(),
        "paired_deltas": [item.to_document() for item in deltas],
        "primary_summary": summary.to_document(),
        "protocol": protocol_document(locked),
        "provenance": execution_provenance_to_dict(candidate_artifact.provenance),
        "result_version": PAIRED_RESULT_VERSION,
        "worker_count": worker_count,
    }
    document = dict(payload)
    document["result_identity"] = _identity(payload)
    return document


def _parse_delta(value: object, index: int) -> PairedSeedDelta:
    context = f"paired_deltas[{index}]"
    raw = expect_object(
        value, {"candidate_mean", "delta", "parent_mean", "seed"}, context
    )
    return PairedSeedDelta(
        seed=expect_int(raw["seed"], f"{context}.seed"),
        candidate_mean=expect_float(raw["candidate_mean"], f"{context}.candidate_mean"),
        parent_mean=expect_float(raw["parent_mean"], f"{context}.parent_mean"),
        delta=expect_float(raw["delta"], f"{context}.delta"),
    )


def _parse_summary(value: object) -> PairedSummary:
    raw = expect_object(
        value,
        {
            "block_count",
            "interval_lower",
            "interval_upper",
            "mean_delta",
            "sample_standard_deviation",
            "standard_error",
        },
        "primary_summary",
    )
    return PairedSummary(
        block_count=expect_int(raw["block_count"], "primary_summary.block_count"),
        mean_delta=expect_float(raw["mean_delta"], "primary_summary.mean_delta"),
        sample_standard_deviation=expect_float(
            raw["sample_standard_deviation"],
            "primary_summary.sample_standard_deviation",
        ),
        standard_error=expect_float(
            raw["standard_error"], "primary_summary.standard_error"
        ),
        interval_lower=expect_float(
            raw["interval_lower"], "primary_summary.interval_lower"
        ),
        interval_upper=expect_float(
            raw["interval_upper"], "primary_summary.interval_upper"
        ),
    )


def parse_paired_result(value: object) -> dict[str, object]:
    raw = expect_object(
        value,
        {
            "arms",
            "candidate_binding",
            "candidate_trace",
            "classification",
            "comparator_binding",
            "paired_deltas",
            "primary_summary",
            "protocol",
            "provenance",
            "result_identity",
            "result_version",
            "worker_count",
        },
        "paired_result",
    )
    if (
        expect_int(raw["result_version"], "paired_result.result_version")
        != PAIRED_RESULT_VERSION
    ):
        raise TargetedHonorReleaseConfirmationPairedError(
            "unsupported paired result version"
        )
    protocol = expect_object(
        raw["protocol"],
        {
            "classification_rule_id",
            "estimand",
            "formal_test",
            "game_mode",
            "games_per_arm",
            "interval_method",
            "max_steps",
            "ordered_seeds",
            "primary_unit",
            "protocol_id",
            "role",
            "rotation_count",
            "seed_block_count",
            "total_games",
        },
        "paired_result.protocol",
    )
    seeds = require_confirmation_population(
        tuple(
            expect_int(item, f"paired_result.protocol.ordered_seeds[{index}]")
            for index, item in enumerate(
                expect_list(
                    protocol["ordered_seeds"],
                    "paired_result.protocol.ordered_seeds",
                )
            )
        )
    )
    if dict(protocol) != protocol_document(seeds):
        raise TargetedHonorReleaseConfirmationPairedError(
            "paired result protocol block drifted"
        )
    deltas = tuple(
        _parse_delta(item, index)
        for index, item in enumerate(expect_list(raw["paired_deltas"], "paired_deltas"))
    )
    if len(deltas) != SEED_BLOCK_COUNT:
        raise TargetedHonorReleaseConfirmationPairedError(
            f"paired result must persist exactly {SEED_BLOCK_COUNT} D_s values"
        )
    if tuple(item.seed for item in deltas) != seeds:
        raise TargetedHonorReleaseConfirmationPairedError(
            "paired delta seed order differs from locked population"
        )
    summary = _parse_summary(raw["primary_summary"])
    if summary != summarize_paired_deltas(deltas):
        raise TargetedHonorReleaseConfirmationPairedError(
            "primary summary differs from persisted paired deltas"
        )
    if raw["classification"] != classify(summary):
        raise TargetedHonorReleaseConfirmationPairedError(
            "classification differs from primary summary"
        )
    if raw["candidate_binding"] != require_exact_candidate_semantics().to_document():
        raise TargetedHonorReleaseConfirmationPairedError(
            "candidate binding drifted"
        )
    if raw["comparator_binding"] != require_exact_comparator():
        raise TargetedHonorReleaseConfirmationPairedError(
            "comparator binding drifted"
        )
    worker_count = expect_int(raw["worker_count"], "paired_result.worker_count")
    if worker_count <= 0:
        raise TargetedHonorReleaseConfirmationPairedError(
            "worker_count must be positive"
        )
    payload = {key: raw[key] for key in raw if key != "result_identity"}
    if (
        expect_str(raw["result_identity"], "paired_result.result_identity")
        != _identity(payload)
    ):
        raise TargetedHonorReleaseConfirmationPairedError(
            "paired result identity mismatch"
        )
    return dict(raw)


def save_paired_result(document: dict[str, object], path: str | Path) -> Path:
    destination = Path(path)
    require_new_artifact_destinations(
        {"paired_result": destination}, required_names=("paired_result",)
    )
    write_new_artifact_file(destination, canonical_json_text(document))
    return destination


def load_paired_result(path: str | Path) -> dict[str, object]:
    try:
        return parse_paired_result(read_json_document(Path(path)))
    except ArtifactValidationError as exc:
        raise TargetedHonorReleaseConfirmationPairedError(str(exc)) from exc
    except (TypeError, ValueError, KeyError) as exc:
        raise TargetedHonorReleaseConfirmationPairedError(str(exc)) from exc


def verify_paired_result(
    path: str | Path,
    *,
    candidate_artifact_path: str | Path,
    parent_artifact_path: str | Path,
    candidate_trace_artifact_path: str | Path,
) -> dict[str, object]:
    document = load_paired_result(path)
    protocol = document["protocol"]
    assert isinstance(protocol, dict)
    seeds = tuple(protocol["ordered_seeds"])  # type: ignore[arg-type]
    candidate = load_arm_artifact(candidate_artifact_path)
    parent = load_arm_artifact(parent_artifact_path)
    arms = document["arms"]
    assert isinstance(arms, dict)
    for name, artifact, artifact_path in (
        ("candidate", candidate, candidate_artifact_path),
        ("parent", parent, parent_artifact_path),
    ):
        if arms[name] != _arm_document(artifact, artifact_path):
            raise TargetedHonorReleaseConfirmationPairedError(
                f"stored {name} arm differs from its artifact"
            )
    deltas = derive_paired_deltas(candidate, parent, seeds=seeds)
    summary = summarize_paired_deltas(deltas)
    if [item.to_document() for item in deltas] != document["paired_deltas"]:
        raise TargetedHonorReleaseConfirmationPairedError(
            "paired deltas differ from raw H/C artifacts"
        )
    if summary.to_document() != document["primary_summary"]:
        raise TargetedHonorReleaseConfirmationPairedError(
            "summary differs from raw H/C artifacts"
        )
    if classify(summary) != document["classification"]:
        raise TargetedHonorReleaseConfirmationPairedError(
            "classification differs from raw H/C artifacts"
        )
    trace = verify_trace_artifact(
        candidate_trace_artifact_path,
        candidate_artifact_path=candidate_artifact_path,
    )
    if document["candidate_trace"] != _trace_reference(
        trace, candidate_trace_artifact_path
    ):
        raise TargetedHonorReleaseConfirmationPairedError(
            "stored H trace reference differs from trace artifact"
        )
    if document["provenance"] != execution_provenance_to_dict(candidate.provenance):
        raise TargetedHonorReleaseConfirmationPairedError(
            "paired result provenance differs from arm provenance"
        )
    return document


def build_classified_result(
    *, paired_result: dict[str, object], paired_result_path: str | Path
) -> dict[str, object]:
    payload: dict[str, object] = {
        "classification": paired_result["classification"],
        "paired_result_digest": artifact_file_digest(paired_result_path),
        "paired_result_identity": paired_result["result_identity"],
        "result_version": CLASSIFIED_RESULT_VERSION,
    }
    document = dict(payload)
    document["classified_identity"] = _identity(payload)
    return document


def save_classified_result(document: dict[str, object], path: str | Path) -> Path:
    destination = Path(path)
    require_new_artifact_destinations(
        {"classified_result": destination}, required_names=("classified_result",)
    )
    write_new_artifact_file(destination, canonical_json_text(document))
    return destination


def verify_classified_result(
    path: str | Path, *, paired_result_path: str | Path
) -> dict[str, object]:
    raw = expect_object(
        read_json_document(Path(path)),
        {
            "classification",
            "classified_identity",
            "paired_result_digest",
            "paired_result_identity",
            "result_version",
        },
        "classified_result",
    )
    if (
        expect_int(raw["result_version"], "classified_result.result_version")
        != CLASSIFIED_RESULT_VERSION
    ):
        raise TargetedHonorReleaseConfirmationPairedError(
            "unsupported classified result version"
        )
    paired = load_paired_result(paired_result_path)
    if raw["classification"] != paired["classification"]:
        raise TargetedHonorReleaseConfirmationPairedError(
            "classified result label drifted"
        )
    if (
        expect_str(
            raw["paired_result_identity"],
            "classified_result.paired_result_identity",
        )
        != paired["result_identity"]
    ):
        raise TargetedHonorReleaseConfirmationPairedError(
            "paired result identity drifted"
        )
    if (
        expect_str(
            raw["paired_result_digest"], "classified_result.paired_result_digest"
        )
        != artifact_file_digest(paired_result_path)
    ):
        raise TargetedHonorReleaseConfirmationPairedError(
            "paired result digest drifted"
        )
    payload = {key: raw[key] for key in raw if key != "classified_identity"}
    if (
        expect_str(raw["classified_identity"], "classified_result.classified_identity")
        != _identity(payload)
    ):
        raise TargetedHonorReleaseConfirmationPairedError(
            "classified result identity mismatch"
        )
    return dict(raw)


__all__ = [
    "CLASSIFIED_RESULT_VERSION",
    "PAIRED_RESULT_VERSION",
    "TargetedHonorReleaseConfirmationPairedError",
    "build_classified_result",
    "build_paired_result",
    "classify",
    "derive_paired_deltas",
    "load_paired_result",
    "parse_paired_result",
    "save_classified_result",
    "save_paired_result",
    "verify_classified_result",
    "verify_paired_result",
]
