"""lisjong#258 / #259 shared formal-test population (lisjong-arena#464).

Generates the 200-hanchan, test-only HandBelief source (lisjong#256 v1) that the
formal tests of lisjong#258 and lisjong#259 share, as two complete v1 sources of
100 hanchan each.  It reuses ``_play`` / ``write_source`` /
``verify_source_coverage`` of the pilot producer
``generate_hand_belief_source_255.py`` unchanged; the pilot's seed guard and the
lisjong#257 / lisjong#262 populations are neither lifted nor touched.

- The seeds (934000..934199), the split (every seed is ``test``; train and
  valid are empty), the two units and the 32 workers are fixed by the protocol.
  No argument changes them.
- Generation requires a fresh RESERVED Seed Registry allocation whose owner
  issue, protocol, seed domain, population, split and seed membership are this
  population and whose ``arena_revision`` is the clean executing checkout
  (``lisjong_arena.measurement_allocation_guard``).  Any mismatch stops the run
  before a game is played.
- Generation requires ``LISJONG_SHANTEN_BACKEND=rust`` and the pinned runtime.
- There is no resume, reuse or partial adoption.  A failed run leaves no
  ``generation.json``; its files are diagnostics only.

Producer identity.  The manifest records the revisions that really ran: this
Arena checkout and its pinned lisjong / lisjong-engine.  They are NOT the
lisjong#257 producer (Arena 07554c2, lisjong e6346ed, lisjong-engine 8735e89)
that the lisjong#258 / #259 selections registered, so lisjong's
``check_producer`` rejects this population until lisjong registers this
producer for the formal test.  ``compare-reference`` gives the evidence for that
decision: it replays already used lisjong#257 seeds with the current producer
and compares the rows, byte for byte, with the lisjong#257 source.

Output directory (new; an existing path is refused)::

    unit-0, unit-1/      one v1 source each (manifest / decisions / hand_facts /
                         coverage)
    generation.json      written only when both units are complete

Archive directory (``--archive-dir``; no ``progress-unit-*`` file may exist)::

    progress-unit-<k>.tar.zst        the unit's source files
    progress-unit-<k>.complete.json  written last: the unit record, with the
                                     file digests and the archive's SHA-256

Only ``generation.json`` says that all 200 hanchan exist; nothing may be
evaluated without it.

Usage::

    LISJONG_SHANTEN_BACKEND=rust python scripts/generate_hand_belief_formal_test_464.py \\
        run --seed-ledger <live ledger> --allocation-identity <sha256> \\
        --workers 32 --output <new directory> --archive-dir <directory>
    python scripts/generate_hand_belief_formal_test_464.py check-allocation \\
        --seed-ledger <live ledger> --allocation-identity <sha256> \\
        --arena-revision <full sha>
    python scripts/generate_hand_belief_formal_test_464.py compare-reference \\
        --reference <lisjong#257 unit directory> [--reference ...] \\
        --seeds 933000..933002 --output <new report.json> [--workers N]

Generated data is not committed.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import sys
import tarfile
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from lisjong_arena import measurement_allocation_guard as guard
from lisjong_arena import seed_registry

sys.path.insert(0, str(Path(__file__).resolve().parent))
import generate_hand_belief_source_255 as dev  # noqa: E402

OWNER_ISSUE = "lisbun/lisjong#258"
"""The allocation's owner; lisjong#259 shares the population (lisjong#258 comment
6047898044)."""
PROTOCOL = "hand-belief-formal-test-258-259-v1"
POPULATION = "hand-belief-formal-test-258-259-200-hanchan"
SPLIT = "TEST200"
TEST_SEEDS = tuple(range(934000, 934200))
UNIT_COUNT = 2
UNIT_SIZE = 100
REQUIRED_WORKERS = 32
USED_RANGES = (
    range(920000, 920544),  # L0.3 engine calibration / scientific
    range(931000, 932000),  # RETIRED #385 incl. every pilot
    range(932000, 932100),  # lisjong#245
    range(933000, 933400),  # lisjong#257 measurement
    range(935000, 935002),  # #457 pilot
    range(936000, 936400),  # lisjong#262 ron-legal baseline (#460)
)
"""Engine-domain seeds allocated before this population."""

REFERENCE_SEEDS = range(933000, 933400)
"""The already used lisjong#257 population: the only seeds ``compare-reference``
replays.  It never plays a seed of this population."""

ARCHIVE_PREFIX = "progress-"
ARCHIVE_LEVEL = 10
GENERATION_FILENAME = "generation.json"
GENERATION_SCHEMA = "lisjong-arena-hand-belief-formal-test-generation-v1"
UNIT_SCHEMA = "lisjong-arena-hand-belief-formal-test-unit-v1"
COMPARISON_SCHEMA = "lisjong-arena-hand-belief-producer-comparison-v1"
SOURCE_FILES = ("manifest.json", "decisions.jsonl", "hand_facts.jsonl", "coverage.json")
ROW_FILES = (("decisions", "decisions.jsonl", 1), ("hand_facts", "hand_facts.jsonl", 2))
"""(manifest name, file name, index of the rows in a played game)."""


class FormalTestSourceError(RuntimeError):
    """A precondition, generation or completeness check failed (STOP / INVALID)."""


def unit_splits(unit: int) -> dict[str, list[int]]:
    """Seeds of one unit: 100 consecutive ``test`` seeds, no train / valid."""
    if unit not in range(UNIT_COUNT):
        raise FormalTestSourceError(f"unit must be 0..{UNIT_COUNT - 1}")
    return {
        "train": [],
        "valid": [],
        "test": list(TEST_SEEDS[unit * UNIT_SIZE : (unit + 1) * UNIT_SIZE]),
    }


def allocation_preset() -> guard.AllocationPreset:
    """This population's fixed allocation preset; no argument changes it."""
    return guard.AllocationPreset(
        owner_issue=OWNER_ISSUE,
        protocol=PROTOCOL,
        seed_domain=seed_registry.LISJONG_ENGINE_HANCHAN_SEED_DOMAIN,
        population=POPULATION,
        split=SPLIT,
        seeds=TEST_SEEDS,
        splits=(("train", ()), ("valid", ()), ("test", TEST_SEEDS)),
        split_sizes=(0, 0, 200),
        used_ranges=USED_RANGES,
    )


