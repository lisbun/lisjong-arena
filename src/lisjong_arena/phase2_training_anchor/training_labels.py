"""Phase 2のtraining-only omniscient label path。

このmoduleだけが、anchor時点のprivileged `RoundState`を読む。player-safe
anchor pathとは別pathであり、player-safe value側へlabel、label availability、
unsupported reason、hidden truthを一切書き戻さない。

```text
same anchor omniscient truth
    -> exact expected-count (3 opponents x 34 base kinds)
    -> exact red-five presence (3 suited categories per opponent)
    -> per-opponent exact structural wait mask (stable 13-equivalent only)
```

## Target availability is per target, not per anchor

anchor eligibility、expected-count validity、structural-wait availabilityは
別々のcontractである。あるopponentのstructural waitがunavailableでも、
expected-count targetやanchor全体をdropしない。

known unsupported stateだけをtarget-specific reason codeで表す。engine invariant
violationやexact builderのunexpected failureはordinary `unavailable`へ丸めず、
そのままfail closedする。

## Ron-legal auxiliary is explicitly deferred

Phase 2では`ron_legal_wait`を実装しない。engineの`can_declare_ron()`は
winning tile、source seat、`WinOrigin`、furiten（temporary / riichi furitenは
missed-ron historyに依存する）、yaku、last-tile timing、effective `RuleSet`を
必要とする。TURN / pre-action anchorには、これらを与えるcanonicalな仮想ron
trigger contextが存在しない。

したがってcontext-freeな34-vectorを新規に発明せず、必要になった時点で
public response trigger + source seat + `WinOrigin` + rule / furiten / timing
contextへ条件付けたtraining-only targetとして別途設計する。ron-legal
auxiliaryはcanonical `HandBelief.wait_probability`を置換しない。
"""

from dataclasses import dataclass
from enum import Enum

from lisjong.belief import (
    SCALE,
    TILE_TYPE_COUNT,
    exact_hand_belief_with_waits,
    tile_type_index,
    wind_for_seat,
)
from lisjong.belief.self_belief import concealed_hand_marginals
from lisjong.policy_contract import Seat, TileCategory, Wind
from lisjong_engine.public_state import public_meld, public_tile
from lisjong_engine.round_state import RoundState
from lisjong_engine.seat import Seat as EngineSeat

from lisjong_arena.lisjong_engine.domain_conversion import (
    public_meld_from_engine_meld,
    seat_from_engine_seat,
    tile_from_public_tile,
)

OPPONENT_COUNT = 3
"""viewer以外のtarget opponent数。"""

RED_FIVE_CATEGORIES: tuple[TileCategory, TileCategory, TileCategory] = (
    TileCategory.MANZU,
    TileCategory.PINZU,
    TileCategory.SOUZU,
)
"""red-five truthを保持する3色。順序はlisjong canonical red-five axisに従う。"""

_MAX_COPIES_PER_TILE_KIND = 4
_STABLE_EQUIVALENT_TILE_COUNT = 13
_MELD_STRUCTURAL_EQUIVALENT_COUNT = 3


class StructuralWaitUnavailableReason(Enum):
    """structural-wait targetだけがgenerateできなかったknown unsupported state。

    expected-count targetやanchor eligibilityへは影響しない。
    """

    UNSTABLE_HAND_SIZE = "unstable_hand_size"
    """`len(concealed) + 3 * len(melds) != 13`のtransient / 14-equivalent state。

    exact wait builderがstable 13-equivalent handだけを受け付けるための、
    このtarget固有のavailability条件である。expected-count targetはこの条件を
    共有しない。
    """


@dataclass(frozen=True, slots=True)
class OpponentIdentity:
    """target opponent rowのlogical identity。

    tensor row indexだけをidentityの正本にしない。viewerからの相対offsetに
    加えてabsolute seatとseat windを明示的にbindingするため、dealer / viewer
    rotationが起きてもrow identityは対応するopponentへ正しく追従する。
    """

    seat: Seat
    wind: Wind
    viewer_relative_offset: int

    def __post_init__(self) -> None:
        if not isinstance(self.seat, Seat):
            raise TypeError("seat must be a lisjong Seat")
        if not isinstance(self.wind, Wind):
            raise TypeError("wind must be a lisjong Wind")
        if self.viewer_relative_offset not in (1, 2, 3):
            raise ValueError("viewer_relative_offset must be 1, 2 or 3")


