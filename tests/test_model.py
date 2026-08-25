import dataclasses
import unittest

from _round_stats_fixtures import (
    neutral_candidate_mahjong_metrics,
    neutral_seat_round_stats_tuple,
)
from lisjong.policy_contract import Seat

from lisjong_arena.model import (
    ComparisonPlan,
    PolicyMetrics,
    PolicySpec,
    SeatResult,
    SingleRoundCandidateMahjongMetrics,
    SingleRoundCandidateMetrics,
    SingleRoundEvaluationPlan,
    SingleRoundEvaluationResult,
    SingleRoundGameResult,
)


class _StubPolicy:
    def choose_action(self, decision: object) -> object:
        raise AssertionError("model tests must not execute policies")


def _spec(identity: str = "a") -> PolicySpec:
    return PolicySpec(identity=identity, factory=_StubPolicy)


class PolicySpecTest(unittest.TestCase):
    def test_identity_and_factory_are_kept_as_given(self) -> None:
        spec = _spec("ukeire-v1")

        self.assertEqual(spec.identity, "ukeire-v1")
        self.assertIs(spec.factory, _StubPolicy)

    def test_identity_is_not_derived_from_the_factory(self) -> None:
        """同じclassでも別identityで比較対象を区別できる。"""
        first = PolicySpec(identity="ukeire-v1", factory=_StubPolicy)
        second = PolicySpec(identity="ukeire-v2", factory=_StubPolicy)

        self.assertNotEqual(first.identity, second.identity)
        self.assertIs(first.factory, second.factory)

    def test_is_immutable(self) -> None:
        spec = _spec()

        with self.assertRaises(dataclasses.FrozenInstanceError):
            spec.identity = "b"

    def test_rejects_empty_identity(self) -> None:
        with self.assertRaises(ValueError):
            PolicySpec(identity="", factory=_StubPolicy)

    def test_rejects_non_str_identity(self) -> None:
        with self.assertRaises(TypeError):
            PolicySpec(identity=1, factory=_StubPolicy)

    def test_rejects_non_callable_factory(self) -> None:
        with self.assertRaises(TypeError):
            PolicySpec(identity="a", factory=_StubPolicy())


class ComparisonPlanTest(unittest.TestCase):
    def test_defaults_match_the_local_game_runner_conditions(self) -> None:
        plan = ComparisonPlan(policy_a=_spec("a"), policy_b=_spec("b"), seeds=(1,))

        self.assertEqual(plan.game_mode, "4p-red-half")
        self.assertEqual(plan.max_steps, 10_000)

    def test_keeps_seed_input_order(self) -> None:
        plan = ComparisonPlan(
            policy_a=_spec("a"),
            policy_b=_spec("b"),
            seeds=[30, 10, 20],
        )

        self.assertEqual(plan.seeds, (30, 10, 20))

    def test_is_immutable(self) -> None:
        plan = ComparisonPlan(policy_a=_spec("a"), policy_b=_spec("b"), seeds=(1,))

        with self.assertRaises(dataclasses.FrozenInstanceError):
            plan.seeds = (2,)

    def test_rejects_empty_seeds(self) -> None:
        with self.assertRaises(ValueError):
            ComparisonPlan(policy_a=_spec("a"), policy_b=_spec("b"), seeds=())

    def test_rejects_non_int_seeds(self) -> None:
        with self.assertRaises(TypeError):
            ComparisonPlan(policy_a=_spec("a"), policy_b=_spec("b"), seeds=(1, "2"))

    def test_rejects_unordered_seed_collections(self) -> None:
        """seed順序はcomparison protocolの一部なので順序が定義されない入力を拒否する。"""
        for seeds in ({1, 2, 3}, frozenset({1, 2, 3}), "123", iter((1, 2, 3))):
            with self.subTest(seeds=type(seeds).__name__):
                with self.assertRaises(TypeError):
                    ComparisonPlan(
                        policy_a=_spec("a"),
                        policy_b=_spec("b"),
                        seeds=seeds,
                    )

    def test_rejects_duplicate_seeds(self) -> None:
        """同じseed・同じrotationは決定的に同じgameになり母数だけを二重にする。"""
        with self.assertRaises(ValueError):
            ComparisonPlan(policy_a=_spec("a"), policy_b=_spec("b"), seeds=(1, 2, 1))

    def test_rejects_identical_policy_identities(self) -> None:
        with self.assertRaises(ValueError):
            ComparisonPlan(policy_a=_spec("same"), policy_b=_spec("same"), seeds=(1,))

    def test_rejects_non_policy_spec_matchup(self) -> None:
        with self.assertRaises(TypeError):
            ComparisonPlan(policy_a=_StubPolicy, policy_b=_spec("b"), seeds=(1,))
        with self.assertRaises(TypeError):
            ComparisonPlan(policy_a=_spec("a"), policy_b=_StubPolicy, seeds=(1,))

    def test_rejects_empty_game_mode(self) -> None:
        with self.assertRaises(ValueError):
            ComparisonPlan(
                policy_a=_spec("a"),
                policy_b=_spec("b"),
                seeds=(1,),
                game_mode="",
            )

    def test_rejects_non_str_game_mode(self) -> None:
        with self.assertRaises(TypeError):
            ComparisonPlan(
                policy_a=_spec("a"),
                policy_b=_spec("b"),
                seeds=(1,),
                game_mode=None,
            )

    def test_rejects_invalid_max_steps(self) -> None:
        for max_steps in (0, -1):
            with self.subTest(max_steps=max_steps):
                with self.assertRaises(ValueError):
                    ComparisonPlan(
                        policy_a=_spec("a"),
                        policy_b=_spec("b"),
                        seeds=(1,),
                        max_steps=max_steps,
                    )
        with self.assertRaises(TypeError):
            ComparisonPlan(
                policy_a=_spec("a"),
                policy_b=_spec("b"),
                seeds=(1,),
                max_steps=None,
            )


