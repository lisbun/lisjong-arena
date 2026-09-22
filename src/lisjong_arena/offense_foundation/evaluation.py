"""One-shot OFFLINE-EVAL semantic qualification for Issue #331."""

from __future__ import annotations

import itertools
import math
from collections import Counter
from pathlib import Path

from lisjong.action_vocabulary import encode_action
from lisjong.policy_contract import DecisionContext
from lisjong.policy_contract.action import DiscardAction, PassAction, RiichiAction

from lisjong_arena._artifact_io import parse_json_text
from lisjong_arena.single_round_artifact import (
    collect_execution_provenance,
    execution_provenance_to_dict,
)

from . import source_record
from .corpus import _read_row as read_scientific_row
from .corpus import read_corpus
from .learner import (
    LoadedOffenseCheckpoint,
    _validate_scientific_manifest_metadata,
    load_checkpoint,
)
from .protocol import ordered_games
from .qualification import seal, write_document
from .semantics import CALL, WIN, OffenseError, audit_discard
from .serving import OffenseServingRuntime, create_serving_runtime, infer_decision

OFFLINE_SUPPORT_THRESHOLDS = {
    "winning_opportunities": 30,
    "riichi_opportunities": 60,
    "voluntary_call_opportunities": 100,
    "normal_discard_choice_rows": 1_000,
    "ukeire_stage_eligible_rows": 500,
    "second_step_eligible_rows": 250,
}

OFFLINE_SKILL_THRESHOLDS = {
    "winning_recall": 0.99,
    "riichi_recall": 0.98,
    "no_call_agreement": 0.99,
    "shanten_stage_agreement": 0.99,
    "conditional_ukeire_stage_agreement": 0.95,
    "conditional_second_step_agreement": 0.90,
}

OFFLINE_SUPPORT_QUALIFIED = "OFFLINE EVIDENCE SUFFICIENT"
OFFLINE_SUPPORT_INSUFFICIENT = "OFFLINE EVIDENCE INSUFFICIENT"
OFFLINE_SKILL_QUALIFIED = "OFFLINE OFFENSE SKILL QUALIFIED"
OFFLINE_SKILL_NOT_QUALIFIED = "OFFLINE OFFENSE SKILL NOT QUALIFIED"


def _ratio(success: int, total: int) -> float | None:
    return None if total == 0 else success / total


def _action_family(action) -> str:
    if isinstance(action, WIN):
        return "WIN"
    if isinstance(action, RiichiAction):
        return "RIICHI"
    if isinstance(action, CALL):
        return "CALL"
    if isinstance(action, DiscardAction):
        return "DISCARD"
    if isinstance(action, PassAction):
        return "PASS"
    return type(action).__name__


def _regret_summary(values: list[float | int]) -> dict[str, object]:
    positive = [float(value) for value in values if value > 0]
    return {
        "positive_count": len(positive),
        "positive_mean": None if not positive else sum(positive) / len(positive),
        "positive_max": None if not positive else max(positive),
    }


def offline_support_document(
    corpus_path: str | Path,
    checkpoint: LoadedOffenseCheckpoint,
) -> dict[str, object]:
    """Aggregate only sealed OFFLINE-EVAL support summaries before inference."""

    if not isinstance(checkpoint, LoadedOffenseCheckpoint):
        raise TypeError("checkpoint must be a LoadedOffenseCheckpoint")
    manifest = _validate_scientific_manifest_metadata(Path(corpus_path))
    if manifest["identity"] != checkpoint.manifest["scientific_corpus_identity"]:
        raise OffenseError("checkpoint/corpus identity mismatch before OFFLINE-EVAL")

    counts = {
        "winning_opportunities": 0,
        "riichi_opportunities": 0,
        "voluntary_call_opportunities": 0,
        "normal_discard_choice_rows": 0,
        "ukeire_stage_eligible_rows": 0,
        "second_step_eligible_rows": 0,
    }
    offline_games = 0
    for (split, _seed), game in zip(
        ordered_games(manifest["lock"]), manifest["games"], strict=True
    ):
        if split != "OFFLINE-EVAL":
            continue
        offline_games += 1
        support = game["support"]
        counts["winning_opportunities"] += support["winning_opportunities"]
        counts["riichi_opportunities"] += support["riichi_opportunities"]
        counts["voluntary_call_opportunities"] += support[
            "voluntary_call_opportunities"
        ]
        counts["normal_discard_choice_rows"] += support["normal_discard_choice_rows"]
        # Every canonical ordinary-discard decision reaches current-ukeire
        # evaluation for its minimum-shanten candidate set.
        counts["ukeire_stage_eligible_rows"] += support["normal_discard_choice_rows"]
        counts["second_step_eligible_rows"] += support["second_step_applicable_rows"]

    if offline_games != 20:
        raise OffenseError("OFFLINE-EVAL must contain exactly 20 hanchan")
    failures = {
        key: {
            "observed": counts[key],
            "required": required,
        }
        for key, required in OFFLINE_SUPPORT_THRESHOLDS.items()
        if counts[key] < required
    }
    return {
        "hanchan": offline_games,
        "counts": counts,
        "thresholds": dict(OFFLINE_SUPPORT_THRESHOLDS),
        "failures": failures,
        "outcome": (
            OFFLINE_SUPPORT_QUALIFIED if not failures else OFFLINE_SUPPORT_INSUFFICIENT
        ),
    }