@contextlib.contextmanager
def _guarded():
    """Report a shared allocation guard failure as this script's STOP."""
    try:
        yield
    except guard.AllocationGuardError as error:
        raise FormalTestSourceError(str(error)) from error


def check_population() -> None:
    """The protocol's own seed population must be internally consistent."""
    with _guarded():
        guard.check_population(allocation_preset())
    covered = [seed for unit in range(UNIT_COUNT) for seed in unit_splits(unit)["test"]]
    if covered != list(TEST_SEEDS):
        raise FormalTestSourceError("units must cover every seed exactly once")
    if any(seed in REFERENCE_SEEDS for seed in TEST_SEEDS):
        raise FormalTestSourceError("test seeds overlap the reference seeds")


def load_live_ledger(path: Path) -> dict[str, object]:
    with _guarded():
        return guard.load_live_ledger(path)


def authorize(
    ledger: object, allocation_identity: str, *, arena_revision: str
) -> tuple[dict[str, object], dict[str, object]]:
    """Resolve the allocation against the live ledger; fail closed on any mismatch.

    Only a fresh RESERVED allocation of the clean executing revision is accepted.
    Returns ``(binding, allocation record)``.
    """
    check_population()
    with _guarded():
        return guard.authorize(
            ledger,
            allocation_identity,
            allocation_preset(),
            arena_revision=arena_revision,
        )


def current_runtime() -> dict[str, object]:
    """The committed checkout and the pinned, installed runtime (fails closed)."""
    from lisjong_arena.policy_source_record import binding as runtime_identity

    project = Path(__file__).resolve().parents[1] / "pyproject.toml"
    return runtime_identity.runtime_binding(project)


def current_producer() -> dict[str, str]:
    """The manifest ``producer``: the revisions that really run."""
    return {
        "arena_revision": dev._arena_revision(),
        "lisjong_engine_revision": dev._revision("lisjong-engine"),
        "lisjong_revision": dev._revision("lisjong"),
        "policy": dev.POLICY,
    }


