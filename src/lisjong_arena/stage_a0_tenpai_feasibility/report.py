"""Stage A0 feasibility artifact: versioned machine-readable qualification record。

このartifactはtechnical feasibilityだけを記録する。strength、CE、learnability、
model metric、VALIDATION target behaviorは一切含めない。coverageの数字は
model-quality resultではない。

`hard_outcome`は#258が列挙する4つのうち1つだけを取る。まだ実測できていない
場合は`None`のままにし、`pending_reason`でoperator側に残る手順を明示する。
無理にhard outcomeを作らない。
"""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from lisjong.policy_contract import Seat

from lisjong_arena._artifact_io import canonical_json_text, write_new_artifact_file

from .errors import StageA0ProtocolError, StageA0ReportError
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

RETAINED_AUGMENTATION_NOT_QUALIFIED = "RETAINED AUGMENTATION NOT QUALIFIED"

HARD_OUTCOMES = (
    RETAINED_AUGMENTATION_QUALIFIED,
    FRESH_LIVE_LABEL_PATH_QUALIFIED,
    TENPAI_LABEL_PATH_BLOCKED,
    STOP_INVALID,
)
RETAINED_ROUTE_OUTCOMES = (
    RETAINED_AUGMENTATION_QUALIFIED,
    RETAINED_AUGMENTATION_NOT_QUALIFIED,
)

_REPORT_FIELDS = {
    "report_schema_version",
    "report_identity",
    "protocol",
    "provenance",
    "instrumentation_provenance",
    "canonical_wait_implementation",
    "sources",
    "counts",
    "qualifications",
    "routes",
    "outcome",
}
_PROTOCOL_FIELDS = {"protocol_id", "issue", "parent_issues"}
_CANONICAL_FIELDS = {"builder", "entry_point", "implementation_identity"}
_SOURCE_FIELDS = {
    "paths_examined",
    "retained_corpus_identity",
    "smoke_population_identity",
    "sidecar_identity",
    "retained_evidence",
}
_COUNT_FIELDS = {"row_count", "opponent_target_cell_count", "availability"}
_ROUTE_FIELDS = {
    "retained_augmentation",
    "fresh_live_label",
    "recommended_stage_a0_corpus_route",
}
_OUTCOME_FIELDS = {"hard_outcome", "pending_reason"}
_QUALIFICATION_FIELDS = {"qualified", "detail"}
_RETAINED_EVIDENCE_FIELDS = {
    "report_identity",
    "retained_corpus_identity",
    "retained_outcome",
}


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


def report_identity(document: dict[str, object]) -> str:
    """`report_identity`自身を除いたcanonical documentのsha256を返す。"""
    logical = {
        name: value for name, value in document.items() if name != "report_identity"
    }
    return hashlib.sha256(canonical_json_text(logical).encode("utf-8")).hexdigest()


def _error(message: str) -> StageA0ReportError:
    return StageA0ReportError(message)


def _expect_object(value: object, fields: set[str], context: str) -> dict:
    if type(value) is not dict:
        raise _error(f"{context} must be an object")
    if set(value) != fields:
        raise _error(f"{context} fields are invalid")
    return value


def _expect(value: object, expected: type, context: str):
    if type(value) is not expected:
        raise _error(f"{context} must be a {expected.__name__}")
    return value


def _expect_digest(value: object, context: str) -> str:
    text = _expect(value, str, context)
    if len(text) != 64 or any(
        character not in "0123456789abcdef" for character in text
    ):
        raise _error(f"{context} must be a lowercase sha256 digest")
    return text


def _expect_optional(value: object, expected: type, context: str):
    if value is None:
        return None
    return _expect(value, expected, context)


@dataclass(frozen=True, slots=True)
class LoadedFeasibilityReport:
    """strict readback済みのfeasibility report。"""

    path: Path
    document: dict

    @property
    def identity(self) -> str:
        return self.document["report_identity"]

    @property
    def hard_outcome(self) -> str | None:
        return self.document["outcome"]["hard_outcome"]

    @property
    def retained_outcome(self) -> str | None:
        route = self.document["routes"]["retained_augmentation"]
        return None if route is None else route["outcome"]

    @property
    def retained_corpus_identity(self) -> str | None:
        return self.document["sources"]["retained_corpus_identity"]


