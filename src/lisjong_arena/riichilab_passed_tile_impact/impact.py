"""Issue #503: 確定安全牌候補の影響範囲を、固定した423席の入力で数える。

lisbun/lisjong#312の`PassedTileGenbutsuPlacementAwareSpeedCallPolicy`について、
lisbun/lisjong-arena#489と同じ固定入力（#441のChampion実戦 dev 209席・baseline 214席）の
打牌選択行で、候補が使う経路に入る局面（対象局面）の数と、候補とChampionの打牌が違う数を
分けて数える。

- 対象局面は、打牌前の入力と合法手だけで決める（設計 5節）。実戦で選ばれた打牌にも、候補が返す
  `PolicyDecision.analysis`が`None`かどうかにも依存させない。
- 候補とChampionは同じ`DecisionContext`で1回ずつ実行し、返された打牌を比べる。実戦の打牌は
  Championの再現確認と、選ばれた牌が確定安全牌に入るかの参考にだけ使う。
- 強さの評価ではない。この入力は対象選定に使ったので、採否判断の根拠にしない。
- 個別行・対局IDは出力しない。件数だけを返す。
"""

import hashlib
import time
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from lisjong.belief.riichi_safe_tiles import (
    RIICHI_SAFE_TILE_RULE_ASSUMPTION,
    derive_riichi_safe_tiles,
)
from lisjong.policies import (
    PassedTileGenbutsuPlacementAwareSpeedCallPolicy,
    PlacementAwareSpeedCallPolicy,
)
from lisjong.policies.placement_aware_speed_call import (
    GameSituationMode,
    situation_mode,
)
from lisjong.policy_contract.action import DiscardAction
from lisjong.policy_contract.decision_context import DecisionContext
from lisjong.policy_contract.policy_input import PolicyInput
from lisjong.policy_contract.riichi import RiichiState
from lisjong.policy_contract.seat import Seat
from lisjong.structural_efficiency import (
    StructuralShantenEvaluator,
    evaluate_post_discard_hands,
)

from lisjong_arena.riichilab_corpus.models import CorpusError, sha256_bytes
from lisjong_arena.riichilab_corpus.validation import (
    parse_jsonl_gzip,
    validate_mjai_log,
)
from lisjong_arena.riichilab_deal_in_risk_disagreement.analysis import (
    REGION_R1,
    _RiichiDeclarationTracker,
    classify_region,
)
from lisjong_arena.riichilab_disagreement_diagnostic.analysis import (
    DiagnosticBlockedError,
    DiagnosticInvalidError,
    _require_revision,
)
from lisjong_arena.riichilab_r1_choice_loss_diagnostic.champion_play import (
    CHAMPION_GROUPS,
    PLAYED_AT_THRESHOLD,
    SHARED_GAMES,
    _select_games,
)
from lisjong_arena.riichilab_r1_choice_loss_diagnostic.classification import (
    GENBUTSU_AVAILABLE,
    classify_r1_choice,
)
from lisjong_arena.riichilab_self_history.mjai import log_path
from lisjong_arena.riichilab_source_pilot.dataset import SOURCE_GAME_MODE
from lisjong_arena.riichilab_source_pilot.materialization import (
    MaterializationDecisionObservation,
    materialize_game,
)

SCHEMA_VERSION = "arena-riichilab-passed-tile-impact-count-v1"
OUTCOME_COMPLETE = "PASSED TILE IMPACT COUNT COMPLETE"

# 対象局面の区分（設計 5節）。
CATEGORY_S1 = "S1"  # 河の現物が合法打牌になく、通った牌だけが確定安全牌
CATEGORY_S2 = "S2"  # 河の現物が合法打牌にあり、通った牌でさらに候補が広がる
CATEGORY_RIVER_ONLY = "river_only"  # 確定安全牌の経路に入るが、河の現物と同じ集合
CATEGORY_NO_SAFE = "fold_no_safe"  # 単独リーチ・FOLDだが合法打牌に確定安全牌がない
CATEGORY_PUSH = "push"  # 単独リーチ・聴牌を保てる打牌がある
CATEGORIES = (
    CATEGORY_S1,
    CATEGORY_S2,
    CATEGORY_RIVER_ONLY,
    CATEGORY_NO_SAFE,
    CATEGORY_PUSH,
)
ENGAGED = (CATEGORY_S1, CATEGORY_S2, CATEGORY_RIVER_ONLY)
UNCHANGED_BY_DESIGN = (CATEGORY_NO_SAFE, CATEGORY_PUSH)


