"""Small real-backend characterization for the current RiichiEnv pin.

The call / kan / ron and seed checks were introduced for 0.4.10 (#228) and
re-run unchanged on 0.4.11 (#560). They inspect upstream decisions without
passing call candidates through Arena's target resolver.
"""

import importlib.metadata
import random
import unittest

from riichienv import (
    Action,
    ActionType,
    Meld,
    MeldType,
    Observation,
    RiichiEnv,
    calculate_shanten,
)

_CALL_PRIORITY = (
    ActionType.RON,
    ActionType.TSUMO,
    ActionType.KAKAN,
    ActionType.DAIMINKAN,
    ActionType.PON,
    ActionType.CHI,
)


def _seek_legal_action(seed: int, target: ActionType):
    env = RiichiEnv(seed=seed, game_mode="4p-red-single")
    observations = env.reset()
    for _ in range(200):
        chosen = {}
        for player_id, observation in observations.items():
            legal = observation.legal_actions()
            if not legal:
                continue
            matching = next(
                (action for action in legal if action.action_type == target), None
            )
            if matching is not None:
                return env, observations, player_id, observation, matching
            chosen[player_id] = next(
                (
                    action
                    for kind in _CALL_PRIORITY
                    for action in legal
                    if action.action_type == kind
                ),
                legal[0],
            )
        observations = env.step(chosen)
    raise AssertionError(f"{target} was not found in seed {seed}")


class RiichiEnvCompatibilityTest(unittest.TestCase):
    def test_runtime_identity_and_observation_serialization(self) -> None:
        self.assertEqual(importlib.metadata.version("riichienv"), "0.4.11")
        env = RiichiEnv(seed=7, game_mode="4p-red-single")
        observation = env.reset()[0]
        self.assertIsInstance(observation, Observation)
        self.assertIsInstance(observation.player_id, int)
        for name in (
            "hands",
            "melds",
            "discards",
            "dora_indicators",
            "scores",
            "riichi_declared",
            "last_tedashis",
        ):
            self.assertIsInstance(getattr(observation, name), list)
        for name in ("honba", "riichi_sticks", "round_wind", "oya", "kyoku_index"):
            self.assertIsInstance(getattr(observation, name), int)
        self.assertIsNone(observation.last_discard)
        self.assertIsInstance(observation.drawn_tile, int)
        self.assertTrue(observation.new_events())
        legal = observation.legal_actions()
        self.assertTrue(legal)
        self.assertIsInstance(legal[0], Action)
        self.assertEqual(legal[0].actor, observation.player_id)
        self.assertIsInstance(legal[0].tile, int)
        self.assertIsInstance(legal[0].consume_tiles, list)
        restored = Observation.deserialize_from_base64(
            observation.serialize_to_base64()
        )
        self.assertEqual(restored.player_id, observation.player_id)
        self.assertEqual(restored.hands, observation.hands)
        self.assertEqual(len(restored.legal_actions()), len(legal))

    def test_fresh_constructor_seed_and_reset_boundary(self) -> None:
        for mode in ("4p-red-single", "4p-red-east", "4p-red-half"):
            with self.subTest(mode=mode):
                first = RiichiEnv(seed=12345, game_mode=mode)
                second = RiichiEnv(seed=12345, game_mode=mode)
                first_obs = first.reset()
                second_obs = second.reset()
                self.assertEqual(first.game_mode, second.game_mode)
                self.assertEqual(first.wall, second.wall)
                self.assertEqual(first.hands, second.hands)
                self.assertEqual(first.mjai_log, second.mjai_log)
                self.assertEqual(
                    first_obs[0].serialize_to_base64(),
                    second_obs[0].serialize_to_base64(),
                )
                first.reset(seed=12345)
                self.assertNotEqual(first.wall, second.wall)

    def test_call_kan_and_ron_candidates_use_physical_tiles(self) -> None:
        for seed, kind in (
            (1, ActionType.CHI),
            (1, ActionType.PON),
            (5, ActionType.DAIMINKAN),
        ):
            with self.subTest(kind=kind):
                env, observations, player_id, observation, action = _seek_legal_action(
                    seed, kind
                )
                self.assertEqual(action.actor, player_id)
                self.assertEqual(observation.last_discard, action.tile)
                self.assertTrue(action.consume_tiles)
                chosen = {
                    pid: next(
                        (
                            candidate
                            for candidate in obs.legal_actions()
                            if candidate.action_type == kind
                        ),
                        obs.legal_actions()[0],
                    )
                    for pid, obs in observations.items()
                }
                next_observations = env.step(chosen)
                self.assertIn(player_id, next_observations)
                self.assertTrue(
                    any(isinstance(meld, Meld) for meld in env.melds[player_id])
                )

        env, observations, player_id, observation, action = _seek_legal_action(
            2, ActionType.KAKAN
        )
        self.assertEqual(action.actor, player_id)
        self.assertEqual(len(action.consume_tiles), 3)
        env.step({player_id: action})
        self.assertTrue(
            any(meld.meld_type == MeldType.Kakan for meld in env.melds[player_id])
        )

        env = RiichiEnv(seed=10, game_mode="4p-red-single")
        observations = env.reset()
        rng = random.Random(10)
        for _ in range(200):
            ron = next(
                (
                    (player_id, obs, action)
                    for player_id, obs in observations.items()
                    for action in obs.legal_actions()
                    if action.action_type == ActionType.RON
                ),
                None,
            )
            if ron is not None:
                player_id, observation, action = ron
                self.assertEqual(action.actor, player_id)
                self.assertEqual(action.tile, observation.last_discard)
                break
            observations = env.step(
                {
                    player_id: rng.choice(obs.legal_actions())
                    for player_id, obs in observations.items()
                    if obs.legal_actions()
                }
            )
        else:
            self.fail("seed 10 did not expose a Ron response")


