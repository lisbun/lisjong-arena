"""CLI: ``python -m lisjong_arena.aabb_kyoku_diagnostic {run,verify,summarize}``。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from lisjong_arena.artifact import load_comparison_artifact
from lisjong_arena.heuristic_candidate_aabb.protocol import GAME_MODE, MAX_STEPS

from .replay import (
    ReplayError,
    file_sha256,
    load_records,
    run_replay,
    verify_against_comparison,
)
from .summary import summarize


def _seeds(text: str) -> list[int]:
    if ".." in text:
        first, last = text.split("..")
        return list(range(int(first), int(last) + 1))
    return [int(part) for part in text.split(",") if part]


def _write_new(path: Path, document: dict) -> None:
    with Path(path).open("x", encoding="utf-8") as handle:
        json.dump(document, handle, ensure_ascii=False, indent=1, sort_keys=True)
        handle.write("\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lisjong_arena.aabb_kyoku_diagnostic")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="replay games and record kyoku-level results")
    run.add_argument("--policy-a", required=True, help="candidate catalog identity")
    run.add_argument("--policy-b", required=True, help="incumbent catalog identity")
    run.add_argument("--seeds", required=True, help="'first..last' or 'a,b,c'")
    run.add_argument("--game-mode", default=GAME_MODE)
    run.add_argument("--max-steps", type=int, default=MAX_STEPS)
    run.add_argument("--workers", type=int, default=1)
    run.add_argument("--out", required=True, type=Path)
    verify = sub.add_parser("verify", help="match replayed results to comparison.json")
    verify.add_argument("--records", required=True, type=Path)
    verify.add_argument("--comparison", required=True, type=Path)
    verify.add_argument("--out", required=True, type=Path)
    summ = sub.add_parser("summarize", help="descriptive metrics after a PASS verify")
    summ.add_argument("--records", required=True, type=Path)
    summ.add_argument("--verification", required=True, type=Path)
    summ.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)

    if args.command == "run":

        def progress(done: int, total: int) -> None:
            print(f"{done}/{total}", file=sys.stderr, flush=True)

        run_replay(
            policy_a=args.policy_a,
            policy_b=args.policy_b,
            seeds=_seeds(args.seeds),
            game_mode=args.game_mode,
            max_steps=args.max_steps,
            output=args.out,
            workers=args.workers,
            progress=progress,
        )
        return 0
    if args.command == "verify":
        comparison = load_comparison_artifact(args.comparison)
        document = verify_against_comparison(load_records(args.records), comparison)
        document["records_sha256"] = file_sha256(args.records)
        document["comparison_sha256"] = file_sha256(args.comparison)
        _write_new(args.out, document)
        print(document["status"])
        return 0 if document["status"] == "PASS" else 1
    verification = json.loads(Path(args.verification).read_text(encoding="utf-8"))
    if verification.get("status") != "PASS":
        raise ReplayError("summarize requires a PASS verification (STOP otherwise)")
    if verification.get("records_sha256") != file_sha256(args.records):
        raise ReplayError("records differ from the verified records")
    document = summarize(
        load_records(args.records),
        policy_a=verification["policy_a_identity"],
        policy_b=verification["policy_b_identity"],
    )
    document["verification_sha256"] = file_sha256(args.verification)
    document["records_sha256"] = verification["records_sha256"]
    _write_new(args.out, document)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