@dataclass(frozen=True, slots=True)
class OpponentExpectedCounts:
    """1 opponentのexact concealed truth。

    `counts`は34 base tile kindのrealized concealed physical copy count
    （0..4）である。normal fiveと赤5は34-axisでは同じ5へaggregateする。public
    meld tileはconcealed targetへ含めない。

    `red_five_present`はそのaggregateでは表現できない物理truthを、training-only
    側で不可逆に失わないために保持する。Phase 2ではred-five headを実装しない。
    """

    identity: OpponentIdentity
    counts: tuple[int, ...]
    red_five_present: tuple[bool, bool, bool]
    concealed_size: int

    def __post_init__(self) -> None:
        if not isinstance(self.identity, OpponentIdentity):
            raise TypeError("identity must be an OpponentIdentity")
        if len(self.counts) != TILE_TYPE_COUNT:
            raise ValueError(f"counts must contain exactly {TILE_TYPE_COUNT} values")
        for count in self.counts:
            if type(count) is not int or not 0 <= count <= _MAX_COPIES_PER_TILE_KIND:
                raise ValueError("each count must be an int in 0..4")
        if len(self.red_five_present) != len(RED_FIVE_CATEGORIES):
            raise ValueError("red_five_present must contain exactly 3 flags")
        if sum(self.counts) != self.concealed_size:
            raise ValueError("counts must sum to the concealed hand size")


@dataclass(frozen=True, slots=True)
class OpponentStructuralWait:
    """1 opponentのexact structural wait target、またはそのunavailable reason。

    `mask`は34 base tile kindのbinary multi-labelであり、`1`はその基本牌種を
    加えるとrealized handがstructurallyに完成することを表す。非聴牌の
    all-zero maskはvalid labelであり、`unavailable_reason`が設定された状態
    （maskが`None`）とは明確に区別する。
    """

    identity: OpponentIdentity
    mask: tuple[int, ...] | None
    unavailable_reason: StructuralWaitUnavailableReason | None

    def __post_init__(self) -> None:
        if not isinstance(self.identity, OpponentIdentity):
            raise TypeError("identity must be an OpponentIdentity")
        if (self.mask is None) == (self.unavailable_reason is None):
            raise ValueError("exactly one of mask / unavailable_reason must be set")
        if self.mask is not None:
            if len(self.mask) != TILE_TYPE_COUNT:
                raise ValueError(f"mask must contain exactly {TILE_TYPE_COUNT} values")
            for value in self.mask:
                if value not in (0, 1):
                    raise ValueError("mask values must be exactly 0 or 1")

    @property
    def is_available(self) -> bool:
        return self.mask is not None


@dataclass(frozen=True, slots=True)
class ExactTrainingLabels:
    """1 anchor分のtraining-only omniscient labelsとavailability metadata。

    availability / unsupported reasonはこの型の側にだけ存在し、player-safe
    anchorへは持ち込まない。
    """

    viewer_seat: Seat
    expected_counts: tuple[OpponentExpectedCounts, ...]
    structural_waits: tuple[OpponentStructuralWait, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.viewer_seat, Seat):
            raise TypeError("viewer_seat must be a lisjong Seat")
        if len(self.expected_counts) != OPPONENT_COUNT:
            raise ValueError("expected_counts must contain exactly 3 rows")
        if len(self.structural_waits) != OPPONENT_COUNT:
            raise ValueError("structural_waits must contain exactly 3 rows")

        identities = tuple(row.identity for row in self.expected_counts)
        if tuple(row.identity for row in self.structural_waits) != identities:
            raise ValueError(
                "expected_counts and structural_waits must share the same "
                "opponent row identities in the same order"
            )
        if len({identity.seat for identity in identities}) != OPPONENT_COUNT:
            raise ValueError("opponent rows must reference three distinct seats")
        if self.viewer_seat in {identity.seat for identity in identities}:
            raise ValueError("the viewer seat must not appear as a target opponent")

    @property
    def opponent_identities(self) -> tuple[OpponentIdentity, ...]:
        return tuple(row.identity for row in self.expected_counts)

    def expected_count(self, wind: Wind, tile_type: object) -> int:
        """logical windとbase tile kindからexact expected countを引く。

        row位置ではなくlogical identityで引くaccessorを正本にする。
        """
        for row in self.expected_counts:
            if row.identity.wind is wind:
                return row.counts[tile_type_index(tile_type)]
        raise KeyError(f"{wind!r} is not a target opponent of this anchor")


def _opponent_identity(
    viewer_seat: Seat,
    target_seat: Seat,
    dealer_seat: Seat,
) -> OpponentIdentity:
    """viewer相対offsetとlogical seat / windを両方bindingしたidentityを作る。"""
    return OpponentIdentity(
        seat=target_seat,
        wind=wind_for_seat(target_seat, dealer_seat),
        viewer_relative_offset=(int(target_seat) - int(viewer_seat)) % 4,
    )


def _concealed_lisjong_tiles(round_state: RoundState, engine_seat: EngineSeat) -> tuple:
    """omniscient concealed handを、赤5 identityを保ったままlisjong Tileへ変換する。"""
    return tuple(
        tile_from_public_tile(public_tile(engine_tile))
        for engine_tile in round_state.hand_tiles(engine_seat)
    )


