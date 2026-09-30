"""AABB半荘比較の決定的再生・局単位記録・正式記録との照合(Issue #432)。

正式runner(``comparison.run_comparison*``、lock、comparison / result artifact)は
変更も呼び出しもしない。seat assignmentとPolicy生成は既存
``comparison._seat_assignment`` / ``_create_policies``をそのまま使い、1 gameは
``LocalGameRunner``の公開``trace_sink``でraw eventを受け取るだけである。
traceの受信はPolicyの入力・行動へ影響しない(再生最終点と正式記録の全件一致で
確認する)。

出力は正式証跡と別の場所へ書き、既存fileを上書きしない。
"""

from __future__ import annotations

import dataclasses
import hashlib
import importlib.metadata
import json
import os
import sys
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

from lisjong.policy_contract import Seat

from lisjong_arena.comparison import _create_policies, _seat_assignment
from lisjong_arena.game_trace import GameTraceEvent
from lisjong_arena.model import ComparisonPlan
from lisjong_arena.policy_catalog import POLICY_CATALOG
from lisjong_arena.riichienv.local_game_runner import LocalGameRunner

from .accounting import account_game

RECORD_SCHEMA = "arena-aabb-kyoku-diagnostic-record-v2"
VERIFICATION_SCHEMA = "arena-aabb-kyoku-diagnostic-verification-v1"
ROTATIONS = 4


class ReplayError(RuntimeError):
    """再生・照合の前提が崩れた。部分的な結果は採用しない。"""


class _EventCollector:
    """``GameTraceSink``: raw eventをdictとして順に保持するだけのsink。"""

    def __init__(self) -> None:
        self.events: list[dict] = []
        self.completed = False

    def on_start(self, *, seed: int, game_mode: str) -> None:
        self.events.clear()

    def on_event(self, event: GameTraceEvent) -> None:
        if event.sequence != len(self.events):
            raise ReplayError("trace event sequence is not contiguous")
        self.events.append(json.loads(event.event))

    def on_complete(self) -> None:
        self.completed = True


def runtime_identity() -> dict[str, object]:
    """実行環境の記録。取得できない値は推測せず``None``にする。"""
    identity: dict[str, object] = {
        "python": sys.version,
        "shanten_backend_env": os.environ.get("LISJONG_SHANTEN_BACKEND"),
    }
    for name in ("riichienv", "lisjong", "lisjong-arena"):
        try:
            identity[f"{name}_version"] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            identity[f"{name}_version"] = None
    try:
        from lisjong.hand_evaluation import _shanten_backend

        identity["shanten_backend_selected"] = _shanten_backend.BACKEND_NAME
    except Exception:  # noqa: BLE001 - recorded as unavailable
        identity["shanten_backend_selected"] = None
    native = sys.modules.get("_lisjong_native")
    identity["native_source_revision"] = getattr(native, "SOURCE_REVISION", None)
    identity["native_api_version"] = getattr(native, "API_VERSION", None)
    return identity


def build_plan(
    *,
    policy_a: str,
    policy_b: str,
    seeds: Sequence[int],
    game_mode: str,
    max_steps: int,
) -> ComparisonPlan:
    try:
        spec_a, spec_b = POLICY_CATALOG[policy_a], POLICY_CATALOG[policy_b]
    except KeyError as exc:
        raise ReplayError(f"unknown catalog identity {exc}") from None
    return ComparisonPlan(
        policy_a=spec_a,
        policy_b=spec_b,
        seeds=tuple(seeds),
        game_mode=game_mode,
        max_steps=max_steps,
    )


def replay_game(plan: ComparisonPlan, seed: int, rotation: int) -> dict[str, object]:
    """1 gameを再生し、局単位の記録を持つJSON互換dictを返す。"""
    assignment = _seat_assignment(plan, rotation)
    policies = _create_policies(assignment, seed=seed, rotation=rotation)
    collector = _EventCollector()
    result = LocalGameRunner(
        policies,
        seed=seed,
        game_mode=plan.game_mode,
        max_steps=plan.max_steps,
        trace_sink=collector,
    ).run()
    if not collector.completed:
        raise ReplayError("trace sink did not complete")
    account = account_game(collector.events, tuple(result.scores))
    raw = json.dumps(collector.events, ensure_ascii=False, separators=(",", ":"))
    return {
        "schema": RECORD_SCHEMA,
        "seed": seed,
        "rotation": rotation,
        "game_mode": plan.game_mode,
        "max_steps": plan.max_steps,
        "seat_identities": [assignment[seat].identity for seat in Seat],
        "scores": list(result.scores),
        "ranks": list(result.ranks),
        "raw_event_count": len(collector.events),
        "raw_events_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        "final_award_seat": account.final_award_seat,
        "final_award": account.final_award,
        "kyokus": [dataclasses.asdict(kyoku) for kyoku in account.kyokus],
        "runtime": runtime_identity(),
    }


