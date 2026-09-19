"""Issue #250 write-once Overall pre-execution lock (``overall-lock.json``).

formal Overall eventはresult exposure前にparticipant / population / protocol /
provenanceをmachine-readableへ固定する。ここはそのlockだけを所有し、generic
experiment-lock frameworkもChampion registryも導入しない。

merged-main execution disciplineは既存``lisjong_arena._execution_safety``を
そのままreuseする。PR branch上でformal resultを作れないよう、lock生成時と
実行直前の双方でclean worktree、merged revision containment、write-once
destinationをfail closedに確認する。

generic ``ComparisonArtifact``はこのIssueのためだけに拡張しない。generic
artifactが既に持つprovenance fieldはlockとcross-checkし、Arena revision /
lisjong-engine revisionのようなOverall固有の強いbindingはこのlockが保持する。
"""

from __future__ import annotations

import hashlib
import platform
import sys
from importlib import metadata
from pathlib import Path

from lisjong_arena._artifact_io import (
    ArtifactValidationError,
    canonical_json_text,
    expect_bool,
    expect_int,
    expect_object,
    expect_str,
    read_json_document,
    sha256_bytes,
    write_new_artifact_file,
)
from lisjong_arena._execution_safety import (
    ExecutionSafetyError,
    require_clean_arena_head,
    require_merged_arena_revision,
    require_new_artifact_destinations,
)
from lisjong_arena.artifact import ExecutionProvenance
from lisjong_arena.environment_identity import (
    EnvironmentIdentityError,
    verify_environment,
)
from lisjong_arena.single_round_artifact import (
    SingleRoundExecutionProvenance,
    collect_execution_provenance,
    execution_provenance_to_dict,
    parse_execution_provenance,
)

from .protocol import (
    EXECUTION_BRANCH,
    HEURISTIC_FAMILY,
    IMPLEMENTATION_SOURCES,
    LEARNING_FAMILY,
    OverallChampionProtocolError,
    ParticipantBinding,
    ServedCheckpoint,
    parse_participant_binding,
    protocol_document,
    require_overall_population,
    require_participants,
    require_protocol_document,
    resolve_binding_callable,
)

LOCK_VERSION = 1
EXECUTION_TARGET_TYPE = "reviewed-merged-main-v1"

REQUIRED_DESTINATIONS = ("comparison_artifact", "overall_result")
"""bundleのwrite-once destination。``overall-lock.json``自体は別pathへ書く。"""

NO_RESCUE_BOUNDARY = (
    "locked 100 seed blocks are never extended, replaced, or dropped after "
    "result exposure",
    "the AABB rotation plan is never replaced after result exposure",
    "the primary statistic is never switched from average rank to final score",
    "the interval method and classification threshold remain fixed",
    "another population is never rerun to break an inconclusive result",
    "partial or failed execution is never partially adopted",
    "secondary score diagnostics never override the primary classification",
    "OVERALL INCONCLUSIVE is a valid terminal outcome, not permission to add seeds",
    "a future sample-size change is a new protocol revision, never a rescue of "
    "this event",
)

_PROJECT_PATH = Path(__file__).resolve().parents[3] / "pyproject.toml"


class OverallChampionLockError(ValueError):
    """Issue #250 pre-execution lockを生成または消費できない場合。"""


def document_identity(payload: dict[str, object]) -> str:
    """canonical JSON bytesのSHA-256をdocument identityとする。"""
    return hashlib.sha256(canonical_json_text(payload).encode("utf-8")).hexdigest()


def collect_ml_runtime(distribution_names: object = ()) -> dict[str, str]:
    """Learning participantが依存するML runtimeのexact versionを収集する。

    どのruntimeが``relevant``かはparticipantごとに異なるため、package名は
    callerが宣言する。宣言されたpackageが見つからない場合はunknownで埋めず
    fail closedする。Learning Championがpure-Python実装ならここは空でよい。
    """
    if isinstance(distribution_names, (str, bytes, bytearray)):
        raise TypeError("ml_runtime_packages must be an ordered collection of names")
    try:
        names = tuple(distribution_names)  # type: ignore[arg-type]
    except TypeError:
        raise TypeError(
            "ml_runtime_packages must be an ordered collection of names"
        ) from None
    if any(type(name) is not str or not name for name in names):
        raise TypeError("ml_runtime_packages must contain only non-empty names")
    if len(set(names)) != len(names):
        raise OverallChampionLockError("ml_runtime_packages must not repeat a package")
    runtime: dict[str, str] = {}
    for name in sorted(names):
        try:
            runtime[name] = metadata.version(name)
        except metadata.PackageNotFoundError as exc:
            raise OverallChampionLockError(
                f"declared ML runtime package {name!r} is not installed"
            ) from exc
    return runtime


