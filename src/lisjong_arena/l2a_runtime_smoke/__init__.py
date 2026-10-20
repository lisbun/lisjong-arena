"""Issue #507: 凍結したL2a合成Policy 1席 + C0 3席の16半荘動作確認。"""

from .smoke import (
    ALLOCATION_IDENTITY,
    PROTOCOL,
    SEEDS,
    IllegalL2aActionError,
    L2aRuntimeSmokeError,
    RecordingL2aPolicy,
    l2a_seat,
    load_l2a_runtime,
    run_smoke,
)

__all__ = [
    "ALLOCATION_IDENTITY",
    "PROTOCOL",
    "SEEDS",
    "IllegalL2aActionError",
    "L2aRuntimeSmokeError",
    "RecordingL2aPolicy",
    "l2a_seat",
    "load_l2a_runtime",
    "run_smoke",
]
