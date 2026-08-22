"""Arena-local `ValidationSession` / `RankedSession` -> real lisjong
`RiichiLabSeatAdapter` minimal integration test(Issue #23)。

実RiichiEnvが生成する`Observation`を使い、fake WebSocket transportの
`start_game` / `request_action`から実`RiichiLabSeatAdapter` /
`MinimalPolicy`を経て送信前validation済みMJAI responseまで届くことを
確認する。あわせて、Policyへ`request_id` / `time` / `ack` / WebSocket
objectが一切漏れないことを確認する。

`RiichiLabSeatAdapter`自体のcorrectness(Policy呼び出し順序、Observation
deserialize、`possible_actions` semantic validation)の正本はlisjongに残る。
ここではArena-local Sessionが実Adapterを正しくconsumerとして配線できている
ことだけを、最小限のcoverageで確認する。fake adapterによる単体lifecycle
coverageは`test_riichilab_session.py`が担当する。
"""

import unittest

from _riichilab_session_test_helpers import server_style_request_action
from lisjong.policies import MinimalPolicy
from lisjong.policy_contract.seat import Seat
from riichienv import RiichiEnv

from lisjong_arena.riichilab.session import RankedSession, ValidationSession

_LEAKED_ATTRS = ("request_id", "time", "possible_actions", "ack", "transport")


class _RecordingPolicy:
    def __init__(self) -> None:
        self.seen_decisions: list[object] = []

    def choose_action(self, decision):
        self.seen_decisions.append(decision)
        return MinimalPolicy().choose_action(decision)


class ValidationSessionAdapterIntegrationTest(unittest.TestCase):
    def test_request_action_round_trips_through_real_adapter_and_policy(self) -> None:
        env = RiichiEnv(seed=42, game_mode="4p-red-east")
        observations = env.reset()
        player_id, observation = next(iter(observations.items()))
        seat = Seat(player_id)

        policy = _RecordingPolicy()
        session = ValidationSession(policy)
        session.handle_event({"type": "start_game", "id": int(seat)})

        request = server_style_request_action(observation, request_id=1)
        outgoing = session.handle_event(request)

        self.assertIsNotNone(outgoing)
        self.assertEqual(outgoing["request_id"], 1)
        self.assertIn("type", outgoing)

        self.assertEqual(len(policy.seen_decisions), 1)
        decision = policy.seen_decisions[0]
        self.assertTrue(hasattr(decision, "input"))
        self.assertTrue(hasattr(decision, "legal_actions"))
        for leaked_attr in _LEAKED_ATTRS:
            self.assertFalse(hasattr(decision, leaked_attr))


class RankedSessionAdapterIntegrationTest(unittest.TestCase):
    def test_request_uses_real_adapter_and_validation_boundary(self) -> None:
        env = RiichiEnv(seed=42, game_mode="4p-red-east")
        observations = env.reset()
        player_id, observation = next(iter(observations.items()))

        policy = _RecordingPolicy()
        session = RankedSession(policy)
        session.handle_event({"type": "start_game", "id": player_id})
        outgoing = session.handle_event(
            server_style_request_action(observation, request_id=37)
        )

        self.assertEqual(outgoing["request_id"], 37)
        self.assertIn("type", outgoing)
        self.assertEqual(len(policy.seen_decisions), 1)
        decision = policy.seen_decisions[0]
        for leaked_attr in _LEAKED_ATTRS:
            self.assertFalse(hasattr(decision, leaked_attr))


if __name__ == "__main__":
    unittest.main()