def _replay_task(args: tuple) -> dict[str, object]:
    policy_a, policy_b, seeds, game_mode, max_steps, seed, rotation = args
    plan = build_plan(
        policy_a=policy_a,
        policy_b=policy_b,
        seeds=seeds,
        game_mode=game_mode,
        max_steps=max_steps,
    )
    return replay_game(plan, seed, rotation)


def expected_seat_identities(policy_a: str, policy_b: str, rotation: int) -> list[str]:
    """``comparison._seat_assignment``と同じrotationのseat配置(identity列)。"""
    base = (policy_a, policy_a, policy_b, policy_b)
    return [base[(seat - rotation) % ROTATIONS] for seat in range(4)]


def _read_partial(path: Path) -> list[dict[str, object]]:
    """``.partial``を読み、書きかけの最終行だけを取り除く。

    改行で終わっていない最終行は、追記中の中断による未完了行として捨て、fileを
    最後の完全な行までtruncateする。途中の行の破損やschema不一致はfail closedする。
    """
    data = path.read_bytes()
    complete_end = data.rfind(b"\n") + 1
    if complete_end != len(data):
        with path.open("r+b") as handle:
            handle.truncate(complete_end)
            handle.flush()
            os.fsync(handle.fileno())
    return _parse_lines(data[:complete_end].decode("utf-8"), path)


def _check_resumable(
    record: dict[str, object],
    *,
    policy_a: str,
    policy_b: str,
    seeds: Sequence[int],
    game_mode: str,
    max_steps: int,
    source: Path,
) -> tuple[int, int]:
    rotation = record.get("rotation")
    seed = record.get("seed")
    if (
        type(rotation) is not int
        or type(seed) is not int
        or record.get("game_mode") != game_mode
        or record.get("max_steps") != max_steps
        or record.get("seed") not in seeds
        or rotation not in range(ROTATIONS)
        or record.get("seat_identities")
        != expected_seat_identities(policy_a, policy_b, rotation)
    ):
        raise ReplayError(
            f"{source} holds a record for different replay conditions "
            f"(seed={record.get('seed')!r}, rotation={rotation!r})"
        )
    return (record["seed"], rotation)


