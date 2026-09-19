"""Issue #250 Overall result artifact and bundle-level strict verification.

conceptual bundle:

```text
<bundle>/
  overall-lock.json      pre-execution participant / protocol / provenance lock
  comparison.json        既存 ComparisonArtifact そのもの
  overall-result.json    このmoduleが所有するpurpose-specific result
```

``overall-result.json``のrecorded statisticsは検証時に信用しない。verifyは
必ずstrict-readした``comparison.json``のraw seat-resultsからseed-block
statistics、95% interval、secondary diagnostics、classificationを再導出し、
recorded documentと完全一致することだけを成功条件とする。result fieldsを
自己整合的に書き換えてresult identityを計算し直しても、raw evidenceとの
re-derivation mismatchで拒否される。
"""

from __future__ import annotations

from pathlib import Path

from lisjong_arena._artifact_io import (
    ArtifactValidationError,
    canonical_json_text,
    expect_float,
    expect_int,
    expect_list,
    expect_object,
    expect_str,
    read_json_document,
    write_new_artifact_file,
)
from lisjong_arena._execution_safety import (
    ExecutionSafetyError,
    require_new_artifact_destinations,
)
from lisjong_arena.artifact import (
    ARTIFACT_SCHEMA_VERSION,
    COMPARISON_PROTOCOL,
    ComparisonArtifact,
    ComparisonArtifactError,
    load_comparison_artifact,
)
from lisjong_arena.paired_evaluation import PairedSummary, artifact_file_digest

from .lock import (
    OverallChampionLockError,
    document_identity,
    load_lock_document,
    locked_participants,
    locked_seeds,
    parse_lock_document,
    require_comparison_provenance,
)
from .protocol import (
    HEURISTIC_FAMILY,
    LEARNING_FAMILY,
    OverallChampionProtocolError,
    parse_participant_binding,
    protocol_document,
    require_protocol_document,
)
from .statistics import (
    OverallChampionStatisticsError,
    OverallSeedBlock,
    block_sign_counts,
    classify,
    derive_secondary_diagnostics,
    derive_seed_blocks,
    summarize_seed_blocks,
)

RESULT_VERSION = 1


class OverallChampionResultError(ValueError):
    """Overall result artifactまたはbundleが不正な場合。

    このerrorはexhaustive classificationの``STOP / INVALID``に対応し、
    invalid evidenceを通常のsuperiority resultへ変換しない。
    """


def _comparison_document(
    artifact: ComparisonArtifact, path: str | Path
) -> dict[str, object]:
    return {
        "artifact_digest": artifact_file_digest(path),
        "comparison_protocol": artifact.comparison_protocol,
        "provenance": {
            "execution_environment": artifact.provenance.execution_environment,
            "lisjong_arena_version": artifact.provenance.lisjong_arena_version,
            "lisjong_revision": artifact.provenance.lisjong_revision,
            "lisjong_version": artifact.provenance.lisjong_version,
            "python_version": artifact.provenance.python_version,
            "riichienv_version": artifact.provenance.riichienv_version,
        },
        "schema_version": artifact.schema_version,
    }


def _primary_document(
    blocks: tuple[OverallSeedBlock, ...], summary: PairedSummary
) -> dict[str, object]:
    return {
        "block_sign_counts": block_sign_counts(blocks),
        "seed_blocks": [block.to_document() for block in blocks],
        "summary": summary.to_document(),
    }


def build_overall_result(
    *,
    lock_document: dict[str, object],
    comparison_artifact: ComparisonArtifact,
    comparison_artifact_path: str | Path,
) -> dict[str, object]:
    """lockとraw comparison evidenceからOverall result documentを構築する。"""
    lock = parse_lock_document(lock_document)
    heuristic, learning = locked_participants(lock)
    seeds = locked_seeds(lock)
    require_comparison_provenance(lock, comparison_artifact.provenance)

    blocks = derive_seed_blocks(
        comparison_artifact,
        heuristic=heuristic,
        learning=learning,
        seeds=seeds,
    )
    summary = summarize_seed_blocks(blocks)
    diagnostics = derive_secondary_diagnostics(
        comparison_artifact, heuristic=heuristic, learning=learning
    )
    payload: dict[str, object] = {
        "classification": classify(summary),
        "comparison": _comparison_document(
            comparison_artifact, comparison_artifact_path
        ),
        "lock_identity": lock["lock_identity"],
        "participants": {
            HEURISTIC_FAMILY: heuristic.to_document(),
            LEARNING_FAMILY: learning.to_document(),
        },
        "primary": _primary_document(blocks, summary),
        "protocol": protocol_document(seeds),
        "result_version": RESULT_VERSION,
        "secondary_diagnostics": diagnostics.to_document(),
    }
    document = dict(payload)
    document["result_identity"] = document_identity(payload)
    return document


