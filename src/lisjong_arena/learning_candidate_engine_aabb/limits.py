"""正式eventの資源上限の強制（#565）。

既存の開発実験（#520 / #524 / #539等）と同じ三重方式にそろえる。

1. **OS（process tree）**: ``systemd-run --user --scope -p MemoryMax=<peak_rss_kib>K
   -p MemorySwapMax=0 -p OOMPolicy=kill -p RuntimeMaxSec=<wall_seconds>`` の中で ``run``
   する。spawn workerは同じcgroupへ入るので、親 + workerにkernelの上限が効く。
   ``require_os_limits`` が開始時に、自processのcgroup v2とunitの設定がlockの上限と
   一致することを確かめる（読めない・不一致はSTOP）
2. **外側のrealtime watchdog**: scopeの外で実時間を見て期限後にscopeを止める（operator手順。
   codeからは確認できない）
3. **code**: ``EventBudget`` が ``run`` の開始直後から公開直前までをmonotonicとrealtimeの
   大きい方で計り、各半荘の待ち時間を残り時間で切り詰める。メモリは親 + workerのpeak RSSの
   和を半荘ごとと公開直前に確かめる

codeの確認は同期処理（live gateのモデルload等）の途中では割り込めない。その間の上限は
1のOS上限が正本である。
"""

import re
import subprocess
import time
from collections.abc import Callable
from pathlib import Path, PurePosixPath

from lisjong_arena.heuristic_candidate_engine_aabb.executor import RunStopped

_TIMESPAN_UNITS = {
    "w": 604_800.0,
    "d": 86_400.0,
    "h": 3_600.0,
    "min": 60.0,
    "s": 1.0,
    "ms": 1e-3,
    "us": 1e-6,
}
_TIMESPAN_TOKEN = re.compile(r"(\d+(?:\.\d+)?)(w|d|h|min|ms|us|s)")


def parse_systemd_timespan(text: str) -> float | None:
    """``systemctl show`` のtimespan（例 ``1h 40min``）を秒にする。``infinity`` は ``None``。"""
    text = text.strip()
    if text == "infinity":
        return None
    tokens = text.split()
    if not tokens:
        raise RunStopped(f"cannot parse the systemd timespan {text!r}")
    total = 0.0
    for token in tokens:
        match = _TIMESPAN_TOKEN.fullmatch(token)
        if match is None:
            raise RunStopped(f"cannot parse the systemd timespan {text!r}")
        total += float(match.group(1)) * _TIMESPAN_UNITS[match.group(2)]
    return total


def _cgroup_limit(path: Path) -> int | None:
    text = path.read_text(encoding="ascii").strip()
    return None if text == "max" else int(text)


def probe_os_limits(
    *,
    proc_cgroup: Path = Path("/proc/self/cgroup"),
    cgroup_root: Path = Path("/sys/fs/cgroup"),
    systemctl: Callable[[str], str] | None = None,
) -> dict[str, object]:
    """自processのcgroup v2の ``memory.max`` / ``memory.swap.max`` とunitの ``RuntimeMaxUSec``。

    値が無制限なら ``None``。読めなければ ``RunStopped``（fail closed）。
    """
    try:
        entries = [
            line.split(":", 2)
            for line in proc_cgroup.read_text(encoding="ascii").splitlines()
        ]
        unified = [entry[2] for entry in entries if entry[:2] == ["0", ""]]
        if len(unified) != 1:
            raise RunStopped("the process is not in a single cgroup v2 hierarchy")
        relative = PurePosixPath(unified[0])
        directory = cgroup_root.joinpath(*relative.parts[1:])
        memory_max = _cgroup_limit(directory / "memory.max")
        swap_max = _cgroup_limit(directory / "memory.swap.max")
        unit = relative.name
        show = systemctl or _systemctl_runtime_max
        runtime_max = parse_systemd_timespan(show(unit))
    except RunStopped:
        raise
    except Exception as error:
        raise RunStopped(
            f"cannot read the OS resource limits: {type(error).__name__}: {error}"
        ) from None
    return {
        "memory_max_bytes": memory_max,
        "memory_swap_max_bytes": swap_max,
        "runtime_max_seconds": runtime_max,
        "unit": unit,
    }


