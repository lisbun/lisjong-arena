"""Policy comparison resultのversion付きJSON artifact。

実行用の``ComparisonResult``からfactoryを含まないimmutable snapshotを作成し、
明示的に保存する。readerはJSONを実行可能な``ComparisonPlan``へ戻さず、raw result、
metrics、provenanceが現在のcomparison protocolとして自己矛盾していないことを
検証した``ComparisonArtifact``だけを返す。

このmoduleはPolicyを実行せず、game progressionも所有しない。RiichiEnv versionは
package metadataから取得し、``riichienv``を直接importしない。
"""

from __future__ import annotations

import json
import platform
import re
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Any

from lisjong.policy_contract import Seat

from lisjong_arena.comparison import ROTATION_COUNT, aggregate_policy_metrics
from lisjong_arena.model import (
    ComparisonResult,
    PolicyMetrics,
    SeatResult,
    _normalize_seeds,
)

ARTIFACT_SCHEMA_VERSION = 1
COMPARISON_PROTOCOL = "fixed-seed-seat-rotation-v1"
EXECUTION_ENVIRONMENT = "riichienv"

_EXPECTED_RANKS = (1, 2, 3, 4)
_FULL_COMMIT_ID = re.compile(r"[0-9a-f]{40}").fullmatch


class ComparisonArtifactError(ValueError):
    """artifactを生成または検証できない場合。"""


@dataclass(frozen=True, slots=True)
class ArtifactPlan:
    """factoryを含まないcomparison条件のimmutable snapshot。"""

    policy_a_identity: str
    policy_b_identity: str
    seeds: tuple[int, ...]
    game_mode: str
    max_steps: int

    def __post_init__(self) -> None:
        for name in ("policy_a_identity", "policy_b_identity", "game_mode"):
            value = getattr(self, name)
            if type(value) is not str:
                raise TypeError(f"{name} must be a str")
            if not value:
                raise ValueError(f"{name} must not be empty")
        if self.policy_a_identity == self.policy_b_identity:
            raise ValueError("policy identities must be distinct")
        if type(self.max_steps) is not int:
            raise TypeError("max_steps must be an int")
        if self.max_steps <= 0:
            raise ValueError("max_steps must be positive")
        object.__setattr__(self, "seeds", _normalize_seeds(self.seeds))


