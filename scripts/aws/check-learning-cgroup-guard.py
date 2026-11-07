"""#580 cgroup memory guard: Linux/systemd acceptance check with dummy processes.

  python3.14 scripts/aws/check-learning-cgroup-guard.py <new evidence directory>

Needs Linux, cgroup v2, a systemd user manager, and ``lisjong_arena`` plus ``tests/`` on
PYTHONPATH (the synthetic runner case uses the test fixtures). No model, game, Policy,
evaluation seed, or AWS. Cases (each runs in its own watchdog scope ``lp580-check-*.scope``):

  accounting     MemoryMax=8G. The guard resolves the exact scope; parent 32 MiB + child 96 MiB
                 are both counted by memory.peak / memory.current; watchdog final peak recorded
  transient      a child allocates 384 MiB and exits before the next check; memory.peak still
                 shows it and the guard STOPs (soft 256 MiB here, an OS check only)
  watchdog-soft  the watchdog stops a scope whose memory.peak > 256 MiB and quarantines outputs
  oom            MemoryMax=192M and a 512 MiB child: kernel OOM kill, watchdog "memory-oom",
                 outputs quarantined
  wrong-scope    the guard refuses a process outside a user scope, and a 4G scope
  runner         the formal runner with a v2 fixture lock (8 spawn workers, synthetic 600
                 hanchans): completes and writes a PASS memory diagnostic of the same scope
  runner-stop    the same, stopped by the watchdog wall: SIGTERM -> STOP / INVALID, nothing
                 published, no staging left, memory diagnostic STOP

The small thresholds only exercise the OS mechanics; the locked contract stays
6 GiB soft / 8 GiB hard.
"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

WATCHDOG = Path(__file__).with_name("learning-573-watchdog.py")
MIB = 1024 * 1024
GIB = 1024 * MIB


def _touch(size: int) -> bytearray:
    memory = bytearray(size)
    memory[::4096] = b"x" * len(range(0, size, 4096))
    return memory


def _write(path: Path, document: dict) -> None:
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")


def _guard(soft: int | None = None, *, wall: int = 60):
    from lisjong_arena.learning_candidate_engine_aabb import memory_guard as mg

    class Budget:
        def check(self, stage):
            pass

    document = mg.memory_guard_document()
    if soft is not None:
        document["soft_peak_bytes"] = soft
    return mg.CgroupMemoryGuard(
        document,
        {"timeout_seconds": wall, "wall_seconds": wall},
        Budget(),
        scope=mg.CgroupScope(),
        parent_peak_rss_kib=lambda: None,
    )


def _child(size: int, ready: Path | None, release: Path | None) -> None:
    memory = _touch(size)
    if ready is not None:
        ready.write_text(str(len(memory)))
        while not release.exists():
            time.sleep(0.05)


def _spawn_child(size: int, ready=None, release=None) -> subprocess.Popen:
    return subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "--dummy",
            "child",
            str(size),
            str(ready or ""),
            str(release or ""),
        ]
    )


def dummy_accounting(case: Path) -> None:
    guard = _guard()
    guard.start()
    parent = _touch(32 * MIB)
    ready, release = case / "child.ready", case / "child.release"
    child = _spawn_child(96 * MIB, ready, release)
    while not ready.exists():
        time.sleep(0.05)
    guard.check("both allocated")
    sample = guard.diagnostic(lock_digest="-", error=None)["last"]
    release.touch()
    child.wait()
    guard.check("after the child")
    _write(
        case / "dummy.json",
        {
            "allocated": len(parent) + int(ready.read_text()),
            "diagnostic": guard.diagnostic(lock_digest="-", error=None),
            "both_allocated": sample,
            "child_returncode": child.returncode,
        },
    )


def dummy_transient(case: Path) -> None:
    from lisjong_arena.heuristic_candidate_engine_aabb.executor import RunStopped

    guard = _guard(soft=256 * MIB)
    guard.start()
    _spawn_child(384 * MIB).wait()
    try:
        guard.check("after a transient child")
        stopped = None
    except RunStopped as error:
        stopped = str(error)
    _write(
        case / "dummy.json",
        {
            "diagnostic": guard.diagnostic(lock_digest="-", error=None),
            "stopped": stopped,
        },
    )


def dummy_hold(case: Path) -> None:
    memory = _touch(384 * MIB)
    _write(case / "dummy.json", {"pid": os.getpid(), "allocated": len(memory)})
    time.sleep(60)


def dummy_oom(case: Path) -> None:
    _write(case / "dummy.json", {"pid": os.getpid()})
    child = _spawn_child(512 * MIB)
    child.wait()
    time.sleep(10)


def dummy_guard_start(case: Path) -> None:
    from lisjong_arena.heuristic_candidate_engine_aabb.executor import RunStopped

    try:
        _guard().start()
        stopped = None
    except RunStopped as error:
        stopped = str(error)
    _write(
        case / "dummy.json",
        {"cgroup": Path("/proc/self/cgroup").read_text(), "stopped": stopped},
    )


def dummy_runner(case: Path, play_name: str) -> None:
    import _learning_engine_aabb_fixtures as fx

    from lisjong_arena.heuristic_candidate_engine_aabb.executor import (
        current_peak_rss_kib,
    )
    from lisjong_arena.learning_candidate_engine_aabb import runner

    result = runner.run_formal_event(
        case / "lock.json",
        ledger_path=case / "ledger.json",
        play=getattr(fx, play_name),
        live_gate=fx.gate,
        parent_peak_rss_kib=current_peak_rss_kib,
        on_hanchan=lambda done, total: (
            print(f"{done}/{total}", flush=True) if done % 100 == 0 else None
        ),
    )
    _write(case / "dummy.json", {"result_digest": result["result_digest"]})


def _stage(evidence: Path, case: str, wall: int, memory_max: str, *extra, runtime=60):
    directory = evidence / case
    directory.mkdir()
    command = [
        sys.executable,
        str(WATCHDOG),
        "run",
        "--stage",
        f"check-{case}",
        "--unit-prefix",
        "lp580",
        "--wall",
        str(wall),
        "--runtime-max",
        str(runtime),
        "--memory-max",
        memory_max,
        "--disk-limit-bytes",
        str(GIB),
        "--watch",
        str(evidence),
        "--disk-poll",
        "5",
        "--start-file",
        str(evidence / "start.json"),
        "--log-dir",
        str(evidence),
        *extra,
    ]
    return directory, command


def _run(command, directory, case, evidence, dummy_args):
    completed = subprocess.run(
        [
            *command,
            "--",
            sys.executable,
            str(Path(__file__).resolve()),
            "--dummy",
            *dummy_args,
            str(directory),
        ],
        check=False,
    )
    record = json.loads((evidence / f"check-{case}.watchdog.json").read_text())
    return completed.returncode, record


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(f"FAIL: {message}")


def check(evidence: Path) -> None:
    evidence.mkdir(parents=True, exist_ok=False)
    subprocess.run(
        [
            sys.executable,
            str(WATCHDOG),
            "init",
            "--start-file",
            str(evidence / "start.json"),
            "--limit-seconds",
            "900",
        ],
        check=True,
    )
    summary = {}
    soft6 = str(6 * GIB)

    # accounting
    case = "accounting"
    directory, command = _stage(
        evidence, case, 60, "8G", "--memory-soft-peak-bytes", soft6
    )
    code, record = _run(command, directory, case, evidence, [case])
    dummy = json.loads((directory / "dummy.json").read_text())
    sample = dummy["both_allocated"]
    scope = dummy["diagnostic"]["scope"]
    _require(code == 0 and record["stopped_by"] is None, f"{case}: watchdog {code}")
    _require(record["final_peak_recorded"], f"{case}: final peak")
    _require(
        record["memory_peak_path"] == f"/sys/fs/cgroup{scope['path']}/memory.peak",
        f"{case}: guard scope differs from the watchdog scope",
    )
    _require(scope["unit"] == record["unit"], f"{case}: unit")
    _require(sample["peak_bytes"] >= dummy["allocated"], f"{case}: peak < allocated")
    _require(
        sample["current_bytes"] >= dummy["allocated"], f"{case}: current < allocated"
    )
    _require(record["memory_peak_bytes"] >= sample["peak_bytes"], f"{case}: final")
    _require(record["memory_max_events_delta"] == 0, f"{case}: max events")
    summary[case] = {
        "allocated": dummy["allocated"],
        "current_bytes": sample["current_bytes"],
        "peak_bytes": sample["peak_bytes"],
        "stat": sample["stat"],
        "watchdog_final_peak_bytes": record["memory_peak_bytes"],
    }

    # transient
    case = "transient"
    directory, command = _stage(
        evidence, case, 60, "8G", "--memory-soft-peak-bytes", soft6
    )
    code, record = _run(command, directory, case, evidence, [case])
    dummy = json.loads((directory / "dummy.json").read_text())
    last = dummy["diagnostic"]["last"]
    _require(code == 0, f"{case}: watchdog {code}")
    _require(
        dummy["stopped"] is not None and "soft limit" in dummy["stopped"],
        f"{case}: guard did not stop: {dummy['stopped']}",
    )
    _require(last["current_bytes"] < 256 * MIB < last["peak_bytes"], f"{case}: peak")
    summary[case] = {
        "current_bytes": last["current_bytes"],
        "peak_bytes": last["peak_bytes"],
        "stopped": dummy["stopped"],
    }

    # watchdog-soft
    case = "watchdog-soft"
    directory, command = _stage(
        evidence,
        case,
        60,
        "8G",
        "--memory-soft-peak-bytes",
        str(256 * MIB),
        "--guard-output",
        str(evidence / case / "result.json"),
        "--guard-output",
        str(evidence / case / "raw"),
    )
    (directory / "result.json").write_text("{}")
    (directory / ".raw.staging-check").mkdir()
    code, record = _run(command, directory, case, evidence, ["hold"])
    pid = json.loads((directory / "dummy.json").read_text())["pid"]
    _require(code == 124 and record["stopped_by"] == "memory-soft", f"{case}: {code}")
    _require(
        sorted(Path(item["from"]).name for item in record["quarantined"])
        == [".raw.staging-check", "result.json"],
        f"{case}: quarantine {record['quarantined']}",
    )
    _require(not (directory / "result.json").exists(), f"{case}: output left")
    status = Path(f"/proc/{pid}/status")
    _require(not status.exists() or "State:\tZ" in status.read_text(), f"{case}: alive")
    summary[case] = {
        "peak_bytes": record["memory_peak_bytes"],
        "quarantined": len(record["quarantined"]),
        "stopped_by": record["stopped_by"],
    }

    # oom
    case = "oom"
    directory, command = _stage(
        evidence,
        case,
        60,
        "192M",
        "--memory-soft-peak-bytes",
        soft6,
        "--guard-output",
        str(evidence / case / "result.json"),
    )
    (directory / "result.json").write_text("{}")
    code, record = _run(command, directory, case, evidence, [case])
    _require(code != 0 and record["stopped_by"] == "memory-oom", f"{case}: {record}")
    _require(not (directory / "result.json").exists(), f"{case}: output left")
    summary[case] = {
        "events_last": record["memory_events_last"],
        "exit_code": code,
        "scope": record["scope"],
        "stopped_by": record["stopped_by"],
    }

    # wrong-scope: outside a user scope, and inside a 4G scope
    case = "wrong-scope"
    directory = evidence / case
    directory.mkdir()
    subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "--dummy",
            "guard-start",
            str(directory),
        ],
        check=True,
    )
    outside = json.loads((directory / "dummy.json").read_text())
    _require(outside["stopped"] is not None, f"{case}: outside scope accepted")
    directory4, command = _stage(evidence, "wrong-scope-4g", 60, "4G")
    code, record = _run(
        command, directory4, "wrong-scope-4g", evidence, ["guard-start"]
    )
    inside = json.loads((directory4 / "dummy.json").read_text())
    _require(code == 0, f"{case}: 4G stage {code}")
    _require(
        inside["stopped"] is not None and "MemoryMax" in inside["stopped"],
        f"{case}: 4G scope accepted: {inside['stopped']}",
    )
    summary[case] = {"outside": outside["stopped"], "scope_4g": inside["stopped"]}

    # runner / runner-stop
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tests"))
    import _learning_engine_aabb_fixtures as fx

    from lisjong_arena import seed_registry
    from lisjong_arena.learning_candidate_engine_aabb.lock import save_lock
    from lisjong_arena.learning_candidate_engine_aabb.result import verify_result

    wall = fx.LIMITS_V2["wall_seconds"]
    for case, play, watchdog_wall in (
        ("runner", "fake_play", 600),
        ("runner-stop", "fake_play_slow", 12),
    ):
        directory = evidence / case
        outputs = ("raw", "result.json", "observations.json")
        directory, command = _stage(
            evidence,
            case,
            watchdog_wall,
            "8G",
            "--memory-soft-peak-bytes",
            soft6,
            *(
                item
                for name in outputs
                for item in ("--guard-output", str(evidence / case / name))
            ),
            runtime=wall,
        )
        seed_registry.write_ledger(directory / "ledger.json", fx.ledger())
        lock = fx.lock_document_v2(directory)
        save_lock(lock, directory / "lock.json")
        code, record = _run(command, directory, case, evidence, ["runner", play])
        memory = [
            json.loads(path.read_text())
            for path in sorted((directory / "diagnostics").glob("memory-*.json"))
        ]
        _require(len(memory) == 1, f"{case}: memory diagnostics {len(memory)}")
        scope_path = memory[0]["scope"]["path"]
        _require(
            record["memory_peak_path"] == f"/sys/fs/cgroup{scope_path}/memory.peak",
            f"{case}: runner scope differs from the watchdog scope",
        )
        staging = [p.name for p in directory.iterdir() if "staging" in p.name]
        _require(staging == [], f"{case}: staging left {staging}")
        if case == "runner":
            _require(code == 0 and record["stopped_by"] is None, f"{case}: {code}")
            _require(memory[0]["status"] == "PASS", f"{case}: status")
            _require(
                memory[0]["sample_counts"]["after a hanchan"] == 600, f"{case}: count"
            )
            verify_result(
                lock_path=directory / "lock.json",
                raw_path=directory / "raw",
                result_path=directory / "result.json",
                observations_path=directory / "observations.json",
            )
        else:
            _require(code == 124 and record["stopped_by"] == "wall", f"{case}: {code}")
            _require(memory[0]["status"] == "STOP / INVALID", f"{case}: status")
            _require("SIGTERM" in memory[0]["error"], f"{case}: {memory[0]['error']}")
            for name in outputs:
                _require(not (directory / name).exists(), f"{case}: {name} published")
            stops = list((directory / "diagnostics").glob("stop-*.json"))
            _require(len(stops) == 1, f"{case}: stop documents {len(stops)}")
        summary[case] = {
            "max_peak_bytes": memory[0]["max_peak_bytes"],
            "quarantined": record["quarantined"],
            "rss_diagnostic_sum_kib": memory[0]["rss_diagnostic"][
                "worker_peak_rss_kib_sum"
            ],
            "sample_counts": memory[0]["sample_counts"],
            "status": memory[0]["status"],
            "stopped_by": record["stopped_by"],
            "watchdog_final_peak_bytes": record["memory_peak_bytes"],
        }

    _write(evidence / "summary.json", summary)
    (evidence / "PASS").write_text("cgroup memory guard OS checks PASS\n")
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--dummy":
        name, arguments = sys.argv[2], sys.argv[3:]
        if name == "child":
            size, ready, release = arguments
            _child(int(size), Path(ready) if ready else None, Path(release or "."))
        elif name == "runner":
            dummy_runner(Path(arguments[1]), arguments[0])
        else:
            {
                "accounting": dummy_accounting,
                "transient": dummy_transient,
                "hold": dummy_hold,
                "oom": dummy_oom,
                "guard-start": dummy_guard_start,
            }[name](Path(arguments[0]))
    else:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("directory", type=Path)
        check(parser.parse_args().directory.resolve())