def _own_melds(round_state: RoundState, engine_seat: EngineSeat) -> tuple:
    """opponentのmeldをlisjong `PublicMeld`へ変換する。

    exact wait builderの既存`own_melds` semanticsへそのまま渡す。chi / pon /
    いずれの槓もcompleted 1 meldとして数えられる。
    """
    return tuple(
        public_meld_from_engine_meld(public_meld(meld))
        for meld in round_state.melds(engine_seat)
    )


def expected_counts_for_concealed_hand(
    identity: OpponentIdentity,
    concealed_tiles: tuple,
) -> OpponentExpectedCounts:
    """concealed truthからexpected-count rowとred-five truthを導出する。

    `concealed_hand_marginals()`はconcealed tilesだけからexact
    `expected_count_raw` / `red_five_probability_raw`を導出する既存の共通
    ロジックであり、Phase 2でもこれを再利用してsemanticsの二重定義を避ける。

    このtargetにstable 13-equivalent constraintを課さない。concealed physical
    truthを数えられればexpected-count labelは生成できる。
    """
    expected_count_raw, red_five_probability_raw = concealed_hand_marginals(
        concealed_tiles
    )

    counts = []
    for raw in expected_count_raw:
        if raw % SCALE != 0:
            raise ValueError(
                "concealed expected counts must be exact integer copy counts"
            )
        count = raw // SCALE
        if not 0 <= count <= _MAX_COPIES_PER_TILE_KIND:
            # 物理的に不可能なcopy数はengine invariant violationであり、
            # ordinary unavailableへ丸めずfail closedする。
            raise ValueError(
                "concealed copy count outside the physical 0..4 range; "
                "this indicates an engine state inconsistency"
            )
        counts.append(count)

    return OpponentExpectedCounts(
        identity=identity,
        counts=tuple(counts),
        red_five_present=tuple(raw == SCALE for raw in red_five_probability_raw),
        concealed_size=len(concealed_tiles),
    )


def structural_wait_for_hand(
    identity: OpponentIdentity,
    concealed_tiles: tuple,
    own_melds: tuple,
) -> OpponentStructuralWait:
    """stable 13-equivalent handのときだけexact structural wait maskを作る。

    stable条件はこのtarget固有のavailability条件として先に判定し、known
    unsupported stateだけをreason codeにする。条件を満たしたあとの
    `exact_hand_belief_with_waits()`のfailureはunexpectedであり、
    `unavailable`へ丸めずそのまま伝播させる。
    """
    structural_size = len(concealed_tiles) + _MELD_STRUCTURAL_EQUIVALENT_COUNT * len(
        own_melds
    )
    if structural_size != _STABLE_EQUIVALENT_TILE_COUNT:
        return OpponentStructuralWait(
            identity=identity,
            mask=None,
            unavailable_reason=(StructuralWaitUnavailableReason.UNSTABLE_HAND_SIZE),
        )

    belief = exact_hand_belief_with_waits(concealed_tiles, own_melds)
    mask = []
    for index in range(TILE_TYPE_COUNT):
        raw = belief.wait_probability_raw[index]
        if raw not in (0, SCALE):
            raise ValueError(
                "exact structural wait ground truth must be binary 0 / SCALE"
            )
        mask.append(1 if raw == SCALE else 0)

    return OpponentStructuralWait(
        identity=identity,
        mask=tuple(mask),
        unavailable_reason=None,
    )


def build_exact_training_labels(
    round_state: RoundState,
    viewer_seat: EngineSeat,
) -> ExactTrainingLabels:
    """anchor時点のomniscient truthから、training-only exact labelsを構成する。

    `round_state`はprivileged omniscient stateであり、この関数の外（player-safe
    anchor path / feature path）へは渡さない。
    """
    if not isinstance(round_state, RoundState):
        raise TypeError("round_state must be a lisjong-engine RoundState")
    if not isinstance(viewer_seat, EngineSeat):
        raise TypeError("viewer_seat must be a lisjong-engine Seat")

    viewer = seat_from_engine_seat(viewer_seat)
    dealer_seat = seat_from_engine_seat(round_state.dealer_seat)

    rows: list[tuple[OpponentIdentity, tuple, tuple]] = []
    for engine_seat in EngineSeat:
        if engine_seat is viewer_seat:
            continue
        target_seat = seat_from_engine_seat(engine_seat)
        rows.append(
            (
                _opponent_identity(viewer, target_seat, dealer_seat),
                _concealed_lisjong_tiles(round_state, engine_seat),
                _own_melds(round_state, engine_seat),
            )
        )

    # row順序はviewer相対offsetで固定する。absolute seat / windはidentityが
    # 保持するため、dealer rotationでもrow identityは対応先へ追従する。
    rows.sort(key=lambda row: row[0].viewer_relative_offset)

    return ExactTrainingLabels(
        viewer_seat=viewer,
        expected_counts=tuple(
            expected_counts_for_concealed_hand(identity, concealed)
            for identity, concealed, _ in rows
        ),
        structural_waits=tuple(
            structural_wait_for_hand(identity, concealed, melds)
            for identity, concealed, melds in rows
        ),
    )
