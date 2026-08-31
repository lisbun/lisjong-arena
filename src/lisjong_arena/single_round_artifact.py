"""ABBB / ``4p-red-single`` strength evaluationのversion付きJSON artifact。

既存``lisjong_arena.artifact``の``ComparisonArtifact``はAABB comparison
protocol専用のschemaである。ABBBはmatchupとmetricsの意味が異なるため、旧
schemaへoptional fieldを足して統合せず、独立したcontractとして持つ。低レベルの
JSON plumbingだけを``lisjong_arena._artifact_io``経由で共有し、既存AABB
artifactのserialized contractは変更しない。

このmoduleが所有するのはpersistence contractだけである。

- artifactの正本はraw ``SingleRoundGameResult``の列であり、derived statisticsは
  そこから再生成できるcacheとして保存する
- derived statisticsのcanonical計算は``lisjong_arena.single_round_evaluation``が
  所有し、ここでは同じ式を再実装せず``aggregate_candidate_metrics()`` /
  ``summarize_single_round_strength()``を呼ぶだけにする
- 実行のためのobjectは復元しない。artifact planは``PolicySpec.factory``も
  executableな``SingleRoundEvaluationPlan``も持たないimmutable snapshotである

Policyを実行せず、game progressionも所有しない。package versionとVCS revision
はinstall metadataから取得し、取得できない値を推測しない。
"""

from __future__ import annotations

import json
import platform
import re
from collections.abc import Sequence
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Any

from lisjong.policy_contract import Seat

from lisjong_arena._artifact_io import (
    ArtifactValidationError,
    canonical_json_text,
    expect_bool,
    expect_float,
    expect_int,
    expect_list,
    expect_object,
    expect_optional_bool,
    expect_optional_float,
    expect_optional_int,
    expect_str,
    read_json_document,
    write_new_artifact_file,
)
from lisjong_arena.model import (
    SINGLE_ROUND_GAME_MODE,
    SINGLE_ROUND_ROTATION_COUNT,
    SingleRoundCandidateMahjongMetrics,
    SingleRoundCandidateMetrics,
    SingleRoundEvaluationResult,
    SingleRoundGameResult,
    _normalize_seeds,
    validate_single_round_game_results,
)
from lisjong_arena.riichienv.round_stats import SeatRoundStats
from lisjong_arena.single_round_evaluation import (
    SeedBlockStatistics,
    SingleRoundStrengthSummary,
    aggregate_candidate_metrics,
    summarize_single_round_strength,
)

SINGLE_ROUND_ARTIFACT_SCHEMA_VERSION = 1
"""ABBB strength artifactのschema version。未知のversionはfail closedする。"""

SINGLE_ROUND_EVALUATION_PROTOCOL = "abbb-single-round-v1"
"""artifactが記録するevaluation protocol identity。AABBとは別のidentityである。"""

EXECUTION_ENVIRONMENT = "riichienv"
"""現在のABBB execution path identity。"""

_FULL_COMMIT_ID = re.compile(r"[0-9a-f]{40}").fullmatch


class SingleRoundArtifactError(ArtifactValidationError):
    """ABBB artifactを生成、検証、または合成できない場合。"""


@dataclass(frozen=True, slots=True)
class SingleRoundArtifactPlan:
    """factoryを含まないABBB evaluation条件のimmutable snapshot。

    ``PolicySpec``ではなくidentityだけを保持する。artifact readerはここから
    実行可能な``SingleRoundEvaluationPlan``やPolicy factoryを復元しない。

    ``game_mode``と``rotation_count``はcallerが選べるoptionではなく、
    protocol invariantが実際にその値であったことのrecordとして保存し、
    異なる値をfail closedする。
    """

    candidate_identity: str
    baseline_identity: str
    seeds: tuple[int, ...]
    game_mode: str
    rotation_count: int
    max_steps: int

    def __post_init__(self) -> None:
        for name in ("candidate_identity", "baseline_identity", "game_mode"):
            value = getattr(self, name)
            if type(value) is not str:
                raise TypeError(f"{name} must be a str")
            if not value:
                raise ValueError(f"{name} must not be empty")
        if self.candidate_identity == self.baseline_identity:
            raise ValueError("candidate and baseline identities must be distinct")
        if self.game_mode != SINGLE_ROUND_GAME_MODE:
            raise ValueError(f"unsupported game mode: {self.game_mode!r}")
        for name in ("rotation_count", "max_steps"):
            value = getattr(self, name)
            if type(value) is not int:
                raise TypeError(f"{name} must be an int")
        if self.rotation_count != SINGLE_ROUND_ROTATION_COUNT:
            raise ValueError(f"unsupported rotation count: {self.rotation_count!r}")
        if self.max_steps <= 0:
            raise ValueError("max_steps must be positive")
        object.__setattr__(self, "seeds", _normalize_seeds(self.seeds))


