"""lisjong#262 stage C ron-legal baseline population (lisjong-arena#460).

Generates 400 hanchan (seeds 936000..936399) as 400 independent per-seed ron
sources, each made by the unchanged #457 producer (``play`` /
``write_population`` / base coverage / ron reader / native label builder).  Neither
the 2-hanchan pilot CLI nor the #257 / #258 / #259 populations are touched.

- The seeds, the train / valid / eval split and the 32 workers are fixed by the
  protocol.  No argument changes them.  The manifest's ``test`` split is the
  lisjong#262 eval split.
- Generation requires a fresh RESERVED Seed Registry allocation whose owner issue,
  protocol, seed domain, population, split and seed membership are this
  population and whose ``arena_revision`` is the clean executing checkout.  Any
  mismatch stops the run before a game is played.
- Generation requires ``LISJONG_SHANTEN_BACKEND=rust``, the pinned runtime and the
  native scorer built from the installed lisjong.
- Each seed is played, written, re-read and archived by one worker process.  A
  worker returns only a small receipt; no journal returns to the parent, so the
  parent never holds the population in memory.
- There is no resume, reuse or partial adoption.  A failed or interrupted run,
  and any allocation that was not adopted completely, is RETIRED.

Output directory (new; an existing path is refused)::

    plan.json            execution plan, written before the first game
    seed-<seed>/         base/ and ron/ of one hanchan (kept for local checks)
    generation.json      written only after all 400 seeds were verified

Archive directory (``--archive-dir``; no ``progress-seed-*`` file may exist)::

    progress-seed-<seed>.tar.zst        the seed's base/ and ron/ files
    progress-seed-<seed>.complete.json  written last: the seed receipt, with the
                                        file digests and the archive's SHA-256

A seed is complete only when both archive files exist and agree.  Only
``generation.json`` says that all 400 hanchan exist; nothing may be measured
without it.  A receipt is not a statement about the population.

Usage::

    LISJONG_SHANTEN_BACKEND=rust python scripts/generate_ron_legal_baseline_460.py \\
        run --seed-ledger <live ledger> --allocation-identity <sha256> \\
        --workers 32 --output <new directory> --archive-dir <directory>
    python scripts/generate_ron_legal_baseline_460.py check-allocation \\
        --seed-ledger <live ledger> --allocation-identity <sha256> \\
        --arena-revision <full sha>
    python scripts/generate_ron_legal_baseline_460.py verify-collected \\
        --directory <collected output directory>

Generated data is not committed.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import platform
import sys
import sysconfig
import tarfile
import time
from collections import namedtuple
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

from lisjong_arena import measurement_allocation_guard as guard
from lisjong_arena import seed_registry

sys.path.insert(0, str(Path(__file__).resolve().parent))
import generate_ron_legal_source_457 as ron  # noqa: E402

base = ron.base

OWNER_ISSUE = "lisbun/lisjong#262"
PROTOCOL = "ron-legal-baseline-measurement-v1"
POPULATION = "ron-legal-baseline-measurement-400-hanchan"
SPLIT = "TRAIN160-VALID80-EVAL160"
MEASUREMENT_SEEDS = tuple(range(936000, 936400))
TRAIN_SEEDS = tuple(range(936000, 936160))
VALID_SEEDS = tuple(range(936160, 936240))
EVAL_SEEDS = tuple(range(936240, 936400))
REQUIRED_WORKERS = 32
USED_RANGES = (
    range(920000, 920544),  # L0.3 engine calibration / scientific
    range(931000, 932000),  # RETIRED #385 incl. every pilot
    range(932000, 932100),  # lisjong#245
    range(933000, 933400),  # lisjong#257 measurement
    range(934000, 934200),  # #258 / #259 formal evaluation candidates
    range(935000, 935002),  # #457 pilot
)
"""Engine-domain seeds allocated or planned before this population."""

SCORING_API_VERSION = 1
ARCHIVE_PREFIX = "progress-"
ARCHIVE_LEVEL = 10
GENERATION_FILENAME = "generation.json"
PLAN_FILENAME = "plan.json"
GENERATION_SCHEMA = "lisjong-arena-ron-legal-baseline-generation-v1"
PLAN_SCHEMA = "lisjong-arena-ron-legal-baseline-plan-v1"
RECEIPT_SCHEMA = "lisjong-arena-ron-legal-baseline-seed-receipt-v1"
SOURCE_FILES = (
    "base/manifest.json",
    "base/decisions.jsonl",
    "base/hand_facts.jsonl",
    "base/coverage.json",
    "ron/manifest.json",
    "ron/ron_facts.jsonl",
    "ron/ron_history.jsonl",
)


class BaselineSourceError(RuntimeError):
    """A precondition, generation or completeness check failed (STOP / INVALID)."""


Stages = namedtuple("Stages", "play write check runtime producer")
"""Every execution-dependent step; tests substitute them with fixtures."""


def seed_split(seed: int) -> str:
    """The manifest split name of a population seed (eval is ``test``)."""
    if seed in TRAIN_SEEDS:
        return "train"
    if seed in VALID_SEEDS:
        return "valid"
    if seed in EVAL_SEEDS:
        return "test"
    raise BaselineSourceError(f"seed {seed} is not in the population")


def seed_splits(seed: int) -> dict[str, list[int]]:
    name = seed_split(seed)
    return {key: [seed] if key == name else [] for key in base.SPLITS}


def allocation_preset() -> guard.AllocationPreset:
    """This population's fixed allocation preset; no argument changes it."""
    return guard.AllocationPreset(
        owner_issue=OWNER_ISSUE,
        protocol=PROTOCOL,
        seed_domain=seed_registry.LISJONG_ENGINE_HANCHAN_SEED_DOMAIN,
        population=POPULATION,
        split=SPLIT,
        seeds=MEASUREMENT_SEEDS,
        splits=(("train", TRAIN_SEEDS), ("valid", VALID_SEEDS), ("test", EVAL_SEEDS)),
        split_sizes=(160, 80, 160),
        used_ranges=USED_RANGES,
    )