def _play_one(seed: int):
    """One hanchan plus its own wall / CPU time, measured inside the worker."""
    started, cpu = time.perf_counter(), time.process_time()
    game = dev._play(seed)
    stats = {
        "wall_seconds": time.perf_counter() - started,
        "cpu_seconds": time.process_time() - cpu,
    }
    return game, stats


def _play_all(seeds: list[int], workers: int, play) -> list:
    if workers == 1:
        return [play(seed) for seed in seeds]
    with ProcessPoolExecutor(workers) as executor:
        return list(executor.map(play, seeds))


def _json(value: object) -> str:
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def archive_names(unit: int) -> tuple[str, str]:
    return (
        f"{ARCHIVE_PREFIX}unit-{unit}.tar.zst",
        f"{ARCHIVE_PREFIX}unit-{unit}.complete.json",
    )


def generate_unit(
    unit: int, directory: Path, *, workers: int, producer: dict[str, str], play
) -> dict[str, object]:
    """Play, write and re-check one unit; return its generation record."""
    splits = unit_splits(unit)
    seeds = splits["test"]
    started = time.perf_counter()
    results = _play_all(seeds, workers, play)
    games = [game for game, _ in results]
    if [game[0] for game in games] != seeds:
        raise FormalTestSourceError(f"unit {unit}: games differ from the unit seeds")
    manifest = dev.write_source(
        directory, splits=splits, games=games, producer=producer
    )
    total = dev.verify_source_coverage(directory)
    if (
        manifest["splits"] != splits
        or manifest["producer"] != producer
        or total != manifest["files"]["decisions"]["rows"]
    ):
        raise FormalTestSourceError(f"unit {unit}: written source is inconsistent")
    return {
        "schema": UNIT_SCHEMA,
        "unit": unit,
        "seeds": [seeds[0], seeds[-1]],
        "hanchan": len(seeds),
        "in_scope_decisions": total,
        "wall_seconds": round(time.perf_counter() - started, 3),
        "files": {name: guard.file_digest(directory / name) for name in SOURCE_FILES},
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
    record: dict[str, object], directory: Path, archive_dir: Path
) -> dict[str, object]:
    """Put one complete unit into the archive directory; the record goes last."""
    unit = record["unit"]
    archive_name, record_name = archive_names(unit)
    for name in (archive_name, record_name):
        if (archive_dir / name).exists():
            raise FormalTestSourceError(f"refusing to overwrite {archive_dir / name}")
    partial = archive_dir / f".partial-{archive_name}"
    with tarfile.open(partial, "w:zst", level=ARCHIVE_LEVEL) as archive:
        for name in SOURCE_FILES:
            archive.add(directory / name, arcname=f"unit-{unit}/{name}")
    os.replace(partial, archive_dir / archive_name)
    record = {
        **record,
        "archive": {
            "name": archive_name,
            **guard.file_digest(archive_dir / archive_name),
        },
    }
    guard.write_new(archive_dir / record_name, _json(record))
    return record


def run(
    *,
    seed_ledger: Path,
    allocation_identity: str,
    workers: int,
    output: Path,
    archive_dir: Path,
    play=_play_one,
) -> dict[str, object]:
    runtime = current_runtime()
    if runtime["shanten_backend"]["name"] != "rust":
        raise FormalTestSourceError(
            "formal-test generation requires LISJONG_SHANTEN_BACKEND=rust"
        )
    producer = current_producer()
    ledger = load_live_ledger(seed_ledger)
    binding, record = authorize(
        ledger, allocation_identity, arena_revision=producer["arena_revision"]
    )
    if runtime["arena_revision"] != producer["arena_revision"]:
        raise FormalTestSourceError("the executing checkout is not clean")
    if output.exists():
        raise FormalTestSourceError(f"refusing to overwrite {output}")
    if archive_dir.exists() and any(
        p.name.startswith((f"{ARCHIVE_PREFIX}unit-", ".partial-"))
        for p in archive_dir.iterdir()
    ):
        raise FormalTestSourceError("archive directory already holds unit files")
    conditions = {
        "owner_issue": OWNER_ISSUE,
        "protocol": PROTOCOL,
        "population": POPULATION,
        "allocation_identity": binding["allocation_identity"],
        "producer": producer,
        "shanten_backend": runtime["shanten_backend"],
        "workers": workers,
    }
    output.mkdir(parents=True)
    archive_dir.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    units = []
    for unit in range(UNIT_COUNT):
        directory = output / f"unit-{unit}"
        unit_record = generate_unit(
            unit, directory, workers=workers, producer=producer, play=play
        )
        unit_record = publish_unit(
            {**unit_record, **conditions}, directory, archive_dir
        )
        units.append(unit_record)
        print(
            f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} unit {unit} "
            f"generated ({unit_record['wall_seconds']}s)",
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
            "first": TEST_SEEDS[0],
            "last": TEST_SEEDS[-1],
            "count": len(TEST_SEEDS),
        },
        "splits": {"train": [], "valid": [], "test": [TEST_SEEDS[0], TEST_SEEDS[-1]]},
        "workers": workers,
        "wall_seconds": round(time.perf_counter() - started, 3),
        "producer": producer,
        "runtime": runtime,
        "allocation_binding": binding,
        "allocation": record,
        "ledger_revision": seed_registry.ledger_revision(ledger),
        "units": units,
    }
    guard.write_new(output / GENERATION_FILENAME, _json(document))
    return document


