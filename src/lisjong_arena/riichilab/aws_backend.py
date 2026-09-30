"""AWS bot-process backend verification before entering either live runner (#434).

The bootstrap probes before fetching credentials; each real bot process repeats
the check here and then runs the CLI in this same interpreter (including the
optional viewer). The compact, allowlisted receipt survives instance collection.
"""

from __future__ import annotations

import argparse
import json
import os
import runpy
import sys
from pathlib import Path

from lisjong_arena.shanten_backend_verification.backend import (
    EXPECTED_LISJONG_REVISION,
    EXPECTED_NATIVE_API_VERSION,
    EXPECTED_WHEEL_SHA256,
    ShantenBackendVerificationError,
    require_shanten_backend,
    verify_installed_native,
)

EVIDENCE_FILENAME = "backend.json"
_FIELDS = {
    "schema_version",
    "backend",
    "lisjong_revision",
    "native_api_version",
    "wheel_sha256",
    "r5_probe_calls",
    "pid",
}


def probe_backend(backend: str, wheel: Path | None) -> dict[str, object]:
    if (backend == "rust") != (wheel is not None):
        raise ShantenBackendVerificationError(
            "rust requires --wheel; python forbids it"
        )
    identity = require_shanten_backend(backend)
    record = {
        "schema_version": 1,
        "backend": backend,
        "lisjong_revision": identity["lisjong_revision"],
        "native_api_version": None,
        "wheel_sha256": None,
        "r5_probe_calls": 0,
        "pid": os.getpid(),
    }
    if backend == "rust":
        installed = verify_installed_native(wheel)
        # Use lisjong's real production factory, not a direct extension call.
        # This tiny structural input tests wiring, not strength or latency.
        from lisjong.policies.terminal_shanten_progression_mechanism_riichi_defense import (
            _new_progression_evaluator,
        )

        native = sys.modules["_lisjong_native"]
        before = native.progression_evaluation_call_count()
        hand = (1,) * 13 + (0,) * 21
        remaining = (0,) * 13 + (3,) + (0,) * 20
        _new_progression_evaluator().evaluate_roots((hand,), remaining, 3)
        calls = native.progression_evaluation_call_count() - before
        if calls < 1:
            raise ShantenBackendVerificationError("R5 factory did not reach native")
        record.update(
            native_api_version=identity["native"]["api_version"],
            wheel_sha256=installed["sha256"],
            r5_probe_calls=calls,
        )
    return record


def read_backend_evidence(path: Path, expected: str) -> dict[str, object]:
    """Validate only bounded known fields; never echo untrusted file content."""
    try:
        if path.stat().st_size > 2048:
            raise ValueError
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or set(value) != _FIELDS:
            raise ValueError
        if expected not in ("python", "rust") or value["backend"] != expected:
            raise ValueError
        if type(value["schema_version"]) is not int or value["schema_version"] != 1:
            raise ValueError
        if value["lisjong_revision"] != EXPECTED_LISJONG_REVISION:
            raise ValueError
        if type(value["pid"]) is not int or not 0 < value["pid"] < 2**31:
            raise ValueError
        calls = value["r5_probe_calls"]
        if type(calls) is not int or not 0 <= calls < 2**31:
            raise ValueError
        if expected == "rust":
            if (
                type(value["native_api_version"]) is not int
                or value["native_api_version"] != EXPECTED_NATIVE_API_VERSION
                or value["wheel_sha256"] != EXPECTED_WHEEL_SHA256
                or calls < 1
            ):
                raise ValueError
        elif (
            value["native_api_version"] is not None
            or value["wheel_sha256"] is not None
            or calls != 0
        ):
            raise ValueError
        return value
    except OSError, ValueError, TypeError:
        raise ShantenBackendVerificationError(
            "missing or invalid bot backend evidence"
        ) from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", required=True, choices=("python", "rust"))
    parser.add_argument("--wheel", type=Path)
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--runner", choices=("continuous", "spectate"))
    parser.add_argument("--profile")
    parser.add_argument("runner_args", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.runner and (args.profile is None or args.evidence is None):
        parser.error("--runner requires --profile and --evidence")
    record = probe_backend(args.backend, args.wheel)
    if args.evidence is not None:
        with args.evidence.open("x", encoding="utf-8") as handle:
            json.dump(record, handle, sort_keys=True)
            handle.write("\n")
    if args.runner is None:
        print(json.dumps(record, sort_keys=True))
        return 0
    module = (
        "lisjong_play.riichilab_html"
        if args.runner == "spectate"
        else "lisjong_arena.riichilab.continuous_ranked"
    )
    forwarded = args.runner_args
    if forwarded[:1] == ["--"]:
        forwarded = forwarded[1:]
    sys.argv = [module, "--profile", args.profile, *forwarded]
    runpy.run_module(module, run_name="__main__")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