@dataclass(frozen=True, slots=True)
class Applicability:
    """打牌前の入力と合法手だけで決まる、候補の対象局面の区分。"""

    riichi_players: int
    category: str | None
    minimum_shanten: int | None
    all_last_top: bool
    declaration_known: bool | None
    safe_tile_types: frozenset | None

    def shanten_bucket(self) -> str:
        if self.minimum_shanten is None:
            return "unavailable"
        return "2+" if self.minimum_shanten >= 2 else str(self.minimum_shanten)


def classify_applicability(
    policy_input: PolicyInput, discards: tuple[DiscardAction, ...]
) -> Applicability:
    """候補の経路に入るかを、`PolicyInput`と合法打牌だけから決める。

    `category`は、他家のリーチ者（宣言済みを含む）がちょうど1人のときだけ決まる。
    """
    seat = int(policy_input.self_seat)
    riichi_players = sum(
        player.riichi is not RiichiState.NONE
        for index, player in enumerate(policy_input.players)
        if index != seat
    )
    top = situation_mode(policy_input) is GameSituationMode.ALL_LAST_TOP_SPEED
    if riichi_players != 1:
        return Applicability(riichi_players, None, None, top, None, None)

    minimum = min(
        evaluation.post_discard_shanten
        for evaluation in evaluate_post_discard_hands(
            policy_input, discards, StructuralShantenEvaluator()
        )
    )
    if minimum < 1:
        return Applicability(1, CATEGORY_PUSH, minimum, top, None, None)

    (player,) = derive_riichi_safe_tiles(policy_input).by_riichi_player
    legal = frozenset(action.tile.tile_type for action in discards)
    river_legal = bool(legal & player.river_tile_types)
    passed_new_legal = bool(
        legal & (player.passed_tile_types - player.river_tile_types)
    )
    if river_legal and passed_new_legal:
        category = CATEGORY_S2
    elif passed_new_legal:
        category = CATEGORY_S1
    elif river_legal:
        category = CATEGORY_RIVER_ONLY
    else:
        category = CATEGORY_NO_SAFE
    return Applicability(
        1, category, minimum, top, player.declaration_known, player.safe_tile_types
    )


