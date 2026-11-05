"""正式result・実行観測の構築・保存・offline verify（#562）。

```text
result = f(lock, 保存済みraw record, 観測値fileのSHA-256)
  blocks                 seed blockごとの R_A / R_B / R_B - R_A（整数）
  statistic              mean D、SD(K-1)、区間、classification（rankだけから）
  final_points_difference 副指標: final_points差（判定に使わない）
  secondary_metrics      保存物からの局指標・点数分解（Heuristic engine AABBと同じ定義）
  bindings               lock_digest、protocol_digest、raw manifest・観測値のSHA-256
observations             半荘ごとのwall秒・peak RSS・roleごとの判断時間（実行観測）
verify                   保存済みlock・raw・result・観測値だけから主副指標・分類を再導出し、
                         完全一致だけを成功とする。観測値は再導出せず、schema・schedule・
                         hashだけを照合する
```

verifyはengineを再実行せず、Policyも生成しない。
"""

import hashlib
import math
import re
from pathlib import Path

from lisjong_arena._artifact_io import (
    ArtifactValidationError,
    canonical_json_text,
    read_json_document,
    write_new_artifact_file,
)
from lisjong_arena.heuristic_candidate_engine_aabb.kyoku_record import (
    ROLE_CANDIDATE,
    ROLE_INCUMBENT,
    SEAT_COUNT,
    placement_roles,
    verify_schedule,
)
from lisjong_arena.heuristic_candidate_engine_aabb.record_store import (
    RecordBindings,
    VerifiedKyokuRecords,
    read_kyoku_records,
    require_bindings,
)
from lisjong_arena.heuristic_candidate_engine_aabb.secondary_metrics import (
    summarize_seat_groups,
)
from lisjong_arena.overall_champion_aabb.lock import document_identity

from .lock import parse_lock_document
from .protocol import (
    EVALUATION_KIND,
    FORMAL_K,
    LAYOUT,
    PROTOCOL_DIGEST,
    PROTOCOL_ID,
    SECONDARY_METRICS_SCHEMA,
    LearningEngineAabbError,
    expected_schedule,
    require_hex64,
    schedule_document,
)
from .statistics import (
    block_rank_totals,
    final_points_difference,
    summarize_rank_blocks,
)

RESULT_SCHEMA = "arena-learning-candidate-engine-aabb-result-v1"
_HEX40 = re.compile(r"[0-9a-f]{40}")
OBSERVATIONS_SCHEMA = "arena-learning-candidate-engine-aabb-observations-v1"

_OBSERVATION_FIELDS = frozenset(
    {"hanchans", "lock_digest", "not_deterministic", "schema", "summary"}
)
_HANCHAN_OBSERVATION_FIELDS = frozenset(
    {
        "decision_seconds",
        "decisions",
        "peak_rss_kib",
        "placement",
        "seed",
        "wall_seconds",
    }
)


class LearningEngineAabbResultError(LearningEngineAabbError):
    """resultを作れない、またはverifyが一致しない場合。"""


def record_bindings(lock: dict) -> RecordBindings:
    return RecordBindings(
        lock_digest=lock["lock_digest"], environment_digest=lock["environment_digest"]
    )


def locked_identities(lock: dict) -> tuple[str, str]:
    participants = lock["participants"]
    return (
        participants[ROLE_CANDIDATE]["policy_identity"],
        participants[ROLE_INCUMBENT]["policy_identity"],
    )


def require_records(lock: dict, records: VerifiedKyokuRecords) -> None:
    """recordがlockのschedule・participant・bindingに一致することを要求する（K1 / K2）。"""
    try:
        verify_schedule(
            records.hanchans,
            expected_schedule(
                schedule_document(lock["population"]["ordered_seeds"])["hanchans"],
                *locked_identities(lock),
            ),
        )
        require_bindings(records, record_bindings(lock))
    except LearningEngineAabbError:
        raise
    except Exception as error:
        raise LearningEngineAabbResultError(
            f"records do not match the lock: {error}"
        ) from error


def secondary_metrics(records: VerifiedKyokuRecords) -> dict[str, object]:
    """Heuristic engine AABBと同じ定義の副指標（全seatをroleどおりに数える）。"""
    hanchans = records.hanchans
    groups: dict[int, list] = {}
    for index, row in enumerate(hanchans):
        groups.setdefault(row["seed"], []).extend(
            (index, seat, row["seats"][seat]["role"]) for seat in range(SEAT_COUNT)
        )
    return summarize_seat_groups(
        records,
        list(groups.items()),
        layout=LAYOUT,
        schema=SECONDARY_METRICS_SCHEMA,
    )