class _SemanticAccumulator:
    def __init__(self) -> None:
        self.decisions = 0
        self.choice_rows = 0
        self.exact_choice = 0
        self.discard_choice_rows = 0
        self.discard_exact = 0
        self.teacher_probability_sum = 0.0
        self.masked_ce_sum = 0.0
        self.winning_count = 0
        self.winning_success = 0
        self.riichi_count = 0
        self.riichi_success = 0
        self.no_call_count = 0
        self.no_call_success = 0
        self.no_call_teacher_pass_count = 0
        self.no_call_teacher_pass_exact = 0
        self.shanten_count = 0
        self.shanten_success = 0
        self.ukeire_eligible = 0
        self.ukeire_conditional_count = 0
        self.ukeire_conditional_success = 0
        self.second_eligible = 0
        self.second_conditional_count = 0
        self.second_conditional_success = 0
        self.shanten_regrets: list[float | int] = []
        self.ukeire_regrets: list[float | int] = []
        self.second_regrets: list[float | int] = []
        self.confusion: Counter[tuple[str, str]] = Counter()

    def observe(self, context, teacher_trace, stages, inference) -> None:
        teacher = teacher_trace.selected_action
        learner = inference.action
        legal = teacher_trace.legal_actions
        self.decisions += 1

        choice = len(legal) >= 2
        if choice:
            self.choice_rows += 1
            if learner == teacher:
                self.exact_choice += 1
            teacher_index = encode_action(teacher)
            teacher_log_probability = float(inference.log_probabilities[teacher_index])
            if not math.isfinite(teacher_log_probability):
                raise OffenseError("teacher log probability is non-finite")
            self.teacher_probability_sum += math.exp(teacher_log_probability)
            self.masked_ce_sum += -teacher_log_probability
            self.confusion[(_action_family(teacher), _action_family(learner))] += 1

        if isinstance(teacher, DiscardAction) and choice:
            self.discard_choice_rows += 1
            self.discard_exact += int(learner == teacher)

        if isinstance(teacher, WIN):
            self.winning_count += 1
            self.winning_success += int(isinstance(learner, WIN))

        if isinstance(teacher, RiichiAction):
            self.riichi_count += 1
            self.riichi_success += int(isinstance(learner, RiichiAction))

        call_opportunity = choice and any(isinstance(action, CALL) for action in legal)
        if call_opportunity:
            self.no_call_count += 1
            self.no_call_success += int(not isinstance(learner, CALL))
            if isinstance(teacher, PassAction):
                self.no_call_teacher_pass_count += 1
                self.no_call_teacher_pass_exact += int(learner == teacher)

        if stages is not None and choice:
            self.shanten_count += 1
            audit = audit_discard(stages, learner)
            self.shanten_success += int(audit["shanten_agreement"])
            if audit["shanten_regret"] is not None:
                self.shanten_regrets.append(audit["shanten_regret"])

            self.ukeire_eligible += int(audit["ukeire_eligible"])
            ukeire = audit["ukeire_conditional_agreement"]
            if ukeire is not None:
                self.ukeire_conditional_count += 1
                self.ukeire_conditional_success += int(ukeire)
            if audit["ukeire_regret"] is not None:
                self.ukeire_regrets.append(audit["ukeire_regret"])

            if audit["second_step_eligible"]:
                self.second_eligible += 1
            second = audit["second_step_conditional_agreement"]
            if second is not None:
                self.second_conditional_count += 1
                self.second_conditional_success += int(second)
            if audit["second_step_regret"] is not None:
                self.second_regrets.append(audit["second_step_regret"])

    def document(self) -> dict[str, object]:
        confusion: dict[str, dict[str, int]] = {}
        for (teacher, learner), count in sorted(self.confusion.items()):
            confusion.setdefault(teacher, {})[learner] = count
        return {
            "decision_count": self.decisions,
            "winning": {
                "count": self.winning_count,
                "learner_winning_action_count": self.winning_success,
                "miss_count": self.winning_count - self.winning_success,
                "recall": _ratio(self.winning_success, self.winning_count),
            },
            "riichi": {
                "count": self.riichi_count,
                "learner_riichi_count": self.riichi_success,
                "miss_count": self.riichi_count - self.riichi_success,
                "recall": _ratio(self.riichi_success, self.riichi_count),
            },
            "no_call": {
                "opportunity_count": self.no_call_count,
                "agreement_count": self.no_call_success,
                "violation_count": self.no_call_count - self.no_call_success,
                "agreement": _ratio(self.no_call_success, self.no_call_count),
                "teacher_pass_count": self.no_call_teacher_pass_count,
                "teacher_pass_exact_count": self.no_call_teacher_pass_exact,
                "teacher_pass_exact_agreement": _ratio(
                    self.no_call_teacher_pass_exact,
                    self.no_call_teacher_pass_count,
                ),
            },
            "shanten": {
                "eligible_count": self.shanten_count,
                "agreement_count": self.shanten_success,
                "agreement": _ratio(self.shanten_success, self.shanten_count),
                "regret": _regret_summary(self.shanten_regrets),
            },
            "ukeire": {
                "eligible_count": self.ukeire_eligible,
                "conditional_count": self.ukeire_conditional_count,
                "conditional_agreement_count": self.ukeire_conditional_success,
                "conditional_agreement": _ratio(
                    self.ukeire_conditional_success,
                    self.ukeire_conditional_count,
                ),
                "regret": _regret_summary(self.ukeire_regrets),
            },
            "second_step": {
                "eligible_count": self.second_eligible,
                "conditional_count": self.second_conditional_count,
                "conditional_agreement_count": self.second_conditional_success,
                "conditional_agreement": _ratio(
                    self.second_conditional_success,
                    self.second_conditional_count,
                ),
                "regret": _regret_summary(self.second_regrets),
            },
            "secondary": {
                "choice_rows": self.choice_rows,
                "all_choice_top1_agreement": _ratio(
                    self.exact_choice, self.choice_rows
                ),
                "discard_choice_rows": self.discard_choice_rows,
                "discard_top1_agreement": _ratio(
                    self.discard_exact, self.discard_choice_rows
                ),
                "mean_teacher_action_probability": (
                    None
                    if self.choice_rows == 0
                    else self.teacher_probability_sum / self.choice_rows
                ),
                "choice_row_masked_ce": (
                    None
                    if self.choice_rows == 0
                    else self.masked_ce_sum / self.choice_rows
                ),
                "action_family_confusion": confusion,
            },
        }


