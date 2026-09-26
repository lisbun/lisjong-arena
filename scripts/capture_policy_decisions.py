"""Capture per-seat Policy decision sequences from one LocalGameRunner game.

Development-only utility for lisbun/lisjong-arena#398 (lisbun/lisjong#213).
It runs one fixed-seed RiichiEnv game with the same Policy in every seat, wraps
each seat's Policy so that every ``choose_action()`` call appends
``(DecisionContext, InternalAction)`` to one list, and pickles that list.
lisjong replays such lists to compare its internal computation backends
without depending on Arena.

```text
# capture (Policy imported from an explicit lisjong checkout)
PYTHONPATH=<lisjong checkout>/src python scripts/capture_policy_decisions.py \\
    capture --policy lisjong.policies:TwoStepUkeirePolicy \\
    --seed 0 --game-mode 4p-red-half --output <new file>.pickle

# recompute the digests of an existing capture
python scripts/capture_policy_decisions.py digest <file>.pickle
```

``--policy`` accepts a curated ``POLICY_CATALOG`` id or an importable
``module:Class`` reference (constructed without arguments).

``capture`` writes ``<output>`` and ``<output>.manifest.json`` (Arena /
lisjong source revisions, RiichiEnv version, Policy, seed, game mode, result,
elapsed time, and both digests).

Two digests are reported:

- ``bytes_sha256`` identifies one concrete pickle file.  It is not stable across
  runs: the recording order of several seats inside one step and pickle
  memoization can differ even when every decision is identical.
- ``semantic_sha256`` hashes, seat by seat in decision order,
  ``repr((decision, action))``.  Use it to check that a regenerated capture
  has the same content as an original one.

Constraints:

- Only unpickle files you produced yourself (pickle can execute code).
- This is not a strength evaluation, a durable game record, or a training
  dataset.  It does not allocate seeds; choose a seed that the Seed Registry
  rules allow for non-scientific development runs.
- Sequential, single process.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.metadata
import json
import os
import pathlib
import pickle
import subprocess
import sys
import time


def _policy_factory(reference: str):
    if ":" in reference:
        module_name, _, class_name = reference.partition(":")
        return getattr(importlib.import_module(module_name), class_name)
    from lisjong_arena.policy_catalog import POLICY_CATALOG

    return POLICY_CATALOG[reference].factory


class _RecordingPolicy:
    """Delegate to the wrapped Policy and record its input and output."""

    def __init__(self, inner, sink: list) -> None:
        self._inner = inner
        self._sink = sink

    def choose_action(self, decision):
        action = self._inner.choose_action(decision)
        self._sink.append((decision, action))
        return action


def semantic_digest(records) -> str:
    """Seat-by-seat digest that ignores the within-step recording order."""
    by_seat: dict[int, list[str]] = {}
    for decision, action in records:
        by_seat.setdefault(int(decision.input.self_seat), []).append(
            repr((decision, action))
        )
    digest = hashlib.sha256()
    for seat in sorted(by_seat):
        for line in by_seat[seat]:
            digest.update(f"{seat}\t{line}\n".encode("utf-8"))
    return digest.hexdigest()


def _source_identity(module) -> dict[str, object]:
    path = pathlib.Path(module.__file__).resolve()
    identity: dict[str, object] = {"module_file": str(path)}
    try:
        head = subprocess.run(
            ["git", "-C", str(path.parent), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "-C", str(path.parent), "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except OSError, subprocess.CalledProcessError:
        identity["git_revision"] = None
        return identity
    identity["git_revision"] = head
    identity["git_dirty"] = bool(status.strip())
    return identity


def _capture(arguments: argparse.Namespace) -> int:
    import lisjong
    from lisjong.policy_contract.seat import Seat

    import lisjong_arena
    from lisjong_arena.riichienv.local_game_runner import LocalGameRunner

    output = pathlib.Path(arguments.output)
    manifest_path = output.with_name(output.name + ".manifest.json")
    if output.exists() or manifest_path.exists():
        raise SystemExit(f"refusing to overwrite {output} or {manifest_path}")

    factory = _policy_factory(arguments.policy)
    records: list = []
    policies = {seat: _RecordingPolicy(factory(), records) for seat in Seat}
    started = time.perf_counter()
    result = LocalGameRunner(
        policies, seed=arguments.seed, game_mode=arguments.game_mode
    ).run()
    elapsed = time.perf_counter() - started

    payload = pickle.dumps(records, protocol=pickle.HIGHEST_PROTOCOL)
    output.write_bytes(payload)
    manifest = {
        "script": "scripts/capture_policy_decisions.py",
        "policy": arguments.policy,
        "seed": arguments.seed,
        "game_mode": arguments.game_mode,
        "seats": "same Policy in all four seats, one instance per seat",
        "decisions": len(records),
        "steps": result.steps,
        "scores": list(result.scores),
        "elapsed_s": elapsed,
        "pickle_protocol": pickle.HIGHEST_PROTOCOL,
        "bytes_sha256": hashlib.sha256(payload).hexdigest(),
        "semantic_sha256": semantic_digest(records),
        "lisjong": _source_identity(lisjong),
        "lisjong_arena": _source_identity(lisjong_arena),
        "riichienv_version": importlib.metadata.version("riichienv"),
        "python": sys.version,
        "environment_variables": {
            "LISJONG_SHANTEN_BACKEND": os.environ.get("LISJONG_SHANTEN_BACKEND"),
        },
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, indent=2))
    return 0


def _digest(arguments: argparse.Namespace) -> int:
    for path in arguments.paths:
        payload = pathlib.Path(path).read_bytes()
        records = pickle.loads(payload)
        print(
            json.dumps(
                {
                    "path": path,
                    "decisions": len(records),
                    "bytes_sha256": hashlib.sha256(payload).hexdigest(),
                    "semantic_sha256": semantic_digest(records),
                }
            )
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    capture = commands.add_parser("capture")
    capture.add_argument("--policy", required=True)
    capture.add_argument("--seed", type=int, required=True)
    capture.add_argument("--game-mode", default="4p-red-half")
    capture.add_argument("--output", required=True)
    digest = commands.add_parser("digest")
    digest.add_argument("paths", nargs="+")
    arguments = parser.parse_args(argv)
    if arguments.command == "capture":
        return _capture(arguments)
    return _digest(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
