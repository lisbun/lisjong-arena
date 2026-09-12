"""Shared pre-execution safety checks for write-once Arena runs.

This module contains only generic, read-only checks. It does not choose an
experiment, allocate seeds, create output directories, or retry failed work.
Purpose-specific research modules and the portable strength runner can reuse
the same implementation without sharing their scientific contracts.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

_ARENA_SOURCE_DIRECTORY = Path(__file__).resolve().parent
_FULL_COMMIT_ID = re.compile(r"[0-9a-f]{40}\Z").fullmatch
_GIT_TIMEOUT_SECONDS = 30


class ExecutionSafetyError(ValueError):
    """A deterministic pre-execution safety check failed."""


def require_new_artifact_destinations(
    locations: Mapping[str, str | Path],
    *,
    required_names: Sequence[str],
    writable_check: Callable[[Path, int], bool] = os.access,
) -> None:
    """Validate prepared write-once destinations without creating targets."""
    if not isinstance(locations, Mapping):
        raise ExecutionSafetyError("locked artifact locations are invalid")
    if isinstance(required_names, (str, bytes, bytearray)):
        raise TypeError("required_names must be an ordered collection")
    names = tuple(required_names)
    if not names or any(type(name) is not str or not name for name in names):
        raise TypeError("required_names must contain non-empty strings")

    resolved_targets: dict[Path, str] = {}
    for name in names:
        value = locations.get(name)
        if not isinstance(value, (str, Path)) or not str(value) or "\x00" in str(value):
            raise ExecutionSafetyError(f"locked output {name} path is unusable")
        path = Path(value)
        try:
            resolved = path.resolve(strict=False)
        except (OSError, RuntimeError) as exc:
            raise ExecutionSafetyError(
                f"locked output {name} path cannot be resolved"
            ) from exc
        if resolved in resolved_targets:
            raise ExecutionSafetyError(
                f"locked outputs {resolved_targets[resolved]} and {name} "
                "refer to the same destination"
            )
        resolved_targets[resolved] = name
        if path.exists():
            raise ExecutionSafetyError(
                f"locked output {name} already exists; outputs are write-once"
            )
        parent = path.parent
        if not parent.exists():
            raise ExecutionSafetyError(
                f"locked output {name} parent directory does not exist"
            )
        if not parent.is_dir():
            raise ExecutionSafetyError(
                f"locked output {name} parent is not a directory"
            )
        if not writable_check(parent, os.W_OK):
            raise ExecutionSafetyError(
                f"locked output {name} parent directory is not writable"
            )


def _git_output(*arguments: str) -> str:
    try:
        completed = subprocess.run(
            ("git", "-C", str(_ARENA_SOURCE_DIRECTORY), *arguments),
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ExecutionSafetyError(
            "Arena execution target cannot be verified: "
            f"git {arguments[0]} could not be executed"
        ) from exc
    if completed.returncode != 0:
        raise ExecutionSafetyError(
            f"Arena execution target cannot be verified: git {arguments[0]} failed"
        )
    return completed.stdout


def require_clean_arena_head() -> str:
    """Return exact HEAD only when the complete Arena worktree is clean."""
    if _git_output("status", "--porcelain").strip():
        raise ExecutionSafetyError(
            "Arena worktree must be clean before the pre-execution lock or run"
        )
    revision = _git_output("rev-parse", "--verify", "HEAD^{commit}").strip()
    if _FULL_COMMIT_ID(revision) is None:
        raise ExecutionSafetyError("Arena HEAD is not a lowercase full commit ID")
    return revision


def require_merged_arena_revision(revision: str, *, branch: str = "main") -> str:
    """Return ``revision`` only when it is contained in a long-lived branch.

    A post-merge run must execute reviewed code, not an arbitrary local commit
    that happens to be checked out. Containment in the local ``branch`` ref is
    a read-only check, so a stale local branch fails closed and is fixed by
    fetching, never by relaxing the check.
    """
    if _FULL_COMMIT_ID(revision) is None:
        raise ExecutionSafetyError("Arena revision is not a lowercase full commit ID")
    if type(branch) is not str or not branch:
        raise ExecutionSafetyError("Arena containment branch is invalid")
    resolved = _git_output("rev-parse", "--verify", f"{branch}^{{commit}}").strip()
    if _FULL_COMMIT_ID(resolved) is None:
        raise ExecutionSafetyError(
            f"Arena branch {branch} does not resolve to a commit"
        )
    try:
        _git_output("merge-base", "--is-ancestor", revision, resolved)
    except ExecutionSafetyError as exc:
        raise ExecutionSafetyError(
            f"Arena revision {revision} is not contained in {branch}; "
            f"fetch {branch} or check out the merged revision"
        ) from exc
    return revision


__all__ = [
    "ExecutionSafetyError",
    "require_clean_arena_head",
    "require_merged_arena_revision",
    "require_new_artifact_destinations",
]