def load_feasibility_report(path) -> LoadedFeasibilityReport:
    """feasibility reportをschema / protocol / identity付きでstrictに読む。

    stale / malformed / unrelated / tamperedなreportがfresh fallbackを
    authorizeしないよう、fail closedで検証する。
    """
    path = Path(path)
    if not path.is_file():
        raise _error("feasibility report path is not a file")
    text = path.read_text(encoding="utf-8")
    try:
        document = json.loads(text)
    except json.JSONDecodeError as error:
        raise _error("feasibility report is not valid JSON") from error

    document = _expect_object(document, _REPORT_FIELDS, "report")
    if document["report_schema_version"] != REPORT_SCHEMA_VERSION:
        raise _error(
            f"unsupported report schema: {document['report_schema_version']!r}"
        )
    protocol = _expect_object(document["protocol"], _PROTOCOL_FIELDS, "protocol")
    if protocol["protocol_id"] != PROTOCOL_ID:
        raise _error("report protocol id does not match the #258 protocol")
    if protocol["issue"] != ISSUE_IDENTITY:
        raise _error("report issue identity does not match the #258 issue")
    if protocol["parent_issues"] != list(PARENT_ISSUE_IDENTITIES):
        raise _error("report parent issue identities are not supported")

    provenance = _expect(document["provenance"], dict, "provenance")
    if not provenance or any(
        type(value) is not str or not value for value in provenance.values()
    ):
        raise _error("report provenance values must be non-empty strings")
    _expect_optional(
        document["instrumentation_provenance"], dict, "instrumentation_provenance"
    )
    canonical = _expect_object(
        document["canonical_wait_implementation"],
        _CANONICAL_FIELDS,
        "canonical_wait_implementation",
    )
    if (
        canonical["builder"] != CANONICAL_WAIT_BUILDER
        or canonical["entry_point"] != CANONICAL_WAIT_ENTRY_POINT
    ):
        raise _error("report canonical wait implementation identity is not supported")
    _expect_digest(
        canonical["implementation_identity"],
        "canonical_wait_implementation.implementation_identity",
    )

    sources = _expect_object(document["sources"], _SOURCE_FIELDS, "sources")
    paths = _expect(sources["paths_examined"], list, "sources.paths_examined")
    if any(type(item) is not str or not item for item in paths):
        raise _error("sources.paths_examined must be non-empty strings")
    retained_identity = sources["retained_corpus_identity"]
    if retained_identity is not None:
        _expect_digest(retained_identity, "sources.retained_corpus_identity")
    _expect_optional(
        sources["smoke_population_identity"], str, "sources.smoke_population_identity"
    )
    if sources["sidecar_identity"] is not None:
        _expect_digest(sources["sidecar_identity"], "sources.sidecar_identity")
    evidence = sources["retained_evidence"]
    if evidence is not None:
        evidence = _expect_object(
            evidence, _RETAINED_EVIDENCE_FIELDS, "sources.retained_evidence"
        )
        _expect_digest(
            evidence["report_identity"], "sources.retained_evidence.report_identity"
        )
        if evidence["retained_outcome"] not in RETAINED_ROUTE_OUTCOMES:
            raise _error(
                "sources.retained_evidence.retained_outcome is not a route outcome"
            )

    counts = _expect_object(document["counts"], _COUNT_FIELDS, "counts")
    for name in ("row_count", "opponent_target_cell_count"):
        if _expect(counts[name], int, f"counts.{name}") < 0:
            raise _error(f"counts.{name} must not be negative")
    availability = _expect(counts["availability"], dict, "counts.availability")
    if set(availability) != {
        availability_value.value for availability_value in TargetAvailability
    }:
        raise _error("counts.availability must count every reason code")
    if any(type(value) is not int or value < 0 for value in availability.values()):
        raise _error("counts.availability values must be non-negative ints")
    if sum(availability.values()) != counts["opponent_target_cell_count"]:
        raise _error("counts.availability does not sum to the cell count")

    qualifications = _expect(document["qualifications"], dict, "qualifications")
    for name, value in qualifications.items():
        entry = _expect_object(value, _QUALIFICATION_FIELDS, f"qualifications.{name}")
        _expect(entry["qualified"], bool, f"qualifications.{name}.qualified")
        _expect(entry["detail"], str, f"qualifications.{name}.detail")

    routes = _expect_object(document["routes"], _ROUTE_FIELDS, "routes")
    retained_route = routes["retained_augmentation"]
    if retained_route is not None:
        if type(retained_route) is not dict:
            raise _error("routes.retained_augmentation must be an object or null")
        if retained_route.get("outcome") not in RETAINED_ROUTE_OUTCOMES:
            raise _error(
                "routes.retained_augmentation.outcome is not a retained route outcome"
            )
    _expect_optional(
        routes["recommended_stage_a0_corpus_route"],
        str,
        "routes.recommended_stage_a0_corpus_route",
    )

    outcome = _expect_object(document["outcome"], _OUTCOME_FIELDS, "outcome")
    hard_outcome = outcome["hard_outcome"]
    pending_reason = outcome["pending_reason"]
    if hard_outcome is not None and hard_outcome not in HARD_OUTCOMES:
        raise _error(
            f"outcome.hard_outcome is not a #258 hard outcome: {hard_outcome!r}"
        )
    if (hard_outcome is None) == (pending_reason is None):
        raise _error(
            "exactly one of outcome.hard_outcome / outcome.pending_reason must be set"
        )
    if pending_reason is not None:
        _expect(pending_reason, str, "outcome.pending_reason")

    identity = _expect_digest(document["report_identity"], "report_identity")
    if identity != report_identity(document):
        raise _error("report_identity does not match the report content")
    if canonical_json_text(document) != text:
        raise _error("report bytes are not canonical JSON")
    return LoadedFeasibilityReport(path=path, document=document)


