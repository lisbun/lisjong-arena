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
    collect_execution_provenance,
    execution_provenance_to_dict,
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
            "target_type": "reviewed-merged-main-v1",
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


def parse_lock_document(value: object) -> dict[str, object]:
    """strict readbackでlockを読み戻し、identityと固定条件を確認する。"""
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
    if protocol != protocol_document():
        raise ProgressionLockError("lock protocol block does not match")
    candidate = expect_object(
        document["candidate_binding"],
        set(require_exact_candidate_semantics().to_document()),
        "candidate_binding",
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
    if comparator["identity"] != COMPARATOR_IDENTITY:
        raise ProgressionLockError("lock comparator identity is not the locked T")
    if expect_str(document["parent_identity"], "parent_identity") != PARENT_IDENTITY:
        raise ProgressionLockError("lock parent identity is not the locked C")
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


__all__ = [
    "LOCK_VERSION",
    "NO_RESCUE_BOUNDARY",
    "REQUIRED_DESTINATIONS",
    "ProgressionLockError",
    "build_lock_document",
    "load_lock_document",
    "parse_lock_document",
    "save_lock_document",
]
