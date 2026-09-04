"""Offline Q CLI: `generate` first (Issue #140).

```text
python -m lisjong_arena.learned_policy_offline_q generate \
    --dataset DIR --report FILE
```

`generate`はlocked seed population (245..276) をteacher x4のfixed-seed
hanchanとして実行し、macro-transition datasetとTRAIN/VALIDATION support gate
reportを書き出す。TEST split rowはsupport gate reportに一切使わない。
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
)
from .protocol import DATASET_ORDERED_SEEDS, verify_contract_identity
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

    arguments = parser.parse_args(argv)
    return arguments.handler(arguments)


if __name__ == "__main__":
    sys.exit(main())
