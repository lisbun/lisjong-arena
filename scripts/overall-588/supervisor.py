"""#588 one-shot supervisor for an Overall v2 event (lock -> run -> fresh verify).

  supervisor.py prepare --event-dir NEW_DIR --plan PLAN_INPUT.json
  supervisor.py run     --event-dir DIR
  supervisor.py check   --event-dir DIR

Standard library only. Needs Linux, cgroup v2 and a systemd user manager.

Event directory (``prepare`` creates it; it must not exist yet)::

  plan.json       write-once plan: label, entry, workers, lock arguments, budgets,
                  seed ledger reference, approval reference, injection (dev only)
  staging/        the only place the lock lets the event write its four outputs
  logs/           stage stdout/stderr, 1 Hz cgroup samples, barrier files
  lock.json       written by the lock stage (destinations inside staging/)
  published/      appears only on PASS: staging/ renamed after all gates passed,
                  with SHA256SUMS; files made read-only
  quarantine/     appears only on STOP / INVALID: staging/ renamed, evidence kept
  receipt.json    write-once terminal receipt (PASS / STOP / INVALID)

An event output is valid only when receipt.json says PASS *and* ``check`` passes
(published/SHA256SUMS matches). No receipt, or any other status, means not valid.

Each stage (lock, run, verify) runs in its own transient scope::

  systemd-run --user --scope --unit=lo588-EVENT-STAGE -p MemoryMax=.. -p MemorySwapMax=0
              -p OOMPolicy=kill -p TimeoutStopSec=3 -p RuntimeMaxSec=..

The supervisor is a separate process. Every poll it reads the scope's cgroup files:
memory.current, memory.peak, memory.events, memory.max, memory.swap.max,
memory.swap.current, cgroup.procs, plus free space of the event filesystem and the
size of staging/. It stops the whole scope (all descendants, including spawn
workers) when:

  wall / total-wall   stage or whole-event wall budget exceeded (max of monotonic
                      and realtime elapsed)
  memory-soft         memory.peak > memory_soft_peak_bytes
  memory-oom          oom / oom_kill / oom_group_kill rose, or the scope ended as
                      oom-kill (the kernel hard limit fired)
  swap                memory.swap.current > 0
  disk-low            free bytes < min_free_disk_bytes
  disk-output         staging/ bytes > max_staging_bytes
  disk-unreadable     free space or staging/ size could not be read
  monitor-unreadable  any cgroup file missing, unreadable or malformed
  monitor-isolation   the scope cgroup was not found, or memory.max / memory.swap.max
                      differ from the plan (the hard limits are not in force)

and records ``child-exit`` when a stage command exits non-zero. Host RSS of the
scope's PIDs is recorded as a diagnostic only, never as a stop threshold.

Status: STOP for a guard / budget / child exit; INVALID when the event ran but its
outputs cannot be trusted (unexpected file set, lock mismatch, verifier failure,
result mismatch, leftover processes, cleanup / quarantine / publish error, supervisor
error). A non-null ``quarantine_error`` or ``cleanup_error`` is always INVALID.
There is no restart, no retry and no partial publication; a receipt that exists
blocks any further run of the same event directory. The supervisor never prints a
classification; on PASS it reports only the result identity.

Exit codes: 0 PASS, 3 STOP, 4 INVALID, 2 refused before the event started.
"""

import argparse
import hashlib
import json
import os
import platform
import re
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

PLAN_SCHEMA = "overall-588-event-plan-v1"
RECEIPT_SCHEMA = "overall-588-event-receipt-v1"
LABELS = ("DEVELOPMENT", "FORMAL")
ENTRIES = ("formal", "development-reduced")
STAGES = ("lock", "run", "verify")
FORMAL_WORKERS = 8
UNIT_PREFIX = "lo588"
OUTPUTS = {
    "comparison_artifact": "comparison.json",
    "overall_result": "overall-result.json",
    "kyoku_records": "kyoku-records.json",
    "kyoku_summary": "kyoku-summary.json",
}
SUMS_NAME = "SHA256SUMS"
RESERVED_LOCK_FLAGS = (
    "--out",
    "--workers",
    "--comparison-artifact",
    "--overall-result",
    "--kyoku-records",
    "--kyoku-summary",
)
INJECTIONS = (
    "worker-crash",
    "hang",
    "cgroup-unreadable",
    "disk-low",
    "disk-unreadable",
    "mutate-kyoku",
    "truncate-kyoku",
    "verifier-fail",
    "cleanup-fail",
)
INJECT_AFTER_SECONDS = 5.0
STOP_EVENTS = ("oom", "oom_kill", "oom_group_kill")
STOP_REASONS = (
    "preflight",
    "wall",
    "total-wall",
    "memory-soft",
    "memory-oom",
    "swap",
    "disk-low",
    "disk-output",
    "disk-unreadable",
    "monitor-unreadable",
    "monitor-isolation",
    "child-exit",
)
INVALID_REASONS = (
    "output-set",
    "lock-mismatch",
    "verify-failed",
    "result-mismatch",
    "publish-error",
    "cleanup-leftover",
    "cleanup-error",
    "quarantine-error",
    "supervisor-error",
)
EXIT_PASS, EXIT_REFUSED, EXIT_STOP, EXIT_INVALID = 0, 2, 3, 4
SCOPE_GRACE_SECONDS = 10.0
DEV_ENTRY = Path(__file__).resolve().with_name("dev_entry.py")
CGROUP_CONTROLLERS = Path("/sys/fs/cgroup/cgroup.controllers")


