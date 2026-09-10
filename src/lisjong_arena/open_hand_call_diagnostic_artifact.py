"""Issue #196 decision-divergence diagnostic sidecar artifact。

既存ABBB strength artifact schemaは変更せず、diagnosticsをpurpose-specificな
versioned JSONへ保存する。sidecarはstrength artifactのSHA-256、plan、execution
provenanceへbindし、strict readback時にraw strength resultsとの整合も検証する。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lisjong.policy_contract import Seat

from lisjong_arena._artifact_io import (
    ArtifactValidationError,
    canonical_json_text,
    expect_float,
    expect_int,
    expect_list,
    expect_object,
    expect_optional_float,
    expect_str,
    read_json_document,
    write_new_artifact_file,
)
from lisjong_arena.open_hand_call_diagnostics import (
    OPEN_HAND_DIAGNOSTIC_BASELINE_IDENTITY,
    OPEN_HAND_DIAGNOSTIC_CANDIDATE_IDENTITY,
    OPEN_HAND_DIAGNOSTIC_LISJONG_REVISION,
    OpenHandDiagnosticEvaluationResult,
    OpenHandDiagnosticSummary,
    OpenHandGameDiagnostics,
    aggregate_open_hand_diagnostics,
)
from lisjong_arena.single_round_artifact import (
    SINGLE_ROUND_ARTIFACT_SCHEMA_VERSION,
    SINGLE_ROUND_EVALUATION_PROTOCOL,
    SingleRoundArtifactPlan,
    SingleRoundExecutionProvenance,
    SingleRoundStrengthArtifact,
    execution_provenance_to_dict,
    load_single_round_artifact,
    parse_execution_provenance,
)

OPEN_HAND_DIAGNOSTIC_SCHEMA_VERSION = 1
OPEN_HAND_DIAGNOSTIC_PROTOCOL = "open-hand-call-divergence-v1"
OPEN_HAND_DIAGNOSTIC_ARTIFACT_KIND = "open-hand-call-divergence-diagnostics"

_SHA256 = re.compile(r"[0-9a-f]{64}").fullmatch


class OpenHandDiagnosticArtifactError(ArtifactValidationError):
    """diagnostic sidecarを生成またはstrict検証できない場合。"""


@dataclass(frozen=True, slots=True)
class OpenHandCallDiagnosticArtifact:
    schema_version: int
    diagnostic_protocol: str
    strength_artifact_sha256: str
    plan: SingleRoundArtifactPlan
    provenance: SingleRoundExecutionProvenance
    game_diagnostics: tuple[OpenHandGameDiagnostics, ...]
    summary: OpenHandDiagnosticSummary

    def __post_init__(self) -> None:
        if self.schema_version != OPEN_HAND_DIAGNOSTIC_SCHEMA_VERSION:
            raise ValueError("unsupported diagnostic schema version")
        if self.diagnostic_protocol != OPEN_HAND_DIAGNOSTIC_PROTOCOL:
            raise ValueError("unsupported diagnostic protocol")
        if (
            type(self.strength_artifact_sha256) is not str
            or _SHA256(self.strength_artifact_sha256) is None
        ):
            raise ValueError("strength_artifact_sha256 must be lowercase SHA-256")
        if not isinstance(self.plan, SingleRoundArtifactPlan):
            raise TypeError("plan must be a SingleRoundArtifactPlan")
        if self.plan.candidate_identity != OPEN_HAND_DIAGNOSTIC_CANDIDATE_IDENTITY:
            raise ValueError("diagnostic candidate identity is invalid")
        if self.plan.baseline_identity != OPEN_HAND_DIAGNOSTIC_BASELINE_IDENTITY:
            raise ValueError("diagnostic baseline identity is invalid")
        if not isinstance(self.provenance, SingleRoundExecutionProvenance):
            raise TypeError("provenance must be a SingleRoundExecutionProvenance")
        if self.provenance.lisjong_revision != OPEN_HAND_DIAGNOSTIC_LISJONG_REVISION:
            raise ValueError("diagnostic lisjong revision is not the locked revision")
        games = tuple(self.game_diagnostics)
        if any(not isinstance(game, OpenHandGameDiagnostics) for game in games):
            raise TypeError(
                "game_diagnostics must contain only OpenHandGameDiagnostics"
            )
        object.__setattr__(self, "game_diagnostics", games)
        expected_keys = tuple(
            (seed, rotation)
            for seed in self.plan.seeds
            for rotation in range(self.plan.rotation_count)
        )
        actual_keys = tuple((game.seed, game.rotation) for game in games)
        if actual_keys != expected_keys:
            raise ValueError(
                "game_diagnostics must follow plan seed order and rotation order"
            )
        if self.summary != aggregate_open_hand_diagnostics(games):
            raise ValueError("summary must match canonical diagnostic aggregation")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _plan_to_dict(plan: SingleRoundArtifactPlan) -> dict[str, Any]:
    return {
        "baseline_identity": plan.baseline_identity,
        "candidate_identity": plan.candidate_identity,
        "game_mode": plan.game_mode,
        "max_steps": plan.max_steps,
        "rotation_count": plan.rotation_count,
        "seeds": list(plan.seeds),
    }


def _game_to_dict(game: OpenHandGameDiagnostics) -> dict[str, Any]:
    return {
        "baseline_pass_to_candidate_chi": game.baseline_pass_to_candidate_chi,
        "baseline_pass_to_candidate_pon": game.baseline_pass_to_candidate_pon,
        "candidate_seat": int(game.candidate_seat),
        "divergent_action_decisions": game.divergent_action_decisions,
        "shared_prefix_initial_call_opportunities": (
            game.shared_prefix_initial_call_opportunities
        ),
        "rotation": game.rotation,
        "same_action_decisions": game.same_action_decisions,
        "scaled_candidate_score_delta": game.scaled_candidate_score_delta,
        "seed": game.seed,
        "total_candidate_seat_decisions": game.total_candidate_seat_decisions,
    }


def _summary_to_dict(summary: OpenHandDiagnosticSummary) -> dict[str, Any]:
    return {
        "action_divergence_rate": summary.action_divergence_rate,
        "baseline_pass_to_candidate_chi": summary.baseline_pass_to_candidate_chi,
        "baseline_pass_to_candidate_pon": summary.baseline_pass_to_candidate_pon,
        "candidate_only_chi_count": summary.candidate_only_chi_count,
        "candidate_only_pon_count": summary.candidate_only_pon_count,
        "divergent_action_decisions": summary.divergent_action_decisions,
        "divergent_decisions_by_candidate_seat": list(
            summary.divergent_decisions_by_candidate_seat
        ),
        "divergent_game_count": summary.divergent_game_count,
        "divergent_negative_score_delta_seed_blocks": (
            summary.divergent_negative_score_delta_seed_blocks
        ),
        "divergent_positive_score_delta_seed_blocks": (
            summary.divergent_positive_score_delta_seed_blocks
        ),
        "divergent_seed_block_count": summary.divergent_seed_block_count,
        "divergent_zero_score_delta_seed_blocks": (
            summary.divergent_zero_score_delta_seed_blocks
        ),
        "game_count": summary.game_count,
        "mean_divergences_per_divergent_game": (
            summary.mean_divergences_per_divergent_game
        ),
        "nonzero_score_delta_seed_block_count": (
            summary.nonzero_score_delta_seed_block_count
        ),
        "shared_prefix_initial_call_opportunities": (
            summary.shared_prefix_initial_call_opportunities
        ),
        "same_action_decisions": summary.same_action_decisions,
        "seed_block_count": summary.seed_block_count,
        "total_candidate_seat_decisions": (summary.total_candidate_seat_decisions),
    }


def _artifact_to_dict(artifact: OpenHandCallDiagnosticArtifact) -> dict[str, Any]:
    return {
        "artifact_kind": OPEN_HAND_DIAGNOSTIC_ARTIFACT_KIND,
        "diagnostic_protocol": artifact.diagnostic_protocol,
        "game_diagnostics": [_game_to_dict(game) for game in artifact.game_diagnostics],
        "plan": _plan_to_dict(artifact.plan),
        "provenance": execution_provenance_to_dict(artifact.provenance),
        "schema_version": artifact.schema_version,
        "strength_artifact": {
            "evaluation_protocol": SINGLE_ROUND_EVALUATION_PROTOCOL,
            "schema_version": SINGLE_ROUND_ARTIFACT_SCHEMA_VERSION,
            "sha256": artifact.strength_artifact_sha256,
        },
        "summary": _summary_to_dict(artifact.summary),
    }


def _validate_strength_binding(
    artifact: OpenHandCallDiagnosticArtifact,
    strength: SingleRoundStrengthArtifact,
) -> None:
    if artifact.plan != strength.plan:
        raise OpenHandDiagnosticArtifactError(
            "diagnostic plan does not match strength artifact"
        )
    if artifact.provenance != strength.provenance:
        raise OpenHandDiagnosticArtifactError(
            "diagnostic provenance does not match strength artifact"
        )
    if len(artifact.game_diagnostics) != len(strength.game_results):
        raise OpenHandDiagnosticArtifactError(
            "diagnostic game count does not match strength artifact"
        )
    for diagnostic, game_result in zip(
        artifact.game_diagnostics, strength.game_results, strict=True
    ):
        expected_delta = 3 * game_result.candidate_score - sum(
            score
            for seat, score in enumerate(game_result.scores)
            if seat != game_result.candidate_seat
        )
        if (
            diagnostic.seed,
            diagnostic.rotation,
            diagnostic.candidate_seat,
            diagnostic.scaled_candidate_score_delta,
        ) != (
            game_result.seed,
            game_result.rotation,
            game_result.candidate_seat,
            expected_delta,
        ):
            raise OpenHandDiagnosticArtifactError(
                "diagnostic game record does not match strength artifact"
            )


def save_open_hand_diagnostic_artifact(
    result: OpenHandDiagnosticEvaluationResult,
    *,
    strength_artifact_path: str | Path,
    path: str | Path,
) -> None:
    """diagnosticsを既存strength artifactへhash/provenance bindして保存する。"""
    if not isinstance(result, OpenHandDiagnosticEvaluationResult):
        raise TypeError("result must be an OpenHandDiagnosticEvaluationResult")
    strength_path = Path(strength_artifact_path)
    strength = load_single_round_artifact(strength_path)
    evaluation = result.evaluation_result
    if strength.game_results != evaluation.game_results:
        raise OpenHandDiagnosticArtifactError(
            "strength artifact game_results do not match diagnostic evaluation"
        )
    plan = strength.plan
    artifact = OpenHandCallDiagnosticArtifact(
        schema_version=OPEN_HAND_DIAGNOSTIC_SCHEMA_VERSION,
        diagnostic_protocol=OPEN_HAND_DIAGNOSTIC_PROTOCOL,
        strength_artifact_sha256=_sha256(strength_path),
        plan=plan,
        provenance=strength.provenance,
        game_diagnostics=result.game_diagnostics,
        summary=result.summary,
    )
    _validate_strength_binding(artifact, strength)
    write_new_artifact_file(
        Path(path), canonical_json_text(_artifact_to_dict(artifact))
    )


def _parse_plan(value: object) -> SingleRoundArtifactPlan:
    raw = expect_object(
        value,
        {
            "baseline_identity",
            "candidate_identity",
            "game_mode",
            "max_steps",
            "rotation_count",
            "seeds",
        },
        "plan",
    )
    return SingleRoundArtifactPlan(
        candidate_identity=expect_str(
            raw["candidate_identity"], "plan.candidate_identity"
        ),
        baseline_identity=expect_str(
            raw["baseline_identity"], "plan.baseline_identity"
        ),
        seeds=tuple(
            expect_int(seed, f"plan.seeds[{index}]")
            for index, seed in enumerate(expect_list(raw["seeds"], "plan.seeds"))
        ),
        game_mode=expect_str(raw["game_mode"], "plan.game_mode"),
        rotation_count=expect_int(raw["rotation_count"], "plan.rotation_count"),
        max_steps=expect_int(raw["max_steps"], "plan.max_steps"),
    )


def _parse_game(value: object, index: int) -> OpenHandGameDiagnostics:
    context = f"game_diagnostics[{index}]"
    raw = expect_object(
        value,
        {
            "baseline_pass_to_candidate_chi",
            "baseline_pass_to_candidate_pon",
            "candidate_seat",
            "divergent_action_decisions",
            "shared_prefix_initial_call_opportunities",
            "rotation",
            "same_action_decisions",
            "scaled_candidate_score_delta",
            "seed",
            "total_candidate_seat_decisions",
        },
        context,
    )
    return OpenHandGameDiagnostics(
        seed=expect_int(raw["seed"], f"{context}.seed"),
        rotation=expect_int(raw["rotation"], f"{context}.rotation"),
        candidate_seat=Seat(
            expect_int(raw["candidate_seat"], f"{context}.candidate_seat")
        ),
        total_candidate_seat_decisions=expect_int(
            raw["total_candidate_seat_decisions"],
            f"{context}.total_candidate_seat_decisions",
        ),
        same_action_decisions=expect_int(
            raw["same_action_decisions"], f"{context}.same_action_decisions"
        ),
        divergent_action_decisions=expect_int(
            raw["divergent_action_decisions"],
            f"{context}.divergent_action_decisions",
        ),
        shared_prefix_initial_call_opportunities=expect_int(
            raw["shared_prefix_initial_call_opportunities"],
            f"{context}.shared_prefix_initial_call_opportunities",
        ),
        baseline_pass_to_candidate_chi=expect_int(
            raw["baseline_pass_to_candidate_chi"],
            f"{context}.baseline_pass_to_candidate_chi",
        ),
        baseline_pass_to_candidate_pon=expect_int(
            raw["baseline_pass_to_candidate_pon"],
            f"{context}.baseline_pass_to_candidate_pon",
        ),
        scaled_candidate_score_delta=expect_int(
            raw["scaled_candidate_score_delta"],
            f"{context}.scaled_candidate_score_delta",
        ),
    )


def _parse_summary(value: object) -> OpenHandDiagnosticSummary:
    keys = {
        "action_divergence_rate",
        "baseline_pass_to_candidate_chi",
        "baseline_pass_to_candidate_pon",
        "candidate_only_chi_count",
        "candidate_only_pon_count",
        "divergent_action_decisions",
        "divergent_decisions_by_candidate_seat",
        "divergent_game_count",
        "divergent_negative_score_delta_seed_blocks",
        "divergent_positive_score_delta_seed_blocks",
        "divergent_seed_block_count",
        "divergent_zero_score_delta_seed_blocks",
        "game_count",
        "mean_divergences_per_divergent_game",
        "nonzero_score_delta_seed_block_count",
        "shared_prefix_initial_call_opportunities",
        "same_action_decisions",
        "seed_block_count",
        "total_candidate_seat_decisions",
    }
    raw = expect_object(value, keys, "summary")
    seats = expect_list(
        raw["divergent_decisions_by_candidate_seat"],
        "summary.divergent_decisions_by_candidate_seat",
    )
    return OpenHandDiagnosticSummary(
        game_count=expect_int(raw["game_count"], "summary.game_count"),
        seed_block_count=expect_int(
            raw["seed_block_count"], "summary.seed_block_count"
        ),
        total_candidate_seat_decisions=expect_int(
            raw["total_candidate_seat_decisions"],
            "summary.total_candidate_seat_decisions",
        ),
        same_action_decisions=expect_int(
            raw["same_action_decisions"], "summary.same_action_decisions"
        ),
        divergent_action_decisions=expect_int(
            raw["divergent_action_decisions"],
            "summary.divergent_action_decisions",
        ),
        action_divergence_rate=expect_float(
            raw["action_divergence_rate"], "summary.action_divergence_rate"
        ),
        shared_prefix_initial_call_opportunities=expect_int(
            raw["shared_prefix_initial_call_opportunities"],
            "summary.shared_prefix_initial_call_opportunities",
        ),
        baseline_pass_to_candidate_chi=expect_int(
            raw["baseline_pass_to_candidate_chi"],
            "summary.baseline_pass_to_candidate_chi",
        ),
        baseline_pass_to_candidate_pon=expect_int(
            raw["baseline_pass_to_candidate_pon"],
            "summary.baseline_pass_to_candidate_pon",
        ),
        candidate_only_chi_count=expect_int(
            raw["candidate_only_chi_count"], "summary.candidate_only_chi_count"
        ),
        candidate_only_pon_count=expect_int(
            raw["candidate_only_pon_count"], "summary.candidate_only_pon_count"
        ),
        divergent_game_count=expect_int(
            raw["divergent_game_count"], "summary.divergent_game_count"
        ),
        divergent_seed_block_count=expect_int(
            raw["divergent_seed_block_count"],
            "summary.divergent_seed_block_count",
        ),
        nonzero_score_delta_seed_block_count=expect_int(
            raw["nonzero_score_delta_seed_block_count"],
            "summary.nonzero_score_delta_seed_block_count",
        ),
        divergent_positive_score_delta_seed_blocks=expect_int(
            raw["divergent_positive_score_delta_seed_blocks"],
            "summary.divergent_positive_score_delta_seed_blocks",
        ),
        divergent_zero_score_delta_seed_blocks=expect_int(
            raw["divergent_zero_score_delta_seed_blocks"],
            "summary.divergent_zero_score_delta_seed_blocks",
        ),
        divergent_negative_score_delta_seed_blocks=expect_int(
            raw["divergent_negative_score_delta_seed_blocks"],
            "summary.divergent_negative_score_delta_seed_blocks",
        ),
        mean_divergences_per_divergent_game=expect_optional_float(
            raw["mean_divergences_per_divergent_game"],
            "summary.mean_divergences_per_divergent_game",
        ),
        divergent_decisions_by_candidate_seat=tuple(
            expect_int(item, f"summary.divergent_decisions_by_candidate_seat[{i}]")
            for i, item in enumerate(seats)
        ),
    )


def _parse_artifact(value: object) -> OpenHandCallDiagnosticArtifact:
    raw = expect_object(
        value,
        {
            "artifact_kind",
            "diagnostic_protocol",
            "game_diagnostics",
            "plan",
            "provenance",
            "schema_version",
            "strength_artifact",
            "summary",
        },
        "artifact",
    )
    if expect_str(raw["artifact_kind"], "artifact.artifact_kind") != (
        OPEN_HAND_DIAGNOSTIC_ARTIFACT_KIND
    ):
        raise OpenHandDiagnosticArtifactError("unexpected diagnostic artifact kind")
    strength = expect_object(
        raw["strength_artifact"],
        {"evaluation_protocol", "schema_version", "sha256"},
        "strength_artifact",
    )
    if (
        expect_str(
            strength["evaluation_protocol"], "strength_artifact.evaluation_protocol"
        )
        != SINGLE_ROUND_EVALUATION_PROTOCOL
    ):
        raise OpenHandDiagnosticArtifactError("unexpected strength protocol")
    if (
        expect_int(strength["schema_version"], "strength_artifact.schema_version")
        != SINGLE_ROUND_ARTIFACT_SCHEMA_VERSION
    ):
        raise OpenHandDiagnosticArtifactError("unexpected strength schema version")
    games = tuple(
        _parse_game(item, index)
        for index, item in enumerate(
            expect_list(raw["game_diagnostics"], "game_diagnostics")
        )
    )
    return OpenHandCallDiagnosticArtifact(
        schema_version=expect_int(raw["schema_version"], "schema_version"),
        diagnostic_protocol=expect_str(
            raw["diagnostic_protocol"], "diagnostic_protocol"
        ),
        strength_artifact_sha256=expect_str(
            strength["sha256"], "strength_artifact.sha256"
        ),
        plan=_parse_plan(raw["plan"]),
        provenance=parse_execution_provenance(raw["provenance"]),
        game_diagnostics=games,
        summary=_parse_summary(raw["summary"]),
    )


def load_open_hand_diagnostic_artifact(
    path: str | Path,
    *,
    strength_artifact_path: str | Path,
) -> OpenHandCallDiagnosticArtifact:
    """sidecarと参照strength artifactをstrict readbackしてbindingを検証する。"""
    try:
        artifact = _parse_artifact(read_json_document(Path(path)))
        strength_path = Path(strength_artifact_path)
        if _sha256(strength_path) != artifact.strength_artifact_sha256:
            raise OpenHandDiagnosticArtifactError(
                "strength artifact SHA-256 does not match diagnostic sidecar"
            )
        strength = load_single_round_artifact(strength_path)
        _validate_strength_binding(artifact, strength)
        return artifact
    except OpenHandDiagnosticArtifactError:
        raise
    except (
        ArtifactValidationError,
        KeyError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        raise OpenHandDiagnosticArtifactError(
            "diagnostic artifact is malformed or inconsistent"
        ) from exc


__all__ = [
    "OPEN_HAND_DIAGNOSTIC_ARTIFACT_KIND",
    "OPEN_HAND_DIAGNOSTIC_PROTOCOL",
    "OPEN_HAND_DIAGNOSTIC_SCHEMA_VERSION",
    "OpenHandCallDiagnosticArtifact",
    "OpenHandDiagnosticArtifactError",
    "load_open_hand_diagnostic_artifact",
    "save_open_hand_diagnostic_artifact",
]
