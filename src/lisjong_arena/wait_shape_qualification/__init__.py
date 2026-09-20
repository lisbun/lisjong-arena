"""#317/#322 canonical wait-shape qualification primitives.

The package deliberately composes the exact-alignment work from #258 instead of
modifying its non-riichi Tenpai semantics.
"""

from .labels import (
    WaitShapeAvailability,
    WaitShapeProjection,
    WaitShapeQualificationError,
    WaitShapeTarget,
    build_wait_shape_target,
    build_wait_shape_target_from_retained_cell,
    project_exact_wait_shapes,
)
from .protocol import (
    DEFENSE_DIAGNOSTIC_STATUS,
    PILOT_SEEDS,
    PRIMARY_SHAPES,
    SCIENTIFIC_EVAL_SEEDS,
    SCIENTIFIC_SELECT_SEEDS,
    SCIENTIFIC_TRAIN_SEEDS,
    protocol_lock_document,
    protocol_lock_identity,
)

__all__ = [
    "DEFENSE_DIAGNOSTIC_STATUS",
    "PILOT_SEEDS",
    "PRIMARY_SHAPES",
    "SCIENTIFIC_EVAL_SEEDS",
    "SCIENTIFIC_SELECT_SEEDS",
    "SCIENTIFIC_TRAIN_SEEDS",
    "WaitShapeAvailability",
    "WaitShapeProjection",
    "WaitShapeQualificationError",
    "WaitShapeTarget",
    "build_wait_shape_target",
    "build_wait_shape_target_from_retained_cell",
    "project_exact_wait_shapes",
    "protocol_lock_document",
    "protocol_lock_identity",
]