def _seat_result(**overrides: object) -> SeatResult:
    fields = {
        "seed": 12345,
        "rotation": 0,
        "game_mode": "4p-red-half",
        "seat": Seat.SEAT_0,
        "policy_identity": "minimal",
        "score": 24000,
        "rank": 2,
    }
    fields.update(overrides)
    return SeatResult(**fields)


class SeatResultTest(unittest.TestCase):
    def test_keeps_the_raw_comparison_fields(self) -> None:
        result = _seat_result()

        self.assertEqual(result.seed, 12345)
        self.assertEqual(result.rotation, 0)
        self.assertEqual(result.game_mode, "4p-red-half")
        self.assertIs(result.seat, Seat.SEAT_0)
        self.assertEqual(result.policy_identity, "minimal")
        self.assertEqual(result.score, 24000)
        self.assertEqual(result.rank, 2)

    def test_is_immutable(self) -> None:
        result = _seat_result()

        with self.assertRaises(dataclasses.FrozenInstanceError):
            result.rank = 1

    def test_rejects_invalid_values(self) -> None:
        with self.assertRaises(ValueError):
            _seat_result(rotation=-1)
        with self.assertRaises(ValueError):
            _seat_result(game_mode="")
        with self.assertRaises(ValueError):
            _seat_result(policy_identity="")
        with self.assertRaises(ValueError):
            _seat_result(rank=0)
        with self.assertRaises(ValueError):
            _seat_result(rank=5)
        with self.assertRaises(TypeError):
            _seat_result(seat=0)
        with self.assertRaises(TypeError):
            _seat_result(score="24000")


def _metrics(**overrides: object) -> PolicyMetrics:
    fields = {
        "policy_identity": "minimal",
        "game_count": 4,
        "seat_result_count": 8,
        "average_rank": 2.5,
        "average_score": 25_000.0,
        "first_count": 2,
        "second_count": 2,
        "third_count": 2,
        "fourth_count": 2,
    }
    fields.update(overrides)
    return PolicyMetrics(**fields)


class PolicyMetricsTest(unittest.TestCase):
    def test_is_immutable_and_keeps_valid_metrics(self) -> None:
        metrics = _metrics()

        self.assertEqual(metrics.seat_result_count, 8)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            metrics.average_rank = 1.0

    def test_rejects_invalid_identity_counts_and_averages(self) -> None:
        invalid_values = (
            ("policy_identity", ""),
            ("game_count", 0),
            ("seat_result_count", -1),
            ("average_rank", 0.9),
            ("average_rank", float("nan")),
            ("average_score", float("inf")),
            ("first_count", -1),
        )
        for field, value in invalid_values:
            with self.subTest(field=field, value=value):
                with self.assertRaises(ValueError):
                    _metrics(**{field: value})

    def test_rejects_incorrect_types_and_rank_count_mismatch(self) -> None:
        with self.assertRaises(TypeError):
            _metrics(game_count=True)
        with self.assertRaises(TypeError):
            _metrics(average_rank=2)
        with self.assertRaises(ValueError):
            _metrics(first_count=1)


