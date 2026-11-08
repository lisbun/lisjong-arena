"""Learning正式評価のcgroup memory guard（#580、lock v2）。

lock v2の ``execution.memory_guard`` は ``cgroup_peak_v1`` で、値は下の定数と完全一致する。

- soft STOP: runnerが属するexact systemd scope（cgroup v2）の ``memory.peak`` が
  ``SOFT_PEAK_BYTES`` を**超えたら**STOP / INVALID（ちょうど同じ値はSTOPしない）
- hard cap: 同じscopeの ``memory.max = HARD_MAX_BYTES``、``memory.swap.max = 0``、
  systemdの ``MemoryMax`` / ``MemorySwapMax`` / ``OOMPolicy=kill`` / ``RuntimeMaxUSec`` を照合する
- ``memory.events`` の ``oom`` / ``oom_kill`` / ``oom_group_kill`` が開始時から増えたらSTOP。
  ``max`` の増分はSTOPにせず、警告として件数を記録する（回収に成功しても増えるため）
- scope消滅・path / inode不一致・limit変化・読めない値・非整数・counterの減少はSTOP
- ``parent ru_maxrss + sum(worker PID ru_maxrss)`` は記録するがSTOPには使わない

判定はrunnerの同期点（開始時・周期poll・各半荘・保存後・公開直前・公開後）だけで行う。
runnerの同期処理の途中は外側watchdogが同じscopeを監視し、OSの ``memory.max`` が最終保護である。
記録は ``diagnostics/memory-<UTC>.json``（``not_a_result``）で、科学的な保存物には入れない。
"""

import datetime
import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from lisjong_arena._artifact_io import canonical_json_text, write_new_artifact_file
from lisjong_arena.heuristic_candidate_engine_aabb.executor import RunStopped

from .limits import parse_systemd_timespan

KIND = "cgroup_peak_v1"
SOFT_PEAK_BYTES = 6 * 1024**3
HARD_MAX_BYTES = 8 * 1024**3
SWAP_MAX_BYTES = 0
STOP_EVENTS = ("oom", "oom_group_kill", "oom_kill")
EVENT_KEYS = ("high", "low", "max", "oom", "oom_group_kill", "oom_kill")
STAT_KEYS = ("anon", "file", "kernel")
POLL_SECONDS = 5
FORMAL_WORKERS = 8
MEMORY_SCHEMA = "arena-learning-candidate-engine-aabb-memory-v1"
RSS_BASIS = (
    "parent ru_maxrss + sum of each worker PID's ru_maxrss; diagnostic only, "
    "not a STOP condition"
)
SYSTEMD_PROPERTIES = (
    "ActiveState",
    "ControlGroup",
    "MemoryMax",
    "MemorySwapMax",
    "OOMPolicy",
    "RuntimeMaxUSec",
)
_COUNTED_STAGES = ("after a hanchan", "poll")


def memory_guard_document() -> dict[str, object]:
    """lock v2の ``execution.memory_guard``（#580で承認された値）。"""
    return {
        "hard_max_bytes": HARD_MAX_BYTES,
        "kind": KIND,
        "poll_seconds": POLL_SECONDS,
        "rss_sum": "diagnostic_only",
        "scope_policy": "exact_systemd_scope_v1",
        "soft_peak_bytes": SOFT_PEAK_BYTES,
        "stop_comparison": "memory.peak > soft_peak_bytes",
        "stop_events": list(STOP_EVENTS),
        "swap_max_bytes": SWAP_MAX_BYTES,
    }


def is_memory_guard_document(value: object) -> bool:
    """型も含めて完全一致するか（``True == 1`` などを通さない）。"""
    if type(value) is not dict:
        return False
    try:
        return canonical_json_text(value) == canonical_json_text(
            memory_guard_document()
        )
    except Exception:
        return False


