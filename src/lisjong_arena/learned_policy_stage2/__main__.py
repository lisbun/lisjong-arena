"""Stage 2 CLI: `generate` -> `train` -> `test`（TESTはone-shot）。

```text
python -m lisjong_arena.learned_policy_stage2 generate --dataset DIR
python -m lisjong_arena.learned_policy_stage2 train    --dataset DIR --checkpoint DIR
python -m lisjong_arena.learned_policy_stage2 test     --dataset DIR --checkpoint DIR \
    --result FILE
```

`generate`と`train`はTEST partitionのmetricを一切計算しない。`test`だけが
frozen checkpointに対してTESTを1回評価し、その実行でのみTEST exposureを行う。
"""

import argparse
import platform
import resource
import sys
import time
from pathlib import Path

from lisjong_arena._artifact_io import canonical_json_text

from .artifact import (
    FEATURES_FILENAME,
    LEGAL_MASK_FILENAME,
    MANIFEST_FILENAME,
    ROWS_FILENAME,
    Stage2DatasetWriter,
    load_dataset,
)
from .coverage import build_coverage
from .decision_rule import classify_outcome
from .protocol import ORDERED_SEEDS, Split, verify_contract_identity
from .recording import RowEncodeCost, build_decision_rows, record_teacher_game
from .serving_check import run_serving_path_check


