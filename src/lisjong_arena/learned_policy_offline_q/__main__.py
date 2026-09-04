"""Offline Q CLI: `generate` -> `train-bc` / `train-q` -> `test` -> `smoke`
-> `freeze` -> `screen` (Issue #140).

```text
python -m lisjong_arena.learned_policy_offline_q generate  --dataset DIR --report FILE
python -m lisjong_arena.learned_policy_offline_q train-bc  --dataset DIR --checkpoint DIR
python -m lisjong_arena.learned_policy_offline_q train-q   --dataset DIR --checkpoint DIR
python -m lisjong_arena.learned_policy_offline_q test      --dataset DIR \
    --bc-checkpoint DIR --q-checkpoint DIR --result FILE
python -m lisjong_arena.learned_policy_offline_q smoke     \
    --bc-checkpoint DIR --q-checkpoint DIR --report FILE
python -m lisjong_arena.learned_policy_offline_q freeze    \
    --bc-checkpoint DIR --q-checkpoint DIR \
    --retention-backend NAME --retention-root DIR --retention-key KEY
python -m lisjong_arena.learned_policy_offline_q screen    \
    --bundle DIR --artifact FILE --result FILE
```

`generate`/`train-bc`/`train-q`はTEST partitionのmetricを一切計算しない。
`test`だけがfrozen checkpointに対してTESTを1回評価し、その実行でのみTEST
exposureを行う。`smoke`はserving semantics検証のみでmodel tuningへ使わない。
`screen`はvalid smoke後にのみ実行する。
"""

import argparse
import sys
from pathlib import Path

from lisjong_arena._artifact_io import canonical_json_text

from .artifact import (
    FEATURES_FILENAME,
    LEGAL_MASK_FILENAME,
    MANIFEST_FILENAME,
    NEXT_FEATURES_FILENAME,
    NEXT_LEGAL_MASK_FILENAME,
    ROWS_FILENAME,
    OfflineQDatasetWriter,
    load_dataset,
)
from .protocol import (
    DATASET_ORDERED_SEEDS,
    SERVING_SMOKE_SEEDS,
    Split,
    verify_contract_identity,
)
from .recording import record_teacher_game
from .support import build_support_gate_report
from .transitions import build_macro_transitions


