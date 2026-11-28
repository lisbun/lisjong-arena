"""通常ツモcaptureの実行費用の測定（#600、計画承認後にだけ実行する）。

```text
parent（監視thread: 1秒ごとに全processのVmRSS合計・一時disk・wall timeを見る）
  C0 worker（長く動く1 process）  run_policy_hanchan            -> CompletedMatchを保持
  C1 worker（長く動く1 process）  capture_live_draw_hanchan     -> CompletedMatch + CapturedHanchanを保持
  段階1: 24組（seed 591000..591003 × placement 0..5）を交互順で実行（組iが偶数ならC0→C1）
         組ごとに同一性digest（CompletedMatch投影・selector回数・Policy選択列・delivery fact）を照合
         最初の1組のあとに、C1の1半荘分で段階2を試し、全体の所要時間を保守的に見積もる
  段階2: C1 workerが保持する24半荘分で S（canonical serialization）、
         W（write_live_draw_capture。内部でSとstaging / 公開後の2回のreadbackを含む）、
         R（read_live_draw_capture 1回）を測る
```

- 強さの比較ではない。得点・順位は同一性digestの入力にだけ使い、workerの外へ出さない。
  補正値・κ・r は計算しない（#591の再評価を防ぐ）
- captureの中身（全員の手牌）は、permission 0700の一時directoryにだけ書き、成功・失敗・STOPの
  いずれでも削除して残存を確認する。workerから親へ渡すのは数値とdigestだけで、worker内の例外も
  型名だけを渡す（例外messageに牌が含まれ得るため）
- 不一致・timeout・上限超過・監視不能・worker終了のいずれでも、その場で止める。再試行・seed追加・
  条件変更はしない。途中までの値は ``status`` を付けて記録する
- S / W / R は包含関係がある（WはSと2回のreadbackを含む）。合計して二重計上しない
- ``VmHWM`` はprocess生涯のpeakで、段階別のpeakではない。1秒ごとのVmRSS監視はsampledで、
  瞬間的なpeakを見逃し得る（厳密なhard capではない）
- 実producer全体（``build_records``・結果JSON・paired manifest・補正）のメモリは測らない
"""

from __future__ import annotations

import argparse
import dataclasses
import enum
import gzip
import hashlib
import json
import multiprocessing
import os
import platform
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter
from fractions import Fraction
from pathlib import Path

OWNER_ISSUE = "lisbun/lisjong-arena#600"
REPORT_SCHEMA = "arena-live-draw-capture-cost-report-v1"
SEEDS = (591_000, 591_001, 591_002, 591_003)
PLACEMENTS = tuple(range(6))
C0 = "C0"
C1 = "C1"
GIB_KIB = 1_048_576
LIMITS = {
    "wall_seconds": 3 * 3600,
    "rss_kib": 4 * GIB_KIB,
    "disk_bytes": 2 * 1024**3,
    "hanchan_timeout_seconds": 15 * 60,
}
PROJECTION_SAFETY = 1.25
POLL_SECONDS = 1.0
EXTRAPOLATION_HANCHANS = (360, 3000)
EXTRAPOLATION_SAFETY = 2
ACCESS = "measurement executor only (lisbun/lisjong-arena#600)"
RETENTION = "deleted at the end of the lisbun/lisjong-arena#600 measurement"
THREAD_ENV = (
    "OMP_NUM_THREADS",
    "MKL_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "RAYON_NUM_THREADS",
    "PYTHONHASHSEED",
)

STATUS_COMPLETED = "COMPLETED"
STATUS_INVALID = "STOPPED-INVALID"
STATUS_LIMIT = "STOPPED-LIMIT"
STATUS_BUDGET = "NOT-COMPLETED-BUDGET"
STATUS_FAILED = "STOPPED-FAILED"


class MeasurementStop(Exception):
    """測定を止める理由（``status`` は報告に使う分類）。"""

    def __init__(self, status: str, reason: str) -> None:
        super().__init__(f"{status}: {reason}")
        self.status = status
        self.reason = reason


# ---------------------------------------------------------------------------
# 決定的な投影とdigest
# ---------------------------------------------------------------------------


