"""Arena Session -> real lisjong Adapter -> Policy compatibility test(Issue #23)。

Adapter / Policy semanticsのcorrectnessはlisjongが所有するため、このtestは
`possible_actions`を生成・正規化しない。pin済みlisjong / RiichiEnv 0.4.8で
取得したknown-validな単一`request_action` fixtureを使い、Arena-local Sessionが
実`RiichiLabSeatAdapter`を介してPolicyまで接続できることだけを確認する。

fake AdapterによるSession lifecycleの詳細なcoverageは
`test_riichilab_session.py`が担当する。
"""

import copy
import unittest

from lisjong.policies import MinimalPolicy
from lisjong.riichilab_adapter.adapter import RiichiLabSeatAdapter

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

    def choose_action(self, decision):
        self.calls += 1
        return MinimalPolicy().choose_action(decision)


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


if __name__ == "__main__":
    unittest.main()