class PlanError(ValueError):
    """The plan is not acceptable; nothing was started."""


class Halt(Exception):
    """A guard fired inside a stage."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


class SupervisorTerminated(BaseException):
    """SIGTERM / SIGHUP reached the supervisor itself."""


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _canonical(document: dict) -> str:
    return json.dumps(document, indent=2, sort_keys=True, ensure_ascii=True) + "\n"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_once(path: Path, text: str) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())


# ---------------------------------------------------------------- plan


def _int(value, name: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise PlanError(f"{name} must be an int >= {minimum}")
    return value


def _text(value, name: str) -> str:
    if type(value) is not str or not value.strip():
        raise PlanError(f"{name} must be a non-empty string")
    return value


def validate_plan(document: object) -> dict:
    """Return the plan unchanged, or raise PlanError. FORMAL rules are strict."""
    keys = {
        "schema",
        "event_name",
        "label",
        "entry",
        "dev_seed_blocks",
        "workers",
        "lock_arguments",
        "budgets",
        "seed_ledger",
        "approval_reference",
        "inject",
    }
    if not isinstance(document, dict) or set(document) != keys:
        raise PlanError(f"plan must have exactly the keys {sorted(keys)}")
    if document["schema"] != PLAN_SCHEMA:
        raise PlanError(f"plan schema must be {PLAN_SCHEMA}")
    name = document["event_name"]
    if type(name) is not str or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", name):
        raise PlanError("event_name must match [a-z0-9][a-z0-9-]{0,39}")
    label, entry = document["label"], document["entry"]
    if label not in LABELS:
        raise PlanError(f"label must be one of {LABELS}")
    if entry not in ENTRIES:
        raise PlanError(f"entry must be one of {ENTRIES}")
    workers = _int(document["workers"], "workers", minimum=1)
    if entry == "development-reduced":
        _int(document["dev_seed_blocks"], "dev_seed_blocks", minimum=1)
        if document["dev_seed_blocks"] > 8:
            raise PlanError("dev_seed_blocks must be 1..8")
    elif document["dev_seed_blocks"] is not None:
        raise PlanError("dev_seed_blocks is only for the development-reduced entry")
    arguments = document["lock_arguments"]
    if not isinstance(arguments, list) or not all(
        type(item) is str for item in arguments
    ):
        raise PlanError("lock_arguments must be a list of strings")
    for item in arguments:
        if item.split("=", 1)[0] in RESERVED_LOCK_FLAGS:
            raise PlanError(f"lock_arguments must not set {item.split('=', 1)[0]}")
    if "--seeds" not in arguments:
        raise PlanError("lock_arguments must contain --seeds")
    budgets = document["budgets"]
    budget_keys = {
        "total_wall_seconds",
        "stage_wall_seconds",
        "memory_soft_peak_bytes",
        "memory_max_bytes",
        "min_free_disk_bytes",
        "max_staging_bytes",
        "poll_seconds",
        "max_cost_usd",
    }
    if not isinstance(budgets, dict) or set(budgets) != budget_keys:
        raise PlanError(f"budgets must have exactly the keys {sorted(budget_keys)}")
    total = _int(budgets["total_wall_seconds"], "total_wall_seconds", minimum=1)
    walls = budgets["stage_wall_seconds"]
    if not isinstance(walls, dict) or set(walls) != set(STAGES):
        raise PlanError(f"stage_wall_seconds must have exactly {list(STAGES)}")
    for stage in STAGES:
        _int(walls[stage], f"stage_wall_seconds.{stage}", minimum=1)
    soft = _int(budgets["memory_soft_peak_bytes"], "memory_soft_peak_bytes", minimum=1)
    hard = _int(budgets["memory_max_bytes"], "memory_max_bytes", minimum=1 << 20)
    if hard % 4096:
        raise PlanError("memory_max_bytes must be a multiple of 4096 (page size)")
    _int(budgets["min_free_disk_bytes"], "min_free_disk_bytes")
    _int(budgets["max_staging_bytes"], "max_staging_bytes", minimum=1)
    poll = budgets["poll_seconds"]
    if type(poll) not in (int, float) or not 0.05 <= poll <= 10:
        raise PlanError("poll_seconds must be a number in [0.05, 10]")
    cost = budgets["max_cost_usd"]
    if cost is not None and (type(cost) not in (int, float) or cost < 0):
        raise PlanError("max_cost_usd must be null or a number >= 0")
    inject = document["inject"]
    if inject is not None and inject not in INJECTIONS:
        raise PlanError(f"inject must be null or one of {INJECTIONS}")
    for key in ("seed_ledger", "approval_reference"):
        if document[key] is not None:
            _text(document[key], key)
    if label == "FORMAL":
        if entry != "formal":
            raise PlanError("a FORMAL plan must use the formal entry")
        if inject is not None:
            raise PlanError("a FORMAL plan must not inject faults")
        if workers != FORMAL_WORKERS:
            raise PlanError(f"a FORMAL plan must use {FORMAL_WORKERS} workers")
        if soft >= hard:
            raise PlanError("a FORMAL plan needs memory_soft_peak_bytes < memory_max")
        if sum(walls.values()) > total:
            raise PlanError("a FORMAL plan's stage walls must fit the total wall")
        if cost is None:
            raise PlanError("a FORMAL plan must declare max_cost_usd")
        for key in ("seed_ledger", "approval_reference"):
            _text(document[key], key)
    return document


def _entry_argv(plan: dict) -> list[str]:
    if plan["entry"] == "formal":
        return [sys.executable, "-m", "lisjong_arena.overall_champion_aabb"]
    return [
        sys.executable,
        str(DEV_ENTRY),
        "--seed-blocks",
        str(plan["dev_seed_blocks"]),
    ]


def _paths(event: Path) -> dict[str, Path]:
    staging = event / "staging"
    return {
        "plan": event / "plan.json",
        "lock": event / "lock.json",
        "staging": staging,
        "logs": event / "logs",
        "published": event / "published",
        "quarantine": event / "quarantine",
        "receipt": event / "receipt.json",
        **{name: staging / file for name, file in OUTPUTS.items()},
    }


def stage_argv(plan: dict, paths: dict[str, Path], stage: str) -> list[str]:
    entry = _entry_argv(plan)
    if stage == "lock":
        return [
            *entry,
            "lock",
            *plan["lock_arguments"],
            "--out",
            str(paths["lock"]),
            "--workers",
            str(plan["workers"]),
            "--comparison-artifact",
            str(paths["comparison_artifact"]),
            "--overall-result",
            str(paths["overall_result"]),
            "--kyoku-records",
            str(paths["kyoku_records"]),
            "--kyoku-summary",
            str(paths["kyoku_summary"]),
        ]
    if stage == "run":
        return [
            *entry,
            "run",
            "--lock",
            str(paths["lock"]),
            "--progress",
            "--no-report",
        ]
    comparison = paths["comparison_artifact"]
    if plan["inject"] in ("verifier-fail", "cleanup-fail"):
        # DEVELOPMENT only: hand the real verifier a wrong comparison file.
        comparison = paths["kyoku_records"]
    return [
        *entry,
        "verify",
        "--no-report",
        "--lock",
        str(paths["lock"]),
        "--comparison",
        str(comparison),
        "--result",
        str(paths["overall_result"]),
        "--kyoku-records",
        str(paths["kyoku_records"]),
        "--kyoku-summary",
        str(paths["kyoku_summary"]),
    ]


def prepare(event: Path, plan_input: Path) -> int:
    plan = validate_plan(json.loads(plan_input.read_text(encoding="utf-8")))
    event.mkdir(parents=False, exist_ok=False)
    paths = _paths(event)
    _write_once(paths["plan"], _canonical(plan))
    paths["staging"].mkdir()
    paths["logs"].mkdir()
    print(f"plan_sha256={_sha256(paths['plan'])}")
    print(f"event_dir={event}")
    return 0


# ---------------------------------------------------------------- platform


def _launch(unit: str, plan: dict, stage: str, argv: list[str], logs: Path):
    """Start ARGV inside a fresh transient scope behind the completion barrier."""
    budgets = plan["budgets"]
    done = logs / f"{stage}.done.json"
    release = logs / f"{stage}.release"
    command = [
        "systemd-run",
        "--user",
        "--scope",
        f"--unit={unit}",
        "-p",
        f"MemoryMax={budgets['memory_max_bytes']}",
        "-p",
        "MemorySwapMax=0",
        "-p",
        "OOMPolicy=kill",
        "-p",
        "TimeoutStopSec=3",
        "-p",
        f"RuntimeMaxSec={budgets['stage_wall_seconds'][stage] + 60}",
        "--",
        sys.executable,
        str(Path(__file__).resolve()),
        "_exec",
        str(done),
        str(release),
        *argv,
    ]
    stdout = (logs / f"{stage}.stdout").open("x", encoding="utf-8")
    stderr = (logs / f"{stage}.stderr").open("x", encoding="utf-8")
    try:
        process = subprocess.Popen(
            command, stdout=stdout, stderr=stderr, start_new_session=True
        )
    finally:
        stdout.close()
        stderr.close()
    return command, process


def _cgroup_dir(scope: str) -> Path | None:
    """The scope's own cgroup directory (never a lookup by PID)."""
    out = subprocess.run(
        ["systemctl", "--user", "show", scope, "--property=ControlGroup", "--value"],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    relative = out.stdout.strip()
    if out.returncode or not relative:
        return None
    path = PurePosixPath(relative)
    if not path.is_absolute() or ".." in path.parts or path.name != scope:
        raise Halt("monitor-isolation", f"unexpected ControlGroup {relative!r}")
    return Path("/sys/fs/cgroup") / relative.lstrip("/")


def _scope_state(scope: str) -> dict:
    out = subprocess.run(
        ["systemctl", "--user", "show", scope]
        + ["-p", "ActiveState", "-p", "Result", "-p", "LoadState"],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    return dict(line.split("=", 1) for line in out.stdout.splitlines() if "=" in line)


def _scope_active(state: dict) -> bool:
    return state.get("ActiveState") in ("active", "activating", "deactivating")


def _wait_inactive(scope: str, seconds: float = 5.0) -> dict:
    """The scope state once it is no longer active, or the last state after SECONDS."""
    deadline = time.monotonic() + seconds
    state = _scope_state(scope)
    while _scope_active(state) and time.monotonic() < deadline:
        time.sleep(0.1)
        state = _scope_state(scope)
    return state


def _reset_scope(scope: str) -> None:
    subprocess.run(
        ["systemctl", "--user", "reset-failed", scope],
        capture_output=True,
        check=False,
        timeout=10,
    )


def _stop_scope(scope: str, process: subprocess.Popen) -> float:
    """SIGTERM the whole scope, SIGKILL after 3 s, then the process group as well."""
    started = time.monotonic()
    subprocess.run(
        ["systemctl", "--user", "stop", scope], capture_output=True, check=False
    )
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        subprocess.run(
            ["systemctl", "--user", "kill", "--signal=SIGKILL", scope],
            capture_output=True,
            check=False,
        )
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
    return time.monotonic() - started


def _free_bytes(path: Path) -> int:
    stat = os.statvfs(path)
    return stat.f_bavail * stat.f_frsize


def _tree_bytes(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path, onerror=_raise):
        for name in files:
            total += os.lstat(os.path.join(root, name)).st_size
    return total


def _raise(error: OSError) -> None:
    raise error


def _proc_rss(pid: int) -> int:
    for line in Path(f"/proc/{pid}/status").read_text().splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1]) * 1024
    return 0