def _write_json(path: Path, document: dict) -> None:
    if path.exists():
        raise FileExistsError(f"{path} already exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(canonical_json_text(document), encoding="utf-8", newline="\n")


def _peak_ram_bytes() -> int:
    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(usage) * 1024 if platform.system() == "Linux" else int(usage)


def _generate(arguments: argparse.Namespace) -> int:
    verify_contract_identity()
    dataset_path = Path(arguments.dataset)
    writer = Stage2DatasetWriter(dataset_path)
    measurements = []
    try:
        for seed in ORDERED_SEEDS:
            recording = record_teacher_game(seed)
            cost: list[RowEncodeCost] = []
            entry = writer.add_game(
                seed=seed,
                split=recording.split,
                step_count=recording.result.steps,
                scores=recording.result.scores,
                ranks=recording.result.ranks,
                rows=build_decision_rows(recording, cost=cost),
            )
            measurements.append(
                {
                    "seed": seed,
                    "split": recording.split.value,
                    "decision_rows": entry.row_count,
                    "round_count": entry.round_count,
                    "wall_clock_seconds": recording.wall_clock_seconds,
                    "cpu_seconds": recording.cpu_seconds,
                    "feature_encode_seconds_total": cost[0].feature_encode_seconds,
                    "feature_encode_seconds_per_decision": (
                        cost[0].feature_encode_seconds / entry.row_count
                    ),
                }
            )
            print(
                f"seed={seed} split={recording.split.value} "
                f"rows={entry.row_count} "
                f"wall={recording.wall_clock_seconds:.2f}s",
                flush=True,
            )
        dataset = writer.finalize()
    except BaseException:
        writer.discard()
        raise

    non_finite = dataset.count_non_finite_features()
    coverage = build_coverage(dataset)
    file_bytes = {
        name: (dataset.path / name).stat().st_size
        for name in (
            MANIFEST_FILENAME,
            ROWS_FILENAME,
            FEATURES_FILENAME,
            LEGAL_MASK_FILENAME,
        )
    }
    total_bytes = sum(file_bytes.values())
    document = {
        "dataset_identity": dataset.identity,
        "non_finite_feature_count": non_finite,
        "games": measurements,
        "generation_totals": {
            "hanchan_count": len(measurements),
            "decision_rows": dataset.row_count,
            "wall_clock_seconds": sum(
                entry["wall_clock_seconds"] for entry in measurements
            ),
            "cpu_seconds": sum(entry["cpu_seconds"] for entry in measurements),
            "wall_clock_seconds_per_hanchan": sum(
                entry["wall_clock_seconds"] for entry in measurements
            )
            / len(measurements),
            "cpu_seconds_per_hanchan": sum(
                entry["cpu_seconds"] for entry in measurements
            )
            / len(measurements),
            "decision_rows_per_hanchan": dataset.row_count / len(measurements),
            "feature_encode_seconds_per_decision": sum(
                entry["feature_encode_seconds_total"] for entry in measurements
            )
            / dataset.row_count,
            "peak_process_ram_bytes": _peak_ram_bytes(),
        },
        "storage": {
            "file_bytes": file_bytes,
            "total_bytes": total_bytes,
            "bytes_per_hanchan": total_bytes / len(measurements),
            "bytes_per_row": total_bytes / dataset.row_count,
        },
        "coverage": coverage.to_document(),
    }
    _write_json(Path(arguments.report), document)
    print(f"dataset_identity={dataset.identity}")
    print(f"rows={dataset.row_count} non_finite_features={non_finite}")
    return 0


def _train(arguments: argparse.Namespace) -> int:
    from .training import save_checkpoint, train_stage2_model

    dataset = load_dataset(arguments.dataset)
    started = time.perf_counter()
    run = train_stage2_model(dataset)
    checkpoint = save_checkpoint(arguments.checkpoint, dataset, run)
    print(f"dataset_identity={dataset.identity}")
    print(f"checkpoint_identity={checkpoint.identity}")
    print(f"weights_sha256={checkpoint.weights_sha256}")
    print(f"selected_epoch={run.selected_epoch}")
    print(
        "selected_validation_choice_masked_ce="
        f"{run.selected_validation_choice_masked_ce:.6f}"
    )
    print(f"training_wall_clock_seconds={time.perf_counter() - started:.2f}")
    return 0


def _test(arguments: argparse.Namespace) -> int:
    from .evaluation import evaluate_split
    from .training import load_checkpoint, load_split_tensors

    dataset = load_dataset(arguments.dataset)
    checkpoint = load_checkpoint(arguments.checkpoint)
    if checkpoint.manifest["dataset_identity"] != dataset.identity:
        raise SystemExit("checkpoint was not trained on this dataset identity")

    tensors = load_split_tensors(dataset)
    metrics = {
        split: evaluate_split(checkpoint.model, dataset, tensors[split])
        for split in Split
    }
    serving = run_serving_path_check(checkpoint, dataset, split=Split.TEST)
    coverage = build_coverage(dataset)
    decision = classify_outcome(
        dataset_identity=dataset.identity,
        non_finite_feature_count=dataset.count_non_finite_features(),
        coverage=coverage,
        serving=serving,
        test_metrics=metrics[Split.TEST],
        test_exposure_count=1,
    )
    document = {
        "dataset_identity": dataset.identity,
        "checkpoint_identity": checkpoint.identity,
        "weights_sha256": checkpoint.weights_sha256,
        "selected_epoch": checkpoint.manifest["selected_epoch"],
        "runtime": checkpoint.manifest["runtime"],
        "metrics": {split.value: metrics[split].to_document() for split in Split},
        "generalization_gap": {
            "train_minus_test_choice_masked_ce": (
                metrics[Split.TRAIN].choice_masked_cross_entropy
                - metrics[Split.TEST].choice_masked_cross_entropy
            ),
            "validation_minus_test_choice_masked_ce": (
                metrics[Split.VALIDATION].choice_masked_cross_entropy
                - metrics[Split.TEST].choice_masked_cross_entropy
            ),
        },
        "serving_path": serving.to_document(),
        "decision": decision.to_document(),
    }
    _write_json(Path(arguments.result), document)
    test = metrics[Split.TEST]
    print(f"TEST choice rows              {test.choice_rows}")
    print(f"TEST choice masked CE         {test.choice_masked_cross_entropy:.6f}")
    print(f"uniform legal CE reference    {test.uniform_choice_cross_entropy:.6f}")
    print(f"TEST choice exact agreement   {test.choice_exact_agreement:.6f}")
    print(f"uniform exact agreement       {test.uniform_choice_exact_agreement:.6f}")
    print(f"hard gate passed              {decision.hard_gate_passed}")
    print(f"model-learning gate passed    {decision.model_learning_gate_passed}")
    print(f"outcome                       {decision.outcome.value}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m lisjong_arena.learned_policy_stage2",
        description="Learned Policy Stage 2 behavior-cloning vertical slice",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    generate = commands.add_parser("generate", help="generate the locked dataset")
    generate.add_argument("--dataset", required=True)
    generate.add_argument("--report", required=True)
    generate.set_defaults(handler=_generate)

    train = commands.add_parser("train", help="train the locked model")
    train.add_argument("--dataset", required=True)
    train.add_argument("--checkpoint", required=True)
    train.set_defaults(handler=_train)

    test = commands.add_parser("test", help="evaluate the frozen checkpoint once")
    test.add_argument("--dataset", required=True)
    test.add_argument("--checkpoint", required=True)
    test.add_argument("--result", required=True)
    test.set_defaults(handler=_test)

    arguments = parser.parse_args(argv)
    return arguments.handler(arguments)


if __name__ == "__main__":
    sys.exit(main())
