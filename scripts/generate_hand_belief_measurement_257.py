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
    generation.json           written only when all four units are complete:
                              allocation binding, runtime identity, unit records

Archive directory (``--archive-dir``; files are added as each unit completes)::

    <prefix>unit-k.tar.zst        the unit's source directory
    <prefix>unit-k.complete.json  written last: the unit record (file digests,
                                  per-game timings, producer, allocation,
                                  backend, workers) and the archive's SHA-256

A unit is complete only when both of its archive files exist and agree.  If a
later unit fails, the completed units stay in the archive directory.  A second
run with ``--reuse-dir <directory holding those files>`` first re-checks and
restores every completed unit (archive SHA-256, file digests, coverage, and that
allocation, producer revisions, backend and worker count are the current ones)
and only then generates the units that are missing; a unit that fails the check
stops the run before any game is played.  A unit record is not a
statement about the population: only ``generation.json`` says that all 400
hanchan exist, and nothing may be evaluated without it.

Usage::

    LISJONG_SHANTEN_BACKEND=rust python scripts/generate_hand_belief_measurement_257.py \\
        run --seed-ledger <live ledger> --allocation-identity <sha256> \\
        --workers 32 --output <new directory> --archive-dir <directory> \\
        [--archive-prefix <text>] [--reuse-dir <directory>]
    python scripts/generate_hand_belief_measurement_257.py check-allocation \\
        --seed-ledger <live ledger> --allocation-identity <sha256> \\
        --arena-revision <full sha>

Generated data is not committed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tarfile
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
UNIT_SCHEMA = "lisjong-arena-hand-belief-measurement-unit-v1"
ARCHIVE_LEVEL = 10
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


def _split_bounds(splits: dict[str, list[int]]) -> dict[str, list[int]]:
    return {name: [seeds[0], seeds[-1]] for name, seeds in splits.items()}


