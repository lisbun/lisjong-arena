"""Issue #252 Phase B — paired seed-block primary statisticとclassification。

primary unitは1 ordered seedであり、各seed ``s``について

```text
P_s = P armのfocal seat final scoreを4 rotationsで平均
C_s = C armのfocal seat final scoreを4 rotationsで平均
D_s = P_s - C_s
```

を取る。summaryは100個の``D_s``に対する``mean``とnormal-approx 95% interval
だけである。400 P games + 400 C gamesを800 independent samplesとしては扱わない。

## classification

```text
lower > 0   -> PROGRESSION DEVELOPMENT SIGNAL
upper < 0   -> PROGRESSION DEVELOPMENT NEGATIVE
otherwise   -> PROGRESSION DEVELOPMENT INCONCLUSIVE
```

secondary diagnosticsはdescriptiveであり、``classify_paired_summary()``の
引数にも入らない。classificationを書き換える経路を型の形で持たない。

## このmoduleが所有しないもの

game execution、rotation、artifact schema、canonical strength aggregationは
既存Arena contractが所有する。ここはstrict-readした2本のarm artifactから
paired値をraw evidenceで再導出し、事前登録したruleを1回だけ適用する。

seed-block validation、paired delta導出、paired summary statistics、arm
diagnostics、artifact digestといったmechanicsは
``lisjong_arena.paired_evaluation``がArena-owned reusable primitiveとして
所有する。このmoduleはそれらを複製せず、#252固有のidentity / population /
protocol / classification / artifact schemaだけを載せる。
"""

from __future__ import annotations

import math
from pathlib import Path

from lisjong_arena import paired_evaluation
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
from lisjong_arena._execution_safety import require_new_artifact_destinations
from lisjong_arena.model import SingleRoundGameResult
from lisjong_arena.paired_evaluation import (
    INTERVAL_Z,
    PairedEvaluationError,
    PairedSeedDelta,
    PairedSummary,
    arm_diagnostics,
    artifact_file_digest,
    load_arm_artifact,
    paired_deltas_from_block_means,
)
from lisjong_arena.single_round_artifact import (
    SingleRoundStrengthArtifact,
    execution_provenance_to_dict,
)

from .protocol import (
    CANDIDATE_IDENTITY,
    CLASSIFICATION_RULE_ID,
    COMPARATOR_IDENTITY,
    INCONCLUSIVE_LABEL,
    MAX_STEPS,
    NEGATIVE_LABEL,
    PARENT_IDENTITY,
    PHASE_B_GAMES_PER_ARM,
    PHASE_B_SEEDS,
    SIGNAL_LABEL,
    ProgressionProtocolError,
    document_identity,
    progression_diagnostics_availability,
    protocol_document,
    require_exact_candidate_semantics,
    require_exact_comparator,
    require_phase_b_population,
)

PAIRED_RESULT_VERSION = 1
"""paired result documentのschema version。"""

_DELTA_FIELDS = {"candidate_mean", "delta", "parent_mean", "seed"}

_SUMMARY_FIELDS = {
    "block_count",
    "interval_lower",
    "interval_upper",
    "mean_delta",
    "sample_standard_deviation",
    "standard_error",
}

_CLASSIFICATION_FIELDS = {"kind", "label", "rule_id"}

_ARM_FIELDS = {
    "artifact_digest",
    "candidate_identity",
    "comparator_identity",
    "diagnostics",
    "game_count",
    "ordered_seeds",
    "plan_identity",
}

_RESULT_FIELDS = {
    "arms",
    "candidate_binding",
    "classification",
    "comparator_binding",
    "paired_deltas",
    "primary_summary",
    "progression_diagnostics",
    "protocol",
    "provenance",
    "result_identity",
    "result_version",
    "worker_count",
}


class PairedResultError(ValueError):
    """paired derivation / classification / readbackが成立しない場合。"""


