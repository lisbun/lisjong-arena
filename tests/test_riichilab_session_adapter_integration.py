"""Arena-local `ValidationSession` / `RankedSession` -> real lisjong
`RiichiLabSeatAdapter` minimal wiring test(Issue #23)。

Issue #23の最新補足契約は、Adapter / Policy semantics(possible_actions
construction/normalization、Observation deserialize等)そのものをArena
testへ重複実装・再定義しないことを明示している。したがってここでは、
`start_game`を受けたArena-local Sessionが(fake stubへ差し替えず)実
`RiichiLabSeatAdapter`を正しくconsumerとしてbindできることだけを確認する
最小限のwiring coverageに留める。

`possible_actions`の構築・正規化、`process_request_action()`のrequest
round-trip、Policyへの非漏洩detail等のAdapter correctness coverageの正本は
引き続きlisjongにある。fake adapterによる単体lifecycle coverageは
`test_riichilab_session.py`が担当する。
"""

import unittest

from lisjong.policies import MinimalPolicy
from lisjong.policy_contract.seat import Seat
from lisjong.riichilab_adapter.adapter import RiichiLabSeatAdapter

from lisjong_arena.riichilab.session import RankedSession, ValidationSession


class ValidationSessionAdapterIntegrationTest(unittest.TestCase):
    def test_start_game_binds_the_real_riichilab_seat_adapter(self) -> None:
        session = ValidationSession(MinimalPolicy())
        session.handle_event({"type": "start_game", "id": 0})

        self.assertIsInstance(session._adapter, RiichiLabSeatAdapter)
        self.assertEqual(session._adapter.self_seat, Seat.SEAT_0)


class RankedSessionAdapterIntegrationTest(unittest.TestCase):
    def test_start_game_binds_the_real_riichilab_seat_adapter_for_every_seat(
        self,
    ) -> None:
        for seat_id in range(4):
            with self.subTest(seat_id=seat_id):
                session = RankedSession(MinimalPolicy())
                session.handle_event({"type": "start_game", "id": seat_id})

                self.assertIsInstance(session._adapter, RiichiLabSeatAdapter)
                self.assertEqual(session._adapter.self_seat, Seat(seat_id))


if __name__ == "__main__":
    unittest.main()
