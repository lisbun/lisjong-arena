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

from lisjong_arena.riichienv.round_stats import SeatRoundStats

_RANKS = (1, 2, 3, 4)

SINGLE_ROUND_GAME_MODE = "4p-red-single"
"""single-round評価protocolが所有する固定game mode。callerから変更できない。"""

SINGLE_ROUND_ROTATION_COUNT = 4
"""1 seedあたりのcandidate seat rotation数。"""


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
class SingleRoundEvaluationPlan:
    """candidate 1体 + baseline 3体によるsingle-round評価を完全に決める不変条件。

    ``[A, B, B, B]``のABBB seat rotationはこの契約が意味を持つ理由そのもの
    なので、既存``ComparisonPlan``へoption追加せず独立した型として持つ。

    ``game_mode``はfieldとして公開しない。single-round評価は
    ``lisjong_arena.single_round_evaluation``のprotocol invariantとして
    常に``4p-red-single``を使い、callerが他のgame modeへ切り替えられる
    余地を持たせない。
    """

    candidate: PolicySpec
    baseline: PolicySpec
    seeds: tuple[int, ...]
    max_steps: int = 10_000

    def __post_init__(self) -> None:
        if not isinstance(self.candidate, PolicySpec):
            raise TypeError("candidate must be a PolicySpec")
        if not isinstance(self.baseline, PolicySpec):
            raise TypeError("baseline must be a PolicySpec")
        if self.candidate.identity == self.baseline.identity:
            raise ValueError("candidate and baseline must have distinct identities")
        if type(self.max_steps) is not int:
            raise TypeError("max_steps must be an int")
        if self.max_steps <= 0:
            raise ValueError("max_steps must be positive")

        object.__setattr__(self, "seeds", _normalize_seeds(self.seeds))


@dataclass(frozen=True, slots=True)
class SingleRoundGameResult:
    """1 seed・1 rotation分のflatなraw single-round evaluation record。

    candidate scoreを別fieldへ縮約せず、4 seat分のfinal scoresを正本として
    保持する。``candidate_score``はここから導出できる。

    ``seat_round_stats``も同じ思想でSeat 0..3分の生ABBB非依存 raw fact
    (``lisjong_arena.riichienv.round_stats.SeatRoundStats``)を正本として
    保持する。baseline 3 seat分を含めここで捨てず、``candidate_round_stats``
    がcandidateの分だけを``candidate_seat``経由で導出する。
    """

    seed: int
    rotation: int
    game_mode: str
    candidate_seat: Seat
    scores: tuple[int, int, int, int]
    seat_round_stats: tuple[
        SeatRoundStats, SeatRoundStats, SeatRoundStats, SeatRoundStats
    ]

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
        if not isinstance(self.candidate_seat, Seat):
            raise TypeError("candidate_seat must be a Seat")

        if isinstance(self.scores, (str, bytes, bytearray)):
            raise TypeError("scores must be an ordered collection of ints")
        try:
            scores = tuple(self.scores)
        except TypeError:
            raise TypeError("scores must be an ordered collection of ints") from None
        if len(scores) != 4:
            raise ValueError("scores must contain exactly four values")
        if any(type(score) is not int for score in scores):
            raise TypeError("scores must contain only ints")
        object.__setattr__(self, "scores", scores)

        try:
            seat_round_stats = tuple(self.seat_round_stats)
        except TypeError:
            raise TypeError("seat_round_stats must be an iterable") from None
        if len(seat_round_stats) != 4:
            raise ValueError("seat_round_stats must contain exactly four values")
        if any(not isinstance(item, SeatRoundStats) for item in seat_round_stats):
            raise TypeError("seat_round_stats must contain only SeatRoundStats")
        for seat, stats in enumerate(seat_round_stats):
            if stats.end_score != scores[seat]:
                raise ValueError(
                    f"seat_round_stats[{seat}].end_score does not match scores[{seat}]"
                )
        object.__setattr__(self, "seat_round_stats", seat_round_stats)

    @property
    def candidate_score(self) -> int:
        """``scores[candidate_seat]``から導出したcandidateのfinal score。"""
        return self.scores[self.candidate_seat]

    @property
    def candidate_round_stats(self) -> SeatRoundStats:
        """``seat_round_stats[candidate_seat]``から導出したcandidateのraw fact。"""
        return self.seat_round_stats[self.candidate_seat]