def require_retained_not_qualified(
    report: LoadedFeasibilityReport,
) -> dict[str, object]:
    """fresh fallbackを authorize できる retained evidence だけを通す。

    retained routeが明示的に`RETAINED AUGMENTATION NOT QUALIFIED`であること
    を要求する。stale / incompatible / conflictingなevidenceはfail closedし、
    fresh fallbackとして扱わない。
    """
    if not isinstance(report, LoadedFeasibilityReport):
        raise TypeError("report must be a LoadedFeasibilityReport")
    outcome = report.retained_outcome
    if outcome is None:
        raise _error(
            "the retained route was not measured in this report; the fresh "
            "live-label path cannot be authorized without it"
        )
    if outcome != RETAINED_AUGMENTATION_NOT_QUALIFIED:
        raise _error(
            f"the retained route is {outcome!r}; the fresh live-label path is "
            "only a fallback for RETAINED AUGMENTATION NOT QUALIFIED"
        )
    if report.hard_outcome is not None:
        raise _error(
            f"the retained report already carries hard outcome "
            f"{report.hard_outcome!r}; it cannot also authorize a fresh fallback"
        )
    if report.retained_corpus_identity is None:
        raise _error(
            "the retained report does not identify the retained corpus it examined"
        )
    return {
        "report_identity": report.identity,
        "retained_corpus_identity": report.retained_corpus_identity,
        "retained_outcome": outcome,
    }


@dataclass(frozen=True, slots=True)
class FeasibilityReport:
    """#258 feasibility artifactのimmutable value。"""

    provenance: dict
    instrumentation_provenance: dict | None
    source_paths_examined: tuple[str, ...]
    retained_corpus_identity: str | None
    retained_outcome: dict | None
    retained_evidence: dict | None
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
        if self.hard_outcome == FRESH_LIVE_LABEL_PATH_QUALIFIED and (
            self.retained_evidence is None
        ):
            raise StageA0ProtocolError(
                "FRESH LIVE-LABEL PATH QUALIFIED requires a validated retained "
                "qualification report showing RETAINED AUGMENTATION NOT "
                "QUALIFIED; the fresh route is only a fallback"
            )
        if self.retained_evidence is not None and (
            set(self.retained_evidence) != _RETAINED_EVIDENCE_FIELDS
        ):
            raise StageA0ProtocolError("retained evidence fields are invalid")

    def to_document(self) -> dict[str, object]:
        document: dict[str, object] = {
            "report_schema_version": REPORT_SCHEMA_VERSION,
            "protocol": {
                "protocol_id": PROTOCOL_ID,
                "issue": ISSUE_IDENTITY,
                "parent_issues": list(PARENT_ISSUE_IDENTITIES),
            },
            "provenance": dict(self.provenance),
            "instrumentation_provenance": (
                None
                if self.instrumentation_provenance is None
                else dict(self.instrumentation_provenance)
            ),
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
                "retained_evidence": (
                    None
                    if self.retained_evidence is None
                    else dict(self.retained_evidence)
                ),
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
        document["report_identity"] = report_identity(document)
        return document

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
    "RETAINED_AUGMENTATION_NOT_QUALIFIED",
    "RETAINED_AUGMENTATION_QUALIFIED",
    "RETAINED_ROUTE_OUTCOMES",
    "STOP_INVALID",
    "TENPAI_LABEL_PATH_BLOCKED",
    "FeasibilityReport",
    "LoadedFeasibilityReport",
    "QualificationCheck",
    "build_checks",
    "check_fail_closed",
    "check_physical_inventory",
    "check_public_private_boundary",
    "check_seat_mapping",
    "check_stable_thirteen",
    "load_feasibility_report",
    "report_identity",
    "require_retained_not_qualified",
]
