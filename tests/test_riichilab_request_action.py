"""`lisjong_arena.riichilab.request_action`のprotocol-facing correctness(Arena-owned、Issue #27)。

lisjong Issue #38で確立したcontractをbehavior-preservingにArenaへcanonical
physical migrationしたものである。
"""

import unittest

from riichienv import RiichiEnv

from lisjong_arena.riichilab.adapter_errors import (
    MalformedRequestActionError,
    ObservationDeserializeError,
)
from lisjong_arena.riichilab.request_action import (
    ParsedRequestAction,
    parse_request_action,
)


def _real_observation_base64() -> str:
    env = RiichiEnv(seed=7, game_mode="4p-red-east")
    _pid, observation = next(iter(env.reset().items()))
    return observation.serialize_to_base64()


def _valid_request(**overrides: object) -> dict:
    request = {
        "type": "request_action",
        "request_id": 1,
        "possible_actions": [{"type": "none", "actor": 0}],
        "observation": _real_observation_base64(),
    }
    request.update(overrides)
    return request


class ParseRequestActionTest(unittest.TestCase):
    def test_accepts_a_well_formed_request_action(self) -> None:
        request = _valid_request()

        parsed = parse_request_action(request)

        self.assertIsInstance(parsed, ParsedRequestAction)
        self.assertEqual(parsed.request_id, 1)
        self.assertEqual(parsed.possible_actions, ({"type": "none", "actor": 0},))
        self.assertEqual(parsed.observation.player_id, 0)
        self.assertIsNone(parsed.time)

    def test_keeps_time_as_opaque_metadata_without_interpreting_it(self) -> None:
        request = _valid_request(time={"grace_ms": 500, "bank_ms": 10000})

        parsed = parse_request_action(request)

        self.assertEqual(parsed.time, {"grace_ms": 500, "bank_ms": 10000})

    def test_unknown_additional_fields_do_not_cause_rejection(self) -> None:
        request = _valid_request(
            future_field="value", another_future_field={"nested": True}
        )

        parsed = parse_request_action(request)

        self.assertEqual(parsed.request_id, 1)

    def test_rejects_non_mapping_input(self) -> None:
        with self.assertRaises(MalformedRequestActionError):
            parse_request_action(["not", "a", "mapping"])

    def test_rejects_wrong_type_value(self) -> None:
        request = _valid_request(type="start_game")

        with self.assertRaises(MalformedRequestActionError):
            parse_request_action(request)

    def test_rejects_missing_request_id(self) -> None:
        request = _valid_request()
        del request["request_id"]

        with self.assertRaises(MalformedRequestActionError):
            parse_request_action(request)

    def test_rejects_missing_possible_actions(self) -> None:
        request = _valid_request()
        del request["possible_actions"]

        with self.assertRaises(MalformedRequestActionError):
            parse_request_action(request)

    def test_rejects_missing_observation(self) -> None:
        request = _valid_request()
        del request["observation"]

        with self.assertRaises(MalformedRequestActionError):
            parse_request_action(request)

    def test_rejects_boolean_request_id(self) -> None:
        # RiichiLab Protocol v2のrequest_idはinteger契約であり、boolはint
        # のサブクラスだが意味を持たないため明示的に除外する(lisjong Issue
        # #38 review: blocking 2)。
        request = _valid_request(request_id=True)

        with self.assertRaises(MalformedRequestActionError):
            parse_request_action(request)

    def test_rejects_string_request_id(self) -> None:
        # RiichiLab Protocol v2のrequest_idはgame内で一意なmonotonically
        # increasing integerであり、strは許容しない(lisjong Issue #38
        # review: blocking 2)。
        request = _valid_request(request_id="req-42")

        with self.assertRaises(MalformedRequestActionError):
            parse_request_action(request)

    def test_rejects_float_request_id(self) -> None:
        request = _valid_request(request_id=1.0)

        with self.assertRaises(MalformedRequestActionError):
            parse_request_action(request)

    def test_rejects_none_request_id(self) -> None:
        request = _valid_request(request_id=None)

        with self.assertRaises(MalformedRequestActionError):
            parse_request_action(request)

    def test_rejects_unsafe_request_id_type(self) -> None:
        request = _valid_request(request_id={"nested": 1})

        with self.assertRaises(MalformedRequestActionError):
            parse_request_action(request)

    def test_rejects_non_sequence_possible_actions(self) -> None:
        request = _valid_request(possible_actions={"type": "none", "actor": 0})

        with self.assertRaises(MalformedRequestActionError):
            parse_request_action(request)

    def test_rejects_string_possible_actions(self) -> None:
        request = _valid_request(possible_actions="none")

        with self.assertRaises(MalformedRequestActionError):
            parse_request_action(request)

    def test_rejects_non_string_observation(self) -> None:
        request = _valid_request(observation=12345)

        with self.assertRaises(MalformedRequestActionError):
            parse_request_action(request)

    def test_rejects_undecodable_observation(self) -> None:
        request = _valid_request(observation="not-valid-base64-observation!!")

        with self.assertRaises(ObservationDeserializeError):
            parse_request_action(request)


if __name__ == "__main__":
    unittest.main()
