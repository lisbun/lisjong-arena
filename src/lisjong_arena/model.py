"""Policy comparisonの入力条件と結果を表す不変value型。

ここに置くのは比較条件（``PolicySpec`` / ``ComparisonPlan``）とその結果
（``SeatResult`` / ``PolicyMetrics`` / ``ComparisonResult``）だけであり、
game進行・Policy判断・RiichiEnv固有表現は所有しない。``Seat``もArenaで
再定義せず``lisjong.policy_contract.Seat``をそのまま使用する。

意味が曖昧な比較結果を生まないため、入力valueはconstruction時点でfail closed
する。ここで拒否しない不正入力は、後段のcomparisonでraw resultやmetricsの
母数を静かに壊すため、validationはvalue側へ寄せる。
"""

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from math import isfinite

from lisjong.policy_contract import Policy, Seat

_RANKS = (1, 2, 3, 4)


@dataclass(frozen=True, slots=True)
class PolicySpec:
    """比較対象1つ分の明示的identityとPolicy factory。

    比較対象としてPolicy instanceそのものを保持しない。Arenaは各game・各seat
    ごとにfactoryから新しいPolicyを生成し、instanceをseat間・game間で共有
    しない（``lisjong_arena.comparison``を参照）。

    ``identity``はclass名から暗黙導出しない。``ukeire-v1`` / ``ukeire-v2`` の
    ように、同じclassでも設定違い・model違いを別の比較対象として区別できる
    余地を残すため、呼び出し側が明示する契約とする。

    ``factory``は呼び出すとlisjongの``Policy``契約へ適合するobjectを返す
    callableである。``Policy``はruntime-checkableでないstructural Protocol
    であり、``isinstance``では``choose_action``というmethod名の有無しか
    検査できないため、ここでは擬似的なruntime validationを行わない。実際の
    Policy契約の検証は既存のlisjong境界（``execute_policy()``）が担う。
    """

    identity: str
    factory: Callable[[], Policy]

    def __post_init__(self) -> None:
        if type(self.identity) is not str:
            raise TypeError("identity must be a str")
        if not self.identity:
            raise ValueError("identity must not be empty")
        if not callable(self.factory):
            raise TypeError("factory must be callable")


def _normalize_seeds(value: object) -> tuple[int, ...]:
    """入力順序を保持したままseedsをtupleへ正規化する。

    seedの入力順序はcomparison protocolの一部なので、順序が定義されない
    collection（set等）と、意図せず1文字ずつ展開されるstr / bytesは拒否する。
    """
    if isinstance(value, (str, bytes, bytearray)):
        raise TypeError("seeds must be an ordered collection of ints")
    if isinstance(value, Sequence):
        seeds = tuple(value)
    elif isinstance(value, Iterable):
        raise TypeError("seeds must be an ordered collection of ints")
    else:
        raise TypeError("seeds must be an ordered collection of ints")

    if not seeds:
        raise ValueError("seeds must not be empty")
    if any(type(seed) is not int for seed in seeds):
        raise TypeError("seeds must contain only ints")
    if len(set(seeds)) != len(seeds):
        raise ValueError("seeds must not contain duplicates")
    return seeds


@dataclass(frozen=True, slots=True)
class ComparisonPlan:
    """1回のPolicy comparisonを完全に決める不変の実行条件。

    同じ``ComparisonPlan``を同じPolicy実装へ適用すれば、raw resultとmetricsが
    再現する。``seeds``はordered collectionであり、入力順序をそのまま実行順序
    として扱う。

    重複seedを拒否するのは、同じseed・同じrotationのgameが決定的に同一結果へ
    なり、metricsの母数だけを二重に膨らませて比較の意味を曖昧にするためである。
    A/Bのidentity一致を拒否するのも同じ理由で、集計先を区別できなくなる。
    """

    policy_a: PolicySpec
    policy_b: PolicySpec
    seeds: tuple[int, ...]
    game_mode: str = "4p-red-half"
    max_steps: int = 10_000

    def __post_init__(self) -> None:
        if not isinstance(self.policy_a, PolicySpec):
            raise TypeError("policy_a must be a PolicySpec")
        if not isinstance(self.policy_b, PolicySpec):
            raise TypeError("policy_b must be a PolicySpec")
        if self.policy_a.identity == self.policy_b.identity:
            raise ValueError("policy_a and policy_b must have distinct identities")
        if type(self.game_mode) is not str:
            raise TypeError("game_mode must be a str")
        if not self.game_mode:
            raise ValueError("game_mode must not be empty")
        if type(self.max_steps) is not int:
            raise TypeError("max_steps must be an int")
        if self.max_steps <= 0:
            raise ValueError("max_steps must be positive")

        object.__setattr__(self, "seeds", _normalize_seeds(self.seeds))