def save_overall_result(document: dict[str, object], path: str | Path) -> Path:
    """result artifactをwrite-once JSON fileへ保存する。"""
    destination = Path(path)
    try:
        require_new_artifact_destinations(
            {"overall_result": destination}, required_names=("overall_result",)
        )
    except ExecutionSafetyError as exc:
        raise OverallChampionResultError(str(exc)) from exc
    write_new_artifact_file(destination, canonical_json_text(document))
    return destination


_RESULT_FIELDS = {
    "classification",
    "comparison",
    "lock_identity",
    "participants",
    "primary",
    "protocol",
    "result_identity",
    "result_version",
    "secondary_diagnostics",
}

_PRIMARY_FIELDS = {"block_sign_counts", "seed_blocks", "summary"}
_SIGN_COUNT_FIELDS = {
    "negative_block_count",
    "positive_block_count",
    "zero_block_count",
}
_SUMMARY_FIELDS = {
    "block_count",
    "interval_lower",
    "interval_upper",
    "mean_delta",
    "sample_standard_deviation",
    "standard_error",
}
_SEED_BLOCK_FIELDS = {
    "delta",
    "heuristic_mean_rank",
    "learning_mean_rank",
    "seed",
}
_CLASSIFICATION_FIELDS = {"kind", "label", "rule_id"}


def _parse_seed_block(value: object, index: int) -> OverallSeedBlock:
    context = f"overall_result.primary.seed_blocks[{index}]"
    raw = expect_object(value, _SEED_BLOCK_FIELDS, context)
    return OverallSeedBlock(
        seed=expect_int(raw["seed"], f"{context}.seed"),
        heuristic_mean_rank=expect_float(
            raw["heuristic_mean_rank"], f"{context}.heuristic_mean_rank"
        ),
        learning_mean_rank=expect_float(
            raw["learning_mean_rank"], f"{context}.learning_mean_rank"
        ),
        delta=expect_float(raw["delta"], f"{context}.delta"),
    )


def _parse_summary(value: object) -> PairedSummary:
    context = "overall_result.primary.summary"
    raw = expect_object(value, _SUMMARY_FIELDS, context)
    return PairedSummary(
        block_count=expect_int(raw["block_count"], f"{context}.block_count"),
        mean_delta=expect_float(raw["mean_delta"], f"{context}.mean_delta"),
        sample_standard_deviation=expect_float(
            raw["sample_standard_deviation"],
            f"{context}.sample_standard_deviation",
        ),
        standard_error=expect_float(raw["standard_error"], f"{context}.standard_error"),
        interval_lower=expect_float(raw["interval_lower"], f"{context}.interval_lower"),
        interval_upper=expect_float(raw["interval_upper"], f"{context}.interval_upper"),
    )


def parse_overall_result(value: object) -> dict[str, object]:
    """result documentをstrictに読み戻し、自己整合性を検証する。

    ここで確認するのはdocument内部の整合だけである。raw comparison evidence
    との一致は``verify_overall_bundle()``が担当する。
    """
    try:
        return _parse_overall_result(value)
    except (
        OverallChampionResultError,
        OverallChampionProtocolError,
        OverallChampionStatisticsError,
    ) as exc:
        raise OverallChampionResultError(str(exc)) from exc
    except ArtifactValidationError as exc:
        raise OverallChampionResultError(str(exc)) from exc
    except (TypeError, ValueError, KeyError) as exc:
        raise OverallChampionResultError(str(exc)) from exc