def _proc_parent(pid: int) -> int:
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    return int(fields[1])


def _proc_cmdline(pid: int) -> str:
    return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode()


# ---------------------------------------------------------------- cgroup samples


def _read_int(path: Path) -> int:
    value = path.read_text().strip()
    if not value.isdecimal():
        raise ValueError(f"{path.name} is not a nonnegative integer: {value!r}")
    return int(value)


def _read_events(path: Path) -> dict[str, int]:
    events: dict[str, int] = {}
    for line in path.read_text().splitlines():
        fields = line.split()
        if len(fields) != 2 or not fields[1].isdecimal() or fields[0] in events:
            raise ValueError(f"memory.events is malformed: {line!r}")
        events[fields[0]] = int(fields[1])
    if any(key not in events for key in (*STOP_EVENTS, "max")):
        raise ValueError("memory.events lacks oom/oom_kill/oom_group_kill/max")
    return events


def read_cgroup(directory: Path) -> dict:
    """One fail-closed reading of the scope cgroup (OSError / ValueError if not)."""
    procs = [
        int(line) for line in (directory / "cgroup.procs").read_text().split() if line
    ]
    return {
        "memory_current": _read_int(directory / "memory.current"),
        "memory_peak": _read_int(directory / "memory.peak"),
        "memory_max": (directory / "memory.max").read_text().strip(),
        "swap_max": (directory / "memory.swap.max").read_text().strip(),
        "swap_current": _read_int(directory / "memory.swap.current"),
        "events": _read_events(directory / "memory.events"),
        "pids": procs,
    }