def _utc_now() -> str:
    return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _decimal(text: str, context: str) -> int:
    if not text.isascii() or not text.isdecimal():
        raise RunStopped(f"{context} is not a non-negative integer: {text!r}")
    return int(text)


def _read_int(path: Path) -> int:
    return _decimal(path.read_text(encoding="ascii").strip(), path.name)


def _read_keyed(path: Path, required: tuple[str, ...]) -> dict[str, int]:
    """``key value`` 形式（memory.events / memory.stat）。重複・非整数・必須keyの欠落はSTOP。"""
    values: dict[str, int] = {}
    for line in path.read_text(encoding="ascii").splitlines():
        fields = line.split()
        if len(fields) != 2 or fields[0] in values:
            raise RunStopped(f"{path.name} is malformed: {line!r}")
        values[fields[0]] = _decimal(fields[1], f"{path.name} {fields[0]}")
    missing = [key for key in required if key not in values]
    if missing:
        raise RunStopped(f"{path.name} lacks {missing}")
    return values


def systemctl_show(unit: str, properties: tuple[str, ...]) -> dict[str, str]:
    completed = subprocess.run(
        [
            "systemctl",
            "--user",
            "show",
            unit,
            *(f"--property={name}" for name in properties),
        ],
        capture_output=True,
        check=True,
        text=True,
        timeout=30,
    )
    shown: dict[str, str] = {}
    for line in completed.stdout.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            shown[key] = value
    return shown


@dataclass(frozen=True, slots=True)
class ScopeIdentity:
    path: str
    unit: str
    inode: int
    device: int

    def document(self) -> dict[str, object]:
        return {
            "device": self.device,
            "inode": self.inode,
            "path": self.path,
            "unit": self.unit,
        }


