"""Bounded F0 audit of immutable #258 retained sidecars; no game execution."""

import json
from pathlib import Path

from lisjong.policy_contract.riichi import RiichiState

from lisjong_arena._artifact_io import canonical_json_text
from lisjong_arena.learned_policy_offline_q.artifact import (
    PROVENANCE_FIELDS,
    provenance_document,
)
from lisjong_arena.learned_policy_offline_q.protocol import (
    LOCKED_FEATURE_SCHEMA_FINGERPRINT,
    LOCKED_TENSOR_SCHEMA_VERSION,
    LOCKED_VOCABULARY_FINGERPRINT,
)
from lisjong_arena.stage_a0_tenpai_feasibility.labels import TargetAvailability
from lisjong_arena.stage_a0_tenpai_feasibility.protocol import (
    RETAINED_DATASET_IDENTITY,
    RETAINED_TRAIN_SEEDS,
    exact_wait_implementation_identity,
)
from lisjong_arena.stage_a0_tenpai_feasibility.sidecar import (
    ROUTE_RETAINED,
    SIDECAR_SCHEMA_VERSION,
    load_sidecar,
    recompute_target,
)

from .labels import (
    WaitShapeAvailability as A,
)
from .labels import (
    WaitShapeQualificationError,
    build_wait_shape_target_from_retained_cell,
)
from .protocol import (
    LISJONG_ENGINE_REVISION,
    RIICHIENV_VERSION,
    TEACHER_LISJONG_REVISION,
    protocol_lock_document,
    protocol_lock_identity,
)

F0_SCHEMA_VERSION = "arena-retained-wait-shape-f0-v1"
QUALIFIED = "RETAINED WAIT-SHAPE LABEL PATH QUALIFIED"
NOT_QUALIFIED = "RETAINED WAIT-SHAPE LABEL PATH NOT QUALIFIED"
_COUNTS = {
    "retained_riichi_excluded_cells_examined",
    "same_state_cells",
    "canonical_labels_produced",
    "same_state_binding_failures",
    "multi_positive_count",
    "all_six_channel_zero_count",
    "ordinary_five_all_zero_kokushi_positive_count",
    "kokushi_descriptive_count",
}


def _require(condition, message):
    if not condition:
        raise WaitShapeQualificationError(message)


def _hex(value, size):
    return (
        type(value) is str
        and len(value) == size
        and all(c in "0123456789abcdef" for c in value)
    )


def _identity():
    return {
        "protocol_lock_identity": protocol_lock_identity(),
        "target": protocol_lock_document()["target"],
        "sidecar_schema_version": SIDECAR_SCHEMA_VERSION,
        "public_row_contract": "stage_a0_tenpai_feasibility.public_row.PublicDecisionRow",
        "tensor_schema_version": LOCKED_TENSOR_SCHEMA_VERSION,
        "feature_schema_fingerprint": LOCKED_FEATURE_SCHEMA_FINGERPRINT,
        "vocabulary_fingerprint": LOCKED_VOCABULARY_FINGERPRINT,
    }


def _outcome(counts, reasons):
    passed = (
        counts["same_state_binding_failures"] == 0
        and reasons[A.CANONICAL_BUILDER_FAILURE.value] == 0
        and reasons[A.INVALID_SEMANTIC_PROJECTION.value] == 0
        and counts["canonical_labels_produced"] > 0
    )
    return QUALIFIED if passed else NOT_QUALIFIED