# ---------------------------------------------------------------- stage


class Event:
    def __init__(self, event: Path, plan: dict) -> None:
        self.dir = event
        self.plan = plan
        self.paths = _paths(event)
        self.budgets = plan["budgets"]
        self.inject = plan["inject"]
        self.mono0 = time.monotonic()
        self.real0 = time.time()
        self.injected: list[dict] = []

    def elapsed(self) -> float:
        return max(time.monotonic() - self.mono0, time.time() - self.real0)

    def remaining(self) -> float:
        return self.budgets["total_wall_seconds"] - self.elapsed()

    # DEVELOPMENT-only fault injection hooks (validate_plan forbids them in FORMAL).

    def _inject_sample(self, stage: str, elapsed: float, sample: dict) -> None:
        if stage != "run" or elapsed < INJECT_AFTER_SECONDS:
            return
        if self.inject == "cgroup-unreadable":
            self._note("cgroup-unreadable")
            raise OSError("injected: memory.current unreadable")

    def _inject_disk(self, stage: str, elapsed: float, free: int) -> int:
        if stage != "run" or elapsed < INJECT_AFTER_SECONDS:
            return free
        if self.inject == "disk-low":
            self._note("disk-low")
            return 0
        if self.inject == "disk-unreadable":
            self._note("disk-unreadable")
            raise OSError("injected: statvfs failed")
        return free

    def _inject_action(self, stage: str, elapsed: float, wrapper: int, pids) -> None:
        if stage != "run" or elapsed < INJECT_AFTER_SECONDS or self._done_note():
            return
        if self.inject == "worker-crash":
            for pid in sorted(pids, reverse=True):
                try:
                    if "spawn_main" in _proc_cmdline(pid):
                        os.kill(pid, signal.SIGKILL)
                        self._note("worker-crash", pid=pid)
                        return
                except OSError:
                    continue
        elif self.inject == "hang":
            for pid in pids:
                try:
                    if pid != wrapper and _proc_parent(pid) == wrapper:
                        os.kill(pid, signal.SIGSTOP)
                        self._note("hang", pid=pid)
                        return
                except OSError:
                    continue

    def _done_note(self) -> bool:
        return any(item["kind"] in ("worker-crash", "hang") for item in self.injected)

    def _note(self, kind: str, **detail) -> None:
        if not any(item["kind"] == kind for item in self.injected):
            self.injected.append({"kind": kind, "at": _now(), **detail})

    def run_stage(self, stage: str) -> dict:
        logs = self.paths["logs"]
        argv = stage_argv(self.plan, self.paths, stage)
        scope = f"{UNIT_PREFIX}-{self.plan['event_name']}-{stage}.scope"
        wall = self.budgets["stage_wall_seconds"][stage]
        soft = self.budgets["memory_soft_peak_bytes"]
        hard = str(self.budgets["memory_max_bytes"])
        poll = self.budgets["poll_seconds"]
        record: dict = {
            "stage": stage,
            "argv": argv,
            "scope": scope,
            "started_at": _now(),
            "wall_limit_seconds": wall,
            "reason": None,
            "detail": None,
        }
        if self.remaining() <= 0:
            record.update(reason="total-wall", detail="not started: budget spent")
            return record
        done = logs / f"{stage}.done.json"
        release = logs / f"{stage}.release"
        command, process = _launch(
            scope[: -len(".scope")], self.plan, stage, argv, logs
        )
        record["command"] = command
        start = time.monotonic(), time.time()
        cgroup = None
        final = False
        baseline = None
        last = None
        peak = current_max = swap_max = rss_max = 0
        pids_max = 0
        pids_seen: set[int] = set()
        free_min = None
        staging_max = 0
        reason = detail = None
        samples = (logs / f"{stage}.samples.jsonl").open("x", encoding="utf-8")

        def take_sample(elapsed: float) -> None:
            nonlocal baseline, last, peak, current_max, swap_max, rss_max, pids_max
            sample = read_cgroup(cgroup)
            self._inject_sample(stage, elapsed, sample)
            if sample["memory_max"] != hard or sample["swap_max"] != "0":
                raise Halt(
                    "monitor-isolation",
                    f"memory.max={sample['memory_max']} memory.swap.max="
                    f"{sample['swap_max']} (plan {hard} / 0)",
                )
            if baseline is None:
                baseline = sample["events"]
                if not sample["pids"]:
                    raise Halt("monitor-isolation", "scope has no processes")
                if any(baseline[key] for key in STOP_EVENTS):
                    raise Halt("memory-oom", "OOM events already present")
            for key, value in baseline.items():
                if sample["events"].get(key, -1) < value:
                    raise ValueError(f"memory.events {key} decreased")
            last = sample["events"]
            peak = max(peak, sample["memory_peak"])
            current_max = max(current_max, sample["memory_current"])
            swap_max = max(swap_max, sample["swap_current"])
            pids_max = max(pids_max, len(sample["pids"]))
            pids_seen.update(sample["pids"])
            rss = 0
            for pid in sample["pids"]:
                try:
                    rss += _proc_rss(pid)
                except OSError, ValueError, IndexError:
                    pass  # diagnostic only; a PID can exit between reads
            rss_max = max(rss_max, rss)
            samples.write(
                json.dumps(
                    {
                        "t": round(elapsed, 3),
                        "current": sample["memory_current"],
                        "peak": sample["memory_peak"],
                        "swap": sample["swap_current"],
                        "pids": len(sample["pids"]),
                        "rss_sum_diagnostic": rss,
                        "events": sample["events"],
                    },
                    sort_keys=True,
                )
                + "\n"
            )
            if any(last[key] > baseline[key] for key in STOP_EVENTS):
                raise Halt("memory-oom", f"memory.events {last}")
            if sample["swap_current"] > 0:
                raise Halt("swap", f"memory.swap.current={sample['swap_current']}")
            if sample["memory_peak"] > soft:
                raise Halt("memory-soft", f"memory.peak={sample['memory_peak']}")
            if stage == "run":
                self._inject_action(stage, elapsed, process.pid, sample["pids"])

        def check_disk(elapsed: float) -> None:
            nonlocal free_min, staging_max
            try:
                free = self._inject_disk(stage, elapsed, _free_bytes(self.dir))
                size = _tree_bytes(self.paths["staging"])
            except OSError as error:
                raise Halt("disk-unreadable", str(error)) from None
            free_min = free if free_min is None else min(free_min, free)
            staging_max = max(staging_max, size)
            if free < self.budgets["min_free_disk_bytes"]:
                raise Halt("disk-low", f"free={free}")
            if size > self.budgets["max_staging_bytes"]:
                raise Halt("disk-output", f"staging={size}")

        try:
            while True:
                elapsed = max(time.monotonic() - start[0], time.time() - start[1])
                if cgroup is None:
                    cgroup = _cgroup_dir(scope)
                    if cgroup is None and elapsed > SCOPE_GRACE_SECONDS:
                        if process.poll() is not None:
                            break
                        raise Halt("monitor-isolation", "scope cgroup not found")
                if cgroup is not None and not final:
                    try:
                        take_sample(elapsed)
                    except (OSError, ValueError) as error:
                        if process.poll() is not None:
                            break  # the scope ended (e.g. kernel OOM kill)
                        raise Halt("monitor-unreadable", str(error)) from None
                    if done.exists():
                        take_sample(elapsed)  # final readback, scope still alive
                        final = True
                        release.touch(exist_ok=False)
                check_disk(elapsed)
                if elapsed > wall:
                    raise Halt("wall", f"{elapsed:.1f}s > {wall}s")
                if self.remaining() <= 0:
                    raise Halt("total-wall", f"event elapsed {self.elapsed():.1f}s")
                if process.poll() is not None:
                    break
                time.sleep(poll)
        except Halt as halt:
            reason, detail = halt.reason, halt.detail
        except (OSError, ValueError) as error:
            reason, detail = "monitor-unreadable", str(error)
        except BaseException as error:
            reason, detail = "supervisor-error", f"{type(error).__name__}: {error}"
            if isinstance(error, SupervisorTerminated):
                detail = "supervisor received a termination signal"
        finally:
            samples.close()
        stop_seconds = None
        if reason is not None and process.poll() is None:
            stop_seconds = _stop_scope(scope, process)
        code = process.wait()
        # Cleanup: the scope must disappear once its processes are gone.
        leftover = cleanup_error = None
        state = _wait_inactive(scope)
        if _scope_active(state):
            leftover = True
            _stop_scope(scope, process)
            state = _wait_inactive(scope)
            if _scope_active(state):
                cleanup_error = f"scope still {state.get('ActiveState')} after stop"
        _reset_scope(scope)
        if reason is None and state.get("Result") == "oom-kill":
            reason, detail = "memory-oom", "scope Result=oom-kill"
        if reason is None and code != 0:
            if state.get("Result") == "oom-kill":
                reason, detail = "memory-oom", "scope Result=oom-kill"
            else:
                reason = "verify-failed" if stage == "verify" else "child-exit"
                detail = f"exit code {code}"
        if reason is None and not final:
            reason, detail = "monitor-unreadable", "no final cgroup readback"
        if reason is None and leftover:
            reason, detail = "cleanup-leftover", "processes outlived the command"
        if cleanup_error:
            reason, detail = "cleanup-error", cleanup_error
        record.update(
            finished_at=_now(),
            elapsed_seconds=round(
                max(time.monotonic() - start[0], time.time() - start[1]), 3
            ),
            exit_code=code,
            reason=reason,
            detail=detail,
            stop_seconds=stop_seconds,
            cgroup=str(cgroup) if cgroup else None,
            final_readback=final,
            memory_peak_bytes=peak,
            memory_current_max_bytes=current_max,
            memory_max=hard,
            memory_soft_peak_bytes=soft,
            swap_current_max_bytes=swap_max,
            memory_events_baseline=baseline,
            memory_events_last=last,
            pids_max=pids_max,
            pids_seen=len(pids_seen),
            rss_sum_max_diagnostic_bytes=rss_max,
            free_disk_min_bytes=free_min,
            staging_bytes_max=staging_max,
            scope_state=state,
            leftover_processes=bool(leftover),
            cleanup_error=cleanup_error,
        )
        return record


