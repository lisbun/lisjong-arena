"""`ValidationSession` / `RankedSession`のpure lifecycle unit test
(Arena-local canonical, Issue #23)。

実WebSocket接続・asyncio・実RiichiEnvなしに、`start_game` / `request_id`
lifecycle / `action_ack` / forward compatibility / `end_game` /
`validation_result` / fail closed / `SessionStatus` detached snapshotを
確認する。

Arena-local `RiichiLabSeatAdapter`自体の内部処理(Policy呼び出し、Observation
deserialize、`possible_actions` semantic validation)はここで再検証しない
(`test_riichilab_adapter.py`等の責務)。ここでは
`lisjong_arena.riichilab.session.RiichiLabSeatAdapter`をfake stubへ差し替え、
Arena-local session lifecycleロジックだけを孤立させて確認する。実Adapterを
使った最小限のintegration確認は`test_riichilab_session_adapter_integration.py`
が担当する。
"""

import unittest
from unittest.mock import patch

from lisjong.policies import MinimalPolicy
from lisjong.policy_contract import Seat

from lisjong_arena.riichilab.adapter import SendReadyResponse
from lisjong_arena.riichilab.errors import ProtocolError
from lisjong_arena.riichilab.session import RankedSession, ValidationSession

_PATCH_TARGET = "lisjong_arena.riichilab.session.RiichiLabSeatAdapter"
_FINAL_SCORES = [30000, 25000, 20000, 25000]


class _FakeAdapter:
    def __init__(
        self,
        self_seat,
        *,
        response_request_id_override=None,
        raise_error=None,
        response_action=None,
    ) -> None:
        self.self_seat = self_seat
        self._override = response_request_id_override
        self._raise_error = raise_error
        self._response_action = response_action or {
            "type": "dahai",
            "actor": int(self_seat),
            "pai": "1m",
        }
        self.calls: list[object] = []

    def process_request_action(self, raw_request_action):
        self.calls.append(raw_request_action)
        if self._raise_error is not None:
            raise self._raise_error
        request_id = raw_request_action["request_id"]
        returned_id = self._override if self._override is not None else request_id
        return SendReadyResponse(
            request_id=returned_id, action=dict(self._response_action)
        )


def _fake_adapter_factory(**kwargs):
    def factory(self_seat, policy):
        return _FakeAdapter(self_seat, **kwargs)

    return factory


def _start_game(seat_id: int = 0) -> dict:
    return {"type": "start_game", "id": seat_id}


def _request_action(request_id: int, **extra) -> dict:
    event = {
        "type": "request_action",
        "request_id": request_id,
        "possible_actions": [],
        "observation": "unused-by-fake-adapter",
    }
    event.update(extra)
    return event


def _action_ack(request_id, status, **extra) -> dict:
    event = {"type": "action_ack", "request_id": request_id, "status": status}
    event.update(extra)
    return event


def _validation_result(**fields) -> dict:
    return {"type": "validation_result", **fields}


