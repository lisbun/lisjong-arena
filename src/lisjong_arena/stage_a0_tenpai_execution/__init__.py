"""Exact execution path for lisbun/lisjong-arena#262.

The package consumes the immutable #259 Lock B and owns only execution,
artifact readback and classification.  It does not modify the scientific
protocol.
"""

from .protocol import (
    EXPECTED_LOCK_B_IDENTITY,
    GATE_FAIL,
    GATE_PASS,
    PREFLIGHT_PASS,
)

__all__ = [
    "EXPECTED_LOCK_B_IDENTITY",
    "GATE_FAIL",
    "GATE_PASS",
    "PREFLIGHT_PASS",
]
