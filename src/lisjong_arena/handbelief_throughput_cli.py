"""Strict retained-S64 measurement and result readback for Arena #166."""

import argparse
import json

from lisjong_arena.stage3_optimization_saturation.experiment import (
    configure_torch_runtime,
    saturation_population_data,
    saturation_train_view,
)
from lisjong_arena.stage3_optimization_saturation.retained import (
    load_predecessor_result,
    load_retained,
)

from .handbelief_throughput import (
    ThroughputError,
    load_result,
    make_result,
    profile,
    provenance,
    save_result,
)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Measure one retained-S64 S2 CPU epoch without optimization."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run")
    run.add_argument("--corpus-root", required=True)
    run.add_argument("--predecessor-root", required=True)
    run.add_argument("--output", required=True)
    verify = commands.add_parser("verify")
    verify.add_argument("artifact")
    arguments = parser.parse_args(argv)
    if arguments.command == "verify":
        result = load_result(arguments.artifact)
    else:
        # Read the exact retained #150/#157 evidence before any measured training.
        retained = load_retained(arguments.corpus_root, arguments.predecessor_root)
        load_predecessor_result(arguments.predecessor_root, retained.predecessor_lock)
        data = saturation_train_view(
            saturation_population_data(retained.raw, retained.dataset)
        )
        if (
            len({sequence.key.game.game_seed for sequence in data.train_sequences}),
            len(
                {sequence.key.game.game_seed for sequence in data.validation_sequences}
            ),
        ) != (64, 16):
            raise ThroughputError("retained S64 split differs")
        configure_torch_runtime()
        source = provenance()
        workload, samples = profile(data)
        result = make_result(workload, samples, source)
        save_result(arguments.output, result)
        result = load_result(arguments.output)
    print(
        json.dumps(
            {
                "result_identity": result["result_identity"],
                "summary": result["summary"],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
