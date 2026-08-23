"""RiichiLab protocol-facing decision bridge専用の例外(Arena-local canonical、Issue #27)。

request_action入力validation、Observation deserialize、seat-bound bridge
instanceのseat照合、送信前possible_actions semantic validation、MJAI response
正規化という責務境界ごとに、呼び出し側が原因追跡できる最小限の例外型だけを
定義する。`lisjong_arena.riichienv.adapter` / `lisjong.policy_contract`が
送出する例外はここで変更・再wrapせず、そのまま伝播させる。

このhierarchyは、lisjong Issue #38/#39で確立したcontractをbehavior-
preservingにArenaへcanonical physical migrationしたものである(Arena Issue
#27)。

**このhierarchyは既存Arena lower-level client error hierarchy
(`lisjong_arena.riichilab.errors.RiichiLabClientError`)へreparentしない。**
`RiichiLabAdapterError`は直接`Exception`を継承する。現行ranked / validation
CLIが`RiichiLabClientError`だけをcatchする範囲を、この移行で変更してはならない
(Arena Issue #27)。
"""


class RiichiLabAdapterError(Exception):
    """RiichiLab protocol-facing decision bridge境界のfail closed例外の基底class。"""


class MalformedRequestActionError(RiichiLabAdapterError):
    """parsed済みrequest_action相当dataが必須field欠落・型不正等でmalformedな場合。

    `type != "request_action"`、`request_id` / `possible_actions` /
    `observation`の欠落または安全に扱えない型を含む。
    """


class ObservationDeserializeError(RiichiLabAdapterError):
    """base64 observationを4-player `riichienv.Observation`へ復元できない場合。"""


class SeatMismatchError(RiichiLabAdapterError):
    """deserialize済みObservationのplayer_idが、このbridgeのbound seatと一致しない場合。"""


class PossibleActionsValidationError(RiichiLabAdapterError):
    """送信予定Actionをserver提示`possible_actions`へ安全に照合できない場合。

    malformed / unknown candidate、比較不能、semantic match 0件を拒否する。
    同じsemantic Actionへ複数candidateが一致する場合は、1件以上一致として受理する。
    """


class ProtocolConversionError(RiichiLabAdapterError):
    """resolve済みRiichiEnv Actionを、RiichiLab Bot-to-Server response相当の
    MJAI dictへ変換できない場合。
    """


__all__ = [
    "MalformedRequestActionError",
    "ObservationDeserializeError",
    "PossibleActionsValidationError",
    "ProtocolConversionError",
    "RiichiLabAdapterError",
    "SeatMismatchError",
]
