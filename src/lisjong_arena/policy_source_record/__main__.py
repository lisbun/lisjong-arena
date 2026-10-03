"""CLI: population / generate / readback / replay-verify. No AWS resources."""

import argparse
import json
import sys
from pathlib import Path

from lisjong_arena._artifact_io import canonical_json_text, write_new_artifact_file
from lisjong_arena.seed_registry import SeedRegistryError, load_ledger, parse_seed_spec

from . import record
from .errors import PolicySourceRecordError
from .generation import generate
from .replay import replay_verify

_SPLIT_OPTIONS = {"TRAIN": "train", "SELECT": "select", "OFFLINE-EVAL": "offline_eval"}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m lisjong_arena.policy_source_record"
    )
    commands = parser.add_subparsers(dest="command", required=True)

    population = commands.add_parser(
        "population", help="write a DEVELOPMENT population document"
    )
    for split, dest in _SPLIT_OPTIONS.items():
        population.add_argument(f"--{split.lower()}", dest=dest, metavar="SEEDS")
    population.add_argument("--output", required=True)

    run = commands.add_parser("generate", help="teacher self-play source generation")
    run.add_argument("--population", required=True)
    run.add_argument("--teacher", required=True, help="policy_catalog identity")
    run.add_argument("--output", required=True)
    run.add_argument("--workers", type=int, default=1)
    run.add_argument("--ledger", help="Arena seed ledger (SCIENTIFIC only)")
    run.add_argument("--project", default="pyproject.toml")

    readback = commands.add_parser("readback", help="strict source readback")
    readback.add_argument("--source", required=True)

    replay = commands.add_parser("replay-verify", help="rerun the teacher on every row")
    replay.add_argument("--source", required=True)
    replay.add_argument("--workers", type=int, default=1)
    replay.add_argument("--project", default="pyproject.toml")
    return parser


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "population":
            populations = {
                split: parse_seed_spec(getattr(args, dest))
                for split, dest in _SPLIT_OPTIONS.items()
                if getattr(args, dest) is not None
            }
            document = record.population_document(record.DEVELOPMENT, populations)
            write_new_artifact_file(Path(args.output), canonical_json_text(document))
            output = {
                "population": args.output,
                "games": sum(map(len, populations.values())),
            }
        elif args.command == "generate":
            population = record.load_population(args.population)
            ledger = None if args.ledger is None else load_ledger(args.ledger)
            manifest = generate(
                population,
                args.output,
                teacher=args.teacher,
                project=args.project,
                ledger=ledger,
                workers=args.workers,
                progress=lambda done, total: print(
                    f"{done}/{total}", file=sys.stderr, flush=True
                ),
            )
            output = {"identity": manifest["identity"], "games": len(manifest["games"])}
        elif args.command == "readback":
            manifest = record.read_source_record(args.source)
            output = {
                "identity": manifest["identity"],
                "purpose": manifest["purpose"],
                "teacher": manifest["source_contract"]["teacher"]["catalog_identity"],
                "games": len(manifest["games"]),
                "decisions": sum(g["decision_count"] for g in manifest["games"]),
            }
        else:
            output = replay_verify(
                args.source, project=args.project, workers=args.workers
            )
    except (PolicySourceRecordError, SeedRegistryError, FileExistsError) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        return 1
    print(json.dumps(output, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
