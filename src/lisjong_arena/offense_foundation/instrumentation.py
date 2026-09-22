"""What the #331/#332 execution path actually instruments.

Issue #340's admission gate probes the capability of the code that will really
run rather than trusting an operator assertion.

Production and calibration intentionally have different durability contracts:

* :func:`lisjong_arena.offense_foundation.corpus.generate` provides atomic
  operational progress.  That is sufficient for #332 production admission
  because an interrupted scientific phase is never resumed or partially
  adopted; an authorized retry reruns the same locked phase from the beginning.
* the bounded operational calibration runner additionally publishes #339
  per-seed durable receipts.  Calibration keeps that stronger requirement
  because complete task-level timing evidence is part of calibration
  correctness.

``GENERATION_INSTRUMENTATION_IDENTITY`` names the performance-relevant
artifact write path -- the paired staged corpus and player-safe source-record
writes that dominate measured I/O.  The bounded calibration runner performs
exactly the same writes, so it shares this identity; its additional timing-only
receipt persistence is calibration evidence rather than a production
scientific write-path requirement.
"""

from __future__ import annotations

GENERATION_INSTRUMENTATION_IDENTITY = (
    "offense-foundation-332/paired-corpus-source-record-write/v1"
)
GENERATION_PROGRESS_SCHEMA = "arena-aws-progress-v1"

#: The level `corpus.generate` provides today.
GENERATION_DURABLE_EVIDENCE_LEVEL = "atomic-operational-progress"

#: The level #332 production requires before billable admission.
PRODUCTION_REQUIRED_DURABLE_EVIDENCE_LEVEL = "atomic-operational-progress"

#: Backward-compatible name used by the existing #332 launcher JSON contract.
REQUIRED_DURABLE_EVIDENCE_LEVEL = PRODUCTION_REQUIRED_DURABLE_EVIDENCE_LEVEL

#: The level the bounded calibration runner provides.
CALIBRATION_DURABLE_EVIDENCE_LEVEL = "per-seed-durable-receipt"

#: Calibration intentionally keeps the stronger #339 receipt requirement.
CALIBRATION_REQUIRED_DURABLE_EVIDENCE_LEVEL = "per-seed-durable-receipt"

GENERATION_LIMITATION = (
    "an interrupted production run retains atomic operational progress but no "
    "#339 per-seed scientific receipt; partial scientific output is invalid and "
    "an authorized retry reruns the same locked phase from the beginning"
)


def describe_generation_instrumentation() -> dict[str, object]:
    """Report the real capability and production requirement."""

    supported = (
        GENERATION_DURABLE_EVIDENCE_LEVEL
        == PRODUCTION_REQUIRED_DURABLE_EVIDENCE_LEVEL
    )
    return {
        "durable_evidence_level": GENERATION_DURABLE_EVIDENCE_LEVEL,
        "follow_up": None,
        "instrumentation_identity": GENERATION_INSTRUMENTATION_IDENTITY,
        "limitation": GENERATION_LIMITATION,
        "per_seed_durable_receipt_supported": False,
        "progress_schema": GENERATION_PROGRESS_SCHEMA,
        "required_durable_evidence_level": (
            PRODUCTION_REQUIRED_DURABLE_EVIDENCE_LEVEL
        ),
        "status": "PASS" if supported else "FAIL",
    }


def describe_calibration_instrumentation() -> dict[str, object]:
    """Report the stronger capability required of bounded calibration."""

    supported = (
        CALIBRATION_DURABLE_EVIDENCE_LEVEL
        == CALIBRATION_REQUIRED_DURABLE_EVIDENCE_LEVEL
    )
    return {
        "durable_evidence_level": CALIBRATION_DURABLE_EVIDENCE_LEVEL,
        "follow_up": None,
        "instrumentation_identity": GENERATION_INSTRUMENTATION_IDENTITY,
        "limitation": None,
        "per_seed_durable_receipt_supported": True,
        "progress_schema": GENERATION_PROGRESS_SCHEMA,
        "required_durable_evidence_level": (
            CALIBRATION_REQUIRED_DURABLE_EVIDENCE_LEVEL
        ),
        "status": "PASS" if supported else "FAIL",
    }


__all__ = [
    "CALIBRATION_DURABLE_EVIDENCE_LEVEL",
    "CALIBRATION_REQUIRED_DURABLE_EVIDENCE_LEVEL",
    "GENERATION_DURABLE_EVIDENCE_LEVEL",
    "GENERATION_INSTRUMENTATION_IDENTITY",
    "GENERATION_LIMITATION",
    "PRODUCTION_REQUIRED_DURABLE_EVIDENCE_LEVEL",
    "REQUIRED_DURABLE_EVIDENCE_LEVEL",
    "describe_calibration_instrumentation",
    "describe_generation_instrumentation",
]
