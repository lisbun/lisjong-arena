"""lisjong#257 HandBelief measurement population (lisjong-arena#453).

Generates the 400-hanchan measurement population as four complete lisjong#256
v1 sources of 100 hanchan each (see ``docs/hand-belief-source-453.md``).  It
reuses ``_play`` / ``write_source`` / ``verify_source_coverage`` of the pilot
producer ``generate_hand_belief_source_255.py`` unchanged; the pilot's seed
guard is neither lifted nor bypassed.

- The seeds, the train / valid / eval split and the four units are fixed by the
  protocol (lisjong#257 pre-registration).  No argument changes them.
- Generation requires a Seed Registry allocation (RESERVED / COMMITTED) whose
  owner issue, protocol, seed domain, population, split and seed membership are
  this population, and whose ``arena_revision`` is the executing checkout.  Any
  mismatch stops the run before a game is played.
- Generation requires ``LISJONG_SHANTEN_BACKEND=rust``.

Output directory (all new; an existing path is refused)::

    unit-0 .. unit-3/         one v1 source each (manifest / decisions /
                              hand_facts / coverage)
    generation.json           allocation binding, runtime identity, per-unit and
                              per-game timings, file digests

Usage::

    LISJONG_SHANTEN_BACKEND=rust python scripts/generate_hand_belief_measurement_257.py \\
        run --seed-ledger <live ledger> --allocation-identity <sha256> \\
        --workers 32 --output <new directory>
    python scripts/generate_hand_belief_measurement_257.py check-allocation \\
        --seed-ledger <live ledger> --allocation-identity <sha256> \\
        --arena-revision <full sha>

Generated data is not committed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import generate_hand_belief_source_255 as dev  # noqa: E402

OWNER_ISSUE = "lisbun/lisjong#257"
PROTOCOL = "hand-belief-accuracy-baseline-measurement-v1"
POPULATION = "hand-belief-accuracy-baseline-measurement"
SPLIT = "TRAIN-VALID-EVAL"
MEASUREMENT_SEEDS = tuple(range(933000, 933400))
TRAIN_SEEDS = tuple(range(933000, 933160))
VALID_SEEDS = tuple(range(933160, 933240))
EVAL_SEEDS = tuple(range(933240, 933400))
UNIT_COUNT = 4
USED_RANGES = (range(920000, 920544), range(931000, 932000), range(932000, 932100))
"""Engine-domain seeds allocated before this population (calibration / L0.3,
the RETIRED lisjong-arena#385 population incl. every pilot, lisjong#245)."""

GENERATION_SCHEMA = "lisjong-arena-hand-belief-measurement-generation-v1"
GENERATION_FILENAME = "generation.json"
SOURCE_FILES = ("manifest.json", "decisions.jsonl", "hand_facts.jsonl", "coverage.json")
_SHA256 = re.compile(r"\A[0-9a-f]{64}\Z")


class MeasurementSourceError(RuntimeError):
    """A precondition, generation or completeness check failed (STOP / INVALID)."""


def unit_splits(unit: int) -> dict[str, list[int]]:
    """Seeds of one unit.  The manifest's ``test`` split is the lisjong#257 eval."""
    if unit not in range(UNIT_COUNT):
        raise MeasurementSourceError(f"unit must be 0..{UNIT_COUNT - 1}")

    def part(seeds: tuple[int, ...]) -> list[int]:
        size = len(seeds) // UNIT_COUNT
        return list(seeds[unit * size : (unit + 1) * size])

    return {
        "train": part(TRAIN_SEEDS),
        "valid": part(VALID_SEEDS),
        "test": part(EVAL_SEEDS),
    }


def check_population() -> None:
    """The protocol's own seed population must be internally consistent."""
    if TRAIN_SEEDS + VALID_SEEDS + EVAL_SEEDS != MEASUREMENT_SEEDS:
        raise MeasurementSourceError("splits must partition 933000..933399 in order")
    if (len(TRAIN_SEEDS), len(VALID_SEEDS), len(EVAL_SEEDS)) != (160, 80, 160):
        raise MeasurementSourceError("splits must be train 160 / valid 80 / eval 160")
    units = [unit_splits(unit) for unit in range(UNIT_COUNT)]
    if any(
        (len(u["train"]), len(u["valid"]), len(u["test"])) != (40, 20, 40)
        for u in units
    ):
        raise MeasurementSourceError("each unit must be train 40 / valid 20 / eval 40")
    covered = sorted(seed for u in units for name in dev.SPLITS for seed in u[name])
    if covered != list(MEASUREMENT_SEEDS):
        raise MeasurementSourceError("units must cover every seed exactly once")
    if any(seed in used for seed in MEASUREMENT_SEEDS for used in USED_RANGES):
        raise MeasurementSourceError("measurement seeds overlap already used seeds")


def _seed_domain() -> str:
    from lisjong_arena import seed_registry

    return seed_registry.LISJONG_ENGINE_HANCHAN_SEED_DOMAIN


def load_live_ledger(path: Path) -> dict[str, object]:
    from lisjong_arena import seed_registry

    try:
        return seed_registry.load_ledger(path, strict_serialization=False)
    except seed_registry.SeedRegistryError as error:
        raise MeasurementSourceError(f"invalid live ledger: {error}") from error


def authorize(
    ledger: object, allocation_identity: str, *, arena_revision: str
) -> tuple[dict[str, object], dict[str, object]]:
    """Resolve the allocation against the live ledger; fail closed on any mismatch.

    Returns ``(binding, allocation record)``.
    """
    from lisjong_arena import seed_registry

    check_population()
    if type(allocation_identity) is not str or not _SHA256.fullmatch(
        allocation_identity
    ):
        raise MeasurementSourceError("allocation identity must be SHA-256 hex")
    try:
        binding = seed_registry.allocation_binding(ledger, allocation_identity)
        record = seed_registry.require_allocation_binding(
            ledger,
            binding,
            seeds=MEASUREMENT_SEEDS,
            owner_issue=OWNER_ISSUE,
            protocol=PROTOCOL,
            seed_domain=_seed_domain(),
            population=POPULATION,
            split=SPLIT,
        )
    except seed_registry.SeedRegistryError as error:
        raise MeasurementSourceError(
            f"allocation is not authorized: {error}"
        ) from error
    if record["arena_revision"] != arena_revision:
        raise MeasurementSourceError(
            f"allocation arena_revision {record['arena_revision']} is not the "
            f"executing checkout {arena_revision}"
        )
    return binding, record


def _play_one(seed: int):
    """One hanchan plus its own wall / CPU time, measured inside the worker."""
    started, cpu = time.perf_counter(), time.process_time()
    game = dev._play(seed)
    stats = {
        "wall_seconds": time.perf_counter() - started,
        "cpu_seconds": time.process_time() - cpu,
    }
    return game, stats


def _file_digest(path: Path) -> dict[str, object]:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return {"bytes": path.stat().st_size, "sha256": digest.hexdigest()}


def generate_unit(
    unit: int, directory: Path, *, workers: int, producer: dict[str, str], play
) -> dict[str, object]:
    """Play, write and re-check one unit; return its generation record."""
    splits = unit_splits(unit)
    seeds = [seed for name in dev.SPLITS for seed in splits[name]]
    started = time.perf_counter()
    if workers == 1:
        results = [play(seed) for seed in seeds]
    else:
        with ProcessPoolExecutor(workers) as executor:
            results = list(executor.map(play, seeds))
    games = [game for game, _ in results]
    if [game[0] for game in games] != seeds:
        raise MeasurementSourceError(f"unit {unit}: games differ from the unit seeds")
    manifest = dev.write_source(
        directory, splits=splits, games=games, producer=producer
    )
    total = dev.verify_source_coverage(directory)
    if manifest["splits"] != splits or total != manifest["files"]["decisions"]["rows"]:
        raise MeasurementSourceError(f"unit {unit}: written source is inconsistent")
    return {
        "unit": unit,
        "splits": {name: [seeds_[0], seeds_[-1]] for name, seeds_ in splits.items()},
        "hanchan": len(seeds),
        "in_scope_decisions": total,
        "wall_seconds": round(time.perf_counter() - started, 3),
        "files": {name: _file_digest(directory / name) for name in SOURCE_FILES},
        "games": [
            {
                "seed": game[0],
                "decisions": len(game[1]),
                "wall_seconds": round(stats["wall_seconds"], 3),
                "cpu_seconds": round(stats["cpu_seconds"], 3),
            }
            for game, stats in results
        ],
    }


def run(arguments: argparse.Namespace, *, play=_play_one) -> dict[str, object]:
    from lisjong_arena import seed_registry
    from lisjong_arena.policy_source_record import binding as runtime_identity

    project = Path(__file__).resolve().parents[1] / "pyproject.toml"
    runtime = runtime_identity.runtime_binding(project)
    if runtime["shanten_backend"]["name"] != "rust":
        raise MeasurementSourceError(
            "measurement generation requires LISJONG_SHANTEN_BACKEND=rust"
        )
    ledger = load_live_ledger(arguments.seed_ledger)
    binding, record = authorize(
        ledger,
        arguments.allocation_identity,
        arena_revision=str(runtime["arena_revision"]),
    )
    output = arguments.output
    if output.exists():
        raise MeasurementSourceError(f"refusing to overwrite {output}")
    output.mkdir(parents=True)
    producer = {
        "arena_revision": dev._arena_revision(),
        "lisjong_engine_revision": dev._revision("lisjong-engine"),
        "lisjong_revision": dev._revision("lisjong"),
        "policy": dev.POLICY,
    }
    if producer["arena_revision"] != record["arena_revision"]:
        raise MeasurementSourceError("the executing checkout is not clean")
    started = time.perf_counter()
    units = []
    for unit in range(UNIT_COUNT):
        units.append(
            generate_unit(
                unit,
                output / f"unit-{unit}",
                workers=arguments.workers,
                producer=producer,
                play=play,
            )
        )
        print(
            f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} unit {unit} done "
            f"({units[-1]['wall_seconds']}s)",
            file=sys.stderr,
            flush=True,
        )
    document = {
        "schema": GENERATION_SCHEMA,
        "owner_issue": OWNER_ISSUE,
        "protocol": PROTOCOL,
        "population": POPULATION,
        "split": SPLIT,
        "seeds": {
            "first": MEASUREMENT_SEEDS[0],
            "last": MEASUREMENT_SEEDS[-1],
            "count": len(MEASUREMENT_SEEDS),
        },
        "workers": arguments.workers,
        "wall_seconds": round(time.perf_counter() - started, 3),
        "producer": producer,
        "runtime": runtime,
        "allocation_binding": binding,
        "allocation": record,
        "ledger_revision": seed_registry.ledger_revision(ledger),
        "units": units,
    }
    (output / GENERATION_FILENAME).write_text(
        json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return document


def check_allocation(arguments: argparse.Namespace) -> dict[str, object]:
    from lisjong_arena import seed_registry

    ledger = load_live_ledger(arguments.seed_ledger)
    binding, record = authorize(
        ledger, arguments.allocation_identity, arena_revision=arguments.arena_revision
    )
    return {
        "allocation_binding": binding,
        "allocation": record,
        "ledger_revision": seed_registry.ledger_revision(ledger),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    run_parser = commands.add_parser("run")
    run_parser.add_argument("--workers", type=int, required=True)
    run_parser.add_argument("--output", type=Path, required=True)
    check = commands.add_parser(
        "check-allocation", help="no-game authorization check of an allocation"
    )
    check.add_argument("--arena-revision", required=True)
    for command in (run_parser, check):
        command.add_argument("--seed-ledger", type=Path, required=True)
        command.add_argument("--allocation-identity", required=True)
    return parser


def main(argv=None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        if arguments.command == "check-allocation":
            print(json.dumps(check_allocation(arguments), indent=2, sort_keys=True))
            return 0
        document = run(arguments)
        summary = {key: document[key] for key in ("seeds", "wall_seconds", "workers")}
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0
    except (MeasurementSourceError, dev.HandBeliefSourceProducerError) as error:
        print(f"STOP / INVALID: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