def focal_seed_block_means(
    game_results: tuple[SingleRoundGameResult, ...],
) -> tuple[tuple[int, float], ...]:
    """neutral seed-block mechanicsを#252のerror contextで包む。

    validation内容とfloat computation orderはArena-owned mechanicsが所有し、
    ここは複製しない。#252 pathで送出するerror typeだけを維持する。
    """
    try:
        return paired_evaluation.focal_seed_block_means(game_results)
    except PairedEvaluationError as exc:
        raise PairedResultError(str(exc)) from exc


def _require_arm_artifact(
    artifact: SingleRoundStrengthArtifact,
    *,
    expected_identity: str,
    arm: str,
) -> None:
    if not isinstance(artifact, SingleRoundStrengthArtifact):
        raise PairedResultError(f"{arm} arm artifact must be a strength artifact")
    if artifact.plan.candidate_identity != expected_identity:
        raise PairedResultError(
            f"{arm} arm artifact candidate identity is "
            f"{artifact.plan.candidate_identity!r}, not {expected_identity!r}"
        )
    if artifact.plan.baseline_identity != COMPARATOR_IDENTITY:
        raise PairedResultError(
            f"{arm} arm artifact baseline identity is "
            f"{artifact.plan.baseline_identity!r}, not the locked passive comparator"
        )
    try:
        require_phase_b_population(artifact.plan.seeds)
    except ProgressionProtocolError as exc:
        raise PairedResultError(f"{arm} arm artifact: {exc}") from exc
    if artifact.plan.max_steps != MAX_STEPS:
        raise PairedResultError(
            f"{arm} arm artifact max_steps is {artifact.plan.max_steps!r}, not the "
            f"locked {MAX_STEPS!r}"
        )
    if len(artifact.game_results) != PHASE_B_GAMES_PER_ARM:
        raise PairedResultError(
            f"{arm} arm artifact must contain exactly {PHASE_B_GAMES_PER_ARM} games, "
            f"got {len(artifact.game_results)}"
        )


def derive_paired_deltas(
    candidate_artifact: SingleRoundStrengthArtifact,
    parent_artifact: SingleRoundStrengthArtifact,
) -> tuple[PairedSeedDelta, ...]:
    """2本のarm artifactから100個の``D_s``をraw evidenceで再導出する。

    両armが同じordered seedsと同じrotation shapeを使っていることを確認し、
    partial armや順序違いをfail closedする。
    """
    _require_arm_artifact(
        candidate_artifact, expected_identity=CANDIDATE_IDENTITY, arm="candidate"
    )
    _require_arm_artifact(
        parent_artifact, expected_identity=PARENT_IDENTITY, arm="parent"
    )
    if candidate_artifact.plan.seeds != parent_artifact.plan.seeds:
        raise PairedResultError("both arms must use the same ordered seeds")

    candidate_blocks = focal_seed_block_means(candidate_artifact.game_results)
    parent_blocks = focal_seed_block_means(parent_artifact.game_results)
    if len(candidate_blocks) != len(PHASE_B_SEEDS):
        raise PairedResultError(
            f"expected {len(PHASE_B_SEEDS)} paired seed blocks, "
            f"got {len(candidate_blocks)}"
        )
    if tuple(seed for seed, _ in candidate_blocks) != PHASE_B_SEEDS:
        raise PairedResultError("candidate arm seed blocks are not the locked order")
    if tuple(seed for seed, _ in parent_blocks) != PHASE_B_SEEDS:
        raise PairedResultError("parent arm seed blocks are not the locked order")

    try:
        return paired_deltas_from_block_means(candidate_blocks, parent_blocks)
    except PairedEvaluationError as exc:
        raise PairedResultError(str(exc)) from exc


def summarize_paired_deltas(
    deltas: tuple[PairedSeedDelta, ...],
) -> PairedSummary:
    """neutral paired summary mechanicsを#252のerror contextで包む。"""
    try:
        return paired_evaluation.summarize_paired_deltas(deltas)
    except PairedEvaluationError as exc:
        raise PairedResultError(str(exc)) from exc


