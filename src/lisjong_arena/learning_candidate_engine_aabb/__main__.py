"""CLI: ``python -m lisjong_arena.learning_candidate_engine_aabb {lock,run,verify}``、
開発準備入口（#567）は ``{dev-plan,dev-run,dev-verify}``。

``lock`` は3 artifactを実際にloadしてbindingを作るため、torchと同じrevisionの
native wheelが入った環境で、Arm Yのcheckpointを
``LISJONG_ARENA_INITIAL_LEARNING_CHAMPION_CHECKPOINT`` で指定して実行する。
"""

import argparse
import json
import sys
from pathlib import Path

from lisjong_arena import seed_registry

from .lock import build_lock, load_lock, save_lock
from .result import verify_result
from .runner import run_formal_event


def _seeds(text: str) -> list[int]:
    return [int(item) for item in text.split(",") if item.strip()]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m " + __package__)
    sub = parser.add_subparsers(dest="command", required=True)

    lock = sub.add_parser("lock", help="write the pre-execution lock")
    lock.add_argument("--event", type=int, required=True, help="owner Issue number")
    lock.add_argument("--seeds", required=True, help="100 seeds, comma separated")
    lock.add_argument("--allocation-identity", required=True)
    lock.add_argument("--timeout-seconds", type=int, required=True)
    lock.add_argument("--wall-seconds", type=int, required=True)
    lock.add_argument("--peak-rss-kib", type=int, required=True)
    lock.add_argument("--wheel", required=True)
    lock.add_argument("--discard-artifact", required=True)
    lock.add_argument("--reaction-artifact", required=True)
    lock.add_argument("--champion-checkpoint", required=True)
    lock.add_argument("--raw", required=True)
    lock.add_argument("--result", required=True)
    lock.add_argument("--observations", required=True)
    lock.add_argument("--diagnostics", required=True)
    lock.add_argument("--ledger", default=str(seed_registry.DEFAULT_LEDGER_PATH))
    lock.add_argument("--output", required=True)

    run = sub.add_parser("run", help="run a locked formal event")
    run.add_argument("--lock", required=True)
    run.add_argument("--ledger", default=str(seed_registry.DEFAULT_LEDGER_PATH))

    verify = sub.add_parser("verify", help="re-derive a formal result offline")
    verify.add_argument("--lock", required=True)

    # 開発準備入口（#567）。seed・配置・件数は定数で、引数では変えられない
    dev_plan = sub.add_parser(
        "dev-plan", help="write the plan of the fixed 18-hanchan preparation check"
    )
    dev_plan.add_argument("--timeout-seconds", type=int, required=True)
    dev_plan.add_argument("--wall-seconds", type=int, required=True)
    dev_plan.add_argument("--peak-rss-kib", type=int, required=True)
    dev_plan.add_argument("--wheel", required=True)
    dev_plan.add_argument("--discard-artifact", required=True)
    dev_plan.add_argument("--reaction-artifact", required=True)
    dev_plan.add_argument("--champion-checkpoint", required=True)
    dev_plan.add_argument("--output-dir", required=True)
    dev_plan.add_argument("--diagnostics", required=True)
    dev_plan.add_argument("--ledger", default=str(seed_registry.DEFAULT_LEDGER_PATH))
    dev_plan.add_argument("--plan", required=True)

    dev_run = sub.add_parser("dev-run", help="run the planned preparation check")
    dev_run.add_argument("--plan", required=True)
    dev_run.add_argument("--ledger", default=str(seed_registry.DEFAULT_LEDGER_PATH))

    dev_verify = sub.add_parser(
        "dev-verify", help="re-derive a preparation report from the stored files"
    )
    dev_verify.add_argument("--output-dir", required=True)
    return parser


def _development(args) -> int:
    from . import development

    if args.command == "dev-plan":
        document = development.build_plan(
            limits={
                "peak_rss_kib": args.peak_rss_kib,
                "timeout_seconds": args.timeout_seconds,
                "wall_seconds": args.wall_seconds,
            },
            wheel_path=str(Path(args.wheel).resolve()),
            artifact_paths={
                "champion": str(Path(args.champion_checkpoint).resolve()),
                "discard": str(Path(args.discard_artifact).resolve()),
                "reaction": str(Path(args.reaction_artifact).resolve()),
            },
            output_dir=str(Path(args.output_dir).resolve()),
            diagnostics_dir=str(Path(args.diagnostics).resolve()),
            ledger_path=args.ledger,
        )
        development.save_plan(document, args.plan)
        print(json.dumps({"plan_digest": document["plan_digest"]}))
        return 0
    if args.command == "dev-run":
        report = development.run_development(
            args.plan,
            ledger_path=args.ledger,
            on_hanchan=lambda done, total: print(
                f"{done}/{total}", file=sys.stderr, flush=True
            ),
        )
    else:
        report = development.verify_development(args.output_dir)
    status = report["reproduction"]["status"]
    print(
        json.dumps({"report_digest": report["report_digest"], "reproduction": status})
    )
    # 再現比較の不一致はSTOPではないが、成功の終了codeにはしない
    return 0 if status == development.MATCH else 3


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command.startswith("dev-"):
        return _development(args)
    if args.command == "lock":
        document = build_lock(
            event=args.event,
            ordered_seeds=_seeds(args.seeds),
            allocation_identity=args.allocation_identity,
            limits={
                "peak_rss_kib": args.peak_rss_kib,
                "timeout_seconds": args.timeout_seconds,
                "wall_seconds": args.wall_seconds,
            },
            wheel_path=str(Path(args.wheel).resolve()),
            artifact_paths={
                "champion": str(Path(args.champion_checkpoint).resolve()),
                "discard": str(Path(args.discard_artifact).resolve()),
                "reaction": str(Path(args.reaction_artifact).resolve()),
            },
            destinations={
                "diagnostics": str(Path(args.diagnostics).resolve()),
                "observations": str(Path(args.observations).resolve()),
                "raw": str(Path(args.raw).resolve()),
                "result": str(Path(args.result).resolve()),
            },
            ledger_path=args.ledger,
        )
        save_lock(document, args.output)
        print(json.dumps({"lock_digest": document["lock_digest"]}))
        return 0
    if args.command == "run":
        result = run_formal_event(
            args.lock,
            ledger_path=args.ledger,
            on_hanchan=lambda done, total: print(
                f"{done}/{total}", file=sys.stderr, flush=True
            ),
        )
    else:
        lock = load_lock(args.lock)
        destinations = lock["artifact_destinations"]
        result = verify_result(
            lock_path=args.lock,
            raw_path=destinations["raw"],
            result_path=destinations["result"],
            observations_path=destinations["observations"],
        )
    print(
        json.dumps(
            {
                "classification": result["statistic"]["classification"],
                "result_digest": result["result_digest"],
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