def check_allocation(
    seed_ledger: Path, allocation_identity: str, arena_revision: str
) -> dict[str, object]:
    ledger = load_live_ledger(seed_ledger)
    binding, record = authorize(
        ledger, allocation_identity, arena_revision=arena_revision
    )
    return {
        "allocation_binding": binding,
        "allocation": record,
        "ledger_revision": seed_registry.ledger_revision(ledger),
    }


def _rows_digest(lines: list[bytes]) -> dict[str, object]:
    return {"rows": len(lines), "sha256": hashlib.sha256(b"".join(lines)).hexdigest()}


def read_reference(
    directories: list[Path], seeds: list[int]
) -> tuple[dict[str, str], dict[int, dict[str, dict[str, object]]]]:
    """The reference producer and, per requested seed, its row digests.

    Every reference file must be the one its manifest records, all references
    must share one producer, and each seed must be in exactly one of them.
    """
    producers, found = [], {}
    for directory in directories:
        try:
            manifest = json.loads((directory / "manifest.json").read_text("utf-8"))
            producer, splits, files = (
                manifest["producer"],
                manifest["splits"],
                manifest["files"],
            )
            members = {seed for name in dev.SPLITS for seed in splits[name]}
            expected = {
                name: {"bytes": files[name]["bytes"], "sha256": files[name]["sha256"]}
                for name, _, _ in ROW_FILES
            }
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise FormalTestSourceError(
                f"{directory}: unreadable reference manifest"
            ) from error
        producers.append(producer)
        wanted = [seed for seed in seeds if seed in members]
        if not wanted:
            raise FormalTestSourceError(f"{directory}: holds none of the seeds")
        lines = {seed: {name: [] for name, _, _ in ROW_FILES} for seed in wanted}
        for name, filename, _ in ROW_FILES:
            if guard.file_digest(directory / filename) != expected[name]:
                raise FormalTestSourceError(
                    f"{directory}: {filename} differs from its manifest"
                )
            with open(directory / filename, "rb") as stream:
                for line in stream:
                    seed = json.loads(line)["key"]["seed"]
                    if seed in lines:
                        lines[seed][name].append(line)
        for seed in wanted:
            if seed in found:
                raise FormalTestSourceError(f"seed {seed} is in two references")
            found[seed] = {
                name: _rows_digest(lines[seed][name]) for name, _, _ in ROW_FILES
            }
    if sorted(found) != sorted(seeds):
        raise FormalTestSourceError("a seed is in none of the references")
    if any(producer != producers[0] for producer in producers):
        raise FormalTestSourceError("the references were made by different producers")
    return producers[0], found