@dataclass(frozen=True, slots=True)
class SingleRoundExecutionProvenance:
    """ABBB評価の実行経路と再現性に必要なpackage identity。

    値はすべてinstall metadataから確認できたfactだけで構成し、取得できない
    値をunknown / 空文字で埋めない。``lisjong``と``lisjong-engine``は評価
    semanticsそのものへ影響するため、full commit IDを必須とする
    (取得できない環境ではartifactを生成せずfail closedする)。

    ``lisjong-arena``自身はeditable installで実行されるためVCS revisionを
    install metadataから確認できず、推測もしない。distribution versionだけを
    記録する既存``lisjong_arena.artifact``のprovenance思想をそのまま踏襲する。
    """

    execution_environment: str
    lisjong_arena_version: str
    lisjong_version: str
    lisjong_revision: str
    lisjong_engine_version: str
    lisjong_engine_revision: str
    riichienv_version: str
    python_version: str

    def __post_init__(self) -> None:
        for name in (
            "execution_environment",
            "lisjong_arena_version",
            "lisjong_version",
            "lisjong_revision",
            "lisjong_engine_version",
            "lisjong_engine_revision",
            "riichienv_version",
            "python_version",
        ):
            value = getattr(self, name)
            if type(value) is not str:
                raise TypeError(f"{name} must be a str")
            if not value:
                raise ValueError(f"{name} must not be empty")
        if self.execution_environment != EXECUTION_ENVIRONMENT:
            raise ValueError(
                f"unsupported execution environment: {self.execution_environment!r}"
            )
        for name in ("lisjong_revision", "lisjong_engine_revision"):
            if _FULL_COMMIT_ID(getattr(self, name)) is None:
                raise ValueError(f"{name} must be a lowercase full commit ID")


@dataclass(frozen=True, slots=True)
class SingleRoundStrengthArtifact:
    """検証済みの過去ABBB strength evaluation record。

    ``SingleRoundEvaluationResult``とは異なりPolicy factoryを持たず、実行には
    使用しない。``summary``はconstruction時点でraw ``game_results``から
    canonical aggregationにより再計算し、一致しない場合は受理しない。
    """

    schema_version: int
    evaluation_protocol: str
    plan: SingleRoundArtifactPlan
    provenance: SingleRoundExecutionProvenance
    game_results: tuple[SingleRoundGameResult, ...]
    summary: SingleRoundStrengthSummary

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int:
            raise TypeError("schema_version must be an int")
        if self.schema_version != SINGLE_ROUND_ARTIFACT_SCHEMA_VERSION:
            raise ValueError(f"unsupported schema version: {self.schema_version!r}")
        if type(self.evaluation_protocol) is not str:
            raise TypeError("evaluation_protocol must be a str")
        if self.evaluation_protocol != SINGLE_ROUND_EVALUATION_PROTOCOL:
            raise ValueError(
                f"unsupported evaluation protocol: {self.evaluation_protocol!r}"
            )
        if not isinstance(self.plan, SingleRoundArtifactPlan):
            raise TypeError("plan must be a SingleRoundArtifactPlan")
        if not isinstance(self.provenance, SingleRoundExecutionProvenance):
            raise TypeError("provenance must be a SingleRoundExecutionProvenance")
        object.__setattr__(
            self,
            "game_results",
            validate_single_round_game_results(self.game_results, self.plan.seeds),
        )
        _validate_summary(self.summary, self.plan.candidate_identity, self.game_results)


@dataclass(frozen=True, slots=True)
class CumulativeSingleRoundStrength:
    """compatibleな複数artifactを1つのstrength evaluationとして再集計した結果。

    ``summary``は各artifactのaggregateを加重平均したものではなく、連結した
    raw game resultsへcanonical aggregationを再適用して得た値である。
    """

    plan: SingleRoundArtifactPlan
    provenance: SingleRoundExecutionProvenance
    artifact_count: int
    game_results: tuple[SingleRoundGameResult, ...]
    summary: SingleRoundStrengthSummary

    def __post_init__(self) -> None:
        if not isinstance(self.plan, SingleRoundArtifactPlan):
            raise TypeError("plan must be a SingleRoundArtifactPlan")
        if not isinstance(self.provenance, SingleRoundExecutionProvenance):
            raise TypeError("provenance must be a SingleRoundExecutionProvenance")
        if type(self.artifact_count) is not int:
            raise TypeError("artifact_count must be an int")
        if self.artifact_count <= 0:
            raise ValueError("artifact_count must be positive")
        object.__setattr__(
            self,
            "game_results",
            validate_single_round_game_results(self.game_results, self.plan.seeds),
        )
        _validate_summary(self.summary, self.plan.candidate_identity, self.game_results)