def classify_paired_summary(summary: PairedSummary) -> dict[str, object]:
    """事前登録したruleを1回だけ適用する。

    引数はprimary summaryだけである。secondary diagnosticsやgame countは
    受け取らないため、classificationを後から書き換える経路を持たない。
    """
    if not isinstance(summary, PairedSummary):
        raise PairedResultError("summary must be a PairedSummary")
    lower = summary.interval_lower
    upper = summary.interval_upper
    if not math.isfinite(lower) or not math.isfinite(upper) or lower > upper:
        raise PairedResultError("paired interval is not a finite ordered interval")
    if lower > 0.0:
        kind, label = "SIGNAL", SIGNAL_LABEL
    elif upper < 0.0:
        kind, label = "NEGATIVE", NEGATIVE_LABEL
    else:
        kind, label = "INCONCLUSIVE", INCONCLUSIVE_LABEL
    return {"kind": kind, "label": label, "rule_id": CLASSIFICATION_RULE_ID}


def _arm_document(
    artifact: SingleRoundStrengthArtifact, artifact_path: str | Path
) -> dict[str, object]:
    return {
        # ``artifact_digest``は保存されたartifact fileそのもののsha256であり、
        # ``plan_identity``はcandidate / comparator / ordered seedsだけを束ねた
        # plan条件のidentityである。前者が内容、後者が条件を固定する。
        "artifact_digest": artifact_file_digest(artifact_path),
        "candidate_identity": artifact.plan.candidate_identity,
        "comparator_identity": artifact.plan.baseline_identity,
        "diagnostics": arm_diagnostics(artifact),
        "game_count": len(artifact.game_results),
        "ordered_seeds": list(artifact.plan.seeds),
        "plan_identity": document_identity(
            {
                "baseline_identity": artifact.plan.baseline_identity,
                "candidate_identity": artifact.plan.candidate_identity,
                "seeds": list(artifact.plan.seeds),
            }
        ),
    }


def build_paired_result(
    *,
    candidate_artifact: SingleRoundStrengthArtifact,
    candidate_artifact_path: str | Path,
    parent_artifact: SingleRoundStrengthArtifact,
    parent_artifact_path: str | Path,
    worker_count: int,
) -> dict[str, object]:
    """strict-read済み2 armから、classified paired result documentを作る。"""
    if type(worker_count) is not int or worker_count <= 0:
        raise PairedResultError("worker_count must be a positive int")
    if candidate_artifact.provenance != parent_artifact.provenance:
        raise PairedResultError("both arms must share the same execution provenance")

    deltas = derive_paired_deltas(candidate_artifact, parent_artifact)
    summary = summarize_paired_deltas(deltas)
    classification = classify_paired_summary(summary)
    payload: dict[str, object] = {
        "arms": {
            "candidate": _arm_document(candidate_artifact, candidate_artifact_path),
            "parent": _arm_document(parent_artifact, parent_artifact_path),
        },
        "candidate_binding": require_exact_candidate_semantics().to_document(),
        "classification": classification,
        "comparator_binding": require_exact_comparator(),
        "paired_deltas": [item.to_document() for item in deltas],
        "primary_summary": summary.to_document(),
        "progression_diagnostics": progression_diagnostics_availability(),
        "protocol": protocol_document(),
        "provenance": execution_provenance_to_dict(candidate_artifact.provenance),
        "result_version": PAIRED_RESULT_VERSION,
        "worker_count": worker_count,
    }
    document = dict(payload)
    document["result_identity"] = document_identity(payload)
    return document


def save_paired_result(document: dict[str, object], path: str | Path) -> Path:
    """paired resultをwrite-onceで保存する。"""
    if not isinstance(document, dict):
        raise TypeError("document must be a dict")
    destination = Path(path)
    require_new_artifact_destinations(
        {"paired_result": destination}, required_names=("paired_result",)
    )
    write_new_artifact_file(destination, canonical_json_text(document))
    return destination


