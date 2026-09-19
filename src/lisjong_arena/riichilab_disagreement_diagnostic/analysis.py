"""Issue #251 RiichiLab strong-bot disagreement aggregate diagnostic."""

from collections import Counter
from collections.abc import Callable
from importlib.metadata import version
from pathlib import Path

from lisjong.action_vocabulary import encode_action
from lisjong.hand_evaluation import calculate_shanten
from lisjong.policy_contract import Policy, execute_policy
from lisjong.policy_contract.action import DiscardAction

from lisjong_arena.policy_catalog import POLICY_CATALOG
from lisjong_arena.riichilab_corpus.models import TARGET_BOTS, RecentGamesSnapshot
from lisjong_arena.riichilab_source_pilot.dataset import (
    MaterializedSource,
    materialize_local_corpus,
)
from lisjong_arena.riichilab_source_pilot.materialization import (
    MaterializationDecisionObservation,
    action_family,
)
from lisjong_arena.riichilab_source_pilot.protocol import (
    ARM_R_CORPUS_IDENTITY,
    ARM_R_MANIFEST_SHA256,
)

SCHEMA_VERSION = "arena-riichilab-disagreement-diagnostic-v1"
OUTCOME_COMPLETE = "DISAGREEMENT DIAGNOSTIC COMPLETE"
OUTCOME_BLOCKED = "DIAGNOSTIC BLOCKED"
OUTCOME_INVALID = "STOP / INVALID"

COMPARATOR_IDENTITY = "mechanism-riichi-defense"
COMPARATOR_CLASS_NAME = "MechanismRiichiDefenseYakuhaiCallPolicy"
COMPARATOR_BINDING_ARENA_REVISION = "05bf7c24613e4a94de9db889605390df318e4557"
LISJONG_REVISION = "f29d129c67e5232d06563c6e457754377734ed14"
EXPECTED_RIICHIENV_VERSION = "0.4.10"

EXPECTED_GAMES = 112
EXPECTED_DECISION_OPPORTUNITIES = 19_874
EXPECTED_CHOICE_ROWS = 18_992
EXPECTED_FORCED_ROWS = 882

_BOT_NAMES = {bot_id: name for bot_id, name in TARGET_BOTS}
_HEX = frozenset("0123456789abcdef")


class DisagreementDiagnosticError(Exception):
    """Issue #251 diagnostic base error."""


class DiagnosticBlockedError(DisagreementDiagnosticError):
    """Exact comparison could not be completed."""


class DiagnosticInvalidError(DisagreementDiagnosticError):
    """Protocol or provenance did not match the locked diagnostic."""


def _require_revision(value: object) -> str:
    if (
        type(value) is not str
        or len(value) != 40
        or any(char not in _HEX for char in value)
    ):
        raise DiagnosticInvalidError(
            "arena revision must be a 40-character lowercase hexadecimal commit"
        )
    return value


def _agreement_document(total: int, agreements: int) -> dict[str, object]:
    disagreements = total - agreements
    return {
        "total": total,
        "agreements": agreements,
        "disagreements": disagreements,
        "agreement_rate": None if total == 0 else agreements / total,
        "disagreement_rate": None if total == 0 else disagreements / total,
    }


def _breakdown(
    totals: Counter[str], agreements: Counter[str]
) -> dict[str, dict[str, object]]:
    return {
        key: _agreement_document(totals[key], agreements[key]) for key in sorted(totals)
    }


def _legal_action_bucket(count: int) -> str:
    if count == 2:
        return "2"
    if count <= 4:
        return "3-4"
    if count <= 8:
        return "5-8"
    if count <= 16:
        return "9-16"
    return "17+"


def _post_discard_shanten(
    observation: MaterializationDecisionObservation,
    action: DiscardAction,
) -> int | None:
    """Use only the player-visible concealed hand and public shanten API."""
    concealed = list(observation.decision.input.own_hand.concealed_tiles)
    for index, tile in enumerate(concealed):
        if tile == action.tile:
            del concealed[index]
            break
    else:
        return None

    try:
        return calculate_shanten(concealed)
    except TypeError, ValueError:
        return None


def _create_locked_comparator() -> Policy:
    spec = POLICY_CATALOG.get(COMPARATOR_IDENTITY)
    if spec is None or spec.identity != COMPARATOR_IDENTITY:
        raise DiagnosticInvalidError("locked comparator alias is not available")
    try:
        policy = spec.factory()
    except Exception as error:
        raise DiagnosticInvalidError(
            "locked comparator factory could not construct the policy"
        ) from error
    if type(policy).__name__ != COMPARATOR_CLASS_NAME:
        raise DiagnosticInvalidError(
            "locked comparator alias resolved to a different implementation class"
        )
    return policy


