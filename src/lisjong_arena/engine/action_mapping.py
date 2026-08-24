"""1 seat・1 decisionに閉じたengine descriptorとlisjong `InternalAction`の対応。

engineが1回のdecisionで提示した`tuple[ActionDescriptor, ...]`を、そのdecisionの
`SeatObservation`だけを文脈として`InternalAction`候補へ変換し、同時に逆方向の
decision-local mappingを構築する。

このmappingはprocess-global / match-global / Policy-globalにしない。
`build_action_mapping()`が返す値は1 decision分の不変valueであり、selectorは
呼び出しごとに新しく構築して破棄する。engine側`ActionProjection`は既に
physical duplicateをpublic descriptorへcollapse済みだが、Arena変換後に複数
descriptorが同じ`InternalAction` semantic identityへcollapseした場合は、
representativeを選ばず`AmbiguousActionMappingError`でfail closedする。
"""

from lisjong.policy_contract import (
    AnkanAction,
    ChiAction,
    DaiminkanAction,
    DiscardAction,
    InternalAction,
    KakanAction,
    KyuushuKyuuhaiAction,
    MeldKind,
    PassAction,
    PonAction,
    PublicMeld,
    RiichiAction,
    RonAction,
    Seat,
    Tile,
    TsumoAction,
)
from lisjong_engine.action_descriptor import (
    ACTION_DESCRIPTOR_TYPES,
    AnkanActionDescriptor,
    ChiActionDescriptor,
    DaiminkanActionDescriptor,
    DiscardActionDescriptor,
    KakanActionDescriptor,
    NineTerminalsActionDescriptor,
    PassActionDescriptor,
    PonActionDescriptor,
    RiichiActionDescriptor,
    RonActionDescriptor,
    TsumoActionDescriptor,
)
from lisjong_engine.observation import SeatObservation

from lisjong_arena.engine.domain_conversion import (
    public_meld_from_engine_meld,
    seat_from_engine_seat,
    tile_from_public_tile,
    tiles_from_public_tiles,
)
from lisjong_arena.engine.errors import (
    AmbiguousActionMappingError,
    EngineBridgeError,
    KakanProvenanceError,
    SeatIdentityError,
    UnmappedActionError,
    UnsupportedEngineValueError,
)


def _own_melds(observation: SeatObservation) -> tuple[PublicMeld, ...]:
    """viewer seat自身が現在保持する副露・槓snapshotをlisjong値で返す。"""
    for seat_melds in observation.melds:
        if seat_melds.seat is observation.viewer_seat:
            return tuple(
                public_meld_from_engine_meld(meld) for meld in seat_melds.melds
            )
    raise SeatIdentityError("observation melds do not contain the viewer seat")


def _resolve_kakan_source(
    added_tile: Tile,
    own_melds: tuple[PublicMeld, ...],
) -> tuple[Seat, Tile]:
    """加槓の元Ponをcurrent snapshotから一意に解決する。

    `KakanActionDescriptor`はadded tileだけを公開するため、`KakanAction`が
    要求する`from_seat` / `called_tile`は自席のexisting Ponから解決する。

    元Ponとの関連付けは**tile type**で行う。added tileが赤5で元Ponの
    called tileが通常5である場合も同じPonの加槓であり得るため、赤牌identity
    での照合はしない。一方、`KakanAction.called_tile`へは元Pon自身が保持する
    actual called tileをそのまま渡し、red/non-red semanticを維持する。

    source meld ID、physical tile ID、Python object identityは使用しない。
    一致が0件または2件以上の場合は推測せずfail closedする。
    """
    sources = tuple(
        meld
        for meld in own_melds
        if meld.kind is MeldKind.PON
        and meld.called_tile is not None
        and meld.called_tile.tile_type == added_tile.tile_type
    )
    if len(sources) != 1:
        raise KakanProvenanceError(
            "kakan source pon must resolve to exactly one existing pon; "
            f"found {len(sources)} candidates"
        )
    source = sources[0]
    if source.from_seat is None or source.called_tile is None:
        raise KakanProvenanceError("kakan source pon must retain its call provenance")
    return source.from_seat, source.called_tile


def _translate_discard(descriptor, actor, observation) -> InternalAction:
    return DiscardAction(
        actor=actor,
        tile=tile_from_public_tile(descriptor.tile),
        tsumogiri=descriptor.is_tsumogiri,
    )


def _translate_riichi(descriptor, actor, observation) -> InternalAction:
    return RiichiAction(actor=actor)


def _translate_chi(descriptor, actor, observation) -> InternalAction:
    return ChiAction(
        actor=actor,
        target=seat_from_engine_seat(descriptor.from_seat),
        called_tile=tile_from_public_tile(descriptor.tile),
        consumed_tiles=tiles_from_public_tiles(descriptor.consumed_tiles),
    )


def _translate_pon(descriptor, actor, observation) -> InternalAction:
    return PonAction(
        actor=actor,
        target=seat_from_engine_seat(descriptor.from_seat),
        called_tile=tile_from_public_tile(descriptor.tile),
        consumed_tiles=tiles_from_public_tiles(descriptor.consumed_tiles),
    )


def _translate_daiminkan(descriptor, actor, observation) -> InternalAction:
    return DaiminkanAction(
        actor=actor,
        target=seat_from_engine_seat(descriptor.from_seat),
        called_tile=tile_from_public_tile(descriptor.tile),
        consumed_tiles=tiles_from_public_tiles(descriptor.consumed_tiles),
    )


def _translate_ankan(descriptor, actor, observation) -> InternalAction:
    return AnkanAction(
        actor=actor,
        tiles=tiles_from_public_tiles(descriptor.tiles),
    )


