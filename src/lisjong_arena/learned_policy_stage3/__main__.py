"""Stage 3 CLI: `fixture`（Path Bのみ）-> `smoke`。

```text
python -m lisjong_arena.learned_policy_stage3 fixture \
    --checkpoint /path/outside/git/stage3-fixture \
    --report     /path/outside/git/fixture.json

python -m lisjong_arena.learned_policy_stage3 smoke \
    --checkpoint /path/outside/git/stage3-fixture \
    --result     /path/outside/git/smoke.json
```

`fixture`はexact Stage 2 checkpointが失われている場合のPath B専用であり、
Stage 2 TEST hanchanへは到達しない。`smoke`はcheckpointのartifact classを
問わず、explicit pathのartifactだけをstrict loadして実行する。

生成checkpointとresult artifactはrepository外へ出力し、Gitへcommitしない。
"""

import argparse
import sys
from pathlib import Path

from .fixture import build_fixture_checkpoint, write_report
from .policy import create_serving_runtime
from .protocol import SERVING_SEEDS, Stage3Outcome
from .smoke import run_serving_smoke, write_result


def _fixture(arguments: argparse.Namespace) -> int:
    checkpoint, report = build_fixture_checkpoint(arguments.checkpoint)
    write_report(arguments.report, report)
    print(f"artifact_class={checkpoint.artifact_class.value}")
    print(f"checkpoint_identity={checkpoint.identity}")
    print(f"weights_sha256={checkpoint.weights_sha256}")
    print(f"dataset_identity={checkpoint.dataset_identity}")
    print(f"row_count={report['row_count']}")
    return 0


def _smoke(arguments: argparse.Namespace) -> int:
    runtime = create_serving_runtime(arguments.checkpoint)
    result = run_serving_smoke(runtime)
    write_result(arguments.result, result)
    print(f"artifact_class={result.identity['artifact_class']}")
    print(f"checkpoint_identity={result.identity['checkpoint_identity']}")
    print(f"ordered_seeds={list(SERVING_SEEDS)}")
    print(f"deterministic_repeat={result.deterministic}")
    print(f"FINAL OUTCOME: {result.outcome.value}")
    return 0 if result.outcome is Stage3Outcome.SERVING_CANDIDATE_READY else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lisjong_arena.learned_policy_stage3")
    commands = parser.add_subparsers(dest="command", required=True)

    fixture = commands.add_parser("fixture", help="build the Path B serving fixture")
    fixture.add_argument("--checkpoint", type=Path, required=True)
    fixture.add_argument("--report", type=Path, required=True)
    fixture.set_defaults(handler=_fixture)

    smoke = commands.add_parser("smoke", help="run the actual serving smoke")
    smoke.add_argument("--checkpoint", type=Path, required=True)
    smoke.add_argument("--result", type=Path, required=True)
    smoke.set_defaults(handler=_smoke)

    arguments = parser.parse_args(argv)
    return arguments.handler(arguments)


if __name__ == "__main__":
    sys.exit(main())
