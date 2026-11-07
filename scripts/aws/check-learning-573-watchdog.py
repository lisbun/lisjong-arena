"""Linux/systemd parent-child acceptance check; no model, game, or evaluation seed."""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

WATCHDOG = Path(__file__).with_name("learning-573-watchdog.py")


def dummy(directory: Path, role: str, wait: bool) -> None:
    memory = bytearray(b"x" * (96 if role == "child" else 32) * 1024 * 1024)
    (directory / f"{role}.json").write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "cgroup": Path("/proc/self/cgroup").read_text(),
                "allocation": len(memory),
            }
        )
    )
    if role == "child":
        time.sleep(300 if wait else 3)
        return
    child = subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            str(directory),
            "--dummy",
            "child",
            *(["--wait"] if wait else []),
        ]
    )
    child.wait()


def check(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=False)
    subprocess.run(
        [
            sys.executable,
            str(WATCHDOG),
            "init",
            "--start-file",
            str(directory / "start.json"),
            "--limit-seconds",
            "120",
        ],
        check=True,
    )
    for name, wall, wait in (("peak", 30, False), ("wall", 5, True)):
        case = directory / name
        case.mkdir()
        result = subprocess.run(
            [
                sys.executable,
                str(WATCHDOG),
                "run",
                "--stage",
                f"check-{name}",
                "--wall",
                str(wall),
                "--runtime-max",
                "60",
                "--memory-max",
                "512M",
                "--disk-limit-bytes",
                "10485760",
                "--watch",
                str(directory),
                "--disk-poll",
                "1",
                "--start-file",
                str(directory / "start.json"),
                "--log-dir",
                str(directory),
                "--",
                sys.executable,
                str(Path(__file__).resolve()),
                str(case),
                "--dummy",
                "parent",
                *(["--wait"] if wait else []),
            ],
            check=False,
        )
        record = json.loads((directory / f"check-{name}.watchdog.json").read_text())
        parent, child = [
            json.loads((case / f"{role}.json").read_text())
            for role in ("parent", "child")
        ]
        assert parent["cgroup"] == child["cgroup"]
        cgroup = parent["cgroup"].strip().split("::", 1)[1]
        assert cgroup.endswith("/" + record["unit"])
        assert record["memory_peak_path"] == f"/sys/fs/cgroup{cgroup}/memory.peak"
        assert record["memory_peak_bytes"] >= parent["allocation"] + child["allocation"]
        if name == "peak":
            assert result.returncode == 0 and record["final_peak_recorded"]
            assert record["stopped_by"] is None
        else:
            assert result.returncode == 124 and record["stopped_by"] == "wall"
        for process in (parent, child):
            status = Path(f"/proc/{process['pid']}/status")
            assert not status.exists() or "State:\tZ" in status.read_text()
    (directory / "PASS").write_text(
        "scope attribution, final peak, and parent-child stop PASS\n"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--dummy", choices=("parent", "child"))
    parser.add_argument("--wait", action="store_true")
    args = parser.parse_args()
    if args.dummy:
        dummy(args.directory, args.dummy, args.wait)
    else:
        check(args.directory.resolve())