def _canonical_summary(
    candidate_identity: str,
    game_results: tuple[SingleRoundGameResult, ...],
) -> SingleRoundStrengthSummary:
    """raw game resultsだけからderived strength statisticsを再集計する。"""
    return summarize_single_round_strength(
        aggregate_candidate_metrics(candidate_identity, game_results), game_results
    )


def _validate_summary(
    summary: object,
    candidate_identity: str,
    game_results: tuple[SingleRoundGameResult, ...],
) -> None:
    """derived summaryをraw resultsからのcanonical再集計と照合する。"""
    if not isinstance(summary, SingleRoundStrengthSummary):
        raise TypeError("summary must be a SingleRoundStrengthSummary")
    if summary != _canonical_summary(candidate_identity, game_results):
        raise ValueError("summary does not match aggregated game_results")


def _package_version(distribution_name: str) -> str:
    try:
        return metadata.version(distribution_name)
    except metadata.PackageNotFoundError as exc:
        raise SingleRoundArtifactError(
            f"package metadata is unavailable for {distribution_name!r}"
        ) from exc


def _vcs_revision(distribution_name: str) -> str:
    """VCS install metadataからfull commit IDを取得する。

    editable / directory installのようにVCS revisionを確認できない場合は、
    値を推測せずfail closedする。
    """
    try:
        direct_url_text = metadata.distribution(distribution_name).read_text(
            "direct_url.json"
        )
    except metadata.PackageNotFoundError as exc:
        raise SingleRoundArtifactError(
            f"package metadata is unavailable for {distribution_name!r}"
        ) from exc
    if direct_url_text is None:
        raise SingleRoundArtifactError(
            f"{distribution_name} direct_url.json is unavailable; "
            "source revision cannot be verified"
        )
    try:
        direct_url = json.loads(direct_url_text)
    except json.JSONDecodeError as exc:
        raise SingleRoundArtifactError(
            f"{distribution_name} direct_url.json is malformed"
        ) from exc
    if type(direct_url) is not dict or type(direct_url.get("vcs_info")) is not dict:
        raise SingleRoundArtifactError(
            f"{distribution_name} VCS metadata is unavailable; "
            "source revision cannot be verified"
        )
    vcs_info = direct_url["vcs_info"]
    revision = vcs_info.get("commit_id")
    if vcs_info.get("vcs") != "git" or type(revision) is not str:
        raise SingleRoundArtifactError(
            f"{distribution_name} VCS revision metadata is malformed"
        )
    if _FULL_COMMIT_ID(revision) is None:
        raise SingleRoundArtifactError(
            f"{distribution_name} source revision is not a full commit ID"
        )
    return revision


def _collect_execution_provenance() -> SingleRoundExecutionProvenance:
    return SingleRoundExecutionProvenance(
        execution_environment=EXECUTION_ENVIRONMENT,
        lisjong_arena_version=_package_version("lisjong-arena"),
        lisjong_version=_package_version("lisjong"),
        lisjong_revision=_vcs_revision("lisjong"),
        lisjong_engine_version=_package_version("lisjong-engine"),
        lisjong_engine_revision=_vcs_revision("lisjong-engine"),
        riichienv_version=_package_version("riichienv"),
        python_version=platform.python_version(),
    )


def _artifact_from_result(
    result: SingleRoundEvaluationResult,
) -> SingleRoundStrengthArtifact:
    if not isinstance(result, SingleRoundEvaluationResult):
        raise TypeError(
            "result must be a SingleRoundEvaluationResult "
            "(Mortal single-round results are not supported by this artifact "
            "contract)"
        )
    plan = SingleRoundArtifactPlan(
        candidate_identity=result.plan.candidate.identity,
        baseline_identity=result.plan.baseline.identity,
        seeds=result.plan.seeds,
        game_mode=SINGLE_ROUND_GAME_MODE,
        rotation_count=SINGLE_ROUND_ROTATION_COUNT,
        max_steps=result.plan.max_steps,
    )
    return SingleRoundStrengthArtifact(
        schema_version=SINGLE_ROUND_ARTIFACT_SCHEMA_VERSION,
        evaluation_protocol=SINGLE_ROUND_EVALUATION_PROTOCOL,
        plan=plan,
        provenance=_collect_execution_provenance(),
        game_results=result.game_results,
        summary=summarize_single_round_strength(
            result.candidate_metrics, result.game_results
        ),
    )


