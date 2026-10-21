"""engine版AABB評価の限定した実行入口（#514）。

```text
python -m lisjong_arena.heuristic_candidate_engine_aabb lock ...     正式lockを書く
python -m lisjong_arena.heuristic_candidate_engine_aabb run --lock L 正式eventを実行する
python -m lisjong_arena.heuristic_candidate_engine_aabb verify --lock L
python -m lisjong_arena.heuristic_candidate_engine_aabb dev-check --output D --wheel W --timeout-seconds T
python -m lisjong_arena.heuristic_candidate_engine_aabb dev-verify --output D
```

Kとtimeoutにdefaultはない。正式eventはSeed Registryのallocation、Kの根拠、
校正後に決めたtimeoutがそろうまで始めない（J1 / J2）。
"""

import argparse
import json
import sys
from pathlib import Path

from lisjong_arena import seed_registry

from .development import (
    catalog_participant,
    run_development_check,
    verify_development_check,
)
from .lock import build_lock, load_lock, save_lock
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

    args = parser.parse_args(argv)
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


if __name__ == "__main__":
    raise SystemExit(main())