def _write_new(path: Path, text: str) -> None:
    """Write ``path`` so that it never exists with partial content."""
    partial = path.with_name(f".partial-{path.name}")
    with open(partial, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(partial, path)


def _archive_names(prefix: str, unit: int) -> tuple[str, str]:
    return f"{prefix}unit-{unit}.tar.zst", f"{prefix}unit-{unit}.complete.json"


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
        "schema": UNIT_SCHEMA,
        "unit": unit,
        "splits": _split_bounds(splits),
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


def publish_unit(
    record: dict[str, object], directory: Path, archive_dir: Path, prefix: str
) -> dict[str, object]:
    """Put one complete unit into the archive directory; the record goes last."""
    unit = record["unit"]
    archive_name, record_name = _archive_names(prefix, unit)
    for name in (archive_name, record_name):
        if (archive_dir / name).exists():
            raise MeasurementSourceError(f"refusing to overwrite {archive_dir / name}")
    partial = archive_dir / f".partial-{archive_name}"
    with tarfile.open(partial, "w:zst", level=ARCHIVE_LEVEL) as archive:
        for name in SOURCE_FILES:
            archive.add(directory / name, arcname=f"unit-{unit}/{name}")
    os.replace(partial, archive_dir / archive_name)
    record = {
        **record,
        "archive": {"name": archive_name, **_file_digest(archive_dir / archive_name)},
    }
    _write_new(
        archive_dir / record_name,
        json.dumps(record, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
    )
    return record


def restore_unit(
    unit: int,
    directory: Path,
    *,
    reuse_dir: Path,
    prefix: str,
    conditions: dict[str, object],
) -> dict[str, object] | None:
    """Re-check a completed unit of an earlier run and restore its source.

    Returns ``None`` when the unit was not completed.  Anything else that is not
    exactly the unit this run would generate stops the run.
    """
    archive_name, record_name = _archive_names(prefix, unit)
    archive_path, record_path = reuse_dir / archive_name, reuse_dir / record_name
    if not record_path.exists():
        if archive_path.exists():
            raise MeasurementSourceError(f"unit {unit}: archive without a unit record")
        return None
    if not archive_path.exists():
        raise MeasurementSourceError(f"unit {unit}: unit record without its archive")
    try:
        record = json.loads(record_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise MeasurementSourceError(f"unit {unit}: unreadable unit record") from error
    if type(record) is not dict:
        raise MeasurementSourceError(f"unit {unit}: unit record is not an object")
    splits = unit_splits(unit)
    seeds = [seed for name in dev.SPLITS for seed in splits[name]]
    expected = {
        "schema": UNIT_SCHEMA,
        "unit": unit,
        "splits": _split_bounds(splits),
        "hanchan": len(seeds),
        **conditions,
    }
    for field, value in expected.items():
        if record.get(field) != value:
            raise MeasurementSourceError(
                f"unit {unit}: unit record {field} differs from this run"
            )
    try:
        archive_digest = dict(record["archive"])
        files = {name: record["files"][name] for name in SOURCE_FILES}
        game_seeds = [game["seed"] for game in record["games"]]
        decisions = record["in_scope_decisions"]
    except (KeyError, TypeError, ValueError) as error:
        raise MeasurementSourceError(f"unit {unit}: malformed unit record") from error
    if archive_digest != {"name": archive_name, **_file_digest(archive_path)}:
        raise MeasurementSourceError(f"unit {unit}: archive differs from its record")
    if game_seeds != seeds:
        raise MeasurementSourceError(f"unit {unit}: unit record games differ")
    members = [f"unit-{unit}/{name}" for name in SOURCE_FILES]
    with tarfile.open(archive_path, "r:zst") as archive:
        entries = archive.getmembers()
        if [entry.name for entry in entries] != members or not all(
            entry.isfile() for entry in entries
        ):
            raise MeasurementSourceError(f"unit {unit}: unexpected archive members")
        archive.extractall(directory.parent, filter="data")
    for name in SOURCE_FILES:
        if _file_digest(directory / name) != files[name]:
            raise MeasurementSourceError(f"unit {unit}: {name} differs from its record")
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    if manifest["splits"] != splits or manifest["producer"] != conditions["producer"]:
        raise MeasurementSourceError(f"unit {unit}: manifest differs from this run")
    if dev.verify_source_coverage(directory) != decisions:
        raise MeasurementSourceError(f"unit {unit}: coverage differs from its record")
    return record


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
    output, archive_dir = arguments.output, arguments.archive_dir
    reuse_dir, prefix = arguments.reuse_dir, arguments.archive_prefix
    if output.exists():
        raise MeasurementSourceError(f"refusing to overwrite {output}")
    if reuse_dir is not None and reuse_dir.resolve() == archive_dir.resolve():
        raise MeasurementSourceError("--reuse-dir must not be the archive directory")
    producer = {
        "arena_revision": dev._arena_revision(),
        "lisjong_engine_revision": dev._revision("lisjong-engine"),
        "lisjong_revision": dev._revision("lisjong"),
        "policy": dev.POLICY,
    }
    if producer["arena_revision"] != record["arena_revision"]:
        raise MeasurementSourceError("the executing checkout is not clean")
    # What a unit of an earlier run must share with this run to be reused.
    conditions = {
        "owner_issue": OWNER_ISSUE,
        "protocol": PROTOCOL,
        "population": POPULATION,
        "allocation_identity": binding["allocation_identity"],
        "producer": producer,
        "shanten_backend": runtime["shanten_backend"],
        "workers": arguments.workers,
    }
    output.mkdir(parents=True)
    archive_dir.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    # Every completed unit of the earlier run is checked and restored before any
    # game is played: one bad unit stops the run without generating the others.
    restored: dict[int, dict[str, object]] = {}
    if reuse_dir is not None:
        for unit in range(UNIT_COUNT):
            unit_record = restore_unit(
                unit,
                output / f"unit-{unit}",
                reuse_dir=reuse_dir,
                prefix=prefix,
                conditions=conditions,
            )
            if unit_record is not None:
                restored[unit] = unit_record
    units = []
    for unit in range(UNIT_COUNT):
        directory = output / f"unit-{unit}"
        reused = unit in restored
        if reused:
            unit_record = restored[unit]
            for name in _archive_names(prefix, unit):
                shutil.copyfile(reuse_dir / name, archive_dir / f".partial-{name}")
                os.replace(archive_dir / f".partial-{name}", archive_dir / name)
        else:
            unit_record = generate_unit(
                unit,
                directory,
                workers=arguments.workers,
                producer=producer,
                play=play,
            )
            unit_record = publish_unit(
                {**unit_record, **conditions}, directory, archive_dir, prefix
            )
        units.append({**unit_record, "reused": reused})
        print(
            f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} unit {unit} "
            f"{'reused' if reused else 'generated'} ({unit_record['wall_seconds']}s)",
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
    _write_new(
        output / GENERATION_FILENAME,
        json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
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
    run_parser.add_argument("--archive-dir", type=Path, required=True)
    run_parser.add_argument("--archive-prefix", default="")
    run_parser.add_argument("--reuse-dir", type=Path)
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