def _runtime_document() -> dict[str, object]:
    return {
        "python_implementation": platform.python_implementation(),
        "python_version": platform.python_version(),
        "sys_version": sys.version.split()[0],
    }


def _require_environment_consistent() -> None:
    try:
        result = verify_environment(_PROJECT_PATH)
    except EnvironmentIdentityError as exc:
        raise OverallChampionLockError(str(exc)) from exc
    if not result.ok:
        raise OverallChampionLockError(
            "internal VCS dependency environment is inconsistent: "
            + "; ".join(result.errors)
        )


def _require_participant_source(
    binding: ParticipantBinding,
    provenance: SingleRoundExecutionProvenance,
) -> None:
    """1 participantのimplementation / checkpoint bindingをliveへ照合する。

    - ``implementation_revision``は``implementation_source``が指すexecution
      provenance factと完全一致しなければならない
    - checkpointを持つparticipantでは、``checkpoint_binding``が指すserving側の
      申告(``ServedCheckpoint``)を実際に呼び出し、identityの一致と、file bytes
      から再計算したSHA-256の一致を要求する

    Arenaはcheckpointを探索しない。呼ぶのはlockがexactにbindした1点だけで、
    そこから得た事実だけを照合する。
    """
    field = IMPLEMENTATION_SOURCES[binding.implementation_source]
    live_revision = getattr(provenance, field)
    if binding.implementation_revision != live_revision:
        raise OverallChampionLockError(
            f"{binding.family} participant implementation revision does not match "
            f"the live {binding.implementation_source} revision"
        )
    if not binding.has_checkpoint:
        return

    try:
        resolver = resolve_binding_callable(binding.checkpoint_binding)
    except OverallChampionProtocolError as exc:
        raise OverallChampionLockError(
            f"{binding.family} participant checkpoint binding is unusable: {exc}"
        ) from exc
    try:
        served = resolver()
    except Exception as exc:
        raise OverallChampionLockError(
            f"{binding.family} participant checkpoint binding could not report "
            "the served checkpoint"
        ) from exc
    if not isinstance(served, ServedCheckpoint):
        raise OverallChampionLockError(
            f"{binding.family} participant checkpoint binding must return a "
            "ServedCheckpoint"
        )
    if served.identity != binding.checkpoint_identity:
        raise OverallChampionLockError(
            f"{binding.family} participant serves checkpoint identity "
            f"{served.identity!r}, not the bound {binding.checkpoint_identity!r}"
        )
    try:
        payload = served.path.read_bytes()
    except OSError as exc:
        raise OverallChampionLockError(
            f"{binding.family} participant served checkpoint cannot be read"
        ) from exc
    if sha256_bytes(payload) != binding.checkpoint_digest:
        raise OverallChampionLockError(
            f"{binding.family} participant served checkpoint digest does not "
            "match the bound digest"
        )


def require_participant_sources(
    heuristic: ParticipantBinding,
    learning: ParticipantBinding,
    provenance: SingleRoundExecutionProvenance,
) -> None:
    """両participantのsource bindingをliveへfail-closedでcross-bindする。"""
    for binding in require_participants(heuristic, learning):
        _require_participant_source(binding, provenance)


