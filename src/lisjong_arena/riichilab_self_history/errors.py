"""Issue #269 self-history error contract.

`SelfHistoryError`はIssue #269 acquisition path固有のfail-closed違反を表す。
reuseする#170 low-level primitive（bounded HTTP、strict JSON、gzip / JSONL、
MJAI lifecycle validation）は`CorpusError`を送出するため、acquisition境界では
`ACQUISITION_ERRORS`でまとめて捕捉する。両者とも`ValueError`である。
"""

from __future__ import annotations

from lisjong_arena.riichilab_corpus.models import CorpusError


class SelfHistoryError(ValueError):
    """The self-history input, response, or persisted state violates the contract."""


ACQUISITION_ERRORS: tuple[type[BaseException], ...] = (SelfHistoryError, CorpusError)


__all__ = ["ACQUISITION_ERRORS", "SelfHistoryError"]