def _write_json(path: Path, document: dict) -> None:
    if path.exists():
        raise FileExistsError(f"{path} already exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(canonical_json_text(document), encoding="utf-8", newline="\n")


def _generate(arguments: argparse.Namespace) -> int:
    verify_contract_identity()
    dataset_path = Path(arguments.dataset)
    writer = OfflineQDatasetWriter(dataset_path)
    measurements = []
    try:
        for seed in DATASET_ORDERED_SEEDS:
            recording = record_teacher_game(seed)
            entry = writer.add_game(
                seed=seed,
                split=recording.split,
                scores=recording.result.scores,
                ranks=recording.result.ranks,
                rows=build_macro_transitions(recording),
            )
            measurements.append(
                {
                    "seed": seed,
                    "split": recording.split.value,
                    "macro_transition_rows": entry.row_count,
                    "wall_clock_seconds": recording.wall_clock_seconds,
                    "cpu_seconds": recording.cpu_seconds,
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
    support = build_support_gate_report(dataset)
    file_bytes = {
        name: (dataset.path / name).stat().st_size
        for name in (
            MANIFEST_FILENAME,
            ROWS_FILENAME,
            FEATURES_FILENAME,
            LEGAL_MASK_FILENAME,
            NEXT_FEATURES_FILENAME,
            NEXT_LEGAL_MASK_FILENAME,
        )
    }
    document = {
        "dataset_identity": dataset.identity,
        "non_finite_feature_count": non_finite,
        "games": measurements,
        "generation_totals": {
            "hanchan_count": len(measurements),
            "macro_transition_rows": dataset.row_count,
            "wall_clock_seconds": sum(
                entry["wall_clock_seconds"] for entry in measurements
            ),
            "cpu_seconds": sum(entry["cpu_seconds"] for entry in measurements),
        },
        "storage": {
            "file_bytes": file_bytes,
            "total_bytes": sum(file_bytes.values()),
        },
        "support_gate": support.to_document(),
    }
    _write_json(Path(arguments.report), document)
    print(f"dataset_identity={dataset.identity}")
    print(f"rows={dataset.row_count} non_finite_features={non_finite}")
    print(f"supported_indices={len(support.supported_indices)}")
    print(f"unsupported_indices={len(support.unsupported_indices)}")
    print(
        f"combined_support_complete_rate={support.combined_support_complete_rate:.6f}"
    )
    return 0


def _train_bc(arguments: argparse.Namespace) -> int:
    from .bc_training import save_checkpoint, train_bc_model

    dataset = load_dataset(arguments.dataset)
    run = train_bc_model(dataset)
    checkpoint = save_checkpoint(arguments.checkpoint, dataset, run)
    print(f"dataset_identity={dataset.identity}")
    print(f"checkpoint_identity={checkpoint.identity}")
    print(f"selected_epoch={run.selected_epoch}")
    print(
        "selected_validation_choice_masked_ce="
        f"{run.selected_validation_choice_masked_ce:.6f}"
    )
    return 0


def _train_q(arguments: argparse.Namespace) -> int:
    from .q_training import save_checkpoint, train_q_model

    dataset = load_dataset(arguments.dataset)
    run = train_q_model(dataset)
    checkpoint = save_checkpoint(arguments.checkpoint, dataset, run)
    print(f"dataset_identity={dataset.identity}")
    print(f"checkpoint_identity={checkpoint.identity}")
    print(f"selected_epoch={run.selected_epoch}")
    print(f"final_validation_huber_loss={run.final_validation_huber_loss:.6f}")
    print(f"supported_indices={int(run.support_mask.sum())}")
    return 0


def _test(arguments: argparse.Namespace) -> int:
    from . import bc_training, q_training
    from .exposure_evaluation import evaluate_bc_test, evaluate_q_test
    from .split_tensors import load_split_tensors

    dataset = load_dataset(arguments.dataset)
    bc_checkpoint = bc_training.load_checkpoint(arguments.bc_checkpoint)
    q_checkpoint = q_training.load_checkpoint(arguments.q_checkpoint)
    for name, checkpoint in (("BC", bc_checkpoint), ("Q", q_checkpoint)):
        if checkpoint.manifest["dataset_identity"] != dataset.identity:
            raise SystemExit(f"{name} checkpoint was not trained on this dataset")

    tensors = load_split_tensors(dataset)
    bc_diagnostics = evaluate_bc_test(bc_checkpoint.model, tensors[Split.TEST])
    q_diagnostics = evaluate_q_test(
        q_checkpoint.model, tensors[Split.TRAIN], tensors[Split.TEST]
    )
    document = {
        "dataset_identity": dataset.identity,
        "bc_checkpoint_identity": bc_checkpoint.identity,
        "q_checkpoint_identity": q_checkpoint.identity,
        "bc": bc_diagnostics.to_document(),
        "q": q_diagnostics.to_document(),
    }
    _write_json(Path(arguments.result), document)
    print(f"BC choice masked CE      {bc_diagnostics.choice_masked_cross_entropy:.6f}")
    print(f"BC choice exact agreement {bc_diagnostics.choice_exact_agreement:.6f}")
    print(f"Q selected-action Huber  {q_diagnostics.selected_action_huber_loss:.6f}")
    print(f"Q finite Q rate          {q_diagnostics.finite_q_rate:.6f}")
    return 0


def _smoke(arguments: argparse.Namespace) -> int:
    from . import q_training as _q_training
    from .serving import create_bc_hybrid_runtime, create_q_hybrid_runtime
    from .smoke import run_smoke, summarize_smoke

    q_checkpoint = _q_training.load_checkpoint(arguments.q_checkpoint)
    supported = q_checkpoint.supported_indices
    bc_runtime = create_bc_hybrid_runtime(
        arguments.bc_checkpoint, supported_indices=supported
    )
    q_runtime = create_q_hybrid_runtime(
        arguments.q_checkpoint, supported_indices=supported
    )

    document = {}
    for arm, runtime in (("bc", bc_runtime), ("q", q_runtime)):
        measurements = run_smoke(runtime, SERVING_SMOKE_SEEDS)
        summary = summarize_smoke(arm, measurements)
        document[arm] = {
            "summary": summary.to_document(),
            "games": [item.to_document() for item in measurements],
        }
        print(
            f"{arm}: activation_rate={summary.activation_rate:.4f} "
            f"scaffold_fallback_rate={summary.scaffold_fallback_rate:.4f} "
            f"support_fallback_rate={summary.support_fallback_rate:.4f}"
        )
    _write_json(Path(arguments.report), document)
    return 0


def _freeze(arguments: argparse.Namespace) -> int:
    from .retention import Stage4aRetentionError, freeze_candidates

    try:
        freeze, _ = freeze_candidates(
            bc_checkpoint_path=arguments.bc_checkpoint,
            q_checkpoint_path=arguments.q_checkpoint,
            backend=arguments.retention_backend,
            root=arguments.retention_root,
            key=arguments.retention_key,
        )
    except Stage4aRetentionError as error:
        print("ARTIFACT RETENTION BLOCKED")
        print(str(error))
        return 1
    print(f"bc_checkpoint_identity={freeze.bc_checkpoint_identity}")
    print(f"q_checkpoint_identity={freeze.q_checkpoint_identity}")
    print(f"retention_key={freeze.key}")
    return 0


def _screen(arguments: argparse.Namespace) -> int:
    from .retention import strict_readback
    from .strength import run_strength_screen

    retained = strict_readback(arguments.bundle)
    measurement = run_strength_screen(retained, arguments.artifact)
    document = {
        **measurement.to_document(),
        "summary": measurement.summary.to_document(),
    }
    _write_json(Path(arguments.result), document)
    print(f"outcome={measurement.outcome.value}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m lisjong_arena.learned_policy_offline_q",
        description="Offline Q vertical slice -- BC-vs-Offline-Q controlled comparison",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    generate = commands.add_parser("generate", help="generate the locked dataset")
    generate.add_argument("--dataset", required=True)
    generate.add_argument("--report", required=True)
    generate.set_defaults(handler=_generate)

    train_bc = commands.add_parser("train-bc", help="train Arm A (BC control)")
    train_bc.add_argument("--dataset", required=True)
    train_bc.add_argument("--checkpoint", required=True)
    train_bc.set_defaults(handler=_train_bc)

    train_q = commands.add_parser("train-q", help="train Arm B (support-restricted Q)")
    train_q.add_argument("--dataset", required=True)
    train_q.add_argument("--checkpoint", required=True)
    train_q.set_defaults(handler=_train_q)

    test = commands.add_parser("test", help="evaluate both frozen checkpoints once")
    test.add_argument("--dataset", required=True)
    test.add_argument("--bc-checkpoint", required=True)
    test.add_argument("--q-checkpoint", required=True)
    test.add_argument("--result", required=True)
    test.set_defaults(handler=_test)

    smoke = commands.add_parser("smoke", help="serving smoke for both hybrids")
    smoke.add_argument("--bc-checkpoint", required=True)
    smoke.add_argument("--q-checkpoint", required=True)
    smoke.add_argument("--report", required=True)
    smoke.set_defaults(handler=_smoke)

    freeze = commands.add_parser(
        "freeze", help="retain BC/Q checkpoints before screening"
    )
    freeze.add_argument("--bc-checkpoint", required=True)
    freeze.add_argument("--q-checkpoint", required=True)
    freeze.add_argument("--retention-backend", required=True)
    freeze.add_argument("--retention-root", required=True)
    freeze.add_argument("--retention-key", required=True)
    freeze.set_defaults(handler=_freeze)

    screen = commands.add_parser("screen", help="Q-vs-BC ABBB strength screen")
    screen.add_argument("--bundle", required=True)
    screen.add_argument("--artifact", required=True)
    screen.add_argument("--result", required=True)
    screen.set_defaults(handler=_screen)

    arguments = parser.parse_args(argv)
    return arguments.handler(arguments)


if __name__ == "__main__":
    sys.exit(main())
