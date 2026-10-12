"""Issue #483 Champion side: classify the Champion's own R1 discards and losses.

Issue #441の既存対象（lisjong-dev 209対局、lisjong-baseline 214対局）を、強豪bot側と
同じ分類codeで読む。件数・放銃支出は実戦で選ばれた打牌から集計する。固定した
ChampionはR1打牌選択行の再現・分岐確認のためにだけ実行し（Issue #489）、
代替打牌の結果は評価しない。
"""

import hashlib
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from lisjong.policy_contract import Policy
from lisjong.policy_contract.action import DiscardAction
from lisjong.policy_contract.riichi import RiichiState
from lisjong.policy_contract.seat import Seat

from lisjong_arena.riichilab_corpus.models import CorpusError, sha256_bytes
from lisjong_arena.riichilab_corpus.validation import (
    parse_jsonl_gzip,
    validate_mjai_log,
)
from lisjong_arena.riichilab_deal_in_risk_disagreement.analysis import (
    _ACTION_EVENT_TYPES,
    _BAKAZE_BY_WIND,
    LISJONG_REVISION,
    REGION_R1,
    SHANTEN_BACKEND,
    DiscardKey,
    _create_locked_comparator,
    _require_locked_runtime,
    _RiichiDeclarationTracker,
    classify_region,
)
from lisjong_arena.riichilab_disagreement_diagnostic.analysis import (
    DiagnosticBlockedError,
    DiagnosticInvalidError,
    _require_revision,
)
from lisjong_arena.riichilab_self_history.errors import SelfHistoryError
from lisjong_arena.riichilab_self_history.mjai import log_path
from lisjong_arena.riichilab_self_history.models import SelfHistoryGame
from lisjong_arena.riichilab_self_history.persistence import load_published_history
from lisjong_arena.riichilab_source_pilot.dataset import SOURCE_GAME_MODE
from lisjong_arena.riichilab_source_pilot.materialization import (
    MaterializationDecisionObservation,
    materialize_game,
)

from .champion_view import observe_champion_view
from .classification import (
    GENBUTSU_AVAILABLE,
    GENBUTSU_NONE,
    SHANTEN_KEEP,
    SHANTEN_WORSE,
    UNAVAILABLE,
    classify_r1_choice,
)

SCHEMA_VERSION = "arena-riichilab-r1-choice-loss-champion-play-v3"
OUTCOME_COMPLETE = "R1 CHOICE DIAGNOSTIC (CHAMPION PLAY) COMPLETE"

# #441のChampion（lisjong 51e832e）対象期間。played_atはUTCとして読む（#441の推定を継承）。
PLAYED_AT_THRESHOLD = "2026-09-30T15:00:50"
SHARED_GAMES = 66


@dataclass(frozen=True, slots=True)
class ChampionGroup:
    """#441で固定済みの1 bot分の対象。"""

    name: str
    bot_id: int
    history_identity: str
    games: int
    game_ids_sha256: str
    rounds: int
    deal_in_rounds: int
    # Issue #483に記録したR1の打牌数・ロンされた打牌数・放銃支出。
    r1_discards: int
    r1_ronned_discards: int
    r1_deal_in_loss: int
    # Issue #487に記録した、現物あり・非現物選択・最小向聴数1以上の打牌数。
    target_discards: int


CHAMPION_GROUPS = (
    ChampionGroup(
        name="dev",
        bot_id=313,
        history_identity=(
            "49575871d0a078aecf4c8e1971af1e409ab1c9ca8e52795dc4f431c355f25056"
        ),
        games=209,
        game_ids_sha256=(
            "99c955bd5e467d7d0fdb013308473d92be999055ddd31a15b2ae2933387c28d0"
        ),
        rounds=2161,
        deal_in_rounds=307,
        r1_discards=3724,
        r1_ronned_discards=82,
        r1_deal_in_loss=496200,
        target_discards=330,
    ),
    ChampionGroup(
        name="baseline",
        bot_id=394,
        history_identity=(
            "72e4cab87684e84c73c99001399c4136c67bd7d8cf1233f6814dd51d2e49c095"
        ),
        games=214,
        game_ids_sha256=(
            "bd4f36e4916fef33c081858296fc9d79b87a254da36f5ce1e7bb29fec24099e2"
        ),
        rounds=2117,
        deal_in_rounds=251,
        r1_discards=4154,
        r1_ronned_discards=63,
        r1_deal_in_loss=373100,
        target_discards=390,
    ),
)