def _skill_failures(metrics: dict[str, object]) -> dict[str, dict[str, object]]:
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
        for key, threshold in OFFLINE_SKILL_THRESHOLDS.items()
        if observed[key] is None or observed[key] < threshold
    }


def _iter_offline_rows(
    corpus_path: Path,
    source_path: Path,
    corpus: dict,
    source: dict,
):
    games = ordered_games(corpus["lock"])
    sentinel = object()
    for game_ordinal, ((split, seed), scientific_game, source_game) in enumerate(
        zip(games, corpus["games"], source["games"], strict=True)
    ):
        if split != "OFFLINE-EVAL":
            continue
        scientific_rows_path = corpus_path / f"game-{game_ordinal:03d}" / "rows.jsonl"
        source_rows_path = (
            source_path / f"game-{game_ordinal:03d}" / source_record.SOURCE_FILENAME
        )
        with (
            scientific_rows_path.open(encoding="utf-8") as scientific_rows,
            source_rows_path.open(encoding="utf-8") as source_rows,
        ):
            for scientific_line, source_line in itertools.zip_longest(
                scientific_rows, source_rows, fillvalue=sentinel
            ):
                if scientific_line is sentinel or source_line is sentinel:
                    raise OffenseError(
                        "OFFLINE-EVAL source/scientific decision count mismatch"
                    )
                scientific_row = parse_json_text(scientific_line)
                source_row = parse_json_text(source_line)
                teacher_trace, stages = read_scientific_row(scientific_row)
                policy_input, legal_actions, source_teacher = source_record._read_row(
                    source_row
                )
                if (
                    scientific_row["decision_ordinal"] != source_row["decision_ordinal"]
                    or scientific_row["step_ordinal"] != source_row["step_ordinal"]
                    or scientific_row["actor_seat"] != source_row["actor_seat"]
                    or scientific_row["teacher_action_index"]
                    != encode_action(source_teacher)
                    or scientific_row["legal_indices"]
                    != sorted(encode_action(action) for action in legal_actions)
                ):
                    raise OffenseError("OFFLINE-EVAL paired row identity mismatch")
                yield (
                    game_ordinal,
                    seed,
                    DecisionContext(policy_input, legal_actions),
                    teacher_trace,
                    stages,
                )


