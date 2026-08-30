"""Bounded formal Phase 6 train/validation command (TEST is not an option)."""

import argparse
import gc
import json
import platform
import sys
from importlib.metadata import distribution

from lisjong_arena.phase4_raw_corpus.persistence import load_raw_corpus
from lisjong_arena.phase5_belief_dataset.builder import resolve_training_samples
from lisjong_arena.phase5_belief_dataset.measurements import (
    expected_count_metrics_value,
)
from lisjong_arena.phase5_belief_dataset.persistence import load_belief_dataset

from .artifact import (
    ARTIFACT_SCHEMA_VERSION,
    artifact_logical_identity,
    save_model_artifact,
)
from .constraint import MAX_ITERATIONS, RESIDUAL_TOLERANCE
from .feature import FEATURE_SEMANTICS_ID
from .tensor import (
    CONCEALED_SLOT_SCALE,
    DISCARD_ORDER_SCALE,
    DRAW_COUNT_SCALE,
    EVIDENCE_POSITION_SCALE,
    FEATURE_DIM,
    HONBA_SCALE,
    LIVE_WALL_SCALE,
    RESPONSE_COUNT_SCALE,
    RIICHI_STICK_SCALE,
    SCORE_SCALE,
    TILE_COUNT_SCALE,
)
from .training import (
    LOCKED_DATASET_IDENTITY,
    LOCKED_RAW_CORPUS_IDENTITY,
    FeatureCoverage,
    aggregate_feature_coverage,
    prepare_train_validation_data,
    train_phase6_model,
    verify_phase5_validation_compatibility,
)


def _revision(value: str) -> str:
    if len(value) != 40 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise argparse.ArgumentTypeError("revision must be a full lowercase commit SHA")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Train the locked Phase 6 model on TRAIN/VALIDATION only."
    )
    parser.add_argument("--raw", required=True)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--artifact", required=True)
    parser.add_argument("--lisjong-revision", required=True, type=_revision)
    parser.add_argument("--engine-revision", required=True, type=_revision)
    parser.add_argument("--arena-revision", required=True, type=_revision)
    return parser


def _installed_revision(distribution_name: str) -> str:
    direct_url = distribution(distribution_name).read_text("direct_url.json")
    if direct_url is None:
        raise RuntimeError(f"{distribution_name} lacks direct_url.json provenance")
    try:
        value = json.loads(direct_url)["vcs_info"]["commit_id"]
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise RuntimeError(
            f"{distribution_name} lacks resolved VCS revision provenance"
        ) from error
    return _revision(value)


