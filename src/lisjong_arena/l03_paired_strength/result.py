"""#385 seed-block statistics、terminal interpretation、write-once result。

resultはschedule順の全raw game recordを保持し、``verify_result``はrecorded
statisticsを信用せずraw recordから再導出した文書との完全一致だけを成功とする。
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from lisjong_arena._artifact_io import (
    ArtifactValidationError,
    canonical_json_text,
    expect_list,
    expect_object,
    read_json_document,
    write_new_artifact_file,
)
from lisjong_arena.paired_evaluation import (
    PairedEvaluationError,
    PairedSeedDelta,
    PairedSummary,
    summarize_paired_deltas,
)

from .execution import parse_game_record
from .lock import (
    document_identity,
    load_lock_document,
    parse_lock_document,
)
from .protocol import (
    ARMS,
    BASELINE_ARM,
    CANDIDATE_ARM,
    FINAL_POINTS_PER_PT,
    FOCAL_SEATS,
    HANCHAN_PER_BLOCK,
    IMPROVED_KIND,
    IMPROVED_LABEL,
    MINIMUM_VALID_PAIRED_UNITS,
    NOT_ESTABLISHED_KIND,
    NOT_ESTABLISHED_LABEL,
    PAIRED_UNIT_COUNT,
    PROTOCOL_ID,
    SEED_BLOCK_COUNT,
    PairedStrengthProtocolError,
    game_schedule,
)

RESULT_SCHEMA = "arena-l0.3-outcome-q-paired-strength-result-v1"


class PairedStrengthResultError(ValueError):
    """#385 resultを導出・検証できない場合（STOP / INVALID）。"""


def validate_records(records: object) -> tuple[dict[str, object], ...]:
    """全8,000 recordがfrozen scheduleと同順・同数であることを要求する。"""
    if not isinstance(records, (list, tuple)):
        raise PairedStrengthResultError("game records must be a list")
    schedule = game_schedule()
    if len(records) != len(schedule):
        raise PairedStrengthResultError(
            f"expected {len(schedule)} game records but got {len(records)}"
        )
    try:
        return tuple(
            parse_game_record(record, assignment)
            for record, assignment in zip(records, schedule, strict=True)
        )
    except PairedStrengthProtocolError as exc:
        raise PairedStrengthResultError(str(exc)) from exc


def derive_seed_blocks(
    records: tuple[dict[str, object], ...],
) -> tuple[tuple[PairedSeedDelta, ...], int]:
    """seed blockごとの``D(s)``と、final_points vectorが同一のpaired unit数。

    ``candidate_mean`` / ``parent_mean``はfocal final_points（内部単位）の
    4 focal seat平均、``delta``はその差である。
    """
    blocks: list[PairedSeedDelta] = []
    identical_units = 0
    for start in range(0, len(records), HANCHAN_PER_BLOCK):
        chunk = records[start : start + HANCHAN_PER_BLOCK]
        candidate_total = baseline_total = 0
        for focal_seat, offset in zip(FOCAL_SEATS, range(0, len(chunk), 2)):
            candidate, baseline = chunk[offset], chunk[offset + 1]
            if (candidate["arm"], baseline["arm"]) != (CANDIDATE_ARM, BASELINE_ARM):
                raise PairedStrengthResultError("paired unit arms are out of order")
            candidate_total += candidate["final_points"][focal_seat]  # type: ignore[index]
            baseline_total += baseline["final_points"][focal_seat]  # type: ignore[index]
            if candidate["final_points"] == baseline["final_points"]:
                identical_units += 1
        count = len(FOCAL_SEATS)
        blocks.append(
            PairedSeedDelta(
                seed=chunk[0]["seed"],  # type: ignore[arg-type]
                candidate_mean=candidate_total / count,
                parent_mean=baseline_total / count,
                delta=(candidate_total - baseline_total) / count,
            )
        )
    if len(blocks) != SEED_BLOCK_COUNT:
        raise PairedStrengthResultError("seed block count differs from the protocol")
    return tuple(blocks), identical_units


def classify(summary: PairedSummary) -> dict[str, object]:
    if summary.block_count != SEED_BLOCK_COUNT:
        raise PairedStrengthResultError("summary block count differs from protocol")
    if summary.interval_lower > 0:
        return {"kind": IMPROVED_KIND, "label": IMPROVED_LABEL}
    return {"kind": NOT_ESTABLISHED_KIND, "label": NOT_ESTABLISHED_LABEL}


def _pt(value: float) -> float:
    return value / FINAL_POINTS_PER_PT


