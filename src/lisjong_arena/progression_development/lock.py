"""Issue #252のpre-execution lock document生成とvalidation。

merge後のoperatorが、Issue #252本文どおりのlockをreviewed merged main上で
1回だけ作れるようにするための、purpose-specificなschemaとvalidatorである。

## 実行境界

lock生成は現行Arenaのone-shot execution lock disciplineをthin reuseし、

```text
worktree clean
HEAD == collected provenance revision
HEAD is contained in the merged long-lived branch (default: main)
artifact destinationsが未作成 / write-once
```

を満たさない限りfail closedする。したがってPR branch(未mergeのHEAD)からは
real lockを作れず、real scientific executionも開始できない。

## lockが束ねるもの

`result_exposed = false`、Arena / lisjong / lisjong-engine revision、runtime
version、P / C / T exact identity、#170 adaptive semanticsであることと#171
clairvoyant semanticsが無いこと、Phase A / Phase B population、worker sweep、
8時間のfeasibility閾値、paired primary statisticとclassification rule、
artifact destinations、no-rescue boundaryである。
"""

from __future__ import annotations

import platform
import re
import sys
from pathlib import Path

from lisjong_arena._artifact_io import (
    ArtifactValidationError,
    canonical_json_text,
    expect_bool,
    expect_object,
    expect_str,
    read_json_document,
    write_new_artifact_file,
)
from lisjong_arena._execution_safety import (
    ExecutionSafetyError,
    require_clean_arena_head,
    require_merged_arena_revision,
    require_new_artifact_destinations,
)
from lisjong_arena.single_round_artifact import (
    SingleRoundExecutionProvenance,
    collect_execution_provenance,
    execution_provenance_to_dict,
    parse_execution_provenance,
)

from .protocol import (
    CANDIDATE_IDENTITY,
    COMPARATOR_IDENTITY,
    FEASIBILITY_WALL_CLOCK_LIMIT_HOURS,
    PARENT_IDENTITY,
    PHASE_A_GAME_COUNT,
    PHASE_A_SEEDS,
    PHASE_B_GAMES_PER_ARM,
    PHASE_B_SEEDS,
    PHASE_B_TOTAL_GAMES,
    WORKER_SWEEP,
    document_identity,
    protocol_document,
    require_disjoint_populations,
    require_exact_candidate_semantics,
    require_exact_comparator,
)

LOCK_VERSION = 1
"""pre-execution lock documentのschema version。"""

EXECUTION_TARGET_TYPE = "reviewed-merged-main-v1"
"""lockが要求する実行対象の種類。"""

_FULL_COMMIT_ID = re.compile(r"[0-9a-f]{40}\Z").fullmatch

REQUIRED_DESTINATIONS = (
    "feasibility_record",
    "candidate_artifact",
    "parent_artifact",
    "paired_result",
)
"""lockがbindする書き込み先の名前(すべてwrite-once)。"""

NO_RESCUE_BOUNDARY = (
    "no seed replacement after Phase A result exposure",
    "no seed extension after Phase A result exposure",
    "no horizon / objective / fallback / defense / call change",
    "no clairvoyant or draw-multiset approximation",
    "no comparator change",
    "technical feasibility seeds are never reused as development evidence",
)
"""Issue #252が事前登録したno-rescue boundary。"""

_LOCK_FIELDS = {
    "artifact_destinations",
    "candidate_binding",
    "classification",
    "comparator_binding",
    "execution_target",
    "lock_identity",
    "lock_version",
    "no_rescue_boundary",
    "parent_identity",
    "phase_a",
    "phase_b",
    "protocol",
    "provenance",
    "result_exposed",
    "runtime",
}


class ProgressionLockError(ValueError):
    """lockを生成または検証できない場合。"""


def _runtime_document() -> dict[str, object]:
    return {
        "python_implementation": platform.python_implementation(),
        "python_version": platform.python_version(),
        "sys_version": sys.version.split()[0],
    }


def _classification_document() -> dict[str, object]:
    return {
        "primary_statistic": (
            "mean of D_s over the locked seed blocks, where "
            "D_s = mean focal-seat score of the candidate arm's four rotations "
            "minus the same quantity for the parent arm"
        ),
        "primary_unit": "ordered seed block",
        "rule": (
            "interval lower > 0 -> SIGNAL; "
            "interval upper < 0 -> NEGATIVE; "
            "otherwise -> INCONCLUSIVE"
        ),
        "sample_size_boundary": (
            "the 400 candidate games and 400 parent games are never treated as "
            "800 independent primary observations"
        ),
    }


