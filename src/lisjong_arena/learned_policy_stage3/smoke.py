"""Actual 4p-red-half serving smoke for the Stage 3 candidate.

```text
serving runtime (1回のstrict load)
    -> seeds 216..219 / learned candidate x4 / 4p-red-half
    -> LocalGameRunner + execute_policy() validation境界
    -> objective execution observation
    -> determinism: 同一planをDETERMINISM_RUN_COUNT回実行して照合
```

このsmokeはSERVING-INTEGRATION ONLYであり、game strengthもdecision qualityも
評価しない。Stage 2 TRAIN / VALIDATION / TEST evidenceとは混ぜない。

観測へPolicy-internal analysis（shanten、ukeire、danger、候補評価、選択理由）を
混ぜない。記録するのはstep ordinal、actor seat、実際に実行されたcanonical action
のvocabulary indexというobjective execution factだけである。
"""

import hashlib
import time
from dataclasses import dataclass
from pathlib import Path

from lisjong.action_vocabulary import (
    build_legal_action_mask,
    encode_action,
    resolve_legal_action,
)
from lisjong.policy_contract import DecisionContext, Seat

from lisjong_arena._artifact_io import canonical_json_text
from lisjong_arena.learned_policy_stage2.training import peak_process_ram_bytes
from lisjong_arena.riichienv.local_game_runner import (
    LocalGameInspectionRecorder,
    LocalGameRunner,
)

from .errors import Stage3ServingError
from .policy import ServingRuntime, summarize_latency
from .protocol import (
    DETERMINISM_RUN_COUNT,
    PROTOCOL_ID,
    SERVING_GAME_MODE,
    SERVING_HANCHAN_COUNT,
    SERVING_POPULATION,
    SERVING_ROLE,
    SERVING_SEEDS,
    Stage3Outcome,
    require_serving_seed,
)


@dataclass(frozen=True, slots=True)
class SafetyCounters:
    """serving safety contractのfail-closed観測値。"""

    decisions: int
    masked_illegal_selection: int
    resolve_failure: int
    policy_validation_failure: int
    non_finite_logits: int

    def to_document(self) -> dict[str, object]:
        return {
            "decisions": self.decisions,
            "masked_illegal_selection": self.masked_illegal_selection,
            "resolve_failure": self.resolve_failure,
            "policy_validation_failure": self.policy_validation_failure,
            "non_finite_logits": self.non_finite_logits,
        }

    @property
    def is_clean(self) -> bool:
        return (
            self.masked_illegal_selection == 0
            and self.resolve_failure == 0
            and self.policy_validation_failure == 0
            and self.non_finite_logits == 0
        )


@dataclass(frozen=True, slots=True)
class HanchanSmokeResult:
    """1 hanchanのobjective execution observationと実行量。"""

    seed: int
    scores: tuple
    ranks: tuple
    steps: int
    decisions: int
    trace_digest: str
    wall_clock_seconds: float
    cpu_seconds: float

    def terminal_document(self) -> dict[str, object]:
        """determinism照合に使うterminal result / trace identity。"""
        return {
            "seed": self.seed,
            "scores": list(self.scores),
            "ranks": list(self.ranks),
            "steps": self.steps,
            "decisions": self.decisions,
            "trace_digest": self.trace_digest,
        }

    def to_document(self) -> dict[str, object]:
        return {
            **self.terminal_document(),
            "wall_clock_seconds": self.wall_clock_seconds,
            "cpu_seconds": self.cpu_seconds,
        }


@dataclass(slots=True)
class _VerificationTally:
    """独立照合で実際に観測した違反件数。0であることを後段でfail closedする。"""

    decisions: int = 0
    foreign_action_object: int = 0
    masked_illegal_selection: int = 0
    resolve_failure: int = 0


def _verify_decision(
    context: DecisionContext, selected, tally: "_VerificationTally"
) -> int:
    """実行されたactionが当該decisionのlegal set上でcanonicalであることを数える。

    adapter側の主張ではなく、記録されたexecution observationから独立に照合する。
    違反はここでraiseせずtallyへ計上し、hanchan完了時にまとめてfail closedする。
    そうしないと「0件だった」という報告値が、instrumentationではなくcodeの
    literalになってしまう。
    """
    tally.decisions += 1
    if not any(selected is action for action in context.legal_actions):
        tally.foreign_action_object += 1
    mask = build_legal_action_mask(context)
    index = encode_action(selected)
    if not mask[index]:
        tally.masked_illegal_selection += 1
    try:
        resolved = resolve_legal_action(index, context)
    except Exception:
        tally.resolve_failure += 1
        return index
    if resolved is not selected:
        tally.resolve_failure += 1
    return index


