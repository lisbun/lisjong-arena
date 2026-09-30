"""Frozen #436 Rust execution contract for the one-shanten defense candidate.

Each spawned worker verifies its loaded native files before executing a seed's
four AABB games. The parent receives evidence with every completed seed block;
no partial comparison is returned on any worker or identity failure.
"""

from __future__ import annotations

from pathlib import PurePosixPath, PureWindowsPath

from lisjong_arena._artifact_io import expect_int, expect_object
from lisjong_arena.shanten_backend_verification.backend import (
    require_shanten_backend,
    verify_installed_native,
)
from lisjong_arena.single_round_artifact import (
    collect_execution_provenance,
    execution_provenance_to_dict,
    parse_execution_provenance,
)

from .lock import HeuristicCandidateLockError

REVISION = "e6346ed2bb9e992138c05c4be367bd6a05ed00bc"
WHEEL_SHA256 = "b14e53fea4161cb81c7912ead2c9d1206c2b95a1b19eb4999c6c2b7a056fdbe6"
WHEEL_FILENAME = "lisjong_native-0.1.0-cp314-cp314-manylinux_2_28_x86_64.whl"
PAIR = (
    (
        "one-shanten-defense-placement-aware-speed-call",
        "create_one_shanten_defense_placement_aware_speed_call",
    ),
    ("placement-aware-speed-call", "create_placement_aware_speed_call"),
)


def require_pair(candidate, incumbent) -> None:
    for participant, (identity, factory) in zip(
        (candidate, incumbent), PAIR, strict=True
    ):
        if (
            participant.policy_identity,
            participant.factory_binding,
            participant.implementation_source,
            participant.implementation_revision,
        ) != (identity, f"lisjong_arena.policy_catalog:{factory}", "lisjong", REVISION):
            raise HeuristicCandidateLockError(
                "event 436 participant differs from the frozen candidate/Champion pair"
            )


def execution_contract(wheel_path: str) -> dict[str, object]:
    return {
        "backend": "rust",
        "lisjong_revision": REVISION,
        "native_api_version": 3,
        "source_revision": REVISION,
        "wheel_filename": WHEEL_FILENAME,
        "wheel_sha256": WHEEL_SHA256,
        "wheel_path": wheel_path,
        "candidate_configuration": "no arguments; one-shanten mechanism defense",
        "executor": "spawn-seed-block-rust-436-v1",
    }


def require_contract(value: object) -> dict[str, object]:
    raw = expect_object(value, set(execution_contract("")), "rust_execution")
    # This is a recorded execution path, not a path on the verifying host.
    # Keep its bytes unchanged so the frozen contract/identity still matches.
    path = raw["wheel_path"]
    if not isinstance(path, str) or not any(
        parsed.is_absolute() and parsed.name == WHEEL_FILENAME
        for parsed in (PurePosixPath(path), PureWindowsPath(path))
    ):
        raise HeuristicCandidateLockError(
            "event 436 requires an absolute frozen wheel path"
        )
    if raw != execution_contract(path):
        raise HeuristicCandidateLockError("event 436 Rust execution contract drifted")
    return dict(raw)


def require_provenance(value: object) -> None:
    provenance = parse_execution_provenance(value)
    if (
        provenance.lisjong_revision,
        provenance.lisjong_engine_revision,
        provenance.riichienv_version,
    ) != (REVISION, "8735e89e1aea000ab59368d0368d476787827741", "0.4.10"):
        raise HeuristicCandidateLockError("event 436 dependency provenance drifted")


def verify_process(wheel_path: str) -> dict[str, object]:
    backend = require_shanten_backend("rust", expected_revision=REVISION)
    wheel = verify_installed_native(wheel_path)
    if wheel["sha256"] != WHEEL_SHA256 or wheel["file"] != WHEEL_FILENAME:
        raise HeuristicCandidateLockError("event 436 wheel identity drifted")
    record = {
        "backend": backend,
        "wheel": wheel,
        "provenance": execution_provenance_to_dict(collect_execution_provenance()),
    }
    _require_process(record)
    return record


def _require_process(value: object) -> dict[str, object]:
    raw = expect_object(value, {"backend", "wheel", "provenance"}, "process")
    require_provenance(raw["provenance"])
    backend = expect_object(
        raw["backend"], {"backend", "lisjong_revision", "pid", "native"}, "backend"
    )
    native = expect_object(
        backend["native"],
        {"module_file", "source_revision", "api_version", "probe_native_calls"},
        "native",
    )
    wheel = expect_object(
        raw["wheel"],
        {"file", "sha256", "bytes", "installed_directory", "files"},
        "wheel",
    )
    if (
        backend["backend"],
        backend["lisjong_revision"],
        native["source_revision"],
        native["api_version"],
        wheel["file"],
        wheel["sha256"],
    ) != ("rust", REVISION, REVISION, 3, WHEEL_FILENAME, WHEEL_SHA256):
        raise HeuristicCandidateLockError("Rust process identity mismatch")
    for name, value in (
        ("pid", backend["pid"]),
        ("probe_native_calls", native["probe_native_calls"]),
        ("bytes", wheel["bytes"]),
    ):
        if expect_int(value, name) <= 0:
            raise HeuristicCandidateLockError(f"{name} must be positive")
    if not isinstance(wheel["files"], list) or not wheel["files"]:
        raise HeuristicCandidateLockError("installed wheel file evidence is missing")
    for path in (native["module_file"], wheel["installed_directory"], *wheel["files"]):
        if not isinstance(path, str) or not path:
            raise HeuristicCandidateLockError("native file path evidence is missing")
    return dict(raw)


def require_evidence(lock, value, rows):
    from .rust423 import require_evidence as shared_require_evidence

    return shared_require_evidence(lock, value, rows, event=436)


def execute_rust(plan, *, lock, progress_callback=None):
    from .rust423 import execute_rust as shared_execute_rust

    return shared_execute_rust(
        plan, lock=lock, progress_callback=progress_callback, event=436
    )