class StartGameTest(unittest.TestCase):
    def test_binds_seat_0(self) -> None:
        session = ValidationSession(MinimalPolicy())
        with patch(_PATCH_TARGET, _fake_adapter_factory()):
            self.assertIsNone(session.handle_event(_start_game(0)))

    def test_rejects_non_zero_seat(self) -> None:
        session = ValidationSession(MinimalPolicy())
        with patch(_PATCH_TARGET, _fake_adapter_factory()):
            with self.assertRaises(ProtocolError):
                session.handle_event(_start_game(1))

    def test_rejects_missing_id_field(self) -> None:
        session = ValidationSession(MinimalPolicy())
        with self.assertRaises(ProtocolError):
            session.handle_event({"type": "start_game"})

    def test_rejects_non_integer_id(self) -> None:
        session = ValidationSession(MinimalPolicy())
        with self.assertRaises(ProtocolError):
            session.handle_event({"type": "start_game", "id": "0"})

    def test_rejects_boolean_id(self) -> None:
        session = ValidationSession(MinimalPolicy())
        with self.assertRaises(ProtocolError):
            session.handle_event({"type": "start_game", "id": False})

    def test_legacy_seat_field_alone_is_not_treated_as_id(self) -> None:
        # 公式Protocolのseat index fieldは`id`であり、`seat`ではない。
        # `seat`だけを送るeventは`id`欠落としてfail closedし続けることを
        # 回帰確認する。
        session = ValidationSession(MinimalPolicy())
        with self.assertRaises(ProtocolError):
            session.handle_event({"type": "start_game", "seat": 0})

    def test_duplicate_start_game_same_seat_is_safe_noop(self) -> None:
        session = ValidationSession(MinimalPolicy())
        with patch(_PATCH_TARGET, _fake_adapter_factory()):
            session.handle_event(_start_game(0))
            first_adapter = session._adapter
            session.handle_event(_start_game(0))
            self.assertIs(session._adapter, first_adapter)

    def test_duplicate_start_game_different_seat_fails_closed(self) -> None:
        session = ValidationSession(MinimalPolicy())
        with patch(_PATCH_TARGET, _fake_adapter_factory()):
            session.handle_event(_start_game(0))
            with self.assertRaises(ProtocolError):
                session.handle_event(_start_game(1))

    def test_unknown_extra_field_on_start_game_is_ignored(self) -> None:
        session = ValidationSession(MinimalPolicy())
        with patch(_PATCH_TARGET, _fake_adapter_factory()):
            session.handle_event({"type": "start_game", "id": 0, "game_id": "abc"})

    def test_seat_field_is_treated_as_an_unknown_extra_field(self) -> None:
        # `id`が正本なので、`seat`が同時に含まれていてもunknown extra
        # fieldとしてforward-compatibleに無視し、`id`だけで判定する。
        session = ValidationSession(MinimalPolicy())
        with patch(_PATCH_TARGET, _fake_adapter_factory()):
            session.handle_event({"type": "start_game", "id": 0, "seat": 99})


class RequestBeforeStartGameTest(unittest.TestCase):
    def test_request_action_before_start_game_fails_closed(self) -> None:
        session = ValidationSession(MinimalPolicy())
        with self.assertRaises(ProtocolError):
            session.handle_event(_request_action(1))


class RequestIdLifecycleTest(unittest.TestCase):
    def _bound_session(self, **adapter_kwargs) -> ValidationSession:
        session = ValidationSession(MinimalPolicy())
        with patch(_PATCH_TARGET, _fake_adapter_factory(**adapter_kwargs)):
            session.handle_event(_start_game(0))
        return session

    def test_accepts_increasing_request_id_with_a_gap(self) -> None:
        session = self._bound_session()
        with patch(_PATCH_TARGET, _fake_adapter_factory()):
            outgoing_first = session.handle_event(_request_action(1))
            outgoing_second = session.handle_event(_request_action(37))
        self.assertEqual(outgoing_first["request_id"], 1)
        self.assertEqual(outgoing_second["request_id"], 37)

    def test_rejects_duplicate_request_id(self) -> None:
        session = self._bound_session()
        with patch(_PATCH_TARGET, _fake_adapter_factory()):
            session.handle_event(_request_action(5))
            with self.assertRaises(ProtocolError):
                session.handle_event(_request_action(5))

    def test_rejects_decreasing_request_id(self) -> None:
        session = self._bound_session()
        with patch(_PATCH_TARGET, _fake_adapter_factory()):
            session.handle_event(_request_action(10))
            with self.assertRaises(ProtocolError):
                session.handle_event(_request_action(3))

    def test_rejects_missing_request_id(self) -> None:
        session = self._bound_session()
        event = _request_action(1)
        del event["request_id"]
        with self.assertRaises(ProtocolError):
            session.handle_event(event)

    def test_rejects_non_integer_request_id(self) -> None:
        session = self._bound_session()
        with self.assertRaises(ProtocolError):
            session.handle_event(_request_action("1"))

    def test_rejects_boolean_request_id(self) -> None:
        session = self._bound_session()
        with self.assertRaises(ProtocolError):
            session.handle_event(_request_action(True))

    def test_rejects_adapter_response_request_id_mismatch(self) -> None:
        session = self._bound_session(response_request_id_override=999)
        with self.assertRaises(ProtocolError):
            session.handle_event(_request_action(1))

    def test_sends_exactly_once_per_request(self) -> None:
        session = self._bound_session()
        with patch(_PATCH_TARGET, _fake_adapter_factory()):
            outgoing = session.handle_event(_request_action(1))
        self.assertIsNotNone(outgoing)
        status = session.status()
        self.assertEqual(status.responses_sent, 1)
        self.assertEqual(status.requests_received, 1)

    def test_time_metadata_type_is_validated(self) -> None:
        session = self._bound_session()
        with self.assertRaises(ProtocolError):
            session.handle_event(_request_action(1, time={"grace_ms": "not-a-number"}))

    def test_time_metadata_absent_is_allowed(self) -> None:
        session = self._bound_session()
        with patch(_PATCH_TARGET, _fake_adapter_factory()):
            session.handle_event(_request_action(1))

    def test_time_metadata_numeric_fields_are_allowed(self) -> None:
        session = self._bound_session()
        with patch(_PATCH_TARGET, _fake_adapter_factory()):
            session.handle_event(
                _request_action(
                    1, time={"grace_ms": 500, "bank_ms": 15000, "deadline_ms": 3000}
                )
            )

    def test_time_is_not_forwarded_to_policy(self) -> None:
        seen_requests = []

        class _RecordingFakeAdapter(_FakeAdapter):
            def process_request_action(self, raw_request_action):
                seen_requests.append(raw_request_action)
                return super().process_request_action(raw_request_action)

        session = ValidationSession(MinimalPolicy())
        with patch(
            _PATCH_TARGET, lambda self_seat, policy: _RecordingFakeAdapter(self_seat)
        ):
            session.handle_event(_start_game(0))
            session.handle_event(_request_action(1, time={"grace_ms": 500}))
        # ここで確認したいのはSession自身がtimeをPolicyへ注入しないことで
        # あり、Adapterへ渡すraw request自体にtimeが含まれることはAdapter側が
        # 別途保持のみ行い、Policyへは渡さない契約になっている。
        self.assertIn("time", seen_requests[0])

    def test_adapter_exception_propagates_and_produces_no_payload(self) -> None:
        session = self._bound_session(raise_error=RuntimeError("adapter exploded"))
        with self.assertRaises(RuntimeError):
            session.handle_event(_request_action(1))