def _seat_round_stats_to_dict(stats: SeatRoundStats) -> dict[str, Any]:
    return {
        "deal_in_loss": stats.deal_in_loss,
        "dealt_in": stats.dealt_in,
        "end_score": stats.end_score,
        "exhaustive_draw": stats.exhaustive_draw,
        "first_tenpai_turn": stats.first_tenpai_turn,
        "start_score": stats.start_score,
        "tenpai_at_exhaustive_draw": stats.tenpai_at_exhaustive_draw,
        "win_points": stats.win_points,
        "won": stats.won,
    }


def _game_result_to_dict(game_result: SingleRoundGameResult) -> dict[str, Any]:
    return {
        "candidate_seat": int(game_result.candidate_seat),
        "game_mode": game_result.game_mode,
        "rotation": game_result.rotation,
        "scores": list(game_result.scores),
        "seat_round_stats": [
            _seat_round_stats_to_dict(stats) for stats in game_result.seat_round_stats
        ],
        "seed": game_result.seed,
    }


def _mahjong_metrics_to_dict(
    metrics: SingleRoundCandidateMahjongMetrics,
) -> dict[str, Any]:
    return {
        "deal_in_count": metrics.deal_in_count,
        "deal_in_rate": metrics.deal_in_rate,
        "exhaustive_draw_count": metrics.exhaustive_draw_count,
        "exhaustive_draw_tenpai_count": metrics.exhaustive_draw_tenpai_count,
        "exhaustive_draw_tenpai_rate": metrics.exhaustive_draw_tenpai_rate,
        "mean_deal_in_loss": metrics.mean_deal_in_loss,
        "mean_first_tenpai_turn": metrics.mean_first_tenpai_turn,
        "mean_round_score_delta": metrics.mean_round_score_delta,
        "mean_win_points": metrics.mean_win_points,
        "round_count": metrics.round_count,
        "tenpai_reached_count": metrics.tenpai_reached_count,
        "win_count": metrics.win_count,
        "win_rate": metrics.win_rate,
    }


def _candidate_metrics_to_dict(metrics: SingleRoundCandidateMetrics) -> dict[str, Any]:
    return {
        "candidate_identity": metrics.candidate_identity,
        "game_count": metrics.game_count,
        "mahjong_metrics": _mahjong_metrics_to_dict(metrics.mahjong_metrics),
        "mean_candidate_score": metrics.mean_candidate_score,
        "seat_mean_scores": list(metrics.seat_mean_scores),
    }


def _seed_block_statistics_to_dict(
    statistics: SeedBlockStatistics,
) -> dict[str, Any]:
    return {
        "mean_seed_block_delta": statistics.mean_seed_block_delta,
        "negative_seed_block_count": statistics.negative_seed_block_count,
        "normal_approx_95_interval_lower": (statistics.normal_approx_95_interval_lower),
        "normal_approx_95_interval_upper": (statistics.normal_approx_95_interval_upper),
        "positive_seed_block_count": statistics.positive_seed_block_count,
        "sample_standard_deviation": statistics.sample_standard_deviation,
        "seed_block_count": statistics.seed_block_count,
        "standard_error": statistics.standard_error,
        "zero_seed_block_count": statistics.zero_seed_block_count,
    }


def _summary_to_dict(summary: SingleRoundStrengthSummary) -> dict[str, Any]:
    return {
        "candidate_metrics": _candidate_metrics_to_dict(summary.candidate_metrics),
        "mean_baseline_score": summary.mean_baseline_score,
        "mean_candidate_game_delta": summary.mean_candidate_game_delta,
        "seed_block_statistics": _seed_block_statistics_to_dict(
            summary.seed_block_statistics
        ),
    }


