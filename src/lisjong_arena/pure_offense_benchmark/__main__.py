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
``--focal canonical-first`` / ``--focal outcome-q --focal-artifact DIR``は
lisjong residual runtimeのfocalであり、``focal``moduleが解決する。
``--shanten-backend {python,rust} --shanten-backend-record PATH``（#406、opt-in）は
parentと全game実行processで``LISJONG_SHANTEN_BACKEND``を検査し（#400の
``require_shanten_backend``）、per-gameの検査結果を集約した記録をarmの外の
``PATH``へ書く。未指定時は従来どおりで、backend既定値（python）も変えない。
``summarize``は保存済みarmだけから再導出し、指定順をlineage順として
``(i, j), i < j``の全組について``arm_j - arm_i``をpaired比較する。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from lisjong_arena.policy_reference import resolve_policy_reference
from lisjong_arena.progress import ProgressReporter
from lisjong_arena.seed_registry import load_ledger
from lisjong_arena.shanten_backend_verification.backend import (
    BACKENDS,
    require_shanten_backend,
)
from lisjong_arena.single_round_artifact import collect_execution_provenance
from lisjong_arena.single_round_evaluation import ROTATION_COUNT

from .artifact import load_benchmark_arm, resolve_seed_allocation, save_benchmark_arm
from .execution import benchmark_plan, run_benchmark_arm, shanten_backend_record
from .focal import RUNTIME_FOCALS, resolve_runtime_focal
from .protocol import BENCHMARK_IDENTITY
from .summary import build_summary, format_summary, save_summary


def _run(arguments: argparse.Namespace) -> int:
    destination = Path(arguments.out)
    if destination.exists():
        raise FileExistsError(f"arm destination already exists: {destination}")
    if not destination.parent.is_dir():
        raise FileNotFoundError(f"arm parent directory does not exist: {destination}")
    backend = arguments.shanten_backend
    backend_record_path = arguments.shanten_backend_record
    parent_backend = None
    if (backend is None) != (backend_record_path is None):
        raise ValueError(
            "--shanten-backend and --shanten-backend-record must be given together"
        )
    if backend_record_path is not None:
        if backend_record_path.exists():
            raise FileExistsError(
                f"backend record already exists: {backend_record_path}"
            )
        if not backend_record_path.parent.is_dir():
            raise FileNotFoundError(
                f"backend record parent directory does not exist: {backend_record_path}"
            )
        parent_backend = require_shanten_backend(backend)
    if arguments.focal in RUNTIME_FOCALS:
        if arguments.focal_identity is not None:
            raise ValueError(
                f"--focal {arguments.focal} does not take --focal-identity"
            )
        focal, focal_reference = resolve_runtime_focal(
            arguments.focal, arguments.focal_artifact
        )
    else:
        if arguments.focal_artifact is not None:
            raise ValueError("--focal-artifact is only valid with --focal outcome-q")
        focal = resolve_policy_reference(
            arguments.focal, explicit_identity=arguments.focal_identity
        )
        focal_reference = arguments.focal
    allocation = resolve_seed_allocation(
        load_ledger(arguments.ledger), arguments.allocation_identity
    )
    # 長いrunの後でprovenance不備に気付かないよう、実行前に確定させる。
    collect_execution_provenance()

    plan = benchmark_plan(focal, allocation.seeds)
    reporter = ProgressReporter(ROTATION_COUNT * len(plan.seeds), stream=sys.stderr)
    try:
        arm = run_benchmark_arm(
            plan,
            max_workers=arguments.workers,
            progress_callback=reporter,
            shanten_backend=backend,
        )
    finally:
        reporter.close()
    backend_record = None
    if parent_backend is not None:
        # arm保存前に全gameのbackend検査を集約し、不整合ならarmを書かない。
        backend_record = {
            "focal_identity": focal.identity,
            "workers_requested": arguments.workers,
            **shanten_backend_record(arm, backend=backend, parent=parent_backend),
        }
    save_benchmark_arm(
        arm, destination, focal_reference=focal_reference, allocation=allocation
    )
    print(f"benchmark={BENCHMARK_IDENTITY}")
    print(f"focal_identity={focal.identity}")
    print(f"focal_reference={focal_reference}")
    print(f"seed_blocks={len(plan.seeds)}")
    print(f"arm_written={destination}")
    if backend_record is not None:
        backend_record_path.write_text(
            json.dumps(backend_record, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(f"shanten_backend={backend}")
        print(f"shanten_backend_workers={backend_record['workers_observed']}")
        print(f"shanten_backend_record_written={backend_record_path}")
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
    run.add_argument("--focal-artifact", default=None, type=Path)
    run.add_argument("--ledger", required=True, type=Path)
    run.add_argument("--allocation-identity", required=True)
    run.add_argument("--workers", required=True, type=int)
    run.add_argument("--out", required=True, type=Path)
    run.add_argument(
        "--shanten-backend",
        default=None,
        choices=BACKENDS,
        help="verify this LISJONG_SHANTEN_BACKEND in every game process (#406)",
    )
    run.add_argument("--shanten-backend-record", default=None, type=Path)
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