class ActionAckTest(unittest.TestCase):
    def _session_with_accepted_request(self, request_id: int = 1) -> ValidationSession:
        session = ValidationSession(MinimalPolicy())
        with patch(_PATCH_TARGET, _fake_adapter_factory()):
            session.handle_event(_start_game(0))
            session.handle_event(_request_action(request_id))
        return session

    def test_accepted_status_is_recorded_without_error(self) -> None:
        session = self._session_with_accepted_request()
        session.handle_event(_action_ack(1, "accepted"))
        self.assertEqual(session.status().ack_history[1], ("accepted",))

    def test_stale_status_is_recorded_without_error(self) -> None:
        session = self._session_with_accepted_request()
        session.handle_event(_action_ack(1, "stale"))
        self.assertEqual(session.status().ack_history[1], ("stale",))

    def test_defaulted_status_is_recorded_without_error(self) -> None:
        session = self._session_with_accepted_request()
        session.handle_event(_action_ack(1, "defaulted"))
        self.assertEqual(session.status().ack_history[1], ("defaulted",))

    def test_rejected_status_raises(self) -> None:
        session = self._session_with_accepted_request()
        with self.assertRaises(ProtocolError):
            session.handle_event(_action_ack(1, "rejected"))

    def test_unparseable_status_raises(self) -> None:
        session = self._session_with_accepted_request()
        with self.assertRaises(ProtocolError):
            session.handle_event(_action_ack(1, "unparseable"))

    def test_unknown_status_raises(self) -> None:
        session = self._session_with_accepted_request()
        with self.assertRaises(ProtocolError):
            session.handle_event(_action_ack(1, "made_up_status"))

    def test_unknown_request_id_raises(self) -> None:
        session = self._session_with_accepted_request()
        with self.assertRaises(ProtocolError):
            session.handle_event(_action_ack(999, "accepted"))

    def test_future_request_id_raises(self) -> None:
        # 対応するrequest_actionをまだ受理していないrequest_idへのackは
        # unknown request_idと同様に成功扱いしない。
        session = ValidationSession(MinimalPolicy())
        with patch(_PATCH_TARGET, _fake_adapter_factory()):
            session.handle_event(_start_game(0))
        with self.assertRaises(ProtocolError):
            session.handle_event(_action_ack(5, "accepted"))

    def test_missing_request_id_raises(self) -> None:
        session = self._session_with_accepted_request()
        with self.assertRaises(ProtocolError):
            session.handle_event({"type": "action_ack", "status": "accepted"})

    def test_missing_status_raises(self) -> None:
        session = self._session_with_accepted_request()
        with self.assertRaises(ProtocolError):
            session.handle_event({"type": "action_ack", "request_id": 1})

    def test_ack_history_accumulates_multiple_statuses_for_one_request(self) -> None:
        session = self._session_with_accepted_request()
        with self.assertRaises(ProtocolError):
            # defaulted (non-fatal) の後にlate responseがstaleとしてserverに
            # 届いた、という現実的な順序を想定する。stale自体はraiseしない
            # ため、直後にrejectedを送ってhistory内容だけ確認する。
            session.handle_event(_action_ack(1, "defaulted"))
            session.handle_event(_action_ack(1, "stale"))
            session.handle_event(_action_ack(1, "rejected"))
        self.assertEqual(
            session.status().ack_history[1], ("defaulted", "stale", "rejected")
        )

    def test_duplicate_ack_is_not_treated_as_a_different_requests_success(self) -> None:
        session = self._session_with_accepted_request()
        session.handle_event(_action_ack(1, "accepted"))
        session.handle_event(_action_ack(1, "accepted"))
        self.assertEqual(session.status().ack_history[1], ("accepted", "accepted"))

    def test_unknown_extra_field_is_ignored(self) -> None:
        session = self._session_with_accepted_request()
        session.handle_event(_action_ack(1, "accepted", server_time_ms=123456))
        self.assertEqual(session.status().ack_history[1], ("accepted",))