def _artifact_to_dict(artifact: SingleRoundStrengthArtifact) -> dict[str, Any]:
    return {
        "evaluation_protocol": artifact.evaluation_protocol,
        "game_results": [
            _game_result_to_dict(game_result) for game_result in artifact.game_results
        ],
        "plan": {
            "baseline_identity": artifact.plan.baseline_identity,
            "candidate_identity": artifact.plan.candidate_identity,
            "game_mode": artifact.plan.game_mode,
            "max_steps": artifact.plan.max_steps,
            "rotation_count": artifact.plan.rotation_count,
            "seeds": list(artifact.plan.seeds),
        },
        "provenance": {
            "execution_environment": artifact.provenance.execution_environment,
            "lisjong_arena_version": artifact.provenance.lisjong_arena_version,
            "lisjong_engine_revision": artifact.provenance.lisjong_engine_revision,
            "lisjong_engine_version": artifact.provenance.lisjong_engine_version,
            "lisjong_revision": artifact.provenance.lisjong_revision,
            "lisjong_version": artifact.provenance.lisjong_version,
            "python_version": artifact.provenance.python_version,
            "riichienv_version": artifact.provenance.riichienv_version,
        },
        "schema_version": artifact.schema_version,
        "summary": _summary_to_dict(artifact.summary),
    }


def _serialize_artifact(artifact: SingleRoundStrengthArtifact) -> str:
    return canonical_json_text(_artifact_to_dict(artifact))


def save_single_round_artifact(
    result: SingleRoundEvaluationResult,
    path: str | Path,
) -> None:
    """成功したABBB評価を新しいUTF-8 JSON fileへ保存する。

    ``1 run = 1 immutable artifact``とするため、既存pathは上書きせず
    ``FileExistsError``を送出する。serializationとvalidationはfile作成前に
    完了し、書き込み途中で失敗した場合もpartial fileを残さない。

    保存はevaluationの後段でのみ行い、evaluation結果自体には影響しない。
    """
    artifact = _artifact_from_result(result)
    serialized = _serialize_artifact(artifact)
    write_new_artifact_file(Path(path), serialized)


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
    seeds = tuple(
        expect_int(seed, f"plan.seeds[{index}]")
        for index, seed in enumerate(expect_list(raw["seeds"], "plan.seeds"))
    )
    return SingleRoundArtifactPlan(
        candidate_identity=expect_str(
            raw["candidate_identity"], "plan.candidate_identity"
        ),
        baseline_identity=expect_str(
            raw["baseline_identity"], "plan.baseline_identity"
        ),
        seeds=seeds,
        game_mode=expect_str(raw["game_mode"], "plan.game_mode"),
        rotation_count=expect_int(raw["rotation_count"], "plan.rotation_count"),
        max_steps=expect_int(raw["max_steps"], "plan.max_steps"),
    )


def _parse_provenance(value: object) -> SingleRoundExecutionProvenance:
    raw = expect_object(
        value,
        {
            "execution_environment",
            "lisjong_arena_version",
            "lisjong_engine_revision",
            "lisjong_engine_version",
            "lisjong_revision",
            "lisjong_version",
            "python_version",
            "riichienv_version",
        },
        "provenance",
    )
    return SingleRoundExecutionProvenance(
        execution_environment=expect_str(
            raw["execution_environment"], "provenance.execution_environment"
        ),
        lisjong_arena_version=expect_str(
            raw["lisjong_arena_version"], "provenance.lisjong_arena_version"
        ),
        lisjong_version=expect_str(
            raw["lisjong_version"], "provenance.lisjong_version"
        ),
        lisjong_revision=expect_str(
            raw["lisjong_revision"], "provenance.lisjong_revision"
        ),
        lisjong_engine_version=expect_str(
            raw["lisjong_engine_version"], "provenance.lisjong_engine_version"
        ),
        lisjong_engine_revision=expect_str(
            raw["lisjong_engine_revision"], "provenance.lisjong_engine_revision"
        ),
        riichienv_version=expect_str(
            raw["riichienv_version"], "provenance.riichienv_version"
        ),
        python_version=expect_str(raw["python_version"], "provenance.python_version"),
    )


def _parse_seat_round_stats(value: object, context: str) -> SeatRoundStats:
    raw = expect_object(
        value,
        {
            "deal_in_loss",
            "dealt_in",
            "end_score",
            "exhaustive_draw",
            "first_tenpai_turn",
            "start_score",
            "tenpai_at_exhaustive_draw",
            "win_points",
            "won",
        },
        context,
    )
    return SeatRoundStats(
        start_score=expect_int(raw["start_score"], f"{context}.start_score"),
        end_score=expect_int(raw["end_score"], f"{context}.end_score"),
        won=expect_bool(raw["won"], f"{context}.won"),
        win_points=expect_optional_int(raw["win_points"], f"{context}.win_points"),
        dealt_in=expect_bool(raw["dealt_in"], f"{context}.dealt_in"),
        deal_in_loss=expect_optional_int(
            raw["deal_in_loss"], f"{context}.deal_in_loss"
        ),
        exhaustive_draw=expect_bool(
            raw["exhaustive_draw"], f"{context}.exhaustive_draw"
        ),
        tenpai_at_exhaustive_draw=expect_optional_bool(
            raw["tenpai_at_exhaustive_draw"], f"{context}.tenpai_at_exhaustive_draw"
        ),
        first_tenpai_turn=expect_optional_int(
            raw["first_tenpai_turn"], f"{context}.first_tenpai_turn"
        ),
    )


