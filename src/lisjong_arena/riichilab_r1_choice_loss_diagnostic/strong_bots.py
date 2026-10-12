"""Issue #483 strong-bot side: R1 discard disagreement by candidate-set class.

Issue #455と同じexact #170 corpus・同じcomparator・同じ除外条件を使う、別identityの
bounded runである。#455 / #251のlocked inputs・結果とは混ぜない。
"""

from collections import Counter
from collections.abc import Callable
from pathlib import Path

from lisjong.policy_contract import Policy, execute_policy
from lisjong.policy_contract.action import DiscardAction
from lisjong.policy_contract.riichi import RiichiState

from lisjong_arena.riichilab_corpus.models import TARGET_BOTS, RecentGamesSnapshot
from lisjong_arena.riichilab_deal_in_risk_disagreement.analysis import (
    COMPARATOR_CLASS_NAME,
    COMPARATOR_IDENTITY,
    LISJONG_REVISION,
    REGION_R1,
    SHANTEN_BACKEND,
    _create_locked_comparator,
    _require_locked_runtime,
    _RiichiDeclarationTracker,
    classify_region,
)
from lisjong_arena.riichilab_disagreement_diagnostic.analysis import (
    EXPECTED_CHOICE_ROWS,
    EXPECTED_DECISION_OPPORTUNITIES,
    EXPECTED_FORCED_ROWS,
    EXPECTED_GAMES,
    DiagnosticBlockedError,
    DiagnosticInvalidError,
    _require_revision,
)
from lisjong_arena.riichilab_source_pilot.dataset import (
    MaterializedSource,
    materialize_local_corpus,
)
from lisjong_arena.riichilab_source_pilot.materialization import (
    MaterializationDecisionObservation,
)
from lisjong_arena.riichilab_source_pilot.protocol import (
    ARM_R_CORPUS_IDENTITY,
    ARM_R_MANIFEST_SHA256,
)

from .classification import SHANTEN_KEEP, UNAVAILABLE, classify_r1_choice

SCHEMA_VERSION = "arena-riichilab-r1-choice-loss-strong-bots-v1"
OUTCOME_COMPLETE = "R1 CHOICE DIAGNOSTIC (STRONG BOTS) COMPLETE"

OUTCOME_AGREEMENT = "agreement"
OUTCOME_DISCARD_DISAGREEMENT = "discard_disagreement"
OUTCOME_COMPARATOR_NON_DISCARD = "comparator_non_discard"

# Issue #455の結果に記録したR1件数（選択行、一致、打牌同士の不一致、comparator非打牌）。
EXPECTED_R1_COUNTS = {
    126: (973, 529, 398, 46),
    120: (816, 429, 351, 36),
    294: (771, 412, 314, 45),
}

_BOT_NAMES = {bot_id: name for bot_id, name in TARGET_BOTS}
_NOT_APPLICABLE = "-"


def _relative_shanten(teacher: int | None, comparator: int | None) -> str:
    """#455と同じ、両者の打牌後向聴数の相対比較。"""
    if teacher is None or comparator is None:
        return UNAVAILABLE
    if teacher > comparator:
        return "teacher_shanten_worse"
    if comparator > teacher:
        return "comparator_shanten_worse"
    return "same_shanten"


def _pair_label(teacher_state: str, comparator_state: str) -> str:
    """打牌同士の不一致の、維持 / 悪化の排他的な組合せ。"""
    if UNAVAILABLE in (teacher_state, comparator_state):
        return UNAVAILABLE
    if teacher_state == SHANTEN_KEEP and comparator_state == SHANTEN_KEEP:
        return "both_keep"
    if teacher_state == SHANTEN_KEEP:
        return "comparator_only_worse"
    if comparator_state == SHANTEN_KEEP:
        return "teacher_only_worse"
    return "both_worse"


