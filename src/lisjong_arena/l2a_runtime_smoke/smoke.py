"""凍結したL2a合成Policy 1席 + C0 3席の16半荘動作確認（Issue #507）。

親はlisjong#318（L2a-F、lisjong `docs/learning-l2a.md` 5.4節）。強さ・局素点の
比較はしない。確認するのは次だけである。

- 16半荘すべてが例外なく完走する
- L2a席の全判断が、そのdecisionの``legal_actions``にあるcanonical objectを返す
- L2a席の判断ごとの推論時間（``L2aDecision.timings_ms``の内訳と``decide()``全体）
- process RSS（load直後・各半荘後の現在値と、process全体のpeak）

seed、allocation、seatの回し方はprotocol ``l2a-runtime-smoke-v1``の固定値で、
callerからは変えられない。L2a artifactのidentityはcallerが明示し、loadした
runtimeのidentityと一致しなければ対局を始めない。

失敗はfail closedにする。1半荘でも例外・非合法があればそこで止め、成功した
半荘だけの結果をPASSとして返さない（reportは``FAIL``と原因を記録する）。

推論時間はArena側の実行観測であり、``GameTrace``やPolicy入力へは流さない。
L2a Policyの判断内容（候補・score）は保存しない。
"""

from __future__ import annotations

import json
import math
import platform
import statistics
import time
import traceback
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from lisjong.policy_contract import DecisionContext, Policy, Seat

from lisjong_arena.model import PolicySpec
from lisjong_arena.runtime_measurement import peak_process_ram_bytes

REPORT_SCHEMA = "arena-l2a-runtime-smoke-report-v1"
PROTOCOL = "l2a-runtime-smoke-v1"
ALLOCATION_IDENTITY = "f536a79cfb9c02d382e809795fee54d49f13620e7df0234499897f56bd1cbba7"
SEED_DOMAIN = "riichienv-4p-red-half-hanchan-v1"
SPLIT = "DEVELOPMENT"
GAME_MODE = "4p-red-half"
SEEDS = tuple(range(506900, 506916))
L2A_SEAT_RULE = "l2a-seat-game-ordinal-mod-4"
"""半荘の順番``ordinal``（0始まり）でL2a席を``Seat(ordinal % 4)``にする。"""

C0_IDENTITY = "placement-aware-speed-call"
OUTCOME_PASS = "PASS"
OUTCOME_FAIL = "FAIL"

REPORT_FILE = "report.json"
DECISIONS_FILE = "decisions.jsonl"

TIMING_COMPONENTS = ("candidate_encoding", "shared_context", "forward")


class L2aRuntimeSmokeError(RuntimeError):
    """動作確認を始められない、または判断が契約に反した。"""


class IllegalL2aActionError(L2aRuntimeSmokeError):
    """L2aが選んだactionがdecisionのcanonical合法候補ではない。"""


def l2a_seat(ordinal: int) -> Seat:
    return Seat(ordinal % 4)


def l2a_policy_identity(discard_identity: str, reaction_identity: str) -> str:
    """L2a席の明示的identity。class名からは導かない。"""
    return f"l2a-m1-{discard_identity}-r1-{reaction_identity}"


def load_l2a_runtime(
    discard_artifact: str | Path,
    reaction_artifact: str | Path,
    *,
    expected_identity: tuple[str, str],
):
    """lisjongの``load_l2a_policy_factory``で読み、identityを照合する。"""
    from lisjong.learning.l2a_policy import load_l2a_policy_factory

    runtime = load_l2a_policy_factory(discard_artifact, reaction_artifact)
    if tuple(runtime.identity) != tuple(expected_identity):
        raise L2aRuntimeSmokeError(
            f"loaded L2a runtime identity {runtime.identity} does not match "
            f"the expected {tuple(expected_identity)}"
        )
    return runtime


@dataclass(frozen=True, slots=True)
class L2aDecisionRecord:
    """L2a席の1判断の実行観測。判断内容（候補・score）は持たない。"""

    game_ordinal: int
    kind: str
    scored: bool
    decide_ms: float
    timings_ms: Mapping[str, float] | None

    def to_dict(self) -> dict[str, object]:
        return {
            "game_ordinal": self.game_ordinal,
            "kind": self.kind,
            "scored": self.scored,
            "decide_ms": self.decide_ms,
            "timings_ms": None if self.timings_ms is None else dict(self.timings_ms),
        }