def build_lock_document(
    destinations: dict[str, str | Path],
    *,
    branch: str = "main",
) -> dict[str, object]:
    """reviewed merged main上でだけ成立するlock documentを組み立てる。

    clean worktree、merged revision containment、write-once destinationsの
    いずれかが崩れていればfail closedする。PR branchからreal lockは作れない。
    """
    require_disjoint_populations()
    candidate_binding = require_exact_candidate_semantics().to_document()
    comparator_binding = require_exact_comparator()
    try:
        provenance = collect_execution_provenance()
        head = require_clean_arena_head()
        if head != provenance.lisjong_arena_revision:
            raise ProgressionLockError(
                "Arena HEAD differs from the collected execution provenance"
            )
        require_merged_arena_revision(head, branch=branch)
        require_new_artifact_destinations(
            destinations, required_names=REQUIRED_DESTINATIONS
        )
    except ExecutionSafetyError as exc:
        raise ProgressionLockError(str(exc)) from exc

    payload: dict[str, object] = {
        "artifact_destinations": {
            name: str(destinations[name]) for name in REQUIRED_DESTINATIONS
        },
        "candidate_binding": candidate_binding,
        "classification": _classification_document(),
        "comparator_binding": comparator_binding,
        "execution_target": {
            "branch": branch,
            "revision": head,
            "target_type": EXECUTION_TARGET_TYPE,
        },
        "lock_version": LOCK_VERSION,
        "no_rescue_boundary": list(NO_RESCUE_BOUNDARY),
        "parent_identity": PARENT_IDENTITY,
        "phase_a": {
            "game_count": PHASE_A_GAME_COUNT,
            "ordered_seeds": list(PHASE_A_SEEDS),
            "role": "TECHNICAL FEASIBILITY ONLY",
            "wall_clock_limit_hours": FEASIBILITY_WALL_CLOCK_LIMIT_HOURS,
            "worker_sweep": list(WORKER_SWEEP),
        },
        "phase_b": {
            "games_per_arm": PHASE_B_GAMES_PER_ARM,
            "ordered_seeds": list(PHASE_B_SEEDS),
            "role": "DEVELOPMENT OFFENSIVE-EFFICIENCY SCREEN",
            "seed_block_count": len(PHASE_B_SEEDS),
            "total_games": PHASE_B_TOTAL_GAMES,
        },
        "protocol": protocol_document(),
        "provenance": execution_provenance_to_dict(provenance),
        "result_exposed": False,
        "runtime": _runtime_document(),
    }
    document = dict(payload)
    document["lock_identity"] = document_identity(payload)
    return document


def save_lock_document(document: dict[str, object], path: str | Path) -> Path:
    """lock documentをwrite-onceで保存する。"""
    if not isinstance(document, dict):
        raise TypeError("document must be a dict")
    destination = Path(path)
    require_new_artifact_destinations({"lock": destination}, required_names=("lock",))
    write_new_artifact_file(destination, canonical_json_text(document))
    return destination


def _require_equal(actual: object, expected: object, context: str) -> None:
    if actual != expected:
        raise ProgressionLockError(f"lock {context} does not match the locked contract")


def parse_lock_document(value: object) -> dict[str, object]:
    """strict readbackでlockを読み戻し、locked contractへ再bindする。

    field presenceとcontent hashだけでは、documentを書き換えたうえで
    ``lock_identity``を再計算したlockを弾けない。そのためcandidate /
    comparator / parent binding、protocol、Phase A / Phase B population、
    classification rule、no-rescue boundary、execution target、provenance、
    artifact destinationsを、このrevisionのpurpose-specific contractそのものへ
    再bindしてからidentityを確認する。
    """
    document = expect_object(value, _LOCK_FIELDS, "lock")
    version = document["lock_version"]
    if type(version) is not int:
        raise ProgressionLockError("lock_version must be an int")
    if version != LOCK_VERSION:
        raise ProgressionLockError(f"unsupported lock version: {version!r}")
    if expect_bool(document["result_exposed"], "result_exposed"):
        raise ProgressionLockError(
            "a pre-execution lock must record result_exposed = false"
        )

    protocol = expect_object(document["protocol"], set(protocol_document()), "protocol")
    _require_equal(protocol, protocol_document(), "protocol block")

    candidate = expect_object(
        document["candidate_binding"],
        set(require_exact_candidate_semantics().to_document()),
        "candidate_binding",
    )
    _require_equal(
        candidate,
        require_exact_candidate_semantics().to_document(),
        "candidate binding",
    )
    if candidate["identity"] != CANDIDATE_IDENTITY:
        raise ProgressionLockError("lock candidate identity is not the locked P")
    if candidate["clairvoyant_semantics_present"] is not False:
        raise ProgressionLockError(
            "lock must record that clairvoyant semantics are absent"
        )

    comparator = expect_object(
        document["comparator_binding"],
        set(require_exact_comparator()),
        "comparator_binding",
    )
    _require_equal(comparator, require_exact_comparator(), "comparator binding")
    if comparator["identity"] != COMPARATOR_IDENTITY:
        raise ProgressionLockError("lock comparator identity is not the locked T")
    if expect_str(document["parent_identity"], "parent_identity") != PARENT_IDENTITY:
        raise ProgressionLockError("lock parent identity is not the locked C")

    phase_a = expect_object(
        document["phase_a"],
        {
            "game_count",
            "ordered_seeds",
            "role",
            "wall_clock_limit_hours",
            "worker_sweep",
        },
        "phase_a",
    )
    _require_equal(phase_a["ordered_seeds"], list(PHASE_A_SEEDS), "Phase A population")
    _require_equal(phase_a["game_count"], PHASE_A_GAME_COUNT, "Phase A game count")
    _require_equal(phase_a["worker_sweep"], list(WORKER_SWEEP), "worker sweep")
    _require_equal(
        phase_a["wall_clock_limit_hours"],
        FEASIBILITY_WALL_CLOCK_LIMIT_HOURS,
        "feasibility wall-clock bound",
    )

    phase_b = expect_object(
        document["phase_b"],
        {
            "games_per_arm",
            "ordered_seeds",
            "role",
            "seed_block_count",
            "total_games",
        },
        "phase_b",
    )
    _require_equal(phase_b["ordered_seeds"], list(PHASE_B_SEEDS), "Phase B population")
    _require_equal(
        phase_b["games_per_arm"], PHASE_B_GAMES_PER_ARM, "Phase B games per arm"
    )
    _require_equal(phase_b["total_games"], PHASE_B_TOTAL_GAMES, "Phase B total games")
    _require_equal(
        phase_b["seed_block_count"], len(PHASE_B_SEEDS), "Phase B seed block count"
    )

    _require_equal(
        document["classification"], _classification_document(), "classification rule"
    )
    _require_equal(
        document["no_rescue_boundary"],
        list(NO_RESCUE_BOUNDARY),
        "no-rescue boundary",
    )

    execution_target = expect_object(
        document["execution_target"],
        {"branch", "revision", "target_type"},
        "execution_target",
    )
    if _FULL_COMMIT_ID(expect_str(execution_target["revision"], "revision")) is None:
        raise ProgressionLockError(
            "lock execution target revision must be a lowercase full commit ID"
        )
    _require_equal(
        execution_target["target_type"], EXECUTION_TARGET_TYPE, "execution target type"
    )

    provenance = parse_execution_provenance(document["provenance"])
    if provenance.lisjong_arena_revision != execution_target["revision"]:
        raise ProgressionLockError(
            "lock provenance revision differs from the locked execution target"
        )

    destinations = expect_object(
        document["artifact_destinations"],
        set(REQUIRED_DESTINATIONS),
        "artifact_destinations",
    )
    for name in REQUIRED_DESTINATIONS:
        if not expect_str(destinations[name], f"artifact_destinations.{name}"):
            raise ProgressionLockError(
                f"lock artifact destination {name} must not be empty"
            )

    identity = expect_str(document["lock_identity"], "lock_identity")
    payload = {name: item for name, item in document.items() if name != "lock_identity"}
    if document_identity(payload) != identity:
        raise ProgressionLockError("lock identity does not match its content")
    return document


