import json
import unittest

from riichienv import RiichiEnv


class MortalRiichiEnvApiIntegrationTest(unittest.TestCase):
    def test_new_events_batch_and_mjai_action_resolution_match_riichienv_048(
        self,
    ) -> None:
        env = RiichiEnv(seed=0, game_mode="4p-red-single")
        observations = env.reset()
        observation = observations[0]

        events = observation.new_events()
        self.assertIsInstance(events, list)
        self.assertGreater(len(events), 1)
        self.assertTrue(all(type(event) is str for event in events))

        legal_action = observation.legal_actions()[0]
        response = json.loads(legal_action.to_mjai())
        response["meta"] = {"q_values": [0.0], "eval_time_ns": 1}
        selected = observation.select_action_from_mjai(json.dumps(response))

        self.assertIsNotNone(selected)
        self.assertEqual(selected.to_dict(), legal_action.to_dict())

    def test_illegal_mjai_action_returns_none_instead_of_a_fallback(self) -> None:
        env = RiichiEnv(seed=0, game_mode="4p-red-single")
        observation = env.reset()[0]

        selected = observation.select_action_from_mjai(
            '{"type":"dahai","actor":0,"pai":"?"}'
        )

        self.assertIsNone(selected)


if __name__ == "__main__":
    unittest.main()
