"""正式resultの構築・保存・offline verify（#514、仕様§3・§4・§5.6・§10）。

```text
result = f(lock, 保存済みraw record)
  blocks           seed blockごとの S_A / S_B / S_A - S_B（内部単位の整数）
  statistic        mean D、SD(K-1)、区間、classification
  secondary        保存物からの副指標（#513）。第2層INVALIDなら第2層指標は null
  bindings         lock_digest、protocol_digest、raw manifestのSHA-256、集計Arena revision
verify            保存済みlock・raw・resultだけから同じ値を再導出し、完全一致だけを成功とする
```

verifyはengineを再実行せず、Policyも生成しない。live ledger・実行環境の照合
（``lock.require_live_target``）とは別の、artifact整合の検証である。
"""

from pathlib import Path

from lisjong_arena._artifact_io import (
    ArtifactValidationError,
    canonical_json_text,
    read_json_document,
    write_new_artifact_file,
)
from lisjong_arena.overall_champion_aabb.lock import document_identity

from .kyoku_record import PROTOCOL_ID, verify_schedule
from .lock import locked_participants, parse_lock_document
from .protocol import (
    PROTOCOL_DIGEST,
    EngineAabbProtocolError,
    expected_schedule,
    require_hex,
    schedule_document,
)
from .record_store import (
    RecordBindings,
    VerifiedKyokuRecords,
    read_kyoku_records,
    require_bindings,
)
from .secondary_metrics import summarize_secondary_metrics
from .statistics import block_totals, summarize_blocks

RESULT_SCHEMA = "arena-heuristic-candidate-engine-aabb-result-v1"
EVALUATION_KIND = "FORMAL"


class EngineAabbResultError(EngineAabbProtocolError):
    """resultを作れない、またはverifyが一致しない場合。"""


def record_bindings(lock: dict) -> RecordBindings:
    return RecordBindings(
        lock_digest=lock["lock_digest"], environment_digest=lock["environment_digest"]
    )


def _require_records(lock: dict, records: VerifiedKyokuRecords) -> None:
    """recordがlockのschedule・participant・bindingに一致することを要求する（K1 / K2）。"""
    candidate, incumbent = locked_participants(lock)
    verify_schedule(
        records.hanchans,
        expected_schedule(
            schedule_document(lock["population"]["ordered_seeds"])["hanchans"],
            candidate,
            incumbent,
        ),
    )
    require_bindings(records, record_bindings(lock))


def build_result(
    lock: dict, records: VerifiedKyokuRecords, *, aggregation_revision: str
) -> dict[str, object]:
    """検査済みrecordから正式resultを作る。第1層が通らないrecordでは作れない。"""
    lock = parse_lock_document(lock)
    require_hex(aggregation_revision, 40, "aggregation_revision")
    if not isinstance(records, VerifiedKyokuRecords):
        raise EngineAabbResultError("records must be VerifiedKyokuRecords")
    _require_records(lock, records)
    blocks = block_totals(records.hanchans)
    statistic = summarize_blocks([block["difference_internal"] for block in blocks])
    payload = {
        "aggregation_revision": aggregation_revision,
        "blocks": blocks,
        "evaluation_kind": EVALUATION_KIND,
        "event_fact_status": records.event_fact_status.to_value(),
        "incumbent_role": lock["incumbent_role"],
        "lock_digest": lock["lock_digest"],
        "protocol_digest": PROTOCOL_DIGEST,
        "protocol_id": PROTOCOL_ID,
        "raw_manifest_sha256": records.manifest_sha256,
        "schema": RESULT_SCHEMA,
        "secondary_metrics": summarize_secondary_metrics(records),
        "statistic": statistic,
    }
    return {**payload, "result_digest": document_identity(payload)}


def save_result(document: dict, path: str | Path) -> Path:
    destination = Path(path)
    if destination.exists():
        raise FileExistsError(f"refusing to overwrite result: {destination}")
    write_new_artifact_file(destination, canonical_json_text(document))
    return destination


def verify_result(
    *, lock_path: str | Path, raw_path: str | Path, result_path: str | Path
) -> dict[str, object]:
    """保存物だけからresultを再導出し、記録値と完全一致することを確かめる。"""
    try:
        lock = parse_lock_document(read_json_document(Path(lock_path)))
        recorded = read_json_document(Path(result_path))
    except (ArtifactValidationError, OSError, ValueError) as error:
        raise EngineAabbResultError(f"cannot read lock / result: {error}") from error
    if type(recorded) is not dict or recorded.get("schema") != RESULT_SCHEMA:
        raise EngineAabbResultError("unsupported result schema")
    records = read_kyoku_records(raw_path, event_facts_required=False)
    rederived = build_result(
        lock, records, aggregation_revision=recorded.get("aggregation_revision")
    )
    if recorded != rederived:
        differing = sorted(
            key
            for key in set(recorded) | set(rederived)
            if recorded.get(key) != rederived.get(key)
        )
        raise EngineAabbResultError(
            f"recorded result differs from the re-derivation: {differing}"
        )
    if Path(result_path).read_text(encoding="utf-8") != canonical_json_text(recorded):
        raise EngineAabbResultError("result file is not canonical")
    return rederived


__all__ = [
    "EVALUATION_KIND",
    "RESULT_SCHEMA",
    "EngineAabbResultError",
    "build_result",
    "record_bindings",
    "save_result",
    "verify_result",
]
