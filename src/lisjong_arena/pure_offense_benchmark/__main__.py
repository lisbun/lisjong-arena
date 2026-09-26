"""Issue #389 pure-offense benchmarkのoperator CLI。

```text
python -m lisjong_arena.pure_offense_benchmark run \
    --focal REFERENCE [--focal-identity IDENTITY] \
    --ledger LIVE_LEDGER_JSON --allocation-identity SHA256 \
    --workers N --out ARM_DIR

python -m lisjong_arena.pure_offense_benchmark summarize \
    ARM_DIR [ARM_DIR ...] [--out SUMMARY_JSON]
```

``run``はclean committed Arena checkoutからだけ実行できる（provenanceを
実行前に確定できない場合はgameを1つも実行しない）。seedはSeed Registryの
live ledger snapshotにある#389所有のactive allocationからだけ解決する。
``summarize``は保存済みarmだけから再導出し、指定順をlineage順として
``(i, j), i < j``の全組について``arm_j - arm_i``をpaired比較する。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from lisjong_arena.policy_reference import resolve_policy_reference
from lisjong_arena.progress import ProgressReporter
from lisjong_arena.seed_registry import load_ledger
from lisjong_arena.single_round_artifact import collect_execution_provenance
from lisjong_arena.single_round_evaluation import ROTATION_COUNT

from .artifact import load_benchmark_arm, resolve_seed_allocation, save_benchmark_arm
from .execution import benchmark_plan, run_benchmark_arm
from .protocol import BENCHMARK_IDENTITY
from .summary import build_summary, format_summary, save_summary


def _run(arguments: argparse.Namespace) -> int:
    destination = Path(arguments.out)
    if destination.exists():
        raise FileExistsError(f"arm destination already exists: {destination}")
    if not destination.parent.is_dir():
        raise FileNotFoundError(f"arm parent directory does not exist: {destination}")
    focal = resolve_policy_reference(
        arguments.focal, explicit_identity=arguments.focal_identity
    )
    allocation = resolve_seed_allocation(
        load_ledger(arguments.ledger), arguments.allocation_identity
    )
    # 長いrunの後でprovenance不備に気付かないよう、実行前に確定させる。
    collect_execution_provenance()

    plan = benchmark_plan(focal, allocation.seeds)
    reporter = ProgressReporter(ROTATION_COUNT * len(plan.seeds), stream=sys.stderr)
    try:
        arm = run_benchmark_arm(
            plan, max_workers=arguments.workers, progress_callback=reporter
        )
    finally:
        reporter.close()
    save_benchmark_arm(
        arm, destination, focal_reference=arguments.focal, allocation=allocation
    )
    print(f"benchmark={BENCHMARK_IDENTITY}")
    print(f"focal_identity={focal.identity}")
    print(f"seed_blocks={len(plan.seeds)}")
    print(f"arm_written={destination}")
    return 0


def _summarize(arguments: argparse.Namespace) -> int:
    arms = [load_benchmark_arm(path) for path in arguments.arms]
    document = build_summary(arms)
    if arguments.out is not None:
        save_summary(document, arguments.out)
    print(format_summary(document))
    if arguments.out is not None:
        print(f"summary_written={arguments.out}")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m lisjong_arena.pure_offense_benchmark"
    )
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="run one focal Policy arm")
    run.add_argument("--focal", required=True, help="catalog alias or module:attr")
    run.add_argument("--focal-identity", default=None)
    run.add_argument("--ledger", required=True, type=Path)
    run.add_argument("--allocation-identity", required=True)
    run.add_argument("--workers", required=True, type=int)
    run.add_argument("--out", required=True, type=Path)
    run.set_defaults(handler=_run)

    summarize = commands.add_parser("summarize", help="summarize saved arms")
    summarize.add_argument("arms", nargs="+", type=Path)
    summarize.add_argument("--out", default=None, type=Path)
    summarize.set_defaults(handler=_summarize)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    return arguments.handler(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