def _tenpai_seeking_choice(observation, rng):
    """Never win or call; always riichi; otherwise discard to the lowest shanten."""
    legal = observation.legal_actions()
    for action in legal:
        if action.action_type == ActionType.RIICHI:
            return action
    discards = [a for a in legal if a.action_type == ActionType.DISCARD]
    if discards:
        hand = list(observation.hand)

        def shanten_after(action):
            rest = list(hand)
            rest.remove(action.tile)
            return calculate_shanten(rest)

        scored = [(shanten_after(a), rng.random(), a) for a in discards]
        return min(scored, key=lambda item: item[:2])[2]
    for action in legal:
        if action.action_type == ActionType.PASS:
            return action
    return legal[0]


class ExhaustiveDrawDeltasTest(unittest.TestCase):
    """#560: 0.4.11 (upstream smly/RiichiEnv#249) no longer repeats accepted
    riichi deposits in ``ryukyoku.deltas`` on an all-four-tenpai exhaustive draw
    (the #364 / smly/RiichiEnv#247 pattern on 0.4.10).

    Seed 3 with this tenpai-seeking play reaches that pattern at E4 honba 3
    (riichi by seats 0, 3 and 2). The seed is diagnostic only.
    """

    def test_every_draw_conserves_points_with_accepted_riichi(self) -> None:
        env = RiichiEnv(seed=3, game_mode="4p-red-half")
        observations = env.reset()
        rng = random.Random(3)
        while not env.done():
            observations = env.step(
                {
                    player_id: _tenpai_seeking_choice(observation, rng)
                    for player_id, observation in sorted(observations.items())
                }
            )

        checked = 0
        all_tenpai_with_riichi = 0
        start = None
        reachers: list[int] = []
        draw = None
        for event in env.mjai_log:
            kind = event["type"]
            if kind == "start_kyoku":
                if draw is not None:
                    expected = [
                        score - (1000 if seat in reachers else 0) + delta
                        for seat, (score, delta) in enumerate(
                            zip(start["scores"], draw["deltas"])
                        )
                    ]
                    self.assertEqual(event["scores"], expected)
                    checked += 1
                    if (
                        draw["reason"] == "exhaustive_draw"
                        and len(reachers) == 3
                        and draw["deltas"] == [0, 0, 0, 0]
                    ):
                        all_tenpai_with_riichi += 1
                start, reachers, draw = event, [], None
            elif kind == "reach_accepted":
                reachers.append(event["actor"])
            elif kind == "ryukyoku":
                draw = event
            elif kind == "hora":
                draw = None

        self.assertGreater(checked, 0)
        self.assertGreaterEqual(all_tenpai_with_riichi, 1)
