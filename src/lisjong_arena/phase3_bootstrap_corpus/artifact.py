"""Phase 3 fixed bootstrap corpus construction and persistence。"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lisjong_engine.public_state import PublicMeldType
from lisjong_engine.round_evidence import (
    KanConfirmedEvidence,
    KanDeclaredEvidence,
    MeldCalledEvidence,
    ResponseEpochClosedEvidence,
    ResponseEpochOpenedEvidence,
    ResponseOutcome,
    RiichiDeclaredEvidence,
    RiichiEstablishedEvidence,
    RiichiFailedEvidence,
)
from lisjong_engine.rules import RuleSet

from lisjong_arena.phase2_training_anchor.extraction import (
    FIRST_PARTY_SOURCE_CLASS,
    Phase2GameExtraction,
)
from lisjong_arena.phase2_training_anchor.pipeline_provenance import (
    ANCHOR_SEMANTICS_ID,
    EVIDENCE_CUTOFF_SEMANTICS_ID,
    LABEL_SEMANTICS_ID,
    TrainingPipelineProvenance,
)
from lisjong_arena.phase2_training_anchor.rule_provenance import (
    normalize_effective_rules,
)
from lisjong_arena.phase2_training_anchor.training_sample import TrainingSample
from lisjong_arena.phase3_bootstrap_corpus.encoding import (
    provenance_to_dict,
    sample_to_dict,
)
from lisjong_arena.phase3_bootstrap_corpus.model import (
    FIXED_ANCHOR,
    FIXED_EXECUTION,
    FIXED_POLICY,
    FIXED_POLICY_SEAT_COUNT,
    FIXED_RULES,
    FIXED_SAMPLE_CONTRACT,
    FIXED_SEEDS,
    GENERATION_PROTOCOL,
    SCHEMA_VERSION,
    CorpusCounts,
    Phase3BootstrapArtifactError,
    ValidatedBootstrapCorpus,
)

_EXPECTED_EFFECTIVE_RULES_NORMALIZED = normalize_effective_rules(RuleSet.default())
_FULL_COMMIT_ID = re.compile(r"[0-9a-f]{40}").fullmatch


@dataclass(slots=True)
class _MutableCounts:
    total_decisions: int = 0
    sample_count: int = 0
    expected_count_sample_count: int = 0
    structural_wait_available_count: int = 0
    structural_wait_unavailable_count: int = 0
    evidence_item_prefix_occurrences: int = 0
    riichi_evidence_prefix_occurrences: int = 0
    call_evidence_prefix_occurrences: int = 0
    kan_evidence_prefix_occurrences: int = 0
    response_epoch_evidence_prefix_occurrences: int = 0
    non_action_response_evidence_prefix_occurrences: int = 0
    reason_counts: dict[str, int] = field(default_factory=dict)


def _freeze_counts(counts: _MutableCounts, hanchan_count: int) -> CorpusCounts:
    return CorpusCounts(
        hanchan_count=hanchan_count,
        total_decisions=counts.total_decisions,
        sample_count=counts.sample_count,
        samples_per_hanchan=counts.sample_count / hanchan_count,
        expected_count_sample_count=counts.expected_count_sample_count,
        structural_wait_available_count=counts.structural_wait_available_count,
        structural_wait_unavailable_count=counts.structural_wait_unavailable_count,
        structural_wait_unavailable_reasons=tuple(sorted(counts.reason_counts.items())),
        evidence_item_prefix_occurrences=counts.evidence_item_prefix_occurrences,
        riichi_evidence_prefix_occurrences=counts.riichi_evidence_prefix_occurrences,
        call_evidence_prefix_occurrences=counts.call_evidence_prefix_occurrences,
        kan_evidence_prefix_occurrences=counts.kan_evidence_prefix_occurrences,
        response_epoch_evidence_prefix_occurrences=(
            counts.response_epoch_evidence_prefix_occurrences
        ),
        non_action_response_evidence_prefix_occurrences=(
            counts.non_action_response_evidence_prefix_occurrences
        ),
    )


def _counts_to_dict(counts: CorpusCounts) -> dict[str, Any]:
    return {
        "call_evidence_prefix_occurrences": counts.call_evidence_prefix_occurrences,
        "evidence_item_prefix_occurrences": counts.evidence_item_prefix_occurrences,
        "expected_count_sample_count": counts.expected_count_sample_count,
        "hanchan_count": counts.hanchan_count,
        "kan_evidence_prefix_occurrences": counts.kan_evidence_prefix_occurrences,
        "non_action_response_evidence_prefix_occurrences": (
            counts.non_action_response_evidence_prefix_occurrences
        ),
        "response_epoch_evidence_prefix_occurrences": (
            counts.response_epoch_evidence_prefix_occurrences
        ),
        "riichi_evidence_prefix_occurrences": counts.riichi_evidence_prefix_occurrences,
        "sample_count": counts.sample_count,
        "samples_per_hanchan": counts.samples_per_hanchan,
        "structural_wait_available_count": counts.structural_wait_available_count,
        "structural_wait_unavailable_count": counts.structural_wait_unavailable_count,
        "structural_wait_unavailable_reasons": dict(
            counts.structural_wait_unavailable_reasons
        ),
        "total_decisions": counts.total_decisions,
    }


def _accumulate_sample_counts(counts: _MutableCounts, sample: TrainingSample) -> None:
    counts.sample_count += 1
    counts.expected_count_sample_count += 1
    for wait in sample.labels.structural_waits:
        if wait.mask is None:
            counts.structural_wait_unavailable_count += 1
            reason = wait.unavailable_reason.value
            counts.reason_counts[reason] = counts.reason_counts.get(reason, 0) + 1
        else:
            counts.structural_wait_available_count += 1
    for evidence in sample.anchor.evidence:
        counts.evidence_item_prefix_occurrences += 1
        if isinstance(
            evidence,
            (RiichiDeclaredEvidence, RiichiEstablishedEvidence, RiichiFailedEvidence),
        ):
            counts.riichi_evidence_prefix_occurrences += 1
        if isinstance(evidence, MeldCalledEvidence):
            counts.call_evidence_prefix_occurrences += 1
            if evidence.meld.meld_type is PublicMeldType.DAIMINKAN:
                counts.kan_evidence_prefix_occurrences += 1
        if isinstance(evidence, (KanDeclaredEvidence, KanConfirmedEvidence)):
            counts.kan_evidence_prefix_occurrences += 1
        if isinstance(
            evidence, (ResponseEpochOpenedEvidence, ResponseEpochClosedEvidence)
        ):
            counts.response_epoch_evidence_prefix_occurrences += 1
        if (
            isinstance(evidence, ResponseEpochClosedEvidence)
            and evidence.outcome is ResponseOutcome.NO_PUBLIC_RESPONSE
        ):
            counts.non_action_response_evidence_prefix_occurrences += 1


def _validate_fixed_provenance(provenance: TrainingPipelineProvenance) -> None:
    if not isinstance(provenance, TrainingPipelineProvenance):
        raise TypeError("provenance must be a TrainingPipelineProvenance")
    if not provenance.source_revisions.fully_resolved:
        raise Phase3BootstrapArtifactError(
            "persistent generation requires fully resolved source revisions"
        )
    for name in ("lisjong", "lisjong_engine", "lisjong_arena"):
        revision = getattr(provenance.source_revisions, name)
        if type(revision) is not str or _FULL_COMMIT_ID(revision) is None:
            raise Phase3BootstrapArtifactError(
                f"{name} revision must be a lowercase full commit ID"
            )
    if provenance.anchor_semantics_id != ANCHOR_SEMANTICS_ID:
        raise Phase3BootstrapArtifactError("unexpected anchor semantics identity")
    if provenance.evidence_cutoff_semantics_id != EVIDENCE_CUTOFF_SEMANTICS_ID:
        raise Phase3BootstrapArtifactError(
            "unexpected evidence cutoff semantics identity"
        )
    if provenance.label_semantics_id != LABEL_SEMANTICS_ID:
        raise Phase3BootstrapArtifactError("unexpected label semantics identity")


def _validate_normalized_rules_value(value: object) -> None:
    if type(value) is not dict or not value:
        raise Phase3BootstrapArtifactError("effective_rules must be a non-empty object")
    for key, item in value.items():
        if type(key) is not str or not key:
            raise Phase3BootstrapArtifactError("effective rule field name is invalid")
        _validate_rule_json_value(item, f"effective_rules.{key}")
    if type(value.get("name")) is not str or not value["name"]:
        raise Phase3BootstrapArtifactError("effective_rules.name is invalid")
    if type(value.get("version")) is not int:
        raise Phase3BootstrapArtifactError("effective_rules.version is invalid")


def _validate_rule_json_value(value: object, context: str) -> None:
    if value is None or type(value) in (bool, int, str):
        return
    if type(value) is list:
        for index, item in enumerate(value):
            _validate_rule_json_value(item, f"{context}[{index}]")
        return
    raise Phase3BootstrapArtifactError(f"{context} has an unsupported JSON type")


def build_phase3_bootstrap_value(
    extractions: tuple[Phase2GameExtraction, ...],
    provenance: TrainingPipelineProvenance,
    normalized_effective_rules: str,
) -> dict[str, Any]:
    """固定Phase 3 extraction群からcanonical JSON valueを構成する。"""
    _validate_fixed_provenance(provenance)
    if type(extractions) is not tuple:
        raise TypeError("extractions must be a tuple")
    if len(extractions) != len(FIXED_SEEDS):
        raise Phase3BootstrapArtifactError(
            "exactly eight fixed-seed games are required"
        )
    if tuple(game.source.game_seed for game in extractions) != FIXED_SEEDS:
        raise Phase3BootstrapArtifactError("games must be ordered by seeds 1000..1007")

    try:
        effective_rules_value = json.loads(normalized_effective_rules)
    except (TypeError, json.JSONDecodeError) as exc:
        raise Phase3BootstrapArtifactError(
            "normalized effective rules are invalid"
        ) from exc
    _validate_normalized_rules_value(effective_rules_value)
    renormalized = json.dumps(
        effective_rules_value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    if renormalized != normalized_effective_rules:
        raise Phase3BootstrapArtifactError(
            "effective rules are not canonically normalized"
        )
    if renormalized != _EXPECTED_EFFECTIVE_RULES_NORMALIZED:
        raise Phase3BootstrapArtifactError(
            "effective rules must be the fixed RuleSet.default() configuration"
        )
    fingerprint = hashlib.sha256(renormalized.encode("utf-8")).hexdigest()
    if provenance.effective_rules.fingerprint != fingerprint:
        raise Phase3BootstrapArtifactError("effective rules fingerprint mismatch")
    if effective_rules_value.get("name") != provenance.effective_rules.name:
        raise Phase3BootstrapArtifactError("effective rules name mismatch")
    if effective_rules_value.get("version") != provenance.effective_rules.version:
        raise Phase3BootstrapArtifactError("effective rules version mismatch")

    corpus_counts = _MutableCounts()
    games: list[dict[str, Any]] = []
    for expected_seed, game in zip(FIXED_SEEDS, extractions, strict=True):
        if not isinstance(game, Phase2GameExtraction):
            raise TypeError("extractions must contain Phase2GameExtraction values")
        if game.source.source_class != FIRST_PARTY_SOURCE_CLASS:
            raise Phase3BootstrapArtifactError("unexpected source class")
        if game.source.game_seed != expected_seed:
            raise Phase3BootstrapArtifactError("game seed/order mismatch")
        if game.turn_anchors != len(game.samples):
            raise Phase3BootstrapArtifactError("TURN anchor/sample count mismatch")
        if game.total_decisions < game.turn_anchors:
            raise Phase3BootstrapArtifactError(
                "total decisions below TURN anchor count"
            )
        if not game.samples:
            raise Phase3BootstrapArtifactError("fixed hanchan contains no TURN samples")
        if tuple(sample.anchor.anchor_index for sample in game.samples) != tuple(
            range(len(game.samples))
        ):
            raise Phase3BootstrapArtifactError("samples must be in anchor_index order")

        game_counts = _MutableCounts(total_decisions=game.total_decisions)
        corpus_counts.total_decisions += game.total_decisions
        serialized_samples: list[dict[str, Any]] = []
        for sample in game.samples:
            if sample.provenance != provenance:
                raise Phase3BootstrapArtifactError(
                    "all samples must share one common pipeline provenance"
                )
            if sample.anchor.rule_provenance != provenance.effective_rules:
                raise Phase3BootstrapArtifactError("sample rule provenance mismatch")
            if sample.anchor.source != game.source:
                raise Phase3BootstrapArtifactError("sample source does not match game")
            if sample.anchor.hand_number != sample.anchor.observation.hand_number:
                raise Phase3BootstrapArtifactError("anchor hand_number mismatch")
            if sample.anchor.honba != sample.anchor.observation.honba:
                raise Phase3BootstrapArtifactError("anchor honba mismatch")
            _accumulate_sample_counts(game_counts, sample)
            _accumulate_sample_counts(corpus_counts, sample)
            serialized_samples.append(sample_to_dict(sample))
        games.append(
            {
                "counts": _counts_to_dict(_freeze_counts(game_counts, 1)),
                "sample_count": len(game.samples),
                "samples": serialized_samples,
                "seed": expected_seed,
                "total_decisions": game.total_decisions,
                "turn_anchors": game.turn_anchors,
            }
        )

    counts = _freeze_counts(corpus_counts, len(extractions))
    return {
        "counts": _counts_to_dict(counts),
        "effective_rules": effective_rules_value,
        "games": games,
        "generation_protocol": GENERATION_PROTOCOL,
        "generation_spec": {
            "anchor": FIXED_ANCHOR,
            "execution": FIXED_EXECUTION,
            "policies": {
                "identity": FIXED_POLICY,
                "seat_count": FIXED_POLICY_SEAT_COUNT,
            },
            "rules": FIXED_RULES,
            "sample_contract": FIXED_SAMPLE_CONTRACT,
            "seeds": list(FIXED_SEEDS),
            "source_class": FIRST_PARTY_SOURCE_CLASS,
        },
        "provenance": provenance_to_dict(provenance),
        "schema_version": SCHEMA_VERSION,
    }


def canonical_phase3_bootstrap_bytes(value: object) -> bytes:
    """strict semantic validation後にcanonical UTF-8 JSON bytesを返す。"""
    from lisjong_arena.phase3_bootstrap_corpus.readback import parse_artifact_value

    try:
        parsed = parse_artifact_value(value)
    except Phase3BootstrapArtifactError:
        raise
    except (TypeError, ValueError) as exc:
        raise Phase3BootstrapArtifactError(
            "artifact is malformed or inconsistent"
        ) from exc
    rebuilt = build_phase3_bootstrap_value(
        parsed.extractions,
        parsed.provenance,
        parsed.normalized_effective_rules,
    )
    if rebuilt != value:
        raise Phase3BootstrapArtifactError(
            "artifact value is non-canonical or inconsistent"
        )
    return _json_bytes(rebuilt)


def _json_bytes(value: object) -> bytes:
    try:
        return (
            json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise Phase3BootstrapArtifactError("artifact is not JSON serializable") from exc


def canonical_sha256(canonical_bytes: bytes) -> str:
    if type(canonical_bytes) is not bytes:
        raise TypeError("canonical_bytes must be bytes")
    return hashlib.sha256(canonical_bytes).hexdigest()


def save_phase3_bootstrap_corpus(
    value: object, path: str | Path
) -> ValidatedBootstrapCorpus:
    """validation完了後だけexclusive writeし、strict readback成功後だけ返す。"""
    canonical = canonical_phase3_bootstrap_bytes(value)
    destination = Path(path)
    created = False
    try:
        with destination.open("xb") as stream:
            created = True
            stream.write(canonical)
        readback = load_phase3_bootstrap_corpus(destination)
        if readback.canonical_sha256 != canonical_sha256(canonical):
            raise Phase3BootstrapArtifactError("readback digest mismatch")
        return readback
    except Exception:
        if created:
            try:
                destination.unlink()
            except OSError:
                pass
        raise


def load_phase3_bootstrap_corpus(path: str | Path) -> ValidatedBootstrapCorpus:
    """strict reader implementationへのvalidation-only public entry point。"""
    from lisjong_arena.phase3_bootstrap_corpus.readback import (
        load_phase3_bootstrap_corpus as _load,
    )

    return _load(path)


def _freeze_counts_from_extractions(
    extractions: tuple[Phase2GameExtraction, ...],
) -> CorpusCounts:
    counts = _MutableCounts()
    for game in extractions:
        counts.total_decisions += game.total_decisions
        for sample in game.samples:
            _accumulate_sample_counts(counts, sample)
    return _freeze_counts(counts, len(extractions))


__all__ = [
    "FIXED_ANCHOR",
    "FIXED_EXECUTION",
    "FIXED_POLICY",
    "FIXED_POLICY_SEAT_COUNT",
    "FIXED_RULES",
    "FIXED_SAMPLE_CONTRACT",
    "FIXED_SEEDS",
    "GENERATION_PROTOCOL",
    "SCHEMA_VERSION",
    "CorpusCounts",
    "Phase3BootstrapArtifactError",
    "ValidatedBootstrapCorpus",
    "build_phase3_bootstrap_value",
    "canonical_phase3_bootstrap_bytes",
    "canonical_sha256",
    "load_phase3_bootstrap_corpus",
    "save_phase3_bootstrap_corpus",
]