class SingleRoundEvaluationPlanTest(unittest.TestCase):
    def test_defaults_and_keeps_seed_input_order(self) -> None:
        plan = SingleRoundEvaluationPlan(
            candidate=_spec("a"),
            baseline=_spec("b"),
            seeds=[30, 10, 20],
        )

        self.assertEqual(plan.max_steps, 10_000)
        self.assertEqual(plan.seeds, (30, 10, 20))

    def test_does_not_expose_a_game_mode_field(self) -> None:
        plan = SingleRoundEvaluationPlan(
            candidate=_spec("a"), baseline=_spec("b"), seeds=(1,)
        )

        self.assertFalse(hasattr(plan, "game_mode"))

    def test_is_immutable(self) -> None:
        plan = SingleRoundEvaluationPlan(
            candidate=_spec("a"), baseline=_spec("b"), seeds=(1,)
        )

        with self.assertRaises(dataclasses.FrozenInstanceError):
            plan.seeds = (2,)

    def test_rejects_empty_seeds(self) -> None:
        with self.assertRaises(ValueError):
            SingleRoundEvaluationPlan(
                candidate=_spec("a"), baseline=_spec("b"), seeds=()
            )

    def test_rejects_unordered_seed_collections(self) -> None:
        for seeds in ({1, 2, 3}, frozenset({1, 2, 3}), "123", iter((1, 2, 3))):
            with self.subTest(seeds=type(seeds).__name__):
                with self.assertRaises(TypeError):
                    SingleRoundEvaluationPlan(
                        candidate=_spec("a"), baseline=_spec("b"), seeds=seeds
                    )

    def test_rejects_duplicate_seeds(self) -> None:
        with self.assertRaises(ValueError):
            SingleRoundEvaluationPlan(
                candidate=_spec("a"), baseline=_spec("b"), seeds=(1, 2, 1)
            )

    def test_rejects_identical_candidate_and_baseline_identities(self) -> None:
        with self.assertRaises(ValueError):
            SingleRoundEvaluationPlan(
                candidate=_spec("same"), baseline=_spec("same"), seeds=(1,)
            )

    def test_rejects_non_policy_spec_matchup(self) -> None:
        with self.assertRaises(TypeError):
            SingleRoundEvaluationPlan(
                candidate=_StubPolicy, baseline=_spec("b"), seeds=(1,)
            )
        with self.assertRaises(TypeError):
            SingleRoundEvaluationPlan(
                candidate=_spec("a"), baseline=_StubPolicy, seeds=(1,)
            )

    def test_rejects_invalid_max_steps(self) -> None:
        for max_steps in (0, -1):
            with self.subTest(max_steps=max_steps):
                with self.assertRaises(ValueError):
                    SingleRoundEvaluationPlan(
                        candidate=_spec("a"),
                        baseline=_spec("b"),
                        seeds=(1,),
                        max_steps=max_steps,
                    )
        with self.assertRaises(TypeError):
            SingleRoundEvaluationPlan(
                candidate=_spec("a"), baseline=_spec("b"), seeds=(1,), max_steps=None
            )


def _safe_seat_round_stats(scores: object) -> tuple:
    """``scores``が壊れたshapeのtestでも、``seat_round_stats``自体はvalidな
    中立値にして、testが検証したい``scores``自身のvalidationだけを起こす。
    """
    try:
        end_scores = tuple(int(score) for score in scores)
        if len(end_scores) != 4:
            raise ValueError
    except TypeError, ValueError:
        end_scores = (25_000, 25_000, 25_000, 25_000)
    return neutral_seat_round_stats_tuple(end_scores)


def _single_round_game_result(**overrides: object) -> SingleRoundGameResult:
    fields = {
        "seed": 12345,
        "rotation": 0,
        "game_mode": "4p-red-single",
        "candidate_seat": Seat.SEAT_0,
        "scores": (30_000, 25_000, 25_000, 20_000),
    }
    fields.update(overrides)
    if "seat_round_stats" not in fields:
        fields["seat_round_stats"] = _safe_seat_round_stats(fields["scores"])
    return SingleRoundGameResult(**fields)