def compare_reference(
    *,
    references: list[Path],
    seeds: list[int],
    workers: int,
    output: Path,
    play,
    runtime,
    producer,
) -> dict[str, object]:
    """Replay used lisjong#257 seeds and compare the rows with the reference.

    The comparison is of bytes only: no label is computed and nothing is
    evaluated.  ``identical`` is a statement about these seeds, not a proof for
    every seed.
    """
    check_population()
    if (
        not seeds
        or len(set(seeds)) != len(seeds)
        or any(type(seed) is not int or seed not in REFERENCE_SEEDS for seed in seeds)
    ):
        raise FormalTestSourceError(
            "comparison seeds must be distinct seeds of the used lisjong#257 "
            "population 933000..933399"
        )
    if output.exists():
        raise FormalTestSourceError(f"refusing to overwrite {output}")
    runtime_record, current = runtime(), producer()
    reference_producer, reference = read_reference(references, seeds)
    results = _play_all(seeds, workers, play)
    rows, identical = [], True
    for seed, (game, _) in zip(seeds, results, strict=True):
        if game[0] != seed:
            raise FormalTestSourceError(f"seed {seed}: played a different seed")
        dev.verify_coverage(seed, game[3], game[1], game[2])
        regenerated = {
            name: _rows_digest(
                [dev.canonical_line(row).encode("utf-8") for row in game[index]]
            )
            for name, _, index in ROW_FILES
        }
        same = regenerated == reference[seed]
        identical = identical and same
        rows.append(
            {
                "seed": seed,
                "identical": same,
                "reference": reference[seed],
                "regenerated": regenerated,
            }
        )
    document = {
        "schema": COMPARISON_SCHEMA,
        "identical": identical,
        "reference_producer": reference_producer,
        "current_producer": current,
        "differing_producer_fields": sorted(
            name
            for name in set(reference_producer) | set(current)
            if reference_producer.get(name) != current.get(name)
        ),
        "runtime": runtime_record,
        "seeds": rows,
    }
    guard.write_new(output, _json(document))
    return document


def _seed_list(text: str) -> list[int]:
    first, _, last = text.partition("..")
    try:
        return list(range(int(first), int(last or first) + 1))
    except ValueError as error:
        raise argparse.ArgumentTypeError("seeds must be <first>[..<last>]") from error


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    run_parser = commands.add_parser("run")
    run_parser.add_argument("--workers", type=int, required=True)
    run_parser.add_argument("--output", type=Path, required=True)
    run_parser.add_argument("--archive-dir", type=Path, required=True)
    check = commands.add_parser(
        "check-allocation", help="no-game authorization check of an allocation"
    )
    check.add_argument("--arena-revision", required=True)
    for command in (run_parser, check):
        command.add_argument("--seed-ledger", type=Path, required=True)
        command.add_argument("--allocation-identity", required=True)
    compare = commands.add_parser(
        "compare-reference",
        help="replay used lisjong#257 seeds and compare with the lisjong#257 source",
    )
    compare.add_argument("--reference", type=Path, action="append", required=True)
    compare.add_argument("--seeds", type=_seed_list, required=True)
    compare.add_argument("--workers", type=int, default=1)
    compare.add_argument("--output", type=Path, required=True)
    return parser


def main(argv=None) -> int:
    from lisjong_arena.policy_source_record import PolicySourceRecordError

    arguments = build_parser().parse_args(argv)
    try:
        if arguments.command == "check-allocation":
            result = check_allocation(
                arguments.seed_ledger,
                arguments.allocation_identity,
                arguments.arena_revision,
            )
        elif arguments.command == "compare-reference":
            document = compare_reference(
                references=arguments.reference,
                seeds=arguments.seeds,
                workers=arguments.workers,
                output=arguments.output,
                play=_play_one,
                runtime=current_runtime,
                producer=current_producer,
            )
            result = {
                key: document[key]
                for key in ("identical", "differing_producer_fields", "seeds")
            }
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0 if document["identical"] else 1
        else:
            if arguments.workers != REQUIRED_WORKERS:
                raise FormalTestSourceError(f"workers must be {REQUIRED_WORKERS}")
            document = run(
                seed_ledger=arguments.seed_ledger,
                allocation_identity=arguments.allocation_identity,
                workers=arguments.workers,
                output=arguments.output,
                archive_dir=arguments.archive_dir,
            )
            result = {key: document[key] for key in ("seeds", "wall_seconds")}
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except (
        FormalTestSourceError,
        dev.HandBeliefSourceProducerError,
        PolicySourceRecordError,
    ) as error:
        print(f"STOP / INVALID: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