def _manifest_without_weights(
    *,
    dataset,
    result,
    training_revisions: dict[str, str],
    feature_coverage: dict[str, object],
) -> dict[str, object]:
    import torch

    config = result.config
    source = dataset.provenance.source_revisions
    return {
        "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
        "raw_corpus_identity": dataset.raw_corpus_identity,
        "dataset_identity": dataset.dataset_identity,
        "dataset_source_revisions": {
            "lisjong": source.lisjong,
            "lisjong_engine": source.lisjong_engine,
            "lisjong_arena": source.lisjong_arena,
        },
        "training_source_revisions": training_revisions,
        "feature_semantics_id": FEATURE_SEMANTICS_ID,
        "feature_dimension": FEATURE_DIM,
        "feature_coverage": feature_coverage,
        "tensorization": {
            "normalization": "fixed-semantic-scaling-no-train-statistics",
            "categorical": "fixed-one-hot-no-embeddings",
            "silent_clipping": False,
            "scales": {
                "honba": HONBA_SCALE,
                "riichi_sticks": RIICHI_STICK_SCALE,
                "live_wall": LIVE_WALL_SCALE,
                "discard_order": DISCARD_ORDER_SCALE,
                "evidence_position": EVIDENCE_POSITION_SCALE,
                "score": SCORE_SCALE,
                "tile_count": TILE_COUNT_SCALE,
                "concealed_slot": CONCEALED_SLOT_SCALE,
                "draw_count": DRAW_COUNT_SCALE,
                "response_count": RESPONSE_COUNT_SCALE,
            },
        },
        "model": {
            "family": "feed-forward-919-128-64-136",
            "hidden_widths": [128, 64],
            "activation": "relu",
            "output_shape": [4, 34],
            "dropout": False,
            "batch_normalization": False,
        },
        "parameter_count": result.parameter_count,
        "constraint": {
            "algorithm": "log-domain-ipfp",
            "dtype": "float64",
            "row_semantics": ["opponent", "opponent", "opponent", "other_hidden"],
            "column_semantics": "remaining_tile_counts[34]",
            "max_iterations": MAX_ITERATIONS,
            "residual_tolerance": RESIDUAL_TOLERANCE,
        },
        "training": {
            "device": "cpu",
            "objective": "post-constraint-opponent-expected-count-mse",
            "optimizer": "Adam",
            "learning_rate": config.learning_rate,
            "weight_decay": config.weight_decay,
            "batch_size": config.batch_size,
            "maximum_epochs": config.max_epochs,
            "early_stopping_monitor": "validation_mse",
            "early_stopping_patience": config.patience,
            "early_stopping_min_delta": 0,
            "early_stopping_improvement": "strictly_lower",
            "early_stopping_tie": "keep_earlier_checkpoint",
            "seed": config.seed,
            "train_shuffle": True,
            "validation_shuffle": False,
            "dataloader_generator_seed": config.dataloader_seed,
            "dataloader_workers": config.workers,
            "drop_last": config.drop_last,
            "train_partition_seeds": "100..139",
            "validation_partition_seeds": "140..149",
            "test_partition_seeds": "sealed:150..159",
            "train_samples": result.train_metrics.sample_count,
            "validation_samples": result.validation_metrics.sample_count,
        },
        "runtime": {
            "python": platform.python_version(),
            "torch": torch.__version__,
            "os": platform.platform(),
            "device": "cpu",
            "torch_thread_count": config.torch_threads,
            "deterministic_algorithms": config.deterministic_algorithms,
        },
        "selected_epoch": result.selected_epoch,
        "loss_history": [
            {
                "epoch": value.epoch,
                "train_mse": value.train_mse,
                "validation_mse": value.validation_mse,
            }
            for value in result.history
        ],
        "train_metrics": {
            "mse": result.train_mse,
            **expected_count_metrics_value(result.train_metrics),
        },
        "validation_metrics": {
            "mse": result.validation_mse,
            **expected_count_metrics_value(result.validation_metrics),
        },
        "training_wall_clock_seconds": result.training_wall_clock_seconds,
        "peak_process_ram_bytes": result.peak_process_ram_bytes,
        "inference_throughput": {
            "samples_per_second": result.inference_throughput.samples_per_second,
            "batch_size": result.inference_throughput.batch_size,
            "torch_thread_count": result.inference_throughput.torch_thread_count,
            "platform": result.inference_throughput.platform,
        },
        "constraint_maximum_residual": result.constraint_maximum_residual,
        "constraint_non_convergence_count": result.constraint_non_convergence_count,
        "test_partition_evaluated": False,
    }


def _feature_coverage_value(value: FeatureCoverage) -> dict[str, object]:
    return {
        "samples": value.samples,
        "opponent_riichi_declaration_rows": value.opponent_riichi_declaration_rows,
        "opponent_call_history_rows": value.opponent_call_history_rows,
        "opponent_kan_history_rows": value.opponent_kan_history_rows,
        "meld_kind_counts": dict(
            zip(
                ("chi", "pon", "daiminkan", "ankan", "kakan"),
                value.meld_kind_counts,
                strict=True,
            )
        ),
        "public_draw_source_counts": dict(
            zip(("live_wall", "rinshan"), value.public_draw_source_counts, strict=True)
        ),
        "response_history_counts": {
            f"{trigger}:{outcome}": value.response_history_counts[
                trigger_index * 3 + outcome_index
            ]
            for trigger_index, trigger in enumerate(("discard", "kakan", "ankan"))
            for outcome_index, outcome in enumerate(
                ("no_public_response", "call", "ron")
            )
        },
    }