_STATES = (SHANTEN_KEEP, SHANTEN_WORSE, UNAVAILABLE)

# Issue #487: ロンした席が、打牌時点のリーチ成立者かそれ以外か。
WINNER_RIICHI_ONLY = "riichi_player_only"
WINNER_OTHER_ONLY = "other_only"
WINNER_BOTH = "both"
_WINNERS = (WINNER_RIICHI_ONLY, WINNER_OTHER_ONLY, WINNER_BOTH)
PAYEE_RIICHI = "riichi_player"

# Issue #489: 合法打牌候補の現物の出所。
SOURCE_RIVER = "river"
SOURCE_PASSED_ONLY = "passed_only"
SOURCE_AXIS = (
    "genbutsu_availability|selected_genbutsu|minimum_post_discard_shanten"
    "|genbutsu_source"
)
PATH_AXIS = f"{SOURCE_AXIS}|champion_view"
_TARGET_PREFIXES = (
    f"{GENBUTSU_AVAILABLE}|not_genbutsu|1|",
    f"{GENBUTSU_AVAILABLE}|not_genbutsu|2+|",
)
PAYEE_OTHER = "other"
_PAYEES = (PAYEE_RIICHI, PAYEE_OTHER)


@dataclass(frozen=True, slots=True)
class RonnedDiscard:
    """公開hora eventから求めた、1打牌へのロン。"""

    loss: int
    r1_by_events: bool
    # (ロンした席, その席への支払い)。複数ロンは`hora`ごとに1件。
    loss_by_winner: tuple[tuple[int, int], ...] = ()


@dataclass(frozen=True, slots=True)
class SeatEventSummary:
    rounds: int
    deal_in_rounds: int
    ronned: dict[DiscardKey, RonnedDiscard]


def seat_event_summary(
    events: Iterable[Mapping[str, object]], *, game_id: str, seat: int
) -> SeatEventSummary:
    """公開MJAI eventだけから、1席の局数・放銃局数・ロンされた打牌を求める。

    放銃支出は、その打牌への全`hora`の`deltas[seat]`の符号を反転した合計である
    （複数ロンは1打牌として合算、本場を含む）。槍槓は放銃局数にだけ数える。
    """
    rounds = 0
    deal_in_rounds = 0
    ronned: dict[DiscardKey, RonnedDiscard] = {}
    kyoku: tuple[str, int, int] | None = None
    discards = 0
    accepted = [False] * 4
    declaring = [False] * 4
    last_action: tuple[object, object] | None = None
    last_discard_r1 = False
    dealt_in = False
    for event in events:
        event_type = event.get("type")
        if event_type == "start_kyoku":
            kyoku = (str(event["bakaze"]), int(event["kyoku"]), int(event["honba"]))
            rounds += 1
            discards = 0
            accepted = [False] * 4
            declaring = [False] * 4
            last_action = None
            dealt_in = False
        elif event_type == "reach":
            declaring[int(event["actor"])] = True
        elif event_type == "reach_accepted":
            actor = int(event["actor"])
            accepted[actor] = True
            declaring[actor] = False
        elif event_type == "hora":
            if event.get("target") != seat or event.get("actor") == seat:
                continue
            if kyoku is None:
                raise DiagnosticBlockedError("hora event precedes start_kyoku")
            if not dealt_in:
                dealt_in = True
                deal_in_rounds += 1
            if last_action != ("dahai", seat):
                continue
            key = (game_id, seat, *kyoku, discards)
            previous = ronned.get(key)
            payment = -int(event["deltas"][seat])
            ronned[key] = RonnedDiscard(
                loss=(0 if previous is None else previous.loss) + payment,
                r1_by_events=last_discard_r1,
                loss_by_winner=(
                    *(() if previous is None else previous.loss_by_winner),
                    (int(event["actor"]), payment),
                ),
            )
        elif event_type in _ACTION_EVENT_TYPES:
            actor = event.get("actor")
            if event_type == "dahai" and actor == seat:
                discards += 1
                others = sum(accepted[index] for index in range(4) if index != seat)
                last_discard_r1 = (
                    others == 1 and not accepted[seat] and not declaring[seat]
                )
            last_action = (event_type, actor)
    return SeatEventSummary(rounds, deal_in_rounds, ronned)