class CgroupScope:
    """自processのexact systemd scope（cgroup v2）を読む。読めなければ ``RunStopped``。

    ``proc_cgroup`` / ``cgroup_root`` / ``show`` はtestで差し替えるseam。
    """

    def __init__(
        self,
        *,
        proc_cgroup: Path = Path("/proc/self/cgroup"),
        cgroup_root: Path = Path("/sys/fs/cgroup"),
        show: Callable[[str, tuple[str, ...]], dict[str, str]] = systemctl_show,
        hard_max_bytes: int = HARD_MAX_BYTES,
        swap_max_bytes: int = SWAP_MAX_BYTES,
    ) -> None:
        self._proc = proc_cgroup
        self._root = cgroup_root
        self._show = show
        self._hard = hard_max_bytes
        self._swap = swap_max_bytes

    def _own_path(self) -> str:
        unified = [
            line[3:]
            for line in self._proc.read_text(encoding="ascii").splitlines()
            if line.startswith("0::")
        ]
        if len(unified) != 1:
            raise RunStopped("the process is not in a single cgroup v2 hierarchy")
        path = PurePosixPath(unified[0])
        if not path.is_absolute() or ".." in path.parts or len(path.parts) < 2:
            raise RunStopped(f"unexpected cgroup v2 path {unified[0]!r}")
        return unified[0]

    def _directory(self, path: str) -> Path:
        return self._root.joinpath(*PurePosixPath(path).parts[1:])

    def _require_limits(self, directory: Path) -> None:
        memory_max = (directory / "memory.max").read_text(encoding="ascii").strip()
        swap_max = (directory / "memory.swap.max").read_text(encoding="ascii").strip()
        if memory_max != str(self._hard):
            raise RunStopped(
                f"cgroup memory.max is {memory_max}, not the locked {self._hard} bytes"
            )
        if swap_max != str(self._swap):
            raise RunStopped(
                f"cgroup memory.swap.max is {swap_max}, not the locked {self._swap}"
            )

    def resolve(self, runtime_max_seconds: float) -> ScopeIdentity:
        try:
            return self._resolve(runtime_max_seconds)
        except RunStopped:
            raise
        except Exception as error:
            raise RunStopped(
                f"cannot verify the cgroup memory scope: {type(error).__name__}: {error}"
            ) from None

    def _resolve(self, runtime_max_seconds: float) -> ScopeIdentity:
        path = self._own_path()
        unit = PurePosixPath(path).name
        if not unit.endswith(".scope"):
            raise RunStopped(f"the process is not in a systemd scope: {path}")
        shown = self._show(unit, SYSTEMD_PROPERTIES)
        expected = {
            "ActiveState": "active",
            "ControlGroup": path,
            "MemoryMax": str(self._hard),
            "MemorySwapMax": str(self._swap),
            "OOMPolicy": "kill",
        }
        for name, value in expected.items():
            if shown.get(name) != value:
                raise RunStopped(
                    f"systemd {name} of {unit} is {shown.get(name)!r}, not {value!r}"
                )
        runtime = parse_systemd_timespan(shown.get("RuntimeMaxUSec", ""))
        if runtime != float(runtime_max_seconds):
            raise RunStopped(
                f"RuntimeMaxSec of {unit} is {runtime}, not the locked "
                f"{runtime_max_seconds} s"
            )
        directory = self._directory(path)
        controllers = (
            (directory / "cgroup.controllers").read_text(encoding="ascii").split()
        )
        if "memory" not in controllers:
            raise RunStopped(f"the memory controller is not enabled for {unit}")
        self._require_limits(directory)
        status = os.stat(directory)
        return ScopeIdentity(path, unit, status.st_ino, status.st_dev)

    def sample(self, identity: ScopeIdentity) -> dict[str, object]:
        try:
            return self._sample(identity)
        except RunStopped:
            raise
        except Exception as error:
            raise RunStopped(
                f"cannot read the cgroup memory scope: {type(error).__name__}: {error}"
            ) from None

    def _sample(self, identity: ScopeIdentity) -> dict[str, object]:
        path = self._own_path()
        if path != identity.path:
            raise RunStopped(f"the process moved from {identity.path} to {path}")
        directory = self._directory(path)
        status = os.stat(directory)
        if (status.st_ino, status.st_dev) != (identity.inode, identity.device):
            raise RunStopped(f"the cgroup scope {identity.unit} was replaced")
        self._require_limits(directory)
        stat = _read_keyed(directory / "memory.stat", ("anon", "file"))
        return {
            "current_bytes": _read_int(directory / "memory.current"),
            "events": _read_keyed(directory / "memory.events", EVENT_KEYS),
            "peak_bytes": _read_int(directory / "memory.peak"),
            "stat": {key: stat.get(key) for key in STAT_KEYS},
            "time": _utc_now(),
        }


