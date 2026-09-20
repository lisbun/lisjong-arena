"""One-shot operator CLI for the locked #262 scientific execution."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from lisjong_arena.learned_policy_offline_q.protocol import Split
from lisjong_arena.stage_a0_tenpai_protocol_lock import protocol as locked

from .data import build_scientific_split_tensors, load_scientific_data
from .downstream import evaluate_downstream
from .gate import evaluate_gate, save_gate
from .preflight import run_preflight, save_preflight
from .protocol import GATE_PASS
from .result import build_invalid_result, build_result, save_result
from .training import load_checkpoint, save_checkpoint, train_seed_pair


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Execute lisbun/lisjong-arena#262 exactly once."
    )
    parser.add_argument("command", choices=("execute",))
    parser.add_argument("--lock-b", required=True)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--sidecar", required=True)
    parser.add_argument("--public-keys", required=True)
    parser.add_argument("--out-root", required=True)
    parser.add_argument("--workers", type=int, default=1)
    return parser


def _progress(label: str):
    def callback(completed: int, total: int) -> None:
        if completed == total or completed == 1 or completed % 50 == 0:
            print(f"{label}: {completed}/{total}", flush=True)

    return callback


def execute(arguments: argparse.Namespace) -> int:
    root = Path(arguments.out_root)
    if root.exists():
        raise FileExistsError(
            "scientific output root already exists; "
            "#262 does not permit an in-place rerun"
        )
    root.mkdir(parents=True)

    stage = "E0 preflight"
    preflight = None
    try:
        preflight = run_preflight(
            lock_b_path=arguments.lock_b,
            dataset_path=arguments.dataset,
            sidecar_path=arguments.sidecar,
            public_keys_path=arguments.public_keys,
        )
        save_preflight(preflight, root / "preflight.json")
        print("PREFLIGHT PASS", flush=True)

        scientific = load_scientific_data(
            lock_b_path=arguments.lock_b,
            dataset_path=arguments.dataset,
            sidecar_path=arguments.sidecar,
            public_keys_path=arguments.public_keys,
        )
        tensors = build_scientific_split_tensors(scientific)
        print(
            "scientific rows: "
            f"TRAIN={tensors[Split.TRAIN].row_count} "
            f"VALIDATION={tensors[Split.VALIDATION].row_count}",
            flush=True,
        )

        stage = "E1 A/T training"
        checkpoints = []
        checkpoint_root = root / "checkpoints"
        checkpoint_root.mkdir()
        for seed in locked.TRAINING_SEEDS:
            a_result, t_result = train_seed_pair(seed, tensors)
            for training_result in (a_result, t_result):
                destination = (
                    checkpoint_root
                    / f"{training_result.arm.value}_seed{training_result.seed}"
                )
                checkpoint = save_checkpoint(
                    destination,
                    scientific,
                    training_result,
                )
                checkpoints.append(checkpoint)
                print(
                    f"checkpoint {training_result.arm.value}_seed{seed}: "
                    f"{checkpoint.identity}",
                    flush=True,
                )

        # Freeze all six identities before the first Gate A0.5 inference.
        frozen = tuple(
            load_checkpoint(checkpoint.path) for checkpoint in checkpoints
        )
        t_checkpoints = tuple(
            checkpoint for checkpoint in frozen if checkpoint.arm.value == "T"
        )

        stage = "E2 Gate A0.5"
        gate = evaluate_gate(
            scientific=scientific,
            tensors=tensors,
            t_checkpoints=t_checkpoints,
        )
        save_gate(gate, root / "gate-a0.5.json")
        gate_label = gate["classification"]["label"]
        print(gate_label, flush=True)

        downstream = None
        if gate_label == GATE_PASS:
            stage = "E3 downstream evaluation"
            indexed = {
                (checkpoint.arm.value, checkpoint.seed): checkpoint
                for checkpoint in frozen
            }
            downstream = evaluate_downstream(
                gate_path=root / "gate-a0.5.json",
                a_checkpoint_path=indexed[
                    ("A", locked.INTERACTIVE_ANCHOR_SEED)
                ].path,
                t_checkpoint_path=indexed[
                    ("T", locked.INTERACTIVE_ANCHOR_SEED)
                ].path,
                output_root=root / "downstream",
                workers=arguments.workers,
                progress_callback=_progress("downstream"),
            )
            print(
                "downstream: "
                f"{downstream['paired']['classification']} "
                f"delta={downstream['paired']['mean_delta']:.3f}",
                flush=True,
            )

        stage = "completion artifact"
        completion = build_result(
            preflight=preflight,
            checkpoints=frozen,
            gate=gate,
            downstream=downstream,
        )
        save_result(completion, root / "result.json")
        print(
            json.dumps(
                {
                    "result_identity": completion["result_identity"],
                    "primary_outcome": completion["primary_outcome"],
                    "protected_test_evaluated": completion[
                        "protected_test_evaluated"
                    ],
                },
                ensure_ascii=False,
                indent=2,
            ),
            flush=True,
        )
    except Exception as error:
        invalid = build_invalid_result(
            stage=stage,
            error=error,
            preflight=preflight,
        )
        invalid_path = root / "result.json"
        if invalid_path.exists():
            raise
        save_result(invalid, invalid_path)
        print(
            json.dumps(
                {
                    "result_identity": invalid["result_identity"],
                    "primary_outcome": invalid["primary_outcome"],
                    "failure": invalid["failure"],
                    "protected_test_evaluated": invalid[
                        "protected_test_evaluated"
                    ],
                },
                ensure_ascii=False,
                indent=2,
            ),
            flush=True,
        )
        return 2

    return 0


def main() -> int:
    arguments = _parser().parse_args()
    if arguments.command == "execute":
        return execute(arguments)
    raise AssertionError("unreachable command")


if __name__ == "__main__":
    raise SystemExit(main())