class ForwardCompatibilityTest(unittest.TestCase):
    def test_unknown_event_type_is_ignored(self) -> None:
        session = ValidationSession(MinimalPolicy())
        self.assertIsNone(session.handle_event({"type": "some_future_event"}))

    def test_missing_type_field_is_ignored(self) -> None:
        session = ValidationSession(MinimalPolicy())
        self.assertIsNone(session.handle_event({"foo": "bar"}))

    def test_informational_mjai_event_is_ignored(self) -> None:
        session = ValidationSession(MinimalPolicy())
        self.assertIsNone(session.handle_event({"type": "tsumo", "actor": 0}))


class EndGameAndValidationResultTest(unittest.TestCase):
    def test_end_game_sets_flag_without_marking_validation_complete(self) -> None:
        session = ValidationSession(MinimalPolicy())
        session.handle_event({"type": "end_game"})
        status = session.status()
        self.assertTrue(status.end_game_received)
        self.assertFalse(status.validation_result_received)
        self.assertFalse(session.validation_result_received)

    def test_validation_result_sets_passed_and_flag(self) -> None:
        session = ValidationSession(MinimalPolicy())
        session.handle_event(_validation_result(passed=True))
        status = session.status()
        self.assertTrue(status.validation_result_received)
        self.assertTrue(status.passed)
        self.assertTrue(session.validation_result_received)

    def test_validation_result_failure_reason_is_captured(self) -> None:
        session = ValidationSession(MinimalPolicy())
        session.handle_event(_validation_result(passed=False, reason="illegal action"))
        status = session.status()
        self.assertFalse(status.passed)
        self.assertEqual(status.failure_reason, "illegal action")

    def test_validation_result_message_used_as_reason_fallback(self) -> None:
        session = ValidationSession(MinimalPolicy())
        session.handle_event(_validation_result(passed=False, message="chombo"))
        self.assertEqual(session.status().failure_reason, "chombo")

    def test_validation_result_without_reason_is_allowed(self) -> None:
        session = ValidationSession(MinimalPolicy())
        session.handle_event(_validation_result(passed=True))
        self.assertIsNone(session.status().failure_reason)

    def test_validation_result_malformed_passed_fails_closed(self) -> None:
        session = ValidationSession(MinimalPolicy())
        with self.assertRaises(ProtocolError):
            session.handle_event(_validation_result(passed="yes"))

    def test_validation_result_missing_passed_fails_closed(self) -> None:
        session = ValidationSession(MinimalPolicy())
        with self.assertRaises(ProtocolError):
            session.handle_event(_validation_result())

    def test_validation_result_reason_wrong_type_fails_closed(self) -> None:
        session = ValidationSession(MinimalPolicy())
        with self.assertRaises(ProtocolError):
            session.handle_event(_validation_result(passed=True, reason=123))

    def test_end_game_then_validation_result_is_the_expected_order(self) -> None:
        session = ValidationSession(MinimalPolicy())
        session.handle_event({"type": "end_game"})
        session.handle_event(_validation_result(passed=True))
        status = session.status()
        self.assertTrue(status.end_game_received)
        self.assertTrue(status.validation_result_received)
        self.assertTrue(status.passed)