def derive_secondary_diagnostics(
    records: tuple[dict[str, object], ...], identical_units: int
) -> dict[str, object]:
    """armごとのfocal diagnostic。primary classificationを上書きしない。"""
    arms: dict[str, object] = {}
    for arm in ARMS:
        rows = [record for record in records if record["arm"] == arm]
        focal = [(record, record["focal_seat"]) for record in rows]
        ranks = Counter(record["ranks"][seat] for record, seat in focal)  # type: ignore[index]
        count = len(rows)
        arms[arm] = {
            "focal_deal_ins": sum(record["focal_deal_ins"] for record in rows),  # type: ignore[misc]
            "focal_riichi_deposits": sum(
                record["focal_riichi_deposits"]  # type: ignore[misc]
                for record in rows
            ),
            "focal_wins": sum(record["focal_wins"] for record in rows),  # type: ignore[misc]
            "hanchan": count,
            "kyoku": sum(record["kyoku_count"] for record in rows),  # type: ignore[misc]
            "mean_focal_final_pt": _pt(
                sum(record["final_points"][seat] for record, seat in focal) / count  # type: ignore[index]
            ),
            "mean_focal_raw_final_score": sum(
                record["final_raw_scores"][seat]  # type: ignore[index]
                for record, seat in focal
            )
            / count,
            "mean_focal_rank": sum(rank * n for rank, n in ranks.items()) / count,
            "focal_rank_counts": {
                str(rank): ranks.get(rank, 0) for rank in (1, 2, 3, 4)
            },
        }
    return {
        "arms": arms,
        "identical_final_points_paired_units": identical_units,
        "overrides_primary": False,
    }


def build_result_document(
    *, lock_document: dict[str, object], records: object
) -> dict[str, object]:
    """lockとraw recordから決定的にresult documentを導出する。"""
    lock = parse_lock_document(lock_document)
    validated = validate_records(records)
    blocks, identical_units = derive_seed_blocks(validated)
    if len(validated) // 2 < MINIMUM_VALID_PAIRED_UNITS:
        raise PairedStrengthResultError("minimum valid paired-unit support not met")
    try:
        summary = summarize_paired_deltas(blocks)
    except PairedEvaluationError as exc:
        raise PairedStrengthResultError(str(exc)) from exc
    signs = Counter(
        "positive" if block.delta > 0 else "negative" if block.delta < 0 else "zero"
        for block in blocks
    )
    payload: dict[str, object] = {
        "classification": classify(summary),
        "game_records": [dict(record) for record in validated],
        "lock_identity": lock["lock_identity"],
        "primary": {
            "block_sign_counts": {
                key: signs.get(key, 0) for key in ("negative", "positive", "zero")
            },
            "paired_unit_count": PAIRED_UNIT_COUNT,
            "seed_blocks": [block.to_document() for block in blocks],
            "summary": summary.to_document(),
            "summary_pt": {
                "interval_lower": _pt(summary.interval_lower),
                "interval_upper": _pt(summary.interval_upper),
                "mean_delta": _pt(summary.mean_delta),
                "sample_standard_deviation": _pt(summary.sample_standard_deviation),
                "standard_error": _pt(summary.standard_error),
            },
            "unit": "engine final_points (1 = 0.1 pt); summary_pt in pt",
            "valid_paired_units": len(validated) // 2,
        },
        "protocol_id": PROTOCOL_ID,
        "schema": RESULT_SCHEMA,
        "secondary": derive_secondary_diagnostics(validated, identical_units),
    }
    return {**payload, "result_identity": document_identity(payload)}


def save_result_document(document: dict[str, object], path: str | Path) -> Path:
    destination = Path(path)
    if destination.exists():
        raise PairedStrengthResultError("result destination already exists")
    write_new_artifact_file(destination, canonical_json_text(document))
    return destination


def verify_result(
    *, lock_path: str | Path, result_path: str | Path
) -> dict[str, object]:
    """保存済みresultをraw recordから再導出し、完全一致を要求する。"""
    lock = load_lock_document(lock_path)
    try:
        stored = read_json_document(Path(result_path))
        raw = expect_object(
            stored,
            {
                "classification",
                "game_records",
                "lock_identity",
                "primary",
                "protocol_id",
                "result_identity",
                "schema",
                "secondary",
            },
            "result",
        )
        records = expect_list(raw["game_records"], "result.game_records")
    except (ArtifactValidationError, OSError, ValueError) as exc:
        raise PairedStrengthResultError(str(exc)) from exc
    if raw["lock_identity"] != lock["lock_identity"]:
        raise PairedStrengthResultError("result is bound to a different lock")
    expected = build_result_document(lock_document=lock, records=records)
    if dict(raw) != expected:
        raise PairedStrengthResultError(
            "stored result differs from the result re-derived from raw records"
        )
    return expected


__all__ = [
    "RESULT_SCHEMA",
    "PairedStrengthResultError",
    "build_result_document",
    "classify",
    "derive_secondary_diagnostics",
    "derive_seed_blocks",
    "save_result_document",
    "validate_records",
    "verify_result",
]