@contextlib.contextmanager
def _guarded():
    """Report a shared allocation guard failure as this script's STOP."""
    try:
        yield
    except guard.AllocationGuardError as error:
        raise BaselineSourceError(str(error)) from error


def check_population() -> None:
    """The protocol's own seed population must be internally consistent."""
    with _guarded():
        guard.check_population(allocation_preset())


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
    with _guarded():
        return guard.authorize(
            ledger,
            allocation_identity,
            allocation_preset(),
            arena_revision=arena_revision,
        )


def check_runtime() -> dict[str, object]:
    """Verify and describe the executing runtime; a worker repeats this check."""
    if sys.version_info[:2] != (3, 14) or sysconfig.get_config_var("Py_GIL_DISABLED"):
        raise BaselineSourceError("generation requires normal (GIL) CPython 3.14")
    if os.environ.get("LISJONG_SHANTEN_BACKEND") != "rust":
        raise BaselineSourceError("generation requires LISJONG_SHANTEN_BACKEND=rust")
    from lisjong.belief.ron_legal_ground_truth import require_scoring_backend

    require_scoring_backend()
    import _lisjong_native

    if _lisjong_native.SOURCE_REVISION != base._revision("lisjong"):
        raise BaselineSourceError(
            "native scorer source revision differs from installed lisjong"
        )
    if getattr(_lisjong_native, "SCORING_API_VERSION", None) != SCORING_API_VERSION:
        raise BaselineSourceError("native scoring API version is not the fixed one")
    return {
        "python": sys.version,
        "platform": f"{platform.system()} {platform.machine()}",
        "shanten_backend": "rust",
        "native_source_revision": _lisjong_native.SOURCE_REVISION,
        "scoring_api_version": SCORING_API_VERSION,
    }


_file_digest = guard.file_digest
_write_new = guard.write_new


def _json(value: object) -> str:
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def archive_names(seed: int) -> tuple[str, str]:
    return (
        f"{ARCHIVE_PREFIX}seed-{seed}.tar.zst",
        f"{ARCHIVE_PREFIX}seed-{seed}.complete.json",
    )


def verify_seed_source(directory: Path) -> dict[str, int]:
    """Base coverage, ron reader and the native label builder over one seed."""
    from lisjong.learning.ron_legal_source import (
        read_labelled_ron_source,
        read_ron_source,
    )

    total = base.verify_source_coverage(directory / "base")
    source = read_ron_source(directory / "ron", base_directory=directory / "base")
    _, labelled = read_labelled_ron_source(
        directory / "ron", base_directory=directory / "base"
    )
    if not (total == len(source.decisions) == len(labelled)):
        raise BaselineSourceError("reader decision counts differ from base coverage")
    return {"decisions": total, "labelled_decisions": len(labelled)}