class DisagreementAnalyzer:
    """Streaming aggregate over exact replay-time DecisionContext values."""

    def __init__(
        self,
        *,
        policy_factory: Callable[[], Policy] = _create_locked_comparator,
    ) -> None:
        self._policy_factory = policy_factory
        self._policies: dict[tuple[str, int], Policy] = {}
        self._choice_decisions = 0
        self._forced_decisions = 0
        self._agreements = 0
        self._per_bot_total: Counter[int] = Counter()
        self._per_bot_agree: Counter[int] = Counter()
        self._per_kind_total: Counter[str] = Counter()
        self._per_kind_agree: Counter[str] = Counter()
        self._hand_total: Counter[str] = Counter()
        self._hand_agree: Counter[str] = Counter()
        self._riichi_total: Counter[str] = Counter()
        self._riichi_agree: Counter[str] = Counter()
        self._legal_count_total: Counter[int] = Counter()
        self._legal_count_agree: Counter[int] = Counter()
        self._legal_bucket_total: Counter[str] = Counter()
        self._legal_bucket_agree: Counter[str] = Counter()
        self._confusion: Counter[tuple[str, str]] = Counter()
        self._teacher_action_indices: Counter[int] = Counter()
        self._baseline_action_indices: Counter[int] = Counter()
        self._discard_shanten: Counter[str] = Counter()

    def _policy_for(self, observation: MaterializationDecisionObservation) -> Policy:
        key = (observation.game_id, int(observation.actor_seat))
        policy = self._policies.get(key)
        if policy is None:
            try:
                policy = self._policy_factory()
            except DisagreementDiagnosticError:
                raise
            except Exception as error:
                raise DiagnosticBlockedError(
                    "baseline policy factory failed"
                ) from error
            self._policies[key] = policy
        return policy

    def observe(self, observation: MaterializationDecisionObservation) -> None:
        if not isinstance(observation, MaterializationDecisionObservation):
            raise TypeError("observation must be a MaterializationDecisionObservation")
        if observation.bot_id not in _BOT_NAMES:
            raise DiagnosticBlockedError("replay exposed an unexpected target bot id")
        if observation.legal_action_count != len(observation.decision.legal_actions):
            raise DiagnosticBlockedError(
                "replay observation legal-action count is inconsistent"
            )
        if observation.teacher_action not in observation.decision.legal_actions:
            raise DiagnosticBlockedError(
                "teacher action is not an exact legal action in the replay decision"
            )

        if observation.legal_action_count < 2:
            if observation.legal_action_count != 1:
                raise DiagnosticBlockedError(
                    "forced decision must contain exactly one legal action"
                )
            self._forced_decisions += 1
            return

        policy = self._policy_for(observation)
        try:
            baseline_action = execute_policy(policy, observation.decision)
        except Exception as error:
            raise DiagnosticBlockedError(
                "baseline execute_policy validation failed"
            ) from error

        try:
            teacher_index = encode_action(observation.teacher_action)
            baseline_index = encode_action(baseline_action)
            teacher_family = action_family(observation.teacher_action)
            baseline_family = action_family(baseline_action)
        except Exception as error:
            raise DiagnosticBlockedError(
                "canonical action classification failed"
            ) from error

        agreement = baseline_action == observation.teacher_action
        self._choice_decisions += 1
        self._agreements += int(agreement)
        self._per_bot_total[observation.bot_id] += 1
        self._per_bot_agree[observation.bot_id] += int(agreement)

        kind = observation.decision_kind.value
        self._per_kind_total[kind] += 1
        self._per_kind_agree[kind] += int(agreement)

        hand_key = "open" if observation.is_open_hand else "closed"
        self._hand_total[hand_key] += 1
        self._hand_agree[hand_key] += int(agreement)

        riichi_key = "riichi" if observation.is_riichi_declared else "non_riichi"
        self._riichi_total[riichi_key] += 1
        self._riichi_agree[riichi_key] += int(agreement)

        legal_count = observation.legal_action_count
        self._legal_count_total[legal_count] += 1
        self._legal_count_agree[legal_count] += int(agreement)
        legal_bucket = _legal_action_bucket(legal_count)
        self._legal_bucket_total[legal_bucket] += 1
        self._legal_bucket_agree[legal_bucket] += int(agreement)

        self._confusion[(teacher_family, baseline_family)] += 1
        self._teacher_action_indices[teacher_index] += 1
        self._baseline_action_indices[baseline_index] += 1

        if (
            not agreement
            and isinstance(observation.teacher_action, DiscardAction)
            and isinstance(baseline_action, DiscardAction)
        ):
            teacher_shanten = _post_discard_shanten(
                observation, observation.teacher_action
            )
            baseline_shanten = _post_discard_shanten(observation, baseline_action)
            if teacher_shanten is None or baseline_shanten is None:
                classification = "unavailable"
            elif baseline_shanten > teacher_shanten:
                classification = "baseline_shanten_worse"
            elif teacher_shanten > baseline_shanten:
                classification = "teacher_shanten_worse"
            else:
                classification = "same_shanten"
            self._discard_shanten[classification] += 1

    def aggregate_document(self) -> dict[str, object]:
        per_bot: dict[str, object] = {}
        for bot_id, name in TARGET_BOTS:
            per_bot[name] = {
                "bot_id": bot_id,
                **_agreement_document(
                    self._per_bot_total[bot_id], self._per_bot_agree[bot_id]
                ),
            }

        confusion: dict[str, dict[str, int]] = {}
        for (teacher, baseline), count in sorted(self._confusion.items()):
            confusion.setdefault(teacher, {})[baseline] = count

        exact_legal_counts = {
            str(count): _agreement_document(
                self._legal_count_total[count], self._legal_count_agree[count]
            )
            for count in sorted(self._legal_count_total)
        }
        discard_total = sum(self._discard_shanten.values())
        discard_document = {
            "discard_vs_discard_disagreements": discard_total,
            "baseline_shanten_worse": self._discard_shanten["baseline_shanten_worse"],
            "teacher_shanten_worse": self._discard_shanten["teacher_shanten_worse"],
            "same_shanten": self._discard_shanten["same_shanten"],
            "unavailable": self._discard_shanten["unavailable"],
        }

        return {
            "coverage": {
                "choice_decisions": self._choice_decisions,
                "forced_decisions": self._forced_decisions,
                "observed_decisions": (self._choice_decisions + self._forced_decisions),
            },
            "agreement": _agreement_document(self._choice_decisions, self._agreements),
            "per_bot": per_bot,
            "per_decision_kind": _breakdown(self._per_kind_total, self._per_kind_agree),
            "action_family_confusion": confusion,
            "hand_state": _breakdown(self._hand_total, self._hand_agree),
            "riichi_state": _breakdown(self._riichi_total, self._riichi_agree),
            "legal_action_count": {
                "exact": exact_legal_counts,
                "buckets": _breakdown(
                    self._legal_bucket_total, self._legal_bucket_agree
                ),
            },
            "action_index_counts": {
                "teacher": {
                    str(index): self._teacher_action_indices[index]
                    for index in sorted(self._teacher_action_indices)
                },
                "baseline": {
                    str(index): self._baseline_action_indices[index]
                    for index in sorted(self._baseline_action_indices)
                },
            },
            "discard_shanten_disagreement": discard_document,
        }

    def finalize(
        self,
        source: MaterializedSource,
        *,
        arena_revision: str,
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

        runtime_riichienv = version("riichienv")
        if runtime_riichienv != EXPECTED_RIICHIENV_VERSION:
            raise DiagnosticInvalidError(
                "RiichiEnv runtime version differs from the locked diagnostic"
            )

        aggregate = self.aggregate_document()
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
                "binding_arena_revision": COMPARATOR_BINDING_ARENA_REVISION,
                "lisjong_revision": LISJONG_REVISION,
                "riichienv_version": runtime_riichienv,
            },
            "diagnostic_runtime": {
                "arena_revision": arena_revision,
            },
            "materialization": gate.to_document(),
            "aggregate": aggregate,
            "interpretation_boundary": [
                "This is a behavior-disagreement diagnostic, not a strength evaluation.",
                "Teacher actions are observed choices, not correctness labels.",
                "No hidden opponent hand, wall truth, or future outcome enters the comparison.",
                "Forced decisions are coverage only and are excluded from agreement rates.",
            ],
            "recommended_next_hypothesis": (
                "Inspect discard-vs-discard disagreements where the baseline has "
                "worse post-discard shanten using synthetic or first-party tests. "
                "If that subset is empty, inspect same-shanten discard disagreements "
                "with an existing stable public efficiency primitive before adding "
                "any new diagnostic API."
            ),
        }


