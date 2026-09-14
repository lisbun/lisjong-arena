"""Issue #252 — exact adaptive progression candidateのfeasibility + development評価。

`lisbun/lisjong #169 / PR #170`のexact adaptive candidate

```text
TerminalShantenProgressionMechanismRiichiDefensePolicy
```

を、そのexact parent ``mechanism-riichi-defense`` と同じfresh seeds / seat
rotations / passive tsumogiri x3条件で比較するためのpurpose-specific package
である。

```text
Phase A  647..650 / 4 rotations / 16 games   technical feasibility gate
Phase B  651..750 / 4 rotations / 400+400    paired development screen
```

Policy semantics、game execution、artifact schema、canonical aggregationは
すべて既存Arena / lisjong contractが所有する。このpackageはそれらの上へ
Issue #252のlocked条件、gate、paired primary statistic、one-shot artifact
disciplineを載せるだけであり、新しいgame runnerもgeneric evaluation
frameworkも導入しない。

real Phase A / Phase B executionはmerge後のoperator作業であり、testやCIでは
実行しない。
"""

from .experiment import (
    PhaseBOutcome,
    build_arm_plan,
    require_gate_before_phase_b,
    run_phase_b_development,
)
from .feasibility import (
    FeasibilityError,
    FeasibilityGate,
    FeasibilityRecord,
    WorkerMeasurement,
    evaluate_feasibility_gate,
    load_feasibility_record,
    require_passed_gate,
    run_phase_a_feasibility,
    save_feasibility_record,
    select_fastest_valid_worker_count,
)
from .lock import (
    ProgressionLockError,
    build_lock_document,
    load_lock_document,
    save_lock_document,
)
from .paired import (
    PairedResultError,
    PairedSeedDelta,
    PairedSummary,
    build_paired_result,
    classify_paired_summary,
    derive_paired_deltas,
    load_paired_result,
    save_paired_result,
    summarize_paired_deltas,
)
from .protocol import (
    CANDIDATE_IDENTITY,
    COMPARATOR_IDENTITY,
    INCONCLUSIVE_LABEL,
    INFEASIBLE_LABEL,
    NEGATIVE_LABEL,
    PARENT_IDENTITY,
    PHASE_A_SEEDS,
    PHASE_B_SEEDS,
    PROTOCOL_ID,
    SIGNAL_LABEL,
    ProgressionProtocolError,
    candidate_spec,
    comparator_spec,
    parent_spec,
    protocol_document,
    require_exact_candidate_semantics,
    require_exact_comparator,
)

__all__ = [
    "CANDIDATE_IDENTITY",
    "COMPARATOR_IDENTITY",
    "INCONCLUSIVE_LABEL",
    "INFEASIBLE_LABEL",
    "NEGATIVE_LABEL",
    "PARENT_IDENTITY",
    "PHASE_A_SEEDS",
    "PHASE_B_SEEDS",
    "PROTOCOL_ID",
    "SIGNAL_LABEL",
    "FeasibilityError",
    "FeasibilityGate",
    "FeasibilityRecord",
    "PairedResultError",
    "PairedSeedDelta",
    "PairedSummary",
    "PhaseBOutcome",
    "ProgressionLockError",
    "ProgressionProtocolError",
    "WorkerMeasurement",
    "build_arm_plan",
    "build_lock_document",
    "build_paired_result",
    "candidate_spec",
    "classify_paired_summary",
    "comparator_spec",
    "derive_paired_deltas",
    "evaluate_feasibility_gate",
    "load_feasibility_record",
    "load_lock_document",
    "load_paired_result",
    "parent_spec",
    "protocol_document",
    "require_exact_candidate_semantics",
    "require_exact_comparator",
    "require_gate_before_phase_b",
    "require_passed_gate",
    "run_phase_a_feasibility",
    "run_phase_b_development",
    "save_feasibility_record",
    "save_lock_document",
    "save_paired_result",
    "select_fastest_valid_worker_count",
    "summarize_paired_deltas",
]
