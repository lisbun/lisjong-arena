"""engine版AABB半荘評価の局単位record（#513）。

protocol identity ``arena-heuristic-candidate-engine-aabb-half-v1``
（仕様 ``docs/heuristic-candidate-engine-aabb-half.md`` §7・§10）のうち、
同じ対局実行から得た局精算・公開event factの保存、検査、副指標の再集計を
担当する。protocol / lock / result、runner、seed予約は後続（#514）が担当し、
本packageはそれらを持たない。旧protocol（RiichiEnv）のcodeは使わない。

```text
kyoku_record      第1層: CompletedMatch.history -> 局精算row、K3〜K12・C1〜C10、
                  K1 / K2のschedule照合API
event_facts       第2層: on_round_evidence_complete -> 公開fact row
record_store      manifest、write-once I/O、strict readback、第2層の無効化
secondary_metrics 保存物だけからの副指標・点数分解
```

API・失敗状態・後続が渡す値は ``docs/engine-aabb-kyoku-record.md`` を正本とする。
"""

from .errors import (
    EngineAabbRecordError,
    EventFactError,
    KyokuRecordError,
    SecondaryMetricError,
)
from .event_facts import EVENT_FACT_SCHEMA, project_event_facts, validate_event_fact_row
from .kyoku_record import (
    KYOKU_RECORD_SCHEMA,
    PLACEMENT_CANDIDATE_SEATS,
    PROTOCOL_ID,
    ExpectedHanchan,
    SeatAssignment,
    placement_roles,
    project_hanchan,
    validate_hanchan_row,
    verify_schedule,
)
from .record_store import (
    MANIFEST_SCHEMA,
    EventFactStatus,
    HanchanCapture,
    RecordBindings,
    VerifiedKyokuRecords,
    build_records,
    read_kyoku_records,
    require_bindings,
    write_kyoku_records,
)
from .secondary_metrics import SECONDARY_METRICS_SCHEMA, summarize_secondary_metrics

__all__ = [
    "EVENT_FACT_SCHEMA",
    "KYOKU_RECORD_SCHEMA",
    "MANIFEST_SCHEMA",
    "PLACEMENT_CANDIDATE_SEATS",
    "PROTOCOL_ID",
    "SECONDARY_METRICS_SCHEMA",
    "EngineAabbRecordError",
    "EventFactError",
    "EventFactStatus",
    "ExpectedHanchan",
    "HanchanCapture",
    "KyokuRecordError",
    "RecordBindings",
    "SeatAssignment",
    "SecondaryMetricError",
    "VerifiedKyokuRecords",
    "build_records",
    "placement_roles",
    "project_event_facts",
    "project_hanchan",
    "read_kyoku_records",
    "require_bindings",
    "summarize_secondary_metrics",
    "validate_event_fact_row",
    "validate_hanchan_row",
    "verify_schedule",
    "write_kyoku_records",
]
