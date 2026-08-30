"""Phase 6 research-only history-conditioned snapshot model package.

Only the pure feature contract is imported eagerly. Torch-specific modules remain
explicit imports so normal ``import lisjong_arena`` does not require PyTorch.
"""

from .feature import (
    FEATURE_SEMANTICS_ID,
    OpponentSnapshotFeature,
    Phase6SnapshotFeature,
    build_phase6_snapshot_feature,
)

__all__ = [
    "FEATURE_SEMANTICS_ID",
    "OpponentSnapshotFeature",
    "Phase6SnapshotFeature",
    "build_phase6_snapshot_feature",
]