@dataclass(frozen=True, slots=True)
class SeatResult:
    """1 game・1 seat分のflatなraw comparison record。

    comparison全体のraw resultはこのrecordの列であり、その順序自体も
    ``seed -> rotation -> seat``で安定する決定的な契約として扱う。

    ``LocalGameResult.steps`` / ``decisions``は最小comparisonに不要なので
    ここへは含めない。必要になった時点で拡張する。
    """

    seed: int
    rotation: int
    game_mode: str
    seat: Seat
    policy_identity: str
    score: int
    rank: int

    def __post_init__(self) -> None:
        if type(self.seed) is not int:
            raise TypeError("seed must be an int")
        if type(self.rotation) is not int:
            raise TypeError("rotation must be an int")
        if self.rotation < 0:
            raise ValueError("rotation must not be negative")
        if type(self.game_mode) is not str:
            raise TypeError("game_mode must be a str")
        if not self.game_mode:
            raise ValueError("game_mode must not be empty")
        if not isinstance(self.seat, Seat):
            raise TypeError("seat must be a Seat")
        if type(self.policy_identity) is not str:
            raise TypeError("policy_identity must be a str")
        if not self.policy_identity:
            raise ValueError("policy_identity must not be empty")
        if type(self.score) is not int:
            raise TypeError("score must be an int")
        if type(self.rank) is not int:
            raise TypeError("rank must be an int")
        if self.rank not in _RANKS:
            raise ValueError("rank must be one of 1, 2, 3, 4")


@dataclass(frozen=True, slots=True)
class PolicyMetrics:
    """1つのPolicy identityについての基本metrics。

    母数を明示的に区別する。

    - ``game_count``: そのPolicyが参加したgame数。1 gameで2 seatを担当しても
      1しか増えない。N seedなら4N
    - ``seat_result_count``: そのPolicyが担当したseat結果数。N seedなら8N

    average rank / average scoreと1st〜4th countsはいずれもseat resultを母数と
    する。同一game内の複数seat resultは相関しているため、これらを独立標本と
    みなす信頼区間・検定等はここでは扱わない。
    """

    policy_identity: str
    game_count: int
    seat_result_count: int
    average_rank: float
    average_score: float
    first_count: int
    second_count: int
    third_count: int
    fourth_count: int

    def __post_init__(self) -> None:
        if type(self.policy_identity) is not str:
            raise TypeError("policy_identity must be a str")
        if not self.policy_identity:
            raise ValueError("policy_identity must not be empty")

        for name in (
            "game_count",
            "seat_result_count",
            "first_count",
            "second_count",
            "third_count",
            "fourth_count",
        ):
            value = getattr(self, name)
            if type(value) is not int:
                raise TypeError(f"{name} must be an int")
            if value < 0:
                raise ValueError(f"{name} must not be negative")

        if self.game_count == 0:
            raise ValueError("game_count must be positive")
        if self.seat_result_count == 0:
            raise ValueError("seat_result_count must be positive")
        if self.game_count > self.seat_result_count:
            raise ValueError("game_count must not exceed seat_result_count")

        for name in ("average_rank", "average_score"):
            value = getattr(self, name)
            if type(value) is not float:
                raise TypeError(f"{name} must be a float")
            if not isfinite(value):
                raise ValueError(f"{name} must be finite")
        if not 1.0 <= self.average_rank <= 4.0:
            raise ValueError("average_rank must be between 1.0 and 4.0")

        rank_count = (
            self.first_count + self.second_count + self.third_count + self.fourth_count
        )
        if rank_count != self.seat_result_count:
            raise ValueError("rank counts must sum to seat_result_count")


@dataclass(frozen=True, slots=True)
class ComparisonResult:
    """1回のcomparisonの実行条件、raw result、Policy別metrics。

    identityをkeyにしたmappingではなくA/B固定のfieldで持つ。``ComparisonPlan``
    がA/B identityの重複をすでに拒否しているので集計先は一意だが、A/Bの対応を
    ``plan``と同じ形のまま保つほうが読み手にとって曖昧さがない。
    """

    plan: ComparisonPlan
    seat_results: tuple[SeatResult, ...]
    metrics_a: PolicyMetrics
    metrics_b: PolicyMetrics


__all__ = [
    "ComparisonPlan",
    "ComparisonResult",
    "PolicyMetrics",
    "PolicySpec",
    "SeatResult",
]