def _parse_overall_result(value: object) -> dict[str, object]:
    raw = expect_object(value, _RESULT_FIELDS, "overall_result")
    if (
        expect_int(raw["result_version"], "overall_result.result_version")
        != RESULT_VERSION
    ):
        raise OverallChampionResultError("unsupported overall result version")

    seeds = require_protocol_document(raw["protocol"], "overall_result.protocol")

    participants = expect_object(
        raw["participants"],
        {HEURISTIC_FAMILY, LEARNING_FAMILY},
        "overall_result.participants",
    )
    heuristic = parse_participant_binding(
        participants[HEURISTIC_FAMILY],
        f"overall_result.participants.{HEURISTIC_FAMILY}",
    )
    learning = parse_participant_binding(
        participants[LEARNING_FAMILY],
        f"overall_result.participants.{LEARNING_FAMILY}",
    )
    if heuristic.family != HEURISTIC_FAMILY or learning.family != LEARNING_FAMILY:
        raise OverallChampionResultError("result participant families drifted")

    primary = expect_object(raw["primary"], _PRIMARY_FIELDS, "overall_result.primary")
    blocks = tuple(
        _parse_seed_block(item, index)
        for index, item in enumerate(
            expect_list(primary["seed_blocks"], "overall_result.primary.seed_blocks")
        )
    )
    if tuple(block.seed for block in blocks) != seeds:
        raise OverallChampionResultError(
            "persisted seed blocks differ from the locked ordered population"
        )
    for index, block in enumerate(blocks):
        if block.delta != block.learning_mean_rank - block.heuristic_mean_rank:
            raise OverallChampionResultError(
                f"persisted seed block {index} delta is not "
                "learning_mean_rank - heuristic_mean_rank"
            )
    summary = _parse_summary(primary["summary"])
    if summary != summarize_seed_blocks(blocks):
        raise OverallChampionResultError(
            "persisted primary summary differs from the persisted seed blocks"
        )
    expect_object(
        primary["block_sign_counts"],
        _SIGN_COUNT_FIELDS,
        "overall_result.primary.block_sign_counts",
    )
    if primary["block_sign_counts"] != block_sign_counts(blocks):
        raise OverallChampionResultError(
            "persisted block sign counts differ from the persisted seed blocks"
        )

    expect_object(
        raw["classification"],
        _CLASSIFICATION_FIELDS,
        "overall_result.classification",
    )
    if raw["classification"] != classify(summary):
        raise OverallChampionResultError(
            "persisted classification differs from the persisted primary summary"
        )

    comparison = expect_object(
        raw["comparison"],
        {"artifact_digest", "comparison_protocol", "provenance", "schema_version"},
        "overall_result.comparison",
    )
    if (
        expect_str(
            comparison["comparison_protocol"],
            "overall_result.comparison.comparison_protocol",
        )
        != COMPARISON_PROTOCOL
    ):
        raise OverallChampionResultError("comparison protocol drifted")
    if (
        expect_int(
            comparison["schema_version"], "overall_result.comparison.schema_version"
        )
        != ARTIFACT_SCHEMA_VERSION
    ):
        raise OverallChampionResultError("comparison schema version drifted")
    expect_str(
        comparison["artifact_digest"], "overall_result.comparison.artifact_digest"
    )
    expect_object(
        comparison["provenance"],
        {
            "execution_environment",
            "lisjong_arena_version",
            "lisjong_revision",
            "lisjong_version",
            "python_version",
            "riichienv_version",
        },
        "overall_result.comparison.provenance",
    )

    _require_secondary_shape(raw["secondary_diagnostics"])
    expect_str(raw["lock_identity"], "overall_result.lock_identity")

    payload = {key: raw[key] for key in raw if key != "result_identity"}
    if expect_str(
        raw["result_identity"], "overall_result.result_identity"
    ) != document_identity(payload):
        raise OverallChampionResultError("overall result identity mismatch")
    return dict(raw)


_FAMILY_DIAGNOSTIC_FIELDS = {
    "average_rank",
    "average_score",
    "family",
    "first_count",
    "fourth_count",
    "game_count",
    "policy_identity",
    "seat_mean_ranks",
    "seat_mean_scores",
    "seat_result_count",
    "second_count",
    "third_count",
}


