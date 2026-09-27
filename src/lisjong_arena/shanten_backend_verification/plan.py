"""The frozen #400 run plan (docs/rust-shanten-backend-400.md §5).

``scripts/aws/bootstrap-rust-shanten-400.sh`` executes exactly this plan and
``report.evaluate()`` refuses to judge evidence that does not match it.  The
bootstrap contract test ties the shell constants to these values.

The lisjong revision and wheel identity are the lisjong#217 combination that
#400 (and #406) ran with.  They are frozen here, not taken from ``backend``,
whose ``EXPECTED_*`` values follow the current pin (#409).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PlannedPolicy:
    label: str
    catalog: str
    """``POLICY_CATALOG`` id used for games."""
    replay_class: str
    """``module:Class`` used by lisjong's replay tool."""
    decisions_file: str
    decisions_sha256: str
    decisions: int
    seed0_semantic_sha256: str
    """lisjong#213 semantic digest of the seed-0 capture."""


CHAMPION = PlannedPolicy(
    label="champion",
    catalog="placement-aware-speed-call",
    replay_class="lisjong.policies:PlacementAwareSpeedCallPolicy",
    decisions_file="decisions-placement-aware-speed-call.pickle",
    decisions_sha256="275ca281f3b57bb4e61ed16d9c95837fdca05d232297c0db56789a56adbcb8ea",
    decisions=708,
    seed0_semantic_sha256=(
        "1d80405ec92e2915227aefa4038c4a6ede1a822506ab2ef09bb89cd21418d91b"
    ),
)
TWO_STEP = PlannedPolicy(
    label="two-step",
    catalog="two-step",
    replay_class="lisjong.policies:TwoStepUkeirePolicy",
    decisions_file="decisions-two-step.pickle",
    decisions_sha256="31d0448e067ad5af08d992b939c02b66ad2f8120d919c4efe22670c750b8a4cf",
    decisions=684,
    seed0_semantic_sha256=(
        "7057aee210f8175e0e130fca7179875d304a788e40a5ccd148ef7a7082371f26"
    ),
)
POLICIES = (CHAMPION, TWO_STEP)

LISJONG_REVISION = "2553c1b9f22545bb2fcb914adce1879d15cdc58d"
"""lisjong#217 merge on ``main``; the #400 pin and wheel build source."""
WHEEL_FILENAME = "lisjong_native-0.1.0-cp314-cp314-manylinux_2_28_x86_64.whl"
WHEEL_SHA256 = "ff8aaa400de5b58e4bb61d040dce596b7875047cda2bf2daa15b1a0e3e90166c"
"""``lisjong-native-wheel-<revision>`` artifact of lisjong CI run 36257082986."""
GAME_MODE = "4p-red-half"
SINGLE_SEEDS = (0,)
MULTI_WORKERS = 16
MULTI_SEEDS = tuple(range(32))
STARTUP_REPEAT = 10
REPLAY_ORDER = ("python", "rust", "rust", "python", "python", "rust")
"""Replay run ``n`` (1-based) uses ``REPLAY_ORDER[n - 1]``; one pass each."""
REPLAY_REPEAT = 1


def replay_indices(backend: str) -> tuple[int, ...]:
    return tuple(
        index
        for index, planned in enumerate(REPLAY_ORDER, start=1)
        if planned == backend
    )
