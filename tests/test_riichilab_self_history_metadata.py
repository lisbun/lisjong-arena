"""Issue #269 typed self-history metadata contract tests."""

from __future__ import annotations

import unittest

from lisjong_arena.riichilab_self_history.errors import SelfHistoryError
from lisjong_arena.riichilab_self_history.models import (
    SelfHistoryGame,
    build_history,
    canonical_order,
    history_identity,
    resolve_self_log_url,
)
from lisjong_arena.riichilab_self_history.pagination import parse_self_history_page
from tests._riichilab_self_history_fixtures import api_game, page_payload


def _game(**overrides: object) -> SelfHistoryGame:
    value = api_game("g0001", "2026-09-18T10:30:59")
    value.update(overrides)
    return SelfHistoryGame.from_api_value(value, "game")


class RequiredFieldTest(unittest.TestCase):
    def test_every_required_field_is_projected(self) -> None:
        game = _game()
        self.assertEqual(game.game_id, "g0001")
        self.assertEqual(game.game_type, "ranked")
        self.assertEqual(game.player_count, 4)
        self.assertEqual(game.played_at, "2026-09-18T10:30:59")
        self.assertEqual(game.seat, 0)
        self.assertEqual(game.rank, 1)
        self.assertEqual(game.score, 32000)
        self.assertEqual(game.rating_before, 1500.5)
        self.assertEqual(game.rating_delta, 12.25)
        self.assertEqual(game.mu_before, 25.0)
        self.assertIs(game.is_disconnected, False)
        self.assertIs(game.is_penalized, False)

    def test_each_missing_required_field_rejects(self) -> None:
        for field in (
            "game_id",
            "game_type",
            "player_count",
            "played_at",
            "seat",
            "rank",
            "score",
            "rating_before",
            "rating_delta",
            "mu_before",
            "is_disconnected",
            "is_penalized",
        ):
            value = api_game("g0001", "2026-09-18T10:30:59")
            del value[field]
            with self.subTest(field=field):
                with self.assertRaisesRegex(SelfHistoryError, field):
                    SelfHistoryGame.from_api_value(value, "game")

    def test_non_object_game_rejects(self) -> None:
        with self.assertRaisesRegex(SelfHistoryError, "must be an object"):
            SelfHistoryGame.from_api_value(["g0001"], "game")


class TypeConfusionTest(unittest.TestCase):
    def test_integer_is_not_accepted_for_a_boolean_field(self) -> None:
        for field in ("is_disconnected", "is_penalized"):
            for value in (0, 1, "true", None):
                with self.subTest(field=field, value=value):
                    with self.assertRaisesRegex(SelfHistoryError, "JSON boolean"):
                        _game(**{field: value})

    def test_boolean_is_not_accepted_for_an_integer_field(self) -> None:
        for field in ("player_count", "seat", "rank", "score"):
            with self.subTest(field=field):
                with self.assertRaisesRegex(SelfHistoryError, "JSON integer"):
                    _game(**{field: True})

    def test_boolean_and_text_are_not_accepted_for_a_numeric_field(self) -> None:
        for field in ("rating_before", "rating_delta", "mu_before"):
            for value in (True, "1500", None):
                with self.subTest(field=field, value=value):
                    with self.assertRaisesRegex(SelfHistoryError, "JSON number"):
                        _game(**{field: value})

    def test_integral_rating_values_are_accepted_and_canonicalized_to_float(
        self,
    ) -> None:
        game = _game(rating_before=1500, rating_delta=-12, mu_before=25)
        for value in (game.rating_before, game.rating_delta, game.mu_before):
            self.assertIs(type(value), float)
        self.assertEqual(game.to_value()["rating_before"], 1500.0)

    def test_string_fields_reject_non_strings(self) -> None:
        with self.assertRaises(SelfHistoryError):
            _game(game_type=4)
        with self.assertRaises(SelfHistoryError):
            _game(game_id=1)


class BoundsTest(unittest.TestCase):
    def test_seat_must_be_inside_the_table(self) -> None:
        for seat in (-1, 4, 10):
            with self.subTest(seat=seat):
                with self.assertRaisesRegex(SelfHistoryError, "seat"):
                    _game(seat=seat)
        self.assertEqual(_game(seat=3).seat, 3)

    def test_rank_must_be_inside_the_table(self) -> None:
        for rank in (0, 5, -2):
            with self.subTest(rank=rank):
                with self.assertRaisesRegex(SelfHistoryError, "rank"):
                    _game(rank=rank)
        self.assertEqual(_game(rank=4).rank, 4)

    def test_three_player_bounds_follow_player_count(self) -> None:
        self.assertEqual(_game(player_count=3, seat=2, rank=3).rank, 3)
        with self.assertRaisesRegex(SelfHistoryError, "seat"):
            _game(player_count=3, seat=3, rank=1)
        with self.assertRaisesRegex(SelfHistoryError, "rank"):
            _game(player_count=3, seat=0, rank=4)

    def test_player_count_is_bounded(self) -> None:
        for player_count in (0, 2, 5):
            with self.subTest(player_count=player_count):
                with self.assertRaisesRegex(SelfHistoryError, "player_count"):
                    _game(player_count=player_count)


