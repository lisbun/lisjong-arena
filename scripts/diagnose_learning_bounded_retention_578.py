"""#578: 親の保持量を合成fixtureで比べる診断（非対局・正式seedなし）。

```text
python scripts/diagnose_learning_bounded_retention_578.py [--counts 0 100 300 600]
```

半荘数ごと・方式ごとに新しいprocessで、Learning正式runの親が行う保存処理だけを
in-processで再現する。半荘は #513 の合成fixture（手書きscript）で、spawn worker・
実engine対局・実Policyは使わない。

- ``full``     変更前（36a2eac）: 全半荘の ``ExecutedHanchan`` をlistに保持し、
               完走後に ``write_kyoku_records`` / ``build_observations``
- ``bounded``  #578: 完了ごとに ``_CanonicalSink`` で射影し、captureを手放す。
               完了順は8 worker・``max_ahead`` = 32 を模した窓内の入れ替え

計測値は合成半荘についての値で、実engineの ``CompletedMatch`` / 局evidenceの
大きさや、正式runのRSSを表さない。
"""

import argparse
import gc
import json
import random
import shutil
import subprocess
import sys
import tempfile
import tracemalloc
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKERS = 8
MAX_AHEAD = 4 * WORKERS


def _rss_kib() -> tuple[int | None, int | None]:
    """このprocessの (current, peak) RSS（KiB）。"""
    status = Path("/proc/self/status")
    if status.exists():
        values = {}
        for line in status.read_text().splitlines():
            name, _, rest = line.partition(":")
            if name in ("VmRSS", "VmHWM"):
                values[name] = int(rest.split()[0])
        return values.get("VmRSS"), values.get("VmHWM")
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        current = ctypes.windll.kernel32.GetCurrentProcess
        current.restype = wintypes.HANDLE
        query = ctypes.windll.psapi.GetProcessMemoryInfo
        query.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        if not query(current(), ctypes.byref(counters), counters.cb):
            return None, None
        return counters.WorkingSetSize // 1024, counters.PeakWorkingSetSize // 1024
    return None, None


def _completion_order(count: int) -> list[int]:
    """窓（MAX_AHEAD）内で入れ替えた完了順。先頭が遅れても窓を越えない。"""
    rng = random.Random(578)
    order = []
    for start in range(0, count, MAX_AHEAD):
        block = list(range(start, min(start + MAX_AHEAD, count)))
        rng.shuffle(block)
        order.extend(block)
    return order


def _measure(mode: str, count: int) -> dict:
    sys.path.insert(0, str(ROOT / "tests"))
    import _learning_engine_aabb_fixtures as fx

    from lisjong_arena.heuristic_candidate_engine_aabb.executor import (
        ExecutedHanchan,
    )
    from lisjong_arena.heuristic_candidate_engine_aabb.record_store import (
        HanchanCapture,
        KyokuRecordStream,
        write_kyoku_records,
    )
    from lisjong_arena.learning_candidate_engine_aabb.protocol import LAYOUT
    from lisjong_arena.learning_candidate_engine_aabb.result import (
        build_observations,
        observation_row,
        observations_document,
        record_bindings,
    )
    from lisjong_arena.learning_candidate_engine_aabb.runner import _CanonicalSink

    keys = [(seed, placement) for seed in fx.SEEDS for placement in range(6)][:count]

    def executed(index: int) -> ExecutedHanchan:
        seed, placement = keys[index]
        return ExecutedHanchan(
            fx.capture(seed, placement), 0.5, 2048, None, dict(fx.ANALYSIS)
        )

    tmp = Path(tempfile.mkdtemp())
    try:
        bindings = record_bindings(fx.lock_document(tmp))
        lock = fx.lock_document(tmp)
        gc.collect()
        tracemalloc.start()
        before = _rss_kib()
        buffered = 0
        if mode == "full":
            kept = [executed(index) for index in range(count)]
        else:
            stream = KyokuRecordStream(
                tmp / "raw",
                producer_revision=fx.ARENA_REVISION,
                event_facts_required=False,
                bindings=bindings,
                layout=LAYOUT,
                capture_type=HanchanCapture,
            )
            sink = _CanonicalSink(stream)
            for index in _completion_order(count):
                sink(index, executed(index))
        gc.collect()
        run_current, run_peak = tracemalloc.get_traced_memory()
        live = sum(type(item) is HanchanCapture for item in gc.get_objects())
        after_run = _rss_kib()
        staged = list((tmp / "raw").iterdir()) if (tmp / "raw").exists() else []
        staged_bytes = sum(path.stat().st_size for path in staged)
        if count:
            if mode == "full":
                write_kyoku_records(
                    tmp / "raw",
                    [item.capture for item in kept],
                    producer_revision=fx.ARENA_REVISION,
                    event_facts_required=False,
                    bindings=bindings,
                    layout=LAYOUT,
                    capture_type=HanchanCapture,
                )
                rows = [observation_row(item) for item in kept]
                complete = lambda: build_observations(lock, kept)  # noqa: E731
            else:
                stream.finish()
                rows = sink.observations
                buffered = sink.max_buffered
                complete = lambda: observations_document(lock, rows)  # noqa: E731
            assert len(rows) == count
            # 観測documentは全600半荘を要求するので、600半荘のときだけ作る
            if count == 6 * len(fx.SEEDS):
                complete()
        _, total_peak = tracemalloc.get_traced_memory()
        after_save = _rss_kib()
        tracemalloc.stop()
        return {
            "count": count,
            "live_captures_after_run": live,
            "mode": mode,
            "reorder_buffer_max": buffered,
            "rss_kib": {
                "before": before,
                "after_run": after_run,
                "after_save": after_save,
            },
            "staged_bytes_after_run": staged_bytes,
            "staged_files_after_run": len(staged),
            "tracemalloc_bytes": {
                "run_current": run_current,
                "run_peak": run_peak,
                "peak_including_save": total_peak,
            },
        }
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--counts", type=int, nargs="+", default=[0, 100, 300, 600])
    parser.add_argument("--one", nargs=2, metavar=("MODE", "COUNT"))
    args = parser.parse_args(argv)
    if args.one:
        print(json.dumps(_measure(args.one[0], int(args.one[1]))))
        return 0
    rows = []
    for count in args.counts:
        for mode in ("full", "bounded"):
            output = subprocess.check_output(
                [sys.executable, __file__, "--one", mode, str(count)], text=True
            )
            rows.append(json.loads(output))
    print(json.dumps(rows, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