def current_producer() -> dict[str, str]:
    """The pinned Git dependencies and the clean executing Arena revision."""
    from lisjong_arena.environment_verify import verify_environment

    project = Path(__file__).resolve().parents[1] / "pyproject.toml"
    checked = verify_environment(project)
    if not checked.ok:
        raise BaselineSourceError(f"environment mismatch: {checked.errors}")
    return {
        "arena_revision": base._arena_revision(),
        "lisjong_revision": base._revision("lisjong"),
        "lisjong_engine_revision": base._revision("lisjong-engine"),
        "policy": base.POLICY,
    }


REAL_STAGES = Stages(
    ron.play, ron.write_population, verify_seed_source, check_runtime, current_producer
)


def generate_seed(task: dict[str, object], stages: Stages = REAL_STAGES):
    """Play, write, re-read and archive one hanchan; return its receipt.

    ``task`` is trusted: the parent derived it from the checked population.
    """
    seed, conditions = task["seed"], task["conditions"]
    started, cpu = time.perf_counter(), time.process_time()
    if stages.runtime() != conditions["runtime"]:
        raise BaselineSourceError(f"seed {seed}: worker runtime differs from the plan")
    game = stages.play(seed)
    if game[0][0] != seed:
        raise BaselineSourceError(f"seed {seed}: played a different seed")
    directory = Path(task["source_dir"])
    from lisjong_engine.rules import RuleSet

    manifest = stages.write(
        directory,
        [game],
        seed_splits(seed),
        conditions["producer"],
        ron.rules_projection(RuleSet.default()),
    )
    coverage = manifest["coverage"]
    del game
    counts = stages.check(directory)
    if not coverage or any(row["seed"] != seed for row in coverage):
        raise BaselineSourceError(f"seed {seed}: coverage rows are not this seed's")
    archive_dir = Path(task["archive_dir"])
    archive_name, receipt_name = archive_names(seed)
    for name in (archive_name, receipt_name):
        if (archive_dir / name).exists():
            raise BaselineSourceError(f"refusing to overwrite {archive_dir / name}")
    partial = archive_dir / f".partial-{archive_name}"
    with tarfile.open(partial, "w:zst", level=ARCHIVE_LEVEL) as archive:
        for relative in SOURCE_FILES:
            archive.add(directory / relative, arcname=f"seed-{seed}/{relative}")
    os.replace(partial, archive_dir / archive_name)
    receipt = {
        "schema": RECEIPT_SCHEMA,
        "seed": seed,
        "split": seed_split(seed),
        **conditions,
        **counts,
        "coverage": coverage,
        "files": {name: _file_digest(directory / name) for name in SOURCE_FILES},
        "wall_seconds": round(time.perf_counter() - started, 3),
        "cpu_seconds": round(time.process_time() - cpu, 3),
        "archive": {"name": archive_name, **_file_digest(archive_dir / archive_name)},
    }
    _write_new(archive_dir / receipt_name, _json(receipt))
    return {"seed": seed, "decisions": counts["decisions"]}


CONDITION_FIELDS = (
    "owner_issue",
    "protocol",
    "population",
    "allocation_identity",
    "producer",
    "runtime",
)


def check_receipt(receipt: object, seed: int, conditions: dict[str, object]) -> None:
    """One receipt must be exactly this population's seed under these conditions."""
    if type(receipt) is not dict or receipt.get("schema") != RECEIPT_SCHEMA:
        raise BaselineSourceError(f"seed {seed}: unsupported receipt")
    if receipt.get("seed") != seed or receipt.get("split") != seed_split(seed):
        raise BaselineSourceError(f"seed {seed}: receipt seed or split differs")
    for field in CONDITION_FIELDS:
        if receipt.get(field) != conditions[field]:
            raise BaselineSourceError(f"seed {seed}: receipt {field} differs")
    try:
        files = receipt["files"]
        archive = receipt["archive"]
        decisions = receipt["decisions"]
        labelled = receipt["labelled_decisions"]
        coverage = receipt["coverage"]
    except KeyError as error:
        raise BaselineSourceError(f"seed {seed}: malformed receipt") from error
    if sorted(files) != sorted(SOURCE_FILES):
        raise BaselineSourceError(f"seed {seed}: receipt files differ")
    if archive.get("name") != archive_names(seed)[0]:
        raise BaselineSourceError(f"seed {seed}: receipt archive name differs")
    if (
        type(decisions) is not int
        or decisions != labelled
        or sum(row["decisions"] for row in coverage) != decisions
        or any(row["seed"] != seed for row in coverage)
    ):
        raise BaselineSourceError(f"seed {seed}: receipt counters disagree")