class StrongBotR1Analyzer:
    """Streaming aggregate over the R1 discard choices of the strong bots."""

    def __init__(
        self,
        *,
        policy_factory: Callable[[], Policy] = _create_locked_comparator,
    ) -> None:
        self._policy_factory = policy_factory
        self._policies: dict[tuple[str, int], Policy] = {}
        self._trackers: dict[tuple[str, int], _RiichiDeclarationTracker] = {}
        self._choice_decisions = 0
        self._forced_decisions = 0
        # (bot_id, axis, value, outcome, 維持 / 悪化の区分)
        self._cells: Counter[tuple[int, str, str, str, str]] = Counter()
        # (bot_id, 現物選択可否, #455の相対比較)
        self._relative: Counter[tuple[int, str, str]] = Counter()
        # (bot_id, 現物選択可否, outcome, 強豪botの選択, comparatorの選択)
        self._selected: Counter[tuple[int, str, str, str, str]] = Counter()

    def _policy_for(self, observation: MaterializationDecisionObservation) -> Policy:
        key = (observation.game_id, int(observation.actor_seat))
        policy = self._policies.get(key)
        if policy is None:
            try:
                policy = self._policy_factory()
            except DiagnosticInvalidError, DiagnosticBlockedError:
                raise
            except Exception as error:
                raise DiagnosticBlockedError(
                    "comparator policy factory failed"
                ) from error
            self._policies[key] = policy
        return policy

    def observe(self, observation: MaterializationDecisionObservation) -> None:
        if not isinstance(observation, MaterializationDecisionObservation):
            raise TypeError("observation must be a MaterializationDecisionObservation")
        bot_id = observation.bot_id
        if bot_id not in _BOT_NAMES:
            raise DiagnosticBlockedError("replay exposed an unexpected target bot id")
        decision = observation.decision
        if observation.legal_action_count != len(decision.legal_actions):
            raise DiagnosticBlockedError(
                "replay observation legal-action count is inconsistent"
            )
        teacher_action = observation.teacher_action
        if teacher_action not in decision.legal_actions:
            raise DiagnosticBlockedError(
                "teacher action is not an exact legal action in the replay decision"
            )

        policy_input = decision.input
        seat = int(observation.actor_seat)
        tracker = self._trackers.setdefault(
            (observation.game_id, seat), _RiichiDeclarationTracker()
        )
        tracker.update(policy_input)

        if observation.legal_action_count < 2:
            if observation.legal_action_count != 1:
                raise DiagnosticBlockedError(
                    "forced decision must contain exactly one legal action"
                )
            self._forced_decisions += 1
            return

        # #455と同じく全選択行でcomparatorを実行し、instanceはgame x seatで分ける。
        policy = self._policy_for(observation)
        try:
            comparator_action = execute_policy(policy, decision)
        except Exception as error:
            raise DiagnosticBlockedError(
                "comparator execute_policy validation failed"
            ) from error
        self._choice_decisions += 1

        if (
            not isinstance(teacher_action, DiscardAction)
            or policy_input.players[seat].riichi is not RiichiState.NONE
            or classify_region(policy_input) != REGION_R1
        ):
            return

        choice = classify_r1_choice(observation, tracker)
        teacher_state = choice.shanten_state(teacher_action)
        teacher_selected = choice.selected_genbutsu(teacher_action)
        availability = choice.genbutsu_availability
        if comparator_action == teacher_action:
            outcome = OUTCOME_AGREEMENT
            label = teacher_state
            comparator_selected = teacher_selected
        elif isinstance(comparator_action, DiscardAction):
            outcome = OUTCOME_DISCARD_DISAGREEMENT
            label = _pair_label(teacher_state, choice.shanten_state(comparator_action))
            comparator_selected = choice.selected_genbutsu(comparator_action)
            relative = _relative_shanten(
                choice.post_discard_shanten(teacher_action),
                choice.post_discard_shanten(comparator_action),
            )
            self._relative[(bot_id, availability, relative)] += 1
        else:
            outcome = OUTCOME_COMPARATOR_NON_DISCARD
            label = teacher_state
            comparator_selected = _NOT_APPLICABLE

        for axis, value in choice.axes():
            self._cells[(bot_id, axis, value, outcome, label)] += 1
        self._selected[
            (bot_id, availability, outcome, teacher_selected, comparator_selected)
        ] += 1

    def r1_counts(self, bot_id: int) -> tuple[int, int, int, int]:
        """(選択行, 一致, 打牌同士の不一致, comparator非打牌)。"""
        by_outcome = Counter()
        for (bot, axis, _value, outcome, _label), count in self._cells.items():
            if bot == bot_id and axis == "all":
                by_outcome[outcome] += count
        return (
            sum(by_outcome.values()),
            by_outcome[OUTCOME_AGREEMENT],
            by_outcome[OUTCOME_DISCARD_DISAGREEMENT],
            by_outcome[OUTCOME_COMPARATOR_NON_DISCARD],
        )

    def _bots_document(self, bot_ids: tuple[int, ...]) -> dict[str, object]:
        axes: dict[str, dict[str, dict[str, object]]] = {}
        for (bot, axis, value, outcome, label), count in sorted(self._cells.items()):
            if bot not in bot_ids:
                continue
            cell = axes.setdefault(axis, {}).setdefault(
                value,
                {
                    "rows": 0,
                    OUTCOME_AGREEMENT: {},
                    OUTCOME_DISCARD_DISAGREEMENT: {},
                    OUTCOME_COMPARATOR_NON_DISCARD: {},
                },
            )
            cell["rows"] += count
            group = cell[outcome]
            group["total"] = group.get("total", 0) + count
            group[label] = group.get(label, 0) + count

        relative: dict[str, dict[str, int]] = {}
        for (bot, availability, key), count in sorted(self._relative.items()):
            if bot in bot_ids:
                group = relative.setdefault(availability, {})
                group[key] = group.get(key, 0) + count

        selected: dict[str, dict[str, dict[str, int]]] = {}
        for (bot, availability, outcome, teacher, comparator), count in sorted(
            self._selected.items()
        ):
            if bot in bot_ids:
                group = selected.setdefault(availability, {}).setdefault(outcome, {})
                key = f"teacher={teacher}|comparator={comparator}"
                group[key] = group.get(key, 0) + count

        return {
            "axes": axes,
            "relative_post_discard_shanten": relative,
            "selected_genbutsu": selected,
        }

    def aggregate_document(self) -> dict[str, object]:
        return {
            "coverage": {
                "choice_decisions": self._choice_decisions,
                "forced_decisions": self._forced_decisions,
                "observed_decisions": self._choice_decisions + self._forced_decisions,
            },
            "all_bots": self._bots_document(
                tuple(bot_id for bot_id, _name in TARGET_BOTS)
            ),
            "per_bot": {
                name: {"bot_id": bot_id, **self._bots_document((bot_id,))}
                for bot_id, name in TARGET_BOTS
            },
        }

    def finalize(
        self, source: MaterializedSource, *, arena_revision: str
    ) -> dict[str, object]:
        arena_revision = _require_revision(arena_revision)
        if source.corpus_identity != ARM_R_CORPUS_IDENTITY:
            raise DiagnosticInvalidError(
                "corpus identity is not the locked #170 source"
            )
        if source.manifest_sha256 != ARM_R_MANIFEST_SHA256:
            raise DiagnosticInvalidError(
                "manifest identity is not the locked #170 source"
            )

        gate = source.report
        exact_coverage = (
            gate.gate_passed
            and gate.games_processed == EXPECTED_GAMES
            and gate.games_replayable == EXPECTED_GAMES
            and gate.games_unsupported == 0
            and gate.decision_opportunities == EXPECTED_DECISION_OPPORTUNITIES
            and gate.eligible_rows == EXPECTED_CHOICE_ROWS
            and gate.forced_rows == EXPECTED_FORCED_ROWS
            and gate.unresolved_rows == 0
            and gate.leakage_failures == 0
            and self._choice_decisions == EXPECTED_CHOICE_ROWS
            and self._forced_decisions == EXPECTED_FORCED_ROWS
        )
        if not exact_coverage:
            raise DiagnosticBlockedError(
                "exact #170 choice/forced coverage did not match the locked baseline"
            )
        for bot_id, expected in EXPECTED_R1_COUNTS.items():
            if self.r1_counts(bot_id) != expected:
                raise DiagnosticBlockedError(
                    "R1 counts did not match the recorded Issue #455 result"
                )
        runtime_riichienv = _require_locked_runtime()

        return {
            "schema_version": SCHEMA_VERSION,
            "outcome": OUTCOME_COMPLETE,
            "source": {
                "corpus_identity": source.corpus_identity,
                "manifest_sha256": source.manifest_sha256,
                "snapshot_identity": source.snapshot_identity,
                "teacher_bots": [
                    {"bot_id": bot_id, "name": name} for bot_id, name in TARGET_BOTS
                ],
            },
            "comparator": {
                "arena_identity": COMPARATOR_IDENTITY,
                "implementation_class": COMPARATOR_CLASS_NAME,
                "lisjong_revision": LISJONG_REVISION,
                "shanten_backend": SHANTEN_BACKEND,
                "riichienv_version": runtime_riichienv,
            },
            "diagnostic_runtime": {"arena_revision": arena_revision},
            "materialization": gate.to_document(),
            "aggregate": self.aggregate_document(),
            "interpretation_boundary": [
                "Exploratory follow-up chosen after seeing the Issue #455 result; "
                "not an independent verification, strength evaluation, or causal "
                "estimate.",
                "Teacher actions are observed choices, not correctness labels.",
                "Only decision-time player-safe information classifies a row.",
                "Rows depend on each other within a game and a bot; no interval "
                "or test is computed.",
                "The three-bot total is not a representative of the top-bot group.",
            ],
        }


def run_strong_bots(
    snapshot: RecentGamesSnapshot,
    output_dir: str | Path,
    *,
    arena_revision: str,
    policy_factory: Callable[[], Policy] = _create_locked_comparator,
) -> dict[str, object]:
    """Run the exact offline #170 replay and return aggregate-only evidence."""
    _require_revision(arena_revision)
    _require_locked_runtime()
    analyzer = StrongBotR1Analyzer(policy_factory=policy_factory)
    source = materialize_local_corpus(
        snapshot,
        output_dir,
        decision_observer=analyzer.observe,
    )
    return analyzer.finalize(source, arena_revision=arena_revision)


__all__ = [
    "EXPECTED_R1_COUNTS",
    "OUTCOME_AGREEMENT",
    "OUTCOME_COMPARATOR_NON_DISCARD",
    "OUTCOME_COMPLETE",
    "OUTCOME_DISCARD_DISAGREEMENT",
    "SCHEMA_VERSION",
    "StrongBotR1Analyzer",
    "run_strong_bots",
]