class SingleRoundGameResultTest(unittest.TestCase):
    def test_keeps_the_raw_evaluation_fields(self) -> None:
        result = _single_round_game_result()

        self.assertEqual(result.seed, 12345)
        self.assertEqual(result.rotation, 0)
        self.assertEqual(result.game_mode, "4p-red-single")
        self.assertIs(result.candidate_seat, Seat.SEAT_0)
        self.assertEqual(result.scores, (30_000, 25_000, 25_000, 20_000))

    def test_candidate_score_is_derived_from_the_candidate_seat(self) -> None:
        result = _single_round_game_result(
            candidate_seat=Seat.SEAT_2, scores=(30_000, 25_000, 21_000, 24_000)
        )

        self.assertEqual(result.candidate_score, 21_000)

    def test_is_immutable(self) -> None:
        result = _single_round_game_result()

        with self.assertRaises(dataclasses.FrozenInstanceError):
            result.rotation = 1

    def test_rejects_invalid_values(self) -> None:
        with self.assertRaises(ValueError):
            _single_round_game_result(rotation=-1)
        with self.assertRaises(ValueError):
            _single_round_game_result(game_mode="")
        with self.assertRaises(TypeError):
            _single_round_game_result(candidate_seat=0)
        with self.assertRaises(ValueError):
            _single_round_game_result(scores=(1, 2, 3))
        with self.assertRaises(TypeError):
            _single_round_game_result(scores=(1, 2, 3, "4"))


def _single_round_metrics(**overrides: object) -> SingleRoundCandidateMetrics:
    fields = {
        "candidate_identity": "minimal",
        "game_count": 4,
        "mean_candidate_score": 25_000.0,
        "seat_mean_scores": (24_000.0, 25_000.0, 26_000.0, 25_000.0),
    }
    fields.update(overrides)
    if "mahjong_metrics" not in fields:
        game_count = fields["game_count"]
        round_count = game_count if type(game_count) is int and game_count > 0 else 4
        fields["mahjong_metrics"] = neutral_candidate_mahjong_metrics(
            round_count=round_count
        )
    return SingleRoundCandidateMetrics(**fields)


class SingleRoundCandidateMetricsTest(unittest.TestCase):
    def test_is_immutable_and_keeps_valid_metrics(self) -> None:
        metrics = _single_round_metrics()

        self.assertEqual(metrics.game_count, 4)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            metrics.mean_candidate_score = 1.0

    def test_rejects_invalid_identity_and_counts(self) -> None:
        with self.assertRaises(ValueError):
            _single_round_metrics(candidate_identity="")
        with self.assertRaises(ValueError):
            _single_round_metrics(game_count=0)
        with self.assertRaises(TypeError):
            _single_round_metrics(game_count=True)

    def test_rejects_non_finite_scores(self) -> None:
        with self.assertRaises(ValueError):
            _single_round_metrics(mean_candidate_score=float("nan"))
        with self.assertRaises(ValueError):
            _single_round_metrics(seat_mean_scores=(float("inf"), 1.0, 1.0, 1.0))

    def test_rejects_incorrect_seat_mean_scores_shape(self) -> None:
        with self.assertRaises(ValueError):
            _single_round_metrics(seat_mean_scores=(1.0, 1.0, 1.0))
        with self.assertRaises(TypeError):
            _single_round_metrics(seat_mean_scores=(1, 1.0, 1.0, 1.0))


def _single_round_plan(seeds: tuple[int, ...] = (11, 22)) -> SingleRoundEvaluationPlan:
    return SingleRoundEvaluationPlan(
        candidate=_spec("candidate"), baseline=_spec("baseline"), seeds=seeds
    )


def _valid_game_results(
    plan: SingleRoundEvaluationPlan,
) -> tuple[SingleRoundGameResult, ...]:
    return tuple(
        SingleRoundGameResult(
            seed=seed,
            rotation=rotation,
            game_mode="4p-red-single",
            candidate_seat=Seat(rotation),
            scores=(25_000, 25_000, 25_000, 25_000),
            seat_round_stats=neutral_seat_round_stats_tuple(
                (25_000, 25_000, 25_000, 25_000)
            ),
        )
        for seed in plan.seeds
        for rotation in range(4)
    )


