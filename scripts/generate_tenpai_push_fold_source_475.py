"""Tenpai PUSH/FOLD paired source, production population (lisjong-arena#475).

Generates the 400-hanchan source of the lisbun/lisjong#288 wire contract from
which lisjong fits the comparison tables (train) and reports the round-result
differences (valid).  It is the lisjong-arena#476 producer
(``generate_tenpai_push_fold_source_476.py``) with another seed range and
allocation; the control, the fold-side replays, the determinism checks and the
recorded facts are that script's, unchanged.

- The seeds (939000..939399), the split (train 939000..939199, valid
  939200..939399) and the 32 workers are fixed by the protocol.  No argument
  changes them.  The lisjong-arena#476 pilot seeds (938000..938015) are not
  reused.
- Generation requires a fresh RESERVED Seed Registry allocation of this
  population whose ``arena_revision`` is the clean executing checkout, the
  pinned runtime, ``LISJONG_SHANTEN_BACKEND=rust`` and the native scorer built
  from the pinned lisjong.
- There is no resume, reuse or partial adoption.  A failed run leaves no
  ``generation.json``; its files are diagnostics only.
- Arena fits no table and judges nothing: bucket bounds, the minimum support
  and the stop rule are lisjong's.

Usage::

    LISJONG_SHANTEN_BACKEND=rust python scripts/generate_tenpai_push_fold_source_475.py \\
        run --seed-ledger <live ledger> --allocation-identity <sha256> \\
        --selection <selection.json> --workers 32 --output <new directory>
    python scripts/generate_tenpai_push_fold_source_475.py check-allocation \\
        --seed-ledger <live ledger> --allocation-identity <sha256> \\
        --arena-revision <full sha>

Generated data is not committed.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from lisjong_arena import measurement_allocation_guard as guard
from lisjong_arena import seed_registry

sys.path.insert(0, str(Path(__file__).resolve().parent))
import generate_tenpai_push_fold_source_476 as producer  # noqa: E402

OWNER_ISSUE = "lisbun/lisjong-arena#475"
PROTOCOL = "tenpai-push-fold-source-v1"
POPULATION = "tenpai-push-fold-source-400-hanchan"
SPLIT = "TRAIN200-VALID200"
SEEDS = tuple(range(939000, 939400))
SPLITS = {"train": SEEDS[:200], "valid": SEEDS[200:]}
REQUIRED_WORKERS = 32
USED_RANGES = (
    *producer.USED_RANGES,
    range(938000, 938016),  # lisjong-arena#476 pilot
)
"""Engine-domain seeds allocated before this population."""


def allocation_preset() -> guard.AllocationPreset:
    """This population's fixed allocation preset; no argument changes it."""
    return guard.AllocationPreset(
        owner_issue=OWNER_ISSUE,
        protocol=PROTOCOL,
        seed_domain=seed_registry.LISJONG_ENGINE_HANCHAN_SEED_DOMAIN,
        population=POPULATION,
        split=SPLIT,
        seeds=SEEDS,
        splits=tuple(SPLITS.items()),
        split_sizes=(200, 200),
        used_ranges=USED_RANGES,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    run_parser = commands.add_parser("run")
    run_parser.add_argument("--selection", type=Path, required=True)
    run_parser.add_argument("--workers", type=int, required=True)
    run_parser.add_argument("--output", type=Path, required=True)
    check = commands.add_parser(
        "check-allocation", help="no-game authorization check of the allocation"
    )
    check.add_argument("--arena-revision", required=True)
    for command in (run_parser, check):
        command.add_argument("--seed-ledger", type=Path, required=True)
        command.add_argument("--allocation-identity", required=True)
    return parser


def main(argv=None) -> int:
    from lisjong.hand_evaluation.scoring import ScoringBackendUnavailableError
    from lisjong.learning.errors import LearningError

    from lisjong_arena.policy_source_record import PolicySourceRecordError

    arguments = build_parser().parse_args(argv)
    try:
        if arguments.command == "check-allocation":
            result = producer.check_allocation(
                arguments.seed_ledger,
                arguments.allocation_identity,
                arguments.arena_revision,
                allocation_preset(),
            )
        else:
            if arguments.workers != REQUIRED_WORKERS:
                raise producer.TenpaiPushFoldProducerError(
                    f"workers must be {REQUIRED_WORKERS}"
                )
            document = producer.run_reserved(arguments, allocation_preset())
            result = {key: document[key] for key in ("files", "total", "wall_seconds")}
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except (
        producer.TenpaiPushFoldProducerError,
        PolicySourceRecordError,
        LearningError,
        ScoringBackendUnavailableError,
        producer.wire.TenpaiPushFoldSourceError,
    ) as error:
        print(f"STOP / INVALID: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
