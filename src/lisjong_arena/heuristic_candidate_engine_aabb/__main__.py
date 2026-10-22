"""engine版AABB評価の限定した実行入口（#514）。

```text
python -m lisjong_arena.heuristic_candidate_engine_aabb lock ...     正式lockを書く
python -m lisjong_arena.heuristic_candidate_engine_aabb run --lock L 正式eventを実行する
python -m lisjong_arena.heuristic_candidate_engine_aabb verify --lock L
python -m lisjong_arena.heuristic_candidate_engine_aabb dev-check --output D --wheel W --timeout-seconds T
python -m lisjong_arena.heuristic_candidate_engine_aabb dev-verify --output D

校正（#517。強さの結果ではない）
python -m lisjong_arena.heuristic_candidate_engine_aabb backend-check --backend B --output F ...
python -m lisjong_arena.heuristic_candidate_engine_aabb backend-compare --python F1 --rust F2 --output F3
python -m lisjong_arena.heuristic_candidate_engine_aabb calibration-plan ...
python -m lisjong_arena.heuristic_candidate_engine_aabb calibration-run --plan P
python -m lisjong_arena.heuristic_candidate_engine_aabb calibration-verify --plan P
python -m lisjong_arena.heuristic_candidate_engine_aabb calibration-report --plan P

#520 同点数牌paijia候補の開発比較（DEVELOPMENT。正式結果ではない）
python -m lisjong_arena.heuristic_candidate_engine_aabb paijia-plan --stage S --run-kind R ...
python -m lisjong_arena.heuristic_candidate_engine_aabb paijia-activation-run --plan P
python -m lisjong_arena.heuristic_candidate_engine_aabb paijia-activation-verify --plan P
python -m lisjong_arena.heuristic_candidate_engine_aabb paijia-screen-run --plan P
python -m lisjong_arena.heuristic_candidate_engine_aabb paijia-screen-verify --plan P
python -m lisjong_arena.heuristic_candidate_engine_aabb paijia-screen-report --plan P
```

Kとtimeoutにdefaultはない。正式eventはSeed Registryのallocation、Kの根拠、
校正後に決めたtimeoutがそろうまで始めない（J1 / J2）。
"""

import argparse
import json
import sys
from pathlib import Path

from lisjong_arena import seed_registry
from lisjong_arena._artifact_io import canonical_json_text, write_new_artifact_file

from .backend_check import MATCH, compare_backend_checks, run_backend_check
from .calibration import (
    build_plan,
    run_calibration,
    save_plan,
    verify_calibration,
    write_report,
)
from .development import (
    catalog_participant,
    run_development_check,
    verify_development_check,
)
from .lock import build_lock, load_lock, save_lock
from .paijia_tie_activation import (
    run_activation,
    screen_prerequisite,
    verify_activation,
)
from .paijia_tie_plan import (
    ACTIVATION,
    ALLOCATED,
    REHEARSAL,
    REHEARSAL_SEEDS,
    RUN_KINDS,
    STAGES,
    EngineAabbPaijiaTieError,
)
from .paijia_tie_plan import build_plan as build_paijia_plan
from .paijia_tie_plan import save_plan as save_paijia_plan
from .paijia_tie_screen import run_screen, verify_screen
from .paijia_tie_screen import write_report as write_screen_report
from .result import verify_result
from .runner import run_formal_event


def _seeds(text: str) -> list[int]:
    return [int(item) for item in text.split(",") if item.strip()]