def run_replay(
    *,
    policy_a: str,
    policy_b: str,
    seeds: Sequence[int],
    game_mode: str,
    max_steps: int,
    output: Path,
    workers: int,
    progress: Callable[[int, int], None] | None = None,
) -> Path:
    """全(seed, rotation)を再生し、``output``(新規)へJSON Linesで書く。

    完了したgameは``<output>.partial``へ1行ずつ追記・fsyncする。中断後に同じ
    引数で再実行すると、``.partial``の完了分を再利用して残りだけを再生する。
    再利用する記録は、各rotationの期待seat配置・game mode・max_steps・seedが
    完全に一致しなければ拒否する。全件がそろった時点で(seed, rotation)順に
    ``<output>.writing``へ書き、``output``へrenameしてから``.partial``を削除する。

    中断箇所ごとの再開:

    - ``.partial``の書きかけ最終行: 捨てて、そのgameを再生し直す
    - ``.writing``の書きかけ: 捨てて、``.partial``から書き直す
    - rename後・``.partial``削除前: ``output``と``.partial``の内容が一致すれば
      ``.partial``を削除して完了とする
    """
    output = Path(output)
    build_plan(
        policy_a=policy_a,
        policy_b=policy_b,
        seeds=seeds,
        game_mode=game_mode,
        max_steps=max_steps,
    )
    seeds = tuple(seeds)
    conditions = dict(
        policy_a=policy_a,
        policy_b=policy_b,
        seeds=seeds,
        game_mode=game_mode,
        max_steps=max_steps,
    )
    partial = output.with_name(output.name + ".partial")
    writing = output.with_name(output.name + ".writing")
    if output.exists():
        if not partial.exists():
            raise ReplayError(f"refusing to overwrite {output}")
        finished = load_records(output)
        pending = _read_partial(partial)
        keys = {_check_resumable(r, source=output, **conditions) for r in finished}
        expected = {(seed, rotation) for seed in seeds for rotation in range(ROTATIONS)}
        if keys != expected or len(finished) != len(expected):
            raise ReplayError(f"{output} is not a complete replay; not overwriting")
        if sorted(map(json.dumps, finished)) != sorted(map(json.dumps, pending)):
            raise ReplayError(f"{output} and {partial} disagree; not overwriting")
        partial.unlink()
        return output
    if writing.exists():
        writing.unlink()
    done: dict[tuple[int, int], dict[str, object]] = {}
    if partial.exists():
        for record in _read_partial(partial):
            key = _check_resumable(record, source=partial, **conditions)
            if key in done:
                raise ReplayError(f"{partial} holds duplicate game {key}")
            done[key] = record
    tasks = [
        (policy_a, policy_b, seeds, game_mode, max_steps, seed, rotation)
        for seed in seeds
        for rotation in range(ROTATIONS)
        if (seed, rotation) not in done
    ]
    total = len(done) + len(tasks)
    with partial.open("a", encoding="utf-8") as handle:

        def keep(record: dict[str, object]) -> None:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
            done[(record["seed"], record["rotation"])] = record
            if progress:
                progress(len(done), total)

        if workers == 1:
            iterator: Iterable[dict[str, object]] = map(_replay_task, tasks)
            for record in iterator:
                keep(record)
        else:
            with ProcessPoolExecutor(max_workers=workers) as pool:
                futures = [pool.submit(_replay_task, task) for task in tasks]
                for future in as_completed(futures):
                    keep(future.result())
    records = [done[key] for key in sorted(done)]
    if len(records) != len(seeds) * ROTATIONS:
        raise ReplayError("replay is incomplete")
    runtimes = {json.dumps(r["runtime"], sort_keys=True) for r in records}
    if len(runtimes) != 1:
        raise ReplayError("games ran under different runtime identities")
    with writing.open("x", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    writing.rename(output)
    partial.unlink()
    return output


def _parse_lines(text: str, source: Path) -> list[dict[str, object]]:
    records = []
    for number, line in enumerate(text.splitlines(), start=1):
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ReplayError(f"{source}:{number} is not a complete record") from exc
        if not isinstance(record, dict) or record.get("schema") != RECORD_SCHEMA:
            raise ReplayError(f"{source}:{number} has an unknown record schema")
        records.append(record)
    return records


def load_records(path: Path) -> list[dict[str, object]]:
    path = Path(path)
    return _parse_lines(path.read_text(encoding="utf-8"), path)


def file_sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def verify_against_comparison(
    records: Sequence[dict[str, object]], comparison: object
) -> dict[str, object]:
    """再生記録が正式comparison artifactの全seat-resultと完全一致するかを返す。

    ``comparison``は``artifact.load_comparison_artifact()``の結果。1件でも差があれば
    ``status = STOP``。
    """
    plan = comparison.plan
    expected = {
        (r.seed, r.rotation, int(r.seat)): (r.policy_identity, r.score, r.rank)
        for r in comparison.seat_results
    }
    actual = {}
    for record in records:
        if record["game_mode"] != plan.game_mode:
            raise ReplayError("game_mode differs from the comparison plan")
        for seat in range(4):
            key = (record["seed"], record["rotation"], seat)
            if key in actual:
                raise ReplayError(f"duplicate replay record {key}")
            actual[key] = (
                record["seat_identities"][seat],
                record["scores"][seat],
                record["ranks"][seat],
            )
    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    mismatched = sorted(
        key for key in set(expected) & set(actual) if expected[key] != actual[key]
    )
    status = "PASS" if not (missing or extra or mismatched) else "STOP"
    return {
        "schema": VERIFICATION_SCHEMA,
        "status": status,
        "policy_a_identity": plan.policy_a_identity,
        "policy_b_identity": plan.policy_b_identity,
        "seed_count": len(plan.seeds),
        "expected_seat_results": len(expected),
        "replayed_seat_results": len(actual),
        "missing": [list(k) for k in missing[:20]],
        "extra": [list(k) for k in extra[:20]],
        "mismatched": [
            {"key": list(k), "expected": expected[k], "replayed": actual[k]}
            for k in mismatched[:20]
        ],
        "missing_count": len(missing),
        "extra_count": len(extra),
        "mismatched_count": len(mismatched),
    }


__all__ = [
    "RECORD_SCHEMA",
    "VERIFICATION_SCHEMA",
    "ReplayError",
    "build_plan",
    "expected_seat_identities",
    "file_sha256",
    "load_records",
    "replay_game",
    "run_replay",
    "runtime_identity",
    "verify_against_comparison",
]
