"""#362 C2 operator driver: hanchan process parallelism around the frozen L0.3 producer.

This file is operational tooling only. It is executed OUTSIDE the frozen
scientific checkout (Arena ``1a14855832315abfe30244d68b7ea6498c3370a0``) and
imports the producer installed from that checkout, so the source records the
frozen ``arena_revision`` and nothing here enters the scientific artifact.

``generate`` performs exactly the per-game body of
``generate_focal_outcome_source()`` (``run_focal_game`` -> ``write_game``) in
independent spawn worker processes, then reassembles the game summaries in
canonical global ``game_ordinal`` order 0..N-1 and publishes through the
unchanged ``build_manifest`` / ``verify_focal_outcome_source`` path. Completion
order, worker count and timing never reach the manifest. Any failed game aborts
the whole run and nothing is published.

``readback`` runs locally after artifact recovery: Arena strict verification,
pinned lisjong strict read, ``build_outcome_targets()`` and the #79 preflight
facts / frozen hard stops.
"""

from __future__ import annotations

import argparse
import json
import math
import multiprocessing
import shutil
import sys
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path

from lisjong_arena import seed_registry
from lisjong_arena.focal_outcome_source import source as producer
from lisjong_arena.focal_outcome_source.accounting import FocalOutcomeSourceError
from lisjong_arena.offense_foundation.qualification import write_document

FROZEN_ARENA_REVISION = "1a14855832315abfe30244d68b7ea6498c3370a0"
OWNER_ISSUE = "lisbun/lisjong-arena#362"
PROTOCOL = "l0.3-outcome-scientific-v1"
POPULATION = "l0.3-outcome-scientific"
SPLIT_SIZES = (("TRAIN", 400), ("SELECT", 100))
MAX_WORKERS = 32
TRAIN_SUPPORT_MINIMUM = 0.20
"""#79 frozen hard stop: TRAIN canonical-first and non-canonical-first >= 20%."""

_SPAWN = multiprocessing.get_context("spawn")


class C2DriverError(RuntimeError):
    """#362 operator precondition or readback failure (STOP / INVALID)."""


# ---------------------------------------------------------------------------
# generate
# ---------------------------------------------------------------------------


def _write_one(staging: str, game_ordinal: int, seed: int, split: str):
    """One hanchan in a worker process: the producer's own per-game body."""
    execution = producer.run_focal_game(
        seed=seed, focal_seat=producer.focal_seat_for(game_ordinal)
    )
    return producer.write_game(
        Path(staging) / f"game-{game_ordinal:03d}",
        execution,
        game_ordinal=game_ordinal,
        seed=seed,
        split=split,
    )


def generate_parallel(
    destination,
    *,
    population_role,
    games,
    allocation_bindings,
    source_contract,
    workers,
    progress=None,
):
    """Parallel equivalent of ``generate_focal_outcome_source()``.

    Pre-execution validation, staging, manifest, strict readback and the final
    rename are the producer's. Only the game loop is distributed.
    """
    population = producer.validate_population(
        population_role, games, allocation_bindings
    )
    producer.validate_source_contract(source_contract)
    if type(workers) is not int or not 1 <= workers <= MAX_WORKERS:
        raise C2DriverError(f"workers must be an int from 1 through {MAX_WORKERS}")
    workers = min(workers, len(population))
    destination = Path(destination)
    staging = destination.with_name(f".{destination.name}.partial")
    if destination.exists() or staging.exists():
        raise FileExistsError(f"refusing to overwrite: {destination}")
    staging.mkdir(parents=False)
    try:
        summaries = [None] * len(population)
        executor = ProcessPoolExecutor(max_workers=workers, mp_context=_SPAWN)
        try:
            futures = {
                executor.submit(_write_one, str(staging), ordinal, seed, split): ordinal
                for ordinal, (seed, split) in enumerate(population)
            }
            for completed, future in enumerate(as_completed(futures), start=1):
                summaries[futures[future]] = future.result()
                if progress is not None:
                    progress(completed, len(population))
        finally:
            executor.shutdown(wait=True, cancel_futures=True)
        if any(summary is None for summary in summaries):
            raise C2DriverError("incomplete parallel hanchan collection")
        for ordinal, summary in enumerate(summaries):
            if summary["game_ordinal"] != ordinal:
                raise C2DriverError("game summary is not at its canonical ordinal")
        write_document(
            staging / producer.MANIFEST_FILENAME,
            producer.build_manifest(
                population_role=population_role,
                game_summaries=summaries,
                allocation_bindings=allocation_bindings,
                source_contract=source_contract,
            ),
        )
        verified = producer.verify_focal_outcome_source(staging)
        staging.rename(destination)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return verified