def _progress(done: int, total: int) -> None:
    print(f"hanchan {done}/{total}", file=sys.stderr, flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="lisjong_arena.heuristic_candidate_engine_aabb"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    lock = sub.add_parser("lock", help="write the pre-execution lock")
    lock.add_argument("--event", type=int, required=True)
    lock.add_argument("--candidate", required=True, help="policy_catalog identity")
    lock.add_argument("--incumbent", required=True, help="policy_catalog identity")
    lock.add_argument("--incumbent-role", required=True)
    lock.add_argument("--lisjong-revision", required=True)
    lock.add_argument("--seeds", required=True, help="comma separated, in order")
    lock.add_argument("--k", type=int, required=True)
    lock.add_argument("--allocation-identity", required=True)
    lock.add_argument("--k-rationale", required=True, help="JSON file")
    lock.add_argument("--timeout-seconds", type=int, required=True)
    lock.add_argument("--wheel", required=True)
    lock.add_argument("--raw", required=True)
    lock.add_argument("--result", required=True)
    lock.add_argument("--diagnostics", required=True)
    lock.add_argument("--ledger", default=str(seed_registry.DEFAULT_LEDGER_PATH))
    lock.add_argument("--output", required=True)

    run = sub.add_parser("run", help="run a locked formal event")
    run.add_argument("--lock", required=True)
    run.add_argument("--ledger", default=str(seed_registry.DEFAULT_LEDGER_PATH))

    verify = sub.add_parser("verify", help="re-derive a formal result offline")
    verify.add_argument("--lock", required=True)

    dev = sub.add_parser("dev-check", help="§10.7 record path check (development)")
    dev.add_argument("--output", required=True)
    dev.add_argument("--wheel", required=True)
    dev.add_argument("--timeout-seconds", type=int, required=True)

    dev_verify = sub.add_parser("dev-verify", help="re-derive a development summary")
    dev_verify.add_argument("--output", required=True)

    check = sub.add_parser("backend-check", help="P2: run the 12 dev hanchans once")
    check.add_argument("--backend", choices=("python", "rust"), required=True)
    check.add_argument("--output", required=True)
    check.add_argument("--wheel", default="", help="required for rust")
    check.add_argument("--timeout-seconds", type=int, required=True)

    compare = sub.add_parser("backend-compare", help="P2: compare python and rust")
    compare.add_argument("--python", required=True)
    compare.add_argument("--rust", required=True)
    compare.add_argument("--output", required=True)

    plan = sub.add_parser("calibration-plan", help="write the calibration plan")
    plan.add_argument("--seeds", required=True, help="comma separated, in order")
    plan.add_argument("--allocation-identity", required=True)
    plan.add_argument("--timeout-seconds", type=int, required=True)
    plan.add_argument("--wall-limit-seconds", type=int, required=True)
    plan.add_argument("--rss-limit-bytes", type=int, required=True)
    plan.add_argument("--limit-enforcement", required=True)
    plan.add_argument("--wheel", required=True)
    plan.add_argument("--calibration-output", required=True)
    plan.add_argument("--diagnostics", required=True)
    plan.add_argument("--ledger", default=str(seed_registry.DEFAULT_LEDGER_PATH))
    plan.add_argument("--output", required=True)

    for name in ("calibration-run", "calibration-verify", "calibration-report"):
        command = sub.add_parser(name)
        command.add_argument("--plan", required=True)
        if name == "calibration-run":
            command.add_argument(
                "--ledger", default=str(seed_registry.DEFAULT_LEDGER_PATH)
            )

    paijia = sub.add_parser("paijia-plan", help="#520: write a write-once plan")
    paijia.add_argument("--stage", choices=STAGES, required=True)
    paijia.add_argument("--run-kind", choices=RUN_KINDS, required=True)
    paijia.add_argument("--seeds", help="allocated only: comma separated, in order")
    paijia.add_argument("--allocation-identity", help="allocated only")
    paijia.add_argument("--timeout-seconds", type=int, required=True)
    paijia.add_argument("--wall-limit-seconds", type=int, required=True)
    paijia.add_argument("--rss-limit-bytes", type=int, required=True)
    paijia.add_argument("--limit-enforcement", required=True)
    paijia.add_argument("--screen-wall-limit-seconds", type=int, help="activation")
    paijia.add_argument("--screen-rss-limit-bytes", type=int, help="activation")
    paijia.add_argument("--screen-limit-enforcement", help="activation")
    paijia.add_argument("--activation-plan", help="allocated screen only")
    paijia.add_argument("--wheel", required=True)
    paijia.add_argument("--stage-output", required=True)
    paijia.add_argument("--diagnostics", required=True)
    paijia.add_argument("--ledger", default=str(seed_registry.DEFAULT_LEDGER_PATH))
    paijia.add_argument("--output", required=True)

    for name in ("paijia-activation-run", "paijia-screen-run"):
        command = sub.add_parser(name)
        command.add_argument("--plan", required=True)
        command.add_argument("--ledger", default=str(seed_registry.DEFAULT_LEDGER_PATH))
    for name in (
        "paijia-activation-verify",
        "paijia-screen-verify",
        "paijia-screen-report",
    ):
        sub.add_parser(name).add_argument("--plan", required=True)

    args = parser.parse_args(argv)
    if args.command.startswith("paijia-"):
        return _paijia(args)
    if args.command == "backend-check":
        document = run_backend_check(
            args.output,
            backend=args.backend,
            wheel_path=str(Path(args.wheel).resolve()) if args.wheel else "",
            timeout_seconds=args.timeout_seconds,
        )
        print(f"BACKEND CHECK {args.backend} {document['document_digest']}")
        return 0
    if args.command == "backend-compare":
        comparison = compare_backend_checks(args.python, args.rust)
        write_new_artifact_file(Path(args.output), canonical_json_text(comparison))
        print(f"P2 {comparison['status']} {comparison['comparison_digest']}")
        return 0 if comparison["status"] == MATCH else 3
    if args.command == "calibration-plan":
        document = build_plan(
            ordered_seeds=_seeds(args.seeds),
            allocation_identity=args.allocation_identity,
            timeout_seconds=args.timeout_seconds,
            resource_limits={
                "enforcement": args.limit_enforcement,
                "process_tree_rss_bytes": args.rss_limit_bytes,
                "wall_seconds_per_stage": args.wall_limit_seconds,
            },
            wheel_path=str(Path(args.wheel).resolve()),
            destinations={
                "diagnostics": args.diagnostics,
                "output": args.calibration_output,
            },
            ledger_path=args.ledger,
        )
        save_plan(document, args.output)
        print(document["plan_digest"])
        return 0
    if args.command == "calibration-run":
        summary = run_calibration(
            args.plan, ledger_path=args.ledger, on_hanchan=_progress
        )
        print(f"{summary['status']} {summary['summary_digest']}")
        return 0
    if args.command == "calibration-verify":
        verified = verify_calibration(args.plan)
        print(json.dumps(verified, sort_keys=True, indent=2))
        return 0
    if args.command == "calibration-report":
        report = write_report(args.plan)
        print(f"REPORT {report['report_digest']} (not a strength result)")
        return 0
    if args.command == "lock":
        document = build_lock(
            event=args.event,
            candidate=catalog_participant(args.candidate, args.lisjong_revision),
            incumbent=catalog_participant(args.incumbent, args.lisjong_revision),
            incumbent_role=args.incumbent_role,
            ordered_seeds=_seeds(args.seeds),
            k=args.k,
            allocation_identity=args.allocation_identity,
            k_rationale=json.loads(Path(args.k_rationale).read_text(encoding="utf-8")),
            timeout_seconds=args.timeout_seconds,
            wheel_path=str(Path(args.wheel).resolve()),
            destinations={
                "diagnostics": args.diagnostics,
                "raw": args.raw,
                "result": args.result,
            },
            ledger_path=args.ledger,
        )
        save_lock(document, args.output)
        print(document["lock_digest"])
    elif args.command == "run":
        result = run_formal_event(
            args.lock, ledger_path=args.ledger, on_hanchan=_progress
        )
        print(json.dumps(result["statistic"], sort_keys=True))
    elif args.command == "verify":
        lock_document = load_lock(args.lock)
        destinations = lock_document["artifact_destinations"]
        result = verify_result(
            lock_path=args.lock,
            raw_path=destinations["raw"],
            result_path=destinations["result"],
        )
        print(
            f"VERIFIED {result['result_digest']} {result['statistic']['classification']}"
        )
    elif args.command == "dev-check":
        summary = run_development_check(
            args.output,
            wheel_path=str(Path(args.wheel).resolve()),
            timeout_seconds=args.timeout_seconds,
            on_hanchan=_progress,
        )
        print(f"DEVELOPMENT {summary['summary_digest']} (not a formal result)")
    else:
        summary = verify_development_check(args.output)
        print(f"VERIFIED DEVELOPMENT {summary['summary_digest']} (not a formal result)")
    return 0


def _paijia_plan(args) -> dict:
    limits = {
        "enforcement": args.limit_enforcement,
        "process_tree_rss_bytes": args.rss_limit_bytes,
        "wall_seconds": args.wall_limit_seconds,
    }
    allocated = args.run_kind == ALLOCATED
    if allocated != (args.seeds is not None) or allocated != (
        args.allocation_identity is not None
    ):
        raise EngineAabbPaijiaTieError(
            "--seeds and --allocation-identity are required for allocated plans and "
            f"not accepted for a rehearsal (fixed seeds {REHEARSAL_SEEDS})"
        )
    screen_arguments = (
        args.screen_wall_limit_seconds,
        args.screen_rss_limit_bytes,
        args.screen_limit_enforcement,
    )
    screen_limits = None
    if args.stage == ACTIVATION:
        if None in screen_arguments:
            raise EngineAabbPaijiaTieError(
                "an activation plan fixes the screen limits for its budget gate"
            )
        screen_limits = {
            "enforcement": args.screen_limit_enforcement,
            "process_tree_rss_bytes": args.screen_rss_limit_bytes,
            "wall_seconds": args.screen_wall_limit_seconds,
        }
    elif any(value is not None for value in screen_arguments):
        raise EngineAabbPaijiaTieError("--screen-* belong to the activation plan")
    needs_activation = args.stage != ACTIVATION and allocated
    if needs_activation != (args.activation_plan is not None):
        raise EngineAabbPaijiaTieError(
            "--activation-plan is required exactly for an allocated screen plan"
        )
    return build_paijia_plan(
        stage=args.stage,
        run_kind=args.run_kind,
        ordered_seeds=_seeds(args.seeds) if allocated else REHEARSAL_SEEDS,
        allocation_identity=args.allocation_identity,
        timeout_seconds=args.timeout_seconds,
        resource_limits=limits,
        screen_limits=screen_limits,
        prerequisite=screen_prerequisite(args.activation_plan)
        if needs_activation
        else None,
        wheel_path=str(Path(args.wheel).resolve()),
        destinations={
            "diagnostics": args.diagnostics,
            "output": args.stage_output,
        },
        ledger_path=args.ledger,
    )


def _paijia(args) -> int:
    if args.command == "paijia-plan":
        document = _paijia_plan(args)
        save_paijia_plan(document, args.output)
        print(f"{args.stage} {args.run_kind} {document['plan_digest']}")
        return 0
    if args.command == "paijia-activation-run":
        summary = run_activation(
            args.plan, ledger_path=args.ledger, on_hanchan=_progress
        )
        decision = summary["gate"]["decision"] if summary["gate"] else REHEARSAL
        print(f"{summary['status']} {decision} {summary['summary_digest']}")
        return 0
    if args.command == "paijia-screen-run":
        summary = run_screen(args.plan, ledger_path=args.ledger, on_hanchan=_progress)
        progression = summary["progression"]
        decision = progression["decision"] if progression else REHEARSAL
        print(f"{summary['status']} {decision} {summary['summary_digest']}")
        return 0
    if args.command == "paijia-screen-report":
        report = write_screen_report(args.plan)
        print(f"REPORT {report['report_digest']} (development, not a formal result)")
        return 0
    verify = (
        verify_activation
        if args.command == "paijia-activation-verify"
        else verify_screen
    )
    print(json.dumps(verify(args.plan), sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
