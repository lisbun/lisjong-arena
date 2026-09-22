"""#331 prerequisite CLI. No implicit seeds, training, or AWS resource creation."""

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from lisjong_arena._artifact_io import (
    canonical_json_text,
    parse_json_text,
    write_new_artifact_file,
)
from lisjong_arena.aws_execution_observability import ProgressTracker, write_progress
from lisjong_arena.seed_registry import load_ledger

from .corpus import generate, read_corpus
from .protocol import make_lock, require_request_allocations, validate_request
from .qualification import (
    P0_PASS,
    P1_PASS,
    qualification_contract,
    qualify,
    read_document,
    require_matching_qualification_contract,
    require_qualification,
    runtime_binding,
    write_document,
)
from .source_record import read_source_record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default="pyproject.toml")
    commands = parser.add_subparsers(dest="command", required=True)
    qualification = commands.add_parser(
        "qualify", help="P0/P1 fixture qualification only; no games"
    )
    qualification.add_argument("--output", required=True)
    request_validation = commands.add_parser(
        "validate-request", help="validate an operator-supplied population request"
    )
    request_validation.add_argument("--request", required=True)
    request_validation.add_argument("--seed-ledger")
    request_validation.add_argument(
        "--phase", choices=("P2", "SCIENTIFIC"), required=True
    )
    contract = commands.add_parser(
        "qualification-contract",
        help="derive the cross-platform scientific/runtime contract fields",
    )
    contract.add_argument("--qualification", required=True)
    contract.add_argument("--output", required=True)
    contract_match = commands.add_parser(
        "require-qualification-contract-match",
        help="fail closed unless a local and remote qualification contract match",
    )
    contract_match.add_argument("--local", required=True)
    contract_match.add_argument("--remote", required=True)
    lock = commands.add_parser(
        "lock", help="bind operator-supplied fresh population before generation"
    )
    lock.add_argument("--request", required=True)
    lock.add_argument("--seed-ledger")
    lock.add_argument("--qualification", required=True)
    lock.add_argument("--p2-corpus")
    lock.add_argument("--output", required=True)
    generation = commands.add_parser("generate")
    generation.add_argument("--lock", required=True)
    generation.add_argument("--p2-corpus")
    generation.add_argument("--output", required=True)
    generation.add_argument("--source-record-output")
    generation.add_argument("--workers", type=int, default=1)
    generation.add_argument("--operational-progress-path")
    generation.add_argument("--operational-run-id")
    readback = commands.add_parser("readback")
    readback.add_argument("--corpus", required=True)
    readback.add_argument("--lock", required=True)
    source_readback = commands.add_parser("source-readback")
    source_readback.add_argument("--source-record", required=True)
    source_readback.add_argument("--corpus", required=True)
    source_readback.add_argument("--lock", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "qualify":
            result = qualify(runtime_binding(args.project))
            write_document(args.output, result)
            print(
                json.dumps(
                    {
                        "identity": result["identity"],
                        "p0": result["p0"],
                        "p1": result["p1"],
                    }
                )
            )
            return 0 if result["p0"] == P0_PASS and result["p1"] == P1_PASS else 2
        if args.command == "validate-request":
            request = parse_json_text(Path(args.request).read_text(encoding="utf-8"))
            validate_request(request)
            if request["phase"] != args.phase:
                raise ValueError("population request phase differs from expected phase")
            ledger = load_ledger(args.seed_ledger) if args.seed_ledger else None
            require_request_allocations(request, ledger)
            print(json.dumps({"phase": args.phase, "status": "PASS"}))
            return 0
        if args.command == "qualification-contract":
            report = read_document(args.qualification)
            contract = qualification_contract(report)
            write_new_artifact_file(Path(args.output), canonical_json_text(contract))
            print(json.dumps(contract, sort_keys=True))
            return 0
        if args.command == "require-qualification-contract-match":
            local_contract = parse_json_text(
                Path(args.local).read_text(encoding="utf-8")
            )
            remote_contract = parse_json_text(
                Path(args.remote).read_text(encoding="utf-8")
            )
            require_matching_qualification_contract(local_contract, remote_contract)
            print(json.dumps({"status": "PASS"}))
            return 0
        if args.command == "lock":
            report = read_document(args.qualification)
            require_qualification(report, runtime_binding(args.project))
            request = parse_json_text(Path(args.request).read_text(encoding="utf-8"))
            ledger = load_ledger(args.seed_ledger) if args.seed_ledger else None
            require_request_allocations(request, ledger)
            p2 = read_corpus(args.p2_corpus) if args.p2_corpus else None
            result = make_lock(request, report, p2)
            write_document(args.output, result)
        elif args.command == "generate":
            started = time.monotonic()
            started_at = datetime.now(UTC)
            tracker = None
            if bool(args.operational_progress_path) != bool(args.operational_run_id):
                raise ValueError(
                    "operational progress path and run id must be supplied together"
                )
            if args.operational_progress_path:
                total = sum(
                    len(v)
                    for v in read_document(args.lock)["request"]["populations"].values()
                )
                tracker = ProgressTracker(
                    run_id=args.operational_run_id,
                    unit_kind="hanchan",
                    total_units=total,
                    worker_count=args.workers,
                    started_at=started_at,
                )
                write_progress(
                    args.operational_progress_path,
                    tracker.snapshot(0, now=started_at),
                )

            def progress(completed, total):
                # Only operational units/time; no partial support or results.
                elapsed = time.monotonic() - started
                document = {
                    "completed": completed,
                    "total": total,
                    "elapsed_seconds": elapsed,
                    "eta_seconds": elapsed / completed * (total - completed),
                }
                if tracker is not None:
                    snapshot = tracker.snapshot(completed, now=datetime.now(UTC))
                    write_progress(args.operational_progress_path, snapshot)
                    document.update(
                        {
                            "throughput_per_hour": snapshot["throughput_per_hour"],
                            "estimated_finish_at": snapshot["estimated_finish_at"],
                            "workers": snapshot["worker_count"],
                        }
                    )
                print(json.dumps(document), flush=True)

            result = generate(
                read_document(args.lock),
                args.output,
                project=args.project,
                p2_path=args.p2_corpus,
                progress=progress,
                workers=args.workers,
                source_record_destination=args.source_record_output,
            )
        elif args.command == "readback":
            result = read_corpus(args.corpus, expected_lock=read_document(args.lock))
        else:
            result = read_source_record(
                args.source_record,
                expected_lock=read_document(args.lock),
                corpus_path=args.corpus,
            )
        print(
            json.dumps(
                {"identity": result["identity"], "p2_outcome": result.get("p2_outcome")}
            )
        )
        return 0
    except Exception as error:
        print(f"STOP / INVALID: {type(error).__name__}: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