# ---------------------------------------------------------------- event


def _status(reason: str | None) -> str:
    if reason is None:
        return "PASS"
    return "STOP" if reason in STOP_REASONS else "INVALID"


def _check_outputs(paths: dict[str, Path]) -> str | None:
    expected = set(OUTPUTS.values())
    present = {entry.name for entry in paths["staging"].iterdir()}
    if present != expected:
        return f"staging holds {sorted(present)}, expected {sorted(expected)}"
    for name in expected:
        entry = paths["staging"] / name
        if entry.is_symlink() or not entry.is_file():
            return f"{name} is not a regular file"
    return None


def _check_lock(paths: dict[str, Path]) -> tuple[str | None, dict]:
    lock = json.loads(paths["lock"].read_text(encoding="utf-8"))
    info = {
        "lock_sha256": _sha256(paths["lock"]),
        "lock_identity": lock.get("lock_identity"),
        "lock_version": lock.get("lock_version"),
    }
    if lock.get("lock_version") != 2:
        return "the event requires lock v2", info
    wanted = {name: str(paths[name]) for name in OUTPUTS}
    if lock.get("artifact_destinations") != wanted:
        return "lock destinations are not the event staging paths", info
    return None, info


def _inject_outputs(event: Event) -> None:
    target = event.paths["kyoku_records"]
    if event.inject == "mutate-kyoku":
        data = bytearray(target.read_bytes())
        index = max(i for i, byte in enumerate(data) if 0x30 <= byte <= 0x39)
        data[index] = 0x30 + (data[index] - 0x30 + 1) % 10  # change one digit
        target.chmod(0o644)
        target.write_bytes(bytes(data))
        event._note("mutate-kyoku", offset=index)
    elif event.inject == "truncate-kyoku":
        data = target.read_bytes()
        target.chmod(0o644)
        target.write_bytes(data[: len(data) // 2])
        event._note("truncate-kyoku")


def _sums(directory: Path) -> str:
    return "".join(
        f"{_sha256(directory / name)}  {name}\n" for name in sorted(OUTPUTS.values())
    )


def _publish(event: Event) -> dict:
    paths = event.paths
    sums = _sums(paths["staging"])
    _write_once(paths["staging"] / SUMS_NAME, sums)
    os.rename(paths["staging"], paths["published"])
    published = paths["published"]
    if _sums(published) != sums:
        raise OSError("published files changed during publication")
    for entry in published.iterdir():
        entry.chmod(0o444)
    published.chmod(0o555)
    return {"path": str(published), "sha256sums": sums.splitlines()}


def _quarantine(event: Event) -> tuple[dict | None, str | None]:
    paths = event.paths
    if event.inject == "cleanup-fail":
        _write_once(paths["quarantine"], "injected: quarantine target occupied\n")
        event._note("cleanup-fail")
    try:
        if not paths["staging"].exists():
            return None, None
        os.rename(paths["staging"], paths["quarantine"])
        contents = sorted(entry.name for entry in paths["quarantine"].iterdir())
        return {"path": str(paths["quarantine"]), "contents": contents}, None
    except OSError as error:
        return None, f"{type(error).__name__}: {error}"


def _environment() -> dict:
    boot = Path("/proc/sys/kernel/random/boot_id")
    return {
        "boot_id": boot.read_text().strip() if boot.exists() else None,
        "hostname": platform.node(),
        "kernel": platform.release(),
        "python": sys.version.split()[0],
        "python_executable": sys.executable,
        "supervisor_sha256": _sha256(Path(__file__).resolve()),
        "dev_entry_sha256": _sha256(DEV_ENTRY) if DEV_ENTRY.exists() else None,
        "wsl": "microsoft" in platform.release().lower(),
    }


def preflight(event: Event) -> str | None:
    paths = event.paths
    if paths["receipt"].exists() or paths["lock"].exists():
        return "event already ran (receipt or lock exists); events are one-shot"
    for name in ("published", "quarantine"):
        if os.path.lexists(paths[name]):
            return f"{name}/ already exists"
    if not paths["staging"].is_dir() or any(paths["staging"].iterdir()):
        return "staging/ must exist and be empty"
    if any(paths["logs"].iterdir()):
        return "logs/ must be empty"
    controllers = CGROUP_CONTROLLERS
    if not controllers.exists() or "memory" not in controllers.read_text().split():
        return "cgroup v2 memory controller not available"
    if _free_bytes(event.dir) < event.budgets["min_free_disk_bytes"]:
        return "free disk below min_free_disk_bytes before start"
    return None


def run_event(event_dir: Path) -> int:
    paths = _paths(event_dir)
    plan_text = paths["plan"].read_text(encoding="utf-8")
    plan = validate_plan(json.loads(plan_text))
    if _canonical(plan) != plan_text:
        raise PlanError("plan.json is not the canonical prepared plan")
    event = Event(event_dir, plan)
    receipt: dict = {
        "schema": RECEIPT_SCHEMA,
        "event_dir": str(event_dir.resolve()),
        "event_name": plan["event_name"],
        "label": plan["label"],
        "entry": plan["entry"],
        "plan_sha256": _sha256(paths["plan"]),
        "seed_ledger": plan["seed_ledger"],
        "approval_reference": plan["approval_reference"],
        "budgets": plan["budgets"],
        "inject": plan["inject"],
        "started_at": _now(),
        "environment": _environment(),
        "stages": [],
        "lock": None,
        "published": None,
        "quarantine": None,
        "quarantine_error": None,
        "result_identity": None,
        "population_consumed": False,
    }
    refused = preflight(event)
    if refused is not None:
        print(f"supervisor: refused: {refused}", file=sys.stderr)
        return EXIT_REFUSED

    def terminate(signum, frame):
        raise SupervisorTerminated(signum)

    previous = {
        sig: signal.signal(sig, terminate) for sig in (signal.SIGTERM, signal.SIGHUP)
    }
    reason = detail = None
    try:
        for stage in STAGES:
            if stage == "lock":
                receipt["population_consumed"] = True
            record = event.run_stage(stage)
            receipt["stages"].append(record)
            if record["reason"] is not None:
                reason, detail = record["reason"], record["detail"]
                break
            if stage == "lock":
                problem, receipt["lock"] = _check_lock(paths)
                if problem:
                    reason, detail = "lock-mismatch", problem
                    break
            elif stage == "run":
                problem = _check_outputs(paths)
                if problem:
                    reason, detail = "output-set", problem
                    break
                _inject_outputs(event)
        if reason is None:
            result = json.loads(paths["overall_result"].read_text(encoding="utf-8"))
            printed = re.findall(
                r"^result_identity=(\S+)$",
                (paths["logs"] / "verify.stdout").read_text(encoding="utf-8"),
                flags=re.M,
            )
            if printed != [result.get("result_identity")]:
                reason, detail = "result-mismatch", "verifier and result file differ"
            elif (problem := _check_outputs(paths)) is not None:
                reason, detail = "output-set", problem
            else:
                try:
                    receipt["published"] = _publish(event)
                    receipt["result_identity"] = result["result_identity"]
                except OSError as error:
                    reason, detail = "publish-error", f"{type(error).__name__}: {error}"
    except BaseException as error:  # never leave without a receipt
        reason = "supervisor-error"
        detail = f"{type(error).__name__}: {error}"
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    if reason is not None:
        receipt["published"] = None
        receipt["result_identity"] = None
        receipt["quarantine"], receipt["quarantine_error"] = _quarantine(event)
        if receipt["quarantine_error"] is not None:
            detail = f"{reason}: {detail}; quarantine: {receipt['quarantine_error']}"
            reason = "quarantine-error"
    status = _status(reason)
    if any(stage.get("cleanup_error") for stage in receipt["stages"]):
        status = "INVALID"
    receipt.update(
        status=status,
        trigger={"reason": reason, "detail": detail} if reason else None,
        injected=event.injected,
        finished_at=_now(),
        elapsed_seconds=round(event.elapsed(), 3),
    )
    pending = event_dir / ".receipt.pending"
    _write_once(pending, _canonical(receipt))
    os.link(pending, paths["receipt"])  # fails if a receipt already exists
    pending.unlink()
    print(f"status={status}")
    if reason:
        print(f"reason={reason}")
    if status == "PASS":
        print(f"result_identity={receipt['result_identity']}")
        print(f"published={paths['published']}")
    print(f"receipt={paths['receipt']}")
    return {"PASS": EXIT_PASS, "STOP": EXIT_STOP}.get(status, EXIT_INVALID)


def check_event(event_dir: Path) -> int:
    """Consumer gate: 0 only for a PASS receipt whose published sums still match."""
    paths = _paths(event_dir)
    try:
        receipt = json.loads(paths["receipt"].read_text(encoding="utf-8"))
        if receipt.get("schema") != RECEIPT_SCHEMA or receipt.get("status") != "PASS":
            raise ValueError(f"receipt status is {receipt.get('status')!r}")
        if receipt.get("quarantine_error") is not None or os.path.lexists(
            paths["quarantine"]
        ):
            raise ValueError("quarantine present")
        if receipt.get("plan_sha256") != _sha256(paths["plan"]):
            raise ValueError("plan.json changed")
        if (receipt.get("lock") or {}).get("lock_sha256") != _sha256(paths["lock"]):
            raise ValueError("lock.json changed")
        recorded = "".join(line + "\n" for line in receipt["published"]["sha256sums"])
        if (paths["published"] / SUMS_NAME).read_text() != recorded:
            raise ValueError("SHA256SUMS differs from the receipt")
        if _sums(paths["published"]) != recorded:
            raise ValueError("published files differ from SHA256SUMS")
        names = {entry.name for entry in paths["published"].iterdir()}
        if names != {*OUTPUTS.values(), SUMS_NAME}:
            raise ValueError(f"published/ holds {sorted(names)}")
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"check=NOT-VALID ({error})")
        return 1
    print("check=PASS")
    print(f"label={receipt['label']}")
    print(f"result_identity={receipt['result_identity']}")
    return 0


def _execute_barrier(done: Path, release: Path, argv: list[str]) -> int:
    """Inside the scope: run ARGV, then hold the scope until its final readback."""
    code = subprocess.call(argv)
    _write_once(done, json.dumps({"returncode": code}) + "\n")
    while not release.exists():
        time.sleep(0.05)
    return 128 - code if code < 0 else code


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["_exec"]:
        return _execute_barrier(Path(argv[1]), Path(argv[2]), argv[3:])
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    commands = parser.add_subparsers(dest="command", required=True)
    first = commands.add_parser("prepare")
    first.add_argument("--event-dir", type=Path, required=True)
    first.add_argument("--plan", type=Path, required=True)
    for name in ("run", "check"):
        commands.add_parser(name).add_argument("--event-dir", type=Path, required=True)
    arguments = parser.parse_args(argv)
    arguments.event_dir = arguments.event_dir.absolute()
    try:
        if arguments.command == "prepare":
            return prepare(arguments.event_dir, arguments.plan)
        if arguments.command == "check":
            return check_event(arguments.event_dir)
        return run_event(arguments.event_dir)
    except (PlanError, FileExistsError, FileNotFoundError) as error:
        print(f"supervisor: refused: {error}", file=sys.stderr)
        return EXIT_REFUSED


if __name__ == "__main__":
    sys.exit(main())