def check_archive(path: Path, seed: int, receipt: dict[str, object]) -> None:
    """The archive must hold exactly the receipt's files with the recorded digests."""
    if {"name": path.name, **_file_digest(path)} != receipt["archive"]:
        raise BaselineSourceError(f"seed {seed}: archive differs from its receipt")
    members = [f"seed-{seed}/{relative}" for relative in SOURCE_FILES]
    with tarfile.open(path, "r:zst") as archive:
        entries = archive.getmembers()
        if [e.name for e in entries] != members or not all(e.isfile() for e in entries):
            raise BaselineSourceError(f"seed {seed}: unexpected archive members")
        for entry, relative in zip(entries, SOURCE_FILES, strict=True):
            digest, size = hashlib.sha256(), 0
            with archive.extractfile(entry) as stream:
                for block in iter(lambda: stream.read(1 << 20), b""):
                    digest.update(block)
                    size += len(block)
            if {"bytes": size, "sha256": digest.hexdigest()} != receipt["files"][
                relative
            ]:
                raise BaselineSourceError(f"seed {seed}: {relative} differs")


def verify_population(
    archive_dir: Path, conditions: dict[str, object]
) -> list[dict[str, object]]:
    """Read every seed back from disk; return the population's seed entries.

    All 400 receipt / archive pairs must exist exactly once and agree; a missing,
    extra, duplicated or mismatching one is a failure of the whole population.
    """
    check_population()
    expected = {name for seed in MEASUREMENT_SEEDS for name in archive_names(seed)}
    present = {
        p.name
        for p in archive_dir.iterdir()
        if p.name.startswith((ARCHIVE_PREFIX, ".partial-"))
    }
    if present != expected:
        missing, extra = sorted(expected - present), sorted(present - expected)
        raise BaselineSourceError(
            f"archive files differ: {len(missing)} missing, {len(extra)} unexpected"
        )
    entries = []
    for seed in MEASUREMENT_SEEDS:
        archive_name, receipt_name = archive_names(seed)
        receipt_path = archive_dir / receipt_name
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise BaselineSourceError(f"seed {seed}: unreadable receipt") from error
        check_receipt(receipt, seed, conditions)
        check_archive(archive_dir / archive_name, seed, receipt)
        entries.append(
            {
                "seed": seed,
                "split": receipt["split"],
                "decisions": receipt["decisions"],
                "reactions": sum(row["reactions"] for row in receipt["coverage"]),
                "rounds": len(receipt["coverage"]),
                "archive": receipt["archive"],
                "receipt": {"name": receipt_name, **_file_digest(receipt_path)},
            }
        )
    return entries


def _totals(entries: list[dict[str, object]]) -> dict[str, object]:
    return {
        "hanchan": len(entries),
        "by_split": {
            name: sum(e["split"] == name for e in entries) for name in base.SPLITS
        },
        "decisions": sum(e["decisions"] for e in entries),
        "rounds": sum(e["rounds"] for e in entries),
        "reactions": sum(e["reactions"] for e in entries),
    }


def _execute(tasks: list[dict[str, object]], workers: int, worker) -> None:
    """Run every task; the first failure cancels the rest and fails the run."""
    if workers == 1:
        for task in tasks:
            worker(task)
        return
    executor = ProcessPoolExecutor(workers)
    try:
        futures = [executor.submit(worker, task) for task in tasks]
        for future in as_completed(futures):
            future.result()
    except BaseException:
        executor.shutdown(wait=True, cancel_futures=True)
        raise
    executor.shutdown(wait=True)


class _StagedWorker:
    """Picklable worker bound to substituted stages (tests only)."""

    def __init__(self, stages: Stages) -> None:
        self.stages = stages

    def __call__(self, task):
        return generate_seed(task, self.stages)


