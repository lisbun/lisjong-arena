"""Formal test-source generator for lisbun/lisjong#245 (lisbun/lisjong-arena#447).

Plays the same Champion self-play hanchan as the S1 development generator
(``generate_riichi_deal_in_source_237.py``) and writes the same wire shape, but
for the *formal* test population of the #245 wait-probability estimator.  Arena
records observed facts only; the label meaning and the ``select`` / ``test``
procedure stay in lisjong.  The development generator and its RETIRED-seed guard
are not changed or bypassed: its ``_play`` / ``write_source`` are reused as is.

Two modes share the same generation and completeness checks:

``formal``
    Seeds are fixed by the protocol (``FORMAL_SEEDS``, 932000..932099).  The run
    requires a Seed Registry allocation (RESERVED / COMMITTED) whose owner issue,
    protocol, seed domain, population, split and exact membership match, and whose
    ``arena_revision`` is the executing checkout.  The manifest has
    ``train=[]``, ``valid=[]`` and ``test`` = the 100 seeds.
``smoke``
    Path check on development seeds only (931100..931139, already consumed by the
    #237 path check).  No registry binding; formal seeds are refused.

Generation is all-or-nothing: the output is written only after every seed has
returned exactly once, so a failed or interrupted run leaves no partial source.

Output directory (new; existing paths are refused)::

    source/manifest.json      lisjong-riichi-deal-in-source-manifest-v1
    source/decisions.jsonl    player-safe
    source/label_facts.jsonl  training-only
    generation.json           allocation binding, runtime identity, per-game timings

Usage::

    LISJONG_SHANTEN_BACKEND=rust python scripts/generate_riichi_wait_formal_source_245.py \\
        formal --seed-ledger <live ledger> --allocation-identity <sha256> \\
        --workers 32 --output <new directory>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from concurrent.futures import FIRST_EXCEPTION, ProcessPoolExecutor, wait
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import generate_riichi_deal_in_source_237 as dev  # noqa: E402

OWNER_ISSUE = "lisbun/lisjong#245"
PROTOCOL = "riichi-wait-estimator-formal-test-v1"
POPULATION = "riichi-wait-formal-test"
SPLIT = "TEST"
FORMAL_SEEDS = tuple(range(932000, 932100))
SMOKE_FIRST = 931100
SMOKE_LAST = 931139
S1_SEEDS = frozenset(range(931200, 931400))
"""S1 train / valid / test.  The formal test must not reuse any of them."""
DEVELOPMENT_RANGE = range(931000, 932000)
"""RETIRED lisjong-arena#385 population; development seeds, never formal."""

GENERATION_SCHEMA = "lisjong-arena-riichi-wait-formal-generation-v1"
GENERATION_FILENAME = "generation.json"
SOURCE_DIRECTORY = "source"
MODES = ("formal", "smoke")
_SHA256 = re.compile(r"\A[0-9a-f]{64}\Z")


class FormalSourceError(RuntimeError):
    """A precondition, generation or completeness check failed (STOP / INVALID)."""


def _seed_domain() -> str:
    from lisjong_arena import seed_registry

    return seed_registry.LISJONG_ENGINE_HANCHAN_SEED_DOMAIN


def check_formal_population() -> None:
    """The protocol's own seed population must be internally consistent."""
    seeds = FORMAL_SEEDS
    if len(seeds) != 100 or len(set(seeds)) != 100:
        raise FormalSourceError("formal population must be 100 distinct seeds")
    if seeds[0] != 932000 or seeds[-1] != 932099:
        raise FormalSourceError("formal population must be 932000..932099")
    overlap = {seed for seed in seeds if seed in S1_SEEDS or seed in DEVELOPMENT_RANGE}
    if overlap:
        raise FormalSourceError("formal seeds overlap S1 or development seeds")


