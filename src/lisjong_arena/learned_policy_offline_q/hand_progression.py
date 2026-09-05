"""Measurement D — player-safe hand-progression derivation (Issue #152).

Issue #152のMeasurement Dは、**既存artifactのplayer-safe evidenceから一意に
復元できる場合だけ**実施する。このmoduleはその「一意に復元できるか」を機械的に
判定し、できない場合を`UNAVAILABLE`としてfail closedへ倒す責務だけを持つ。

```text
retained macro-transition row
    feature_values[own_hand.tile_counts]        自seatのconcealed hand
    legal discard vocabulary index              打牌候補
        |
        v
reconstruct_concealed_tiles()                   exactな整数枚数へ逆写像
        |                                       曖昧なら fail closed
        v
discard_tile_for_index()                        lisjong action vocabularyのdecoder
        |
        v
lisjong.hand_evaluation.calculate_shanten()     既存first-party primitive
```

**このmoduleが使う情報は自seatのconcealed handと打牌候補だけである。**
opponent hand、wall、future state、teacher-internal analysisを読まない。
`feature_values`は該当seatのPolicy-visible observationとして記録されたもので
あり、そのうちさらに`own_hand`グループだけを読む。

**shanten semanticsを再実装しない。** `calculate_shanten()`はlisjongの公開
契約であり、赤5と通常5の同一視も確定面子数の判断もそちらが所有する。Arena側は
`Tile`列を組み立てて渡すだけである。

**ukeireはこのmoduleでは導出しない。** 既存のukeire semanticsは
`lisjong.policies.ukeire.UkeirePolicy`がPolicyInput束縛のprivate helperとして
所有しており、Arenaから一意に再利用できる公開契約が存在しない。Arena側で
同等物を書き直すことは「既存semanticsと異なるukeire定義」の新規導入になるため
行わず、`UKEIRE_UNAVAILABLE_REASON`として明示的に`UNAVAILABLE`扱いにする。
"""

from dataclasses import dataclass
from enum import Enum

from lisjong.action_vocabulary import decode_action
from lisjong.hand_evaluation import calculate_shanten
from lisjong.policy_contract import Seat, Tile
from lisjong.policy_contract.action import DiscardAction

from lisjong_arena.learned_policy_input.feature import TILE_AXIS, TILE_AXIS_SIZE
from lisjong_arena.learned_policy_input.tensor import (
    FEATURE_GROUPS,
    TILE_AXIS_LABELS,
    TILE_COUNT_SCALE,
)

from .errors import OfflineQAmbiguousStateError, OfflineQDiagnosisError
from .protocol import FEATURE_DIMENSION, VOCABULARY_SIZE, action_family

MEASUREMENT_D_ID = "arena-learned-policy-offlineq-hand-progression-v1"

DISCARD_ACTION_FAMILY = "discard"
"""Measurement A-Dが対象にするordinary discard vocabulary blockのfamily名。"""

MAX_COPIES_PER_TILE_AXIS_ENTRY = 4
"""37牌軸1entryあたりの物理上限枚数（赤5は独立entryなので実際は1）。"""

VALID_CONCEALED_TILE_COUNTS = frozenset({1, 2, 4, 5, 7, 8, 10, 11, 13, 14})
"""`calculate_shanten()`が受け付ける純手牌枚数。確定面子0..4個に対応する。

lisjong側の公開契約と同じ集合をここへ複製しているのは、reconstructionが
一意でない行を**calculate_shanten()を呼ぶ前に**拒否するためであり、向聴
semanticsを再定義するためではない。
"""

UKEIRE_UNAVAILABLE_REASON = (
    "ukeire semantics are owned by lisjong UkeirePolicy as a PolicyInput-bound "
    "private helper; Arena has no first-party public ukeire contract to reuse "
    "exactly, and defining an Arena-local equivalent would introduce a second "
    "ukeire definition"
)


class MeasurementAvailability(Enum):
    """Measurement D（およびその構成要素）のavailability。"""

    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"