def _parse_delta(value: object, index: int) -> PairedSeedDelta:
    context = f"paired_deltas[{index}]"
    document = expect_object(value, _DELTA_FIELDS, context)
    return PairedSeedDelta(
        seed=expect_int(document["seed"], f"{context}.seed"),
        candidate_mean=expect_float(
            document["candidate_mean"], f"{context}.candidate_mean"
        ),
        parent_mean=expect_float(document["parent_mean"], f"{context}.parent_mean"),
        delta=expect_float(document["delta"], f"{context}.delta"),
    )


def _parse_summary(value: object) -> PairedSummary:
    document = expect_object(value, _SUMMARY_FIELDS, "primary_summary")
    return PairedSummary(
        block_count=expect_int(document["block_count"], "primary_summary.block_count"),
        mean_delta=expect_float(document["mean_delta"], "primary_summary.mean_delta"),
        sample_standard_deviation=expect_float(
            document["sample_standard_deviation"],
            "primary_summary.sample_standard_deviation",
        ),
        standard_error=expect_float(
            document["standard_error"], "primary_summary.standard_error"
        ),
        interval_lower=expect_float(
            document["interval_lower"], "primary_summary.interval_lower"
        ),
        interval_upper=expect_float(
            document["interval_upper"], "primary_summary.interval_upper"
        ),
    )


def parse_paired_result(value: object) -> dict[str, object]:
    """strict readbackでpaired resultを読み戻し、再導出と一致を確認する。"""
    document = expect_object(value, _RESULT_FIELDS, "paired result")
    version = expect_int(document["result_version"], "result_version")
    if version != PAIRED_RESULT_VERSION:
        raise PairedResultError(f"unsupported paired result version: {version!r}")
    protocol = expect_object(document["protocol"], set(protocol_document()), "protocol")
    if protocol != protocol_document():
        raise PairedResultError("paired result protocol block does not match")

    deltas = tuple(
        _parse_delta(item, index)
        for index, item in enumerate(
            expect_list(document["paired_deltas"], "paired_deltas")
        )
    )
    if tuple(item.seed for item in deltas) != PHASE_B_SEEDS:
        raise PairedResultError("paired deltas are not the locked seed population")
    summary = _parse_summary(document["primary_summary"])
    rederived = summarize_paired_deltas(deltas)
    if rederived != summary:
        raise PairedResultError(
            "stored primary summary does not match the paired deltas"
        )
    classification = expect_object(
        document["classification"], _CLASSIFICATION_FIELDS, "classification"
    )
    if classification != classify_paired_summary(summary):
        raise PairedResultError(
            "stored classification does not match the pre-registered rule"
        )
    expected_candidate = require_exact_candidate_semantics().to_document()
    if (
        expect_object(
            document["candidate_binding"], set(expected_candidate), "candidate_binding"
        )
        != expected_candidate
    ):
        raise PairedResultError(
            "paired result candidate binding is not the locked #170 generation"
        )
    expected_comparator = require_exact_comparator()
    if (
        expect_object(
            document["comparator_binding"],
            set(expected_comparator),
            "comparator_binding",
        )
        != expected_comparator
    ):
        raise PairedResultError(
            "paired result comparator binding is not the locked passive T"
        )
    expected_diagnostics = progression_diagnostics_availability()
    if (
        expect_object(
            document["progression_diagnostics"],
            set(expected_diagnostics),
            "progression_diagnostics",
        )
        != expected_diagnostics
    ):
        raise PairedResultError(
            "paired result diagnostics availability does not match the current seam"
        )
    expect_str(document["result_identity"], "result_identity")
    worker_count = expect_int(document["worker_count"], "worker_count")
    if worker_count <= 0:
        raise PairedResultError("paired result worker count must be positive")
    for arm in ("candidate", "parent"):
        arm_document = expect_object(
            expect_object(document["arms"], {"candidate", "parent"}, "arms")[arm],
            _ARM_FIELDS,
            f"arms.{arm}",
        )
        expect_str(arm_document["artifact_digest"], f"arms.{arm}.artifact_digest")
        expect_int(arm_document["game_count"], f"arms.{arm}.game_count")

    payload = {
        name: item for name, item in document.items() if name != "result_identity"
    }
    if document_identity(payload) != document["result_identity"]:
        raise PairedResultError("paired result identity does not match its content")
    return document