def require_live_ml_runtime(document: dict[str, object]) -> dict[str, str]:
    """lockされたML runtimeをlive環境から再取得し、exact一致を要求する。

    lock後のversion driftとpackage消失はどちらもここでrejectする。宣言された
    package集合そのものはlock identityが固定するため、宣言外のpackageを
    Arenaが推測で列挙することはしない。
    """
    parsed = parse_lock_document(document)
    locked = parsed["ml_runtime"]
    assert isinstance(locked, dict)
    live = collect_ml_runtime(tuple(locked))
    if live != locked:
        raise OverallChampionLockError(
            "live ML runtime differs from the pre-execution lock "
            f"(locked={dict(sorted(locked.items()))!r}, "
            f"live={dict(sorted(live.items()))!r})"
        )
    return live


def build_lock_document(
    *,
    destinations: dict[str, str | Path],
    heuristic: ParticipantBinding,
    learning: ParticipantBinding,
    seeds: object,
    max_workers: int,
    ml_runtime_packages: object = (),
) -> dict[str, object]:
    """result exposure前のOverall lock documentを構築する。

    生成時点でmerged-main execution disciplineとwrite-once destinationを
    確認するので、PR branchやdirty worktreeからformal lockを作れない。
    """
    if type(max_workers) is not int or max_workers <= 0:
        raise OverallChampionLockError("max_workers must be a positive int")
    try:
        heuristic_binding, learning_binding = require_participants(heuristic, learning)
        ordered = require_overall_population(seeds)
    except OverallChampionProtocolError as exc:
        raise OverallChampionLockError(str(exc)) from exc
    ml_runtime = collect_ml_runtime(ml_runtime_packages)

    try:
        _require_environment_consistent()
        provenance = collect_execution_provenance()
        head = require_clean_arena_head()
        if head != provenance.lisjong_arena_revision:
            raise OverallChampionLockError(
                "Arena HEAD differs from collected execution provenance"
            )
        require_merged_arena_revision(head, branch=EXECUTION_BRANCH)
        require_new_artifact_destinations(
            destinations, required_names=REQUIRED_DESTINATIONS
        )
    except ExecutionSafetyError as exc:
        raise OverallChampionLockError(str(exc)) from exc
    require_participant_sources(heuristic_binding, learning_binding, provenance)

    payload: dict[str, object] = {
        "artifact_destinations": {
            name: str(destinations[name]) for name in REQUIRED_DESTINATIONS
        },
        "execution_target": {
            "branch": EXECUTION_BRANCH,
            "revision": head,
            "target_type": EXECUTION_TARGET_TYPE,
        },
        "lock_version": LOCK_VERSION,
        "max_workers": max_workers,
        "ml_runtime": ml_runtime,
        "no_rescue_boundary": list(NO_RESCUE_BOUNDARY),
        "participants": {
            HEURISTIC_FAMILY: heuristic_binding.to_document(),
            LEARNING_FAMILY: learning_binding.to_document(),
        },
        "protocol": protocol_document(ordered),
        "provenance": execution_provenance_to_dict(provenance),
        "result_exposed": False,
        "runtime": _runtime_document(),
    }
    document = dict(payload)
    document["lock_identity"] = document_identity(payload)
    return document


def save_lock_document(document: dict[str, object], path: str | Path) -> Path:
    """lockをwrite-once JSON fileへ保存する。既存pathは上書きしない。"""
    destination = Path(path)
    try:
        require_new_artifact_destinations(
            {"lock": destination}, required_names=("lock",)
        )
    except ExecutionSafetyError as exc:
        raise OverallChampionLockError(str(exc)) from exc
    write_new_artifact_file(destination, canonical_json_text(document))
    return destination


_LOCK_FIELDS = {
    "artifact_destinations",
    "execution_target",
    "lock_identity",
    "lock_version",
    "max_workers",
    "ml_runtime",
    "no_rescue_boundary",
    "participants",
    "protocol",
    "provenance",
    "result_exposed",
    "runtime",
}


def parse_lock_document(value: object) -> dict[str, object]:
    """lock documentをstrictに検証する。不明schema / 欠損fieldは拒否する。"""
    try:
        return _parse_lock_document(value)
    except OverallChampionLockError, OverallChampionProtocolError:
        raise
    except ArtifactValidationError as exc:
        raise OverallChampionLockError(str(exc)) from exc
    except (TypeError, ValueError, KeyError) as exc:
        raise OverallChampionLockError(str(exc)) from exc