class RecordingL2aPolicy:
    """1 seat・1 gameぶんのL2a Policyを包み、判断ごとの時間と合法性を記録する。

    選択は包んだPolicyの``decide()``が返したactionをそのまま返す。そのactionが
    decisionの``legal_actions``にあるobjectでなければ例外にする（fallbackしない）。
    """

    __slots__ = ("_game_ordinal", "_policy", "_records")

    def __init__(
        self, policy, *, game_ordinal: int, records: list[L2aDecisionRecord]
    ) -> None:
        self._policy = policy
        self._game_ordinal = game_ordinal
        self._records = records

    def choose_action(self, decision: DecisionContext):
        started = time.perf_counter()
        result = self._policy.decide(decision)
        decide_ms = (time.perf_counter() - started) * 1000.0
        if not any(result.action is legal for legal in decision.legal_actions):
            raise IllegalL2aActionError(
                f"L2a selected {type(result.action).__name__}, which is not a "
                "canonical legal action of this decision"
            )
        timings = result.timings_ms
        self._records.append(
            L2aDecisionRecord(
                game_ordinal=self._game_ordinal,
                kind=result.kind.value,
                scored=timings is not None,
                decide_ms=decide_ms,
                timings_ms=None if timings is None else dict(timings),
            )
        )
        return result.action


def _create_c0() -> Policy:
    from lisjong_arena.policy_catalog import create_placement_aware_speed_call

    return create_placement_aware_speed_call()


C0_SPEC = PolicySpec(identity=C0_IDENTITY, factory=_create_c0)


GameRunner = Callable[[Mapping[Seat, Policy], int], object]
"""``(seatごとのPolicy, seed) -> LocalGameResult``。testでは差し替える。"""


def run_local_game(policies: Mapping[Seat, Policy], seed: int):
    from lisjong_arena.riichienv.local_game_runner import LocalGameRunner

    return LocalGameRunner(policies, seed=seed, game_mode=GAME_MODE).run()


def current_rss_bytes() -> int | None:
    """Linuxの``/proc/self/status``の``VmRSS``。読めなければ``None``。"""
    try:
        text = Path("/proc/self/status").read_text(encoding="ascii")
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1]) * 1024
    return None


def summarize(values: list[float]) -> dict[str, object]:
    """件数・中央値・p95（nearest-rank）・最大。空なら値は``None``。"""
    if not values:
        return {"count": 0, "median": None, "p95": None, "max": None}
    ordered = sorted(values)
    rank = math.ceil(0.95 * len(ordered))
    return {
        "count": len(ordered),
        "median": statistics.median(ordered),
        "p95": ordered[rank - 1],
        "max": ordered[-1],
    }


def summarize_timings(records: list[L2aDecisionRecord]) -> dict[str, object]:
    """全判断の``decide()``時間と、scorerを呼んだ判断の種類別内訳。"""
    summary: dict[str, object] = {
        "all": {"decide": summarize([record.decide_ms for record in records])}
    }
    for kind in sorted({record.kind for record in records if record.scored}):
        scored = [record for record in records if record.scored and record.kind == kind]
        entry = {"decide": summarize([record.decide_ms for record in scored])}
        for component in TIMING_COMPONENTS:
            entry[component] = summarize(
                [record.timings_ms[component] for record in scored]
            )
        summary[kind] = entry
    return summary


@dataclass
class _Totals:
    games_completed: int = 0
    l2a_decisions: int = 0
    illegal: int = 0
    exceptions: int = 0
    by_kind: Counter = field(default_factory=Counter)
    scored_by_kind: Counter = field(default_factory=Counter)


def _game_entry(ordinal, seed, seat, records, result, wall_seconds):
    kinds = Counter(record.kind for record in records)
    scored = Counter(record.kind for record in records if record.scored)
    return {
        "ordinal": ordinal,
        "seed": seed,
        "l2a_seat": int(seat),
        "completed": True,
        "scores": list(result.scores),
        "ranks": list(result.ranks),
        "steps": result.steps,
        "decisions_all_seats": result.decisions,
        "l2a_decisions": len(records),
        "l2a_by_kind": dict(sorted(kinds.items())),
        "l2a_scored_by_kind": dict(sorted(scored.items())),
        "wall_seconds": wall_seconds,
        "rss_bytes": current_rss_bytes(),
        "peak_rss_bytes": peak_process_ram_bytes(),
    }