def _systemctl_runtime_max(unit: str) -> str:
    completed = subprocess.run(
        ["systemctl", "--user", "show", "--property=RuntimeMaxUSec", "--value", unit],
        capture_output=True,
        check=True,
        text=True,
        timeout=30,
    )
    return completed.stdout


def require_os_limits(limits: dict, observed: dict) -> None:
    """OSの上限がlockの上限と一致することを要求する（#565）。"""
    expected_memory = limits["peak_rss_kib"] * 1024
    unit = observed.get("unit")
    if observed.get("memory_max_bytes") != expected_memory:
        raise RunStopped(
            f"cgroup memory.max of {unit} is {observed.get('memory_max_bytes')}, "
            f"not the locked {expected_memory} bytes (MemoryMax={limits['peak_rss_kib']}K)"
        )
    if observed.get("memory_swap_max_bytes") != 0:
        raise RunStopped(
            f"cgroup memory.swap.max of {unit} is "
            f"{observed.get('memory_swap_max_bytes')}, not 0 (MemorySwapMax=0)"
        )
    if observed.get("runtime_max_seconds") != float(limits["wall_seconds"]):
        raise RunStopped(
            f"RuntimeMaxSec of {unit} is {observed.get('runtime_max_seconds')}, "
            f"not the locked {limits['wall_seconds']} s"
        )


class EventBudget:
    """eventの全体wall。monotonicとrealtimeの経過の大きい方で計る（#517の時計差）。"""

    def __init__(
        self,
        wall_seconds: int,
        *,
        clock: Callable[[], float] = time.monotonic,
        realtime: Callable[[], float] = time.time,
        started: tuple[float, float] | None = None,
    ) -> None:
        """``started`` は先に読んだ ``(clock(), realtime())``。lockを読む前から計るために使う。"""
        self._wall = wall_seconds
        self._clock = clock
        self._realtime = realtime
        self._clock_start, self._realtime_start = (
            (clock(), realtime()) if started is None else started
        )

    def elapsed(self) -> float:
        return max(
            self._clock() - self._clock_start,
            self._realtime() - self._realtime_start,
        )

    def remaining(self) -> float:
        return self._wall - self.elapsed()

    def check(self, stage: str) -> None:
        elapsed = self.elapsed()
        if elapsed > self._wall:
            raise RunStopped(
                f"wall time {elapsed:.0f} s exceeded the locked {self._wall} s "
                f"({stage})"
            )


def require_memory(
    limits: dict, parent_peak_kib: int | None, worker_peak_kib: int | None, stage: str
) -> None:
    """親 + workerのpeak RSSの和がlockの上限以内であることを要求する。"""
    if parent_peak_kib is None or worker_peak_kib is None:
        raise RunStopped("peak RSS cannot be measured on this platform")
    total = parent_peak_kib + worker_peak_kib
    if total > limits["peak_rss_kib"]:
        raise RunStopped(
            f"peak RSS {total} KiB (parent {parent_peak_kib} + worker "
            f"{worker_peak_kib}) exceeded the locked {limits['peak_rss_kib']} KiB "
            f"({stage})"
        )


class ResourceGuard:
    """全体wallと親 + workerのpeak RSSの上限を確かめる（超えたらevent全体を止める）。

    正式run（#562）と開発準備入口（#567）が同じものを使う。
    """

    def __init__(
        self,
        limits: dict,
        budget: EventBudget,
        parent_peak_rss_kib: Callable[[], int | None],
    ) -> None:
        self._limits = limits
        self._budget = budget
        self._parent = parent_peak_rss_kib

    def __call__(self, item) -> None:
        """1半荘ごと（``run_tasks`` の ``on_executed``）。"""
        self._budget.check("after a hanchan")
        require_memory(
            self._limits, self._parent(), item.peak_rss_kib, "after a hanchan"
        )

    def before_publish(self, executed) -> None:
        """公開（rename）の直前。保存・集計を含めた全体wallと親のpeak RSSを確かめる。"""
        self._budget.check("before publishing")
        peaks = [item.peak_rss_kib for item in executed]
        worker = None if None in peaks else max(peaks, default=0)
        require_memory(self._limits, self._parent(), worker, "before publishing")


__all__ = [
    "EventBudget",
    "ResourceGuard",
    "parse_systemd_timespan",
    "probe_os_limits",
    "require_memory",
    "require_os_limits",
]