def _parse_game_result(value: object, index: int) -> SingleRoundGameResult:
    context = f"game_results[{index}]"
    raw = expect_object(
        value,
        {
            "candidate_seat",
            "game_mode",
            "rotation",
            "scores",
            "seat_round_stats",
            "seed",
        },
        context,
    )
    seat_number = expect_int(raw["candidate_seat"], f"{context}.candidate_seat")
    try:
        candidate_seat = Seat(seat_number)
    except ValueError as exc:
        raise SingleRoundArtifactError(f"{context}.candidate_seat is invalid") from exc
    scores = tuple(
        expect_int(score, f"{context}.scores[{seat}]")
        for seat, score in enumerate(expect_list(raw["scores"], f"{context}.scores"))
    )
    seat_round_stats = tuple(
        _parse_seat_round_stats(stats, f"{context}.seat_round_stats[{seat}]")
        for seat, stats in enumerate(
            expect_list(raw["seat_round_stats"], f"{context}.seat_round_stats")
        )
    )
    return SingleRoundGameResult(
        seed=expect_int(raw["seed"], f"{context}.seed"),
        rotation=expect_int(raw["rotation"], f"{context}.rotation"),
        game_mode=expect_str(raw["game_mode"], f"{context}.game_mode"),
        candidate_seat=candidate_seat,
        scores=scores,
        seat_round_stats=seat_round_stats,
    )


def _parse_mahjong_metrics(
    value: object, context: str
) -> SingleRoundCandidateMahjongMetrics:
    raw = expect_object(
        value,
        {
            "deal_in_count",
            "deal_in_rate",
            "exhaustive_draw_count",
            "exhaustive_draw_tenpai_count",
            "exhaustive_draw_tenpai_rate",
            "mean_deal_in_loss",
            "mean_first_tenpai_turn",
            "mean_round_score_delta",
            "mean_win_points",
            "round_count",
            "tenpai_reached_count",
            "win_count",
            "win_rate",
        },
        context,
    )
    return SingleRoundCandidateMahjongMetrics(
        round_count=expect_int(raw["round_count"], f"{context}.round_count"),
        mean_round_score_delta=expect_float(
            raw["mean_round_score_delta"], f"{context}.mean_round_score_delta"
        ),
        win_count=expect_int(raw["win_count"], f"{context}.win_count"),
        win_rate=expect_float(raw["win_rate"], f"{context}.win_rate"),
        mean_win_points=expect_optional_float(
            raw["mean_win_points"], f"{context}.mean_win_points"
        ),
        deal_in_count=expect_int(raw["deal_in_count"], f"{context}.deal_in_count"),
        deal_in_rate=expect_float(raw["deal_in_rate"], f"{context}.deal_in_rate"),
        mean_deal_in_loss=expect_optional_float(
            raw["mean_deal_in_loss"], f"{context}.mean_deal_in_loss"
        ),
        exhaustive_draw_count=expect_int(
            raw["exhaustive_draw_count"], f"{context}.exhaustive_draw_count"
        ),
        exhaustive_draw_tenpai_count=expect_int(
            raw["exhaustive_draw_tenpai_count"],
            f"{context}.exhaustive_draw_tenpai_count",
        ),
        exhaustive_draw_tenpai_rate=expect_optional_float(
            raw["exhaustive_draw_tenpai_rate"],
            f"{context}.exhaustive_draw_tenpai_rate",
        ),
        tenpai_reached_count=expect_int(
            raw["tenpai_reached_count"], f"{context}.tenpai_reached_count"
        ),
        mean_first_tenpai_turn=expect_optional_float(
            raw["mean_first_tenpai_turn"], f"{context}.mean_first_tenpai_turn"
        ),
    )