class CgroupMemoryGuard:
    """lock v2のmemory guard。全体wallも確かめる（v1の ``ResourceGuard`` と同じ位置で使う）。"""

    def __init__(
        self,
        memory_guard: dict,
        limits: dict,
        budget,
        *,
        scope,
        parent_peak_rss_kib: Callable[[], int | None],
    ) -> None:
        self._soft = memory_guard["soft_peak_bytes"]
        self._memory_guard = memory_guard
        self._limits = limits
        self._budget = budget
        self._scope = scope
        self._parent = parent_peak_rss_kib
        self._identity: ScopeIdentity | None = None
        self._baseline: dict | None = None
        self._last: dict | None = None
        self._max_peak = 0
        self._max_events_delta = 0
        self._checkpoints: list[dict] = []
        self._counts = dict.fromkeys(_COUNTED_STAGES, 0)
        self._worker_peaks: dict[int, int] = {}
        self._parent_peak: int | None = None

    def start(self) -> None:
        """exact scopeとOS上限を確かめ、``memory.events`` のbaselineを取る。"""
        self._identity = self._scope.resolve(float(self._limits["wall_seconds"]))
        sample = self._scope.sample(self._identity)
        self._baseline = sample
        self._judge("start", sample)

    def check(self, stage: str) -> None:
        if self._identity is None or self._baseline is None:
            raise RunStopped("the cgroup memory guard was not started")
        self._judge(stage, self._scope.sample(self._identity))

    def poll(self) -> None:
        self.check("poll")

    def _judge(self, stage: str, sample: dict) -> None:
        previous, self._last = self._last, sample
        if stage in self._counts:
            self._counts[stage] += 1
        else:
            self._checkpoints.append({"sample": sample, "stage": stage})
        peak = sample["peak_bytes"]
        if previous is not None and peak < previous["peak_bytes"]:
            raise RunStopped(f"cgroup memory.peak decreased ({stage})")
        self._max_peak = max(self._max_peak, peak)
        baseline = self._baseline["events"]
        events = sample["events"]
        for key in EVENT_KEYS:
            if events[key] < baseline[key]:
                raise RunStopped(f"cgroup memory.events {key} decreased ({stage})")
        for key in STOP_EVENTS:
            if events[key] > baseline[key]:
                raise RunStopped(
                    f"cgroup memory.events {key} increased by "
                    f"{events[key] - baseline[key]} ({stage})"
                )
        self._max_events_delta = events["max"] - baseline["max"]
        if peak > self._soft:
            raise RunStopped(
                f"cgroup memory.peak {peak} bytes exceeded the locked soft limit "
                f"{self._soft} bytes ({stage})"
            )

    def __call__(self, item) -> None:
        """1半荘ごと。RSSは診断として記録するだけ。"""
        self._budget.check("after a hanchan")
        if item.worker_pid is None or item.peak_rss_kib is None:
            raise RunStopped("parallel worker identity or peak RSS unavailable")
        self._worker_peaks[item.worker_pid] = max(
            item.peak_rss_kib, self._worker_peaks.get(item.worker_pid, 0)
        )
        self._parent_peak = self._parent()
        self.check("after a hanchan")

    def before_publish(self, measured) -> None:
        self._budget.check("before publishing")
        self._parent_peak = self._parent()
        self.check("before publishing")

    def after_publish(self) -> None:
        """公開rename後の最終readback。失敗したら公開を巻き戻す（caller）。"""
        self.check("after publishing")

    def diagnostic(
        self, *, lock_digest: str, error: BaseException | None
    ) -> dict[str, object]:
        return {
            "baseline": self._baseline,
            "checkpoints": self._checkpoints,
            "error": None if error is None else f"{type(error).__name__}: {error}",
            "last": self._last,
            "lock_digest": lock_digest,
            "max_events_delta": self._max_events_delta,
            "max_peak_bytes": self._max_peak,
            "memory_guard": self._memory_guard,
            "not_a_result": True,
            "rss_diagnostic": {
                "basis": RSS_BASIS,
                "parent_peak_rss_kib": self._parent_peak,
                "worker_peak_rss_kib": {
                    str(pid): peak for pid, peak in sorted(self._worker_peaks.items())
                },
                "worker_peak_rss_kib_sum": sum(self._worker_peaks.values()),
            },
            "sample_counts": dict(self._counts),
            "schema": MEMORY_SCHEMA,
            "scope": None if self._identity is None else self._identity.document(),
            "status": "PASS" if error is None else "STOP / INVALID",
        }


def write_memory_diagnostic(directory: Path, document: dict) -> Path:
    directory.mkdir(exist_ok=True)
    stamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%S%fZ")
    path = directory / f"memory-{stamp}.json"
    write_new_artifact_file(path, canonical_json_text(document))
    return path


__all__ = [
    "FORMAL_WORKERS",
    "HARD_MAX_BYTES",
    "KIND",
    "MEMORY_SCHEMA",
    "SOFT_PEAK_BYTES",
    "STOP_EVENTS",
    "CgroupMemoryGuard",
    "CgroupScope",
    "ScopeIdentity",
    "is_memory_guard_document",
    "memory_guard_document",
    "systemctl_show",
    "write_memory_diagnostic",
]
