"""Allocation guard shared by measurement-source generation scripts.

lisbun/lisjong-arena#463 (decision: #461).  A measurement-source script may
play a game only for a population that the live Seed Registry ledger reserved
for exactly that purpose.  This module holds the checks every such script
repeats: reading the live ledger, the population skeleton check and the
allocation authorization.

Every population value comes from the calling purpose's fixed
``AllocationPreset``.  Nothing here has a default, and a caller must not build
a preset from arguments: the seeds, splits, owner issue, protocol and
population are protocol constants of that purpose.  Any disagreement with the
live ledger fails closed before a game is played.

Worker granularity, receipts, archives, ``generation.json``, reuse policy,
producers and Policies stay with each purpose's script.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path

from lisjong_arena import seed_registry

_SHA256 = re.compile(r"\A[0-9a-f]{64}\Z")


class AllocationGuardError(RuntimeError):
    """The population or its live allocation is not what the preset fixes."""


@dataclass(frozen=True)
class AllocationPreset:
    """One purpose's fixed population and the allocation it must own.

    ``splits`` lists ``(name, seeds)`` in population order and ``split_sizes``
    the fixed length of each; ``used_ranges`` are the seeds of the same domain
    allocated or planned before this population.
    """

    owner_issue: str
    protocol: str
    seed_domain: str
    population: str
    split: str
    seeds: tuple[int, ...]
    splits: tuple[tuple[str, tuple[int, ...]], ...]
    split_sizes: tuple[int, ...]
    used_ranges: tuple[range, ...]


def check_population(preset: AllocationPreset) -> None:
    """The preset's own seed population must be internally consistent."""
    seeds = preset.seeds
    if (
        type(seeds) is not tuple
        or not seeds
        or any(type(seed) is not int for seed in seeds)
        or len(set(seeds)) != len(seeds)
    ):
        raise AllocationGuardError("population seeds must be distinct integers")
    names = [name for name, _ in preset.splits]
    if not names or len(set(names)) != len(names):
        raise AllocationGuardError("split names must be present and distinct")
    if sum((tuple(members) for _, members in preset.splits), ()) != seeds:
        raise AllocationGuardError("splits must partition the population in order")
    if tuple(len(members) for _, members in preset.splits) != preset.split_sizes:
        raise AllocationGuardError(
            f"split sizes must be {'/'.join(map(str, preset.split_sizes))}"
        )
    if any(seed in used for seed in seeds for used in preset.used_ranges):
        raise AllocationGuardError("population seeds overlap already used seeds")


def load_live_ledger(path: Path) -> dict[str, object]:
    try:
        return seed_registry.load_ledger(path, strict_serialization=False)
    except seed_registry.SeedRegistryError as error:
        raise AllocationGuardError(f"invalid live ledger: {error}") from error


def authorize(
    ledger: object,
    allocation_identity: str,
    preset: AllocationPreset,
    *,
    arena_revision: str,
) -> tuple[dict[str, object], dict[str, object]]:
    """Resolve the allocation against the live ledger; fail closed on any mismatch.

    Only a fresh RESERVED allocation of the clean executing revision is accepted.
    Returns ``(binding, allocation record)``.
    """
    check_population(preset)
    if type(allocation_identity) is not str or not _SHA256.fullmatch(
        allocation_identity
    ):
        raise AllocationGuardError("allocation identity must be SHA-256 hex")
    try:
        binding = seed_registry.allocation_binding(ledger, allocation_identity)
        record = seed_registry.require_allocation_binding(
            ledger,
            binding,
            seeds=preset.seeds,
            owner_issue=preset.owner_issue,
            protocol=preset.protocol,
            seed_domain=preset.seed_domain,
            population=preset.population,
            split=preset.split,
        )
    except seed_registry.SeedRegistryError as error:
        raise AllocationGuardError(f"allocation is not authorized: {error}") from error
    if record["state"] != seed_registry.RESERVED:
        raise AllocationGuardError("generation requires a fresh RESERVED allocation")
    if (
        type(arena_revision) is not str
        or "-dirty" in arena_revision
        or record["arena_revision"] != arena_revision
    ):
        raise AllocationGuardError(
            f"allocation arena_revision {record['arena_revision']} is not the "
            f"clean executing checkout {arena_revision}"
        )
    return binding, record


def file_digest(path: Path) -> dict[str, object]:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return {"bytes": path.stat().st_size, "sha256": digest.hexdigest()}


def write_new(path: Path, text: str) -> None:
    """Write ``path`` so that it never exists with partial content."""
    partial = path.with_name(f".partial-{path.name}")
    with open(partial, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(partial, path)