def run(
    *,
    seed_ledger: Path,
    allocation_identity: str,
    workers: int,
    output: Path,
    archive_dir: Path,
    stages: Stages = REAL_STAGES,
) -> dict[str, object]:
    producer = stages.producer()
    runtime = stages.runtime()
    ledger = load_live_ledger(seed_ledger)
    binding, record = authorize(
        ledger, allocation_identity, arena_revision=producer["arena_revision"]
    )
    if output.exists():
        raise BaselineSourceError(f"refusing to overwrite {output}")
    if archive_dir.exists() and any(
        p.name.startswith((ARCHIVE_PREFIX, ".partial-")) for p in archive_dir.iterdir()
    ):
        raise BaselineSourceError("archive directory already holds seed files")
    conditions = {
        "owner_issue": OWNER_ISSUE,
        "protocol": PROTOCOL,
        "population": POPULATION,
        "allocation_identity": binding["allocation_identity"],
        "producer": producer,
        "runtime": runtime,
    }
    output.mkdir(parents=True)
    archive_dir.mkdir(parents=True, exist_ok=True)
    plan = {
        "schema": PLAN_SCHEMA,
        **conditions,
        "split": SPLIT,
        "context_protocol": ron.CONTEXT,
        "seeds": {"first": 936000, "last": 936399, "count": len(MEASUREMENT_SEEDS)},
        "splits": {
            "train": [TRAIN_SEEDS[0], TRAIN_SEEDS[-1]],
            "valid": [VALID_SEEDS[0], VALID_SEEDS[-1]],
            "test": [EVAL_SEEDS[0], EVAL_SEEDS[-1]],
        },
        "workers": workers,
        "allocation_binding": binding,
        "ledger_revision": seed_registry.ledger_revision(ledger),
    }
    plan_text = _json(plan)
    _write_new(output / PLAN_FILENAME, plan_text)
    print(json.dumps(plan, sort_keys=True), flush=True)
    started = time.perf_counter()
    tasks = [
        {
            "seed": seed,
            "conditions": conditions,
            "source_dir": str(output / f"seed-{seed}"),
            "archive_dir": str(archive_dir),
        }
        for seed in MEASUREMENT_SEEDS
    ]
    _execute(tasks, workers, _StagedWorker(stages))
    # Nothing a worker returned is trusted: every seed is read back from disk.
    entries = verify_population(archive_dir, conditions)
    document = {
        "schema": GENERATION_SCHEMA,
        "owner_issue": OWNER_ISSUE,
        "protocol": PROTOCOL,
        "population": POPULATION,
        "split": SPLIT,
        "seeds": plan["seeds"],
        "workers": workers,
        "wall_seconds": round(time.perf_counter() - started, 3),
        "producer": producer,
        "runtime": runtime,
        "allocation_binding": binding,
        "allocation": record,
        "ledger_revision": plan["ledger_revision"],
        "plan_sha256": hashlib.sha256(plan_text.encode("utf-8")).hexdigest(),
        "totals": _totals(entries),
        "entries": entries,
    }
    _write_new(output / GENERATION_FILENAME, _json(document))
    return document


def verify_collected(directory: Path) -> dict[str, object]:
    """Re-check a collected output directory against its generation record."""
    try:
        document = json.loads((directory / GENERATION_FILENAME).read_text("utf-8"))
        conditions = {
            "owner_issue": document["owner_issue"],
            "protocol": document["protocol"],
            "population": document["population"],
            "allocation_identity": document["allocation_binding"][
                "allocation_identity"
            ],
            "producer": document["producer"],
            "runtime": document["runtime"],
        }
        recorded_entries = document["entries"]
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise BaselineSourceError("generation.json is missing or malformed") from error
    if (
        document.get("schema") != GENERATION_SCHEMA
        or conditions["owner_issue"] != OWNER_ISSUE
        or conditions["protocol"] != PROTOCOL
        or conditions["population"] != POPULATION
        or document.get("split") != SPLIT
    ):
        raise BaselineSourceError("generation.json is not this population's record")
    entries = verify_population(directory, conditions)
    if entries != recorded_entries or document.get("totals") != _totals(entries):
        raise BaselineSourceError("generation.json differs from the archive files")
    return {
        "totals": document["totals"],
        "allocation": conditions["allocation_identity"],
    }


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
    verify = commands.add_parser(
        "verify-collected", help="re-check a collected output directory"
    )
    verify.add_argument("--directory", type=Path, required=True)
    return parser


def main(argv=None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        if arguments.command == "check-allocation":
            result = check_allocation(
                arguments.seed_ledger,
                arguments.allocation_identity,
                arguments.arena_revision,
            )
        elif arguments.command == "verify-collected":
            result = verify_collected(arguments.directory)
        else:
            if arguments.workers != REQUIRED_WORKERS:
                raise BaselineSourceError(f"workers must be {REQUIRED_WORKERS}")
            document = run(
                seed_ledger=arguments.seed_ledger,
                allocation_identity=arguments.allocation_identity,
                workers=arguments.workers,
                output=arguments.output,
                archive_dir=arguments.archive_dir,
            )
            result = {key: document[key] for key in ("seeds", "totals", "wall_seconds")}
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except (BaselineSourceError, ron.RonProducerError, base._E) as error:
        print(f"STOP / INVALID: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