def _parse_lock_document(value: object) -> dict[str, object]:
    raw = expect_object(value, _LOCK_FIELDS, "lock")
    if expect_int(raw["lock_version"], "lock.lock_version") != LOCK_VERSION:
        raise OverallChampionLockError("unsupported lock version")
    if expect_bool(raw["result_exposed"], "lock.result_exposed"):
        raise OverallChampionLockError(
            "pre-execution lock must record result_exposed=false"
        )
    max_workers = expect_int(raw["max_workers"], "lock.max_workers")
    if max_workers <= 0:
        raise OverallChampionLockError("lock max_workers must be positive")

    require_protocol_document(raw["protocol"], "lock.protocol")

    participants = expect_object(
        raw["participants"],
        {HEURISTIC_FAMILY, LEARNING_FAMILY},
        "lock.participants",
    )
    heuristic = parse_participant_binding(
        participants[HEURISTIC_FAMILY], f"lock.participants.{HEURISTIC_FAMILY}"
    )
    learning = parse_participant_binding(
        participants[LEARNING_FAMILY], f"lock.participants.{LEARNING_FAMILY}"
    )
    require_participants(heuristic, learning)

    if raw["no_rescue_boundary"] != list(NO_RESCUE_BOUNDARY):
        raise OverallChampionLockError("lock no-rescue boundary drifted")

    ml_runtime = raw["ml_runtime"]
    if type(ml_runtime) is not dict:
        raise OverallChampionLockError("lock.ml_runtime must be an object")
    for name, version in ml_runtime.items():
        expect_str(name, "lock.ml_runtime key")
        expect_str(version, f"lock.ml_runtime.{name}")

    execution_target = expect_object(
        raw["execution_target"],
        {"branch", "revision", "target_type"},
        "lock.execution_target",
    )
    if (
        expect_str(execution_target["branch"], "lock.execution_target.branch")
        != EXECUTION_BRANCH
    ):
        raise OverallChampionLockError("lock execution branch drifted")
    if (
        expect_str(execution_target["target_type"], "lock.execution_target.target_type")
        != EXECUTION_TARGET_TYPE
    ):
        raise OverallChampionLockError("lock execution target type drifted")

    provenance = parse_execution_provenance(raw["provenance"])
    if execution_target["revision"] != provenance.lisjong_arena_revision:
        raise OverallChampionLockError(
            "lock execution revision differs from recorded provenance"
        )

    destinations = expect_object(
        raw["artifact_destinations"],
        set(REQUIRED_DESTINATIONS),
        "lock.artifact_destinations",
    )
    resolved: dict[Path, str] = {}
    for name in REQUIRED_DESTINATIONS:
        path = Path(
            expect_str(destinations[name], f"lock.artifact_destinations.{name}")
        ).resolve(strict=False)
        if path in resolved:
            raise OverallChampionLockError(
                f"locked outputs {resolved[path]} and {name} refer to the same path"
            )
        resolved[path] = name

    runtime = expect_object(
        raw["runtime"],
        {"python_implementation", "python_version", "sys_version"},
        "lock.runtime",
    )
    for name in ("python_implementation", "python_version", "sys_version"):
        expect_str(runtime[name], f"lock.runtime.{name}")

    payload = {key: raw[key] for key in raw if key != "lock_identity"}
    if expect_str(raw["lock_identity"], "lock.lock_identity") != document_identity(
        payload
    ):
        raise OverallChampionLockError("lock identity mismatch")
    return dict(raw)


def load_lock_document(path: str | Path) -> dict[str, object]:
    """lock fileをstrict-readする。"""
    try:
        return parse_lock_document(read_json_document(Path(path)))
    except OverallChampionLockError, OverallChampionProtocolError:
        raise
    except ArtifactValidationError as exc:
        raise OverallChampionLockError(str(exc)) from exc
    except (TypeError, ValueError, KeyError) as exc:
        raise OverallChampionLockError(str(exc)) from exc


def locked_participants(
    document: dict[str, object],
) -> tuple[ParticipantBinding, ParticipantBinding]:
    parsed = parse_lock_document(document)
    participants = parsed["participants"]
    assert isinstance(participants, dict)
    return (
        parse_participant_binding(
            participants[HEURISTIC_FAMILY], f"lock.participants.{HEURISTIC_FAMILY}"
        ),
        parse_participant_binding(
            participants[LEARNING_FAMILY], f"lock.participants.{LEARNING_FAMILY}"
        ),
    )