def load_lock_document(path: str | Path) -> dict[str, object]:
    """保存済みlockをstrict readbackで読み戻す。"""
    try:
        return parse_lock_document(read_json_document(Path(path)))
    except ArtifactValidationError as exc:
        raise ProgressionLockError(str(exc)) from exc


def locked_execution_revision(document: dict[str, object]) -> str:
    """lockが束ねたexecution target revisionを返す。"""
    return str(document["execution_target"]["revision"])  # type: ignore[index]


def locked_destination(document: dict[str, object], name: str) -> Path:
    """lockが束ねた書き込み先の1つをPathで返す。"""
    if name not in REQUIRED_DESTINATIONS:
        raise ProgressionLockError(f"unknown locked destination: {name!r}")
    return Path(str(document["artifact_destinations"][name]))  # type: ignore[index]


def require_locked_destination(
    document: dict[str, object], name: str, candidate: str | Path
) -> Path:
    """実際の書き込み先がlock済みdestinationとexact一致することを要求する。"""
    locked = locked_destination(document, name)
    given = Path(candidate)
    if locked.resolve(strict=False) != given.resolve(strict=False):
        raise ProgressionLockError(
            f"output {name} is not the locked destination; "
            "a locked run cannot write somewhere else"
        )
    return given


def require_live_execution_target(
    document: dict[str, object],
) -> SingleRoundExecutionProvenance:
    """live実行環境がlocked execution targetとexact一致することを要求する。

    game 1より前にここでfail closedするため、未mergeのPR branch(HEADが
    locked revisionと異なる)や、lock生成後にrevisionがdriftした環境からは
    real executionを開始できない。
    """
    try:
        head = require_clean_arena_head()
    except ExecutionSafetyError as exc:
        raise ProgressionLockError(str(exc)) from exc
    locked_revision = locked_execution_revision(document)
    if head != locked_revision:
        raise ProgressionLockError(
            f"live Arena HEAD {head} is not the locked execution target "
            f"{locked_revision}"
        )
    live = collect_execution_provenance()
    if execution_provenance_to_dict(live) != document["provenance"]:
        raise ProgressionLockError(
            "live execution provenance differs from the locked provenance"
        )
    return live


__all__ = [
    "LOCK_VERSION",
    "NO_RESCUE_BOUNDARY",
    "REQUIRED_DESTINATIONS",
    "ProgressionLockError",
    "build_lock_document",
    "load_lock_document",
    "locked_destination",
    "locked_execution_revision",
    "parse_lock_document",
    "require_live_execution_target",
    "require_locked_destination",
    "save_lock_document",
]
