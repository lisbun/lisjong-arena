"""Stage 4a CLI: `freeze` (Gate 0) -> `screen` (bounded strength screening)。

```text
python -m lisjong_arena.learned_policy_stage4a freeze \
    --retention-backend <operator-declared backend identity> \
    --retention-root    /non-ephemeral/root/outside/git \
    --retention-key     learned-stage4a/<run name>

python -m lisjong_arena.learned_policy_stage4a screen \
    --bundle /non-ephemeral/root/outside/git/learned-stage4a/<run name>
```

`freeze`はretention先を検証してからcandidateを生成する。宣言できる
non-ephemeral rootが無い場合はcandidateを作らず、`ARTIFACT RETENTION
BLOCKED`で停止する。

`screen`はretained bundleのstrict readbackを前提に、primaryとsecondaryを
無条件に実行する。primary resultを見てsecondaryをskipする入口を持たない。
生成物(checkpoint / artifact / result)はbundle配下へ置き、Gitへcommitしない。
"""

import argparse
import sys
from pathlib import Path

from .candidate import freeze_candidate, resolve_retention_target
from .errors import Stage4aRetentionError
from .evaluation import (
    ComparisonRole,
    artifact_filename,
    create_stage4a_candidate,
    run_comparison,
)
from .protocol import SCREENING_SEEDS, Stage4aOutcome
from .result import build_screening_result, format_result_report, write_result

RESULT_FILENAME = "stage4a-result.json"


def _freeze(arguments: argparse.Namespace) -> int:
    try:
        target = resolve_retention_target(
            backend=arguments.retention_backend,
            root=arguments.retention_root,
            key=arguments.retention_key,
        )
        freeze, checkpoint, report = freeze_candidate(target)
    except Stage4aRetentionError as error:
        print(f"retention error: {error}", file=sys.stderr)
        print(f"FINAL OUTCOME: {Stage4aOutcome.ARTIFACT_RETENTION_BLOCKED.value}")
        return 1

    print(f"candidate_identity={freeze.candidate_identity}")
    print(f"checkpoint_schema_version={freeze.checkpoint_schema_version}")
    print(f"checkpoint_identity={freeze.checkpoint_identity}")
    print(f"dataset_identity={freeze.dataset_identity}")
    print(f"weights_sha256={freeze.weights_sha256}")
    print(f"weights_bytes={freeze.weights_bytes}")
    print(f"selected_epoch={report['selected_epoch']}")
    print(f"row_count={report['row_count']}")
    print(f"retention_backend={freeze.retention_backend}")
    print(f"retention_key={freeze.retention_key}")
    print(f"artifact_bytes={checkpoint.artifact_bytes}")
    print("strict_readback=PASS")
    return 0


def _screen(arguments: argparse.Namespace) -> int:
    bundle = Path(arguments.bundle)
    candidate = create_stage4a_candidate(bundle)
    measurements = {
        role: run_comparison(candidate, role, bundle / artifact_filename(role))
        for role in (ComparisonRole.PRIMARY, ComparisonRole.SECONDARY)
    }
    checkpoint = candidate.runtime.checkpoint
    result = build_screening_result(
        candidate.freeze,
        measurements[ComparisonRole.PRIMARY],
        measurements[ComparisonRole.SECONDARY],
        candidate_load_cost={
            "artifact_bytes": checkpoint.artifact_bytes,
            "load_wall_clock_seconds": checkpoint.load_wall_clock_seconds,
            "load_cpu_seconds": checkpoint.load_cpu_seconds,
            "peak_process_ram_bytes_after_load": (
                candidate.runtime.peak_process_ram_bytes_after_load
            ),
        },
    )
    write_result(bundle / RESULT_FILENAME, result)
    print(f"ordered_seeds={SCREENING_SEEDS[0]}..{SCREENING_SEEDS[-1]}")
    print("\n".join(format_result_report(result)))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lisjong_arena.learned_policy_stage4a")
    commands = parser.add_subparsers(dest="command", required=True)

    freeze = commands.add_parser("freeze", help="run Gate 0 candidate freeze")
    freeze.add_argument("--retention-backend", required=True)
    freeze.add_argument("--retention-root", type=Path, required=True)
    freeze.add_argument("--retention-key", required=True)
    freeze.set_defaults(handler=_freeze)

    screen = commands.add_parser("screen", help="run the bounded strength screening")
    screen.add_argument("--bundle", type=Path, required=True)
    screen.set_defaults(handler=_screen)

    arguments = parser.parse_args(argv)
    return arguments.handler(arguments)


if __name__ == "__main__":
    sys.exit(main())