def _parse_candidate_metrics(
    value: object, context: str
) -> SingleRoundCandidateMetrics:
    raw = expect_object(
        value,
        {
            "candidate_identity",
            "game_count",
            "mahjong_metrics",
            "mean_candidate_score",
            "seat_mean_scores",
        },
        context,
    )
    seat_mean_scores = tuple(
        expect_float(score, f"{context}.seat_mean_scores[{seat}]")
        for seat, score in enumerate(
            expect_list(raw["seat_mean_scores"], f"{context}.seat_mean_scores")
        )
    )
    return SingleRoundCandidateMetrics(
        candidate_identity=expect_str(
            raw["candidate_identity"], f"{context}.candidate_identity"
        ),
        game_count=expect_int(raw["game_count"], f"{context}.game_count"),
        mean_candidate_score=expect_float(
            raw["mean_candidate_score"], f"{context}.mean_candidate_score"
        ),
        seat_mean_scores=seat_mean_scores,
        mahjong_metrics=_parse_mahjong_metrics(
            raw["mahjong_metrics"], f"{context}.mahjong_metrics"
        ),
    )


def _parse_seed_block_statistics(value: object, context: str) -> SeedBlockStatistics:
    raw = expect_object(
        value,
        {
            "mean_seed_block_delta",
            "negative_seed_block_count",
            "normal_approx_95_interval_lower",
            "normal_approx_95_interval_upper",
            "positive_seed_block_count",
            "sample_standard_deviation",
            "seed_block_count",
            "standard_error",
            "zero_seed_block_count",
        },
        context,
    )
    return SeedBlockStatistics(
        seed_block_count=expect_int(
            raw["seed_block_count"], f"{context}.seed_block_count"
        ),
        mean_seed_block_delta=expect_float(
            raw["mean_seed_block_delta"], f"{context}.mean_seed_block_delta"
        ),
        sample_standard_deviation=expect_optional_float(
            raw["sample_standard_deviation"], f"{context}.sample_standard_deviation"
        ),
        standard_error=expect_optional_float(
            raw["standard_error"], f"{context}.standard_error"
        ),
        normal_approx_95_interval_lower=expect_optional_float(
            raw["normal_approx_95_interval_lower"],
            f"{context}.normal_approx_95_interval_lower",
        ),
        normal_approx_95_interval_upper=expect_optional_float(
            raw["normal_approx_95_interval_upper"],
            f"{context}.normal_approx_95_interval_upper",
        ),
        positive_seed_block_count=expect_int(
            raw["positive_seed_block_count"], f"{context}.positive_seed_block_count"
        ),
        zero_seed_block_count=expect_int(
            raw["zero_seed_block_count"], f"{context}.zero_seed_block_count"
        ),
        negative_seed_block_count=expect_int(
            raw["negative_seed_block_count"], f"{context}.negative_seed_block_count"
        ),
    )


def _parse_summary(value: object) -> SingleRoundStrengthSummary:
    raw = expect_object(
        value,
        {
            "candidate_metrics",
            "mean_baseline_score",
            "mean_candidate_game_delta",
            "seed_block_statistics",
        },
        "summary",
    )
    return SingleRoundStrengthSummary(
        candidate_metrics=_parse_candidate_metrics(
            raw["candidate_metrics"], "summary.candidate_metrics"
        ),
        mean_baseline_score=expect_float(
            raw["mean_baseline_score"], "summary.mean_baseline_score"
        ),
        mean_candidate_game_delta=expect_float(
            raw["mean_candidate_game_delta"], "summary.mean_candidate_game_delta"
        ),
        seed_block_statistics=_parse_seed_block_statistics(
            raw["seed_block_statistics"], "summary.seed_block_statistics"
        ),
    )


def _parse_artifact(value: object) -> SingleRoundStrengthArtifact:
    raw = expect_object(
        value,
        {
            "evaluation_protocol",
            "game_results",
            "plan",
            "provenance",
            "schema_version",
            "summary",
        },
        "artifact",
    )
    schema_version = expect_int(raw["schema_version"], "schema_version")
    if schema_version != SINGLE_ROUND_ARTIFACT_SCHEMA_VERSION:
        raise SingleRoundArtifactError(
            f"unsupported schema version: {schema_version!r}"
        )
    evaluation_protocol = expect_str(raw["evaluation_protocol"], "evaluation_protocol")
    if evaluation_protocol != SINGLE_ROUND_EVALUATION_PROTOCOL:
        raise SingleRoundArtifactError(
            f"unsupported evaluation protocol: {evaluation_protocol!r}"
        )
    return SingleRoundStrengthArtifact(
        schema_version=schema_version,
        evaluation_protocol=evaluation_protocol,
        plan=_parse_plan(raw["plan"]),
        provenance=_parse_provenance(raw["provenance"]),
        game_results=tuple(
            _parse_game_result(game_result, index)
            for index, game_result in enumerate(
                expect_list(raw["game_results"], "game_results")
            )
        ),
        summary=_parse_summary(raw["summary"]),
    )