def locked_seeds(document: dict[str, object]) -> tuple[int, ...]:
    parsed = parse_lock_document(document)
    protocol = parsed["protocol"]
    assert isinstance(protocol, dict)
    return require_overall_population(tuple(protocol["ordered_seeds"]))  # type: ignore[arg-type]


def locked_max_workers(document: dict[str, object]) -> int:
    parsed = parse_lock_document(document)
    return int(parsed["max_workers"])  # type: ignore[arg-type]


def locked_destinations(document: dict[str, object]) -> dict[str, Path]:
    parsed = parse_lock_document(document)
    raw = parsed["artifact_destinations"]
    assert isinstance(raw, dict)
    return {name: Path(str(raw[name])) for name in REQUIRED_DESTINATIONS}


def locked_provenance(document: dict[str, object]) -> SingleRoundExecutionProvenance:
    parsed = parse_lock_document(document)
    return parse_execution_provenance(parsed["provenance"])


def require_live_execution_target(
    document: dict[str, object],
) -> SingleRoundExecutionProvenance:
    """実行直前のlive targetがlockと完全一致することを要求する。

    formal executionはreview済みmerged implementationを対象とし、PR branchや
    dirty worktreeからは実行できない。

    ここはexecution preflightの単一境界であり、次をすべてfail closedにする。

    - internal VCS dependency環境の整合
    - execution provenance(Arena / lisjong / lisjong-engine revision等)の一致
    - clean worktreeとmerged main containment
    - 宣言されたML runtime versionの一致(lock後のdriftを拒否)
    - participant implementation revisionとserved checkpointの一致
    """
    locked = locked_provenance(document)
    try:
        _require_environment_consistent()
        live = collect_execution_provenance()
        head = require_clean_arena_head()
        require_merged_arena_revision(head, branch=EXECUTION_BRANCH)
    except ExecutionSafetyError as exc:
        raise OverallChampionLockError(str(exc)) from exc
    if live != locked or head != locked.lisjong_arena_revision:
        raise OverallChampionLockError(
            "live execution target differs from the pre-execution lock"
        )
    require_live_ml_runtime(document)
    heuristic, learning = locked_participants(document)
    require_participant_sources(heuristic, learning, live)
    return live


def require_comparison_provenance(
    document: dict[str, object],
    provenance: ExecutionProvenance,
) -> None:
    """generic comparison artifactのprovenanceをlockとcross-bindする。

    generic ``ComparisonArtifact``は``ExecutionProvenance``しか持たないため、
    ここで照合するのは両者に共通するfieldだけである。Arena revisionや
    lisjong-engine revisionのようなOverall固有の強いbindingはlock側が保持し、
    generic artifact schemaをこのIssueのために拡張しない。
    """
    if not isinstance(provenance, ExecutionProvenance):
        raise OverallChampionLockError("provenance must be an ExecutionProvenance")
    locked = locked_provenance(document)
    for name in (
        "execution_environment",
        "lisjong_arena_version",
        "lisjong_version",
        "lisjong_revision",
        "riichienv_version",
        "python_version",
    ):
        if getattr(provenance, name) != getattr(locked, name):
            raise OverallChampionLockError(
                f"comparison artifact provenance {name} differs from the "
                "pre-execution lock"
            )


__all__ = [
    "EXECUTION_TARGET_TYPE",
    "LOCK_VERSION",
    "NO_RESCUE_BOUNDARY",
    "REQUIRED_DESTINATIONS",
    "OverallChampionLockError",
    "build_lock_document",
    "collect_ml_runtime",
    "document_identity",
    "load_lock_document",
    "locked_destinations",
    "locked_max_workers",
    "locked_participants",
    "locked_provenance",
    "locked_seeds",
    "parse_lock_document",
    "require_comparison_provenance",
    "require_live_ml_runtime",
    "require_participant_sources",
    "require_live_execution_target",
    "save_lock_document",
]