def validate_single_round_game_results(
    game_results: object,
    seeds: tuple[int, ...],
) -> tuple[SingleRoundGameResult, ...]:
    """ABBB raw game resultsがordered seedsと整合することを検証してtupleへ正規化する。

    ここで固定するのはsingle-round評価protocolのraw result contractである。

    - 件数は``rotations 4 x seeds``ちょうど
    - 順序は``seed入力順 -> rotation 0..3``
    - ``candidate_seat``は``Seat(rotation)``
    - ``game_mode``は``4p-red-single``固定

    実行結果``SingleRoundEvaluationResult``と、実行を伴わないartifact contract
    (``lisjong_arena.single_round_artifact``)が同じraw ordering ruleを別実装
    しないよう、両者がこの1関数を共有する。ここでは``seeds``が
    ``_normalize_seeds()``済みであることを前提とし、seed自体の正規化はしない。
    """
    if isinstance(game_results, (str, bytes, bytearray)):
        raise TypeError("game_results must be an ordered collection")
    try:
        results = tuple(game_results)
    except TypeError:
        raise TypeError("game_results must be an ordered collection") from None
    if any(not isinstance(item, SingleRoundGameResult) for item in results):
        raise TypeError("game_results must contain only SingleRoundGameResult")

    expected_count = SINGLE_ROUND_ROTATION_COUNT * len(seeds)
    if len(results) != expected_count:
        raise ValueError(
            f"game_results must contain exactly {expected_count} records "
            f"(seeds={len(seeds)} x "
            f"rotations={SINGLE_ROUND_ROTATION_COUNT}) but got {len(results)}"
        )

    expected_order = [
        (seed, rotation)
        for seed in seeds
        for rotation in range(SINGLE_ROUND_ROTATION_COUNT)
    ]
    for game_result, (expected_seed, expected_rotation) in zip(results, expected_order):
        if game_result.seed != expected_seed:
            raise ValueError(
                "game_results must be ordered by plan.seeds input order "
                f"but expected seed={expected_seed!r}, got "
                f"seed={game_result.seed!r}"
            )
        if game_result.rotation != expected_rotation:
            raise ValueError(
                "game_results must be ordered by rotation 0..3 within each "
                f"seed but expected rotation={expected_rotation!r}, got "
                f"rotation={game_result.rotation!r}"
            )
        if game_result.candidate_seat != Seat(expected_rotation):
            raise ValueError(
                "game_results candidate_seat must equal Seat(rotation) but "
                f"expected {Seat(expected_rotation)!r}, got "
                f"{game_result.candidate_seat!r}"
            )
        if game_result.game_mode != SINGLE_ROUND_GAME_MODE:
            raise ValueError(
                f"game_results must all use game_mode {SINGLE_ROUND_GAME_MODE!r} "
                f"but got {game_result.game_mode!r}"
            )

    return results


def _validate_bounded_count(name: str, value: object, *, maximum: int) -> None:
    if type(value) is not int:
        raise TypeError(f"{name} must be an int")
    if value < 0:
        raise ValueError(f"{name} must not be negative")
    if value > maximum:
        raise ValueError(f"{name} must not exceed {maximum}")


def _validate_unit_rate(name: str, value: object) -> None:
    if type(value) is not float:
        raise TypeError(f"{name} must be a float")
    if not isfinite(value):
        raise ValueError(f"{name} must be finite")
    if not 0.0 <= value <= 1.0:
        raise ValueError(f"{name} must be between 0.0 and 1.0")


def _validate_gated_mean(
    name: str, value: object, *, count: int, count_name: str
) -> None:
    """``count``が0なら``value``は``None``、正なら有限floatであることを検証する。"""
    if count == 0:
        if value is not None:
            raise ValueError(f"{name} must be None when {count_name} is 0")
        return
    if type(value) is not float:
        raise TypeError(f"{name} must be a float when {count_name} is positive")
    if not isfinite(value):
        raise ValueError(f"{name} must be finite")


