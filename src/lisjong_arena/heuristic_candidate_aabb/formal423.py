"""Admission for the #423 AWS formal runner; no game/statistics implementation."""

from __future__ import annotations

import subprocess
from pathlib import Path

from lisjong_arena.seed_registry import require_allocation_binding

from .calibration423 import verify
from .protocol import PROTOCOL_ID, SEED_DOMAIN, require_population

CALIBRATION_ID = "75bfcbb02eb44e2650d716d6021edd802fe33a15f4a838f1a434dbaf36d7eefd"
# #430 only changed portable saved-wheel path validation. It did not change
# the measured game path from the calibration execution revision (43e468b).
REVIEWED_BASE = "533a584cad5dd5b432d75e916bd7062025ce7640"


def require_allocation(ledger, binding, seeds, *, arena_revision):
    seeds = require_population(seeds)
    record = require_allocation_binding(
        ledger,
        binding,
        seeds=seeds,
        owner_issue="lisbun/lisjong-arena#423",
        protocol=PROTOCOL_ID,
        seed_domain=SEED_DOMAIN,
        population="heuristic-candidate-aabb-423",
        split="FORMAL-EVAL",
    )
    if record["arena_revision"] != arena_revision:
        raise ValueError("formal allocation belongs to another Arena revision")
    return record


def calibration_plan(bundle, repository, revision):
    """Use only the verified pilot and reviewed workload, not arbitrary timings.

    The new runner/helper may differ; runtime sources and dependency pins must
    equal the reviewed base. A future gameplay update needs a new calibration
    decision rather than silently extrapolating this pilot.
    """
    if not bundle or not Path(bundle).is_dir():
        raise ValueError("CalibrationBundlePath must contain the successful pilot")
    result = verify(bundle)
    if result["identity"] != CALIBRATION_ID:
        raise ValueError("unexpected calibration result identity")
    changed = subprocess.check_output(
        [
            "git",
            "-C",
            str(repository),
            "diff",
            "--name-only",
            REVIEWED_BASE,
            revision,
            "--",
            "src",
            "pyproject.toml",
            ":(exclude)src/lisjong_arena/heuristic_candidate_aabb/formal423.py",
        ],
        text=True,
    ).strip()
    if changed:
        raise ValueError(f"workload differs from reviewed calibration path: {changed}")
    prediction = result["runtime_prediction"]
    return {
        "basis": prediction["basis"],
        "calibration_result_identity": result["identity"],
        "calibration_execution_revision": result["operational_calibration"][
            "arena_revision"
        ],
        "reviewed_workload_revision": REVIEWED_BASE,
        "formal_execution_revision": revision,
        "workload_seconds": prediction["headroom_adjusted_upper_seconds"],
        "predicted_lower_seconds": prediction["predicted_lower_seconds"],
        "predicted_upper_seconds": prediction["predicted_upper_seconds"],
        "setup_collection_allowance_seconds": 1800,
        "limitations": result["limitations"],
    }
