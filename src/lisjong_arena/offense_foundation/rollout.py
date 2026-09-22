"""Interactive O0 learner rollout with non-intervening shadow teacher (#331)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from lisjong.action_vocabulary import encode_action
from lisjong.policies import TwoStepUkeirePolicy
from lisjong.policy_contract import (
    DecisionTraceRecorder,
    Seat,
    execute_policy_with_trace,
)
from lisjong.policy_contract.action import DiscardAction, RiichiAction

from lisjong_arena import seed_registry
from lisjong_arena.riichienv.local_game_runner import (
    LocalGameInspectionRecorder,
    LocalGameRunner,
)
from lisjong_arena.single_round_artifact import (
    collect_execution_provenance,
    execution_provenance_to_dict,
)

from .corpus import _support
from .evaluation import (
    OFFLINE_SKILL_QUALIFIED,
    _SemanticAccumulator,
)
from .learner import LoadedOffenseCheckpoint, load_checkpoint
from .qualification import read_document, seal, unseal, write_document
from .semantics import CALL, WIN, OffenseError, audit_trace
from .serving import (
    OffenseServingRuntime,
    create_serving_runtime,
    infer_decision,
)

ROLLOUT_PROTOCOL = "offense-foundation-o0-rollout-v1"
ROLLOUT_OWNER_ISSUE = "lisbun/lisjong-arena#331"
ROLLOUT_POPULATION = "issue-331-offense-foundation-rollout"
ROLLOUT_SPLIT = "ROLLOUT"
ROLLOUT_SEED_BLOCKS = 20
ROLLOUT_ROTATIONS = 4
ROLLOUT_HANCHAN = ROLLOUT_SEED_BLOCKS * ROLLOUT_ROTATIONS
GAME_MODE = "4p-red-half"

ROLLOUT_SUPPORT_THRESHOLDS = {
    "winning_opportunities": 50,
    "riichi_opportunities": 100,
    "voluntary_call_opportunities": 200,
    "normal_discard_choice_rows": 2_000,
    "ukeire_stage_eligible_rows": 1_000,
    "second_step_eligible_rows": 500,
}

ROLLOUT_SKILL_THRESHOLDS = {
    "winning_recall": 0.99,
    "riichi_recall": 0.98,
    "no_call_agreement": 0.99,
    "shanten_stage_agreement": 0.98,
    "conditional_ukeire_stage_agreement": 0.90,
    "conditional_second_step_agreement": 0.85,
}

ROLLOUT_SUPPORT_INSUFFICIENT = "OFFLINE QUALIFIED / ROLLOUT EVIDENCE INSUFFICIENT"
ROLLOUT_NOT_QUALIFIED = "OFFLINE QUALIFIED / ROLLOUT NOT QUALIFIED"
OFFENSE_FOUNDATION_QUALIFIED = "OFFENSE FOUNDATION QUALIFIED"


def _require_rollout_seeds(values) -> tuple[int, ...]:
    try:
        seeds = tuple(values)
    except TypeError:
        raise OffenseError("rollout seeds must be iterable") from None
    if (
        len(seeds) != ROLLOUT_SEED_BLOCKS
        or any(type(seed) is not int or not 0 <= seed < 2**32 for seed in seeds)
        or len(set(seeds)) != len(seeds)
    ):
        raise OffenseError("rollout requires exactly 20 unique uint32 seeds")
    return seeds


def make_rollout_lock(
    *,
    seeds,
    allocation_binding: dict,
    seed_ledger: dict,
    checkpoint: LoadedOffenseCheckpoint,
    offline_result: dict,
) -> dict[str, object]:
    """Bind fresh rollout population only after the offline gate has qualified."""

    if not isinstance(checkpoint, LoadedOffenseCheckpoint):
        raise TypeError("checkpoint must be a LoadedOffenseCheckpoint")
    unseal(offline_result)
    if offline_result.get("outcome") != OFFLINE_SKILL_QUALIFIED:
        raise OffenseError("rollout requires OFFLINE OFFENSE SKILL QUALIFIED")
    if offline_result.get("checkpoint_identity") != checkpoint.identity:
        raise OffenseError("offline result/checkpoint identity mismatch")
    if (
        offline_result.get("scientific_corpus_identity")
        != checkpoint.manifest["scientific_corpus_identity"]
    ):
        raise OffenseError("offline result/scientific corpus identity mismatch")

    seeds = _require_rollout_seeds(seeds)
    consumed = set(checkpoint.manifest["train_seeds"])
    consumed.update(checkpoint.manifest["select_seeds"])
    consumed.update(checkpoint.manifest["offline_eval_seeds"])
    if consumed.intersection(seeds):
        raise OffenseError("rollout seeds overlap scientific corpus populations")

    try:
        record = seed_registry.require_allocation_binding(
            seed_ledger,
            allocation_binding,
            seeds=seeds,
            owner_issue=ROLLOUT_OWNER_ISSUE,
            protocol=ROLLOUT_PROTOCOL,
            seed_domain=seed_registry.RIICHIENV_HALF_HANCHAN_SEED_DOMAIN,
            population=ROLLOUT_POPULATION,
            split=ROLLOUT_SPLIT,
        )
    except seed_registry.SeedRegistryError as error:
        raise OffenseError(f"invalid rollout seed allocation: {error}") from error

    provenance = execution_provenance_to_dict(collect_execution_provenance())
    if record["arena_revision"] != provenance["lisjong_arena_revision"]:
        raise OffenseError("rollout allocation is not bound to current Arena revision")
    return seal(
        {
            "schema": "arena-offense-o0-rollout-lock-v1",
            "protocol": ROLLOUT_PROTOCOL,
            "game_mode": GAME_MODE,
            "ordered_seeds": list(seeds),
            "rotations": ROLLOUT_ROTATIONS,
            "hanchan": ROLLOUT_HANCHAN,
            "allocation_binding": dict(allocation_binding),
            "checkpoint_identity": checkpoint.identity,
            "scientific_corpus_identity": checkpoint.manifest[
                "scientific_corpus_identity"
            ],
            "offline_result_identity": offline_result["identity"],
            "execution_provenance": provenance,
        }
    )


def read_rollout_lock(path: str | Path) -> dict[str, object]:
    lock = read_document(path)
    body = unseal(lock)
    if set(body) != {
        "schema",
        "protocol",
        "game_mode",
        "ordered_seeds",
        "rotations",
        "hanchan",
        "allocation_binding",
        "checkpoint_identity",
        "scientific_corpus_identity",
        "offline_result_identity",
        "execution_provenance",
    }:
        raise OffenseError("invalid rollout lock fields")
    if (
        lock["schema"] != "arena-offense-o0-rollout-lock-v1"
        or lock["protocol"] != ROLLOUT_PROTOCOL
        or lock["game_mode"] != GAME_MODE
        or lock["rotations"] != ROLLOUT_ROTATIONS
        or lock["hanchan"] != ROLLOUT_HANCHAN
    ):
        raise OffenseError("rollout lock protocol drift")
    _require_rollout_seeds(lock["ordered_seeds"])
    seed_registry.validate_binding_shape(
        lock["allocation_binding"], seeds=lock["ordered_seeds"]
    )
    return lock


@dataclass(frozen=True, slots=True)
class _AuditRecord:
    learner_action_index: int
    teacher_action_index: int | None
    semantic_failure: bool
    contract_escape: bool
    audited: bool


def _semantic_failure(teacher_trace, stages, learner_action) -> bool:
    teacher = teacher_trace.selected_action
    legal = teacher_trace.legal_actions
    choice = len(legal) >= 2
    if isinstance(teacher, WIN) and not isinstance(learner_action, WIN):
        return True
    if isinstance(teacher, RiichiAction) and not isinstance(
        learner_action, RiichiAction
    ):
        return True
    if choice and any(isinstance(action, CALL) for action in legal):
        if isinstance(learner_action, CALL):
            return True
    if stages is not None and choice:
        if not isinstance(learner_action, DiscardAction):
            return True
        if learner_action not in stages.shanten:
            return True
        if learner_action not in stages.ukeire:
            return True
        if stages.second_step is not None and learner_action not in stages.second_step:
            return True
    return False


class _ShadowAuditedLearnerPolicy:
    """Execute learner action; shadow teacher is diagnostic only."""

    __slots__ = (
        "_runtime",
        "_teacher",
        "_accumulator",
        "_support_counts",
        "_records",
        "_escaped",
        "_post_escape_decisions",
    )

    def __init__(
        self,
        runtime: OffenseServingRuntime,
        accumulator: _SemanticAccumulator,
        support_counts: dict[str, int],
    ) -> None:
        self._runtime = runtime
        self._teacher = TwoStepUkeirePolicy()
        self._accumulator = accumulator
        self._support_counts = support_counts
        self._records: list[_AuditRecord] = []
        self._escaped = False
        self._post_escape_decisions = 0

    @property
    def records(self) -> tuple[_AuditRecord, ...]:
        return tuple(self._records)

    @property
    def escaped(self) -> bool:
        return self._escaped

    @property
    def post_escape_decisions(self) -> int:
        return self._post_escape_decisions

    def choose_action(self, decision):
        inference = infer_decision(self._runtime.model, decision)
        if self._escaped:
            self._post_escape_decisions += 1
            self._records.append(
                _AuditRecord(
                    learner_action_index=inference.action_index,
                    teacher_action_index=None,
                    semantic_failure=False,
                    contract_escape=False,
                    audited=False,
                )
            )
            return inference.action

        recorder = DecisionTraceRecorder()
        execute_policy_with_trace(self._teacher, decision, recorder)
        traces = recorder.snapshot()
        if len(traces) != 1:
            raise OffenseError("shadow teacher produced an unexpected trace count")
        teacher_trace = traces[0]
        stages = audit_trace(teacher_trace)
        support = _support(teacher_trace, stages)
        for key, value in support.items():
            self._support_counts[key] += value
        self._accumulator.observe(decision, teacher_trace, stages, inference)

        call_opportunity = len(teacher_trace.legal_actions) >= 2 and any(
            isinstance(action, CALL) for action in teacher_trace.legal_actions
        )
        escape = call_opportunity and isinstance(inference.action, CALL)
        failure = _semantic_failure(teacher_trace, stages, inference.action)
        self._records.append(
            _AuditRecord(
                learner_action_index=inference.action_index,
                teacher_action_index=encode_action(teacher_trace.selected_action),
                semantic_failure=failure,
                contract_escape=escape,
                audited=True,
            )
        )
        if escape:
            # The violating decision itself is counted.  Later open-hand states
            # are not mixed into the closed O0 semantic qualification.
            self._escaped = True
        return inference.action


def _rollout_support_document(counts: dict[str, int]) -> dict[str, object]:
    mapped = {
        "winning_opportunities": counts["winning_opportunities"],
        "riichi_opportunities": counts["riichi_opportunities"],
        "voluntary_call_opportunities": counts["voluntary_call_opportunities"],
        "normal_discard_choice_rows": counts["normal_discard_choice_rows"],
        "ukeire_stage_eligible_rows": counts["normal_discard_choice_rows"],
        "second_step_eligible_rows": counts["second_step_applicable_rows"],
    }
    failures = {
        key: {"observed": mapped[key], "required": required}
        for key, required in ROLLOUT_SUPPORT_THRESHOLDS.items()
        if mapped[key] < required
    }
    return {
        "counts": mapped,
        "thresholds": dict(ROLLOUT_SUPPORT_THRESHOLDS),
        "failures": failures,
        "qualified": not failures,
    }


def _rollout_skill_failures(metrics: dict[str, object]):
    observed = {
        "winning_recall": metrics["winning"]["recall"],
        "riichi_recall": metrics["riichi"]["recall"],
        "no_call_agreement": metrics["no_call"]["agreement"],
        "shanten_stage_agreement": metrics["shanten"]["agreement"],
        "conditional_ukeire_stage_agreement": metrics["ukeire"][
            "conditional_agreement"
        ],
        "conditional_second_step_agreement": metrics["second_step"][
            "conditional_agreement"
        ],
    }
    return {
        key: {"observed": observed[key], "required": threshold}
        for key, threshold in ROLLOUT_SKILL_THRESHOLDS.items()
        if observed[key] is None or observed[key] < threshold
    }


def _verify_focal_execution(inspection, focal: Seat, policy):
    observations = [
        (step.step_ordinal, observation)
        for step in inspection.step_observations
        for observation in step.seat_decisions
        if observation.seat == focal
    ]
    records = policy.records
    if len(observations) != len(records):
        raise OffenseError(
            "rollout learner audit count differs from executed decisions"
        )

    first_divergence = None
    for (step_ordinal, observation), record in zip(observations, records, strict=True):
        actual_index = encode_action(observation.decision_trace.selected_action)
        if actual_index != record.learner_action_index:
            raise OffenseError(
                "shadow audit learner action differs from executed action"
            )
        if record.semantic_failure and first_divergence is None:
            first_divergence = step_ordinal
    return first_divergence


def run_rollout(
    *,
    rollout_lock_path: str | Path,
    checkpoint_path: str | Path,
    offline_result_path: str | Path,
    result_path: str | Path,
) -> dict[str, object]:
    """Execute exactly 20x4 frozen learner-vs-teacher hanchan."""

    lock = read_rollout_lock(rollout_lock_path)
    checkpoint = load_checkpoint(
        checkpoint_path,
        expected_corpus_identity=lock["scientific_corpus_identity"],
    )
    if checkpoint.identity != lock["checkpoint_identity"]:
        raise OffenseError("rollout lock/checkpoint identity mismatch")

    offline = read_document(offline_result_path)
    if (
        offline["identity"] != lock["offline_result_identity"]
        or offline.get("outcome") != OFFLINE_SKILL_QUALIFIED
        or offline.get("checkpoint_identity") != checkpoint.identity
    ):
        raise OffenseError("rollout requires the exact qualified offline result")

    current = execution_provenance_to_dict(collect_execution_provenance())
    if current != lock["execution_provenance"]:
        raise OffenseError("rollout execution provenance differs from frozen lock")

    runtime = create_serving_runtime(checkpoint)
    accumulator = _SemanticAccumulator()
    support_counts = {
        "choice_rows": 0,
        "winning_opportunities": 0,
        "riichi_opportunities": 0,
        "voluntary_call_opportunities": 0,
        "normal_discard_choice_rows": 0,
        "second_step_applicable_rows": 0,
    }
    hanchan = []
    contract_escapes = 0
    post_escape_decisions = 0

    for seed in lock["ordered_seeds"]:
        for rotation in range(ROLLOUT_ROTATIONS):
            focal = Seat(rotation)
            learner = _ShadowAuditedLearnerPolicy(runtime, accumulator, support_counts)
            policies = {
                seat: (learner if seat == focal else TwoStepUkeirePolicy())
                for seat in Seat
            }
            recorder = LocalGameInspectionRecorder()
            result = LocalGameRunner(
                policies,
                seed=seed,
                game_mode=GAME_MODE,
                inspection_recorder=recorder,
            ).run()
            inspection = recorder.snapshot()
            first_divergence = _verify_focal_execution(inspection, focal, learner)
            semantic_failures = sum(
                int(record.semantic_failure) for record in learner.records
            )
            escape_count = int(learner.escaped)
            contract_escapes += escape_count
            post_escape_decisions += learner.post_escape_decisions
            hanchan.append(
                {
                    "seed": seed,
                    "rotation": rotation,
                    "focal_seat": int(focal),
                    "learner_decisions": len(learner.records),
                    "audited_decisions": sum(
                        int(record.audited) for record in learner.records
                    ),
                    "post_escape_decisions_excluded": learner.post_escape_decisions,
                    "semantic_failures": semantic_failures,
                    "first_semantic_divergence_step": first_divergence,
                    "contract_escape": bool(escape_count),
                    "focal_final_score": result.scores[int(focal)],
                    "focal_rank": result.ranks[int(focal)],
                }
            )

    if len(hanchan) != ROLLOUT_HANCHAN:
        raise OffenseError("rollout did not complete the locked 80 hanchan")

    support = _rollout_support_document(support_counts)
    metrics = accumulator.document()
    skill_failures = _rollout_skill_failures(metrics)
    no_call_count = metrics["no_call"]["opportunity_count"]
    contract_escape_rate = (
        None if no_call_count == 0 else contract_escapes / no_call_count
    )

    if not support["qualified"]:
        outcome = ROLLOUT_SUPPORT_INSUFFICIENT
    elif skill_failures:
        outcome = ROLLOUT_NOT_QUALIFIED
    else:
        outcome = OFFENSE_FOUNDATION_QUALIFIED

    result = seal(
        {
            "schema": "arena-offense-o0-rollout-result-v1",
            "rollout_lock_identity": lock["identity"],
            "checkpoint_identity": checkpoint.identity,
            "offline_result_identity": offline["identity"],
            "game_mode": GAME_MODE,
            "hanchan_count": len(hanchan),
            "support": support,
            "metrics": metrics,
            "skill_thresholds": dict(ROLLOUT_SKILL_THRESHOLDS),
            "skill_failures": skill_failures,
            "contract_escapes": {
                "count": contract_escapes,
                "rate_per_no_call_opportunity": contract_escape_rate,
                "affected_hanchan_count": sum(
                    int(item["contract_escape"]) for item in hanchan
                ),
                "post_escape_decisions_excluded": post_escape_decisions,
            },
            "per_hanchan": hanchan,
            "descriptive_only": {
                "mean_focal_final_score": sum(
                    item["focal_final_score"] for item in hanchan
                )
                / len(hanchan),
                "mean_focal_rank": sum(item["focal_rank"] for item in hanchan)
                / len(hanchan),
                "strength_claim": None,
            },
            "provenance": current,
            "outcome": outcome,
        }
    )
    write_document(result_path, result)
    return result


__all__ = [
    "GAME_MODE",
    "OFFENSE_FOUNDATION_QUALIFIED",
    "ROLLOUT_HANCHAN",
    "ROLLOUT_NOT_QUALIFIED",
    "ROLLOUT_OWNER_ISSUE",
    "ROLLOUT_POPULATION",
    "ROLLOUT_PROTOCOL",
    "ROLLOUT_SEED_BLOCKS",
    "ROLLOUT_SKILL_THRESHOLDS",
    "ROLLOUT_SPLIT",
    "ROLLOUT_SUPPORT_INSUFFICIENT",
    "ROLLOUT_SUPPORT_THRESHOLDS",
    "make_rollout_lock",
    "read_rollout_lock",
    "run_rollout",
]
