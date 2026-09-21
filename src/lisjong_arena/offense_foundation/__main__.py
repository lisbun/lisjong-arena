"""#331 prerequisite CLI. No implicit seeds, training, or AWS resource creation."""

import argparse
import json
import sys
import time
from pathlib import Path

from lisjong_arena._artifact_io import parse_json_text

from .corpus import generate, read_corpus
from .protocol import make_lock
from .qualification import (
    P0_PASS,
    P1_PASS,
    qualify,
    read_document,
    require_qualification,
    runtime_binding,
    write_document,
)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default="pyproject.toml")
    commands = parser.add_subparsers(dest="command", required=True)
    qualification = commands.add_parser(
        "qualify", help="P0/P1 fixture qualification only; no games"
    )
    qualification.add_argument("--output", required=True)
    lock = commands.add_parser(
        "lock", help="bind operator-supplied fresh population before generation"
    )
    lock.add_argument("--request", required=True)
    lock.add_argument("--qualification", required=True)
    lock.add_argument("--p2-corpus")
    lock.add_argument("--output", required=True)
    generation = commands.add_parser("generate")
    generation.add_argument("--lock", required=True)
    generation.add_argument("--p2-corpus")
    generation.add_argument("--output", required=True)
    readback = commands.add_parser("readback")
    readback.add_argument("--corpus", required=True)
    readback.add_argument("--lock", required=True)
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
        if args.command == "lock":
            report = read_document(args.qualification)
            require_qualification(report, runtime_binding(args.project))
            request = parse_json_text(Path(args.request).read_text(encoding="utf-8"))
            p2 = read_corpus(args.p2_corpus) if args.p2_corpus else None
            result = make_lock(request, report, p2)
            write_document(args.output, result)
        elif args.command == "generate":
            started = time.monotonic()

            def progress(completed, total):
                # Only operational units/time; no partial support or results.
                elapsed = time.monotonic() - started
                print(
                    json.dumps(
                        {
                            "completed": completed,
                            "total": total,
                            "elapsed_seconds": elapsed,
                            "eta_seconds": elapsed / completed * (total - completed),
                        }
                    ),
                    flush=True,
                )

            result = generate(
                read_document(args.lock),
                args.output,
                project=args.project,
                p2_path=args.p2_corpus,
                progress=progress,
            )
        else:
            result = read_corpus(args.corpus, expected_lock=read_document(args.lock))
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