class ImpactCounter:
    """対象局面の数と、候補がChampionと違う打牌を返した数を数える。"""

    def __init__(self) -> None:
        self._champion = PlacementAwareSpeedCallPolicy()
        self._candidate = PassedTileGenbutsuPlacementAwareSpeedCallPolicy()
        self.counts: Counter[tuple[str, str]] = Counter()
        self.unsupported: Counter[tuple[str, str]] = Counter()
        self.unresolved: Counter[tuple[str, str]] = Counter()
        self.seconds: Counter[tuple[str, str]] = Counter()

    def analyze_game(
        self,
        group: str,
        *,
        game_id: str,
        seat: int,
        bot_id: int,
        events: list[dict],
    ) -> None:
        tracker = _RiichiDeclarationTracker()
        local: Counter[str] = Counter()
        seconds: Counter[str] = Counter()

        def observe(observation: MaterializationDecisionObservation) -> None:
            policy_input = observation.decision.input
            tracker.update(policy_input)
            action = observation.teacher_action
            if observation.legal_action_count < 2:
                local["forced_decisions"] += 1
                return
            if not isinstance(action, DiscardAction):
                local["non_discard_choice_decisions"] += 1
                return
            local["discard_choice_rows"] += 1
            if policy_input.players[seat].riichi is not RiichiState.NONE:
                local["own_riichi_discard_choice_rows"] += 1
                return
            self._count_row(observation, tracker, local, seconds)

        result = materialize_game(
            events,
            game_id=game_id,
            target_seats={Seat(seat): bot_id},
            game_mode=SOURCE_GAME_MODE,
            decision_observer=observe,
        )
        if not result.supported:
            self.counts[(group, "unreplayable_games")] += 1
            self.unsupported[(group, result.unsupported_reason.value)] += 1
            return
        self.counts[(group, "replayed_games")] += 1
        for reason, count in result.unresolved_reasons:
            self.counts[(group, "unresolved_decisions")] += count
            self.unresolved[(group, reason)] += count
        for name, count in local.items():
            self.counts[(group, name)] += count
        for name, value in seconds.items():
            self.seconds[(group, name)] += value

    def _count_row(
        self,
        observation: MaterializationDecisionObservation,
        tracker: _RiichiDeclarationTracker,
        local: Counter[str],
        seconds: Counter[str],
    ) -> None:
        decision = observation.decision
        policy_input = decision.input
        played = observation.teacher_action
        discards = tuple(
            action
            for action in decision.legal_actions
            if isinstance(action, DiscardAction)
        )
        applicability = classify_applicability(policy_input, discards)
        region_r1 = classify_region(policy_input) == REGION_R1
        top = "top" if applicability.all_last_top else "not_top"

        if applicability.riichi_players == 0:
            local["no_riichi_rows"] += 1
        elif applicability.riichi_players >= 2:
            local["multiple_riichi_rows"] += 1
        if region_r1:
            local["r1_rows"] += 1
            self._count_legacy_target(observation, tracker, local)

        if applicability.category is None and not region_r1:
            return

        champion_action, champion_seconds = self._run(self._champion, decision)
        seconds["champion_all"] += champion_seconds
        local["champion_runs"] += 1
        if region_r1:
            local[
                "r1_replay_match" if champion_action == played else "r1_replay_mismatch"
            ] += 1
        category = applicability.category
        if category is None:
            return

        candidate_action, candidate_seconds = self._run(self._candidate, decision)
        seconds["candidate_all"] += candidate_seconds
        changed = candidate_action != champion_action
        bucket = applicability.shanten_bucket()
        safe = applicability.safe_tile_types
        champion_in_safe = champion_action.tile.tile_type in safe if safe else False
        played_in_safe = played.tile.tile_type in safe if safe else False
        cells = [category, f"{category}|shanten={bucket}|{top}"]
        if category in ENGAGED:
            cells.append("engaged")
            cells.append(f"engaged|shanten={bucket}|{top}")
        for cell in cells:
            local[f"rows|{cell}"] += 1
            if region_r1:
                local[f"r1_rows|{cell}"] += 1
            if changed:
                local[f"changed|{cell}"] += 1
                if region_r1:
                    local[f"r1_changed|{cell}"] += 1
            if category in ENGAGED:
                if champion_in_safe:
                    local[f"champion_choice_in_safe|{cell}"] += 1
                if played_in_safe:
                    local[f"played_in_safe|{cell}"] += 1
                if applicability.declaration_known is False:
                    local[f"declaration_unknown|{cell}"] += 1
        if category in ENGAGED:
            seconds["champion_engaged"] += champion_seconds
            seconds["candidate_engaged"] += candidate_seconds
        if category in UNCHANGED_BY_DESIGN and changed:
            local["unexpected_change_outside_engaged"] += 1
        if category == CATEGORY_RIVER_ONLY and changed:
            local["unexpected_change_river_only"] += 1

    @staticmethod
    def _run(policy, decision: DecisionContext):
        started = time.perf_counter()
        action = policy.choose_action(decision)
        return action, time.perf_counter() - started

    @staticmethod
    def _count_legacy_target(
        observation: MaterializationDecisionObservation,
        tracker: _RiichiDeclarationTracker,
        local: Counter[str],
    ) -> None:
        """#489が使った診断（宣言牌位置を推定するtracker）の値。照合用。"""
        choice = classify_r1_choice(observation, tracker)
        minimum = choice.minimum_post_discard_shanten
        if choice.genbutsu_availability != GENBUTSU_AVAILABLE or (
            minimum is None or minimum < 1
        ):
            return
        policy_input = observation.decision.input
        river = frozenset(
            discard.tile.tile_type
            for index, player in enumerate(policy_input.players)
            if index != int(policy_input.self_seat)
            and player.riichi is RiichiState.ACCEPTED
            for discard in player.discards
        )
        river_legal = any(
            isinstance(action, DiscardAction) and action.tile.tile_type in river
            for action in observation.decision.legal_actions
        )
        key = "legacy_river" if river_legal else "legacy_passed_only"
        local[f"{key}|minimum_shanten_1plus"] += 1

    def document(self, groups: tuple[str, ...]) -> dict[str, object]:
        def merge(counter: Counter[tuple[str, str]]) -> dict[str, float]:
            merged: Counter[str] = Counter()
            for (group, name), value in counter.items():
                if group in groups:
                    merged[name] += value
            return dict(sorted(merged.items()))

        return {
            "counts": merge(self.counts),
            "unreplayable_game_reasons": merge(self.unsupported),
            "unresolved_decision_reasons": merge(self.unresolved),
            "policy_seconds": {
                name: round(value, 3) for name, value in merge(self.seconds).items()
            },
        }


