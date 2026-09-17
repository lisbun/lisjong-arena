"""Stage A0 feasibility artifact: versioned machine-readable qualification record。

このartifactはtechnical feasibilityだけを記録する。strength、CE、learnability、
model metric、VALIDATION target behaviorは一切含めない。coverageの数字は
model-quality resultではない。

`hard_outcome`は#258が列挙する4つのうち1つだけを取る。まだ実測できていない
場合は`None`のままにし、`pending_reason`でoperator側に残る手順を明示する。
無理にhard outcomeを作らない。
"""

from dataclasses import dataclass
from pathlib import Path

from lisjong.policy_contract import Seat

from lisjong_arena._artifact_io import canonical_json_text, write_new_artifact_file

from .errors import StageA0ProtocolError
from .labels import (
    TargetAvailability,
    is_stable_thirteen_equivalent,
    opponent_identity,
    violates_physical_inventory,
)
from .protocol import (
    CANONICAL_WAIT_BUILDER,
    CANONICAL_WAIT_ENTRY_POINT,
    ISSUE_IDENTITY,
    PARENT_ISSUE_IDENTITIES,
    PROTOCOL_ID,
    RELATIVE_OPPONENT_OFFSETS,
    exact_wait_implementation_identity,
)
from .sidecar import availability_counts

REPORT_SCHEMA_VERSION = "arena-stage-a0-tenpai-feasibility-report-v1"

RETAINED_AUGMENTATION_QUALIFIED = "RETAINED AUGMENTATION QUALIFIED"
FRESH_LIVE_LABEL_PATH_QUALIFIED = "FRESH LIVE-LABEL PATH QUALIFIED"
TENPAI_LABEL_PATH_BLOCKED = "TENPAI LABEL PATH BLOCKED"
STOP_INVALID = "STOP / INVALID"

HARD_OUTCOMES = (
    RETAINED_AUGMENTATION_QUALIFIED,
    FRESH_LIVE_LABEL_PATH_QUALIFIED,
    TENPAI_LABEL_PATH_BLOCKED,
    STOP_INVALID,
)


@dataclass(frozen=True, slots=True)
class QualificationCheck:
    """1つのqualification軸のboolean判定と、その根拠。"""

    name: str
    qualified: bool
    detail: str

    def __post_init__(self) -> None:
        if type(self.name) is not str or not self.name:
            raise TypeError("name must be a non-empty str")
        if type(self.qualified) is not bool:
            raise TypeError("qualified must be an exact bool")
        if type(self.detail) is not str or not self.detail:
            raise TypeError("detail must be a non-empty str")

    def to_document(self) -> dict[str, object]:
        return {"qualified": self.qualified, "detail": self.detail}


def check_seat_mapping(cells) -> QualificationCheck:
    """relative slot -> canonical seat -> hidden truthのbindingを独立に再検証する。"""
    rows: dict[tuple, set] = {}
    for cell in cells:
        identity = cell.identity
        expected = opponent_identity(
            Seat(cell.row_identity.actor_seat),
            identity.viewer_relative_offset,
            Seat(cell.dealer_seat),
        )
        if expected != identity:
            return QualificationCheck(
                name="seat_mapping",
                qualified=False,
                detail=(
                    "an opponent cell does not match the canonical seat / wind "
                    "derived from its actor seat and relative offset"
                ),
            )
        if int(identity.seat) == cell.row_identity.actor_seat:
            return QualificationCheck(
                name="seat_mapping",
                qualified=False,
                detail="an acting seat appears as its own opponent target",
            )
        rows.setdefault(cell.row_identity.decision_key, set()).add(
            identity.viewer_relative_offset
        )
    for key, offsets in rows.items():
        if tuple(sorted(offsets)) != RELATIVE_OPPONENT_OFFSETS:
            return QualificationCheck(
                name="seat_mapping",
                qualified=False,
                detail=f"decision {key!r} does not carry all three opponent slots",
            )
    return QualificationCheck(
        name="seat_mapping",
        qualified=True,
        detail=(
            f"{len(rows)} decision rows bind three distinct canonical seats each "
            "through an explicit (actor + relative offset) mod 4 relation"
        ),
    )


def check_stable_thirteen(cells) -> QualificationCheck:
    """stable 13-equivalent contractがreason codeと一致していることを再検証する。"""
    masked = 0
    for cell in cells:
        if cell.concealed_tiles is None or cell.melds is None:
            continue
        stable = is_stable_thirteen_equivalent(cell.concealed_tiles, cell.melds)
        if cell.availability is TargetAvailability.AVAILABLE and not stable:
            return QualificationCheck(
                name="stable_13_equivalent",
                qualified=False,
                detail="an AVAILABLE target is not a stable 13-equivalent state",
            )
        if cell.availability is TargetAvailability.NOT_STABLE_13_EQUIVALENT and stable:
            return QualificationCheck(
                name="stable_13_equivalent",
                qualified=False,
                detail=(
                    "a target masked as NOT_STABLE_13_EQUIVALENT is actually a "
                    "stable 13-equivalent state"
                ),
            )
        if cell.availability is TargetAvailability.NOT_STABLE_13_EQUIVALENT:
            masked += 1
    return QualificationCheck(
        name="stable_13_equivalent",
        qualified=True,
        detail=(
            f"{masked} transient / 14-equivalent targets are masked with an "
            "explicit reason code instead of being zero-filled"
        ),
    )


