"""Single RiichiEnv Action -> canonical InternalAction translation boundary."""

from lisjong.policy_contract.action import InternalAction
from lisjong.policy_contract.seat import Seat
from riichienv import Action as RiichiEnvAction
from riichienv import Observation

from lisjong_arena.riichienv.adapter.action_mapping import (
    UnsupportedActionError,
    _TRANSLATORS,
)


def translate_external_action(
    action: RiichiEnvAction, observation: Observation, self_seat: Seat
) -> InternalAction:
    """Translate one legal RiichiEnv action without creating a mapping lifecycle.

    This is the same canonical semantic translator family used by
    ``RiichiEnvActionMapping``.  It exists for offline replay/materialization
    callers that already own the exact decision observation and need to map one
    observed legal action into the internal action vocabulary.
    """
    if not isinstance(self_seat, Seat):
        raise TypeError("self_seat must be a Seat")
    translator = _TRANSLATORS.get(action.action_type)
    if translator is None:
        raise UnsupportedActionError(
            f"unsupported RiichiEnv action_type: {action.action_type!r}"
        )
    return translator(action, observation, self_seat)