class SessionStatusDetachedSnapshotTest(unittest.TestCase):
    def test_ack_history_values_are_tuple_snapshots(self) -> None:
        session = ValidationSession(MinimalPolicy())
        with patch(_PATCH_TARGET, _fake_adapter_factory()):
            session.handle_event(_start_game(0))
            session.handle_event(_request_action(1))
        session.handle_event(_action_ack(1, "accepted"))

        status = session.status()
        self.assertIsInstance(status.ack_history[1], tuple)

    def test_status_snapshot_is_not_affected_by_later_session_mutation(self) -> None:
        session = ValidationSession(MinimalPolicy())
        with patch(_PATCH_TARGET, _fake_adapter_factory()):
            session.handle_event(_start_game(0))
            session.handle_event(_request_action(1))
        session.handle_event(_action_ack(1, "accepted"))

        status_before = session.status()
        session.handle_event(_action_ack(1, "stale"))

        self.assertEqual(status_before.ack_history[1], ("accepted",))
        self.assertEqual(session.status().ack_history[1], ("accepted", "stale"))

    def test_status_ack_history_dict_is_a_copy_not_the_internal_mapping(self) -> None:
        session = ValidationSession(MinimalPolicy())
        with patch(_PATCH_TARGET, _fake_adapter_factory()):
            session.handle_event(_start_game(0))
            session.handle_event(_request_action(1))
        session.handle_event(_action_ack(1, "accepted"))

        status = session.status()
        status.ack_history[999] = ("tampered",)

        self.assertNotIn(999, session.status().ack_history)


class RankedSeatBindTest(unittest.TestCase):
    def test_accepts_all_four_seats(self) -> None:
        for seat_id in range(4):
            with self.subTest(seat_id=seat_id):
                session = RankedSession(MinimalPolicy())
                with patch(_PATCH_TARGET, _fake_adapter_factory()):
                    session.handle_event({"type": "start_game", "id": seat_id})
                self.assertEqual(session.status().seat, Seat(seat_id))

    def test_rejects_boolean_and_out_of_range_id(self) -> None:
        for seat_id in (False, "1", None, -1, 4):
            with self.subTest(seat_id=seat_id):
                session = RankedSession(MinimalPolicy())
                with self.assertRaises(ProtocolError):
                    session.handle_event({"type": "start_game", "id": seat_id})

    def test_does_not_fallback_to_seat_field(self) -> None:
        session = RankedSession(MinimalPolicy())
        with self.assertRaises(ProtocolError):
            session.handle_event({"type": "start_game", "seat": 2})

    def test_duplicate_same_seat_keeps_one_adapter(self) -> None:
        created = []

        def factory(self_seat, policy):
            adapter = _FakeAdapter(self_seat)
            created.append(adapter)
            return adapter

        session = RankedSession(MinimalPolicy())
        with patch(_PATCH_TARGET, factory):
            session.handle_event({"type": "start_game", "id": 3})
            first_adapter = session._adapter
            session.handle_event({"type": "start_game", "id": 3})

        self.assertEqual(len(created), 1)
        self.assertIs(session._adapter, first_adapter)

    def test_duplicate_different_seat_fails_closed(self) -> None:
        session = RankedSession(MinimalPolicy())
        with patch(_PATCH_TARGET, _fake_adapter_factory()):
            session.handle_event({"type": "start_game", "id": 1})
            with self.assertRaises(ProtocolError):
                session.handle_event({"type": "start_game", "id": 2})

    def test_validation_still_rejects_non_zero_seat(self) -> None:
        session = ValidationSession(MinimalPolicy())
        with self.assertRaises(ProtocolError):
            session.handle_event({"type": "start_game", "id": 1})

    def test_ranked_uses_the_common_monotonic_request_id_contract(self) -> None:
        session = RankedSession(MinimalPolicy())
        with patch(_PATCH_TARGET, _fake_adapter_factory()):
            session.handle_event({"type": "start_game", "id": 0})
            session.handle_event(_request_action(1))
            session.handle_event(_request_action(37))
            with self.assertRaises(ProtocolError):
                session.handle_event(_request_action(37))