def _observe_execution(inspection, policies) -> tuple[str, "_VerificationTally", dict]:
    """記録されたdecisionを照合し、trace digestとper-seat index列を返す。"""
    lines: list[str] = []
    per_seat: dict[int, list[int]] = {int(seat): [] for seat in Seat}
    tally = _VerificationTally()
    for step in inspection.step_observations:
        for observation in step.seat_decisions:
            trace = observation.decision_trace
            context = DecisionContext(
                input=observation.policy_input,
                legal_actions=trace.legal_actions,
            )
            index = _verify_decision(context, trace.selected_action, tally)
            seat = int(observation.seat)
            per_seat[seat].append(index)
            lines.append(f"{step.step_ordinal}\t{seat}\t{index}")

    for seat, indices in per_seat.items():
        selected = [sample.selected_index for sample in policies[Seat(seat)].samples]
        if selected != indices:
            raise Stage3ServingError(
                f"seat {seat} executed indices differ from the model selections"
            )
    digest = hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()
    return digest, tally, per_seat


def run_serving_hanchan(
    runtime: ServingRuntime, seed: int
) -> tuple[HanchanSmokeResult, tuple, "_VerificationTally", int]:
    """1 seedをlearned candidate x4で完走させ、observationを照合する。"""
    require_serving_seed(seed)
    policies = {seat: runtime.create_policy() for seat in Seat}
    if len({id(policy) for policy in policies.values()}) != len(policies):
        raise Stage3ServingError("each seat must use a distinct Policy instance")

    recorder = LocalGameInspectionRecorder()
    runner = LocalGameRunner(
        policies,
        seed=seed,
        game_mode=SERVING_GAME_MODE,
        inspection_recorder=recorder,
    )
    wall_start = time.perf_counter()
    cpu_start = time.process_time()
    result = runner.run()
    wall_clock = time.perf_counter() - wall_start
    cpu_seconds = time.process_time() - cpu_start

    digest, tally, _ = _observe_execution(recorder.snapshot(), policies)
    if tally.decisions != result.decisions:
        raise Stage3ServingError(
            "observed decision count does not match the executed decision count"
        )
    if (
        tally.foreign_action_object
        or tally.masked_illegal_selection
        or tally.resolve_failure
    ):
        raise Stage3ServingError(
            f"serving safety verification failed on seed {seed}: {tally}"
        )
    checks = sum(policies[seat].finite_logit_checks for seat in Seat)
    non_finite = sum(policies[seat].non_finite_logits for seat in Seat)
    if non_finite or checks != tally.decisions:
        raise Stage3ServingError(
            f"logit finiteness was not established for every decision on seed {seed}"
        )
    samples = tuple(sample for seat in Seat for sample in policies[seat].samples)
    return (
        HanchanSmokeResult(
            seed=seed,
            scores=tuple(result.scores),
            ranks=tuple(result.ranks),
            steps=result.steps,
            decisions=result.decisions,
            trace_digest=digest,
            wall_clock_seconds=wall_clock,
            cpu_seconds=cpu_seconds,
        ),
        samples,
        tally,
        non_finite,
    )


@dataclass(frozen=True, slots=True)
class ServingSmokeRun:
    """同一planの1回ぶんの実行。"""

    run_ordinal: int
    hanchan: tuple
    latency: object
    counters: SafetyCounters
    peak_process_ram_bytes: int | None

    def terminal_document(self) -> list:
        return [item.terminal_document() for item in self.hanchan]

    def to_document(self) -> dict[str, object]:
        return {
            "run_ordinal": self.run_ordinal,
            "hanchan": [item.to_document() for item in self.hanchan],
            "latency": self.latency.to_document(),
            "safety": self.counters.to_document(),
            "peak_process_ram_bytes": self.peak_process_ram_bytes,
            "wall_clock_seconds": sum(item.wall_clock_seconds for item in self.hanchan),
            "cpu_seconds": sum(item.cpu_seconds for item in self.hanchan),
        }


