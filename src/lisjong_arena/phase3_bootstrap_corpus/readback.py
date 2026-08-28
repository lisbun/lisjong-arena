"""Strict validation-only reader for the Phase 3 bootstrap artifact。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from lisjong_arena.phase2_training_anchor.extraction import (
    FIRST_PARTY_SOURCE_CLASS,
    Phase2GameExtraction,
)
from lisjong_arena.phase2_training_anchor.pipeline_provenance import (
    TrainingPipelineProvenance,
)
from lisjong_arena.phase2_training_anchor.player_safe_anchor import AnchorSourceIdentity
from lisjong_arena.phase2_training_anchor.training_labels import (
    StructuralWaitUnavailableReason,
)
from lisjong_arena.phase3_bootstrap_corpus.decoding import (
    expect_int,
    expect_list,
    expect_object,
    expect_str,
    parse_provenance,
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
    Phase3BootstrapArtifactError,
    ValidatedBootstrapCorpus,
)
from lisjong_arena.phase3_bootstrap_corpus.sample_decoding import parse_sample

_STRUCTURAL_WAIT_REASONS = {reason.value for reason in StructuralWaitUnavailableReason}


def _validate_rule_json_value(value: object, context: str) -> None:
    if value is None or type(value) in (bool, int, str):
        return
    if type(value) is list:
        for index, item in enumerate(value):
            _validate_rule_json_value(item, f"{context}[{index}]")
        return
    raise Phase3BootstrapArtifactError(f"{context} has an unsupported JSON type")


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


def _validate_counts_shape(value: object, context: str) -> None:
    raw = expect_object(
        value,
        {
            "call_evidence_prefix_occurrences",
            "evidence_item_prefix_occurrences",
            "expected_count_sample_count",
            "hanchan_count",
            "kan_evidence_prefix_occurrences",
            "non_action_response_evidence_prefix_occurrences",
            "response_epoch_evidence_prefix_occurrences",
            "riichi_evidence_prefix_occurrences",
            "sample_count",
            "samples_per_hanchan",
            "structural_wait_available_count",
            "structural_wait_unavailable_count",
            "structural_wait_unavailable_reasons",
            "total_decisions",
        },
        context,
    )
    integer_names = set(raw) - {
        "samples_per_hanchan",
        "structural_wait_unavailable_reasons",
    }
    for name in integer_names:
        expect_int(raw[name], f"{context}.{name}")
    if type(raw["samples_per_hanchan"]) is not float:
        raise Phase3BootstrapArtifactError(
            f"{context}.samples_per_hanchan must be a JSON float"
        )
    reasons = raw["structural_wait_unavailable_reasons"]
    if type(reasons) is not dict:
        raise Phase3BootstrapArtifactError(
            f"{context}.structural_wait_unavailable_reasons must be an object"
        )
    for reason, count in reasons.items():
        if reason not in _STRUCTURAL_WAIT_REASONS:
            raise Phase3BootstrapArtifactError(
                f"{context} has an unsupported wait reason"
            )
        expect_int(count, f"{context}.structural_wait_unavailable_reasons.{reason}")


@dataclass(frozen=True, slots=True)
class ParsedArtifact:
    extractions: tuple[Phase2GameExtraction, ...]
    provenance: TrainingPipelineProvenance
    normalized_effective_rules: str


def parse_artifact_value(value: object) -> ParsedArtifact:
    raw = expect_object(
        value,
        {
            "counts",
            "effective_rules",
            "games",
            "generation_protocol",
            "generation_spec",
            "provenance",
            "schema_version",
        },
        "artifact",
    )
    if expect_int(raw["schema_version"], "schema_version") != SCHEMA_VERSION:
        raise Phase3BootstrapArtifactError("unsupported schema version")
    if (
        expect_str(raw["generation_protocol"], "generation_protocol")
        != GENERATION_PROTOCOL
    ):
        raise Phase3BootstrapArtifactError("unsupported generation protocol")
    spec = expect_object(
        raw["generation_spec"],
        {
            "anchor",
            "execution",
            "policies",
            "rules",
            "sample_contract",
            "seeds",
            "source_class",
        },
        "generation_spec",
    )
    fixed_scalars = {
        "anchor": FIXED_ANCHOR,
        "execution": FIXED_EXECUTION,
        "rules": FIXED_RULES,
        "sample_contract": FIXED_SAMPLE_CONTRACT,
        "source_class": FIRST_PARTY_SOURCE_CLASS,
    }
    for name, expected in fixed_scalars.items():
        if expect_str(spec[name], f"generation_spec.{name}") != expected:
            raise Phase3BootstrapArtifactError(
                f"generation_spec.{name} is not the fixed protocol"
            )
    policies = expect_object(
        spec["policies"], {"identity", "seat_count"}, "generation_spec.policies"
    )
    if (
        expect_str(policies["identity"], "generation_spec.policies.identity")
        != FIXED_POLICY
    ):
        raise Phase3BootstrapArtifactError("generation_spec policy identity is invalid")
    if (
        expect_int(policies["seat_count"], "generation_spec.policies.seat_count")
        != FIXED_POLICY_SEAT_COUNT
    ):
        raise Phase3BootstrapArtifactError(
            "generation_spec policy seat count is invalid"
        )
    seeds = tuple(
        expect_int(item, f"generation_spec.seeds[{index}]")
        for index, item in enumerate(
            expect_list(spec["seeds"], "generation_spec.seeds")
        )
    )
    if seeds != FIXED_SEEDS:
        raise Phase3BootstrapArtifactError("generation_spec seeds must be 1000..1007")

    provenance = parse_provenance(raw["provenance"])
    _validate_normalized_rules_value(raw["effective_rules"])
    normalized_rules = json.dumps(
        raw["effective_rules"],
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    _validate_counts_shape(raw["counts"], "counts")

    games_raw = expect_list(raw["games"], "games")
    if len(games_raw) != len(FIXED_SEEDS):
        raise Phase3BootstrapArtifactError("artifact must contain exactly eight games")
    extractions: list[Phase2GameExtraction] = []
    for game_index, (game_value, expected_seed) in enumerate(
        zip(games_raw, FIXED_SEEDS, strict=True)
    ):
        context = f"games[{game_index}]"
        game = expect_object(
            game_value,
            {
                "counts",
                "sample_count",
                "samples",
                "seed",
                "total_decisions",
                "turn_anchors",
            },
            context,
        )
        seed = expect_int(game["seed"], f"{context}.seed")
        if seed != expected_seed:
            raise Phase3BootstrapArtifactError("game seed/order is invalid")
        _validate_counts_shape(game["counts"], f"{context}.counts")
        samples = tuple(
            parse_sample(sample, f"{context}.samples[{sample_index}]", provenance)
            for sample_index, sample in enumerate(
                expect_list(game["samples"], f"{context}.samples")
            )
        )
        sample_count = expect_int(game["sample_count"], f"{context}.sample_count")
        turn_anchors = expect_int(game["turn_anchors"], f"{context}.turn_anchors")
        if sample_count != len(samples) or turn_anchors != len(samples):
            raise Phase3BootstrapArtifactError(
                f"{context} sample counts are inconsistent"
            )
        source = AnchorSourceIdentity(
            source_class=FIRST_PARTY_SOURCE_CLASS,
            game_seed=seed,
        )
        try:
            extraction = Phase2GameExtraction(
                source=source,
                total_decisions=expect_int(
                    game["total_decisions"], f"{context}.total_decisions"
                ),
                turn_anchors=turn_anchors,
                samples=samples,
            )
        except (TypeError, ValueError) as exc:
            raise Phase3BootstrapArtifactError(f"{context} is invalid") from exc
        if any(sample.anchor.source != source for sample in samples):
            raise Phase3BootstrapArtifactError(f"{context} sample source mismatch")
        extractions.append(extraction)

    return ParsedArtifact(
        extractions=tuple(extractions),
        provenance=provenance,
        normalized_effective_rules=normalized_rules,
    )


def _reject_json_constant(value: str) -> None:
    raise Phase3BootstrapArtifactError(
        f"non-finite JSON number is not allowed: {value}"
    )


def _reject_duplicate_object_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise Phase3BootstrapArtifactError(f"duplicate JSON object key: {key!r}")
        result[key] = value
    return result


def load_phase3_bootstrap_corpus(path: str | Path) -> ValidatedBootstrapCorpus:
    """source-specific JSONをstrict parseし、validation-only summaryを返す。"""
    from lisjong_arena.phase3_bootstrap_corpus.artifact import (
        _freeze_counts_from_extractions,
        _json_bytes,
        build_phase3_bootstrap_value,
        canonical_sha256,
    )

    source = Path(path)
    try:
        serialized = source.read_bytes()
        text = serialized.decode("utf-8")
    except UnicodeError as exc:
        raise Phase3BootstrapArtifactError("artifact is not valid UTF-8") from exc
    try:
        value = json.loads(
            text,
            parse_constant=_reject_json_constant,
            object_pairs_hook=_reject_duplicate_object_keys,
        )
        parsed = parse_artifact_value(value)
        rebuilt = build_phase3_bootstrap_value(
            parsed.extractions,
            parsed.provenance,
            parsed.normalized_effective_rules,
        )
        if rebuilt != value:
            raise Phase3BootstrapArtifactError("artifact content is inconsistent")
        canonical = _json_bytes(rebuilt)
        if serialized != canonical:
            raise Phase3BootstrapArtifactError(
                "artifact bytes are not canonical JSON"
            )
        counts = _freeze_counts_from_extractions(parsed.extractions)
    except Phase3BootstrapArtifactError:
        raise
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise Phase3BootstrapArtifactError(
            "artifact is malformed or inconsistent"
        ) from exc
    return ValidatedBootstrapCorpus(
        schema_version=SCHEMA_VERSION,
        generation_protocol=GENERATION_PROTOCOL,
        game_seeds=FIXED_SEEDS,
        provenance=parsed.provenance,
        counts=counts,
        canonical_sha256=canonical_sha256(canonical),
        artifact_bytes=len(canonical),
    )