def _read_json(path) -> object:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def c2_population(request: object, ledger: object):
    """The frozen #362 population: TRAIN 400 then SELECT 100, live-authority bound."""
    if type(request) is not dict or set(request) != {name for name, _ in SPLIT_SIZES}:
        raise C2DriverError("request must contain exactly TRAIN and SELECT")
    games: list[tuple[int, str]] = []
    bindings: dict[str, object] = {}
    for split, size in SPLIT_SIZES:
        entry = request[split]
        if type(entry) is not dict or set(entry) != {"binding", "first", "last"}:
            raise C2DriverError(f"{split} request fields are invalid")
        first, last = entry["first"], entry["last"]
        if type(first) is not int or type(last) is not int or last - first + 1 != size:
            raise C2DriverError(f"{split} must be exactly {size} contiguous seeds")
        seeds = list(range(first, last + 1))
        try:
            seed_registry.require_allocation_binding(
                ledger,
                entry["binding"],
                seeds=seeds,
                owner_issue=OWNER_ISSUE,
                protocol=PROTOCOL,
                seed_domain=seed_registry.RIICHIENV_HALF_HANCHAN_SEED_DOMAIN,
                population=POPULATION,
                split=split,
            )
        except seed_registry.SeedRegistryError as error:
            raise C2DriverError(
                f"{split} allocation is not authorized: {error}"
            ) from error
        bindings[split] = entry["binding"]
        games.extend((seed, split) for seed in seeds)
    return games, bindings


def build_request(authorizing_ledgers: dict[str, object], allocations: dict[str, str]):
    """TRAIN / SELECT request from each allocation's authorizing ledger snapshot."""
    request = {}
    for split, _ in SPLIT_SIZES:
        ledger = authorizing_ledgers[split]
        record = seed_registry.find_allocation(ledger, allocations[split])
        membership = record["seed_membership"]
        if membership["kind"] != "range":
            raise C2DriverError(f"{split} allocation is not a contiguous range")
        request[split] = {
            "binding": seed_registry.allocation_binding(ledger, allocations[split]),
            "first": membership["first"],
            "last": membership["last"],
        }
    return request


def _population_summary(games, bindings) -> dict[str, object]:
    return {
        split: {
            "allocation_identity": bindings[split]["allocation_identity"],
            "first": min(seed for seed, s in games if s == split),
            "last": max(seed for seed, s in games if s == split),
            "size": sum(1 for _, s in games if s == split),
        }
        for split, _ in SPLIT_SIZES
    }


def _frozen_source_contract(project: Path) -> dict[str, object]:
    src = (project.parent / "src").resolve()
    if not Path(producer.__file__).resolve().is_relative_to(src):
        raise C2DriverError("the producer is not imported from the frozen checkout")
    contract = producer.build_source_contract(project)
    if contract["arena_revision"] != FROZEN_ARENA_REVISION:
        raise C2DriverError(
            f"arena_revision {contract['arena_revision']} is not the frozen revision"
        )
    return contract


def _progress_writer(path: Path, run_id: str, total: int, workers: int):
    from lisjong_arena.aws_execution_observability import (
        ProgressTracker,
        write_progress,
    )

    path.parent.mkdir(parents=True, exist_ok=True)
    tracker = ProgressTracker(
        run_id=run_id,
        unit_kind="hanchan",
        total_units=total,
        worker_count=workers,
        started_at=datetime.now(UTC),
    )
    write_progress(path, tracker.snapshot(0, now=datetime.now(UTC)))
    return lambda completed, _total: write_progress(
        path, tracker.snapshot(completed, now=datetime.now(UTC))
    )


def _generate_command(args) -> int:
    project = Path(args.project).resolve()
    games, bindings = c2_population(
        _read_json(args.request), _read_json(args.seed_ledger)
    )
    contract = _frozen_source_contract(project)
    workers = min(args.workers, len(games))
    progress = None
    if args.progress:
        progress = _progress_writer(
            Path(args.progress), args.run_id, len(games), workers
        )
    verified = generate_parallel(
        args.destination,
        population_role=producer.SCIENTIFIC_ROLE,
        games=games,
        allocation_bindings=bindings,
        source_contract=contract,
        workers=workers,
        progress=progress,
    )
    print(
        json.dumps(
            {
                "arena_revision": contract["arena_revision"],
                "hanchan": len(verified.games),
                "source_identity": verified.identity,
                "status": "SOURCE PUBLISHED",
            },
            sort_keys=True,
        )
    )
    return 0


# ---------------------------------------------------------------------------
# local readback / #79 preflight facts
# ---------------------------------------------------------------------------


def _split_facts(targets, rows) -> dict[str, object]:
    from lisjong.learning import OutcomeTargets, summarize_outcome_targets

    hanchan = len({row.game_ordinal for row in rows})
    summary = summarize_outcome_targets(
        OutcomeTargets(
            source_identity=targets.source_identity,
            rows=tuple(rows),
            excluded=dict(targets.excluded),
            hanchan_count=hanchan,
        )
    )
    del summary["excluded"]  # excluded counts are source-wide, reported once
    positions = Counter(
        row.survivors.index(row.selected_candidate_index) for row in rows
    )
    summary["selection_position_distribution"] = {
        str(position): positions[position] for position in sorted(positions)
    }
    return summary