def _valid_metrics(plan: SingleRoundEvaluationPlan) -> SingleRoundCandidateMetrics:
    round_count = 4 * len(plan.seeds)
    return SingleRoundCandidateMetrics(
        candidate_identity=plan.candidate.identity,
        game_count=round_count,
        mean_candidate_score=25_000.0,
        seat_mean_scores=(25_000.0, 25_000.0, 25_000.0, 25_000.0),
        mahjong_metrics=neutral_candidate_mahjong_metrics(round_count=round_count),
    )


class SingleRoundEvaluationResultTest(unittest.TestCase):
    """Result construction時点のfail closed contractを固定する。

    正しい組み合わせ以外を静かに受理しないことを、``run_single_round_evaluation``
    経由ではなく``SingleRoundEvaluationResult``を直接構築して検証する。
    """

    def test_accepts_a_consistent_result(self) -> None:
        plan = _single_round_plan()
        game_results = _valid_game_results(plan)
        metrics = _valid_metrics(plan)

        result = SingleRoundEvaluationResult(
            plan=plan, game_results=game_results, candidate_metrics=metrics
        )

        self.assertIs(result.plan, plan)
        self.assertEqual(result.game_results, game_results)
        self.assertIsInstance(result.game_results, tuple)

    def test_game_results_is_coerced_to_an_immutable_tuple(self) -> None:
        plan = _single_round_plan()
        game_results = list(_valid_game_results(plan))
        metrics = _valid_metrics(plan)

        result = SingleRoundEvaluationResult(
            plan=plan, game_results=game_results, candidate_metrics=metrics
        )

        self.assertIsInstance(result.game_results, tuple)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            result.game_results = ()

    def test_rejects_non_plan(self) -> None:
        plan = _single_round_plan()
        with self.assertRaises(TypeError):
            SingleRoundEvaluationResult(
                plan=object(),
                game_results=_valid_game_results(plan),
                candidate_metrics=_valid_metrics(plan),
            )

    def test_rejects_game_results_containing_non_game_result_items(self) -> None:
        plan = _single_round_plan()
        game_results = list(_valid_game_results(plan))
        game_results[0] = object()

        with self.assertRaises(TypeError):
            SingleRoundEvaluationResult(
                plan=plan,
                game_results=tuple(game_results),
                candidate_metrics=_valid_metrics(plan),
            )

    def test_rejects_wrong_game_result_count(self) -> None:
        plan = _single_round_plan()
        game_results = _valid_game_results(plan)[:-1]

        with self.assertRaises(ValueError):
            SingleRoundEvaluationResult(
                plan=plan,
                game_results=game_results,
                candidate_metrics=_valid_metrics(plan),
            )

    def test_rejects_seed_order_that_does_not_match_the_plan(self) -> None:
        plan = _single_round_plan((11, 22))
        game_results = _valid_game_results(_single_round_plan((22, 11)))

        with self.assertRaises(ValueError):
            SingleRoundEvaluationResult(
                plan=plan,
                game_results=game_results,
                candidate_metrics=_valid_metrics(plan),
            )

    def test_rejects_out_of_order_rotation(self) -> None:
        plan = _single_round_plan()
        game_results = list(_valid_game_results(plan))
        game_results[0], game_results[1] = game_results[1], game_results[0]

        with self.assertRaises(ValueError):
            SingleRoundEvaluationResult(
                plan=plan,
                game_results=tuple(game_results),
                candidate_metrics=_valid_metrics(plan),
            )

    def test_rejects_candidate_seat_inconsistent_with_rotation(self) -> None:
        plan = _single_round_plan()
        game_results = list(_valid_game_results(plan))
        game_results[0] = dataclasses.replace(
            game_results[0], candidate_seat=Seat.SEAT_1
        )

        with self.assertRaises(ValueError):
            SingleRoundEvaluationResult(
                plan=plan,
                game_results=tuple(game_results),
                candidate_metrics=_valid_metrics(plan),
            )

    def test_rejects_a_non_single_round_game_mode(self) -> None:
        plan = _single_round_plan()
        game_results = list(_valid_game_results(plan))
        game_results[0] = dataclasses.replace(game_results[0], game_mode="4p-red-half")

        with self.assertRaises(ValueError):
            SingleRoundEvaluationResult(
                plan=plan,
                game_results=tuple(game_results),
                candidate_metrics=_valid_metrics(plan),
            )

    def test_rejects_non_candidate_metrics(self) -> None:
        plan = _single_round_plan()

        with self.assertRaises(TypeError):
            SingleRoundEvaluationResult(
                plan=plan,
                game_results=_valid_game_results(plan),
                candidate_metrics=object(),
            )

    def test_rejects_candidate_metrics_identity_mismatch(self) -> None:
        plan = _single_round_plan()
        metrics = dataclasses.replace(
            _valid_metrics(plan), candidate_identity="someone-else"
        )

        with self.assertRaises(ValueError):
            SingleRoundEvaluationResult(
                plan=plan,
                game_results=_valid_game_results(plan),
                candidate_metrics=metrics,
            )

    def test_rejects_candidate_metrics_game_count_mismatch(self) -> None:
        plan = _single_round_plan()
        # game_countとmahjong_metrics.round_countは内部的に整合させたまま、
        # SingleRoundEvaluationResult自体が要求するgame_results件数とだけ
        # 食い違う値にする。
        metrics = dataclasses.replace(
            _valid_metrics(plan),
            game_count=999,
            mahjong_metrics=neutral_candidate_mahjong_metrics(round_count=999),
        )

        with self.assertRaises(ValueError):
            SingleRoundEvaluationResult(
                plan=plan,
                game_results=_valid_game_results(plan),
                candidate_metrics=metrics,
            )


