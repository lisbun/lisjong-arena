"""RiichiEnv legal ActionとInternalActionのdecision-local mapping。

lisbun/lisjong `docs/internal-action-model.md`、`docs/action-identity.md`が
定めるsemantic identity、semantic aggregation、deterministic representative、
decision-local mapping、revalidation/fail closedの原則を、RiichiEnv 0.4.8
向けに実装する。

対象は初期4人麻雀用11 `InternalAction` variantすべてである。`Action.to_mjai()`
だけを変換の正本にせず、lisbun/lisjong#27で確認したRiichiEnv `Action`公開属性
（`action_type`, `actor`, `tile`, `consume_tiles`）と、同decisionの
`Observation`（`last_discard`, `discards`, `melds`）を組み合わせて変換する。

このmoduleが持たない責務（`docs/architecture.md`「RiichiEnv Adapter」を参照）:

- `PolicyInput`やmaterialized stateの構築（lisbun/lisjong#28）
- `DecisionContext`の最終組み立て（`decision.py`、lisbun/lisjong#23）
- Policyの呼び出し
- 対局loop、`reset()` / `step()` / `done()`の管理

decision-local mappingの生成時に受け取った`Observation.legal_actions()`
以外の外部状態（`RiichiEnv`本体、`env.mjai_log`、他decisionの情報）は
参照しない。
"""

from dataclasses import dataclass

from lisjong.policy_contract.action import (
    AnkanAction,
    ChiAction,
    DaiminkanAction,
    DiscardAction,
    InternalAction,
    KakanAction,
    KyuushuKyuuhaiAction,
    PassAction,
    PonAction,
    RiichiAction,
    RonAction,
    TsumoAction,
)
from lisjong.policy_contract.seat import Seat
from riichienv import Action as RiichiEnvAction
from riichienv import ActionType, MeldType, Observation

from lisjong_arena.riichienv.adapter.seat_conversion import seat_from_player_index
from lisjong_arena.riichienv.adapter.tile_conversion import tile_from_physical_id

# InternalActionはtype文によるtype alias（union）であり、isinstance()の第2引数へ
# 直接使用できないため、11 variant classを明示的なtupleとして保持する
# （lisjong.policy_contract.decision_contextと同じ理由）。
_INTERNAL_ACTION_TYPES = (
    DiscardAction,
    RiichiAction,
    ChiAction,
    PonAction,
    DaiminkanAction,
    AnkanAction,
    KakanAction,
    RonAction,
    TsumoAction,
    PassAction,
    KyuushuKyuuhaiAction,
)


class ActionAdapterError(Exception):
    """RiichiEnv Action変換・decision-local mapping境界のfail closed例外の基底class。"""


class EmptyLegalActionsError(ActionAdapterError):
    """`Observation.legal_actions()`が空の場合。"""


class UnsupportedActionError(ActionAdapterError):
    """未対応、または現在の変換規則では変換できないRiichiEnv Actionを検出した場合。"""


class ActorMismatchError(ActionAdapterError):
    """Actionのactorがこのdecisionの観測playerまたはmappingのseatと一致しない場合。

    RiichiEnv Actionの`actor`が`Observation.player_id`と一致しない場合
    （変換時）と、`resolve()`へ渡された`InternalAction.actor`がmapping生成時の
    seatと一致しない場合（cross-seat利用時）の両方で送出する。
    """


class ContextResolutionError(ActionAdapterError):
    """Context整合条件（target、Kakan元Pon等）を一意に解決できない場合。

    通常discardおよびkakan chankanに対するChi/Pon/Daiminkan/Ronの`target`解決、
    Kakanの元Pon meld解決のいずれかがちょうど1件へ絞り込めない場合に送出する。
    """


class RepresentativeSelectionError(ActionAdapterError):
    """representativeを安全に決定・再検証できない場合。"""


class UnmappedActionError(ActionAdapterError):
    """Policyが選択した`InternalAction`が、このdecisionの候補集合に存在しない場合。"""


class StaleActionMappingError(ActionAdapterError):
    """失効済みのdecision-local mappingを再利用しようとした場合。

    RiichiEnv 0.4.8にはdecisionやeventを識別する公式IDが存在しない
    （lisbun/lisjong#27実測）。架空の外部IDを発明する代わりに、seat-localな
    `RiichiEnvActionMappingSession`がAdapter内部generationを管理する。次の
    mappingが生成された旧mappingと、すでにresolve済みのmappingは、どちらも
    fail closedする。
    """