class PlayedAtTest(unittest.TestCase):
    def test_timezone_naive_played_at_is_preserved_as_naive(self) -> None:
        game = _game(played_at="2026-09-18 10:30:59")
        self.assertEqual(game.played_at, "2026-09-18T10:30:59")
        self.assertNotIn("Z", game.played_at)
        self.assertNotIn("+00:00", game.played_at)

    def test_timezone_aware_played_at_is_normalized_to_utc(self) -> None:
        self.assertEqual(
            _game(played_at="2026-09-18T10:30:59+09:00").played_at,
            "2026-09-18T01:30:59Z",
        )

    def test_mixed_awareness_within_one_history_rejects(self) -> None:
        games = (
            _game(game_id="g1", played_at="2026-09-18T10:30:59"),
            _game(game_id="g2", played_at="2026-09-18T11:30:59Z"),
        )
        with self.assertRaisesRegex(SelfHistoryError, "naive and timezone-aware"):
            build_history(
                bot_id=313,
                retrieved_at="2026-09-19T00:00:00Z",
                declared_total=2,
                games=games,
            )

    def test_log_url_uses_the_played_at_calendar_date_without_conversion(self) -> None:
        self.assertEqual(
            resolve_self_log_url(_game(played_at="2026-09-18 23:59:59")),
            "https://logs.riichi.dev/mjai-logs/2026/09/18/g0001.jsonl.gz",
        )


class RawProvenanceTest(unittest.TestCase):
    def test_unknown_extra_fields_stay_out_of_the_typed_model_but_in_raw(self) -> None:
        payload = page_payload(
            total=1,
            offset=0,
            games=[
                api_game(
                    "g0001",
                    "2026-09-18T10:30:59",
                    extra={"future_metric": 3.5, "opponent_ids": [1, 2, 3]},
                )
            ],
            has_more=False,
            next_cursor=None,
            extra_data={"server_note": "forward compatible"},
        )
        page = parse_self_history_page(
            payload,
            index=0,
            url="https://example.test",
            requested_offset=0,
            requested_cursor=None,
        )
        self.assertNotIn("future_metric", page.games[0].to_value())
        self.assertNotIn("opponent_ids", page.games[0].to_value())
        raw_game = page.raw["data"]["games"][0]
        self.assertEqual(raw_game["future_metric"], 3.5)
        self.assertEqual(page.raw["data"]["server_note"], "forward compatible")
        self.assertEqual(page.to_value()["response"], page.raw)


class CanonicalOrderingTest(unittest.TestCase):
    def test_ordering_is_played_at_then_game_id_and_independent_of_input_order(
        self,
    ) -> None:
        early = _game(game_id="zzzz", played_at="2026-09-01T00:00:00")
        late_a = _game(game_id="aaaa", played_at="2026-09-02T00:00:00")
        late_b = _game(game_id="bbbb", played_at="2026-09-02T00:00:00")
        expected = (early, late_a, late_b)
        self.assertEqual(canonical_order((late_b, early, late_a)), expected)
        self.assertEqual(canonical_order((late_a, late_b, early)), expected)

    def test_identity_is_independent_of_input_order(self) -> None:
        first = _game(game_id="aaaa", played_at="2026-09-02T00:00:00")
        second = _game(game_id="bbbb", played_at="2026-09-01T00:00:00")
        self.assertEqual(
            history_identity(313, (first, second)),
            history_identity(313, (second, first)),
        )

    def test_identity_depends_on_bot_id_and_content(self) -> None:
        game = _game()
        self.assertNotEqual(
            history_identity(313, (game,)), history_identity(1, (game,))
        )
        self.assertNotEqual(
            history_identity(313, (game,)),
            history_identity(313, (_game(score=1),)),
        )

    def test_declared_total_must_match_the_collected_games(self) -> None:
        with self.assertRaisesRegex(SelfHistoryError, "server declared 5"):
            build_history(
                bot_id=313,
                retrieved_at="2026-09-19T00:00:00Z",
                declared_total=5,
                games=(_game(),),
            )

    def test_duplicate_game_ids_reject(self) -> None:
        with self.assertRaisesRegex(SelfHistoryError, "duplicate game_id"):
            build_history(
                bot_id=313,
                retrieved_at="2026-09-19T00:00:00Z",
                declared_total=2,
                games=(_game(), _game()),
            )


if __name__ == "__main__":
    unittest.main()