def _validate(document):
    _require(
        type(document) is dict
        and set(document)
        == {
            "schema_version",
            "identity",
            "execution",
            "source",
            "counts",
            "unavailable_reason_counts",
            "outcome",
        },
        "invalid F0 fields",
    )
    _require(document["schema_version"] == F0_SCHEMA_VERSION, "unsupported F0 schema")
    _require(document["identity"] == _identity(), "incompatible F0 identity")
    execution = document["execution"]
    _require(
        type(execution) is dict
        and set(execution)
        == {
            "lisjong_arena_revision",
            "lisjong_revision",
            "lisjong_engine_revision",
            "riichienv_version",
            "canonical_wait_implementation_identity",
        },
        "invalid execution fields",
    )
    _require(_hex(execution["lisjong_arena_revision"], 40), "invalid Arena revision")
    for name, value in (
        ("lisjong_revision", TEACHER_LISJONG_REVISION),
        ("lisjong_engine_revision", LISJONG_ENGINE_REVISION),
        ("riichienv_version", RIICHIENV_VERSION),
        (
            "canonical_wait_implementation_identity",
            exact_wait_implementation_identity(),
        ),
    ):
        _require(execution[name] == value, f"incompatible execution {name}")
    source = document["source"]
    _require(
        type(source) is dict
        and set(source)
        == {
            "sidecar_identity",
            "dataset_identity",
            "ordered_seeds",
            "provenance",
            "canonical_wait_implementation_identity",
        },
        "invalid source fields",
    )
    _require(_hex(source["sidecar_identity"], 64), "invalid sidecar identity")
    _require(
        _hex(source["canonical_wait_implementation_identity"], 64),
        "invalid source builder identity",
    )
    _require(
        source["dataset_identity"] == RETAINED_DATASET_IDENTITY,
        "unexpected retained dataset",
    )
    seeds = source["ordered_seeds"]
    _require(
        type(seeds) is list
        and all(type(s) is int and s in RETAINED_TRAIN_SEEDS for s in seeds),
        "invalid retained seeds",
    )
    _require(seeds == sorted(set(seeds)), "duplicate or unordered retained seeds")
    provenance = source["provenance"]
    _require(
        type(provenance) is dict
        and set(provenance) == set(PROVENANCE_FIELDS)
        and all(type(v) is str and v for v in provenance.values()),
        "invalid source provenance",
    )
    for name in (
        "lisjong_arena_revision",
        "lisjong_revision",
        "lisjong_engine_revision",
    ):
        _require(_hex(provenance[name], 40), f"invalid source {name}")
    counts = document["counts"]
    reasons = document["unavailable_reason_counts"]
    _require(type(counts) is dict and set(counts) == _COUNTS, "invalid count fields")
    _require(
        type(reasons) is dict
        and set(reasons) == {a.value for a in A if a is not A.AVAILABLE},
        "invalid unavailable reasons",
    )
    _require(
        all(type(v) is int and v >= 0 for v in (*counts.values(), *reasons.values())),
        "counts must be nonnegative exact integers",
    )
    examined = counts["retained_riichi_excluded_cells_examined"]
    labels = counts["canonical_labels_produced"]
    _require(examined == labels + sum(reasons.values()), "cell totals disagree")
    _require(
        labels
        + sum(
            reasons[a.value]
            for a in (
                A.INVALID_PHYSICAL_INVENTORY,
                A.NOT_STABLE_13_EQUIVALENT,
                A.CANONICAL_BUILDER_FAILURE,
                A.INVALID_SEMANTIC_PROJECTION,
                A.NO_STRUCTURAL_WAIT,
            )
        )
        <= counts["same_state_cells"]
        <= examined
        - counts["same_state_binding_failures"]
        - reasons[A.HIDDEN_HAND_UNAVAILABLE.value]
        - reasons[A.MELD_STATE_UNAVAILABLE.value],
        "same-state totals disagree",
    )
    _require(
        reasons[A.RIICHI_BINDING_MISMATCH.value]
        <= counts["same_state_binding_failures"]
        <= reasons[A.RIICHI_BINDING_MISMATCH.value]
        + reasons[A.NOT_ACCEPTED_RIICHI.value],
        "binding totals disagree",
    )
    _require(
        counts["all_six_channel_zero_count"] == reasons[A.NO_STRUCTURAL_WAIT.value],
        "zero-channel totals disagree",
    )
    kokushi_only = counts["ordinary_five_all_zero_kokushi_positive_count"]
    _require(
        kokushi_only <= counts["kokushi_descriptive_count"] <= labels,
        "KOKUSHI totals disagree",
    )
    _require(
        counts["kokushi_descriptive_count"] - kokushi_only
        <= counts["multi_positive_count"]
        <= labels - kokushi_only,
        "multi-positive totals disagree",
    )
    _require(
        document["outcome"] == _outcome(counts, reasons), "F0 classification disagrees"
    )
    return document


