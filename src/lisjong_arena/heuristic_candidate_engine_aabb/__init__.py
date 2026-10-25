"""engine版AABB半荘評価（protocol ``arena-heuristic-candidate-engine-aabb-half-v1``）。

仕様 ``docs/heuristic-candidate-engine-aabb-half.md``。旧protocol（RiichiEnv）のcodeは使わない。

```text
局単位record（#513）
  kyoku_record      第1層: CompletedMatch.history -> 局精算row、K3〜K12・C1〜C10、
                    K1 / K2のschedule照合API
  event_facts       第2層: on_round_evidence_complete -> 公開fact row
  record_store      manifest、write-once I/O、strict readback、第2層の無効化
  secondary_metrics 保存物だけからの副指標・点数分解
protocol・実行（#514）
  protocol          固定値、rules / protocol / schedule digest、participant
  statistics        S_A / S_B、D、区間、classification
  lock              実行前lock、Seed Registry照合、live gate
  executor          1 workerでの逐次実行、新しいPolicy、呼び出し数、timeout
  result / runner   正式result、offline verify、STOP時の診断証跡
  development       §10.7の開発用記録経路確認
校正（#517）
  calibration       CALIBRATION専用のplan / run / verify / report（強さの結果ではない）
  backend_check     校正前のPython/Rust照合P2（v1。保存記録の照合を保持）
Rust必須lisjong（#531）
  rust_only_gate    P2に代わる新version gate（Rust 2回の決定性・P3・counter・再導出）
#520 同点数牌paijia候補の開発比較（DEVELOPMENT）
  paijia_tie_plan        段階1・段階2・rehearsalのwrite-once plan、上限・停止・公開の実行枠
  paijia_tie_worker      worker内のPolicy観測（shadow採点、analysis・判断時間）
  paijia_tie_activation  段階1の変更頻度・時間gate
  paijia_tie_screen      段階2の192半荘screen、進行条件、verify / report
#524 単独副露者genbutsu候補のshadow変更頻度gate（DEVELOPMENT）
  single_open_genbutsu_plan        write-once plan（実行枠は#520のものを使う）
  single_open_genbutsu_worker      worker内のshadow採点（通常 / trace実行の一致を含む）
  single_open_genbutsu_activation  変更頻度・時間gate、summary、verify
#534 call-value-shadow-v1（#533、DEVELOPMENT診断）
  call_value_shadow_plan    write-once plan（固定24 seeds・上限・判定、実行枠は#520のもの）
  call_value_shadow_worker  call意図→公開evidenceで成立/取消→直後打牌、lisjong解析の運搬
  call_value_shadow         summary・固定判定・保存contextからの再導出verify・rehearsal照合
#540 riichi-dama-branch-v1（#539、DEVELOPMENT診断）
  riichi_dama_branch_plan    write-once plan（固定24 seeds・上限・判定・時間見積もりgate）
  riichi_dama_branch_worker  R（Champion×4、分岐点・宣言牌・context）とD arm（分岐席だけ
                             lisjong wrapper、対象局の精算後に打ち切り）
  riichi_dama_branch         R/D照合・Δ・部分集合・bootstrap・判定・2 seed再実行verify・
                             rehearsal照合・時間見積もり
```

依存はprotocol・実行 → 局単位recordの一方向。API・失敗状態は
``docs/engine-aabb-kyoku-record.md`` と ``docs/engine-aabb-evaluation.md`` を正本とする。
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