def authorize(
    ledger: object, allocation_identity: str, *, arena_revision: str
) -> tuple[dict[str, object], dict[str, object]]:
    """Resolve the allocation against the live ledger; fail closed on any mismatch.

    Returns ``(binding, allocation record)``.
    """
    from lisjong_arena import seed_registry

    check_formal_population()
    if type(allocation_identity) is not str or not _SHA256.fullmatch(
        allocation_identity
    ):
        raise FormalSourceError("allocation identity must be SHA-256 hex")
    try:
        binding = seed_registry.allocation_binding(ledger, allocation_identity)
        record = seed_registry.require_allocation_binding(
            ledger,
            binding,
            seeds=FORMAL_SEEDS,
            owner_issue=OWNER_ISSUE,
            protocol=PROTOCOL,
            seed_domain=_seed_domain(),
            population=POPULATION,
            split=SPLIT,
        )
    except seed_registry.SeedRegistryError as error:
        raise FormalSourceError(f"allocation is not authorized: {error}") from error
    if record["arena_revision"] != arena_revision:
        raise FormalSourceError(
            f"allocation arena_revision {record['arena_revision']} is not the "
            f"executing checkout {arena_revision}"
        )
    return binding, record


def load_live_ledger(path: Path) -> dict[str, object]:
    """Read the live ledger snapshot.

    The snapshot is validated in full and identified by its canonical-form digest,
    so a line-ending change made while transporting the file does not matter.
    """
    from lisjong_arena import seed_registry

    try:
        return seed_registry.load_ledger(path, strict_serialization=False)
    except seed_registry.SeedRegistryError as error:
        raise FormalSourceError(f"invalid live ledger: {error}") from error


def smoke_seeds(spec: str) -> tuple[int, ...]:
    first, _, last = spec.partition("..")
    try:
        seeds = tuple(range(int(first), int(last or first) + 1))
    except ValueError as error:
        raise FormalSourceError(f"invalid seed range {spec!r}") from error
    if not seeds or seeds[0] < SMOKE_FIRST or seeds[-1] > SMOKE_LAST:
        raise FormalSourceError(
            f"smoke seeds must stay inside the development range "
            f"{SMOKE_FIRST}..{SMOKE_LAST}"
        )
    return seeds


def _play_one(seed: int) -> tuple[int, list[dict], list[dict], dict[str, float]]:
    import resource  # POSIX only; check-allocation also runs on Windows

    started = time.perf_counter()
    cpu = resource.getrusage(resource.RUSAGE_SELF)
    seed_, decisions, facts = dev._play(seed)
    after = resource.getrusage(resource.RUSAGE_SELF)
    return (
        seed_,
        decisions,
        facts,
        {
            "wall_seconds": time.perf_counter() - started,
            "cpu_seconds": (after.ru_utime - cpu.ru_utime)
            + (after.ru_stime - cpu.ru_stime),
            "maxrss_kb": after.ru_maxrss,
        },
    )


def _check_game(seed: int, decisions: list[dict], facts: list[dict]) -> None:
    for name, rows in (("decision", decisions), ("label fact", facts)):
        if any(row["key"]["seed"] != seed for row in rows):
            raise FormalSourceError(f"seed {seed}: a {name} row has another seed")
    keys = [json.dumps(row["key"], sort_keys=True) for row in decisions]
    if len(set(keys)) != len(keys):
        raise FormalSourceError(f"seed {seed}: duplicate decision keys")
    if keys != [json.dumps(row["key"], sort_keys=True) for row in facts]:
        raise FormalSourceError(f"seed {seed}: decisions and label facts differ")


def generate_games(
    seeds: tuple[int, ...],
    workers: int,
    *,
    play=_play_one,
    progress=lambda message: None,
) -> list[tuple[int, list[dict], list[dict], dict[str, float]]]:
    """Run every seed once.  All-or-nothing: any failure raises and returns nothing."""
    if workers < 1:
        raise FormalSourceError("workers must be at least 1")
    if not seeds or len(set(seeds)) != len(seeds):
        raise FormalSourceError("seeds must be non-empty and distinct")
    started = time.perf_counter()
    games = []
    if workers == 1:
        for seed in seeds:
            games.append(play(seed))
            progress(f"generation {len(games)}/{len(seeds)} seed {seed}")
    else:
        pool = ProcessPoolExecutor(max_workers=workers)
        try:
            pending = {pool.submit(play, seed): seed for seed in seeds}
            while pending:
                done, _ = wait(pending, return_when=FIRST_EXCEPTION)
                for future in done:
                    seed = pending.pop(future)
                    error = future.exception()
                    if error is not None:
                        raise FormalSourceError(
                            f"seed {seed} failed: {error!r}"
                        ) from error
                    games.append(future.result())
                    progress(
                        f"generation {len(games)}/{len(seeds)} seed {seed} "
                        f"({time.perf_counter() - started:.1f}s)"
                    )
        finally:
            # A failed hanchan stops the run: pending hanchan are not started.
            pool.shutdown(wait=True, cancel_futures=True)
    returned = sorted(game[0] for game in games)
    if returned != sorted(seeds):
        raise FormalSourceError(
            "returned seeds differ from the requested seeds "
            f"(missing {sorted(set(seeds) - set(returned))}, "
            f"unexpected {sorted(set(returned) - set(seeds))}, "
            f"repeated {sorted({s for s in returned if returned.count(s) > 1})})"
        )
    for seed, decisions, facts, _ in games:
        _check_game(seed, decisions, facts)
    return sorted(games, key=lambda game: game[0])