class SingleRoundGameResultRoundStatsTest(unittest.TestCase):
    def test_seat_round_stats_is_kept_as_a_tuple(self) -> None:
        result = _single_round_game_result()
        self.assertIsInstance(result.seat_round_stats, tuple)
        self.assertEqual(len(result.seat_round_stats), 4)

    def test_candidate_round_stats_is_derived_from_the_candidate_seat(self) -> None:
        seat_round_stats = neutral_seat_round_stats_tuple(
            (30_000, 25_000, 21_000, 24_000)
        )
        result = _single_round_game_result(
            candidate_seat=Seat.SEAT_2,
            scores=(30_000, 25_000, 21_000, 24_000),
            seat_round_stats=seat_round_stats,
        )

        self.assertIs(result.candidate_round_stats, seat_round_stats[2])

    def test_rejects_wrong_seat_round_stats_count(self) -> None:
        with self.assertRaises(ValueError):
            _single_round_game_result(
                seat_round_stats=neutral_seat_round_stats_tuple(
                    (30_000, 25_000, 25_000, 20_000)
                )[:3]
            )

    def test_rejects_seat_round_stats_containing_non_seat_round_stats_items(
        self,
    ) -> None:
        with self.assertRaises(TypeError):
            _single_round_game_result(
                seat_round_stats=(object(), object(), object(), object())
            )

    def test_rejects_seat_round_stats_end_score_mismatch_with_scores(self) -> None:
        mismatched = neutral_seat_round_stats_tuple((0, 25_000, 25_000, 20_000))
        with self.assertRaises(ValueError):
            _single_round_game_result(
                scores=(30_000, 25_000, 25_000, 20_000),
                seat_round_stats=mismatched,
            )


def _mahjong_metrics(**overrides: object) -> SingleRoundCandidateMahjongMetrics:
    fields = {
        "round_count": 4,
        "mean_round_score_delta": 0.0,
        "win_count": 0,
        "win_rate": 0.0,
        "mean_win_points": None,
        "deal_in_count": 0,
        "deal_in_rate": 0.0,
        "mean_deal_in_loss": None,
        "exhaustive_draw_count": 0,
        "exhaustive_draw_tenpai_count": 0,
        "exhaustive_draw_tenpai_rate": None,
        "tenpai_reached_count": 0,
        "mean_first_tenpai_turn": None,
    }
    fields.update(overrides)
    return SingleRoundCandidateMahjongMetrics(**fields)