@dataclass(frozen=True, slots=True)
class SingleRoundCandidateMahjongMetrics:
    """candidateについての局単位Mahjong metrics(Issue #61)。

    ``lisjong_arena.riichienv.round_stats.SeatRoundStats``のcandidate分だけを
    集計したvalueである。母数0の場合はrate / averageを``0.0``へ丸めず
    ``None``にする。rate / averageだけでなく分子・分母のcountも保持する。
    """

    round_count: int

    mean_round_score_delta: float

    win_count: int
    win_rate: float
    mean_win_points: float | None

    deal_in_count: int
    deal_in_rate: float
    mean_deal_in_loss: float | None

    exhaustive_draw_count: int
    exhaustive_draw_tenpai_count: int
    exhaustive_draw_tenpai_rate: float | None

    tenpai_reached_count: int
    mean_first_tenpai_turn: float | None

    def __post_init__(self) -> None:
        if type(self.round_count) is not int:
            raise TypeError("round_count must be an int")
        if self.round_count <= 0:
            raise ValueError("round_count must be positive")

        if type(self.mean_round_score_delta) is not float:
            raise TypeError("mean_round_score_delta must be a float")
        if not isfinite(self.mean_round_score_delta):
            raise ValueError("mean_round_score_delta must be finite")

        _validate_bounded_count("win_count", self.win_count, maximum=self.round_count)
        _validate_unit_rate("win_rate", self.win_rate)
        if self.win_rate != self.win_count / self.round_count:
            raise ValueError("win_rate must equal win_count / round_count")
        _validate_gated_mean(
            "mean_win_points",
            self.mean_win_points,
            count=self.win_count,
            count_name="win_count",
        )
        if self.mean_win_points is not None and self.mean_win_points <= 0.0:
            raise ValueError(
                "mean_win_points must be positive when win_count is positive"
            )

        _validate_bounded_count(
            "deal_in_count", self.deal_in_count, maximum=self.round_count
        )
        _validate_unit_rate("deal_in_rate", self.deal_in_rate)
        if self.deal_in_rate != self.deal_in_count / self.round_count:
            raise ValueError("deal_in_rate must equal deal_in_count / round_count")
        _validate_gated_mean(
            "mean_deal_in_loss",
            self.mean_deal_in_loss,
            count=self.deal_in_count,
            count_name="deal_in_count",
        )
        if self.mean_deal_in_loss is not None and self.mean_deal_in_loss <= 0.0:
            raise ValueError(
                "mean_deal_in_loss must be positive when deal_in_count is positive"
            )

        _validate_bounded_count(
            "exhaustive_draw_count",
            self.exhaustive_draw_count,
            maximum=self.round_count,
        )
        _validate_bounded_count(
            "exhaustive_draw_tenpai_count",
            self.exhaustive_draw_tenpai_count,
            maximum=self.exhaustive_draw_count,
        )
        if self.exhaustive_draw_count == 0:
            if self.exhaustive_draw_tenpai_rate is not None:
                raise ValueError(
                    "exhaustive_draw_tenpai_rate must be None when "
                    "exhaustive_draw_count is 0"
                )
        else:
            _validate_unit_rate(
                "exhaustive_draw_tenpai_rate", self.exhaustive_draw_tenpai_rate
            )
            if self.exhaustive_draw_tenpai_rate != (
                self.exhaustive_draw_tenpai_count / self.exhaustive_draw_count
            ):
                raise ValueError(
                    "exhaustive_draw_tenpai_rate must equal "
                    "exhaustive_draw_tenpai_count / exhaustive_draw_count"
                )

        _validate_bounded_count(
            "tenpai_reached_count",
            self.tenpai_reached_count,
            maximum=self.round_count,
        )
        _validate_gated_mean(
            "mean_first_tenpai_turn",
            self.mean_first_tenpai_turn,
            count=self.tenpai_reached_count,
            count_name="tenpai_reached_count",
        )
        if (
            self.mean_first_tenpai_turn is not None
            and self.mean_first_tenpai_turn < 0.0
        ):
            raise ValueError(
                "mean_first_tenpai_turn must not be negative when "
                "tenpai_reached_count is positive"
            )