# ---------------------------------------------------------------------------
# observations（時間 / RSS）
# ---------------------------------------------------------------------------


def _finite_nonnegative(value: object, context: str) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise LearningEngineAabbResultError(f"{context} must be a finite number >= 0")
    return float(value)


def _require_hanchan_observation(item: object, expected: dict, context: str) -> dict:
    if type(item) is not dict or set(item) != _HANCHAN_OBSERVATION_FIELDS:
        raise LearningEngineAabbResultError(f"{context} fields do not match the schema")
    if (item["seed"], item["placement"]) != (expected["seed"], expected["placement"]):
        raise LearningEngineAabbResultError(f"{context} is not the scheduled hanchan")
    _finite_nonnegative(item["wall_seconds"], f"{context}.wall_seconds")
    rss = item["peak_rss_kib"]
    if type(rss) is not int or rss <= 0:
        raise LearningEngineAabbResultError(f"{context}.peak_rss_kib must be > 0")
    for name in ("decision_seconds", "decisions"):
        values = item[name]
        if type(values) is not list or len(values) != SEAT_COUNT:
            raise LearningEngineAabbResultError(f"{context}.{name} must hold 4 seats")
    for value in item["decision_seconds"]:
        _finite_nonnegative(value, f"{context}.decision_seconds")
    if any(type(value) is not int or value < 0 for value in item["decisions"]):
        raise LearningEngineAabbResultError(f"{context}.decisions must be ints >= 0")
    return item


def summarize_observations(hanchans: list[dict]) -> dict[str, object]:
    roles = {ROLE_CANDIDATE: [0.0, 0], ROLE_INCUMBENT: [0.0, 0]}
    for item in hanchans:
        for seat, role in enumerate(placement_roles(item["placement"])):
            roles[role][0] += item["decision_seconds"][seat]
            roles[role][1] += item["decisions"][seat]
    return {
        "decision_seconds": {
            role: {
                "decisions": count,
                "mean_seconds": None if count == 0 else seconds / count,
                "total_seconds": seconds,
            }
            for role, (seconds, count) in roles.items()
        },
        "max_hanchan_wall_seconds": max(item["wall_seconds"] for item in hanchans),
        "max_peak_rss_kib": max(item["peak_rss_kib"] for item in hanchans),
        "total_wall_seconds": sum(item["wall_seconds"] for item in hanchans),
    }


def build_observations(lock: dict, executed) -> dict[str, object]:
    """実行した全半荘の計測値から観測document（resultではない）を作る。"""
    hanchans = []
    for item in executed:
        analysis = item.policy_analysis or {}
        hanchans.append(
            {
                "decision_seconds": analysis.get("decision_seconds"),
                "decisions": analysis.get("decisions"),
                "peak_rss_kib": item.peak_rss_kib,
                "placement": item.capture.placement,
                "seed": item.capture.seed,
                "wall_seconds": item.wall_seconds,
            }
        )
    document = {
        "hanchans": hanchans,
        "lock_digest": lock["lock_digest"],
        "not_deterministic": True,
        "schema": OBSERVATIONS_SCHEMA,
        "summary": None,
    }
    document["summary"] = summarize_observations(hanchans)
    return parse_observations(lock, document)


def parse_observations(lock: dict, value: object) -> dict[str, object]:
    """観測documentのschema・lock・schedule（全半荘、順序）・summaryを照合する。"""
    if type(value) is not dict or set(value) != _OBSERVATION_FIELDS:
        raise LearningEngineAabbResultError(
            "observation fields do not match the schema"
        )
    if value["schema"] != OBSERVATIONS_SCHEMA or value["not_deterministic"] is not True:
        raise LearningEngineAabbResultError("unsupported observation schema")
    if value["lock_digest"] != lock["lock_digest"]:
        raise LearningEngineAabbResultError("observations belong to another lock")
    schedule = schedule_document(lock["population"]["ordered_seeds"])["hanchans"]
    hanchans = value["hanchans"]
    if type(hanchans) is not list or len(hanchans) != len(schedule):
        raise LearningEngineAabbResultError("observations must cover every hanchan")
    for index, (item, expected) in enumerate(zip(hanchans, schedule, strict=True)):
        _require_hanchan_observation(item, expected, f"observations.hanchans[{index}]")
    if value["summary"] != summarize_observations(hanchans):
        raise LearningEngineAabbResultError("observation summary does not match")
    return value