def readback(path) -> dict[str, object]:
    """Complete strict readback plus the #79 facts and frozen hard stops."""
    from lisjong.learning import build_outcome_targets, read_outcome_source

    path = Path(path)
    arena = producer.verify_focal_outcome_source(path)
    source = read_outcome_source(path)
    if source.identity != arena.identity:
        raise C2DriverError("Arena and lisjong strict reads disagree on identity")
    expected_splits = [name for name, size in SPLIT_SIZES for _ in range(size)]
    if [game.split for game in source.games] != expected_splits:
        raise C2DriverError("split counts / order are not TRAIN 400 -> SELECT 100")
    if [game.game_ordinal for game in source.games] != list(
        range(len(expected_splits))
    ):
        raise C2DriverError("global game ordinal is not exactly 0..499")
    if any(int(game.focal_seat) != game.game_ordinal % 4 for game in source.games):
        raise C2DriverError("focal-seat rotation is not game_ordinal % 4")
    focal_counts = {
        split: Counter(int(g.focal_seat) for g in source.games if g.split == split)
        for split, _ in SPLIT_SIZES
    }
    targets = build_outcome_targets(source)
    stops = []
    if any(not math.isfinite(row.target_q) for row in targets.rows):
        stops.append("malformed / non-finite target")
    if any(row.selected_candidate_index not in row.survivors for row in targets.rows):
        stops.append("selected action outside recomputed survivors")
    by_split = {
        split: [row for row in targets.rows if row.split == split]
        for split, _ in SPLIT_SIZES
    }
    facts = {split: _split_facts(targets, rows) for split, rows in by_split.items()}
    train = facts["TRAIN"]
    if not train["eligible_row_count"]:
        stops.append("TRAIN has no eligible rows")
    else:
        if train["canonical_first_selected_rate"] < TRAIN_SUPPORT_MINIMUM:
            stops.append("TRAIN canonical-first selection < 20%")
        if train["non_canonical_first_selected_rate"] < TRAIN_SUPPORT_MINIMUM:
            stops.append("TRAIN non-canonical-first selection < 20%")
    return {
        "allocation_bindings": source.allocation_bindings,
        "behavior": source.behavior,
        "excluded": targets.excluded,
        "focal_seat_counts": {
            split: [counts[seat] for seat in range(4)]
            for split, counts in focal_counts.items()
        },
        "hard_stops": stops,
        "hanchan": len(source.games),
        "result": "SCIENTIFIC SOURCE READY" if not stops else "STOP / INVALID",
        "source_contract_digest": source.source_contract_digest,
        "source_identity": source.identity,
        "splits": facts,
        "target_identity": targets.target_identity,
    }


def _readback_command(args) -> int:
    report = readback(args.source)
    text = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        output = Path(args.output)
        if output.exists():
            raise FileExistsError(f"refusing to overwrite: {output}")
        output.write_text(text, encoding="utf-8", newline="\n")
    sys.stdout.write(text)
    return 0 if not report["hard_stops"] else 3


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    generate = commands.add_parser("generate")
    generate.add_argument("--project", required=True)
    generate.add_argument("--request", required=True)
    generate.add_argument("--seed-ledger", required=True)
    generate.add_argument("--destination", required=True)
    generate.add_argument("--workers", type=int, required=True)
    generate.add_argument("--progress")
    generate.add_argument("--run-id", default="local")
    check = commands.add_parser("readback")
    check.add_argument("--source", required=True)
    check.add_argument("--output")
    build = commands.add_parser("build-request")
    for split, _ in SPLIT_SIZES:
        build.add_argument(f"--{split.lower()}-ledger", required=True)
        build.add_argument(f"--{split.lower()}-allocation", required=True)
    build.add_argument("--output", required=True)
    validate = commands.add_parser("check-request")
    validate.add_argument("--request", required=True)
    validate.add_argument("--seed-ledger", required=True)
    return parser


def _build_request_command(args) -> int:
    request = build_request(
        {
            split: _read_json(getattr(args, f"{split.lower()}_ledger"))
            for split, _ in SPLIT_SIZES
        },
        {
            split: getattr(args, f"{split.lower()}_allocation")
            for split, _ in SPLIT_SIZES
        },
    )
    output = Path(args.output)
    if output.exists():
        raise FileExistsError(f"refusing to overwrite: {output}")
    output.write_text(
        json.dumps(request, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return _check_request_command(
        argparse.Namespace(request=output, seed_ledger=args.select_ledger)
    )


def _check_request_command(args) -> int:
    games, bindings = c2_population(
        _read_json(args.request), _read_json(args.seed_ledger)
    )
    print(json.dumps(_population_summary(games, bindings), sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "generate":
            return _generate_command(args)
        if args.command == "build-request":
            return _build_request_command(args)
        if args.command == "check-request":
            return _check_request_command(args)
        return _readback_command(args)
    except (
        C2DriverError,
        FocalOutcomeSourceError,
        FileExistsError,
        seed_registry.SeedRegistryError,
    ) as error:
        print(f"STOP / INVALID: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