@dataclass(frozen=True, slots=True)
class SingleRoundCandidateMetrics:
    """candidateについてのsingle-round評価の基本metrics。

    ``seat_mean_scores``はSeat 0..3順のtupleであり、``seat_mean_scores[seat]``
    がそのseatを担当した時のcandidate平均scoreになる。開始点``25000``を
    hard-codeしたpoint deltaはここでは扱わず、final score自体を正本とする。

    ``mahjong_metrics``はIssue #61で追加した局単位Mahjong metricsであり、
    既存のfinal-score系metrics(``mean_candidate_score`` /
    ``seat_mean_scores``)の意味は変更しない。
    """

    candidate_identity: str
    game_count: int
    mean_candidate_score: float
    seat_mean_scores: tuple[float, float, float, float]
    mahjong_metrics: SingleRoundCandidateMahjongMetrics

    def __post_init__(self) -> None:
        if type(self.candidate_identity) is not str:
            raise TypeError("candidate_identity must be a str")
        if not self.candidate_identity:
            raise ValueError("candidate_identity must not be empty")
        if type(self.game_count) is not int:
            raise TypeError("game_count must be an int")
        if self.game_count <= 0:
            raise ValueError("game_count must be positive")
        if type(self.mean_candidate_score) is not float:
            raise TypeError("mean_candidate_score must be a float")
        if not isfinite(self.mean_candidate_score):
            raise ValueError("mean_candidate_score must be finite")

        if isinstance(self.seat_mean_scores, (str, bytes, bytearray)):
            raise TypeError("seat_mean_scores must be an ordered collection of floats")
        try:
            seat_mean_scores = tuple(self.seat_mean_scores)
        except TypeError:
            raise TypeError(
                "seat_mean_scores must be an ordered collection of floats"
            ) from None
        if len(seat_mean_scores) != 4:
            raise ValueError("seat_mean_scores must contain exactly four values")
        for value in seat_mean_scores:
            if type(value) is not float:
                raise TypeError("seat_mean_scores must contain only floats")
            if not isfinite(value):
                raise ValueError("seat_mean_scores must contain only finite floats")
        object.__setattr__(self, "seat_mean_scores", seat_mean_scores)

        if not isinstance(self.mahjong_metrics, SingleRoundCandidateMahjongMetrics):
            raise TypeError(
                "mahjong_metrics must be a SingleRoundCandidateMahjongMetrics"
            )
        if self.mahjong_metrics.round_count != self.game_count:
            raise ValueError("mahjong_metrics.round_count must equal game_count")


@dataclass(frozen=True, slots=True)
class SingleRoundEvaluationResult:
    """1回のsingle-round評価の実行条件、raw result、candidate metrics。

    construction時点で、``game_results``が``plan``に対して意味的に整合した
    single-round評価の結果であることをfail closedで検証する。ここで拒否しない
    不正な組み合わせは、後段の読み手がraw resultの母数や意味を静かに誤読する
    ため、validationはこのvalue自身へ寄せる。
    """

    plan: SingleRoundEvaluationPlan
    game_results: tuple[SingleRoundGameResult, ...]
    candidate_metrics: SingleRoundCandidateMetrics

    def __post_init__(self) -> None:
        if not isinstance(self.plan, SingleRoundEvaluationPlan):
            raise TypeError("plan must be a SingleRoundEvaluationPlan")

        game_results = validate_single_round_game_results(
            self.game_results, self.plan.seeds
        )
        object.__setattr__(self, "game_results", game_results)
        expected_count = SINGLE_ROUND_ROTATION_COUNT * len(self.plan.seeds)

        if not isinstance(self.candidate_metrics, SingleRoundCandidateMetrics):
            raise TypeError("candidate_metrics must be a SingleRoundCandidateMetrics")
        if self.candidate_metrics.candidate_identity != self.plan.candidate.identity:
            raise ValueError(
                "candidate_metrics.candidate_identity must match "
                "plan.candidate.identity"
            )
        if self.candidate_metrics.game_count != expected_count:
            raise ValueError(
                f"candidate_metrics.game_count must equal {expected_count} but "
                f"got {self.candidate_metrics.game_count}"
            )


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
    "SINGLE_ROUND_GAME_MODE",
    "SINGLE_ROUND_ROTATION_COUNT",
    "ComparisonPlan",
    "ComparisonResult",
    "PolicyMetrics",
    "PolicySpec",
    "SeatResult",
    "SingleRoundCandidateMahjongMetrics",
    "SingleRoundCandidateMetrics",
    "SingleRoundEvaluationPlan",
    "SingleRoundEvaluationResult",
    "SingleRoundGameResult",
    "validate_single_round_game_results",
]