def evaluate_offline(
    corpus_path: str | Path,
    source_record_path: str | Path,
    checkpoint_path: str | Path,
    result_path: str | Path,
    *,
    expected_corpus_identity: str | None = None,
) -> dict[str, object]:
    """Support-gate then expose OFFLINE-EVAL exactly through the frozen model."""

    checkpoint = load_checkpoint(
        checkpoint_path, expected_corpus_identity=expected_corpus_identity
    )
    support = offline_support_document(corpus_path, checkpoint)
    base = {
        "schema": "arena-offense-o0-offline-eval-v1",
        "checkpoint_identity": checkpoint.identity,
        "scientific_corpus_identity": checkpoint.manifest["scientific_corpus_identity"],
        "source_record_identity": checkpoint.manifest["source_record_identity"],
        "support": support,
        "thresholds": dict(OFFLINE_SKILL_THRESHOLDS),
        "provenance": execution_provenance_to_dict(collect_execution_provenance()),
    }
    if support["outcome"] != OFFLINE_SUPPORT_QUALIFIED:
        result = seal(
            {
                **base,
                "offline_eval_payload_exposed": False,
                "metrics": None,
                "skill_failures": None,
                "outcome": OFFLINE_SUPPORT_INSUFFICIENT,
            }
        )
        write_document(result_path, result)
        return result

    corpus_path = Path(corpus_path)
    source_record_path = Path(source_record_path)
    corpus = read_corpus(corpus_path)
    if corpus["identity"] != checkpoint.manifest["scientific_corpus_identity"]:
        raise OffenseError("strict-read corpus identity differs from checkpoint")
    source = source_record.read_source_record(
        source_record_path,
        expected_lock=corpus["lock"],
        corpus_path=corpus_path,
    )
    if source["identity"] != checkpoint.manifest["source_record_identity"]:
        raise OffenseError("strict-read source-record identity differs from checkpoint")

    runtime: OffenseServingRuntime = create_serving_runtime(checkpoint)
    accumulator = _SemanticAccumulator()
    seen = 0
    for _game_ordinal, _seed, context, teacher_trace, stages in _iter_offline_rows(
        corpus_path, source_record_path, corpus, source
    ):
        inference = infer_decision(runtime.model, context)
        accumulator.observe(context, teacher_trace, stages, inference)
        seen += 1
    if seen == 0:
        raise OffenseError("OFFLINE-EVAL contains no decisions")

    metrics = accumulator.document()
    # Re-derived support denominators must agree with the pre-inference support
    # gate for the semantic populations used by the learner qualification.
    support_counts = support["counts"]
    if (
        metrics["winning"]["count"] != support_counts["winning_opportunities"]
        or metrics["riichi"]["count"] != support_counts["riichi_opportunities"]
        or metrics["no_call"]["opportunity_count"]
        != support_counts["voluntary_call_opportunities"]
        or metrics["shanten"]["eligible_count"]
        != support_counts["normal_discard_choice_rows"]
        or metrics["ukeire"]["eligible_count"]
        != support_counts["ukeire_stage_eligible_rows"]
        or metrics["second_step"]["eligible_count"]
        != support_counts["second_step_eligible_rows"]
    ):
        raise OffenseError(
            "OFFLINE-EVAL semantic denominators differ from support gate"
        )

    failures = _skill_failures(metrics)
    result = seal(
        {
            **base,
            "offline_eval_payload_exposed": True,
            "metrics": metrics,
            "skill_failures": failures,
            "outcome": (
                OFFLINE_SKILL_QUALIFIED if not failures else OFFLINE_SKILL_NOT_QUALIFIED
            ),
        }
    )
    write_document(result_path, result)
    return result


__all__ = [
    "OFFLINE_SKILL_NOT_QUALIFIED",
    "OFFLINE_SKILL_QUALIFIED",
    "OFFLINE_SKILL_THRESHOLDS",
    "OFFLINE_SUPPORT_INSUFFICIENT",
    "OFFLINE_SUPPORT_QUALIFIED",
    "OFFLINE_SUPPORT_THRESHOLDS",
    "evaluate_offline",
    "offline_support_document",
]