def _translate_kakan(descriptor, actor, observation) -> InternalAction:
    added_tile = tile_from_public_tile(descriptor.tile)
    from_seat, called_tile = _resolve_kakan_source(added_tile, _own_melds(observation))
    return KakanAction(
        actor=actor,
        added_tile=added_tile,
        from_seat=from_seat,
        called_tile=called_tile,
    )


def _translate_ron(descriptor, actor, observation) -> InternalAction:
    return RonAction(
        actor=actor,
        target=seat_from_engine_seat(descriptor.from_seat),
        winning_tile=tile_from_public_tile(descriptor.tile),
    )


def _translate_tsumo(descriptor, actor, observation) -> InternalAction:
    return TsumoAction(
        actor=actor,
        winning_tile=tile_from_public_tile(descriptor.tile),
    )


def _translate_pass(descriptor, actor, observation) -> InternalAction:
    return PassAction(actor=actor)


def _translate_nine_terminals(descriptor, actor, observation) -> InternalAction:
    return KyuushuKyuuhaiAction(actor=actor)


_TRANSLATORS = {
    DiscardActionDescriptor: _translate_discard,
    RiichiActionDescriptor: _translate_riichi,
    ChiActionDescriptor: _translate_chi,
    PonActionDescriptor: _translate_pon,
    DaiminkanActionDescriptor: _translate_daiminkan,
    AnkanActionDescriptor: _translate_ankan,
    KakanActionDescriptor: _translate_kakan,
    RonActionDescriptor: _translate_ron,
    TsumoActionDescriptor: _translate_tsumo,
    PassActionDescriptor: _translate_pass,
    NineTerminalsActionDescriptor: _translate_nine_terminals,
}


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


def internal_action_from_descriptor(
    descriptor: object,
    actor: Seat,
    observation: SeatObservation,
) -> InternalAction:
    """1件のengine descriptorを、同じdecisionの文脈でlisjong `InternalAction`へ変換する。"""
    if not isinstance(descriptor, ACTION_DESCRIPTOR_TYPES):
        raise TypeError("descriptor must be a lisjong-engine ActionDescriptor")
    if not isinstance(actor, Seat):
        raise TypeError("actor must be a lisjong Seat")
    if not isinstance(observation, SeatObservation):
        raise TypeError("observation must be a lisjong-engine SeatObservation")

    translator = _TRANSLATORS.get(type(descriptor))
    if translator is None:
        raise UnsupportedEngineValueError(
            f"unsupported lisjong-engine ActionDescriptor: {type(descriptor).__name__}"
        )
    return translator(descriptor, actor, observation)


class EngineActionMapping:
    """1 seat・1 decisionに閉じた`InternalAction` <-> engine descriptorの対応。

    `candidates`はそのdecisionでPolicyへ提示する`InternalAction`列であり、
    engineが提示したoption orderをそのまま維持する。`resolve()`は
    `execute_policy()`が返したcanonical `InternalAction`を、**exactly one**の
    元descriptorへ戻す。このdecisionで提示されなかったActionはresolveしない。
    """

    __slots__ = ("_self_seat", "_descriptors_by_action", "_candidates")

    def __init__(
        self,
        *,
        self_seat: Seat,
        descriptors_by_action: dict[InternalAction, object],
    ) -> None:
        if not isinstance(self_seat, Seat):
            raise TypeError("self_seat must be a lisjong Seat")
        if not descriptors_by_action:
            raise EngineBridgeError("a decision must have at least one candidate")
        self._self_seat = self_seat
        self._descriptors_by_action = dict(descriptors_by_action)
        self._candidates = tuple(self._descriptors_by_action)

    @property
    def self_seat(self) -> Seat:
        return self._self_seat

    @property
    def candidates(self) -> tuple[InternalAction, ...]:
        return self._candidates

    def resolve(self, selected: InternalAction) -> object:
        """選択された`InternalAction`を、このdecisionの元descriptorへ戻す。"""
        if not isinstance(selected, _INTERNAL_ACTION_TYPES):
            raise TypeError("selected must be a lisjong InternalAction")
        if selected.actor != self._self_seat:
            raise SeatIdentityError(
                "selected InternalAction.actor does not match this decision's seat"
            )
        try:
            return self._descriptors_by_action[selected]
        except KeyError:
            raise UnmappedActionError(
                "selected InternalAction is not among this decision's candidates"
            ) from None


def build_action_mapping(
    observation: SeatObservation,
    options: object,
) -> EngineActionMapping:
    """1 decisionのdescriptor列から、decision-localな双方向mappingを構築する。"""
    if not isinstance(observation, SeatObservation):
        raise TypeError("observation must be a lisjong-engine SeatObservation")
    try:
        descriptors = tuple(options)
    except TypeError:
        raise TypeError("options must be an iterable of ActionDescriptor") from None
    if not descriptors:
        raise EngineBridgeError("a decision must offer at least one ActionDescriptor")

    actor = seat_from_engine_seat(observation.viewer_seat)

    descriptors_by_action: dict[InternalAction, object] = {}
    for descriptor in descriptors:
        internal_action = internal_action_from_descriptor(
            descriptor,
            actor,
            observation,
        )
        if internal_action in descriptors_by_action:
            raise AmbiguousActionMappingError(
                "multiple engine descriptors collapse onto the same InternalAction: "
                f"{internal_action!r}"
            )
        descriptors_by_action[internal_action] = descriptor

    return EngineActionMapping(
        self_seat=actor,
        descriptors_by_action=descriptors_by_action,
    )