def _error(message: str) -> OfflineQDiagnosisError:
    return OfflineQDiagnosisError(message)


def _own_hand_group():
    groups = [group for group in FEATURE_GROUPS if group.name == "own_hand"]
    if len(groups) != 1:
        raise _error("the locked feature layout has no unique own_hand group")
    return groups[0]


_OWN_HAND_GROUP = _own_hand_group()
OWN_HAND_TILE_COUNT_START = _OWN_HAND_GROUP.start
OWN_HAND_TILE_COUNT_STOP = _OWN_HAND_GROUP.start + TILE_AXIS_SIZE

if (
    _OWN_HAND_GROUP.stop != FEATURE_DIMENSION
    or OWN_HAND_TILE_COUNT_STOP > _OWN_HAND_GROUP.stop
    or len(TILE_AXIS) != TILE_AXIS_SIZE
    or len(TILE_AXIS_LABELS) != TILE_AXIS_SIZE
):
    raise RuntimeError("the locked own_hand feature layout drifted")

_DISCARD_INDICES = tuple(
    index
    for index in range(VOCABULARY_SIZE)
    if action_family(index) == DISCARD_ACTION_FAMILY
)
if not _DISCARD_INDICES or _DISCARD_INDICES != tuple(
    range(_DISCARD_INDICES[0], _DISCARD_INDICES[-1] + 1)
):
    raise RuntimeError("the ordinary discard vocabulary block is not contiguous")


def _decode_discard_tiles() -> tuple[Tile, ...]:
    """discard blockの各indexをTileへ落とす。

    vocabulary contract上、DiscardActionのkeyは`(DiscardAction, tile_index,
    tsumogiri)`でありactorを含まない。したがってこの写像はactorに依存しない。
    """
    tiles: list[Tile] = []
    for index in _DISCARD_INDICES:
        action = decode_action(index, Seat(0))
        if not isinstance(action, DiscardAction):
            raise RuntimeError("the discard vocabulary block decoded a non-discard")
        tiles.append(action.tile)
    return tuple(tiles)


_DISCARD_TILES = _decode_discard_tiles()


def is_discard_index(index: int) -> bool:
    """vocabulary indexがordinary discard blockに属するか。"""
    if type(index) is not int:
        raise TypeError("index must be an int")
    return index in _DISCARD_INDICES


def discard_tile_for_index(index: int) -> Tile:
    """ordinary discard vocabulary indexのTile identityを返す。

    `tsumogiri`区分は同じTileの2 indexへ分かれるが、打牌後の純手牌は同一に
    なるため、Measurement Dはtile identityだけを使う。
    """
    if not is_discard_index(index):
        raise _error(f"vocabulary index {index} is not an ordinary discard action")
    return _DISCARD_TILES[index - _DISCARD_INDICES[0]]


def reconstruct_concealed_tiles(feature_values) -> tuple[Tile, ...]:
    """feature rowの`own_hand.tile_counts`から自seatのconcealed handを復元する。

    `own_hand.tile_counts`は`count / 4.0`のexact float32であり、0..4の整数
    枚数へ一意に戻る。整数へ戻らない値、範囲外の値、`calculate_shanten()`が
    受け付けない手牌枚数はいずれも「一意に復元できない」ため、推測で埋めず
    `OfflineQAmbiguousStateError`でfail closedする。
    """
    values = feature_values
    if len(values) != FEATURE_DIMENSION:
        raise _error(f"feature row dimension must be {FEATURE_DIMENSION}")

    tiles: list[Tile] = []
    for offset in range(TILE_AXIS_SIZE):
        scaled = float(values[OWN_HAND_TILE_COUNT_START + offset])
        count = scaled * TILE_COUNT_SCALE
        rounded = round(count)
        if count != rounded:
            raise OfflineQAmbiguousStateError(
                "own_hand tile count does not decode to an exact integer; the "
                "concealed hand cannot be reconstructed unambiguously"
            )
        if not 0 <= rounded <= MAX_COPIES_PER_TILE_AXIS_ENTRY:
            raise OfflineQAmbiguousStateError(
                "own_hand tile count is outside the physical 0..4 range"
            )
        tiles.extend(TILE_AXIS[offset] for _ in range(rounded))

    if len(tiles) not in VALID_CONCEALED_TILE_COUNTS:
        raise OfflineQAmbiguousStateError(
            f"reconstructed concealed hand size {len(tiles)} is not a valid "
            "concealed hand size; the fixed-meld count is not recoverable"
        )
    return tuple(tiles)