def load_paired_result(path: str | Path) -> dict[str, object]:
    """保存済みpaired resultをstrict readbackで読み戻す(self-consistencyのみ)。

    raw evidenceとの突き合わせまで行う最終verificationは
    ``verify_paired_result()``を使う。
    """
    try:
        return parse_paired_result(read_json_document(Path(path)))
    except ArtifactValidationError as exc:
        raise PairedResultError(str(exc)) from exc


def verify_paired_result(
    path: str | Path,
    *,
    candidate_artifact_path: str | Path,
    parent_artifact_path: str | Path,
) -> dict[str, object]:
    """paired resultを保存済みarm artifactのraw evidenceまで遡って検証する。

    self-consistency(``parse_paired_result()``)だけでは、documentを書き換えた
    うえで``result_identity``まで再計算したresultを弾けない。ここでは

    ```text
    P / C arm artifactをstrict read
    -> artifact digest / plan identity照合
    -> raw game resultsから100個のD_sを再導出
    -> primary summaryを再導出
    -> classificationを再導出
    -> stored paired result全体と一致
    ```

    まで確認する。raw evidenceと違うresultはidentityが揃っていても拒否する。
    """
    document = load_paired_result(path)
    candidate_artifact = load_arm_artifact(candidate_artifact_path)
    parent_artifact = load_arm_artifact(parent_artifact_path)

    arms = document["arms"]
    for name, artifact, artifact_path in (
        ("candidate", candidate_artifact, candidate_artifact_path),
        ("parent", parent_artifact, parent_artifact_path),
    ):
        stored = arms[name]  # type: ignore[index]
        rederived = _arm_document(artifact, artifact_path)
        if stored != rederived:
            raise PairedResultError(
                f"stored {name} arm block does not match the arm artifact on disk"
            )

    deltas = derive_paired_deltas(candidate_artifact, parent_artifact)
    if [item.to_document() for item in deltas] != document["paired_deltas"]:
        raise PairedResultError(
            "stored paired deltas do not match the arm artifacts' raw game results"
        )
    summary = summarize_paired_deltas(deltas)
    if summary.to_document() != document["primary_summary"]:
        raise PairedResultError(
            "stored primary summary does not match the re-derived paired evidence"
        )
    if classify_paired_summary(summary) != document["classification"]:
        raise PairedResultError(
            "stored classification does not match the re-derived paired evidence"
        )
    if candidate_artifact.provenance != parent_artifact.provenance:
        raise PairedResultError("both arms must share the same execution provenance")
    if (
        execution_provenance_to_dict(candidate_artifact.provenance)
        != (document["provenance"])
    ):
        raise PairedResultError(
            "stored provenance does not match the arm artifacts' provenance"
        )
    return document


__all__ = [
    "INTERVAL_Z",
    "PAIRED_RESULT_VERSION",
    "PairedResultError",
    "PairedSeedDelta",
    "PairedSummary",
    "arm_diagnostics",
    "artifact_file_digest",
    "build_paired_result",
    "classify_paired_summary",
    "derive_paired_deltas",
    "focal_seed_block_means",
    "load_arm_artifact",
    "load_paired_result",
    "parse_paired_result",
    "save_paired_result",
    "summarize_paired_deltas",
    "verify_paired_result",
]
