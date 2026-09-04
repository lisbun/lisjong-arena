"""Ordinary-discard-only learned activation predicate (Issue #140).

macro-transition dataset構築（`transitions.py`）とserving hybrid
（`serving.py`）の両方が同じeligibility判定を使う。BC hybrid / Q hybridは
完全に同じactivation / fallback semanticsを持たなければならないため、判定
そのものを1箇所へ集約する。
"""

from collections.abc import Sequence

from lisjong.policy_contract.action import DiscardAction, InternalAction

from .protocol import MINIMUM_CHOICE_LEGAL_ACTION_COUNT


def is_eligible_ordinary_discard_choice(
    legal_actions: Sequence[InternalAction],
) -> bool:
    """全legal_actionsがDiscardActionかつchoice decision (>=2) の場合だけTrue。

    forced decision（`< 2`）、riichi / call / kan / agari / abortive draw等の
    非discard actionを1件でも含むdecisionはFalseになる。
    """
    if len(legal_actions) < MINIMUM_CHOICE_LEGAL_ACTION_COUNT:
        return False
    return all(isinstance(action, DiscardAction) for action in legal_actions)


__all__ = ["is_eligible_ordinary_discard_choice"]