def _file_digest(path: Path) -> dict[str, object]:
    data = path.read_bytes()
    return {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def write_source(
    output: Path,
    seeds: tuple[int, ...],
    games: list[tuple[int, list[dict], list[dict], dict[str, float]]],
    producer: dict[str, str],
) -> dict[str, object]:
    """Write ``output/source`` (formal split layout) and read it back.

    The manifest has ``train=[]``, ``valid=[]`` and ``test`` = ``seeds``.
    Existing paths are refused.  Returns the digests of the written files.
    """
    output.mkdir(parents=False, exist_ok=False)
    source = output / SOURCE_DIRECTORY
    manifest = dev.write_source(
        source,
        splits={"train": [], "valid": [], "test": list(seeds)},
        games=[(seed, decisions, facts) for seed, decisions, facts, _ in games],
        producer=producer,
    )
    digests = {
        name: _file_digest(source / name)
        for name in ("manifest.json", "decisions.jsonl", "label_facts.jsonl")
    }
    # Independent readback of what is on disk.
    written = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    if written != manifest or written["splits"] != {
        "train": [],
        "valid": [],
        "test": list(seeds),
    }:
        raise FormalSourceError("written manifest differs from the requested splits")
    allowed = set(seeds)
    for name, key in (
        ("decisions", "decisions.jsonl"),
        ("label_facts", "label_facts.jsonl"),
    ):
        entry = written["files"][name]
        if (entry["bytes"], entry["sha256"]) != (
            digests[key]["bytes"],
            digests[key]["sha256"],
        ):
            raise FormalSourceError(f"{key} does not match the manifest digest")
        lines = (source / key).read_text(encoding="utf-8").splitlines()
        if len(lines) != entry["rows"]:
            raise FormalSourceError(f"{key} row count differs from the manifest")
        if any(json.loads(line)["key"]["seed"] not in allowed for line in lines):
            raise FormalSourceError(f"{key} has a row outside the requested seeds")
    return digests


def generation_record(
    *,
    mode: str,
    seeds: tuple[int, ...],
    workers: int,
    wall_seconds: float,
    games,
    digests: dict[str, object],
    runtime: dict[str, object],
    producer: dict[str, str],
    binding: dict[str, object] | None,
    record: dict[str, object] | None,
    ledger_revision: str | None,
) -> dict[str, object]:
    document: dict[str, object] = {
        "schema": GENERATION_SCHEMA,
        "mode": mode,
        "owner_issue": OWNER_ISSUE,
        "protocol": PROTOCOL,
        "population": POPULATION,
        "split": SPLIT,
        "seeds": {"first": seeds[0], "last": seeds[-1], "count": len(seeds)},
        "workers": workers,
        "wall_seconds": round(wall_seconds, 3),
        "producer": producer,
        "runtime": runtime,
        "files": digests,
        "games": [
            {
                "seed": seed,
                "decisions": len(decisions),
                "wall_seconds": round(stats["wall_seconds"], 3),
                "cpu_seconds": round(stats["cpu_seconds"], 3),
                "maxrss_kb": int(stats["maxrss_kb"]),
            }
            for seed, decisions, _, stats in games
        ],
    }
    if mode == "formal":
        assert binding is not None and record is not None
        document["allocation_binding"] = binding
        document["allocation"] = {
            name: record[name]
            for name in (
                "allocation_identity",
                "arena_revision",
                "owner_issue",
                "population",
                "protocol",
                "protocol_revision",
                "seed_domain",
                "seed_membership",
                "seed_membership_identity",
                "split",
                "state",
            )
        }
        document["ledger_revision"] = ledger_revision
    return document


def _write_new(path: Path, document: object) -> None:
    if path.exists():
        raise FileExistsError(f"refusing to overwrite: {path}")
    path.write_text(
        json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def run(arguments: argparse.Namespace, *, play=_play_one) -> dict[str, object]:
    from lisjong_arena import seed_registry
    from lisjong_arena.policy_source_record import binding as runtime_identity

    project = Path(__file__).resolve().parents[1] / "pyproject.toml"
    runtime = runtime_identity.runtime_binding(project)
    binding = record = ledger_revision = None
    if arguments.mode == "formal":
        if runtime["shanten_backend"]["name"] != "rust":
            raise FormalSourceError(
                "formal generation requires LISJONG_SHANTEN_BACKEND=rust"
            )
        ledger = load_live_ledger(arguments.seed_ledger)
        binding, record = authorize(
            ledger,
            arguments.allocation_identity,
            arena_revision=str(runtime["arena_revision"]),
        )
        ledger_revision = seed_registry.ledger_revision(ledger)
        seeds = FORMAL_SEEDS
    else:
        seeds = smoke_seeds(arguments.seeds)
    if arguments.output.exists():
        raise FormalSourceError(f"refusing to overwrite {arguments.output}")

    progress_path = arguments.progress

    def progress(message: str) -> None:
        line = f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {message}"
        print(line, flush=True)
        if progress_path is not None:
            with open(progress_path, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()

    progress(f"start mode={arguments.mode} seeds={seeds[0]}..{seeds[-1]}")
    started = time.perf_counter()
    games = generate_games(seeds, arguments.workers, play=play, progress=progress)
    wall_seconds = time.perf_counter() - started
    dependencies = runtime["dependencies"]
    producer = {
        "arena_revision": str(runtime["arena_revision"]),
        "lisjong_engine_revision": str(dependencies["lisjong-engine"]),
        "lisjong_revision": str(dependencies["lisjong"]),
        "policy": dev.POLICY,
    }
    digests = write_source(arguments.output, seeds, games, producer)
    document = generation_record(
        mode=arguments.mode,
        seeds=seeds,
        workers=arguments.workers,
        wall_seconds=wall_seconds,
        games=games,
        digests=digests,
        runtime=runtime,
        producer=producer,
        binding=binding,
        record=record,
        ledger_revision=ledger_revision,
    )
    _write_new(arguments.output / GENERATION_FILENAME, document)
    progress(f"done {len(games)} hanchan in {wall_seconds:.1f}s")
    return document


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="mode", required=True)
    formal = commands.add_parser("formal")
    formal.add_argument("--seed-ledger", type=Path, required=True)
    formal.add_argument("--allocation-identity", required=True)
    check = commands.add_parser(
        "check-allocation", help="no-game authorization check of an allocation"
    )
    check.add_argument("--seed-ledger", type=Path, required=True)
    check.add_argument("--allocation-identity", required=True)
    check.add_argument("--arena-revision", required=True)
    smoke = commands.add_parser("smoke")
    smoke.add_argument("--seeds", required=True)
    for command in (formal, smoke):
        command.add_argument("--workers", type=int, required=True)
        command.add_argument("--output", type=Path, required=True)
        command.add_argument("--progress", type=Path)
    return parser


def check_allocation(arguments: argparse.Namespace) -> dict[str, object]:
    from lisjong_arena import seed_registry

    ledger = load_live_ledger(arguments.seed_ledger)
    binding, record = authorize(
        ledger, arguments.allocation_identity, arena_revision=arguments.arena_revision
    )
    return {
        "allocation_binding": binding,
        "state": record["state"],
        "ledger_revision": seed_registry.ledger_revision(ledger),
        "seeds": {"first": FORMAL_SEEDS[0], "last": FORMAL_SEEDS[-1]},
    }


def main(argv=None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        if arguments.mode == "check-allocation":
            print(json.dumps(check_allocation(arguments), indent=2, sort_keys=True))
            return 0
        run(arguments)
    except FormalSourceError as error:
        print(f"STOP / INVALID: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