def _resolve_call_target(
    observation: Observation, actor: Seat, physical_tile_id: int
) -> Seat:
    """Chi/Pon/Daiminkan/Ronの`target`を、同decisionのObservationから解決する。

    通常discardへの応答では`Observation.last_discard`が示すseatの直近discardが
    `physical_tile_id`と一致することを検証する。kakan chankanへの応答では、
    RiichiEnv 0.4.8がkakanをdiscard相当としてlast_discardへ転用する実装事実
    （lisbun/lisjong#27の`[AI-REVIEW]`対応実測、v0.4.8ソース確認済み）に基づき、
    `last_discard`が示すseatの現在のKAKAN meldに`physical_tile_id`が含まれる
    ことを検証する。どちらの経路でも一致しない場合はfail closedする。
    """
    target_index = observation.last_discard
    if target_index is None:
        raise ContextResolutionError(
            "observation.last_discard is None; cannot resolve call target"
        )

    target = seat_from_player_index(target_index)
    if target == actor:
        raise ContextResolutionError("last_discard seat must differ from actor")

    target_discards = observation.discards[target]
    if target_discards and target_discards[-1] == physical_tile_id:
        return target

    target_melds = observation.melds[target]
    if any(
        meld.meld_type == MeldType.Kakan and physical_tile_id in meld.tiles
        for meld in target_melds
    ):
        return target

    raise ContextResolutionError(
        "called/winning tile matches neither target's last discard "
        "nor an active kakan meld"
    )


def _translate_discard(
    action: RiichiEnvAction, observation: Observation, actor: Seat
) -> DiscardAction:
    tile = tile_from_physical_id(action.tile)
    tsumogiri = action.tile == observation.drawn_tile
    return DiscardAction(actor=actor, tile=tile, tsumogiri=tsumogiri)


def _translate_riichi(
    action: RiichiEnvAction, observation: Observation, actor: Seat
) -> RiichiAction:
    return RiichiAction(actor=actor)


def _translate_call(
    action: RiichiEnvAction,
    observation: Observation,
    actor: Seat,
    action_cls: type[ChiAction] | type[PonAction] | type[DaiminkanAction],
) -> ChiAction | PonAction | DaiminkanAction:
    target = _resolve_call_target(observation, actor, action.tile)
    called_tile = tile_from_physical_id(action.tile)
    consumed_tiles = tuple(tile_from_physical_id(t) for t in action.consume_tiles)
    return action_cls(
        actor=actor,
        target=target,
        called_tile=called_tile,
        consumed_tiles=consumed_tiles,
    )


def _translate_chi(
    action: RiichiEnvAction, observation: Observation, actor: Seat
) -> ChiAction:
    return _translate_call(action, observation, actor, ChiAction)


def _translate_pon(
    action: RiichiEnvAction, observation: Observation, actor: Seat
) -> PonAction:
    return _translate_call(action, observation, actor, PonAction)


def _translate_daiminkan(
    action: RiichiEnvAction, observation: Observation, actor: Seat
) -> DaiminkanAction:
    return _translate_call(action, observation, actor, DaiminkanAction)


def _translate_ankan(
    action: RiichiEnvAction, observation: Observation, actor: Seat
) -> AnkanAction:
    tiles = tuple(tile_from_physical_id(t) for t in action.consume_tiles)
    return AnkanAction(actor=actor, tiles=tiles)


def _translate_kakan(
    action: RiichiEnvAction, observation: Observation, actor: Seat
) -> KakanAction:
    added_tile = tile_from_physical_id(action.tile)
    consumed_ids = frozenset(action.consume_tiles)

    matches = [
        meld
        for meld in observation.melds[actor]
        if meld.meld_type == MeldType.Pon and frozenset(meld.tiles) == consumed_ids
    ]
    if len(matches) != 1:
        raise ContextResolutionError(
            "kakan source pon meld must resolve to exactly one match, "
            f"found {len(matches)}"
        )

    source_meld = matches[0]
    from_seat = seat_from_player_index(source_meld.from_who)
    called_tile = tile_from_physical_id(source_meld.called_tile)
    return KakanAction(
        actor=actor, added_tile=added_tile, from_seat=from_seat, called_tile=called_tile
    )


def _translate_ron(
    action: RiichiEnvAction, observation: Observation, actor: Seat
) -> RonAction:
    target = _resolve_call_target(observation, actor, action.tile)
    winning_tile = tile_from_physical_id(action.tile)
    return RonAction(actor=actor, target=target, winning_tile=winning_tile)