def load_single_round_artifact(path: str | Path) -> SingleRoundStrengthArtifact:
    """JSON fileをfail-closedに検証してimmutable artifact snapshotを返す。"""
    try:
        value = read_json_document(Path(path))
        return _parse_artifact(value)
    except SingleRoundArtifactError:
        raise
    except ArtifactValidationError as exc:
        raise SingleRoundArtifactError(str(exc)) from exc
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise SingleRoundArtifactError("artifact is malformed or inconsistent") from exc


_PLAN_COMPATIBILITY_FIELDS = (
    "candidate_identity",
    "baseline_identity",
    "game_mode",
    "rotation_count",
    "max_steps",
)


def merge_single_round_artifacts(
    artifacts: Sequence[SingleRoundStrengthArtifact],
) -> CumulativeSingleRoundStrength:
    """compatibleな複数artifactを1つのcumulative strength evaluationへ合成する。

    合成はfail closedである。candidate / baseline identity、game mode、
    rotation semantics、``max_steps``、execution provenanceのいずれかが
    一致しない場合、またはseedが重複する場合は合成しない。overlapping seedは
    deduplicateせずrejectする。異なるimplementation revisionのartifactを
    自動的に同一campaign扱いすることもしない。

    schema versionとevaluation protocol identityは、artifact contract自身が
    construction時とload時にsupported値以外をfail closedしているため、ここへ
    到達する時点で一致している。

    順序は入力artifact順、その中では各artifactのordered seed順を保持し、
    protocol orderingを勝手にsortしない。derived statisticsは連結した
    raw game resultsへcanonical aggregationを再適用して得る。
    """
    if isinstance(artifacts, (str, bytes, bytearray)) or not isinstance(
        artifacts, Sequence
    ):
        raise TypeError("artifacts must be an ordered collection")
    items = tuple(artifacts)
    if not items:
        raise SingleRoundArtifactError("at least one artifact is required")
    if any(not isinstance(item, SingleRoundStrengthArtifact) for item in items):
        raise TypeError("artifacts must contain only SingleRoundStrengthArtifact")

    first = items[0]
    for index, artifact in enumerate(items[1:], start=1):
        context = f"artifacts[{index}]"
        for name in _PLAN_COMPATIBILITY_FIELDS:
            if getattr(artifact.plan, name) != getattr(first.plan, name):
                raise SingleRoundArtifactError(
                    f"{context} plan {name} does not match artifacts[0]"
                )
        if artifact.provenance != first.provenance:
            raise SingleRoundArtifactError(
                f"{context} execution provenance does not match artifacts[0]"
            )

    seed_source: dict[int, int] = {}
    seeds: list[int] = []
    for index, artifact in enumerate(items):
        for seed in artifact.plan.seeds:
            if seed in seed_source:
                raise SingleRoundArtifactError(
                    f"artifacts[{index}] repeats seed {seed} already covered by "
                    f"artifacts[{seed_source[seed]}]"
                )
            seed_source[seed] = index
            seeds.append(seed)

    game_results = tuple(
        game_result for artifact in items for game_result in artifact.game_results
    )
    plan = SingleRoundArtifactPlan(
        candidate_identity=first.plan.candidate_identity,
        baseline_identity=first.plan.baseline_identity,
        seeds=tuple(seeds),
        game_mode=first.plan.game_mode,
        rotation_count=first.plan.rotation_count,
        max_steps=first.plan.max_steps,
    )
    return CumulativeSingleRoundStrength(
        plan=plan,
        provenance=first.provenance,
        artifact_count=len(items),
        game_results=game_results,
        summary=_canonical_summary(plan.candidate_identity, game_results),
    )


__all__ = [
    "EXECUTION_ENVIRONMENT",
    "SINGLE_ROUND_ARTIFACT_SCHEMA_VERSION",
    "SINGLE_ROUND_EVALUATION_PROTOCOL",
    "CumulativeSingleRoundStrength",
    "SingleRoundArtifactError",
    "SingleRoundArtifactPlan",
    "SingleRoundExecutionProvenance",
    "SingleRoundStrengthArtifact",
    "load_single_round_artifact",
    "merge_single_round_artifacts",
    "save_single_round_artifact",
]