def _line(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def plain(value: object) -> object:
    """engine / Policyの値を、型名つきの決定的なJSON値へ投影する。

    未知の型は黙って文字列化せず、型名つきで失敗させる（同一性検査を弱めないため）。
    """
    if value is None or type(value) in (bool, int, str):
        return value
    if isinstance(value, enum.Enum):
        return ["enum", type(value).__name__, plain(value.value)]
    if isinstance(value, Fraction):
        return ["fraction", value.numerator, value.denominator]
    if isinstance(value, float):
        return ["float", value.hex()]
    if isinstance(value, int):  # bool以外のintのsubclass
        return ["int", type(value).__name__, int(value)]
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return [
            "dataclass",
            type(value).__name__,
            {f.name: plain(getattr(value, f.name)) for f in dataclasses.fields(value)},
        ]
    if isinstance(value, (tuple, list)):
        return [plain(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return ["set", sorted((plain(item) for item in value), key=_line)]
    if isinstance(value, dict):
        items = [[plain(k), plain(v)] for k, v in value.items()]
        return ["dict", sorted(items, key=lambda item: _line(item[0]))]
    slots = getattr(type(value), "__slots__", None)
    if slots:
        names = (slots,) if isinstance(slots, str) else tuple(slots)
        return [
            "slots",
            type(value).__name__,
            {name: plain(getattr(value, name)) for name in names},
        ]
    raise TypeError(f"no deterministic projection for {type(value).__name__}")


def digest_of(value: object) -> str:
    return hashlib.sha256(_line(plain(value))).hexdigest()


# ---------------------------------------------------------------------------
# /proc
# ---------------------------------------------------------------------------


def proc_status_kib(pid: int | str, field: str) -> int:
    """``/proc/<pid>/status`` の ``field``（KiB）。読めなければ例外。"""
    with open(f"/proc/{pid}/status", encoding="ascii") as handle:
        for line in handle:
            if line.startswith(f"{field}:"):
                return int(line.split()[1])
    raise OSError(f"{field} is missing in /proc/{pid}/status")


def meminfo_kib(field: str) -> int:
    with open("/proc/meminfo", encoding="ascii") as handle:
        for line in handle:
            if line.startswith(f"{field}:"):
                return int(line.split()[1])
    raise OSError(f"{field} is missing in /proc/meminfo")


def tree_bytes(root: Path) -> int:
    total = 0
    for dirpath, _, filenames in os.walk(root):
        for name in filenames:
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_size
            except FileNotFoundError:
                continue
    return total


# ---------------------------------------------------------------------------
# worker
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class WorkerConfig:
    """workerへ渡す設定（picklableな値だけ）。"""

    condition: str
    candidate: dict
    incumbent: dict
    wheel_path: str | None
    arena_revision: str
    engine_revision: str


class _RecordingPolicy:
    """Policyの返した行動を席つきでdigestへ足すproxy。判断には触れない。"""

    __slots__ = ("_policy", "_seat", "_digest")

    def __init__(self, policy, seat, digest) -> None:
        self._policy = policy
        self._seat = seat
        self._digest = digest

    def choose_action(self, decision):
        action = self._policy.choose_action(decision)
        self._digest.update(_line([plain(self._seat), plain(action)]))
        return action


def _usage() -> tuple[float, float]:
    return time.process_time(), time.perf_counter()


def play_one(
    config: WorkerConfig, seed: int, placement: int, retained: list
) -> dict[str, object]:
    """1半荘を実行して保持し、費用と同一性digestを返す（得点そのものは返さない）。"""
    from lisjong_engine.seat import Seat as EngineSeat

    from lisjong_arena.heuristic_candidate_engine_aabb.kyoku_record import (
        ROLE_CANDIDATE,
        placement_roles,
    )
    from lisjong_arena.heuristic_candidate_engine_aabb.protocol import (
        locked_rules,
        parse_participant,
        resolve_factory,
    )
    from lisjong_arena.lisjong_engine.hanchan import run_policy_hanchan
    from lisjong_arena.lisjong_engine.live_draw_capture import (
        capture_live_draw_hanchan,
    )

    candidate = parse_participant(config.candidate, "candidate")
    incumbent = parse_participant(config.incumbent, "incumbent")
    roles = placement_roles(placement)
    selection = hashlib.sha256()
    delivery = hashlib.sha256()
    selector_calls: Counter = Counter()
    policies = {}
    for index, seat in enumerate(EngineSeat):
        participant = candidate if roles[index] == ROLE_CANDIDATE else incumbent
        policy = resolve_factory(participant)(**participant.behavior_configuration)
        policies[seat] = _RecordingPolicy(policy, seat, selection)

    def on_selector(seat) -> None:
        selector_calls[seat] += 1

    def on_delivery(fact) -> None:
        delivery.update(_line(plain(fact)))

    rules = locked_rules()
    rss_before = proc_status_kib("self", "VmRSS")
    cpu0, wall0 = _usage()
    if config.condition == C0:
        match = run_policy_hanchan(
            policies,
            seed=seed,
            rules=rules,
            on_selector_call=on_selector,
            on_delivery=on_delivery,
        )
        captured = None
    else:
        match, captured = capture_live_draw_hanchan(
            policies,
            seed=seed,
            placement=placement,
            rules=rules,
            on_selector_call=on_selector,
            on_delivery=on_delivery,
        )
    cpu1, wall1 = _usage()
    retained.append((match, captured))
    return {
        "cpu_seconds": cpu1 - cpu0,
        "wall_seconds": wall1 - wall0,
        "rss_before_kib": rss_before,
        "rss_after_kib": proc_status_kib("self", "VmRSS"),
        "hwm_kib": proc_status_kib("self", "VmHWM"),
        "match_digest": digest_of(match),
        "selector_calls": [selector_calls[seat] for seat in EngineSeat],
        "selection_digest": selection.hexdigest(),
        "delivery_digest": delivery.hexdigest(),
        "live_draw_rows": None if captured is None else len(captured.rows),
        "retained_hanchans": len(retained),
    }


def _timed(function):
    cpu0, wall0 = _usage()
    result = function()
    cpu1, wall1 = _usage()
    return result, {"cpu_seconds": cpu1 - cpu0, "wall_seconds": wall1 - wall0}


def serialize_write_read(
    config: WorkerConfig, captured: list, directory: Path
) -> dict[str, object]:
    """保持しているcaptureでS / W / Rを測る（追加の対局なし）。"""
    from lisjong_arena.lisjong_engine.live_draw_store import (
        _canonical_line,
        read_live_draw_capture,
        write_live_draw_capture,
    )

    rows = [row for hanchan in captured for row in hanchan.rows]

    def serialize() -> tuple[int, int]:
        payload = "".join(_canonical_line(row) for row in rows).encode("utf-8")
        return len(payload), len(gzip.compress(payload, compresslevel=6))

    # gzipはSの時間に含めない（容量の参考値だけ）
    (raw_bytes, _), _ = _timed(serialize)
    payload_bytes_only, s_timing = _timed(
        lambda: len("".join(_canonical_line(row) for row in rows).encode("utf-8"))
    )
    if payload_bytes_only != raw_bytes:
        raise RuntimeError("serialization is not deterministic")
    _, gzip_bytes = serialize()
    s_hwm = proc_status_kib("self", "VmHWM")

    destination = directory / "capture"
    _, w_timing = _timed(
        lambda: write_live_draw_capture(
            destination,
            captured,
            plan=[(hanchan.seed, hanchan.placement) for hanchan in captured],
            arena_revision=config.arena_revision,
            engine_revision=config.engine_revision,
            rules="RuleSet.default",
            access=ACCESS,
            retention=RETENTION,
        )
    )
    w_hwm = proc_status_kib("self", "VmHWM")
    artifact, r_timing = _timed(lambda: read_live_draw_capture(destination))
    if len(artifact.records) != len(rows):
        raise RuntimeError("readback row count differs")
    del artifact
    return {
        "hanchans": len(captured),
        "rows": len(rows),
        "serialized_bytes": raw_bytes,
        "serialized_gzip_bytes": gzip_bytes,
        "stored_bytes": tree_bytes(destination),
        "S": {**s_timing, "hwm_kib_after": s_hwm},
        "W": {**w_timing, "hwm_kib_after": w_hwm, "includes": ["S", "R", "R"]},
        "R": {**r_timing, "hwm_kib_after": proc_status_kib("self", "VmHWM")},
    }


def worker_main(conn, config: WorkerConfig) -> None:
    """長く動くworker。親からの指示を順に処理し、数値とdigestだけを返す。"""
    try:
        environment = None
        if config.wheel_path is not None:
            from lisjong_arena.heuristic_candidate_engine_aabb.riichi_response_hanchan import (  # noqa: E501
                local_environment_document,
            )

            environment = local_environment_document(config.wheel_path)
        conn.send(("ready", {"environment": environment, "pid": os.getpid()}))
        retained: list = []
        while True:
            message = conn.recv()
            command = message[0]
            if command == "play":
                conn.send(("ok", play_one(config, message[1], message[2], retained)))
            elif command == "probe":
                captured = [retained[0][1]]
                conn.send(
                    ("ok", serialize_write_read(config, captured, Path(message[1])))
                )
            elif command == "phase2":
                captured = [hanchan for _, hanchan in retained]
                conn.send(
                    ("ok", serialize_write_read(config, captured, Path(message[1])))
                )
            elif command == "stop":
                return
            else:
                conn.send(("error", "UnknownCommand"))
                return
    except BaseException as error:  # noqa: BLE001 - 型名だけを親へ渡して止める
        try:
            conn.send(("error", type(error).__name__))
        except Exception:
            pass


# ---------------------------------------------------------------------------
# parent
# ---------------------------------------------------------------------------


def pair_schedule(seeds=SEEDS) -> list[tuple[int, int, int, tuple[str, str]]]:
    """(組番号, seed, placement, 実行順)。組番号が偶数ならC0→C1、奇数ならC1→C0。"""
    schedule = []
    for seed in seeds:
        for placement in PLACEMENTS:
            index = len(schedule)
            order = (C0, C1) if index % 2 == 0 else (C1, C0)
            schedule.append((index, seed, placement, order))
    return schedule


class Monitor(threading.Thread):
    """全processのVmRSS合計・一時disk・wall timeを1秒ごとに見る（sampled soft STOP）。"""

    def __init__(self, pids, directory: Path, limits: dict, started: float) -> None:
        super().__init__(daemon=True)
        self.pids = list(pids)
        self.directory = directory
        self.limits = limits
        self.started = started
        self.tripped = threading.Event()
        self.finished = threading.Event()
        self.reason: tuple[str, str] | None = None
        self.peak_rss_kib = 0
        self.peak_disk_bytes = 0

    def sample(self) -> None:
        rss = sum(proc_status_kib(pid, "VmRSS") for pid in self.pids)
        disk = tree_bytes(self.directory)
        self.peak_rss_kib = max(self.peak_rss_kib, rss)
        self.peak_disk_bytes = max(self.peak_disk_bytes, disk)
        if rss > self.limits["rss_kib"]:
            raise MeasurementStop(STATUS_LIMIT, f"total VmRSS {rss} KiB over the limit")
        if disk > self.limits["disk_bytes"]:
            raise MeasurementStop(
                STATUS_LIMIT, f"temporary disk {disk} B over the limit"
            )
        if time.perf_counter() - self.started > self.limits["wall_seconds"]:
            raise MeasurementStop(STATUS_LIMIT, "total wall time over the limit")

    def run(self) -> None:
        while not self.finished.is_set():
            try:
                self.sample()
            except MeasurementStop as stop:
                self.reason = (stop.status, stop.reason)
                self.tripped.set()
                return
            except Exception as error:  # noqa: BLE001 - 監視できないならSTOP
                self.reason = (
                    STATUS_FAILED,
                    f"monitoring failed: {type(error).__name__}",
                )
                self.tripped.set()
                return
            self.finished.wait(POLL_SECONDS)


class Worker:
    def __init__(self, context, config: WorkerConfig, target) -> None:
        self.condition = config.condition
        self.conn, child = context.Pipe()
        self.process = context.Process(
            target=target, args=(child, config), name=f"capture-cost-{config.condition}"
        )
        self.process.start()
        child.close()

    def request(self, message, timeout: float, monitor: Monitor | None):
        if message is not None:
            self.conn.send(message)
        deadline = time.perf_counter() + timeout
        while True:
            if monitor is not None and monitor.tripped.is_set():
                raise MeasurementStop(*monitor.reason)
            if self.conn.poll(0.2):
                kind, value = self.conn.recv()
                if kind == "error":
                    raise MeasurementStop(
                        STATUS_FAILED, f"{self.condition} worker raised {value}"
                    )
                return value
            if not self.process.is_alive():
                raise MeasurementStop(STATUS_FAILED, f"{self.condition} worker exited")
            if time.perf_counter() > deadline:
                raise MeasurementStop(
                    STATUS_LIMIT, f"{self.condition} worker timed out"
                )

    def close(self) -> None:
        try:
            if self.process.is_alive():
                self.conn.send(("stop",))
        except Exception:
            pass
        self.process.join(10)
        if self.process.is_alive():
            self.process.terminate()
            self.process.join(10)
        if self.process.is_alive():
            self.process.kill()
            self.process.join(10)


IDENTITY_FIELDS = (
    "match_digest",
    "selector_calls",
    "selection_digest",
    "delivery_digest",
)


def require_identical(c0: dict, c1: dict, index: int) -> None:
    for field in IDENTITY_FIELDS:
        if c0[field] != c1[field]:
            raise MeasurementStop(
                STATUS_INVALID, f"pair {index}: {field} differs between C0 and C1"
            )


def _public(played: dict) -> dict:
    """報告に残す値（digestは同一性の確認済みなので残さない）。"""
    return {
        key: played[key]
        for key in (
            "cpu_seconds",
            "wall_seconds",
            "rss_before_kib",
            "rss_after_kib",
            "hwm_kib",
            "live_draw_rows",
            "retained_hanchans",
        )
    }


def remove_directory(directory: Path, remover=shutil.rmtree) -> dict[str, object]:
    try:
        remover(directory)
    except Exception as error:  # noqa: BLE001 - 削除失敗は報告に残す
        return {
            "removed": False,
            "error": type(error).__name__,
            "exists": directory.exists(),
        }
    return {
        "removed": not directory.exists(),
        "error": None,
        "exists": directory.exists(),
    }


def run_measurement(
    *,
    candidate: dict,
    incumbent: dict,
    wheel_path: str | None,
    arena_revision: str,
    engine_revision: str,
    schedule=None,
    limits: dict = LIMITS,
    temp_root: str | None = None,
    target=worker_main,
    context=None,
    remover=shutil.rmtree,
    projection_pairs: int | None = None,
) -> dict[str, object]:
    """測定本体。どの終わり方でもworkerを止め、一時directoryを削除して報告を返す。"""
    context = context or multiprocessing.get_context("spawn")
    schedule = pair_schedule() if schedule is None else list(schedule)
    started = time.perf_counter()
    directory = Path(tempfile.mkdtemp(prefix="arena-600-", dir=temp_root))
    os.chmod(directory, 0o700)
    report: dict[str, object] = {
        "pairs": [],
        "phase2": None,
        "probe": None,
        "projection": None,
        "status": None,
        "stop_reason": None,
        "temp_directory_mode": oct(directory.stat().st_mode & 0o777),
    }
    workers: dict[str, Worker] = {}
    monitor = None
    try:
        for condition in (C0, C1):
            config = WorkerConfig(
                condition,
                candidate,
                incumbent,
                wheel_path,
                arena_revision,
                engine_revision,
            )
            workers[condition] = Worker(context, config, target)
        timeout = float(limits["hanchan_timeout_seconds"])
        ready = {c: w.request(None, timeout, None) for c, w in workers.items()}
        environments = {c: r["environment"] for c, r in ready.items()}
        if environments[C0] != environments[C1]:
            raise MeasurementStop(STATUS_INVALID, "worker environments differ")
        report["worker_environment"] = environments[C0]
        monitor = Monitor(
            [os.getpid(), ready[C0]["pid"], ready[C1]["pid"]],
            directory,
            limits,
            started,
        )
        monitor.start()
        for index, seed, placement, order in schedule:
            pair_started = time.perf_counter()
            played = {}
            for condition in order:
                played[condition] = workers[condition].request(
                    ("play", seed, placement), timeout, monitor
                )
            require_identical(played[C0], played[C1], index)
            report["pairs"].append(
                {
                    "index": index,
                    "order": list(order),
                    C0: _public(played[C0]),
                    C1: _public(played[C1]),
                }
            )
            if index == 0:
                probe_dir = directory / "probe"
                probe_dir.mkdir(mode=0o700)
                probe = workers[C1].request(("probe", str(probe_dir)), timeout, monitor)
                shutil.rmtree(probe_dir)
                report["probe"] = probe
                pair_wall = time.perf_counter() - pair_started
                pairs = projection_pairs or len(schedule)
                projected = (
                    pair_started - started
                ) + PROJECTION_SAFETY * pairs * pair_wall
                report["projection"] = {
                    "projected_wall_seconds": projected,
                    "first_pair_wall_seconds_including_probe": pair_wall,
                    "safety": PROJECTION_SAFETY,
                }
                if projected > limits["wall_seconds"]:
                    raise MeasurementStop(
                        STATUS_BUDGET, "the first pair projects over the wall limit"
                    )
        phase2_dir = directory / "phase2"
        phase2_dir.mkdir(mode=0o700)
        report["phase2"] = workers[C1].request(
            ("phase2", str(phase2_dir)), timeout * 2, monitor
        )
        report["status"] = STATUS_COMPLETED
    except MeasurementStop as stop:
        report["status"], report["stop_reason"] = stop.status, stop.reason
    except Exception as error:  # noqa: BLE001 - 想定外も止めて記録する
        report["status"] = STATUS_FAILED
        report["stop_reason"] = f"parent raised {type(error).__name__}"
    finally:
        if monitor is not None:
            monitor.finished.set()
            monitor.join(5)
            report["monitor"] = {
                "peak_total_rss_kib": monitor.peak_rss_kib,
                "peak_temp_disk_bytes": monitor.peak_disk_bytes,
                "poll_seconds": POLL_SECONDS,
                "kind": "sampled soft stop (not a hard cap)",
            }
        for worker in workers.values():
            worker.close()
        report["cleanup"] = remove_directory(directory, remover)
        report["total_wall_seconds"] = time.perf_counter() - started
    if report["cleanup"]["exists"]:
        report["status"] = STATUS_FAILED
        report["stop_reason"] = (
            f"{report['stop_reason'] or ''} temporary directory remains; "
            "do not run again"
        ).strip()
    report["summary"] = summarize(report)
    return report


# ---------------------------------------------------------------------------
# 集計（費用だけ）
# ---------------------------------------------------------------------------


def _quartiles(values: list[float]) -> dict[str, float] | None:
    if not values:
        return None
    if len(values) == 1:
        return {"median": values[0], "q1": values[0], "q3": values[0]}
    q1, median, q3 = statistics.quantiles(values, n=4, method="inclusive")
    return {"median": median, "q1": q1, "q3": q3}


def _growth_per_hanchan(pairs: list[dict], condition: str) -> float | None:
    if len(pairs) < 2:
        return None
    first = pairs[0][condition]["rss_after_kib"]
    last = pairs[-1][condition]["rss_after_kib"]
    return (last - first) / (len(pairs) - 1)


def summarize(report: dict) -> dict[str, object]:
    pairs = report["pairs"]
    summary: dict[str, object] = {"pairs_completed": len(pairs)}
    if not pairs:
        return summary
    for unit in ("cpu_seconds", "wall_seconds"):
        ratios = [p[C1][unit] / p[C0][unit] for p in pairs if p[C0][unit] > 0]
        total0 = sum(p[C0][unit] for p in pairs)
        total1 = sum(p[C1][unit] for p in pairs)
        summary[f"capture_overhead_{unit}"] = {
            "per_pair": _quartiles(ratios),
            "total_ratio": total1 / total0 if total0 > 0 else None,
            "total_C0": total0,
            "total_C1": total1,
        }
    growth = {c: _growth_per_hanchan(pairs, c) for c in (C0, C1)}
    summary["retained_rss_growth_kib_per_hanchan"] = growth
    rows = [p[C1]["live_draw_rows"] for p in pairs]
    summary["live_draw_rows_per_hanchan"] = sum(rows) / len(rows)
    phase2 = report.get("phase2")
    c1_growth = growth[C1]
    extrapolation = {}
    for hanchans in EXTRAPOLATION_HANCHANS:
        item: dict[str, object] = {
            "kind": "calculation only; safety factor applied; not a guarantee",
            "serial_wall_seconds_C1": summary["capture_overhead_wall_seconds"][
                "total_C1"
            ]
            / len(pairs)
            * hanchans,
        }
        item["retained_rss_kib"] = (
            None if c1_growth is None else c1_growth * hanchans * EXTRAPOLATION_SAFETY
        )
        if phase2:
            per = phase2["stored_bytes"] / phase2["hanchans"]
            item["stored_bytes"] = per * hanchans * EXTRAPOLATION_SAFETY
        extrapolation[str(hanchans)] = item
    summary["extrapolation"] = extrapolation
    if c1_growth is None or c1_growth <= 0:
        summary["retained_memory"] = "NOT-ESTABLISHED"
    return summary


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def machine_document() -> dict[str, object]:
    cpu_model = None
    try:
        with open("/proc/cpuinfo", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("model name"):
                    cpu_model = line.split(":", 1)[1].strip()
                    break
    except OSError:
        pass
    return {
        "cpu_model": cpu_model,
        "cpu_count": os.cpu_count(),
        "mem_available_kib": meminfo_kib("MemAvailable"),
        "mem_total_kib": meminfo_kib("MemTotal"),
        "temp_free_bytes": shutil.disk_usage(tempfile.gettempdir()).free,
        "platform": platform.platform(),
        "python": platform.python_version(),
        "thread_env": {name: os.environ.get(name) for name in THREAD_ENV},
    }


def participants(lisjong_revision: str) -> tuple[dict, dict]:
    from lisjong_arena.heuristic_candidate_engine_aabb.development import (
        catalog_participant,
    )
    from lisjong_arena.heuristic_candidate_engine_aabb.riichi_response_hanchan import (  # noqa: E501
        CANDIDATE_IDENTITY,
        INCUMBENT_IDENTITY,
    )

    return (
        catalog_participant(CANDIDATE_IDENTITY, lisjong_revision).to_document(),
        catalog_participant(INCUMBENT_IDENTITY, lisjong_revision).to_document(),
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--wheel", required=True, help="installed native wheel")
    parser.add_argument("--output", required=True, help="report JSON (new file)")
    parser.add_argument(
        "--confirm-approved-plan",
        required=True,
        help="URL of the user's approval of the #600 plan (recorded in the report)",
    )
    args = parser.parse_args(argv)

    from lisjong_arena.single_round_artifact import (
        collect_execution_provenance,
        execution_provenance_to_dict,
    )

    output = Path(args.output).resolve()
    top = Path(_git("rev-parse", "--show-toplevel")).resolve()
    if output.exists():
        parser.error("--output already exists")
    if top in output.parents:
        parser.error("--output must be outside the repository")
    if _git("status", "--porcelain"):
        parser.error("the Arena checkout must be clean")
    provenance = execution_provenance_to_dict(collect_execution_provenance())
    arena_revision = _git("rev-parse", "HEAD")
    if provenance["lisjong_arena_revision"] != arena_revision:
        parser.error("installed Arena revision differs from HEAD")
    candidate, incumbent = participants(provenance["lisjong_revision"])
    report = {
        "schema": REPORT_SCHEMA,
        "owner": OWNER_ISSUE,
        "approval": args.confirm_approved_plan,
        "purpose": "capture cost and observation identity only; not a strength result",
        "plan": {
            "seeds": list(SEEDS),
            "placements": list(PLACEMENTS),
            "order": "pair i even: C0 then C1; odd: C1 then C0",
            "candidate": candidate,
            "incumbent": incumbent,
            "rules": "RuleSet.default",
            "limits": LIMITS,
            "projection_safety": PROJECTION_SAFETY,
            "extrapolation_safety": EXTRAPOLATION_SAFETY,
        },
        "provenance": provenance,
        "machine": machine_document(),
    }
    report.update(
        run_measurement(
            candidate=candidate,
            incumbent=incumbent,
            wheel_path=str(Path(args.wheel).resolve()),
            arena_revision=arena_revision,
            engine_revision=provenance["lisjong_engine_revision"],
        )
    )
    with open(output, "x", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    print(f"{report['status']}: {output}")
    return 0 if report["status"] == STATUS_COMPLETED else 1


if __name__ == "__main__":
    sys.exit(main())