def run_diagnostic(
    snapshot: RecentGamesSnapshot,
    output_dir: str | Path,
    *,
    arena_revision: str,
    policy_factory: Callable[[], Policy] = _create_locked_comparator,
) -> dict[str, object]:
    """Run the exact offline #170 replay and return aggregate-only evidence."""
    _require_revision(arena_revision)
    analyzer = DisagreementAnalyzer(policy_factory=policy_factory)
    source = materialize_local_corpus(
        snapshot,
        output_dir,
        decision_observer=analyzer.observe,
    )
    return analyzer.finalize(source, arena_revision=arena_revision)


__all__ = [
    "COMPARATOR_BINDING_ARENA_REVISION",
    "COMPARATOR_CLASS_NAME",
    "COMPARATOR_IDENTITY",
    "DiagnosticBlockedError",
    "DiagnosticInvalidError",
    "DisagreementAnalyzer",
    "DisagreementDiagnosticError",
    "EXPECTED_CHOICE_ROWS",
    "EXPECTED_DECISION_OPPORTUNITIES",
    "EXPECTED_FORCED_ROWS",
    "EXPECTED_GAMES",
    "EXPECTED_RIICHIENV_VERSION",
    "LISJONG_REVISION",
    "OUTCOME_BLOCKED",
    "OUTCOME_COMPLETE",
    "OUTCOME_INVALID",
    "SCHEMA_VERSION",
    "run_diagnostic",
]