def _require_secondary_shape(value: object) -> None:
    raw = expect_object(
        value,
        {
            "heuristic",
            "learning",
            "mean_final_score_difference",
            "overrides_primary_classification",
            "sign_interpretation",
        },
        "overall_result.secondary_diagnostics",
    )
    if raw["overrides_primary_classification"] is not False:
        raise OverallChampionResultError(
            "secondary diagnostics must never override the primary classification"
        )
    expect_float(
        raw["mean_final_score_difference"],
        "overall_result.secondary_diagnostics.mean_final_score_difference",
    )
    expect_str(
        raw["sign_interpretation"],
        "overall_result.secondary_diagnostics.sign_interpretation",
    )
    for family in (HEURISTIC_FAMILY, LEARNING_FAMILY):
        context = f"overall_result.secondary_diagnostics.{family}"
        family_raw = expect_object(raw[family], _FAMILY_DIAGNOSTIC_FIELDS, context)
        if expect_str(family_raw["family"], f"{context}.family") != family:
            raise OverallChampionResultError(f"{context}.family drifted")
        for name in (
            "first_count",
            "fourth_count",
            "game_count",
            "seat_result_count",
            "second_count",
            "third_count",
        ):
            expect_int(family_raw[name], f"{context}.{name}")
        for name in ("average_rank", "average_score"):
            expect_float(family_raw[name], f"{context}.{name}")
        for name in ("seat_mean_ranks", "seat_mean_scores"):
            values = expect_list(family_raw[name], f"{context}.{name}")
            if len(values) != 4:
                raise OverallChampionResultError(
                    f"{context}.{name} must contain exactly four values"
                )
            for index, item in enumerate(values):
                expect_float(item, f"{context}.{name}[{index}]")


def load_overall_result(path: str | Path) -> dict[str, object]:
    """result artifactをstrict-readする。"""
    try:
        return parse_overall_result(read_json_document(Path(path)))
    except OverallChampionResultError:
        raise
    except ArtifactValidationError as exc:
        raise OverallChampionResultError(str(exc)) from exc
    except (TypeError, ValueError, KeyError) as exc:
        raise OverallChampionResultError(str(exc)) from exc


def load_bundle_comparison(path: str | Path) -> ComparisonArtifact:
    """bundleのcomparison artifactを既存strict readbackで読み戻す。"""
    try:
        return load_comparison_artifact(path)
    except ComparisonArtifactError as exc:
        raise OverallChampionResultError(str(exc)) from exc


def verify_overall_bundle(
    *,
    lock_path: str | Path,
    comparison_path: str | Path,
    result_path: str | Path,
) -> dict[str, object]:
    """bundle全体をstrictに検証する。

    ```text
    lock strict read
        -> comparison strict read
        -> lock / comparison plan & provenance cross-binding
        -> raw seat-result validation
        -> seed-block rank statistics re-derive
        -> 95% interval re-derive
        -> secondary diagnostics re-derive
        -> classification re-derive
        -> result artifact comparison
        -> result identity validation
    ```

    どの段階でもfail closedし、成功時だけ検証済みresult documentを返す。
    """
    try:
        lock = load_lock_document(lock_path)
    except (OverallChampionLockError, OverallChampionProtocolError) as exc:
        raise OverallChampionResultError(str(exc)) from exc

    comparison = load_bundle_comparison(comparison_path)
    document = load_overall_result(result_path)

    try:
        expected = build_overall_result(
            lock_document=lock,
            comparison_artifact=comparison,
            comparison_artifact_path=comparison_path,
        )
    except (
        OverallChampionLockError,
        OverallChampionProtocolError,
        OverallChampionStatisticsError,
    ) as exc:
        raise OverallChampionResultError(str(exc)) from exc

    if document != expected:
        raise OverallChampionResultError(
            "overall result differs from the value re-derived from the locked "
            "comparison evidence"
        )
    return document


__all__ = [
    "RESULT_VERSION",
    "OverallChampionResultError",
    "build_overall_result",
    "load_bundle_comparison",
    "load_overall_result",
    "parse_overall_result",
    "save_overall_result",
    "verify_overall_bundle",
]
