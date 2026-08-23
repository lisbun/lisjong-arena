"""Arena Session -> Arena-local RiichiLabSeatAdapter -> lisjong Policy compatibility test(Issue #23、Issue #27)。

Adapter / Policy semanticsのcorrectnessはlisjongが所有するため、このtestは
`possible_actions`を生成・正規化しない。pin済みlisjong / RiichiEnv 0.4.8で
取得したknown-validな単一`request_action` fixtureを使い、Arena-local Sessionが
Arena-local `RiichiLabSeatAdapter`(Issue #27でlisjongからphysical
migrationしたcanonical implementation)を介して実lisjong Policyまで
接続できることを確認する、cross-boundary integrationの本線。

fake AdapterによるSession lifecycleの詳細なcoverageは
`test_riichilab_session.py`が担当する。Adapter compositionのfail closed
regression matrix(build_decision failure、Policy例外、
PolicyActionValidationError、MJAI conversion failure、possible_actions
mismatch等)の詳細は`test_riichilab_adapter.py`が担当する。ここでは、それら
がSession経由でwrap/fallbackされずそのまま伝播することだけを確認する。
"""

import copy
import unittest
from unittest.mock import patch

from lisjong.policies import MinimalPolicy
from lisjong.policy_contract import PassAction, PolicyActionValidationError
from lisjong.riichienv_adapter import AdapterSyncError

from lisjong_arena.riichilab.adapter import RiichiLabSeatAdapter
from lisjong_arena.riichilab.adapter_errors import (
    PossibleActionsValidationError,
    ProtocolConversionError,
    RiichiLabAdapterError,
    SeatMismatchError,
)
from lisjong_arena.riichilab.errors import ProtocolError, RiichiLabClientError
from lisjong_arena.riichilab.session import RankedSession, ValidationSession

