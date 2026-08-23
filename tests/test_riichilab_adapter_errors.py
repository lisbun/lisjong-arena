"""Arena-local RiichiLab protocol-facing decision bridge error hierarchy test(Issue #27)。

`lisjong_arena.riichilab.adapter_errors`のcanonical hierarchyを確認する。

migration後も`RiichiLabAdapterError`は既存Arena lower-level client error
hierarchy(`lisjong_arena.riichilab.errors.RiichiLabClientError`)へ
reparentされない。これはbehavior-preserving migrationのための意図的な分離
であり、現行ranked / validation CLIが`RiichiLabClientError`だけをcatchする
範囲を変更してはならないためである(Arena Issue #27)。
"""

import unittest

from lisjong_arena.riichilab.adapter_errors import (
    MalformedRequestActionError,
    ObservationDeserializeError,
    PossibleActionsValidationError,
    ProtocolConversionError,
    RiichiLabAdapterError,
    SeatMismatchError,
)
from lisjong_arena.riichilab.errors import RiichiLabClientError


class AdapterErrorHierarchyTest(unittest.TestCase):
    def test_riichilab_adapter_error_is_a_direct_exception_subclass(self) -> None:
        self.assertTrue(issubclass(RiichiLabAdapterError, Exception))
        self.assertIn(Exception, RiichiLabAdapterError.__bases__)

    def test_riichilab_adapter_error_is_not_a_riichilab_client_error(self) -> None:
        # 現行ranked / validation CLIは`RiichiLabClientError`だけをcatchする。
        # ここでreparentすると、Adapter / Policy boundary failureのuser-
        # facing error behaviorが意図せず変わってしまう(Arena Issue #27)。
        self.assertFalse(issubclass(RiichiLabAdapterError, RiichiLabClientError))

    def test_leaf_errors_are_riichilab_adapter_errors(self) -> None:
        for error_class in (
            MalformedRequestActionError,
            ObservationDeserializeError,
            SeatMismatchError,
            PossibleActionsValidationError,
            ProtocolConversionError,
        ):
            with self.subTest(error_class=error_class):
                self.assertTrue(issubclass(error_class, RiichiLabAdapterError))
                self.assertFalse(issubclass(error_class, RiichiLabClientError))

    def test_all_adapter_errors_are_arena_local(self) -> None:
        for error_class in (
            RiichiLabAdapterError,
            MalformedRequestActionError,
            ObservationDeserializeError,
            SeatMismatchError,
            PossibleActionsValidationError,
            ProtocolConversionError,
        ):
            with self.subTest(error_class=error_class):
                self.assertTrue(
                    error_class.__module__.startswith("lisjong_arena.riichilab"),
                    f"{error_class!r} is not Arena-local: {error_class.__module__}",
                )


if __name__ == "__main__":
    unittest.main()