def _translate_tsumo(
    action: RiichiEnvAction, observation: Observation, actor: Seat
) -> TsumoAction:
    winning_tile = tile_from_physical_id(action.tile)
    return TsumoAction(actor=actor, winning_tile=winning_tile)


def _translate_pass(
    action: RiichiEnvAction, observation: Observation, actor: Seat
) -> PassAction:
    return PassAction(actor=actor)


def _translate_kyuushu_kyuuhai(
    action: RiichiEnvAction, observation: Observation, actor: Seat
) -> KyuushuKyuuhaiAction:
    return KyuushuKyuuhaiAction(actor=actor)


_TRANSLATORS = {
    ActionType.DISCARD: _translate_discard,
    ActionType.RIICHI: _translate_riichi,
    ActionType.CHI: _translate_chi,
    ActionType.PON: _translate_pon,
    ActionType.DAIMINKAN: _translate_daiminkan,
    ActionType.ANKAN: _translate_ankan,
    ActionType.KAKAN: _translate_kakan,
    ActionType.RON: _translate_ron,
    ActionType.TSUMO: _translate_tsumo,
    ActionType.PASS: _translate_pass,
    ActionType.KYUSHU_KYUHAI: _translate_kyuushu_kyuuhai,
}


def _representative_key(action: RiichiEnvAction) -> tuple[int, tuple[int, ...]]:
    """semantic group内でrepresentativeを選ぶための、RiichiEnv固有total key。

    同一semantic group内の外部候補は、変換元のactionが同じvariant・actorである
    ことがgroup化条件そのものであるため、`Action`の残り公開field
    （`tile`、`consume_tiles`）だけで完全に区別できる。これらはいずれも
    RiichiEnvのphysical tile ID（int）であり、常に比較可能な全順序を持つ。
    list index、列挙順、Python object identity、hash iteration、乱数には
    一切依存しない。
    """
    tile_key = action.tile if action.tile is not None else -1
    consume_key = tuple(sorted(action.consume_tiles))
    return (tile_key, consume_key)


@dataclass(frozen=True, slots=True)
class _SemanticGroup:
    """同一semantic identityへ正規化される外部候補群と、その代表。"""

    internal_action: InternalAction
    external_candidates: tuple[RiichiEnvAction, ...]
    representative: RiichiEnvAction


class RiichiEnvActionMappingSession:
    """1 seat分のdecision-local mapping所有境界。

    RiichiEnvには公式decision IDがないため、外部状態からIDを推測しない。
    同じseatの新しいObservationからmappingを構築するたびにAdapter内部の
    generationを進め、以前に返したmappingを失効させる。保持する可変状態は
    generationだけであり、PolicyInput、materialized state、RiichiEnv本体や
    対局loopは所有しない。
    """

    __slots__ = ("_self_seat", "_generation")

    def __init__(self, self_seat: Seat) -> None:
        if not isinstance(self_seat, Seat):
            raise TypeError("self_seat must be a Seat")
        self._self_seat = self_seat
        self._generation = 0

    @property
    def self_seat(self) -> Seat:
        return self._self_seat

    def _is_current(self, generation: int) -> bool:
        return generation == self._generation

    def build(self, observation: Observation) -> "RiichiEnvActionMapping":
        """current decisionのmappingを生成し、以前のmappingを失効させる。"""
        observation_seat = seat_from_player_index(observation.player_id)
        if observation_seat != self._self_seat:
            raise ActorMismatchError(
                "observation.player_id does not match this mapping session's seat"
            )

        # 新decisionの構築が途中でfail closedしても、旧decisionのmappingを
        # 再利用可能なまま残さない。seat整合を確認後、変換開始前に進める。
        self._generation += 1
        return _build_action_mapping(
            observation=observation,
            session=self,
            generation=self._generation,
        )


