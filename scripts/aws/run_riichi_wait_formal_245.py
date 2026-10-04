"""lisbun/lisjong#245 formal run driver (lisbun/lisjong-arena#447).

Run by ``bootstrap-riichi-wait-formal-245.sh`` on one EC2 instance (see
``docs/riichi-wait-formal-245.md``).  Standard library only, except that
``verify-source`` runs in the lisjong consumer environment and imports lisjong.

``run`` executes, in order:

1. unpack the S1 bundle (train / valid) and check it against the pinned SHA-256
2. concurrently: generate the 100-hanchan formal test source (Arena venv,
   ``generate_riichi_wait_formal_source_245.py``) and ``select`` on the S1 train
   / valid (lisjong consumer venv).  ``select`` never reads the test source
3. completeness check of the generated source and ``generation.json``
4. ``test`` once, with the selection fixed
5. evidence: SHA-256 of every file, per-step wall / CPU / max RSS, memory peaks

The run is all-or-nothing.  Any failed step, the compute deadline, or an
interrupt stops the remaining steps (process groups are killed), writes the
report with status ``INCOMPLETE`` and exits non-zero; ``test`` is never run on a
partial source.  The driver does not interpret the result of ``test``.

``smoke`` mode runs the same steps on development seeds (no registry binding)
and writes the report with ``"mode": "smoke"``; it can never touch the formal
seeds.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import lzma
import os
import shutil
import signal
import subprocess
import sys
import tarfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GENERATOR = ROOT / "scripts" / "generate_riichi_wait_formal_source_245.py"

REPORT_SCHEMA = "lisjong-arena-riichi-wait-formal-run-v1"
STATUS_COMPLETE = "COMPLETE"
STATUS_INCOMPLETE = "INCOMPLETE"

FORMAL_FIRST = 932000
FORMAL_LAST = 932099
FORMAL_PROTOCOL = "riichi-wait-estimator-formal-test-v1"

# S1 (lisbun/lisjong#237): the train / valid data the selection is made on.
S1_BUNDLE_SHA256 = "0986a4048110d4c113473403a8d16382623ea0c0a0d3138dc1d70387ee24702e"
S1_BUNDLE_BYTES = 1_837_412
S1_MANIFEST_SHA256_PREFIX = "e1a6f65e"
S1_MANIFEST_SHA256_SUFFIX = "d05b"
S1_SPLITS = {
    "train": list(range(931200, 931360)),
    "valid": list(range(931360, 931380)),
    "test": list(range(931380, 931400)),
}
S1_SOURCE_FILES = ("manifest.json", "decisions.jsonl", "label_facts.jsonl")

DEADLINE_UPTIME_SECONDS = 2700
"""Compute is cut off 45 minutes after the instance boots (formal mode)."""
POLL_SECONDS = 1.0
MEMORY_SAMPLE_SECONDS = 2.0
EVALUATION = ("-m", "lisjong.learning.riichi_wait_evaluation")


class RunError(RuntimeError):
    """A precondition or step failed (STOP / INVALID)."""


# ---------------------------------------------------------------------------
# S1 bundle
# ---------------------------------------------------------------------------


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _bundle_source_members(archive: tarfile.TarFile) -> dict[str, tarfile.TarInfo]:
    members = {}
    for member in archive.getmembers():
        parts = Path(member.name).parts
        if len(parts) >= 3 and parts[-2] == "source" and member.isfile():
            name = parts[-1]
            plain = name[:-3] if name.endswith(".xz") else name
            if plain in S1_SOURCE_FILES:
                if plain in members:
                    raise RunError(f"bundle has two {plain} files")
                members[plain] = member
    missing = [name for name in S1_SOURCE_FILES if name not in members]
    if missing:
        raise RunError(f"bundle source/ has no {missing}")
    return members


def unpack_s1(bundle: Path, destination: Path) -> dict[str, dict[str, object]]:
    """Check the bundle against the pinned SHA-256 and write the three source files.

    ``decisions.jsonl`` and ``label_facts.jsonl`` may be stored xz-compressed in the
    bundle; they are written decompressed.  The manifest's own file digests are
    checked, and the splits must be the S1 splits.
    """
    if bundle.stat().st_size != S1_BUNDLE_BYTES:
        raise RunError("S1 bundle size differs from the pinned size")
    if sha256_file(bundle) != S1_BUNDLE_SHA256:
        raise RunError("S1 bundle SHA-256 differs from the pinned value")
    destination.mkdir(parents=True, exist_ok=False)
    with tarfile.open(bundle, "r:xz") as archive:
        members = _bundle_source_members(archive)
        for plain, member in members.items():
            stream = archive.extractfile(member)
            assert stream is not None
            target = destination / plain
            with stream, open(target, "wb") as out:
                if member.name.endswith(".xz"):
                    with lzma.open(stream) as source:
                        shutil.copyfileobj(source, out, 1 << 20)
                else:
                    shutil.copyfileobj(stream, out, 1 << 20)
    manifest_path = destination / "manifest.json"
    manifest_sha = sha256_file(manifest_path)
    if not (
        manifest_sha.startswith(S1_MANIFEST_SHA256_PREFIX)
        and manifest_sha.endswith(S1_MANIFEST_SHA256_SUFFIX)
    ):
        raise RunError("S1 manifest SHA-256 differs from the recorded value")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest["splits"] != S1_SPLITS:
        raise RunError("S1 manifest splits differ from the S1 splits")
    digests = {}
    for name, filename in (
        ("decisions", "decisions.jsonl"),
        ("label_facts", "label_facts.jsonl"),
    ):
        path = destination / filename
        entry = manifest["files"][name]
        if (path.stat().st_size, sha256_file(path)) != (
            entry["bytes"],
            entry["sha256"],
        ):
            raise RunError(f"S1 {filename} does not match the manifest digest")
        digests[filename] = {"bytes": entry["bytes"], "sha256": entry["sha256"]}
    digests["manifest.json"] = {
        "bytes": manifest_path.stat().st_size,
        "sha256": manifest_sha,
    }
    return digests


# ---------------------------------------------------------------------------
# step runner
# ---------------------------------------------------------------------------


def uptime_seconds() -> float:
    with open("/proc/uptime", encoding="ascii") as handle:
        return float(handle.read().split()[0])


class MemorySampler:
    """Peak system memory in use per phase, from /proc/meminfo."""

    def __init__(self) -> None:
        self.phase = "setup"
        self.peaks: dict[str, int] = {}
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    @staticmethod
    def used_kb() -> int:
        info = {}
        with open("/proc/meminfo", encoding="ascii") as handle:
            for line in handle:
                key, value = line.split(":", 1)
                info[key] = int(value.split()[0])
        return info["MemTotal"] - info["MemAvailable"]

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.peaks[self.phase] = max(self.peaks.get(self.phase, 0), self.used_kb())
            self._stop.wait(MEMORY_SAMPLE_SECONDS)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join()


def _tail(path: Path) -> str:
    return path.read_bytes()[-1500:].decode(errors="replace")


def _kill_all(running: dict) -> None:
    for _, proc, *_ in running.values():
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def run_steps(specs, log_dir: Path, *, deadline_uptime: float | None = None):
    """Run ``(name, argv, env)`` steps concurrently; stop all on a failure.

    Each step runs in its own process group and is reaped with ``os.wait4`` so its
    wall, user / system CPU and max RSS (children included) are recorded.  The
    first non-zero exit, or ``/proc/uptime`` passing ``deadline_uptime``, kills
    every step still running (whole process groups) and raises ``RunError``.
    The deadline is also checked before each step starts: once it has passed,
    no further step is started.
    Returns the step rows (also when failed, as ``RunError.rows``).
    """
    running: dict[int, tuple] = {}
    rows: list[dict[str, object]] = []
    failure: str | None = None
    try:
        for name, argv, env in specs:
            if deadline_uptime is not None and uptime_seconds() > deadline_uptime:
                failure = (
                    f"compute deadline passed before starting step {name} "
                    f"(uptime {uptime_seconds():.0f}s > {deadline_uptime:.0f}s)"
                )
                _kill_all(running)
                break
            logs = tuple(
                log_dir / f"{name.replace('/', '-')}.{stream}"
                for stream in ("stdout", "stderr")
            )
            with open(logs[0], "wb") as stdout, open(logs[1], "wb") as stderr:
                proc = subprocess.Popen(
                    argv,
                    stdout=stdout,
                    stderr=stderr,
                    env=env,
                    start_new_session=True,
                )
            running[proc.pid] = (
                name,
                proc,
                time.perf_counter(),
                logs,
                uptime_seconds(),
            )
        while running:
            for pid in list(running):
                reaped, status, usage = os.wait4(pid, os.WNOHANG)
                if reaped == 0:
                    continue
                name, proc, started, logs, started_uptime = running.pop(pid)
                code = os.waitstatus_to_exitcode(status)
                proc.returncode = code
                rows.append(
                    {
                        "step": name,
                        "argv": list(proc.args),
                        "start_uptime_seconds": round(started_uptime, 1),
                        "wall_seconds": round(time.perf_counter() - started, 3),
                        "user_cpu_seconds": round(usage.ru_utime, 3),
                        "system_cpu_seconds": round(usage.ru_stime, 3),
                        "maxrss_kb": usage.ru_maxrss,
                        "exit_status": code,
                        "stdout_tail": _tail(logs[0]),
                        "stderr_tail": _tail(logs[1]),
                    }
                )
                if code and failure is None:
                    failure = f"step {name} failed with exit status {code}"
                    _kill_all(running)
            if (
                failure is None
                and running
                and deadline_uptime is not None
                and uptime_seconds() > deadline_uptime
            ):
                failure = (
                    f"compute deadline passed (uptime {uptime_seconds():.0f}s > "
                    f"{deadline_uptime:.0f}s)"
                )
                _kill_all(running)
            if running:
                time.sleep(POLL_SECONDS)
    except BaseException:
        _kill_all(running)
        raise
    finally:
        for pid in list(running):
            try:
                _, status, usage = os.wait4(pid, 0)
            except ChildProcessError:
                continue
            name, proc, started, logs, started_uptime = running.pop(pid)
            proc.returncode = os.waitstatus_to_exitcode(status)
            rows.append(
                {
                    "step": name,
                    "argv": list(proc.args),
                    "start_uptime_seconds": round(started_uptime, 1),
                    "wall_seconds": round(time.perf_counter() - started, 3),
                    "exit_status": proc.returncode,
                    "stopped": True,
                    "maxrss_kb": usage.ru_maxrss,
                    "stdout_tail": _tail(logs[0]),
                    "stderr_tail": _tail(logs[1]),
                }
            )
    if failure is not None:
        error = RunError(failure)
        error.rows = rows
        raise error
    return rows


# ---------------------------------------------------------------------------
# source verification (runs in the lisjong consumer environment)
# ---------------------------------------------------------------------------


def verify_source(
    generated: Path,
    *,
    mode: str,
    allocation_identity: str | None,
) -> dict[str, object]:
    """Completeness check of a generated source against ``generation.json``.

    Uses the lisjong strict readers (manifest digests, row counts, player-safe
    decisions) but never opens the label facts.
    """
    from lisjong.learning import riichi_deal_in_source as source

    document = json.loads((generated / "generation.json").read_text(encoding="utf-8"))
    if document["mode"] != mode:
        raise RunError("generation.json mode differs from the run mode")
    manifest, decisions = source.read_decisions(generated / "source")
    if manifest.splits["train"] != () or manifest.splits["valid"] != ():
        raise RunError("generated manifest has train / valid seeds")
    expected = (
        tuple(range(FORMAL_FIRST, FORMAL_LAST + 1))
        if mode == "formal"
        else tuple(range(document["seeds"]["first"], document["seeds"]["last"] + 1))
    )
    if manifest.splits["test"] != expected:
        raise RunError("generated test seeds differ from the expected population")
    if document["seeds"] != {
        "first": expected[0],
        "last": expected[-1],
        "count": len(expected),
    }:
        raise RunError("generation.json seeds differ from the expected population")
    games = document["games"]
    if [game["seed"] for game in games] != list(expected):
        raise RunError("generation.json games differ from the expected seeds")
    if any(decision.key.seed not in set(expected) for decision in decisions):
        raise RunError("a decision has a seed outside the expected population")
    if sum(game["decisions"] for game in games) != len(decisions):
        raise RunError("generation.json decision count differs from the source")
    for name in ("manifest.json", "decisions.jsonl", "label_facts.jsonl"):
        path = generated / "source" / name
        digest = {"bytes": path.stat().st_size, "sha256": sha256_file(path)}
        if document["files"][name] != digest:
            raise RunError(f"{name} differs from generation.json")
    if mode == "formal":
        allocation = document["allocation"]
        if (
            allocation["allocation_identity"] != allocation_identity
            or allocation["protocol"] != FORMAL_PROTOCOL
            or allocation["state"] not in ("RESERVED", "COMMITTED")
        ):
            raise RunError("generation.json allocation differs from the plan")
        if document["runtime"]["shanten_backend"]["name"] != "rust":
            raise RunError("generation did not use the rust backend")
    return {
        "hanchan": len(games),
        "decisions": len(decisions),
        "producer": manifest.producer,
        "files": document["files"],
    }


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


def _compress(source: Path, target: Path) -> None:
    with open(source, "rb") as reader, lzma.open(target, "wb", preset=6) as writer:
        shutil.copyfileobj(reader, writer, 1 << 20)


def _env(*, rust: bool) -> dict[str, str]:
    env = dict(os.environ)
    env.pop("LISJONG_SHANTEN_BACKEND", None)
    if rust:
        env["LISJONG_SHANTEN_BACKEND"] = "rust"
    return env


def _file_digests(root: Path) -> dict[str, dict[str, object]]:
    return {
        str(path.relative_to(root)): {
            "bytes": path.stat().st_size,
            "sha256": sha256_file(path),
        }
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def run(arguments: argparse.Namespace) -> int:
    mode = arguments.mode
    work, output = arguments.work, arguments.output
    for path in (work, output):
        if path.exists():
            raise RunError(f"refusing to overwrite {path}")
    work.mkdir(parents=True)
    output.mkdir(parents=True)
    logs = output / "logs"
    logs.mkdir()
    sampler = MemorySampler()
    report: dict[str, object] = {
        "schema": REPORT_SCHEMA,
        "mode": mode,
        "status": STATUS_INCOMPLETE,
        "reason": "not finished",
        "workers": arguments.workers,
        "nproc": os.cpu_count(),
        "consumer_python": arguments.consumer_python,
        "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "start_uptime_seconds": round(uptime_seconds(), 1),
        "steps": [],
    }
    seconds = arguments.deadline_uptime_seconds
    if seconds is None:
        seconds = DEADLINE_UPTIME_SECONDS if mode == "formal" else 0
    deadline = float(seconds) if seconds > 0 else None
    report["deadline_uptime_seconds"] = deadline
    started = time.perf_counter()
    sampler.start()
    code = 1
    try:
        # 1. S1 train / valid
        sampler.phase = "prepare"
        if arguments.s1_bundle is not None:
            s1_digests = unpack_s1(arguments.s1_bundle, work / "s1")
        else:
            if mode == "formal":
                raise RunError("formal mode requires the pinned S1 bundle")
            shutil.copytree(arguments.s1_source, work / "s1")
            s1_digests = _file_digests(work / "s1")
        report["s1_files"] = s1_digests
        generated = work / "generated"
        selection = output / "selection.json"

        # 2. generate and select (concurrent)
        generate = [
            sys.executable,
            str(GENERATOR),
            mode,
            "--workers",
            str(arguments.workers),
            "--output",
            str(generated),
            "--progress",
            str(output / "progress-generation.txt"),
        ]
        if mode == "formal":
            generate += [
                "--seed-ledger",
                str(arguments.seed_ledger),
                "--allocation-identity",
                arguments.allocation_identity,
            ]
        else:
            generate += ["--seeds", arguments.smoke_seeds]
        sampler.phase = "generate+select"
        report["steps"] += run_steps(
            [
                ("generate", generate, _env(rust=mode == "formal")),
                (
                    "select",
                    [
                        arguments.consumer_python,
                        *EVALUATION,
                        "select",
                        str(work / "s1"),
                        str(selection),
                    ],
                    _env(rust=False),
                ),
            ],
            logs,
            deadline_uptime=deadline,
        )
        report["selection"] = {
            "bytes": selection.stat().st_size,
            "sha256": sha256_file(selection),
        }

        # 3. completeness
        sampler.phase = "verify"
        verify = [
            arguments.consumer_python,
            str(Path(__file__).resolve()),
            "verify-source",
            "--generated",
            str(generated),
            "--mode",
            mode,
        ]
        if mode == "formal":
            verify += ["--allocation-identity", arguments.allocation_identity]
        report["steps"] += run_steps(
            [("verify-source", verify, _env(rust=False))],
            logs,
            deadline_uptime=deadline,
        )
        report["source_check"] = json.loads((logs / "verify-source.stdout").read_text())

        # 4. fix the hashes in local evidence, then run test once.  The hashes
        # are posted to the Issue after the run (pre-registration addendum).
        pre_test = {
            "schema": "lisjong-riichi-wait-formal-pre-test-v1",
            "written_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "mode": mode,
            "selection": report["selection"],
            "source_files": report["source_check"]["files"],
            "allocation_identity": arguments.allocation_identity,
        }
        with open(output / "pre-test-evidence.json", "w", encoding="utf-8") as handle:
            handle.write(json.dumps(pre_test, indent=2, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        sampler.phase = "test"
        result = output / "test-result.json"
        report["steps"] += run_steps(
            [
                (
                    "test",
                    [
                        arguments.consumer_python,
                        *EVALUATION,
                        "test",
                        str(generated / "source"),
                        str(selection),
                        str(result),
                    ],
                    _env(rust=False),
                )
            ],
            logs,
            deadline_uptime=deadline,
        )
        report["test_result"] = {
            "bytes": result.stat().st_size,
            "sha256": sha256_file(result),
        }

        # 5. evidence
        sampler.phase = "evidence"
        evidence = output / "source"
        evidence.mkdir()
        shutil.copy2(generated / "generation.json", output / "generation.json")
        shutil.copy2(generated / "source" / "manifest.json", evidence / "manifest.json")
        for name in ("decisions.jsonl", "label_facts.jsonl"):
            _compress(generated / "source" / name, evidence / f"{name}.xz")
        report["status"] = STATUS_COMPLETE
        report["reason"] = "all steps passed"
        code = 0
    except RunError as error:
        report["reason"] = str(error)
        report["steps"] += getattr(error, "rows", [])
        print(f"STOP / INVALID: {error}", file=sys.stderr)
    except BaseException as error:
        report["reason"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        sampler.stop()
        report["memory_peak_used_kb_by_phase"] = sampler.peaks
        report["ended_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        report["end_uptime_seconds"] = round(uptime_seconds(), 1)
        report["wall_seconds"] = round(time.perf_counter() - started, 3)
        (output / "formal-run-report.json").write_text(
            json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    return code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)

    check = commands.add_parser(
        "check-bundle", help="local no-billing check of the S1 bundle"
    )
    check.add_argument("--bundle", type=Path, required=True)
    check.add_argument("--work", type=Path, required=True)

    verify = commands.add_parser("verify-source")
    verify.add_argument("--generated", type=Path, required=True)
    verify.add_argument("--mode", choices=("formal", "smoke"), required=True)
    verify.add_argument("--allocation-identity")

    run_parser = commands.add_parser("run")
    run_parser.add_argument("--mode", choices=("formal", "smoke"), required=True)
    run_parser.add_argument("--workers", type=int, required=True)
    run_parser.add_argument("--consumer-python", required=True)
    run_parser.add_argument("--work", type=Path, required=True)
    run_parser.add_argument("--output", type=Path, required=True)
    run_parser.add_argument("--s1-bundle", type=Path)
    run_parser.add_argument("--s1-source", type=Path)
    run_parser.add_argument("--seed-ledger", type=Path)
    run_parser.add_argument("--allocation-identity")
    run_parser.add_argument("--smoke-seeds")
    run_parser.add_argument(
        "--deadline-uptime-seconds",
        type=int,
        help="compute deadline as instance uptime; default 2700 (formal), none (smoke)",
    )
    return parser


def main(argv=None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        if arguments.command == "check-bundle":
            digests = unpack_s1(arguments.bundle, arguments.work)
            print(json.dumps(digests, indent=2, sort_keys=True))
            return 0
        if arguments.command == "verify-source":
            summary = verify_source(
                arguments.generated,
                mode=arguments.mode,
                allocation_identity=arguments.allocation_identity,
            )
            print(json.dumps(summary, indent=2, sort_keys=True))
            return 0
        if (arguments.mode == "formal") == (arguments.smoke_seeds is not None):
            raise RunError("--smoke-seeds is for smoke mode only (and required there)")
        if arguments.mode == "formal" and (
            arguments.seed_ledger is None
            or arguments.allocation_identity is None
            or arguments.s1_source is not None
        ):
            raise RunError(
                "formal mode needs --seed-ledger, --allocation-identity and --s1-bundle"
            )
        if (arguments.s1_bundle is None) == (arguments.s1_source is None):
            raise RunError("pass exactly one of --s1-bundle and --s1-source")
        return run(arguments)
    except RunError as error:
        print(f"STOP / INVALID: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
