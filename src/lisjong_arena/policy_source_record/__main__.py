"""CLI: population / generate / readback / replay-verify / archive / restore.

No AWS resources.
"""

import argparse
import json
import sys
from pathlib import Path

from lisjong_arena._artifact_io import canonical_json_text, write_new_artifact_file
from lisjong_arena.seed_registry import SeedRegistryError, load_ledger, parse_seed_spec

from . import archive, record
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
    replay.add_argument("--summary", help="also write the summary to this new file")

    pack = commands.add_parser(
        "archive", help="pack a replay-verified record with its evidence (#449)"
    )
    pack.add_argument("--source", required=True)
    pack.add_argument(
        "--replay-summary", required=True, help="replay-verify --summary output"
    )
    pack.add_argument("--output", required=True)

    restore = commands.add_parser(
        "restore", help="restore a retained record after identity/hash/replay checks"
    )
    restore.add_argument("--archive", required=True)
    restore.add_argument("--expected-identity", required=True)
    restore.add_argument("--output", required=True)
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
        elif args.command == "replay-verify":
            output = replay_verify(
                args.source, project=args.project, workers=args.workers
            )
            if args.summary is not None:
                write_new_artifact_file(Path(args.summary), canonical_json_text(output))
        elif args.command == "archive":
            summary = json.loads(Path(args.replay_summary).read_text(encoding="utf-8"))
            evidence = archive.archive_source_record(
                args.source, args.output, replay=summary
            )
            output = {
                "source_identity": evidence["source"]["identity"],
                "archive_sha256": evidence["archive"]["sha256"],
                "archive_bytes": evidence["archive"]["bytes"],
                "evidence_identity": evidence["identity"],
            }
        else:
            manifest = archive.restore_source_record(
                args.archive, args.output, expected_identity=args.expected_identity
            )
            output = {
                "identity": manifest["identity"],
                "games": len(manifest["games"]),
                "decisions": sum(g["decision_count"] for g in manifest["games"]),
                "output": args.output,
            }
    except (PolicySourceRecordError, SeedRegistryError, FileExistsError) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        return 1
    print(json.dumps(output, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