def audit_retained_sidecar(path):
    """Audit all RIICHI_EXCLUDED cells in the supplied retained TRAIN sidecar.

    Same-state means the #258 co-emitted binding is retained with both hand and
    meld state and consistent public/private riichi. This does not replay games
    or claim an independent reconstruction of the original hidden state.
    """
    sidecar = load_sidecar(path)
    _require(sidecar.route == ROUTE_RETAINED, "F0 requires retained augmentation")
    _require(
        sidecar.manifest["protocol"]["source_identity"] == RETAINED_DATASET_IDENTITY,
        "unexpected retained dataset",
    )
    provenance = provenance_document()
    execution = {
        name: provenance[name]
        for name in (
            "lisjong_arena_revision",
            "lisjong_revision",
            "lisjong_engine_revision",
            "riichienv_version",
        )
    }
    execution["canonical_wait_implementation_identity"] = (
        exact_wait_implementation_identity()
    )
    counts = dict.fromkeys(sorted(_COUNTS), 0)
    reasons = {a.value: 0 for a in A if a is not A.AVAILABLE}
    document = {
        "schema_version": F0_SCHEMA_VERSION,
        "identity": _identity(),
        "execution": execution,
        "source": {
            "sidecar_identity": sidecar.identity,
            "dataset_identity": sidecar.manifest["protocol"]["source_identity"],
            "ordered_seeds": sorted({c.row_identity.seed for c in sidecar.cells}),
            "provenance": sidecar.manifest["provenance"],
            "canonical_wait_implementation_identity": sidecar.manifest[
                "canonical_wait_implementation"
            ]["implementation_identity"],
        },
        "counts": counts,
        "unavailable_reason_counts": reasons,
        "outcome": NOT_QUALIFIED,
    }
    # Check compatibility before invoking the canonical builder.
    _validate(document)
    rows = {}
    for cell in sidecar.cells:
        rows.setdefault(cell.row_identity, []).append(cell)
    for cells in rows.values():
        _require(
            tuple(c.identity.viewer_relative_offset for c in cells) == (1, 2, 3),
            "missing, duplicate or unordered opponent cells",
        )
        _require(
            len({c.public_row_digest for c in cells}) == 1
            and len({c.dealer_seat for c in cells}) == 1,
            "inconsistent same-row binding",
        )
    for cell in sidecar.cells:
        # Non-riichi labels are outside F0; do not recompute or summarize them.
        if cell.availability is not TargetAvailability.RIICHI_EXCLUDED:
            _require(
                cell.public_riichi_state is RiichiState.NONE
                and not cell.privileged_riichi_declared,
                "public-riichi cell has incompatible #258 classification",
            )
            continue
        _require(
            recompute_target(cell) == cell.target, "incompatible #258 riichi exclusion"
        )
        counts["retained_riichi_excluded_cells_examined"] += 1
        bound = (
            cell.public_riichi_state is not RiichiState.NONE
        ) == cell.privileged_riichi_declared
        counts["same_state_binding_failures"] += int(not bound)
        counts["same_state_cells"] += int(
            bound and cell.concealed_tiles is not None and cell.melds is not None
        )
        target = build_wait_shape_target_from_retained_cell(cell)
        if target.availability is not A.AVAILABLE:
            reasons[target.availability.value] += 1
            counts["all_six_channel_zero_count"] += int(
                target.availability is A.NO_STRUCTURAL_WAIT
            )
            continue
        projection = target.projection
        counts["canonical_labels_produced"] += 1
        counts["multi_positive_count"] += int(
            sum((*projection.ordinary, projection.kokushi)) > 1
        )
        counts["ordinary_five_all_zero_kokushi_positive_count"] += int(
            projection.is_kokushi_only
        )
        counts["kokushi_descriptive_count"] += projection.kokushi
    document["outcome"] = _outcome(counts, reasons)
    return _validate(document)


def write_f0_report(path, document):
    """Write validated canonical JSON exclusively; never overwrite evidence."""
    text = canonical_json_text(_validate(document))
    with Path(path).open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(text)


def load_f0_report(path):
    """Reject unknown fields, incompatible identity and contradictory counts."""
    text = Path(path).read_text(encoding="utf-8")
    document = _validate(json.loads(text))
    _require(text == canonical_json_text(document), "F0 report must be canonical JSON")
    return document