def observations_sha256(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# result
# ---------------------------------------------------------------------------


def build_result(
    lock: dict,
    records: VerifiedKyokuRecords,
    *,
    observations_digest: str,
    aggregation_revision: str,
) -> dict[str, object]:
    """検査済みrecordから正式resultを作る。第1層・scheduleが通らないrecordでは作れない。"""
    lock = parse_lock_document(lock)
    if type(aggregation_revision) is not str or not _HEX40.fullmatch(
        aggregation_revision
    ):
        raise LearningEngineAabbResultError("aggregation_revision must be 40-hex")
    require_hex64(observations_digest, "observations_sha256")
    if not isinstance(records, VerifiedKyokuRecords):
        raise LearningEngineAabbResultError("records must be VerifiedKyokuRecords")
    require_records(lock, records)
    blocks = block_rank_totals(records.hanchans)
    statistic = summarize_rank_blocks([block["difference_ranks"] for block in blocks])
    if statistic["k"] != FORMAL_K:
        raise LearningEngineAabbResultError(f"a formal result needs K = {FORMAL_K}")
    candidate, incumbent = locked_identities(lock)
    payload = {
        "aggregation_revision": aggregation_revision,
        "blocks": blocks,
        "evaluation_kind": EVALUATION_KIND,
        "event_fact_status": records.event_fact_status.to_value(),
        "final_points_difference": final_points_difference(records.hanchans),
        "lock_digest": lock["lock_digest"],
        "observations_sha256": observations_digest,
        "participants": {ROLE_CANDIDATE: candidate, ROLE_INCUMBENT: incumbent},
        "protocol_digest": PROTOCOL_DIGEST,
        "protocol_id": PROTOCOL_ID,
        "raw_manifest_sha256": records.manifest_sha256,
        "schema": RESULT_SCHEMA,
        "secondary_metrics": secondary_metrics(records),
        "statistic": statistic,
    }
    return {**payload, "result_digest": document_identity(payload)}


def save_document(document: dict, path: str | Path) -> Path:
    destination = Path(path)
    if destination.exists():
        raise FileExistsError(f"refusing to overwrite: {destination}")
    write_new_artifact_file(destination, canonical_json_text(document))
    return destination


def verify_result(
    *,
    lock_path: str | Path,
    raw_path: str | Path,
    result_path: str | Path,
    observations_path: str | Path,
) -> dict[str, object]:
    """保存物だけからresultを再導出し、記録値と完全一致することを確かめる。"""
    try:
        lock = parse_lock_document(read_json_document(Path(lock_path)))
        recorded = read_json_document(Path(result_path))
        observations = read_json_document(Path(observations_path))
    except LearningEngineAabbError:
        raise
    except (ArtifactValidationError, OSError, ValueError) as error:
        raise LearningEngineAabbResultError(
            f"cannot read lock / result: {error}"
        ) from error
    if type(recorded) is not dict or recorded.get("schema") != RESULT_SCHEMA:
        raise LearningEngineAabbResultError("unsupported result schema")
    parse_observations(lock, observations)
    if Path(observations_path).read_text(encoding="utf-8") != canonical_json_text(
        observations
    ):
        raise LearningEngineAabbResultError("observation file is not canonical")
    try:
        records = read_kyoku_records(
            raw_path, event_facts_required=False, layout=LAYOUT
        )
    except Exception as error:
        raise LearningEngineAabbResultError(
            f"raw record is invalid: {error}"
        ) from error
    rederived = build_result(
        lock,
        records,
        observations_digest=observations_sha256(observations_path),
        aggregation_revision=recorded.get("aggregation_revision"),
    )
    if recorded != rederived:
        differing = sorted(
            key
            for key in set(recorded) | set(rederived)
            if recorded.get(key) != rederived.get(key)
        )
        raise LearningEngineAabbResultError(
            f"recorded result differs from the re-derivation: {differing}"
        )
    if Path(result_path).read_text(encoding="utf-8") != canonical_json_text(recorded):
        raise LearningEngineAabbResultError("result file is not canonical")
    return rederived


__all__ = [
    "OBSERVATIONS_SCHEMA",
    "RESULT_SCHEMA",
    "LearningEngineAabbResultError",
    "build_observations",
    "build_result",
    "observations_sha256",
    "parse_observations",
    "record_bindings",
    "require_records",
    "save_document",
    "secondary_metrics",
    "summarize_observations",
    "verify_result",
]