def post_discard_tiles(
    concealed_tiles: tuple[Tile, ...], discard_tile: Tile
) -> tuple[Tile, ...]:
    """打牌候補を1枚だけ取り除いた純手牌を返す。

    legal maskが立っているdiscard indexは、その牌が実際に手牌にあることを
    意味する。無い場合はfeature reconstructionとlegal maskが矛盾している
    ため、近い牌へ丸めずfail closedする。
    """
    remaining = list(concealed_tiles)
    try:
        remaining.remove(discard_tile)
    except ValueError:
        raise OfflineQAmbiguousStateError(
            "a legal discard tile is absent from the reconstructed concealed "
            "hand; the state cannot be reconstructed unambiguously"
        ) from None
    if len(remaining) not in VALID_CONCEALED_TILE_COUNTS:
        raise OfflineQAmbiguousStateError(
            f"post-discard concealed hand size {len(remaining)} is not a valid "
            "concealed hand size"
        )
    return tuple(remaining)


@dataclass(frozen=True, slots=True)
class HandProgression:
    """1 rowの1 candidate discardについてのplayer-safe hand progression。"""

    pre_discard_shanten: int
    post_discard_shanten: int

    @property
    def keeps_shanten(self) -> bool:
        return self.post_discard_shanten == self.pre_discard_shanten

    @property
    def worsens_shanten(self) -> bool:
        return self.post_discard_shanten > self.pre_discard_shanten


def hand_progression_for_row(
    feature_values, discard_action_indices
) -> tuple[HandProgression, ...]:
    """1 rowの複数candidate discardを、同じcurrent stateから比較する。

    concealed handの復元と打牌前向聴数は1 rowにつき1回だけ行う。打牌は向聴数を
    改善しないため`post >= pre`が構造上の不変条件であり、それが崩れる場合は
    reconstructionが誤っているので値を採用せずfail closedする。
    """
    concealed = reconstruct_concealed_tiles(feature_values)
    pre = calculate_shanten(concealed)
    progressions: list[HandProgression] = []
    for index in discard_action_indices:
        remaining = post_discard_tiles(concealed, discard_tile_for_index(index))
        post = calculate_shanten(remaining)
        if post < pre:
            raise OfflineQAmbiguousStateError(
                "a discard decreased the shanten count; the reconstructed hand is "
                "not consistent with the locked shanten contract"
            )
        progressions.append(
            HandProgression(pre_discard_shanten=pre, post_discard_shanten=post)
        )
    return tuple(progressions)


def hand_progression(feature_values, discard_action_index: int) -> HandProgression:
    """1 rowの1 candidate discardについて打牌前後の向聴数を返す。"""
    return hand_progression_for_row(feature_values, (discard_action_index,))[0]


__all__ = [
    "DISCARD_ACTION_FAMILY",
    "MAX_COPIES_PER_TILE_AXIS_ENTRY",
    "MEASUREMENT_D_ID",
    "OWN_HAND_TILE_COUNT_START",
    "OWN_HAND_TILE_COUNT_STOP",
    "UKEIRE_UNAVAILABLE_REASON",
    "VALID_CONCEALED_TILE_COUNTS",
    "HandProgression",
    "MeasurementAvailability",
    "discard_tile_for_index",
    "hand_progression",
    "hand_progression_for_row",
    "is_discard_index",
    "post_discard_tiles",
    "reconstruct_concealed_tiles",
]
