"""Stage 4a bounded strength screeningのerror型。

Gate 0 (candidate freeze / retention) とscreening executionを別のfailure
classとして区別する。`lisbun/lisjong-arena #138`のexhaustive outcomeへ
mappingできるよう、retention失敗をscreening失敗と同一視しない。
"""


class Stage4aError(Exception):
    """Stage 4a package共通のbase error。"""


class Stage4aProtocolError(Stage4aError):
    """locked Stage 4a protocolに反する入力・状態の場合。"""


class Stage4aRetentionError(Stage4aError):
    """candidate bundleをnon-ephemeral locationへretainできない場合。

    このerrorは`ARTIFACT RETENTION BLOCKED`へ対応する。strength runへ
    進む前にfail closedするためのものであり、screening失敗ではない。
    """


class Stage4aFreezeError(Stage4aError):
    """freeze recordとretained checkpointがbindしない場合。"""


class Stage4aScreeningError(Stage4aError):
    """ABBB screening実行またはartifact readbackが契約を満たさない場合。"""


__all__ = [
    "Stage4aError",
    "Stage4aFreezeError",
    "Stage4aProtocolError",
    "Stage4aRetentionError",
    "Stage4aScreeningError",
]
