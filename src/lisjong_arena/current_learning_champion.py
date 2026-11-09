"""Exact serving factory for the #582-qualified Learning Champion (project #127).

The full L2a (M1+R1) composite, including its decision classifier and guards,
is loaded by the existing `lisjong.learning.l2a_policy` strict loader. This
module does not reconstruct, retrain or modify the evaluated Policy.

The two artifact directories are operator-owned and MUST be explicit. Neither
the current working directory nor an unrelated "latest" checkpoint is searched.
This factory creates a fresh Policy per seat; the immutable runtime is cached
once per process, like the historical Arm Y serving binding.

This alone does not make the existing Overall protocol v1's single-checkpoint
binding compatible with a two-artifact L2a participant. That is tracked by #583.
"""

from __future__ import annotations

import hashlib
import os
from functools import cache
from pathlib import Path

DISCARD_ARTIFACT_ENV = "LISJONG_ARENA_LEARNING_CHAMPION_M1_ARTIFACT"
REACTION_ARTIFACT_ENV = "LISJONG_ARENA_LEARNING_CHAMPION_R1_ARTIFACT"

# Exact frozen component artifact identities in lisjong #368 / Arena #582.
DISCARD_IDENTITY = "c976ed4a5cb667c406eb57f5cc79c128b1a221fb9b478f0dd017fcf48bde6e03"
REACTION_IDENTITY = "d565713f51a2b9f9cbdd2146bf01cb4bfb13ff859b639de962c74562eaf01efc"
POLICY_IDENTITY = f"lisjong-l2a:{DISCARD_IDENTITY}:{REACTION_IDENTITY}"


class LearningChampionBindingError(ValueError):
    """The configured artifacts do not match the evaluated L2a composite."""


def _artifact_path(variable: str) -> Path:
    raw = os.environ.get(variable)
    if raw is None or not raw.strip():
        raise LearningChampionBindingError(
            f"{variable} must point to the exact #582 artifact directory"
        )
    path = Path(raw)
    if not path.is_absolute():
        raise LearningChampionBindingError(f"{variable} must be an absolute path")
    return path


@cache
def _runtime():
    """Strict-load the exact pair once per process, without heuristic fallback."""
    from lisjong.learning.l2a_policy import load_l2a_policy_factory

    from lisjong_arena.learning_candidate_engine_aabb.runtime import (
        configure_ml_runtime,
    )

    discard_path = _artifact_path(DISCARD_ARTIFACT_ENV)
    reaction_path = _artifact_path(REACTION_ARTIFACT_ENV)
    if discard_path == reaction_path:
        raise LearningChampionBindingError(
            "Learning Champion M1 and R1 artifact paths must differ"
        )

    # The established Learning formal serving uses these deterministic CPU
    # conditions. This call does not change any evaluated action rule.
    configure_ml_runtime()
    runtime = load_l2a_policy_factory(discard_path, reaction_path)
    if tuple(runtime.identity) != (DISCARD_IDENTITY, REACTION_IDENTITY):
        raise LearningChampionBindingError(
            "the loaded L2a artifact identities do not match Arena #582"
        )
    return runtime


def current_learning_champion_runtime():
    """Return the same strict-loaded runtime that the factory actually serves."""
    return _runtime()


def create_current_learning_champion_policy():
    """Top-level, Windows-spawn-safe factory; fresh policy per seat/game."""
    return _runtime().create_policy()


def _file_sha256(path: Path) -> str:
    """Re-hash the served bytes at each formal pre/post-execution gate."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def served_current_learning_champion_artifacts() -> dict[str, dict[str, str]]:
    """Attest BOTH served M1/R1 files from the actual strict-loaded runtime.

    This is an exact top-level binding for Overall #583. It never searches for
    a checkpoint and re-hashes the files on every call (including the post-run
    check), rather than trusting caller-supplied digests or cached file bytes.
    Paths are intentionally excluded from the portable formal identity.
    """
    runtime = current_learning_champion_runtime()
    bound = (
        ("discard", DISCARD_ARTIFACT_ENV, runtime.discard, DISCARD_IDENTITY),
        ("reaction", REACTION_ARTIFACT_ENV, runtime.reaction, REACTION_IDENTITY),
    )
    result: dict[str, dict[str, str]] = {}
    for role, env_var, scorer, identity in bound:
        if scorer.artifact.identity != identity:
            raise LearningChampionBindingError(
                f"served {role} identity differs from the frozen #582 artifact"
            )
        root = _artifact_path(env_var)
        manifest = root / "manifest.json"
        weights = root / "weights.f32"
        manifest_digest = _file_sha256(manifest)
        weights_digest = _file_sha256(weights)
        if scorer.artifact.manifest["files"]["weights.f32"]["sha256"] != weights_digest:
            raise LearningChampionBindingError(
                f"served {role} weights changed after strict load"
            )
        result[role] = {
            "identity": identity,
            "manifest_sha256": manifest_digest,
            "weights_sha256": weights_digest,
        }
    return result


__all__ = [
    "DISCARD_ARTIFACT_ENV",
    "REACTION_ARTIFACT_ENV",
    "DISCARD_IDENTITY",
    "REACTION_IDENTITY",
    "POLICY_IDENTITY",
    "LearningChampionBindingError",
    "current_learning_champion_runtime",
    "served_current_learning_champion_artifacts",
    "create_current_learning_champion_policy",
]