def check_physical_inventory(cells) -> QualificationCheck:
    """physical inventory violationがsilentに修復されていないことを再検証する。"""
    masked = 0
    for cell in cells:
        if cell.concealed_tiles is None or cell.melds is None:
            continue
        violates = violates_physical_inventory(cell.concealed_tiles, cell.melds)
        if cell.availability is TargetAvailability.AVAILABLE and violates:
            return QualificationCheck(
                name="physical_inventory",
                qualified=False,
                detail="an AVAILABLE target exceeds the canonical tile inventory",
            )
        if cell.availability is TargetAvailability.INVALID_PHYSICAL_INVENTORY:
            if not violates:
                return QualificationCheck(
                    name="physical_inventory",
                    qualified=False,
                    detail=(
                        "a target masked as INVALID_PHYSICAL_INVENTORY does not "
                        "actually violate the canonical inventory"
                    ),
                )
            masked += 1
    return QualificationCheck(
        name="physical_inventory",
        qualified=True,
        detail=(
            f"{masked} inventory violations are masked with an explicit reason "
            "code and never silently repaired"
        ),
    )


def check_public_private_boundary(cells) -> QualificationCheck:
    """privileged truthがpublic row bytesへ入っていないことを再検証する。"""
    digests: dict[tuple, str] = {}
    for cell in cells:
        key = cell.row_identity.decision_key
        known = digests.setdefault(key, cell.public_row_digest)
        if known != cell.public_row_digest:
            return QualificationCheck(
                name="public_private_boundary",
                qualified=False,
                detail=(
                    "opponent cells of the same decision reference different "
                    "public row digests"
                ),
            )
    return QualificationCheck(
        name="public_private_boundary",
        qualified=True,
        detail=(
            f"{len(digests)} public row digests are stable across every attached "
            "privileged cell; privileged truth lives only in the sidecar"
        ),
    )


def check_fail_closed(cells) -> QualificationCheck:
    """unexpected fail-closed cellが残っていないことを確認する。"""
    counts = availability_counts(cells)
    unexpected = counts[TargetAvailability.OTHER_FAIL_CLOSED.value]
    return QualificationCheck(
        name="fail_closed",
        qualified=unexpected == 0,
        detail=(
            f"{unexpected} cells hit the unexpected fail-closed branch; a "
            "non-zero count blocks qualification"
        ),
    )


@dataclass(frozen=True, slots=True)
class FeasibilityReport:
    """#258 feasibility artifactのimmutable value。"""

    provenance: dict
    source_paths_examined: tuple[str, ...]
    retained_corpus_identity: str | None
    retained_outcome: dict | None
    smoke_population_identity: str | None
    fresh_outcome: dict | None
    sidecar_identity: str | None
    row_count: int
    cell_count: int
    availability_counts: dict
    checks: tuple[QualificationCheck, ...]
    hard_outcome: str | None
    pending_reason: str | None
    recommended_route: str | None

    def __post_init__(self) -> None:
        if self.hard_outcome is not None and self.hard_outcome not in HARD_OUTCOMES:
            raise StageA0ProtocolError(
                f"unsupported #258 hard outcome: {self.hard_outcome!r}"
            )
        if (self.hard_outcome is None) == (self.pending_reason is None):
            raise StageA0ProtocolError(
                "exactly one of hard_outcome / pending_reason must be set; a "
                "feasibility artifact must not invent an outcome it did not "
                "measure"
            )
        names = tuple(check.name for check in self.checks)
        if len(set(names)) != len(names):
            raise StageA0ProtocolError("qualification check names must be unique")

    def to_document(self) -> dict[str, object]:
        return {
            "report_schema_version": REPORT_SCHEMA_VERSION,
            "protocol": {
                "protocol_id": PROTOCOL_ID,
                "issue": ISSUE_IDENTITY,
                "parent_issues": list(PARENT_ISSUE_IDENTITIES),
            },
            "provenance": dict(self.provenance),
            "canonical_wait_implementation": {
                "builder": CANONICAL_WAIT_BUILDER,
                "entry_point": CANONICAL_WAIT_ENTRY_POINT,
                "implementation_identity": exact_wait_implementation_identity(),
            },
            "sources": {
                "paths_examined": list(self.source_paths_examined),
                "retained_corpus_identity": self.retained_corpus_identity,
                "smoke_population_identity": self.smoke_population_identity,
                "sidecar_identity": self.sidecar_identity,
            },
            "counts": {
                "row_count": self.row_count,
                "opponent_target_cell_count": self.cell_count,
                "availability": dict(self.availability_counts),
            },
            "qualifications": {
                check.name: check.to_document() for check in self.checks
            },
            "routes": {
                "retained_augmentation": self.retained_outcome,
                "fresh_live_label": self.fresh_outcome,
                "recommended_stage_a0_corpus_route": self.recommended_route,
            },
            "outcome": {
                "hard_outcome": self.hard_outcome,
                "pending_reason": self.pending_reason,
            },
        }

    def write(self, path) -> Path:
        path = Path(path)
        write_new_artifact_file(path, canonical_json_text(self.to_document()))
        return path


def build_checks(cells, *, deterministic_recomputation: QualificationCheck | None):
    """cellから導出できるqualification checkをまとめて構築する。"""
    checks = [
        check_seat_mapping(cells),
        check_stable_thirteen(cells),
        check_physical_inventory(cells),
        check_public_private_boundary(cells),
        check_fail_closed(cells),
    ]
    if deterministic_recomputation is not None:
        checks.append(deterministic_recomputation)
    return tuple(checks)


__all__ = [
    "FRESH_LIVE_LABEL_PATH_QUALIFIED",
    "HARD_OUTCOMES",
    "REPORT_SCHEMA_VERSION",
    "RETAINED_AUGMENTATION_QUALIFIED",
    "STOP_INVALID",
    "TENPAI_LABEL_PATH_BLOCKED",
    "FeasibilityReport",
    "QualificationCheck",
    "build_checks",
    "check_fail_closed",
    "check_physical_inventory",
    "check_public_private_boundary",
    "check_seat_mapping",
    "check_stable_thirteen",
]
