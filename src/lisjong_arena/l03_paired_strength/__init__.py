"""L0.3 Step F outcome-Q vs canonical-first paired-strength protocol v1（#385）。

parent lisjong-project#79。protocol invariant、pre-execution lock、単一game
実行境界、seed-block statistics、terminal interpretation、write-once result
verificationだけを所有する。training semantics / Policy / residual runtimeは
lisjongが所有し、このpackageはそれらを再定義しない。

このpackageはformal execution（Step G）を自動で開始しない。
"""

from .execution import PairedStrengthExecutionError, execute_schedule, game_record
from .experiment import PairedStrengthOutcome, run_paired_strength
from .lock import (
    PairedStrengthLockError,
    build_lock_document,
    load_lock_document,
    parse_lock_document,
    require_live_target,
    save_lock_document,
)
from .protocol import (
    HANCHAN_COUNT,
    IMPROVED_LABEL,
    NOT_ESTABLISHED_LABEL,
    PAIRED_UNIT_COUNT,
    PROTOCOL_ID,
    SEED_BLOCK_COUNT,
    STOP_INVALID_LABEL,
    PairedStrengthProtocolError,
    game_schedule,
    protocol_document,
)
from .result import (
    PairedStrengthResultError,
    build_result_document,
    verify_result,
)

__all__ = [
    "HANCHAN_COUNT",
    "IMPROVED_LABEL",
    "NOT_ESTABLISHED_LABEL",
    "PAIRED_UNIT_COUNT",
    "PROTOCOL_ID",
    "SEED_BLOCK_COUNT",
    "STOP_INVALID_LABEL",
    "PairedStrengthExecutionError",
    "PairedStrengthLockError",
    "PairedStrengthOutcome",
    "PairedStrengthProtocolError",
    "PairedStrengthResultError",
    "build_lock_document",
    "build_result_document",
    "execute_schedule",
    "game_record",
    "game_schedule",
    "load_lock_document",
    "parse_lock_document",
    "protocol_document",
    "require_live_target",
    "run_paired_strength",
    "save_lock_document",
    "verify_result",
]