_KNOWN_VALID_OBSERVATION = (
    "eyJwbGF5ZXJfaWQiOjAsImhhbmRzIjpbWzE2LDE4LDI5LDM3LDM4LDUxLDU3LDgxLDgzLDEwOCwxMjAsMTI0LDEz"
    "Myw5M10sW10sW10sW11dLCJtZWxkcyI6W1tdLFtdLFtdLFtdXSwiZGlzY2FyZHMiOltbXSxbXSxbXSxbXV0sImRv"
    "cmFfaW5kaWNhdG9ycyI6WzM1XSwic2NvcmVzIjpbMjUwMDAsMjUwMDAsMjUwMDAsMjUwMDBdLCJyaWljaGlfZGVj"
    "bGFyZWQiOltmYWxzZSxmYWxzZSxmYWxzZSxmYWxzZV0sIl9sZWdhbF9hY3Rpb25zIjpbeyJhY3Rpb25fdHlwZSI6"
    "IkRpc2NhcmQiLCJ0aWxlIjoxNiwiY29uc3VtZV90aWxlcyI6W10sImFjdG9yIjowfSx7ImFjdGlvbl90eXBlIjoi"
    "RGlzY2FyZCIsInRpbGUiOjE4LCJjb25zdW1lX3RpbGVzIjpbXSwiYWN0b3IiOjB9LHsiYWN0aW9uX3R5cGUiOiJE"
    "aXNjYXJkIiwidGlsZSI6MjksImNvbnN1bWVfdGlsZXMiOltdLCJhY3RvciI6MH0seyJhY3Rpb25fdHlwZSI6IkRp"
    "c2NhcmQiLCJ0aWxlIjozNywiY29uc3VtZV90aWxlcyI6W10sImFjdG9yIjowfSx7ImFjdGlvbl90eXBlIjoiRGlz"
    "Y2FyZCIsInRpbGUiOjM4LCJjb25zdW1lX3RpbGVzIjpbXSwiYWN0b3IiOjB9LHsiYWN0aW9uX3R5cGUiOiJEaXNj"
    "YXJkIiwidGlsZSI6NTEsImNvbnN1bWVfdGlsZXMiOltdLCJhY3RvciI6MH0seyJhY3Rpb25fdHlwZSI6IkRpc2Nh"
    "cmQiLCJ0aWxlIjo1NywiY29uc3VtZV90aWxlcyI6W10sImFjdG9yIjowfSx7ImFjdGlvbl90eXBlIjoiRGlzY2Fy"
    "ZCIsInRpbGUiOjgxLCJjb25zdW1lX3RpbGVzIjpbXSwiYWN0b3IiOjB9LHsiYWN0aW9uX3R5cGUiOiJEaXNjYXJk"
    "IiwidGlsZSI6ODMsImNvbnN1bWVfdGlsZXMiOltdLCJhY3RvciI6MH0seyJhY3Rpb25fdHlwZSI6IkRpc2NhcmQi"
    "LCJ0aWxlIjoxMDgsImNvbnN1bWVfdGlsZXMiOltdLCJhY3RvciI6MH0seyJhY3Rpb25fdHlwZSI6IkRpc2NhcmQi"
    "LCJ0aWxlIjoxMjAsImNvbnN1bWVfdGlsZXMiOltdLCJhY3RvciI6MH0seyJhY3Rpb25fdHlwZSI6IkRpc2NhcmQi"
    "LCJ0aWxlIjoxMjQsImNvbnN1bWVfdGlsZXMiOltdLCJhY3RvciI6MH0seyJhY3Rpb25fdHlwZSI6IkRpc2NhcmQi"
    "LCJ0aWxlIjoxMzMsImNvbnN1bWVfdGlsZXMiOltdLCJhY3RvciI6MH0seyJhY3Rpb25fdHlwZSI6IkRpc2NhcmQi"
    "LCJ0aWxlIjo5MywiY29uc3VtZV90aWxlcyI6W10sImFjdG9yIjowfV0sImV2ZW50cyI6WyJ7XCJ0eXBlXCI6XCJz"
    "dGFydF9nYW1lXCJ9Iiwie1wiYmFrYXplXCI6XCJFXCIsXCJkb3JhX21hcmtlclwiOlwiOW1cIixcImhvbmJhXCI6"
    "MCxcImt5b2t1XCI6MSxcImt5b3Rha3VcIjowLFwib3lhXCI6MCxcInNjb3Jlc1wiOlsyNTAwMCwyNTAwMCwyNTAw"
    "MCwyNTAwMF0sXCJ0ZWhhaXNcIjpbW1wiNW1yXCIsXCI1bVwiLFwiOG1cIixcIjFwXCIsXCIxcFwiLFwiNHBcIixc"
    "IjZwXCIsXCIzc1wiLFwiM3NcIixcIkVcIixcIk5cIixcIlBcIixcIkNcIl0sW1wiP1wiLFwiP1wiLFwiP1wiLFwi"
    "P1wiLFwiP1wiLFwiP1wiLFwiP1wiLFwiP1wiLFwiP1wiLFwiP1wiLFwiP1wiLFwiP1wiLFwiP1wiXSxbXCI/XCIs"
    "XCI/XCIsXCI/XCIsXCI/XCIsXCI/XCIsXCI/XCIsXCI/XCIsXCI/XCIsXCI/XCIsXCI/XCIsXCI/XCIsXCI/XCIs"
    "XCI/XCJdLFtcIj9cIixcIj9cIixcIj9cIixcIj9cIixcIj9cIixcIj9cIixcIj9cIixcIj9cIixcIj9cIixcIj9c"
    "IixcIj9cIixcIj9cIixcIj9cIl1dLFwidHlwZVwiOlwic3RhcnRfa3lva3VcIn0iLCJ7XCJhY3RvclwiOjAsXCJw"
    "YWlcIjpcIjZzXCIsXCJ0eXBlXCI6XCJ0c3Vtb1wifSJdLCJob25iYSI6MCwicmlpY2hpX3N0aWNrcyI6MCwicm91"
    "bmRfd2luZCI6MCwib3lhIjowLCJreW9rdV9pbmRleCI6MCwid2FpdHMiOltdLCJpc190ZW5wYWkiOmZhbHNlLCJ0"
    "c3Vtb2dpcmlfZmxhZ3MiOltbXSxbXSxbXSxbXV0sInJpaWNoaV9zdXRlaGFpcyI6W251bGwsbnVsbCxudWxsLG51"
    "bGxdLCJsYXN0X3RlZGFzaGlzIjpbbnVsbCxudWxsLG51bGwsbnVsbF0sImxhc3RfZGlzY2FyZCI6bnVsbCwiZHJh"
    "d25fdGlsZSI6OTN9"
)