def main(argv: list[str] | None = None) -> int:
    import torch

    arguments = _parser().parse_args(argv)
    if sys.version_info[:2] != (3, 14):
        raise RuntimeError("formal Phase 6 requires CPython 3.14")
    if torch.__version__ != "2.13.0+cpu":
        raise RuntimeError("formal Phase 6 requires PyTorch 2.13.0 CPU")
    if torch.cuda.is_available():
        raise RuntimeError("formal Phase 6 is CPU-only")
    if _installed_revision("lisjong") != arguments.lisjong_revision:
        raise RuntimeError(
            "declared lisjong revision differs from installed provenance"
        )
    if _installed_revision("lisjong-engine") != arguments.engine_revision:
        raise RuntimeError(
            "declared lisjong-engine revision differs from installed provenance"
        )
    raw = load_raw_corpus(arguments.raw)
    persisted_dataset = load_belief_dataset(arguments.dataset)
    dataset = persisted_dataset.dataset
    if raw.corpus_identity != LOCKED_RAW_CORPUS_IDENTITY:
        raise RuntimeError("raw artifact is not the locked Phase 5 corpus")
    if dataset.raw_corpus_identity != raw.corpus_identity:
        raise RuntimeError("dataset and raw corpus identities differ")
    if dataset.dataset_identity != LOCKED_DATASET_IDENTITY:
        raise RuntimeError("dataset artifact is not the locked Phase 5 dataset")
    samples = resolve_training_samples(dataset, raw)
    baseline = verify_phase5_validation_compatibility(dataset, samples)
    data = prepare_train_validation_data(dataset, samples)
    feature_coverage = {
        "train": _feature_coverage_value(aggregate_feature_coverage(data.train)),
        "validation": _feature_coverage_value(
            aggregate_feature_coverage(data.validation)
        ),
    }
    del raw, samples
    gc.collect()
    result = train_phase6_model(data, dataset_identity=dataset.dataset_identity)
    revisions = {
        "lisjong": arguments.lisjong_revision,
        "lisjong_engine": arguments.engine_revision,
        "lisjong_arena": arguments.arena_revision,
    }
    loaded = save_model_artifact(
        arguments.artifact,
        result.model,
        _manifest_without_weights(
            dataset=dataset,
            result=result,
            training_revisions=revisions,
            feature_coverage=feature_coverage,
        ),
    )
    output = {
        "artifact_logical_identity": artifact_logical_identity(loaded.manifest),
        "weights_sha256": loaded.manifest["weights_sha256"],
        "weights_bytes": loaded.manifest["weights_bytes"],
        "raw_corpus_identity": dataset.raw_corpus_identity,
        "dataset_identity": dataset.dataset_identity,
        "feature_semantics_id": FEATURE_SEMANTICS_ID,
        "feature_dimension": FEATURE_DIM,
        "parameter_count": result.parameter_count,
        "feature_coverage": feature_coverage,
        "train_samples": len(data.train),
        "validation_samples": len(data.validation),
        "phase5_validation_compatibility": expected_count_metrics_value(baseline),
        "selected_epoch": result.selected_epoch,
        "training_wall_clock_seconds": result.training_wall_clock_seconds,
        "train_mse": result.train_mse,
        "validation_mse": result.validation_mse,
        "train_metrics": expected_count_metrics_value(result.train_metrics),
        "validation_metrics": expected_count_metrics_value(result.validation_metrics),
        "constraint_maximum_residual": result.constraint_maximum_residual,
        "constraint_non_convergence_count": result.constraint_non_convergence_count,
        "peak_process_ram_bytes": result.peak_process_ram_bytes,
        "inference_throughput": loaded.manifest["inference_throughput"],
        "test_partition_evaluated": False,
    }
    print(json.dumps(output, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