def run_smoke(
    runtime,
    *,
    output: str | Path,
    environment: Mapping[str, object],
    load_seconds: float,
    game_runner: GameRunner = run_local_game,
    seeds: tuple[int, ...] = SEEDS,
) -> dict[str, object]:
    """16半荘を順に実行し、``output``（新規directory）へreportと判断記録を書く。

    ``runtime``は``load_l2a_runtime``で照合済みのL2a runtime（Policy factory）。
    ``seeds``はtest用で、CLIは常に``SEEDS``を渡す（reportの``outcome``は
    ``SEEDS``全件の完走でなければPASSにならない）。
    """
    directory = Path(output)
    directory.mkdir(parents=True, exist_ok=False)
    discard_identity, reaction_identity = runtime.identity
    l2a_spec = PolicySpec(
        identity=l2a_policy_identity(discard_identity, reaction_identity),
        factory=runtime,
    )
    rss_after_load = current_rss_bytes()

    totals = _Totals()
    games: list[dict[str, object]] = []
    failure: dict[str, object] | None = None
    with (directory / DECISIONS_FILE).open("x", encoding="utf-8", newline="\n") as log:
        all_records: list[L2aDecisionRecord] = []
        for ordinal, seed in enumerate(seeds):
            seat = l2a_seat(ordinal)
            records: list[L2aDecisionRecord] = []
            policies: dict[Seat, Policy] = {
                other: C0_SPEC.factory() for other in Seat if other is not seat
            }
            policies[seat] = RecordingL2aPolicy(
                l2a_spec.factory(), game_ordinal=ordinal, records=records
            )
            started = time.perf_counter()
            try:
                result = game_runner(policies, seed)
            except Exception as error:
                if isinstance(error, IllegalL2aActionError):
                    totals.illegal += 1
                totals.exceptions += 1
                failure = {
                    "ordinal": ordinal,
                    "seed": seed,
                    "l2a_seat": int(seat),
                    "l2a_decisions_before_failure": len(records),
                    "error_type": type(error).__name__,
                    "message": str(error),
                    "traceback": traceback.format_exc(),
                }
                break
            wall_seconds = time.perf_counter() - started
            for record in records:
                log.write(json.dumps(record.to_dict(), allow_nan=False) + "\n")
            all_records.extend(records)
            games.append(
                _game_entry(ordinal, seed, seat, records, result, wall_seconds)
            )
            totals.games_completed += 1
            totals.l2a_decisions += len(records)
            totals.by_kind.update(record.kind for record in records)
            totals.scored_by_kind.update(
                record.kind for record in records if record.scored
            )

    passed = (
        failure is None
        and seeds == SEEDS
        and totals.games_completed == len(SEEDS)
        and totals.illegal == 0
        and totals.exceptions == 0
    )
    report = {
        "schema": REPORT_SCHEMA,
        "outcome": OUTCOME_PASS if passed else OUTCOME_FAIL,
        "protocol": PROTOCOL,
        "allocation_identity": ALLOCATION_IDENTITY,
        "seed_domain": SEED_DOMAIN,
        "split": SPLIT,
        "game_mode": GAME_MODE,
        "seeds": list(seeds),
        "l2a_seat_rule": L2A_SEAT_RULE,
        "policies": {
            "l2a": {
                "identity": l2a_spec.identity,
                "discard_artifact_identity": discard_identity,
                "reaction_artifact_identity": reaction_identity,
            },
            "c0": {"identity": C0_IDENTITY, "seats_per_game": 3},
        },
        "environment": dict(environment),
        "load": {"seconds": load_seconds, "rss_bytes_after": rss_after_load},
        "totals": {
            "games_planned": len(seeds),
            "games_completed": totals.games_completed,
            "l2a_decisions": totals.l2a_decisions,
            "l2a_by_kind": dict(sorted(totals.by_kind.items())),
            "l2a_scored_by_kind": dict(sorted(totals.scored_by_kind.items())),
            "illegal": totals.illegal,
            "exceptions": totals.exceptions,
        },
        "timings_ms": summarize_timings(all_records),
        "memory": {
            "rss_bytes_after_load": rss_after_load,
            "rss_bytes_final": current_rss_bytes(),
            "peak_rss_bytes": peak_process_ram_bytes(),
            "platform": platform.system(),
        },
        "games": games,
        "failure": failure,
        "decisions_file": DECISIONS_FILE,
    }
    with (directory / REPORT_FILE).open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(
            json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2) + "\n"
        )
    return report