class RiichiEnvActionMapping:
    """1 seat・1 decisionに閉じた、InternalAction候補と元RiichiEnv Actionの対応。

    `RiichiEnvActionMappingSession.build()`が生成する。`candidates`はPolicyへ提示してよい、
    semantic identity上重複のない`InternalAction`列である。`resolve()`は、
    Policyが選択した`InternalAction`を元のRiichiEnv legal Actionへ1回だけ
    戻せる。RiichiEnv側にはdecisionを識別する公式IDが存在しないため、この
    seat-local sessionのgenerationにより1 decisionだけ有効となる。次のmapping
    生成後、2回目の`resolve()`、別seatのActionでの呼び出しはfail closedする。
    """

    __slots__ = (
        "_session",
        "_generation",
        "_self_seat",
        "_groups",
        "_candidates",
        "_external_legal_actions",
        "_resolved",
    )

    def __init__(
        self,
        session: RiichiEnvActionMappingSession,
        generation: int,
        self_seat: Seat,
        groups: dict[InternalAction, _SemanticGroup],
        external_legal_actions: tuple[RiichiEnvAction, ...],
    ) -> None:
        self._session = session
        self._generation = generation
        self._self_seat = self_seat
        self._groups = groups
        self._candidates = tuple(groups.keys())
        self._external_legal_actions = external_legal_actions
        self._resolved = False

    @property
    def self_seat(self) -> Seat:
        return self._self_seat

    @property
    def candidates(self) -> tuple[InternalAction, ...]:
        """このdecisionでPolicyへ提示する、semantic重複のないInternalAction候補。"""
        return self._candidates

    def resolve(self, selected: InternalAction) -> RiichiEnvAction:
        """Policyが選択した`InternalAction`を、元のRiichiEnv legal Actionへ戻す。

        次を順に確認し、いずれかを満たさない場合は未検証Actionを返さず例外を
        送出する。

        1. `selected`が有効な`InternalAction`である
        2. session上でこのmappingのgenerationがcurrentである
           （未resolve mappingを含むstale / cross-decision防止）
        3. このmappingがまだresolveされていない（二重resolve防止）
        4. `selected.actor`がこのmappingのseatと一致する（cross-seat防止）
        5. `selected`がこのdecisionの候補へsemantic identity上一致する
        6. 対応するrepresentativeが、生成時のexternal legal action集合に
           実在する
        """
        if not isinstance(selected, _INTERNAL_ACTION_TYPES):
            raise TypeError("selected must be an InternalAction")

        if not self._session._is_current(self._generation):
            raise StaleActionMappingError(
                "a newer decision mapping has invalidated this mapping"
            )

        if self._resolved:
            raise StaleActionMappingError(
                "this decision-local mapping has already been resolved once"
            )

        if selected.actor != self._self_seat:
            raise ActorMismatchError(
                "selected InternalAction.actor does not match this mapping's seat"
            )

        group = self._groups.get(selected)
        if group is None:
            raise UnmappedActionError(
                "selected InternalAction is not among this decision's candidates"
            )

        representative = group.representative
        if representative not in self._external_legal_actions:
            raise RepresentativeSelectionError(
                "representative action is not present in the external legal "
                "action set captured at mapping creation time"
            )

        self._resolved = True
        return representative


def _build_action_mapping(
    observation: Observation,
    session: RiichiEnvActionMappingSession,
    generation: int,
) -> RiichiEnvActionMapping:
    """同decisionのRiichiEnv `Observation`から、decision-local Action mappingを構築する。

    `observation.legal_actions()`が返す外部Action群を、同decisionのseat-visible
    context（`last_discard`、`discards`、`melds`）を使ってsemantic変換し、
    physical copy identityだけが異なる候補を集約する。集約後の代表選択は
    RiichiEnv固有のphysical fieldに基づくdeterministic total keyで行う。
    """
    legal_actions = tuple(observation.legal_actions())
    if not legal_actions:
        raise EmptyLegalActionsError("observation.legal_actions() is empty")

    self_seat = session.self_seat

    candidates_by_action: dict[InternalAction, list[RiichiEnvAction]] = {}
    for external_action in legal_actions:
        if external_action.actor != observation.player_id:
            raise ActorMismatchError(
                "external action actor does not match observation.player_id"
            )

        translator = _TRANSLATORS.get(external_action.action_type)
        if translator is None:
            raise UnsupportedActionError(
                f"unsupported RiichiEnv action_type: {external_action.action_type!r}"
            )

        internal_action = translator(external_action, observation, self_seat)
        candidates_by_action.setdefault(internal_action, []).append(external_action)

    groups: dict[InternalAction, _SemanticGroup] = {}
    for internal_action, external_candidates in candidates_by_action.items():
        representative = min(external_candidates, key=_representative_key)
        groups[internal_action] = _SemanticGroup(
            internal_action=internal_action,
            external_candidates=tuple(external_candidates),
            representative=representative,
        )

    return RiichiEnvActionMapping(
        session=session,
        generation=generation,
        self_seat=self_seat,
        groups=groups,
        external_legal_actions=legal_actions,
    )
