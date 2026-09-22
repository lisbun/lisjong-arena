"""What the #331/#332 execution path actually instruments.

Issue #340's Phase 1 requires `progress + durable receipt supported`. That is a
capability claim about the code that will really run, so it is declared here
next to the generator rather than asserted by an operator or a launcher.

``GENERATION_DURABLE_EVIDENCE_LEVEL`` is what
:func:`lisjong_arena.offense_foundation.corpus.generate` provides today: atomic
operational progress only. It publishes no #339 per-seed receipt, so an
interrupted production run retains no completed hanchan. The #340 admission
gate therefore fails closed for #332 until that capability exists -- see
``PER_SEED_RECEIPT_FOLLOW_UP``.

``GENERATION_INSTRUMENTATION_IDENTITY`` names the *artifact write path* -- the
paired staged corpus and player-safe source-record writes that dominate the
measured I/O. The bounded calibration runner performs exactly the same writes,
so it shares this identity; the per-seed operational receipt it additionally
publishes is a stronger durable-evidence level, not a different write path.
"""

from __future__ import annotations

GENERATION_INSTRUMENTATION_IDENTITY = (
    "offense-foundation-332/paired-corpus-source-record-write/v1"
)
GENERATION_PROGRESS_SCHEMA = "arena-aws-progress-v1"

#: The level `corpus.generate` provides today.
GENERATION_DURABLE_EVIDENCE_LEVEL = "atomic-operational-progress"

#: The level the bounded calibration runner provides.
CALIBRATION_DURABLE_EVIDENCE_LEVEL = "per-seed-durable-receipt"

#: The level #340 requires of a production run before billable admission.
REQUIRED_DURABLE_EVIDENCE_LEVEL = "per-seed-durable-receipt"

PER_SEED_RECEIPT_FOLLOW_UP = "lisbun/lisjong-arena#350"

GENERATION_LIMITATION = (
    "the #331 corpus generator publishes atomic operational progress but no "
    "#339 per-seed durable receipt, so an interrupted run retains no completed "
    f"hanchan; tracked by {PER_SEED_RECEIPT_FOLLOW_UP}"
)


def describe_generation_instrumentation() -> dict[str, object]:
    """Report the real capability of the production generation path."""

    supported = GENERATION_DURABLE_EVIDENCE_LEVEL == REQUIRED_DURABLE_EVIDENCE_LEVEL
    return {
        "durable_evidence_level": GENERATION_DURABLE_EVIDENCE_LEVEL,
        "follow_up": None if supported else PER_SEED_RECEIPT_FOLLOW_UP,
        "instrumentation_identity": GENERATION_INSTRUMENTATION_IDENTITY,
        "limitation": None if supported else GENERATION_LIMITATION,
        "per_seed_durable_receipt_supported": supported,
        "progress_schema": GENERATION_PROGRESS_SCHEMA,
        "required_durable_evidence_level": REQUIRED_DURABLE_EVIDENCE_LEVEL,
        "status": "PASS" if supported else "FAIL",
    }


def describe_calibration_instrumentation() -> dict[str, object]:
    """Report the capability of the bounded calibration runner."""

    return {
        "durable_evidence_level": CALIBRATION_DURABLE_EVIDENCE_LEVEL,
        "follow_up": None,
        "instrumentation_identity": GENERATION_INSTRUMENTATION_IDENTITY,
        "limitation": None,
        "per_seed_durable_receipt_supported": True,
        "progress_schema": GENERATION_PROGRESS_SCHEMA,
        "required_durable_evidence_level": REQUIRED_DURABLE_EVIDENCE_LEVEL,
        "status": "PASS",
    }


__all__ = [
    "CALIBRATION_DURABLE_EVIDENCE_LEVEL",
    "GENERATION_DURABLE_EVIDENCE_LEVEL",
    "GENERATION_INSTRUMENTATION_IDENTITY",
    "GENERATION_LIMITATION",
    "PER_SEED_RECEIPT_FOLLOW_UP",
    "REQUIRED_DURABLE_EVIDENCE_LEVEL",
    "describe_calibration_instrumentation",
    "describe_generation_instrumentation",
]