def _read_events(root: Path, game, group_name: str) -> tuple[list[dict], bytes]:
    try:
        payload = log_path(root, game.game_id).read_bytes()
        validate_mjai_log(payload, seats=(game.seat,))
        return list(parse_jsonl_gzip(payload)), payload
    except (CorpusError, OSError) as error:
        raise DiagnosticBlockedError(
            f"{group_name} MJAI log could not be read"
        ) from error


def run_impact_count(
    history_roots: Mapping[str, str | Path],
    *,
    arena_revision: str,
    lisjong_revision: str,
    limit_games: int | None = None,
) -> dict[str, object]:
    """固定した423席で、候補の対象局面と打牌変更を数える。"""
    arena_revision = _require_revision(arena_revision)
    lisjong_revision = _require_revision(lisjong_revision)
    if set(history_roots) != {group.name for group in CHAMPION_GROUPS}:
        raise DiagnosticInvalidError("history roots must name exactly dev and baseline")
    selections = {
        group.name: _select_games(Path(history_roots[group.name]), group)
        for group in CHAMPION_GROUPS
    }
    shared = set.intersection(
        *({game.game_id for game in games} for games in selections.values())
    )
    if len(shared) != SHARED_GAMES:
        raise DiagnosticInvalidError("shared games differ from the locked #441 count")

    counter = ImpactCounter()
    sources: dict[str, object] = {}
    for group in CHAMPION_GROUPS:
        root = Path(history_roots[group.name])
        digest = hashlib.sha256()
        games: Iterable = selections[group.name]
        if limit_games is not None:
            games = tuple(games)[:limit_games]
        for game in games:
            events, payload = _read_events(root, game, group.name)
            digest.update(f"{game.game_id}:{sha256_bytes(payload)}\n".encode())
            counter.analyze_game(
                group.name,
                game_id=game.game_id,
                seat=game.seat,
                bot_id=group.bot_id,
                events=events,
            )
        if limit_games is None:
            r1_rows = counter.counts[(group.name, "r1_rows")]
            if r1_rows != group.r1_discards:
                raise DiagnosticBlockedError(
                    f"{group.name} R1 rows {r1_rows} differ from the recorded "
                    f"Issue #483 result {group.r1_discards}"
                )
        sources[group.name] = {
            "bot_id": group.bot_id,
            "history_identity": group.history_identity,
            "games": group.games if limit_games is None else limit_games,
            "game_ids_sha256": group.game_ids_sha256,
            "game_logs_sha256": digest.hexdigest(),
        }

    names = tuple(group.name for group in CHAMPION_GROUPS)
    return {
        "schema_version": SCHEMA_VERSION,
        "outcome": OUTCOME_COMPLETE,
        "smoke": limit_games is not None,
        "source": {
            "played_at_threshold": PLAYED_AT_THRESHOLD,
            "shared_games": SHARED_GAMES,
            "groups": sources,
        },
        "runtime": {
            "arena_revision": arena_revision,
            "lisjong_revision": lisjong_revision,
            "rule_assumption": RIICHI_SAFE_TILE_RULE_ASSUMPTION,
        },
        "aggregate": {
            "per_bot": {name: counter.document((name,)) for name in names},
            "seat_total": counter.document(names),
        },
        "interpretation_boundary": [
            "Counts of the locked 423 seats in the Issue #489 input. These games were "
            "used to choose the target, so they are not evidence of strength.",
            "A target decision is defined only from the pre-discard input and the legal "
            "actions. It does not depend on the played discard or on whether the "
            "candidate's PolicyDecision.analysis is None.",
            "Changed counts compare the candidate's and the Champion's action on the "
            "same DecisionContext. Later decisions in a real game would differ once an "
            "earlier action changes, so the counts are not a frequency prediction.",
            "The seat total sums 423 seats over 357 unique games; the 66 shared games "
            "are not independent.",
            "policy_seconds is wall time of choose_action on this machine and only a "
            "rough scale for processing time.",
        ],
    }


__all__ = [
    "Applicability",
    "ImpactCounter",
    "SCHEMA_VERSION",
    "classify_applicability",
    "run_impact_count",
]