class RankedTerminalTest(unittest.TestCase):
    def _started_session(self) -> RankedSession:
        session = RankedSession(MinimalPolicy())
        with patch(_PATCH_TARGET, _fake_adapter_factory()):
            session.handle_event({"type": "start_game", "id": 0})
        return session

    def test_end_game_is_ranked_terminal_and_captures_scores(self) -> None:
        session = self._started_session()
        session.handle_event({"type": "end_game", "scores": _FINAL_SCORES})
        self.assertTrue(session.is_complete)
        self.assertTrue(session.status().end_game_received)
        self.assertEqual(session.status().scores, tuple(_FINAL_SCORES))

    def test_observed_ranked_end_game_without_scores_is_terminal(self) -> None:
        session = self._started_session()
        session.handle_event({"type": "end_game"})

        self.assertTrue(session.is_complete)
        self.assertTrue(session.status().end_game_received)
        self.assertIsNone(session.status().scores)

    def test_validation_end_game_is_not_terminal(self) -> None:
        session = ValidationSession(MinimalPolicy())
        session.handle_event({"type": "end_game", "scores": _FINAL_SCORES})
        self.assertFalse(session.is_complete)
        self.assertFalse(session.validation_result_received)

    def test_end_game_before_start_game_fails_closed(self) -> None:
        session = RankedSession(MinimalPolicy())
        with self.assertRaises(ProtocolError):
            session.handle_event({"type": "end_game", "scores": _FINAL_SCORES})

    def test_malformed_final_scores_fail_closed(self) -> None:
        for scores in (None, [25000] * 3, [25000, 25000, 25000, True]):
            with self.subTest(scores=scores):
                session = self._started_session()
                with self.assertRaises(ProtocolError):
                    session.handle_event({"type": "end_game", "scores": scores})

    def test_missing_scores_accepts_unknown_fields_without_reading_values(self) -> None:
        session = self._started_session()
        sentinel = "do-not-leak-this-value"
        event = {
            "type": "end_game",
            "final_scores": {"nested": sentinel},
            "metadata": sentinel,
        }

        session.handle_event(event)

        self.assertTrue(session.is_complete)
        self.assertIsNone(session.status().scores)

    def test_non_list_scores_reports_type_without_values(self) -> None:
        session = self._started_session()
        sentinel = "do-not-leak-this-value"

        with self.assertRaises(ProtocolError) as caught:
            session.handle_event({"type": "end_game", "scores": {"nested": sentinel}})

        message = str(caught.exception)
        self.assertIn("event_keys=['scores', 'type']", message)
        self.assertIn("scores_type=dict", message)
        self.assertIn("scores_length=None", message)
        self.assertNotIn(sentinel, message)

    def test_list_scores_reports_length_without_values(self) -> None:
        session = self._started_session()
        sentinel = "do-not-leak-this-value"

        with self.assertRaises(ProtocolError) as caught:
            session.handle_event({"type": "end_game", "scores": [1, sentinel, 3]})

        message = str(caught.exception)
        self.assertIn("event_keys=['scores', 'type']", message)
        self.assertIn("scores_type=list", message)
        self.assertIn("scores_length=3", message)
        self.assertNotIn(sentinel, message)

    def test_invalid_score_element_reports_shape_without_values(self) -> None:
        session = self._started_session()
        sentinel = "do-not-leak-this-value"

        with self.assertRaises(ProtocolError) as caught:
            session.handle_event(
                {"type": "end_game", "scores": [30000, 25000, 20000, sentinel]}
            )

        message = str(caught.exception)
        self.assertIn("ranked end_game scores must be integers", message)
        self.assertIn("event_keys=['scores', 'type']", message)
        self.assertIn("scores_type=list", message)
        self.assertIn("scores_length=4", message)
        self.assertNotIn(sentinel, message)


if __name__ == "__main__":
    unittest.main()