@dataclass(frozen=True, slots=True)
class ExecutionProvenance:
    """comparisonの実行経路と再現性に必要なpackage identity。"""

    execution_environment: str
    lisjong_arena_version: str
    lisjong_version: str
    lisjong_revision: str
    riichienv_version: str
    python_version: str

    def __post_init__(self) -> None:
        for name in (
            "execution_environment",
            "lisjong_arena_version",
            "lisjong_version",
            "lisjong_revision",
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
        if _FULL_COMMIT_ID(self.lisjong_revision) is None:
            raise ValueError("lisjong_revision must be a lowercase full commit ID")


@dataclass(frozen=True, slots=True)
class ComparisonArtifact:
    """検証済みの過去comparison record。

    ``ComparisonResult``とは異なりPolicy factoryを持たず、実行には使用しない。
    """

    schema_version: int
    comparison_protocol: str
    plan: ArtifactPlan
    provenance: ExecutionProvenance
    seat_results: tuple[SeatResult, ...]
    metrics_a: PolicyMetrics
    metrics_b: PolicyMetrics

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int:
            raise TypeError("schema_version must be an int")
        if self.schema_version != ARTIFACT_SCHEMA_VERSION:
            raise ValueError(f"unsupported schema version: {self.schema_version!r}")
        if type(self.comparison_protocol) is not str:
            raise TypeError("comparison_protocol must be a str")
        if self.comparison_protocol != COMPARISON_PROTOCOL:
            raise ValueError(
                f"unsupported comparison protocol: {self.comparison_protocol!r}"
            )
        if not isinstance(self.plan, ArtifactPlan):
            raise TypeError("plan must be an ArtifactPlan")
        if not isinstance(self.provenance, ExecutionProvenance):
            raise TypeError("provenance must be an ExecutionProvenance")
        if type(self.seat_results) is not tuple:
            raise TypeError("seat_results must be a tuple")
        if any(not isinstance(result, SeatResult) for result in self.seat_results):
            raise TypeError("seat_results must contain only SeatResult values")
        if not isinstance(self.metrics_a, PolicyMetrics):
            raise TypeError("metrics_a must be PolicyMetrics")
        if not isinstance(self.metrics_b, PolicyMetrics):
            raise TypeError("metrics_b must be PolicyMetrics")
        _validate_comparison_record(self)


def _policy_identity_assignment(
    plan: ArtifactPlan,
    rotation: int,
) -> tuple[str, str, str, str]:
    base = (
        plan.policy_a_identity,
        plan.policy_a_identity,
        plan.policy_b_identity,
        plan.policy_b_identity,
    )
    return tuple(base[(index - rotation) % ROTATION_COUNT] for index in range(4))


def _validate_comparison_record(artifact: ComparisonArtifact) -> None:
    plan = artifact.plan
    expected_count = len(plan.seeds) * ROTATION_COUNT * len(Seat)
    if len(artifact.seat_results) != expected_count:
        raise ValueError(
            f"seat_results count must be {expected_count}, "
            f"got {len(artifact.seat_results)}"
        )

    index = 0
    for seed in plan.seeds:
        for rotation in range(ROTATION_COUNT):
            assignment = _policy_identity_assignment(plan, rotation)
            game_results: list[SeatResult] = []
            for seat in Seat:
                result = artifact.seat_results[index]
                index += 1
                if result.seed != seed:
                    raise ValueError("seat_results seed/order does not match the plan")
                if result.rotation != rotation:
                    raise ValueError(
                        "seat_results rotation/order does not match the plan"
                    )
                if result.seat is not seat:
                    raise ValueError(
                        "seat_results seat/order does not match the protocol"
                    )
                if result.game_mode != plan.game_mode:
                    raise ValueError("seat_results game_mode does not match the plan")
                if result.policy_identity != assignment[seat]:
                    raise ValueError(
                        "seat_results policy assignment does not match the protocol"
                    )
                game_results.append(result)
            if tuple(sorted(result.rank for result in game_results)) != _EXPECTED_RANKS:
                raise ValueError("each game ranks must be a permutation of 1, 2, 3, 4")

    expected_a = aggregate_policy_metrics(
        plan.policy_a_identity,
        artifact.seat_results,
    )
    expected_b = aggregate_policy_metrics(
        plan.policy_b_identity,
        artifact.seat_results,
    )
    if artifact.metrics_a != expected_a:
        raise ValueError("metrics_a does not match aggregated seat_results")
    if artifact.metrics_b != expected_b:
        raise ValueError("metrics_b does not match aggregated seat_results")


def _package_version(distribution_name: str) -> str:
    try:
        return metadata.version(distribution_name)
    except metadata.PackageNotFoundError as exc:
        raise ComparisonArtifactError(
            f"package metadata is unavailable for {distribution_name!r}"
        ) from exc


def _lisjong_revision() -> str:
    try:
        direct_url_text = metadata.distribution("lisjong").read_text("direct_url.json")
    except metadata.PackageNotFoundError as exc:
        raise ComparisonArtifactError(
            "package metadata is unavailable for 'lisjong'"
        ) from exc
    if direct_url_text is None:
        raise ComparisonArtifactError(
            "lisjong direct_url.json is unavailable; source revision cannot be verified"
        )
    try:
        direct_url = json.loads(direct_url_text)
    except json.JSONDecodeError as exc:
        raise ComparisonArtifactError("lisjong direct_url.json is malformed") from exc
    if type(direct_url) is not dict or type(direct_url.get("vcs_info")) is not dict:
        raise ComparisonArtifactError(
            "lisjong VCS metadata is unavailable; source revision cannot be verified"
        )
    vcs_info = direct_url["vcs_info"]
    revision = vcs_info.get("commit_id")
    if vcs_info.get("vcs") != "git" or type(revision) is not str:
        raise ComparisonArtifactError("lisjong VCS revision metadata is malformed")
    if _FULL_COMMIT_ID(revision) is None:
        raise ComparisonArtifactError("lisjong source revision is not a full commit ID")
    return revision


def _collect_execution_provenance() -> ExecutionProvenance:
    return ExecutionProvenance(
        execution_environment=EXECUTION_ENVIRONMENT,
        lisjong_arena_version=_package_version("lisjong-arena"),
        lisjong_version=_package_version("lisjong"),
        lisjong_revision=_lisjong_revision(),
        riichienv_version=_package_version("riichienv"),
        python_version=platform.python_version(),
    )


def _artifact_from_result(result: ComparisonResult) -> ComparisonArtifact:
    if not isinstance(result, ComparisonResult):
        raise TypeError("result must be a ComparisonResult")
    plan = ArtifactPlan(
        policy_a_identity=result.plan.policy_a.identity,
        policy_b_identity=result.plan.policy_b.identity,
        seeds=result.plan.seeds,
        game_mode=result.plan.game_mode,
        max_steps=result.plan.max_steps,
    )
    return ComparisonArtifact(
        schema_version=ARTIFACT_SCHEMA_VERSION,
        comparison_protocol=COMPARISON_PROTOCOL,
        plan=plan,
        provenance=_collect_execution_provenance(),
        seat_results=result.seat_results,
        metrics_a=result.metrics_a,
        metrics_b=result.metrics_b,
    )


def _metrics_to_dict(metrics: PolicyMetrics) -> dict[str, Any]:
    return {
        "average_rank": metrics.average_rank,
        "average_score": metrics.average_score,
        "first_count": metrics.first_count,
        "fourth_count": metrics.fourth_count,
        "game_count": metrics.game_count,
        "policy_identity": metrics.policy_identity,
        "seat_result_count": metrics.seat_result_count,
        "second_count": metrics.second_count,
        "third_count": metrics.third_count,
    }


def _artifact_to_dict(artifact: ComparisonArtifact) -> dict[str, Any]:
    return {
        "comparison_protocol": artifact.comparison_protocol,
        "metrics": {
            "policy_a": _metrics_to_dict(artifact.metrics_a),
            "policy_b": _metrics_to_dict(artifact.metrics_b),
        },
        "plan": {
            "game_mode": artifact.plan.game_mode,
            "max_steps": artifact.plan.max_steps,
            "policy_a_identity": artifact.plan.policy_a_identity,
            "policy_b_identity": artifact.plan.policy_b_identity,
            "seeds": list(artifact.plan.seeds),
        },
        "provenance": {
            "execution_environment": artifact.provenance.execution_environment,
            "lisjong_arena_version": artifact.provenance.lisjong_arena_version,
            "lisjong_revision": artifact.provenance.lisjong_revision,
            "lisjong_version": artifact.provenance.lisjong_version,
            "python_version": artifact.provenance.python_version,
            "riichienv_version": artifact.provenance.riichienv_version,
        },
        "schema_version": artifact.schema_version,
        "seat_results": [
            {
                "game_mode": result.game_mode,
                "policy_identity": result.policy_identity,
                "rank": result.rank,
                "rotation": result.rotation,
                "score": result.score,
                "seat": int(result.seat),
                "seed": result.seed,
            }
            for result in artifact.seat_results
        ],
    }


def _serialize_artifact(artifact: ComparisonArtifact) -> str:
    return (
        json.dumps(
            _artifact_to_dict(artifact),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            indent=2,
        )
        + "\n"
    )


def save_comparison_artifact(
    result: ComparisonResult,
    path: str | Path,
) -> None:
    """成功したcomparisonを新しいUTF-8 JSON fileへ保存する。

    ``1 comparison = 1 immutable artifact``とするため、既存pathは上書きせず
    ``FileExistsError``を送出する。serializationと全validationはfile作成前に
    完了する。
    """
    artifact = _artifact_from_result(result)
    serialized = _serialize_artifact(artifact)
    destination = Path(path)
    created = False
    try:
        with destination.open("x", encoding="utf-8", newline="\n") as stream:
            created = True
            stream.write(serialized)
    except Exception:
        if created:
            try:
                destination.unlink()
            except OSError:
                pass
        raise


def _expect_object(
    value: object,
    expected_keys: set[str],
    context: str,
) -> dict[str, object]:
    if type(value) is not dict:
        raise ComparisonArtifactError(f"{context} must be an object")
    if set(value) != expected_keys:
        raise ComparisonArtifactError(f"{context} fields are invalid")
    return value


def _expect_list(value: object, context: str) -> list[object]:
    if type(value) is not list:
        raise ComparisonArtifactError(f"{context} must be an array")
    return value


def _expect_str(value: object, context: str) -> str:
    if type(value) is not str:
        raise ComparisonArtifactError(f"{context} must be a string")
    return value


def _expect_int(value: object, context: str) -> int:
    if type(value) is not int:
        raise ComparisonArtifactError(f"{context} must be an integer")
    return value


def _expect_float(value: object, context: str) -> float:
    if type(value) is not float:
        raise ComparisonArtifactError(f"{context} must be a JSON number with decimals")
    return value


def _parse_plan(value: object) -> ArtifactPlan:
    raw = _expect_object(
        value,
        {
            "game_mode",
            "max_steps",
            "policy_a_identity",
            "policy_b_identity",
            "seeds",
        },
        "plan",
    )
    seeds = tuple(
        _expect_int(seed, f"plan.seeds[{index}]")
        for index, seed in enumerate(_expect_list(raw["seeds"], "plan.seeds"))
    )
    return ArtifactPlan(
        policy_a_identity=_expect_str(
            raw["policy_a_identity"],
            "plan.policy_a_identity",
        ),
        policy_b_identity=_expect_str(
            raw["policy_b_identity"],
            "plan.policy_b_identity",
        ),
        seeds=seeds,
        game_mode=_expect_str(raw["game_mode"], "plan.game_mode"),
        max_steps=_expect_int(raw["max_steps"], "plan.max_steps"),
    )


def _parse_provenance(value: object) -> ExecutionProvenance:
    raw = _expect_object(
        value,
        {
            "execution_environment",
            "lisjong_arena_version",
            "lisjong_revision",
            "lisjong_version",
            "python_version",
            "riichienv_version",
        },
        "provenance",
    )
    return ExecutionProvenance(
        execution_environment=_expect_str(
            raw["execution_environment"],
            "provenance.execution_environment",
        ),
        lisjong_arena_version=_expect_str(
            raw["lisjong_arena_version"],
            "provenance.lisjong_arena_version",
        ),
        lisjong_version=_expect_str(
            raw["lisjong_version"],
            "provenance.lisjong_version",
        ),
        lisjong_revision=_expect_str(
            raw["lisjong_revision"],
            "provenance.lisjong_revision",
        ),
        riichienv_version=_expect_str(
            raw["riichienv_version"],
            "provenance.riichienv_version",
        ),
        python_version=_expect_str(
            raw["python_version"],
            "provenance.python_version",
        ),
    )


def _parse_seat_result(value: object, index: int) -> SeatResult:
    context = f"seat_results[{index}]"
    raw = _expect_object(
        value,
        {
            "game_mode",
            "policy_identity",
            "rank",
            "rotation",
            "score",
            "seat",
            "seed",
        },
        context,
    )
    seat_number = _expect_int(raw["seat"], f"{context}.seat")
    try:
        seat = Seat(seat_number)
    except ValueError as exc:
        raise ComparisonArtifactError(f"{context}.seat is invalid") from exc
    return SeatResult(
        seed=_expect_int(raw["seed"], f"{context}.seed"),
        rotation=_expect_int(raw["rotation"], f"{context}.rotation"),
        game_mode=_expect_str(raw["game_mode"], f"{context}.game_mode"),
        seat=seat,
        policy_identity=_expect_str(
            raw["policy_identity"],
            f"{context}.policy_identity",
        ),
        score=_expect_int(raw["score"], f"{context}.score"),
        rank=_expect_int(raw["rank"], f"{context}.rank"),
    )


def _parse_metrics(value: object, context: str) -> PolicyMetrics:
    raw = _expect_object(
        value,
        {
            "average_rank",
            "average_score",
            "first_count",
            "fourth_count",
            "game_count",
            "policy_identity",
            "seat_result_count",
            "second_count",
            "third_count",
        },
        context,
    )
    return PolicyMetrics(
        policy_identity=_expect_str(
            raw["policy_identity"],
            f"{context}.policy_identity",
        ),
        game_count=_expect_int(raw["game_count"], f"{context}.game_count"),
        seat_result_count=_expect_int(
            raw["seat_result_count"],
            f"{context}.seat_result_count",
        ),
        average_rank=_expect_float(
            raw["average_rank"],
            f"{context}.average_rank",
        ),
        average_score=_expect_float(
            raw["average_score"],
            f"{context}.average_score",
        ),
        first_count=_expect_int(raw["first_count"], f"{context}.first_count"),
        second_count=_expect_int(raw["second_count"], f"{context}.second_count"),
        third_count=_expect_int(raw["third_count"], f"{context}.third_count"),
        fourth_count=_expect_int(raw["fourth_count"], f"{context}.fourth_count"),
    )


def _parse_artifact(value: object) -> ComparisonArtifact:
    raw = _expect_object(
        value,
        {
            "comparison_protocol",
            "metrics",
            "plan",
            "provenance",
            "schema_version",
            "seat_results",
        },
        "artifact",
    )
    schema_version = _expect_int(raw["schema_version"], "schema_version")
    if schema_version != ARTIFACT_SCHEMA_VERSION:
        raise ComparisonArtifactError(f"unsupported schema version: {schema_version!r}")
    comparison_protocol = _expect_str(
        raw["comparison_protocol"],
        "comparison_protocol",
    )
    if comparison_protocol != COMPARISON_PROTOCOL:
        raise ComparisonArtifactError(
            f"unsupported comparison protocol: {comparison_protocol!r}"
        )
    metrics = _expect_object(
        raw["metrics"],
        {"policy_a", "policy_b"},
        "metrics",
    )
    return ComparisonArtifact(
        schema_version=schema_version,
        comparison_protocol=comparison_protocol,
        plan=_parse_plan(raw["plan"]),
        provenance=_parse_provenance(raw["provenance"]),
        seat_results=tuple(
            _parse_seat_result(result, index)
            for index, result in enumerate(
                _expect_list(raw["seat_results"], "seat_results")
            )
        ),
        metrics_a=_parse_metrics(metrics["policy_a"], "metrics.policy_a"),
        metrics_b=_parse_metrics(metrics["policy_b"], "metrics.policy_b"),
    )


def _reject_json_constant(value: str) -> None:
    raise ComparisonArtifactError(f"non-finite JSON number is not allowed: {value}")


def _reject_duplicate_object_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    """JSON objectのduplicate keyをlast-winsで解釈せず拒否する。"""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ComparisonArtifactError(f"duplicate JSON object key: {key!r}")
        result[key] = value
    return result


def load_comparison_artifact(path: str | Path) -> ComparisonArtifact:
    """JSON fileをfail-closedに検証してimmutable artifact snapshotを返す。"""
    source = Path(path)
    try:
        serialized = source.read_text(encoding="utf-8")
    except UnicodeError as exc:
        raise ComparisonArtifactError("artifact is not valid UTF-8") from exc
    try:
        value = json.loads(
            serialized,
            parse_constant=_reject_json_constant,
            object_pairs_hook=_reject_duplicate_object_keys,
        )
        return _parse_artifact(value)
    except ComparisonArtifactError:
        raise
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ComparisonArtifactError("artifact is malformed or inconsistent") from exc


__all__ = [
    "ARTIFACT_SCHEMA_VERSION",
    "COMPARISON_PROTOCOL",
    "ArtifactPlan",
    "ComparisonArtifact",
    "ComparisonArtifactError",
    "ExecutionProvenance",
    "load_comparison_artifact",
    "save_comparison_artifact",
]
