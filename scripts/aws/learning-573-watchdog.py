"""#573 Learning evaluation watchdog, derived from the #564 preparation operator script.

  watchdog.py init --start-file PATH --limit-seconds N
      write the cumulative start record (realtime, monotonic, boot id, limit). Refuses to overwrite.
  watchdog.py run --stage NAME --wall S --runtime-max S --memory-max 4G
                  --disk-limit-bytes N [--watch DIR ...] [--watch-optional DIR ...] --disk-poll S
      (--watch: must exist; --watch-optional: may not exist yet, counts 0 until created)
                  --start-file PATH --log-dir DIR -- COMMAND ...

run starts COMMAND with
  systemd-run --user --scope --unit=lp573-NAME -p MemoryMax=.. -p MemorySwapMax=0
              -p OOMPolicy=kill -p TimeoutStopSec=3 -p RuntimeMaxSec=..
and stops the scope (SIGTERM, SIGKILL after 3 s; SIGKILL again if still active 5 s later) when
  - max(realtime, monotonic) stage elapsed > --wall               stopped_by = "wall"
  - cumulative elapsed since the start record > its limit         stopped_by = "cumulative"
  - du -sb of the --watch directories > --disk-limit-bytes        stopped_by = "disk"
    (polled every --disk-poll seconds: a detection threshold, not a hard quota)
  - du fails twice (non-zero exit / no or non-numeric output)     stopped_by = "disk-unreadable"
  - the watchdog itself raises                                    stopped_by = "watchdog-error"
The kernel (MemoryMax, OOMPolicy=kill) and systemd (RuntimeMaxSec) may also end the scope;
then stopped_by is null and the scope state is recorded.

Memory guard mode (#580), enabled by --memory-soft-peak-bytes N:
  - the scope's memory.peak > N (polled, and again at the final readback)  stopped_by = "memory-soft"
  - oom / oom_kill / oom_group_kill in the scope's memory.events rose above
    the value read when the scope was found, or the scope ended as oom-kill stopped_by = "memory-oom"
  The increase of the "max" event is recorded only (reclaim can succeed after it).
--guard-output PATH (repeatable): when the stage does not succeed (exit code != 0), PATH and its
sibling staging entries (.NAME.staging-*) are moved to LOG_DIR/NAME.quarantine, so a runner that
was killed (SIGKILL / OOM) cannot leave a published or half-published output behind.
--unit-prefix (default lp573) names the scope PREFIX-NAME.scope.

Each run writes LOG_DIR/NAME.watchdog.json (write-once; a stage never runs twice) and appends
the same record to LOG_DIR/watchdog.jsonl. Exit code: 0 only when the command exited 0 and the
watchdog did not stop it; 124 when the watchdog stopped it; otherwise the command's code
(128 + signal for a signal). Standard library only.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _boot_id() -> str:
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def _write_once(path: Path, document: dict) -> None:
    with path.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(document, indent=2, sort_keys=True) + "\n")


def init(args) -> int:
    _write_once(
        Path(args.start_file),
        {
            "boot_id": _boot_id(),
            "limit_seconds": args.limit_seconds,
            "monotonic": time.monotonic(),
            "realtime": time.time(),
            "started_at": _now(),
        },
    )
    return 0


def _cumulative(start: dict) -> float:
    elapsed = time.time() - start["realtime"]
    if _boot_id() == start["boot_id"]:
        elapsed = max(elapsed, time.monotonic() - start["monotonic"])
    return elapsed


def _du(required, optional=()) -> int | None:
    """Total bytes of the watched directories, or None when any reading fails (fail closed).

    ``required`` directories must exist (missing -> None). An ``optional`` directory that does
    not exist yet (FileNotFoundError) counts 0. Any other stat error (e.g. a parent without
    permission, where os.path.exists() would say False) -> None. Each du must exit 0 with a
    numeric first field; a failure is retried once after 1 s (a file vanishing during the walk),
    and a second failure makes the whole reading None, which stops the scope
    ("disk-unreadable").
    """
    total = 0
    paths = [(path, True) for path in required] + [(path, False) for path in optional]
    for path, must_exist in paths:
        try:
            os.stat(path)
        except FileNotFoundError:
            if must_exist:
                return None
            continue
        except OSError:
            return None
        for attempt in (1, 2):
            try:
                out = subprocess.run(
                    ["du", "-sb", "--", path],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=60,
                )
                fields = out.stdout.split()
                ok = out.returncode == 0 and bool(fields) and fields[0].isdigit()
            except OSError, subprocess.SubprocessError:
                ok = False
            if ok:
                total += int(fields[0])
                break
            if attempt == 1:
                time.sleep(1)
        else:
            return None
    return total


def _scope_peak_path(unit: str) -> Path | None:
    """Resolve the exact unit, never the not-yet-moved systemd-run PID."""
    out = subprocess.run(
        ["systemctl", "--user", "show", unit, "--property=ControlGroup", "--value"],
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    relative = out.stdout.strip()
    if out.returncode or not relative:
        return None
    path = PurePosixPath(relative)
    if not path.is_absolute() or ".." in path.parts or path.name != unit:
        raise RuntimeError(f"unexpected ControlGroup for {unit}: {relative!r}")
    return Path("/sys/fs/cgroup") / relative.lstrip("/") / "memory.peak"


def _read_peak(path: Path) -> int:
    value = path.read_text().strip()
    if not value.isdecimal():
        raise RuntimeError("scope memory.peak is not a nonnegative integer")
    return int(value)


STOP_EVENTS = ("oom", "oom_group_kill", "oom_kill")


def _read_events(path: Path) -> dict[str, int]:
    """memory.events; a malformed file or a missing stop key is an error."""
    events = {}
    for line in path.read_text().splitlines():
        fields = line.split()
        if len(fields) != 2 or not fields[1].isdecimal() or fields[0] in events:
            raise RuntimeError(f"scope memory.events is malformed: {line!r}")
        events[fields[0]] = int(fields[1])
    if any(key not in events for key in (*STOP_EVENTS, "max")):
        raise RuntimeError("scope memory.events lacks oom/oom_kill/oom_group_kill/max")
    return events


def _event_stop(baseline: dict, events: dict) -> bool:
    for key in baseline:
        if events.get(key, -1) < baseline[key]:
            raise RuntimeError(f"scope memory.events {key} decreased")
    return any(events[key] > baseline[key] for key in STOP_EVENTS)


def _quarantine(paths: list[str], target: Path) -> list[dict]:
    """Move each existing output and its hidden staging siblings out of place."""
    moved = []
    for text in paths:
        path = Path(text)
        candidates = [path] if os.path.lexists(path) else []
        if path.parent.is_dir():
            candidates += sorted(path.parent.glob(f".{path.name}.staging-*"))
        for source in candidates:
            target.mkdir(parents=True, exist_ok=True)
            destination = target / f"{len(moved):03d}-{source.name}"
            shutil.move(str(source), str(destination))
            moved.append({"from": str(source), "to": str(destination)})
    return moved


def _execute_barrier(done: Path, release: Path, command: list[str]) -> int:
    """Keep the scope alive after command exit until its final peak is recorded."""
    code = subprocess.call(command)
    _write_once(done, {"returncode": code})
    while not release.exists():
        time.sleep(0.05)
    return 128 - code if code < 0 else code


def _scope_state(unit: str) -> dict:
    out = subprocess.run(
        [
            "systemctl",
            "--user",
            "show",
            unit,
            "-p",
            "Result",
            "-p",
            "ActiveState",
            "-p",
            "LoadState",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    state = dict(line.split("=", 1) for line in out.stdout.splitlines() if "=" in line)
    subprocess.run(
        ["systemctl", "--user", "reset-failed", unit], capture_output=True, check=False
    )
    return state


def run(args) -> int:
    log_dir = Path(args.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    record_path = log_dir / f"{args.stage}.watchdog.json"
    if record_path.exists():
        print(f"watchdog: refusing, {args.stage} already ran", flush=True)
        return 2
    start = json.loads(Path(args.start_file).read_text())
    remaining = start["limit_seconds"] - _cumulative(start)
    unit = f"{args.unit_prefix}-{args.stage}"
    soft = args.memory_soft_peak_bytes
    done = log_dir.resolve() / f"{args.stage}.command-done.json"
    release = log_dir.resolve() / f"{args.stage}.release"
    if done.exists() or release.exists():
        raise FileExistsError("stage barrier files already exist")
    command = [
        "systemd-run",
        "--user",
        "--scope",
        f"--unit={unit}",
        "-p",
        f"MemoryMax={args.memory_max}",
        "-p",
        "MemorySwapMax=0",
        "-p",
        "OOMPolicy=kill",
        "-p",
        "TimeoutStopSec=3",
        "-p",
        f"RuntimeMaxSec={args.runtime_max}",
        "--",
        sys.executable,
        str(Path(__file__).resolve()),
        "_exec",
        str(done),
        str(release),
        *args.command,
    ]
    record = {
        "command": command,
        "cumulative_limit_seconds": start["limit_seconds"],
        "cumulative_remaining_at_start": remaining,
        "disk_limit_bytes": args.disk_limit_bytes,
        "disk_poll_seconds": args.disk_poll,
        "guard_output": args.guard_output,
        "memory_max": args.memory_max,
        "memory_soft_peak_bytes": soft,
        "runtime_max_seconds": args.runtime_max,
        "stage": args.stage,
        "started_at": _now(),
        "unit": f"{unit}.scope",
        "wall_limit_seconds": args.wall,
        "watch": args.watch,
        "watch_optional": args.watch_optional,
    }
    if remaining <= 0:
        record.update(exit_code=124, stopped_by="cumulative", finished_at=_now())
        record["note"] = "not started: the cumulative limit was already spent"
        _write_once(record_path, record)
        with (log_dir / "watchdog.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
        print(f"watchdog: {args.stage} not started, cumulative limit spent", flush=True)
        return 124
    print(f"watchdog: {args.stage} start {' '.join(command)}", flush=True)
    mono0, real0 = time.monotonic(), time.time()
    proc = subprocess.Popen(command)
    stopped_by = None
    stop_seconds = None
    memory_peak = None
    disk_max = 0
    next_disk = 0.0
    watchdog_error = None
    peak_path = None
    final_peak_recorded = False
    events_baseline = None
    events_last = None

    def guard_memory() -> str | None:
        """Memory guard mode: the reason to stop, or None (raises OSError if unreadable)."""
        nonlocal events_baseline, events_last
        events = _read_events(peak_path.with_name("memory.events"))
        if events_baseline is None:
            events_baseline = events
            if any(events[key] for key in STOP_EVENTS):
                return "memory-oom"
        events_last = events
        if _event_stop(events_baseline, events):
            return "memory-oom"
        if memory_peak > soft:
            return "memory-soft"
        return None

    def stop_scope() -> float:
        t0 = time.monotonic()
        subprocess.run(["systemctl", "--user", "stop", f"{unit}.scope"], check=False)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            subprocess.run(
                ["systemctl", "--user", "kill", "--signal=SIGKILL", f"{unit}.scope"],
                check=False,
            )
            proc.kill()
            proc.wait()
        return time.monotonic() - t0

    try:
        while proc.poll() is None:
            elapsed = max(time.monotonic() - mono0, time.time() - real0)
            if peak_path is None:
                peak_path = _scope_peak_path(f"{unit}.scope")
                if peak_path is None and elapsed > 10:
                    raise RuntimeError(
                        "scope ControlGroup unavailable after startup grace"
                    )
            if peak_path is not None and not final_peak_recorded:
                try:
                    peak = _read_peak(peak_path)
                except OSError:
                    # An OOM/systemd kill can remove the entire scope between
                    # poll and read. Preserve its failing exit (never a success).
                    if proc.poll() not in (None, 0):
                        break
                    raise
                memory_peak = max(memory_peak or 0, peak)
                if soft is not None:
                    try:
                        stopped_by = guard_memory()
                    except OSError:
                        if proc.poll() not in (None, 0):
                            break
                        raise
            if done.exists() and not final_peak_recorded:
                if peak_path is None:
                    raise RuntimeError("command ended without a verified scope")
                memory_peak = max(memory_peak or 0, _read_peak(peak_path))
                if soft is not None:
                    stopped_by = stopped_by or guard_memory()
                final_peak_recorded = True
                release.touch(exist_ok=False)
            if elapsed >= next_disk:
                disk = _du(args.watch, args.watch_optional)
                next_disk = elapsed + args.disk_poll
                if disk is None:
                    stopped_by = stopped_by or "disk-unreadable"
                else:
                    disk_max = max(disk_max, disk)
                    if disk > args.disk_limit_bytes:
                        stopped_by = stopped_by or "disk"
            if stopped_by is None and elapsed > args.wall:
                stopped_by = "wall"
            if stopped_by is None and _cumulative(start) > start["limit_seconds"]:
                stopped_by = "cumulative"
            if stopped_by is not None:
                print(f"watchdog: stopping {unit}.scope ({stopped_by})", flush=True)
                stop_seconds = stop_scope()
                break
            time.sleep(1)
    except (
        BaseException
    ) as error:  # never leave the scope running if the watchdog fails
        watchdog_error = f"{type(error).__name__}: {error}"
        stopped_by = "watchdog-error"
        print(f"watchdog: error ({watchdog_error}); stopping {unit}.scope", flush=True)
        stop_seconds = stop_scope()
    code = proc.wait()
    if code == 0 and not final_peak_recorded:
        stopped_by = stopped_by or "memory-unverified"
    disk_at_end = _du(args.watch, args.watch_optional)
    if disk_at_end is None:
        stopped_by = stopped_by or "disk-unreadable"
    elif disk_at_end > args.disk_limit_bytes:
        stopped_by = stopped_by or "disk"
    scope = _scope_state(f"{unit}.scope")
    if soft is not None and scope.get("Result") == "oom-kill":
        stopped_by = stopped_by or "memory-oom"
    exit_code = 124 if stopped_by else (128 - code if code < 0 else code)
    quarantined, quarantine_error = [], None
    if exit_code != 0 and args.guard_output:
        try:
            quarantined = _quarantine(
                args.guard_output, log_dir.resolve() / f"{args.stage}.quarantine"
            )
        except OSError as error:
            quarantine_error = f"{type(error).__name__}: {error}"
    record.update(
        command_returncode=code,
        cumulative_elapsed_at_end=_cumulative(start),
        disk_bytes_max=disk_max,
        disk_bytes_at_end=disk_at_end,
        watchdog_error=watchdog_error,
        elapsed_monotonic=time.monotonic() - mono0,
        elapsed_realtime=time.time() - real0,
        exit_code=exit_code,
        finished_at=_now(),
        memory_peak_bytes=memory_peak,
        memory_peak_path=str(peak_path) if peak_path else None,
        memory_events_baseline=events_baseline,
        memory_events_last=events_last,
        memory_max_events_delta=(
            events_last["max"] - events_baseline["max"]
            if events_last and events_baseline
            else None
        ),
        final_peak_recorded=final_peak_recorded,
        quarantined=quarantined,
        quarantine_error=quarantine_error,
        scope=scope,
        stop_seconds=stop_seconds,
        stopped_by=stopped_by,
    )
    _write_once(record_path, record)
    with (log_dir / "watchdog.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
    print(
        f"watchdog: {args.stage} exit={exit_code} stopped_by={stopped_by} "
        f"elapsed={record['elapsed_realtime']:.1f}s memory_peak={memory_peak}",
        flush=True,
    )
    return exit_code


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "_exec":
        return _execute_barrier(Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4:])
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="mode", required=True)
    first = sub.add_parser("init")
    first.add_argument("--start-file", required=True)
    first.add_argument("--limit-seconds", type=int, required=True)
    stage = sub.add_parser("run")
    stage.add_argument("--stage", required=True)
    stage.add_argument("--wall", type=int, required=True)
    stage.add_argument("--runtime-max", type=int, required=True)
    stage.add_argument("--memory-max", required=True)
    stage.add_argument("--disk-limit-bytes", type=int, required=True)
    stage.add_argument("--watch", action="append", default=[])
    stage.add_argument("--watch-optional", action="append", default=[])
    stage.add_argument("--disk-poll", type=int, required=True)
    stage.add_argument("--memory-soft-peak-bytes", type=int, default=None)
    stage.add_argument("--guard-output", action="append", default=[])
    stage.add_argument("--unit-prefix", default="lp573")
    stage.add_argument("--start-file", required=True)
    stage.add_argument("--log-dir", required=True)
    stage.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.mode == "init":
        return init(args)
    if args.command and args.command[0] == "--":
        args.command = args.command[1:]
    if not args.command:
        parser.error("a command is required after --")
    if not args.watch and not args.watch_optional:
        parser.error("at least one --watch or --watch-optional directory is required")
    if args.memory_soft_peak_bytes is not None and args.memory_soft_peak_bytes <= 0:
        parser.error("--memory-soft-peak-bytes must be positive")
    if not args.unit_prefix.replace("-", "").replace("_", "").isalnum():
        parser.error("--unit-prefix must be letters, digits, '-' or '_'")
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
