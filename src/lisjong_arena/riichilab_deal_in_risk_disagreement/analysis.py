"""Issue #455 deal-in-risk discard disagreement aggregate diagnostic.

Issue #251と同じexact #170 corpus・同じobserver seamを使い、comparatorを現Champion
へ替えた別identityのbounded runである。#251のlocked inputs・結果とは混ぜない。
"""

import json
import os
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from importlib.metadata import PackageNotFoundError, distribution, version
from pathlib import Path

from lisjong.policy_contract import Policy, execute_policy
from lisjong.policy_contract.action import DiscardAction
from lisjong.policy_contract.meld import MeldKind
from lisjong.policy_contract.player_state import PlayerPublicState
from lisjong.policy_contract.policy_input import PolicyInput
from lisjong.policy_contract.riichi import RiichiState
from lisjong.policy_contract.tile import TileType
from lisjong.policy_contract.wind import Wind

from lisjong_arena.policy_catalog import POLICY_CATALOG
from lisjong_arena.riichilab_corpus.models import (
    TARGET_BOTS,
    CorpusError,
    RecentGamesSnapshot,
    sha256_bytes,
)
from lisjong_arena.riichilab_corpus.persistence import (
    GAMES_DIRECTORY,
    group_participations,
    load_cache_index,
)
from lisjong_arena.riichilab_corpus.validation import parse_jsonl_gzip
from lisjong_arena.riichilab_disagreement_diagnostic.analysis import (
    EXPECTED_CHOICE_ROWS,
    EXPECTED_DECISION_OPPORTUNITIES,
    EXPECTED_FORCED_ROWS,
    EXPECTED_GAMES,
    EXPECTED_RIICHIENV_VERSION,
    OUTCOME_BLOCKED,
    OUTCOME_INVALID,
    DiagnosticBlockedError,
    DiagnosticInvalidError,
    _agreement_document,
    _post_discard_shanten,
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

SCHEMA_VERSION = "arena-riichilab-deal-in-risk-disagreement-v1"
OUTCOME_COMPLETE = "DEAL-IN-RISK DISAGREEMENT DIAGNOSTIC COMPLETE"

COMPARATOR_IDENTITY = "placement-aware-speed-call"
COMPARATOR_CLASS_NAME = "PlacementAwareSpeedCallPolicy"
# #441の対象期間にChampionが稼働していたrevisionとbackend。
LISJONG_REVISION = "51e832e50a0ee71eac65e4a017f46590e7438c04"
SHANTEN_BACKEND = "rust"
SHANTEN_BACKEND_ENVIRONMENT_VARIABLE = "LISJONG_SHANTEN_BACKEND"

REGION_R1 = "R1"
REGION_R2 = "R2"
REGION_F = "F"
REGIONS = (REGION_R1, REGION_R2, REGION_F)
RIICHI_REGIONS = (REGION_R1, REGION_R2)

TURN_BANDS = ("1-6", "7-12", "13+")
OWN_MELD_KEYS = ("closed", "open")

_BOT_NAMES = {bot_id: name for bot_id, name in TARGET_BOTS}
_OPEN_MELD_KINDS = frozenset(
    {MeldKind.CHI, MeldKind.PON, MeldKind.DAIMINKAN, MeldKind.KAKAN}
)
_ACTION_EVENT_TYPES = frozenset(
    {"tsumo", "dahai", "chi", "pon", "daiminkan", "ankan", "kakan"}
)
_BAKAZE_BY_WIND = {
    Wind.EAST: "E",
    Wind.SOUTH: "S",
    Wind.WEST: "W",
    Wind.NORTH: "N",
}

# (game_id, seat, bakaze, kyoku, honba, その局での自席の打牌の通し番号)
DiscardKey = tuple[str, int, str, int, int, int]


def _has_open_meld(player: PlayerPublicState) -> bool:
    """チー・ポン・大明槓（加槓したポンを含む）を持つか。暗槓だけは副露としない。"""
    return any(meld.kind in _OPEN_MELD_KINDS for meld in player.melds)


def _turn_band(own_discard_ordinal: int) -> str:
    if own_discard_ordinal <= 6:
        return TURN_BANDS[0]
    if own_discard_ordinal <= 12:
        return TURN_BANDS[1]
    return TURN_BANDS[2]


def classify_region(policy_input: PolicyInput) -> str | None:
    """打牌直前のplayer-visibleな情報だけで区分R1 / R2 / Fを決める。"""
    seat = int(policy_input.self_seat)
    others = [
        player for index, player in enumerate(policy_input.players) if index != seat
    ]
    riichi_count = sum(player.riichi is RiichiState.ACCEPTED for player in others)
    if riichi_count == 1:
        return REGION_R1
    if riichi_count >= 2:
        return REGION_R2
    if any(_has_open_meld(player) for player in others):
        return REGION_F
    return None


@dataclass(slots=True)
class _RiichiDeclarationTracker:
    """1 game x 1 seatの観測列から、他家のリーチ宣言牌の位置を特定する。

    `PolicyInput`は宣言牌の位置を持たないため、同じ席の連続する観測の間で
    他家のリーチ状態が変わった区間から宣言牌のindex範囲を求める。鳴きで自席の
    手番が飛び範囲が1つに絞れない場合は、範囲のまま保持する。
    """

    kyoku: tuple[Wind, int, int] | None = None
    discards_while_not_riichi: dict[int, int] = field(default_factory=dict)
    declaration_range: dict[int, tuple[int, int]] = field(default_factory=dict)

    def update(self, policy_input: PolicyInput) -> None:
        round_state = policy_input.round
        kyoku = (round_state.round_wind, round_state.hand_number, round_state.honba)
        if kyoku != self.kyoku:
            self.kyoku = kyoku
            self.discards_while_not_riichi = {}
            self.declaration_range = {}

        seat = int(policy_input.self_seat)
        for index, player in enumerate(policy_input.players):
            if index == seat:
                continue
            count = len(player.discards)
            if player.riichi is RiichiState.NONE:
                self.discards_while_not_riichi[index] = count
                continue
            if index in self.declaration_range:
                continue
            first = self.discards_while_not_riichi.get(index, 0)
            if player.riichi is RiichiState.DECLARED:
                # 宣言から成立までの間に観測できるのは宣言牌への応答だけである。
                first = count - 1
            if first < 0 or first >= count:
                raise DiagnosticBlockedError(
                    "riichi declaration discard could not be located in the replay"
                )
            # リーチ後の打牌はすべてツモ切りなので、手出しより前は宣言牌ではない。
            candidates = [
                candidate
                for candidate in range(first, count)
                if all(
                    discard.tsumogiri for discard in player.discards[candidate + 1 :]
                )
            ]
            self.declaration_range[index] = (candidates[0], candidates[-1])


def _genbutsu_bounds(
    policy_input: PolicyInput,
    riichi_seat: int,
    declaration_range: tuple[int, int],
) -> tuple[frozenset[TileType], frozenset[TileType]]:
    """1人のリーチ者に対する現物の、確実な集合と可能性のある集合を返す。

    現物はリーチ者自身の捨て牌と、宣言牌より後に他の席が切った牌である。
    """
    riichi_player = policy_input.players[riichi_seat]
    river = frozenset(discard.tile.tile_type for discard in riichi_player.discards)

    def passed_after(declaration_index: int) -> frozenset[TileType]:
        declaration_order = riichi_player.discards[declaration_index].order
        return frozenset(
            discard.tile.tile_type
            for index, player in enumerate(policy_input.players)
            if index != riichi_seat
            for discard in player.discards
            if discard.order > declaration_order
        )

    earliest, latest = declaration_range
    return river | passed_after(latest), river | passed_after(earliest)


def _is_genbutsu(
    tile_type: TileType,
    bounds: tuple[tuple[frozenset[TileType], frozenset[TileType]], ...],
) -> bool | None:
    """全リーチ者に対する現物かどうか。宣言牌の位置で結論が変わる場合は`None`。"""
    if all(tile_type in certain for certain, _possible in bounds):
        return True
    if any(tile_type not in possible for _certain, possible in bounds):
        return False
    return None


def ronned_discard_keys(
    events: Iterable[Mapping[str, object]],
    *,
    game_id: str,
    target_seats: Mapping[int, int],
) -> dict[DiscardKey, int]:
    """公開MJAI eventから、target seatの打牌がロンされた箇所をbot idと対で返す。

    槍槓（加槓へのロン）は打牌ではないので含めない。
    """
    ronned: dict[DiscardKey, int] = {}
    kyoku: tuple[str, int, int] | None = None
    discard_counts = [0, 0, 0, 0]
    last_action: tuple[object, object] | None = None
    for event in events:
        event_type = event.get("type")
        if event_type == "start_kyoku":
            kyoku = (str(event["bakaze"]), int(event["kyoku"]), int(event["honba"]))
            discard_counts = [0, 0, 0, 0]
            last_action = None
            continue
        if event_type == "hora":
            actor = event.get("actor")
            target = event.get("target")
            if (
                actor != target
                and last_action == ("dahai", target)
                and target in target_seats
            ):
                if kyoku is None:
                    raise DiagnosticBlockedError("hora event precedes start_kyoku")
                key = (game_id, int(target), *kyoku, discard_counts[int(target)])
                ronned[key] = target_seats[int(target)]
            continue
        if event_type not in _ACTION_EVENT_TYPES:
            continue
        if event_type == "dahai":
            discard_counts[int(event["actor"])] += 1
        last_action = (event_type, event.get("actor"))
    return ronned


def collect_ronned_discards(
    snapshot: RecentGamesSnapshot, output_dir: str | Path
) -> dict[DiscardKey, int]:
    """identity gateを通過済みのexact corpusから、ロンされた打牌の位置を集める。"""
    output = Path(output_dir)
    try:
        entries = load_cache_index(output)
        grouped = group_participations(snapshot)
        ronned: dict[DiscardKey, int] = {}
        for game_id in snapshot.game_ids:
            payload = (output / GAMES_DIRECTORY / f"{game_id}.jsonl.gz").read_bytes()
            if sha256_bytes(payload) != entries[game_id]["compressed_sha256"]:
                raise CorpusError("cached game bytes changed after materialization")
            ronned.update(
                ronned_discard_keys(
                    parse_jsonl_gzip(payload),
                    game_id=game_id,
                    target_seats={item.seat: item.bot_id for item in grouped[game_id]},
                )
            )
    except (CorpusError, KeyError, TypeError, ValueError) as error:
        raise DiagnosticBlockedError(
            "public ron events could not be read from the exact corpus"
        ) from error
    return ronned


def _runtime_lisjong_revision() -> str | None:
    """installされたlisjongのVCS commit。direct URL installでなければ`None`。"""
    try:
        text = distribution("lisjong").read_text("direct_url.json")
    except PackageNotFoundError:
        return None
    if text is None:
        return None
    try:
        commit = json.loads(text)["vcs_info"]["commit_id"]
    except KeyError, TypeError, ValueError:
        return None
    return commit if type(commit) is str else None


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


def _require_locked_runtime() -> str:
    """comparatorのrevision・backend・RiichiEnvが固定値と一致することを確認する。"""
    if _runtime_lisjong_revision() != LISJONG_REVISION:
        raise DiagnosticInvalidError(
            "installed lisjong revision differs from the locked comparator revision"
        )
    if os.environ.get(SHANTEN_BACKEND_ENVIRONMENT_VARIABLE) != SHANTEN_BACKEND:
        raise DiagnosticInvalidError(
            "shanten backend differs from the locked comparator backend"
        )
    runtime_riichienv = version("riichienv")
    if runtime_riichienv != EXPECTED_RIICHIENV_VERSION:
        raise DiagnosticInvalidError(
            "RiichiEnv runtime version differs from the locked diagnostic"
        )
    return runtime_riichienv


class DealInRiskDisagreementAnalyzer:
    """Streaming aggregate over deal-in-risk discard choices of the strong bots."""

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
        # (bot_id, 件数だけ報告する項目)
        self._out_of_scope: Counter[tuple[int, str]] = Counter()
        # (bot_id, region, 項目...)
        self._counts: Counter[tuple] = Counter()
        self._in_scope_rows: dict[DiscardKey, tuple[int, str, bool]] = {}

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

        own_riichi = policy_input.players[seat].riichi
        teacher_discards = isinstance(teacher_action, DiscardAction)

        if observation.legal_action_count < 2:
            if observation.legal_action_count != 1:
                raise DiagnosticBlockedError(
                    "forced decision must contain exactly one legal action"
                )
            self._forced_decisions += 1
            self._out_of_scope[(bot_id, "forced_decisions")] += 1
            if teacher_discards and own_riichi is RiichiState.ACCEPTED:
                self._out_of_scope[(bot_id, "own_riichi_accepted_forced_discards")] += 1
            return

        # #251と同じく全選択行でcomparatorを実行し、instanceはgame x seatで分ける。
        policy = self._policy_for(observation)
        try:
            comparator_action = execute_policy(policy, decision)
        except Exception as error:
            raise DiagnosticBlockedError(
                "comparator execute_policy validation failed"
            ) from error
        self._choice_decisions += 1

        if not teacher_discards:
            self._out_of_scope[(bot_id, "non_discard_choice_decisions")] += 1
            return
        self._out_of_scope[(bot_id, "discard_choice_rows")] += 1
        if own_riichi is RiichiState.ACCEPTED:
            self._out_of_scope[(bot_id, "own_riichi_accepted_discard_choice_rows")] += 1
            return
        if own_riichi is RiichiState.DECLARED:
            self._out_of_scope[(bot_id, "own_riichi_declaration_discard_rows")] += 1
            return
        region = classify_region(policy_input)
        if region is None:
            self._out_of_scope[(bot_id, "no_region_discard_choice_rows")] += 1
            return

        agreement = comparator_action == teacher_action
        own_discard_ordinal = len(policy_input.players[seat].discards) + 1
        round_state = policy_input.round
        key = (
            observation.game_id,
            seat,
            _BAKAZE_BY_WIND[round_state.round_wind],
            round_state.hand_number,
            round_state.honba,
            own_discard_ordinal,
        )
        if key in self._in_scope_rows:
            raise DiagnosticBlockedError(
                "replay exposed two discard choices at one discard position"
            )
        self._in_scope_rows[key] = (bot_id, region, agreement)

        cell = (bot_id, region)
        band = _turn_band(own_discard_ordinal)
        own_meld = "open" if _has_open_meld(policy_input.players[seat]) else "closed"
        for dimension in (
            ("total",),
            ("turn_band", band),
            ("own_meld", own_meld),
            ("turn_band_by_own_meld", f"{band}|{own_meld}"),
        ):
            self._counts[(*cell, "rows", *dimension)] += 1
            self._counts[(*cell, "agreements", *dimension)] += int(agreement)
        if agreement:
            return

        if not isinstance(comparator_action, DiscardAction):
            self._counts[(*cell, "comparator_non_discard")] += 1
            return
        self._counts[(*cell, "discard_vs_discard")] += 1

        teacher_shanten = _post_discard_shanten(observation, teacher_action)
        comparator_shanten = _post_discard_shanten(observation, comparator_action)
        if teacher_shanten is None or comparator_shanten is None:
            shanten_key = "unavailable"
        elif teacher_shanten > comparator_shanten:
            shanten_key = "teacher_shanten_worse"
        elif comparator_shanten > teacher_shanten:
            shanten_key = "comparator_shanten_worse"
        else:
            shanten_key = "same_shanten"
        self._counts[(*cell, "shanten", shanten_key)] += 1

        if region not in RIICHI_REGIONS:
            return
        bounds = tuple(
            _genbutsu_bounds(policy_input, index, tracker.declaration_range[index])
            for index, player in enumerate(policy_input.players)
            if index != seat and player.riichi is RiichiState.ACCEPTED
        )
        teacher_genbutsu = _is_genbutsu(teacher_action.tile.tile_type, bounds)
        comparator_genbutsu = _is_genbutsu(comparator_action.tile.tile_type, bounds)
        if teacher_genbutsu is None or comparator_genbutsu is None:
            genbutsu_key = "unavailable"
        elif teacher_genbutsu and comparator_genbutsu:
            genbutsu_key = "both_genbutsu"
        elif teacher_genbutsu:
            genbutsu_key = "teacher_genbutsu_comparator_not"
        elif comparator_genbutsu:
            genbutsu_key = "comparator_genbutsu_teacher_not"
        else:
            genbutsu_key = "neither_genbutsu"
        self._counts[(*cell, "genbutsu", genbutsu_key)] += 1

    def _region_document(
        self,
        bot_ids: tuple[int, ...],
        region: str,
        ronned: Mapping[DiscardKey, int],
    ) -> dict[str, object]:
        def total(*item: str) -> int:
            return sum(self._counts[(bot_id, region, *item)] for bot_id in bot_ids)

        def agreement(*dimension: str) -> dict[str, object]:
            return _agreement_document(
                total("rows", *dimension), total("agreements", *dimension)
            )

        ronned_by_agreement = Counter(
            agreed
            for key, (bot_id, row_region, agreed) in self._in_scope_rows.items()
            if bot_id in bot_ids and row_region == region and key in ronned
        )
        document: dict[str, object] = {
            "agreement": agreement("total"),
            "comparator_non_discard_disagreements": total("comparator_non_discard"),
            "discard_shanten_disagreement": {
                "discard_vs_discard_disagreements": total("discard_vs_discard"),
                "teacher_shanten_worse": total("shanten", "teacher_shanten_worse"),
                "comparator_shanten_worse": total(
                    "shanten", "comparator_shanten_worse"
                ),
                "same_shanten": total("shanten", "same_shanten"),
                "unavailable": total("shanten", "unavailable"),
            },
            "turn_band": {band: agreement("turn_band", band) for band in TURN_BANDS},
            "own_meld": {key: agreement("own_meld", key) for key in OWN_MELD_KEYS},
            "turn_band_by_own_meld": {
                f"{band}|{key}": agreement("turn_band_by_own_meld", f"{band}|{key}")
                for band in TURN_BANDS
                for key in OWN_MELD_KEYS
            },
            "teacher_discard_ronned": {
                "total": ronned_by_agreement[True] + ronned_by_agreement[False],
                "in_agreement_rows": ronned_by_agreement[True],
                "in_disagreement_rows": ronned_by_agreement[False],
            },
        }
        if region in RIICHI_REGIONS:
            document["genbutsu_disagreement"] = {
                "discard_vs_discard_disagreements": total("discard_vs_discard"),
                "teacher_genbutsu_comparator_not": total(
                    "genbutsu", "teacher_genbutsu_comparator_not"
                ),
                "comparator_genbutsu_teacher_not": total(
                    "genbutsu", "comparator_genbutsu_teacher_not"
                ),
                "both_genbutsu": total("genbutsu", "both_genbutsu"),
                "neither_genbutsu": total("genbutsu", "neither_genbutsu"),
                "unavailable": total("genbutsu", "unavailable"),
            }
        return document

    def _bots_document(
        self, bot_ids: tuple[int, ...], ronned: Mapping[DiscardKey, int]
    ) -> dict[str, object]:
        out_of_scope = {
            name: sum(self._out_of_scope[(bot_id, name)] for bot_id in bot_ids)
            for name in (
                "forced_decisions",
                "non_discard_choice_decisions",
                "discard_choice_rows",
                "own_riichi_accepted_forced_discards",
                "own_riichi_accepted_discard_choice_rows",
                "own_riichi_declaration_discard_rows",
                "no_region_discard_choice_rows",
            )
        }
        return {
            "counts_only": {
                **out_of_scope,
                "teacher_discards_ronned_any_row": sum(
                    bot_id in bot_ids for bot_id in ronned.values()
                ),
            },
            "regions": {
                region: self._region_document(bot_ids, region, ronned)
                for region in REGIONS
            },
        }

    def aggregate_document(
        self, ronned: Mapping[DiscardKey, int] | None = None
    ) -> dict[str, object]:
        ronned = {} if ronned is None else ronned
        return {
            "coverage": {
                "choice_decisions": self._choice_decisions,
                "forced_decisions": self._forced_decisions,
                "observed_decisions": self._choice_decisions + self._forced_decisions,
            },
            "all_bots": self._bots_document(
                tuple(bot_id for bot_id, _name in TARGET_BOTS), ronned
            ),
            "per_bot": {
                name: {"bot_id": bot_id, **self._bots_document((bot_id,), ronned)}
                for bot_id, name in TARGET_BOTS
            },
        }

    def finalize(
        self,
        source: MaterializedSource,
        *,
        arena_revision: str,
        ronned_discards: Mapping[DiscardKey, int],
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
            "diagnostic_runtime": {
                "arena_revision": arena_revision,
            },
            "materialization": gate.to_document(),
            "aggregate": self.aggregate_document(ronned_discards),
            "interpretation_boundary": [
                "This is a behavior-disagreement diagnostic, not a strength evaluation.",
                "Teacher actions are observed choices, not correctness labels.",
                "No hidden opponent hand, wall truth, or future state enters the "
                "comparison; only the public ron event on the teacher's own "
                "discard is counted.",
                "Whether the comparator's alternative discard would have dealt in "
                "is not evaluated.",
                "Rows depend on each other within a game and a bot; no interval "
                "or test is computed.",
                "The three-bot total is not a representative of the top-bot group.",
            ],
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
    # 結果を計算する前に、固定したcomparator環境でなければ停止する。
    _require_locked_runtime()
    analyzer = DealInRiskDisagreementAnalyzer(policy_factory=policy_factory)
    source = materialize_local_corpus(
        snapshot,
        output_dir,
        decision_observer=analyzer.observe,
    )
    ronned = collect_ronned_discards(snapshot, output_dir)
    return analyzer.finalize(
        source, arena_revision=arena_revision, ronned_discards=ronned
    )


__all__ = [
    "COMPARATOR_CLASS_NAME",
    "COMPARATOR_IDENTITY",
    "DealInRiskDisagreementAnalyzer",
    "LISJONG_REVISION",
    "OUTCOME_BLOCKED",
    "OUTCOME_COMPLETE",
    "OUTCOME_INVALID",
    "REGIONS",
    "SCHEMA_VERSION",
    "SHANTEN_BACKEND",
    "classify_region",
    "collect_ronned_discards",
    "ronned_discard_keys",
    "run_diagnostic",
]