def run_serving_plan(runtime: ServingRuntime, run_ordinal: int) -> ServingSmokeRun:
    """locked serving planを1回実行する。"""
    results = []
    samples: list = []
    tallies: list = []
    non_finite = 0
    for seed in SERVING_SEEDS:
        hanchan, hanchan_samples, tally, hanchan_non_finite = run_serving_hanchan(
            runtime, seed
        )
        results.append(hanchan)
        samples.extend(hanchan_samples)
        tallies.append(tally)
        non_finite += hanchan_non_finite
    if len(results) != SERVING_HANCHAN_COUNT:
        raise Stage3ServingError("serving plan did not run the locked hanchan count")

    # execute_policy()のvalidation失敗はrunnerをabortさせるため、planがここへ到達
    # した時点で0件であることが確定している。他の値はadapter instrumentationと
    # 独立照合の実測値であり、codeのliteralではない。
    counters = SafetyCounters(
        decisions=sum(tally.decisions for tally in tallies),
        masked_illegal_selection=sum(
            tally.masked_illegal_selection for tally in tallies
        ),
        resolve_failure=sum(tally.resolve_failure for tally in tallies),
        policy_validation_failure=0,
        non_finite_logits=non_finite,
    )
    if counters.decisions != len(samples):
        raise Stage3ServingError(
            "verified decision count does not match the recorded model selections"
        )
    return ServingSmokeRun(
        run_ordinal=run_ordinal,
        hanchan=tuple(results),
        latency=summarize_latency(samples),
        counters=counters,
        peak_process_ram_bytes=peak_process_ram_bytes(),
    )


@dataclass(frozen=True, slots=True)
class ServingSmokeResult:
    """Stage 3 serving smokeのpublic result。"""

    identity: dict
    conditions: dict
    artifact_load: dict
    runs: tuple
    deterministic: bool
    outcome: Stage3Outcome

    def to_document(self) -> dict[str, object]:
        return {
            "protocol_id": PROTOCOL_ID,
            "role": SERVING_ROLE,
            "game_mode": SERVING_GAME_MODE,
            "population": SERVING_POPULATION,
            "ordered_seeds": list(SERVING_SEEDS),
            "determinism_run_count": DETERMINISM_RUN_COUNT,
            "artifact": self.identity,
            "artifact_load": self.artifact_load,
            "runtime_conditions": self.conditions,
            "runs": [run.to_document() for run in self.runs],
            "deterministic_repeat": self.deterministic,
            "outcome": self.outcome.value,
            "strength_claim": None,
        }


def run_serving_smoke(runtime: ServingRuntime) -> ServingSmokeResult:
    """locked planをDETERMINISM_RUN_COUNT回実行し、determinismまで確認する。

    失敗gameをskipして成功分だけを返すことはしない。いずれかのhanchanが
    完走しない場合はsmoke全体をfail closedする。
    """
    if not isinstance(runtime, ServingRuntime):
        raise TypeError("runtime must be a ServingRuntime")

    runs = tuple(
        run_serving_plan(runtime, ordinal)
        for ordinal in range(1, DETERMINISM_RUN_COUNT + 1)
    )
    reference = runs[0].terminal_document()
    deterministic = all(run.terminal_document() == reference for run in runs)
    clean = all(run.counters.is_clean for run in runs)

    outcome = (
        Stage3Outcome.SERVING_CANDIDATE_READY
        if deterministic and clean
        else Stage3Outcome.STOP_INVALID
    )
    checkpoint = runtime.checkpoint
    return ServingSmokeResult(
        identity=checkpoint.identity_document(),
        conditions=dict(runtime.conditions),
        artifact_load={
            "artifact_bytes": checkpoint.artifact_bytes,
            "load_wall_clock_seconds": checkpoint.load_wall_clock_seconds,
            "load_cpu_seconds": checkpoint.load_cpu_seconds,
            "peak_process_ram_bytes_after_load": peak_process_ram_bytes(),
        },
        runs=runs,
        deterministic=deterministic,
        outcome=outcome,
    )


def write_result(path: str | Path, result: ServingSmokeResult) -> None:
    """smoke resultをcanonical JSONで書き出す（既存pathは上書きしない）。"""
    path = Path(path)
    if path.exists():
        raise FileExistsError("result destination already exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        canonical_json_text(result.to_document()), encoding="utf-8", newline="\n"
    )


__all__ = [
    "HanchanSmokeResult",
    "SafetyCounters",
    "ServingSmokeResult",
    "ServingSmokeRun",
    "run_serving_hanchan",
    "run_serving_plan",
    "run_serving_smoke",
    "write_result",
]