_KNOWN_VALID_REQUEST_ACTION = {
    "type": "request_action",
    "request_id": 1,
    "possible_actions": [
        {"type": "dahai", "pai": "5mr"},
        {"type": "dahai", "pai": "5m"},
        {"type": "dahai", "pai": "8m"},
        {"type": "dahai", "pai": "1p"},
        {"type": "dahai", "pai": "4p"},
        {"type": "dahai", "pai": "6p"},
        {"type": "dahai", "pai": "3s"},
        {"type": "dahai", "pai": "E"},
        {"type": "dahai", "pai": "N"},
        {"type": "dahai", "pai": "P"},
        {"type": "dahai", "pai": "C"},
        {"type": "dahai", "pai": "6s"},
    ],
    "observation": _KNOWN_VALID_OBSERVATION,
}


class _RecordingPolicy:
    def __init__(self) -> None:
        self.calls = 0
        self.decisions = []

    def choose_action(self, decision):
        self.calls += 1
        self.decisions.append(decision)
        return MinimalPolicy().choose_action(decision)


class _RaisingPolicy:
    def choose_action(self, decision):
        raise RuntimeError("policy exploded")


class _IllegalActionPolicy:
    def choose_action(self, decision):
        return PassAction(actor=decision.input.self_seat)


class SessionAdapterIntegrationTest(unittest.TestCase):
    def _assert_session_connects_real_adapter_to_policy(self, session_type) -> None:
        policy = _RecordingPolicy()
        session = session_type(policy)
        session.handle_event({"type": "start_game", "id": 0})

        self.assertIsInstance(session._adapter, RiichiLabSeatAdapter)
        outgoing = session.handle_event(copy.deepcopy(_KNOWN_VALID_REQUEST_ACTION))

        self.assertIsNotNone(outgoing)
        self.assertEqual(outgoing["request_id"], 1)
        self.assertEqual(policy.calls, 1)

    def test_validation_session_reaches_real_adapter_and_policy(self) -> None:
        self._assert_session_connects_real_adapter_to_policy(ValidationSession)

    def test_ranked_session_reaches_real_adapter_and_policy(self) -> None:
        self._assert_session_connects_real_adapter_to_policy(RankedSession)

    def test_request_id_time_and_possible_actions_do_not_reach_the_policy(
        self,
    ) -> None:
        policy = _RecordingPolicy()
        session = RankedSession(policy)
        session.handle_event({"type": "start_game", "id": 0})

        request = copy.deepcopy(_KNOWN_VALID_REQUEST_ACTION)
        request["time"] = {"grace_ms": 500}
        session.handle_event(request)

        self.assertEqual(len(policy.decisions), 1)
        decision = policy.decisions[0]
        self.assertTrue(hasattr(decision, "input"))
        self.assertTrue(hasattr(decision, "legal_actions"))
        self.assertFalse(hasattr(decision, "request_id"))
        self.assertFalse(hasattr(decision, "time"))
        self.assertFalse(hasattr(decision, "possible_actions"))

    def test_bridge_reuses_the_same_tracker_and_mapping_session_across_requests(
        self,
    ) -> None:
        session = RankedSession(_RecordingPolicy())
        session.handle_event({"type": "start_game", "id": 0})

        adapter = session._adapter
        tracker = adapter._tracker
        mapping_session = adapter._mapping_session

        session.handle_event(copy.deepcopy(_KNOWN_VALID_REQUEST_ACTION))

        self.assertIs(session._adapter._tracker, tracker)
        self.assertIs(session._adapter._mapping_session, mapping_session)

    def test_cross_seat_observation_rejection_propagates_unwrapped(self) -> None:
        # Observationのplayer_idはfixtureで0固定なので、seat 1へbindした
        # Sessionへ渡すとbound seatとの不一致になる。SeatMismatchErrorは
        # Arena `ProtocolError`(RiichiLabClientError系)へwrapされず、
        # Adapter-specific errorとしてそのまま伝播する。
        session = RankedSession(_RecordingPolicy())
        session.handle_event({"type": "start_game", "id": 1})

        with self.assertRaises(SeatMismatchError):
            session.handle_event(copy.deepcopy(_KNOWN_VALID_REQUEST_ACTION))

    def test_build_decision_failure_propagates_unwrapped_through_session(
        self,
    ) -> None:
        session = RankedSession(_RecordingPolicy())
        session.handle_event({"type": "start_game", "id": 0})

        with patch(
            "lisjong_arena.riichilab.adapter.build_decision",
            side_effect=AdapterSyncError("boom"),
        ):
            with self.assertRaises(AdapterSyncError):
                session.handle_event(copy.deepcopy(_KNOWN_VALID_REQUEST_ACTION))

    def test_policy_exception_propagates_unwrapped_through_session(self) -> None:
        session = RankedSession(_RaisingPolicy())
        session.handle_event({"type": "start_game", "id": 0})

        with self.assertRaises(RuntimeError):
            session.handle_event(copy.deepcopy(_KNOWN_VALID_REQUEST_ACTION))

    def test_policy_action_validation_error_propagates_unwrapped_through_session(
        self,
    ) -> None:
        session = RankedSession(_IllegalActionPolicy())
        session.handle_event({"type": "start_game", "id": 0})

        with self.assertRaises(PolicyActionValidationError):
            session.handle_event(copy.deepcopy(_KNOWN_VALID_REQUEST_ACTION))

    def test_mjai_conversion_failure_produces_no_payload_through_session(self) -> None:
        session = RankedSession(_RecordingPolicy())
        session.handle_event({"type": "start_game", "id": 0})

        with patch(
            "lisjong_arena.riichilab.adapter.build_mjai_response",
            side_effect=ProtocolConversionError("boom"),
        ):
            with self.assertRaises(ProtocolConversionError):
                session.handle_event(copy.deepcopy(_KNOWN_VALID_REQUEST_ACTION))

    def test_possible_actions_mismatch_produces_no_payload_through_session(
        self,
    ) -> None:
        session = RankedSession(_RecordingPolicy())
        session.handle_event({"type": "start_game", "id": 0})

        request = copy.deepcopy(_KNOWN_VALID_REQUEST_ACTION)
        request["possible_actions"] = [{"type": "ryukyoku"}]

        with self.assertRaises(PossibleActionsValidationError):
            session.handle_event(request)

    def test_adapter_error_raised_through_session_is_not_a_riichilab_client_error(
        self,
    ) -> None:
        session = RankedSession(_RecordingPolicy())
        session.handle_event({"type": "start_game", "id": 0})

        request = copy.deepcopy(_KNOWN_VALID_REQUEST_ACTION)
        request["possible_actions"] = [{"type": "ryukyoku"}]

        with self.assertRaises(RiichiLabAdapterError) as context:
            session.handle_event(request)

        self.assertNotIsInstance(context.exception, RiichiLabClientError)
        self.assertNotIsInstance(context.exception, ProtocolError)


if __name__ == "__main__":
    unittest.main()