class ChampionPlayAnalyzer:
    """Aggregate over the Champion's own R1 discard choices, per bot."""

    def __init__(self, *, policy_factory: Callable[[], Policy] | None = None) -> None:
        # Issue #489: 指定したときだけ、固定したChampionの分類をR1の行へ付ける。
        self._policy_factory = policy_factory
        # (group, 件数だけ報告する項目)
        self._counts: Counter[tuple[str, str]] = Counter()
        self._unsupported: Counter[tuple[str, str]] = Counter()
        self._unresolved: Counter[tuple[str, str]] = Counter()
        # (group, axis, value, 維持 / 悪化)
        self._discards: Counter[tuple[str, str, str, str]] = Counter()
        self._ronned: Counter[tuple[str, str, str, str]] = Counter()
        self._loss: Counter[tuple[str, str, str, str]] = Counter()
        # (group, axis, value, ロンした席の区分 / 支払先)
        self._winners: Counter[tuple[str, str, str, str]] = Counter()
        self._payee_loss: Counter[tuple[str, str, str, str]] = Counter()

    def analyze_game(
        self,
        group: str,
        *,
        game_id: str,
        seat: int,
        bot_id: int,
        events: list[dict],
    ) -> None:
        summary = seat_event_summary(events, game_id=game_id, seat=seat)
        self._counts[(group, "games")] += 1
        self._counts[(group, "rounds")] += summary.rounds
        self._counts[(group, "deal_in_rounds")] += summary.deal_in_rounds
        ronned_loss = sum(item.loss for item in summary.ronned.values())
        r1_by_events = [item for item in summary.ronned.values() if item.r1_by_events]
        self._counts[(group, "ronned_discards")] += len(summary.ronned)
        self._counts[(group, "ronned_discard_loss")] += ronned_loss
        self._counts[(group, "r1_ronned_discards_by_events")] += len(r1_by_events)
        self._counts[(group, "r1_ronned_discard_loss_by_events")] += sum(
            item.loss for item in r1_by_events
        )

        tracker = _RiichiDeclarationTracker()
        # Championのinstanceは対局 x 席ごとに作る。
        policy = None if self._policy_factory is None else self._policy_factory()
        counts: Counter[str] = Counter()
        rows: dict[DiscardKey, tuple[int, tuple[tuple[str, str, str], ...]]] = {}

        def observe(observation: MaterializationDecisionObservation) -> None:
            policy_input = observation.decision.input
            tracker.update(policy_input)
            action = observation.teacher_action
            if observation.legal_action_count < 2:
                counts["forced_decisions"] += 1
                return
            if not isinstance(action, DiscardAction):
                counts["non_discard_choice_decisions"] += 1
                return
            counts["discard_choice_rows"] += 1
            if policy_input.players[seat].riichi is not RiichiState.NONE:
                counts["own_riichi_discard_choice_rows"] += 1
                return
            if classify_region(policy_input) != REGION_R1:
                counts["other_region_discard_choice_rows"] += 1
                return
            choice = classify_r1_choice(observation, tracker)
            state = choice.shanten_state(action)
            selected = choice.selected_genbutsu(action)
            round_state = policy_input.round
            key = (
                game_id,
                seat,
                _BAKAZE_BY_WIND[round_state.round_wind],
                round_state.hand_number,
                round_state.honba,
                len(policy_input.players[seat].discards) + 1,
            )
            if key in rows:
                raise DiagnosticBlockedError(
                    "replay exposed two discard choices at one discard position"
                )
            availability_selected = f"{choice.genbutsu_availability}|{selected}"
            minimum = dict(choice.axes())["minimum_post_discard_shanten"]
            riichi_seat = next(
                index
                for index, player in enumerate(policy_input.players)
                if index != seat and player.riichi is RiichiState.ACCEPTED
            )
            river = frozenset(
                discard.tile.tile_type
                for discard in policy_input.players[riichi_seat].discards
            )
            if any(
                isinstance(legal, DiscardAction) and legal.tile.tile_type in river
                for legal in observation.decision.legal_actions
            ):
                source = SOURCE_RIVER
            elif choice.genbutsu_availability == GENBUTSU_AVAILABLE:
                source = SOURCE_PASSED_ONLY
            elif choice.genbutsu_availability == GENBUTSU_NONE:
                source = GENBUTSU_NONE
            else:
                source = UNAVAILABLE
            source_value = f"{availability_selected}|{minimum}|{source}"
            extra = [
                ("genbutsu_availability|selected_genbutsu", availability_selected),
                (
                    "genbutsu_availability|selected_genbutsu"
                    "|minimum_post_discard_shanten",
                    f"{availability_selected}|{minimum}",
                ),
                (SOURCE_AXIS, source_value),
            ]
            if policy is not None:
                view = observe_champion_view(policy, observation)
                extra.append((PATH_AXIS, f"{source_value}|{view.label()}"))
            rows[key] = (
                riichi_seat,
                tuple((axis, value, state) for axis, value in (*choice.axes(), *extra)),
            )

        result = materialize_game(
            events,
            game_id=game_id,
            target_seats={Seat(seat): bot_id},
            game_mode=SOURCE_GAME_MODE,
            decision_observer=observe,
        )
        if not result.supported:
            # 再現できない対局は途中までの行を捨て、別計上する。
            self._counts[(group, "unreplayable_games")] += 1
            self._unsupported[(group, result.unsupported_reason.value)] += 1
            self._counts[(group, "unreplayable_r1_ronned_discards_by_events")] += len(
                r1_by_events
            )
            self._counts[(group, "unreplayable_r1_ronned_loss_by_events")] += sum(
                item.loss for item in r1_by_events
            )
            return

        self._counts[(group, "replayed_games")] += 1
        for reason, count in result.unresolved_reasons:
            self._counts[(group, "unresolved_decisions")] += count
            self._unresolved[(group, reason)] += count
        for name, count in counts.items():
            self._counts[(group, name)] += count
        for key, (riichi_seat, cells) in rows.items():
            hit = summary.ronned.get(key)
            if hit is not None:
                to_riichi = sum(
                    loss for winner, loss in hit.loss_by_winner if winner == riichi_seat
                )
                winners = {
                    winner == riichi_seat for winner, _loss in hit.loss_by_winner
                }
                if winners == {True}:
                    winner_key = WINNER_RIICHI_ONLY
                elif winners == {False}:
                    winner_key = WINNER_OTHER_ONLY
                else:
                    winner_key = WINNER_BOTH
            for axis, value, state in cells:
                self._discards[(group, axis, value, state)] += 1
                if hit is not None:
                    self._ronned[(group, axis, value, state)] += 1
                    self._loss[(group, axis, value, state)] += hit.loss
                    self._winners[(group, axis, value, winner_key)] += 1
                    self._payee_loss[(group, axis, value, PAYEE_RIICHI)] += to_riichi
                    self._payee_loss[(group, axis, value, PAYEE_OTHER)] += (
                        hit.loss - to_riichi
                    )

    def _group_document(self, groups: tuple[str, ...]) -> dict[str, object]:
        def collect(counter: Counter[tuple[str, str]]) -> dict[str, int]:
            merged: Counter[str] = Counter()
            for (group, name), count in counter.items():
                if group in groups:
                    merged[name] += count
            return dict(sorted(merged.items()))

        r1_loss = sum(
            count
            for (group, axis, _value, _state), count in self._loss.items()
            if group in groups and axis == "all"
        )
        axes: dict[str, dict[str, dict[str, object]]] = {}
        for key in sorted(self._discards):
            group, axis, value, state = key
            if group not in groups:
                continue
            cell = axes.setdefault(axis, {}).setdefault(
                value,
                {
                    "discards": dict.fromkeys(("total", *_STATES), 0),
                    "ronned_discards": dict.fromkeys(("total", *_STATES), 0),
                    "deal_in_loss": dict.fromkeys(("total", *_STATES), 0),
                },
            )
            for name, counter in (
                ("discards", self._discards),
                ("ronned_discards", self._ronned),
                ("deal_in_loss", self._loss),
            ):
                cell[name]["total"] += counter[key]
                cell[name][state] += counter[key]
        for axis, values in axes.items():
            for value, cell in values.items():
                cell["deal_in_loss_share_of_r1"] = (
                    None if r1_loss == 0 else cell["deal_in_loss"]["total"] / r1_loss
                )
                cell["ronned_discards_by_winner"] = {
                    name: sum(
                        self._winners[(group, axis, value, name)] for group in groups
                    )
                    for name in _WINNERS
                }
                cell["deal_in_loss_by_payee"] = {
                    name: sum(
                        self._payee_loss[(group, axis, value, name)] for group in groups
                    )
                    for name in _PAYEES
                }
        return {
            "counts_only": collect(self._counts),
            "unreplayable_game_reasons": collect(self._unsupported),
            "unresolved_decision_reasons": collect(self._unresolved),
            "r1_deal_in_loss_total": r1_loss,
            "axes": axes,
        }

    def group_count(self, group: str, name: str) -> int:
        return self._counts[(group, name)]

    def target_discards(self, group: str) -> int:
        """現物あり・非現物選択・最小向聴数1以上の打牌数（Issue #487の対象）。"""
        return sum(
            count
            for (name, axis, value, _state), count in self._discards.items()
            if name == group
            and axis == SOURCE_AXIS
            and value.startswith(_TARGET_PREFIXES)
        )

    def r1_totals(self, group: str) -> tuple[int, int, int]:
        """(R1の打牌数, ロンされた打牌数, 放銃支出)。"""
        return tuple(
            sum(
                count
                for (name, axis, _value, _state), count in counter.items()
                if name == group and axis == "all"
            )
            for counter in (self._discards, self._ronned, self._loss)
        )

    def aggregate_document(self) -> dict[str, object]:
        names = tuple(group.name for group in CHAMPION_GROUPS)
        return {
            "per_bot": {name: self._group_document((name,)) for name in names},
            "seat_total": self._group_document(names),
        }


