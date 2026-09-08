"""One-shot Issue #190 training and artifact assembly."""

import time
from pathlib import Path
from shutil import rmtree
from tempfile import mkdtemp

from lisjong_arena._artifact_io import canonical_json_text
from lisjong_arena.learned_policy_offline_q.bc_training import train_from_split_tensors
from lisjong_arena.learned_policy_offline_q.protocol import Split

from .artifact import (
    CHECKPOINTS_DIRNAME,
    RESULT_FILENAME,
    load_artifact,
    result_identity,
    save_scale_checkpoint,
    validate_result,
)
from .errors import DataSufficiencyError
from .metrics import (
    classify_comparison,
    evaluate_validation_rows,
    paired_comparison,
    summarize_validation_rows,
)
from .protocol import (
    PROTOCOL_ID,
    RESULT_SCHEMA_VERSION,
    RETENTION_KEY_PREFIX,
    SCALES,
    plan_document,
)
from .source import LoadedDataSufficiencySource, scale_tensors


def _interpretation_boundary() -> dict[str, object]:
    return {
        "clear_signal": (
            "current corpus remains visibly data-sensitive; return to #45 and do not "
            "implement P2 automatically"
        ),
        "no_clear_signal": (
            "no clear late-curve signal in this bounded preflight; this does not prove "
            "saturation or general data sufficiency"
        ),
        "formal_generalization_claim": False,
        "strength_claim": False,
    }


def build_result(
    *,
    source: LoadedDataSufficiencySource,
    backend: str,
    key: str,
    scales: dict[str, dict[str, object]],
) -> dict[str, object]:
    comparison = paired_comparison(scales)
    document: dict[str, object] = {
        "result_schema_version": RESULT_SCHEMA_VERSION,
        "protocol_id": PROTOCOL_ID,
        "plan": plan_document(),
        "source_dataset": {
            "identity": source.identity,
            "provenance": dict(source.provenance),
        },
        "retention": {"backend": backend, "key": key},
        "scales": scales,
        "primary_comparison": comparison,
        "classification": classify_comparison(comparison).value,
        "interpretation_boundary": _interpretation_boundary(),
    }
    document["result_identity"] = result_identity(document)
    return validate_result(document)


def run_experiment(
    *,
    source: LoadedDataSufficiencySource,
    destination: str | Path,
    backend: str,
    key: str,
):
    """Train S5/S10/S15/S20 once and atomically publish a strict artifact."""
    if not isinstance(source, LoadedDataSufficiencySource):
        raise TypeError("source must be a LoadedDataSufficiencySource")
    if type(backend) is not str or not backend:
        raise DataSufficiencyError("retention backend must be non-empty")
    if (
        type(key) is not str
        or not key.startswith(RETENTION_KEY_PREFIX)
        or key == RETENTION_KEY_PREFIX
    ):
        raise DataSufficiencyError(
            "retention key must use the Issue #190 artifact namespace"
        )
    destination = Path(destination)
    if destination.exists():
        raise FileExistsError("data-sufficiency artifact destination already exists")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        mkdtemp(prefix=f".{destination.name}-staging-", dir=destination.parent)
    )
    published = False
    try:
        checkpoint_root = staging / CHECKPOINTS_DIRNAME
        checkpoint_root.mkdir()
        scale_results: dict[str, dict[str, object]] = {}
        validation_row_indices: tuple[int, ...] | None = None
        for scale in SCALES:
            tensors = scale_tensors(source, scale)
            validation = tensors[Split.VALIDATION]
            if validation_row_indices is None:
                validation_row_indices = validation.row_indices
            elif validation.row_indices != validation_row_indices:
                raise DataSufficiencyError(
                    "VALIDATION population differs between TRAIN scales"
                )
            cpu_start = time.process_time()
            run = train_from_split_tensors(tensors)
            cpu_seconds = time.process_time() - cpu_start
            validation_rows = evaluate_validation_rows(run.model, validation)
            validation_summary = summarize_validation_rows(validation_rows)
            train = tensors[Split.TRAIN]
            checkpoint = save_scale_checkpoint(
                checkpoint_root / scale,
                scale=scale,
                source=source,
                run=run,
                train_row_count=train.row_count,
            )
            scale_results[scale] = {
                "scale": scale,
                "train_seeds": list(
                    dict.fromkeys(row.seed for row in train.source_rows)
                ),
                "train_hanchan_count": len(set(row.seed for row in train.source_rows)),
                "train_row_count": train.row_count,
                "train_eligible_decision_count": train.row_count,
                "training_identity": checkpoint.manifest["training_identity"],
                "checkpoint_identity": checkpoint.identity,
                "checkpoint_weights_sha256": checkpoint.manifest["weights_sha256"],
                "selected_epoch": run.selected_epoch,
                "selected_validation_choice_masked_ce": (
                    run.selected_validation_choice_masked_ce
                ),
                "training_wall_clock_seconds": run.wall_clock_seconds,
                "training_cpu_seconds": cpu_seconds,
                "validation_rows": validation_rows,
                "validation": validation_summary,
            }
        result = build_result(
            source=source, backend=backend, key=key, scales=scale_results
        )
        (staging / RESULT_FILENAME).write_text(
            canonical_json_text(result), encoding="utf-8", newline="\n"
        )
        load_artifact(staging)
        staging.rename(destination)
        published = True
    finally:
        if not published:
            rmtree(staging, ignore_errors=True)
    return load_artifact(destination)


__all__ = ["build_result", "run_experiment"]