class SingleRoundCandidateMahjongMetricsTest(unittest.TestCase):
    def test_accepts_all_zero_counts_with_none_means(self) -> None:
        metrics = _mahjong_metrics()
        self.assertIsNone(metrics.mean_win_points)
        self.assertIsNone(metrics.mean_deal_in_loss)
        self.assertIsNone(metrics.exhaustive_draw_tenpai_rate)
        self.assertIsNone(metrics.mean_first_tenpai_turn)

    def test_accepts_a_fully_populated_example(self) -> None:
        metrics = _mahjong_metrics(
            round_count=400,
            mean_round_score_delta=123.5,
            win_count=90,
            win_rate=90 / 400,
            mean_win_points=6150.0,
            deal_in_count=44,
            deal_in_rate=44 / 400,
            mean_deal_in_loss=5420.5,
            exhaustive_draw_count=50,
            exhaustive_draw_tenpai_count=24,
            exhaustive_draw_tenpai_rate=24 / 50,
            tenpai_reached_count=380,
            mean_first_tenpai_turn=9.4,
        )
        self.assertEqual(metrics.win_count, 90)
        self.assertEqual(metrics.mean_win_points, 6150.0)

    def test_rejects_non_positive_round_count(self) -> None:
        with self.assertRaises(ValueError):
            _mahjong_metrics(round_count=0)

    def test_rejects_win_rate_inconsistent_with_win_count(self) -> None:
        with self.assertRaises(ValueError):
            _mahjong_metrics(round_count=4, win_count=1, win_rate=0.5)

    def test_rejects_win_count_exceeding_round_count(self) -> None:
        with self.assertRaises(ValueError):
            _mahjong_metrics(round_count=4, win_count=5, win_rate=1.25)

    def test_rejects_mean_win_points_when_win_count_is_zero(self) -> None:
        with self.assertRaises(ValueError):
            _mahjong_metrics(win_count=0, win_rate=0.0, mean_win_points=100.0)

    def test_rejects_missing_mean_win_points_when_win_count_is_positive(self) -> None:
        with self.assertRaises(TypeError):
            _mahjong_metrics(round_count=4, win_count=1, win_rate=0.25)

    def test_rejects_deal_in_rate_inconsistent_with_deal_in_count(self) -> None:
        with self.assertRaises(ValueError):
            _mahjong_metrics(round_count=4, deal_in_count=1, deal_in_rate=0.5)

    def test_rejects_mean_deal_in_loss_when_deal_in_count_is_zero(self) -> None:
        with self.assertRaises(ValueError):
            _mahjong_metrics(deal_in_count=0, deal_in_rate=0.0, mean_deal_in_loss=1.0)

    def test_rejects_exhaustive_draw_tenpai_count_exceeding_draw_count(self) -> None:
        with self.assertRaises(ValueError):
            _mahjong_metrics(
                round_count=4,
                exhaustive_draw_count=1,
                exhaustive_draw_tenpai_count=2,
                exhaustive_draw_tenpai_rate=1.0,
            )

    def test_rejects_exhaustive_draw_tenpai_rate_when_draw_count_is_zero(self) -> None:
        with self.assertRaises(ValueError):
            _mahjong_metrics(
                exhaustive_draw_count=0,
                exhaustive_draw_tenpai_count=0,
                exhaustive_draw_tenpai_rate=0.0,
            )

    def test_rejects_missing_exhaustive_draw_tenpai_rate_when_draw_count_is_positive(
        self,
    ) -> None:
        with self.assertRaises(TypeError):
            _mahjong_metrics(
                round_count=4,
                exhaustive_draw_count=1,
                exhaustive_draw_tenpai_count=1,
            )

    def test_rejects_mean_first_tenpai_turn_when_tenpai_reached_count_is_zero(
        self,
    ) -> None:
        with self.assertRaises(ValueError):
            _mahjong_metrics(tenpai_reached_count=0, mean_first_tenpai_turn=1.0)

    def test_rejects_missing_mean_first_tenpai_turn_when_tenpai_reached_positive(
        self,
    ) -> None:
        with self.assertRaises(TypeError):
            _mahjong_metrics(round_count=4, tenpai_reached_count=1)

    def test_is_immutable(self) -> None:
        metrics = _mahjong_metrics()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            metrics.win_count = 1


class SingleRoundCandidateMetricsMahjongMetricsFieldTest(unittest.TestCase):
    def test_rejects_non_mahjong_metrics_type(self) -> None:
        with self.assertRaises(TypeError):
            _single_round_metrics(mahjong_metrics=object())

    def test_rejects_round_count_mismatch_with_game_count(self) -> None:
        with self.assertRaises(ValueError):
            _single_round_metrics(
                game_count=4,
                mahjong_metrics=neutral_candidate_mahjong_metrics(round_count=5),
            )


if __name__ == "__main__":
    unittest.main()