def _select_games(root: Path, group: ChampionGroup) -> tuple[SelfHistoryGame, ...]:
    try:
        history = load_published_history(root)
    except SelfHistoryError as error:
        raise DiagnosticInvalidError(
            f"{group.name} self-history could not be read"
        ) from error
    if history.bot_id != group.bot_id or history.identity != group.history_identity:
        raise DiagnosticInvalidError(
            f"{group.name} self-history is not the locked #441 history"
        )
    selected = tuple(
        game for game in history.games if game.played_at >= PLAYED_AT_THRESHOLD
    )
    digest = hashlib.sha256(
        "\n".join(sorted(game.game_id for game in selected)).encode()
    ).hexdigest()
    if len(selected) != group.games or digest != group.game_ids_sha256:
        raise DiagnosticInvalidError(
            f"{group.name} selection is not the locked #441 Champion games"
        )
    return selected


def run_champion_play(
    history_roots: Mapping[str, str | Path], *, arena_revision: str
) -> dict[str, object]:
    """Classify the Champion's own R1 discards in the locked #441 games."""
    arena_revision = _require_revision(arena_revision)
    runtime_riichienv = _require_locked_runtime()
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

    analyzer = ChampionPlayAnalyzer(policy_factory=_create_locked_comparator)
    sources: dict[str, object] = {}
    for group in CHAMPION_GROUPS:
        root = Path(history_roots[group.name])
        digest = hashlib.sha256()
        for game in selections[group.name]:
            try:
                payload = log_path(root, game.game_id).read_bytes()
                validate_mjai_log(payload, seats=(game.seat,))
                events = list(parse_jsonl_gzip(payload))
            except (CorpusError, OSError) as error:
                raise DiagnosticBlockedError(
                    f"{group.name} MJAI log could not be read"
                ) from error
            digest.update(f"{game.game_id}:{sha256_bytes(payload)}\n".encode())
            analyzer.analyze_game(
                group.name,
                game_id=game.game_id,
                seat=game.seat,
                bot_id=group.bot_id,
                events=events,
            )
        if (
            analyzer.group_count(group.name, "rounds") != group.rounds
            or analyzer.group_count(group.name, "deal_in_rounds")
            != group.deal_in_rounds
        ):
            raise DiagnosticBlockedError(
                f"{group.name} rounds or deal-in rounds differ from the #441 record"
            )
        if analyzer.r1_totals(group.name) != (
            group.r1_discards,
            group.r1_ronned_discards,
            group.r1_deal_in_loss,
        ):
            raise DiagnosticBlockedError(
                f"{group.name} R1 totals differ from the recorded Issue #483 result"
            )
        if analyzer.target_discards(group.name) != group.target_discards:
            raise DiagnosticBlockedError(
                f"{group.name} target discards differ from the recorded Issue #487 "
                "result"
            )
        sources[group.name] = {
            "bot_id": group.bot_id,
            "history_identity": group.history_identity,
            "games": group.games,
            "game_ids_sha256": group.game_ids_sha256,
            "game_logs_sha256": digest.hexdigest(),
        }

    return {
        "schema_version": SCHEMA_VERSION,
        "outcome": OUTCOME_COMPLETE,
        "source": {
            "played_at_threshold": PLAYED_AT_THRESHOLD,
            "shared_games": SHARED_GAMES,
            "unique_games": sum(group.games for group in CHAMPION_GROUPS)
            - SHARED_GAMES,
            "groups": sources,
        },
        "classification_runtime": {
            "arena_revision": arena_revision,
            "lisjong_revision": LISJONG_REVISION,
            "shanten_backend": SHANTEN_BACKEND,
            "riichienv_version": runtime_riichienv,
        },
        "aggregate": analyzer.aggregate_document(),
        "interpretation_boundary": [
            "Exploratory follow-up chosen after seeing the Issue #455 result; "
            "not an independent verification, strength evaluation, or causal "
            "estimate.",
            "Counts and deal-in payments are aggregated from the discards "
            "actually played. The locked Champion is executed on R1 discard "
            "choice rows only to replay them and observe its branch; no "
            "alternative discard is evaluated.",
            "The ronned share per discard is not a per-round deal-in rate.",
            "The champion branch is the value lisjong returns with its decision; "
            "rows where the replayed action differs from the played discard do "
            "not explain the played decision, and a matching action does not "
            "prove that the internal path in live play was identical.",
            "A ron by a seat other than the riichi player is not shown to be "
            "avoidable by a genbutsu discard; only deal-in payments are counted, "
            "not the winnings gained by pushing.",
            "The seat total sums 423 seats over 357 unique games; the 66 shared "
            "games are not independent games.",
            "The strong-bot table and this table differ in reached positions, "
            "period, and opponents; rates must not be subtracted or pooled.",
        ],
    }


__all__ = [
    "CHAMPION_GROUPS",
    "ChampionGroup",
    "ChampionPlayAnalyzer",
    "OUTCOME_COMPLETE",
    "PATH_AXIS",
    "PAYEE_OTHER",
    "PAYEE_RIICHI",
    "PLAYED_AT_THRESHOLD",
    "RonnedDiscard",
    "SCHEMA_VERSION",
    "SOURCE_AXIS",
    "SOURCE_PASSED_ONLY",
    "SOURCE_RIVER",
    "SeatEventSummary",
    "WINNER_BOTH",
    "WINNER_OTHER_ONLY",
    "WINNER_RIICHI_ONLY",
    "seat_event_summary",
    "run_champion_play",
]
