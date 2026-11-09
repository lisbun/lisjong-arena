"""Overall lock v2の局単位客観記録（#586）。

正式eventと**同じ実行中**に各半荘のraw eventを``LocalGameRunner``の公開
``trace_sink``で受け取り、#432の``account_game``で局単位の結果へ変換する。

```text
GameJob (seed, rotation, AABB assignment)
    -> worker: fresh Policies -> LocalGameRunner(trace_sink) -> account_game
    -> parent: canonical order -> ComparisonResult（generic経路と同じ組み立て）
                               -> kyoku records（整合検査）
                               -> kyoku summary（診断副指標）
```

- 点数計算・状態遷移は再実装しない。局の定義と点数内訳は#432 accountingのまま
- 1 gameでも記録の取得・accountingに失敗したらevent全体を失敗させる
- 指標は診断副指標であり、Overall主指標（平均順位差）・区間・classificationを
  変えない。差の向きは主指標と同じ``Learning - Heuristic``
- v1 lock / bundleには関与しない
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import traceback
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from lisjong_arena._artifact_io import (
    ArtifactValidationError,
    canonical_json_text,
    read_json_document,
    write_new_artifact_file,
)
from lisjong_arena._parallel_execution import (
    GameJob,
    GameJobOutcome,
    check_policy_spec_serializable,
    run_game_jobs,
    validate_max_workers,
)
from lisjong_arena.aabb_kyoku_diagnostic.accounting import account_game
from lisjong_arena.comparison import (
    ROTATION_COUNT,
    ComparisonExecutionError,
    _build_seat_results,
    _create_policies,
    _seat_assignment,
    aggregate_policy_metrics,
)
from lisjong_arena.game_trace import GameTraceEvent
from lisjong_arena.model import ComparisonPlan, ComparisonResult, SeatResult
from lisjong_arena.riichienv.local_game_runner import LocalGameResult, LocalGameRunner

KYOKU_RECORDS_SCHEMA = "arena-overall-kyoku-records-v1"
KYOKU_SUMMARY_SCHEMA = "arena-overall-kyoku-summary-v1"
STARTING_POINTS = 25_000
Z_95 = 1.96
OUTCOMES = ("hora", "exhaustive_draw", "abortive_draw")
FAMILIES = ("heuristic", "learning")

_RECORD_FIELDS = {
    "seed",
    "rotation",
    "seat_identities",
    "scores",
    "ranks",
    "raw_event_count",
    "raw_events_sha256",
    "final_award_seat",
    "final_award",
    "kyokus",
}


class OverallKyokuRecordError(ValueError):
    """局記録が欠落・重複・不整合で、正式記録として採用できない。"""


class _EventCollector:
    """``GameTraceSink``: raw eventを順にdictとして保持するだけ。"""

    def __init__(self) -> None:
        self.events: list[dict] = []
        self.completed = False

    def on_start(self, *, seed: int, game_mode: str) -> None:
        self.events.clear()

    def on_event(self, event: GameTraceEvent) -> None:
        if event.sequence != len(self.events):
            raise OverallKyokuRecordError("trace event sequence is not contiguous")
        self.events.append(json.loads(event.event))

    def on_complete(self) -> None:
        self.completed = True


@dataclass(frozen=True, slots=True)
class _KyokuJobOutcome(GameJobOutcome):
    record: dict[str, object] | None


def game_record(
    *,
    seed: int,
    rotation: int,
    seat_identities: Sequence[str],
    result: LocalGameResult,
    events: list[dict],
) -> dict[str, object]:
    """1半荘のraw event列から局記録行を作る（#432 accounting）。

    JSON-nativeな値（tupleではなくlist）へ正規化して返すので、書き出し前の
    検査とfile再読込後の検査が同じ値を見る。
    """
    account = account_game(events, tuple(result.scores))
    raw = json.dumps(events, ensure_ascii=False, separators=(",", ":"))
    return to_json_native(
        {
            "seed": seed,
            "rotation": rotation,
            "seat_identities": list(seat_identities),
            "scores": list(result.scores),
            "ranks": list(result.ranks),
            "raw_event_count": len(events),
            "raw_events_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
            "final_award_seat": account.final_award_seat,
            "final_award": account.final_award,
            "kyokus": [dataclasses.asdict(kyoku) for kyoku in account.kyokus],
        }
    )


def to_json_native(value: dict[str, object]) -> dict[str, object]:
    """tuple等をJSONと同じlist / dictへ揃える。"""
    return json.loads(json.dumps(value, ensure_ascii=False))


def _run_kyoku_game_job(job: GameJob) -> _KyokuJobOutcome:
    """worker内でfresh Policyを作り、trace付きで1 gameを実行して局記録を作る。"""
    try:
        policies = _create_policies(
            job.assignment, seed=job.seed, rotation=job.rotation
        )
        collector = _EventCollector()
        result = LocalGameRunner(
            policies,
            seed=job.seed,
            game_mode=job.game_mode,
            max_steps=job.max_steps,
            trace_sink=collector,
        ).run()
        if not collector.completed:
            raise OverallKyokuRecordError("trace sink did not complete")
        record = game_record(
            seed=job.seed,
            rotation=job.rotation,
            seat_identities=[spec.identity for spec in job.assignment],
            result=result,
            events=collector.events,
        )
    except Exception:
        return _KyokuJobOutcome(
            seed=job.seed,
            rotation=job.rotation,
            result=None,
            error_text="game or kyoku record failed:\n" + traceback.format_exc(),
            record=None,
        )
    return _KyokuJobOutcome(
        seed=job.seed,
        rotation=job.rotation,
        result=result,
        error_text=None,
        record=record,
    )


def execute_with_kyoku_records(
    plan: ComparisonPlan,
    *,
    max_workers: int,
    progress_callback: Callable[[int, int], None] | None = None,
) -> tuple[ComparisonResult, tuple[dict[str, object], ...]]:
    """``run_comparison_parallel``と同じsemanticsで実行し、局記録も返す。

    1 gameでも失敗したら``ComparisonExecutionError``を送出し、部分結果を返さない。
    """
    if not isinstance(plan, ComparisonPlan):
        raise TypeError("plan must be a ComparisonPlan")
    validate_max_workers(max_workers)
    check_policy_spec_serializable(plan.policy_a)
    check_policy_spec_serializable(plan.policy_b)

    assignments = {}
    jobs = []
    for seed in plan.seeds:
        for rotation in range(ROTATION_COUNT):
            assignment = _seat_assignment(plan, rotation)
            assignments[(seed, rotation)] = assignment
            jobs.append(
                GameJob(
                    seed=seed,
                    rotation=rotation,
                    assignment=assignment,
                    game_mode=plan.game_mode,
                    max_steps=plan.max_steps,
                )
            )
    outcomes = run_game_jobs(
        jobs,
        max_workers=max_workers,
        game_runner=_run_kyoku_game_job,
        progress_callback=progress_callback,
    )

    seat_results: list[SeatResult] = []
    records: list[dict[str, object]] = []
    for seed in plan.seeds:
        for rotation in range(ROTATION_COUNT):
            outcome = outcomes[(seed, rotation)]
            if outcome.error_text is not None:
                raise ComparisonExecutionError(
                    "single game execution failed in a worker process",
                    seed=seed,
                    rotation=rotation,
                ) from RuntimeError(outcome.error_text)
            if not isinstance(outcome, _KyokuJobOutcome) or outcome.record is None:
                raise ComparisonExecutionError(
                    "worker returned no kyoku record", seed=seed, rotation=rotation
                )
            seat_results.extend(
                _build_seat_results(
                    outcome.result,
                    assignments[(seed, rotation)],
                    seed=seed,
                    rotation=rotation,
                    game_mode=plan.game_mode,
                )
            )
            records.append(outcome.record)

    frozen = tuple(seat_results)
    result = ComparisonResult(
        plan=plan,
        seat_results=frozen,
        metrics_a=aggregate_policy_metrics(plan.policy_a.identity, frozen),
        metrics_b=aggregate_policy_metrics(plan.policy_b.identity, frozen),
    )
    return result, tuple(records)


# --------------------------------------------------------------------------
# 整合検査（run時の書き出し前とverifyで同じ関数を使う）


def _int(value: object, what: str) -> int:
    if type(value) is not int:
        raise OverallKyokuRecordError(f"{what} must be an int")
    return value


def _require_kyokus(record: dict[str, object], where: str) -> None:
    # summary -> heuristic_candidate_aabb -> overall_champion_aabb の循環を避ける
    from lisjong_arena.aabb_kyoku_diagnostic.summary import POINT_COMPONENTS

    kyokus = record["kyokus"]
    if type(kyokus) is not list or not kyokus:
        raise OverallKyokuRecordError(f"{where}: kyokus must be a non-empty list")
    previous_end = [STARTING_POINTS] * 4
    for index, kyoku in enumerate(kyokus):
        at = f"{where} kyoku {index}"
        if type(kyoku) is not dict or kyoku.get("index") != index:
            raise OverallKyokuRecordError(f"{at}: kyoku index is not contiguous")
        if kyoku.get("outcome") not in OUTCOMES:
            raise OverallKyokuRecordError(f"{at}: unknown outcome")
        seats = kyoku.get("seats")
        if type(seats) is not list or len(seats) != 4:
            raise OverallKyokuRecordError(f"{at}: must have four seats")
        last = index == len(kyokus) - 1
        for seat, row in enumerate(seats):
            if type(row) is not dict or row.get("seat") != seat:
                raise OverallKyokuRecordError(f"{at}: seats are not in 0..3 order")
            start = _int(row.get("start_points"), f"{at} start_points")
            end = _int(row.get("end_points"), f"{at} end_points")
            if start != previous_end[seat]:
                raise OverallKyokuRecordError(
                    f"{at} seat {seat}: start points do not continue the previous kyoku"
                )
            change = sum(
                sign * _int(row.get(name), f"{at} {name}")
                for name, sign in POINT_COMPONENTS.items()
            )
            if change != end - start:
                raise OverallKyokuRecordError(
                    f"{at} seat {seat}: point components do not reconcile"
                )
            if not last and row["final_award"] != 0:
                raise OverallKyokuRecordError(f"{at}: final award before last kyoku")
            won, wins = row.get("won"), _int(row.get("win_count"), f"{at} win_count")
            dealt, deal_ins = (
                row.get("dealt_in"),
                _int(row.get("deal_in_count"), f"{at} deal_in_count"),
            )
            if type(won) is not bool or won != (wins > 0):
                raise OverallKyokuRecordError(f"{at} seat {seat}: won/win_count differ")
            if type(dealt) is not bool or dealt != (deal_ins > 0):
                raise OverallKyokuRecordError(
                    f"{at} seat {seat}: dealt_in/deal_in_count differ"
                )
            previous_end[seat] = end
    if previous_end != record["scores"]:
        raise OverallKyokuRecordError(f"{where}: last kyoku does not end at the score")


def validate_kyoku_records(
    records: object,
    *,
    seeds: Sequence[int],
    heuristic_identity: str,
    learning_identity: str,
    seat_results: Sequence[SeatResult],
) -> tuple[dict[str, object], ...]:
    """全gameの局記録が揃い、AABB割り当て・comparison結果と整合することを検査する。"""
    if not isinstance(records, (list, tuple)):
        raise OverallKyokuRecordError("kyoku records must be a list")
    keys = [(seed, rotation) for seed in seeds for rotation in range(ROTATION_COUNT)]
    if len(records) != len(keys):
        raise OverallKyokuRecordError(
            f"expected {len(keys)} kyoku records but found {len(records)}"
        )
    by_game: dict[tuple[int, int], list[SeatResult]] = defaultdict(list)
    for seat_result in seat_results:
        by_game[(seat_result.seed, seat_result.rotation)].append(seat_result)
    base = (
        heuristic_identity,
        heuristic_identity,
        learning_identity,
        learning_identity,
    )
    for (seed, rotation), record in zip(keys, records, strict=True):
        where = f"seed={seed} rotation={rotation}"
        if type(record) is not dict or set(record) != _RECORD_FIELDS:
            raise OverallKyokuRecordError(f"{where}: record fields differ")
        if record["seed"] != seed or record["rotation"] != rotation:
            raise OverallKyokuRecordError(
                f"{where}: record order or game key differs "
                f"(seed={record['seed']!r}, rotation={record['rotation']!r})"
            )
        expected_identities = [
            base[(seat - rotation) % ROTATION_COUNT] for seat in range(4)
        ]
        if record["seat_identities"] != expected_identities:
            raise OverallKyokuRecordError(f"{where}: seat identities differ from AABB")
        game = sorted(by_game.get((seed, rotation), []), key=lambda r: int(r.seat))
        if [int(r.seat) for r in game] != [0, 1, 2, 3]:
            raise OverallKyokuRecordError(f"{where}: comparison seats are incomplete")
        if (
            [r.policy_identity for r in game] != expected_identities
            or record["scores"] != [r.score for r in game]
            or record["ranks"] != [r.rank for r in game]
        ):
            raise OverallKyokuRecordError(
                f"{where}: record differs from the comparison seat results"
            )
        _int(record["raw_event_count"], f"{where} raw_event_count")
        digest = record["raw_events_sha256"]
        if type(digest) is not str or len(digest) != 64:
            raise OverallKyokuRecordError(f"{where}: raw event digest is malformed")
        _require_kyokus(record, where)
    return tuple(records)


# --------------------------------------------------------------------------
# 診断副指標（#432の定義・重み付け。向きはLearning - Heuristic）


def _interval(values: Sequence[float]) -> dict[str, object]:
    n = len(values)
    mean = sum(values) / n
    if n < 2:
        return {"n": n, "mean": mean, "sd": None, "lower": None, "upper": None}
    sd = math.sqrt(sum((v - mean) ** 2 for v in values) / (n - 1))
    half = Z_95 * sd / math.sqrt(n)
    return {"n": n, "mean": mean, "sd": sd, "lower": mean - half, "upper": mean + half}


def summarize_kyoku_records(
    records: Sequence[dict[str, object]],
    *,
    heuristic_identity: str,
    learning_identity: str,
) -> dict[str, object]:
    """seed block単位で集計する。局数やseat局数を独立標本として扱わない。

    1. seed内の4 rotationでfamily別に分子と分母（参加seat局数）を合算し率を出す
    2. ``D(s) = learning(s) - heuristic(s)``
    3. seed等重み平均 ± 1.96 × SD(N-1) / sqrt(N)

    条件付き値（平均和了打点等）は全seedの分子・分母を合算した記述値で区間なし。
    """
    from lisjong_arena.aabb_kyoku_diagnostic.summary import (
        CONDITIONAL_METRICS,
        PAIRED_RATE_METRICS,
    )

    family_of = {heuristic_identity: "heuristic", learning_identity: "learning"}
    by_seed: dict[int, list[dict]] = defaultdict(list)
    for record in records:
        by_seed[record["seed"]].append(record)  # type: ignore[index]
    counts: dict[str, int] = defaultdict(int)
    pooled = {f: {name: [0, 0] for name in CONDITIONAL_METRICS} for f in FAMILIES}
    totals = {f: {"numerators": defaultdict(float), "seat_kyokus": 0} for f in FAMILIES}
    seed_rows = []
    for seed in by_seed:
        num = {f: {name: 0 for name in PAIRED_RATE_METRICS} for f in FAMILIES}
        den = dict.fromkeys(FAMILIES, 0)
        for game in by_seed[seed]:
            for kyoku in game["kyokus"]:
                counts["kyokus"] += 1
                counts[f"outcome_{kyoku['outcome']}"] += 1
                counts["riichienv_364_signature"] += int(
                    kyoku["known_riichienv_364_signature"]
                )
                for seat in kyoku["seats"]:
                    family = family_of[game["seat_identities"][seat["seat"]]]
                    den[family] += 1
                    for name, fn in PAIRED_RATE_METRICS.items():
                        num[family][name] += fn(seat, kyoku)
                    for name, (fn_n, fn_d) in CONDITIONAL_METRICS.items():
                        pooled[family][name][0] += fn_n(seat, kyoku)
                        pooled[family][name][1] += fn_d(seat, kyoku)
        if not all(den.values()):
            raise OverallKyokuRecordError(f"seed {seed}: a family has no seat-kyoku")
        for family in FAMILIES:
            totals[family]["seat_kyokus"] += den[family]
            for name in PAIRED_RATE_METRICS:
                totals[family]["numerators"][name] += num[family][name]
        seed_rows.append(
            {
                "seed": seed,
                "seat_kyokus": den,
                "numerators": num,
                "learning_minus_heuristic": {
                    name: num["learning"][name] / den["learning"]
                    - num["heuristic"][name] / den["heuristic"]
                    for name in PAIRED_RATE_METRICS
                },
            }
        )
    paired = {
        name: {
            **{
                family: {
                    "numerator": totals[family]["numerators"][name],
                    "seat_kyokus": totals[family]["seat_kyokus"],
                    "pooled": totals[family]["numerators"][name]
                    / totals[family]["seat_kyokus"],
                }
                for family in FAMILIES
            },
            "learning_minus_heuristic": _interval(
                [row["learning_minus_heuristic"][name] for row in seed_rows]
            ),
        }
        for name in PAIRED_RATE_METRICS
    }
    conditional = {
        name: {
            family: {
                "numerator": pooled[family][name][0],
                "denominator": pooled[family][name][1],
                "value": (
                    pooled[family][name][0] / pooled[family][name][1]
                    if pooled[family][name][1]
                    else None
                ),
            }
            for family in FAMILIES
        }
        for name in CONDITIONAL_METRICS
    }
    return {
        "schema": KYOKU_SUMMARY_SCHEMA,
        "status": "DIAGNOSTIC_ONLY",
        "note": (
            "secondary diagnostics; never override the Overall primary mean-rank "
            "classification. Unit = seed block; kyoku and seat-kyoku counts are "
            "not independent samples."
        ),
        "families": {"heuristic": heuristic_identity, "learning": learning_identity},
        "seed_count": len(seed_rows),
        "counts": dict(sorted(counts.items())),
        "paired": paired,
        "conditional": conditional,
        "seed_rows": seed_rows,
    }


# --------------------------------------------------------------------------
# write-once file（lock v2の``kyoku_records`` / ``kyoku_summary`` destination）


def save_kyoku_files(
    *,
    records: Sequence[dict[str, object]],
    summary: dict[str, object],
    lock_identity: str,
    records_path: str | Path,
    summary_path: str | Path,
) -> None:
    """2 fileを新規作成する。既存pathは上書きしない。"""
    from lisjong_arena.aabb_kyoku_diagnostic.replay import runtime_identity

    write_new_artifact_file(
        Path(records_path),
        canonical_json_text(
            {
                "schema": KYOKU_RECORDS_SCHEMA,
                "lock_identity": lock_identity,
                "runtime": runtime_identity(),
                "records": list(records),
            }
        ),
    )
    write_new_artifact_file(
        Path(summary_path),
        canonical_json_text({"lock_identity": lock_identity, **summary}),
    )


def verify_kyoku_files(
    *,
    records_path: str | Path,
    summary_path: str | Path,
    lock_identity: str,
    seeds: Sequence[int],
    heuristic_identity: str,
    learning_identity: str,
    seat_results: Sequence[SeatResult],
) -> dict[str, object]:
    """2 fileをstrict-readし、記録の整合と集計の再導出一致を検査する。"""
    try:
        records_doc = read_json_document(Path(records_path))
        summary_doc = read_json_document(Path(summary_path))
    except (ArtifactValidationError, OSError, ValueError) as exc:
        raise OverallKyokuRecordError(f"cannot read kyoku files: {exc}") from exc
    if type(records_doc) is not dict or set(records_doc) != {
        "schema",
        "lock_identity",
        "runtime",
        "records",
    }:
        raise OverallKyokuRecordError("kyoku records document fields differ")
    if records_doc["schema"] != KYOKU_RECORDS_SCHEMA:
        raise OverallKyokuRecordError("unsupported kyoku records schema")
    if records_doc["lock_identity"] != lock_identity:
        raise OverallKyokuRecordError("kyoku records belong to another lock")
    records = validate_kyoku_records(
        records_doc["records"],
        seeds=seeds,
        heuristic_identity=heuristic_identity,
        learning_identity=learning_identity,
        seat_results=seat_results,
    )
    expected = json.loads(
        canonical_json_text(
            {
                "lock_identity": lock_identity,
                **summarize_kyoku_records(
                    records,
                    heuristic_identity=heuristic_identity,
                    learning_identity=learning_identity,
                ),
            }
        )
    )
    if summary_doc != expected:
        raise OverallKyokuRecordError(
            "kyoku summary differs from the value re-derived from the records"
        )
    return summary_doc


__all__ = [
    "KYOKU_RECORDS_SCHEMA",
    "KYOKU_SUMMARY_SCHEMA",
    "OverallKyokuRecordError",
    "execute_with_kyoku_records",
    "game_record",
    "to_json_native",
    "save_kyoku_files",
    "summarize_kyoku_records",
    "validate_kyoku_records",
    "verify_kyoku_files",
]
