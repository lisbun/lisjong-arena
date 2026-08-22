"""RiichiLab lower-level runtime専用の例外(Arena-local canonical, Issue #23)。

`docs/riichilab-client.md`「責務境界」「fail closed」を実装する。lisjong側
`RiichiLabSeatAdapter`、Policy、`InternalAction`が送出する例外はここで変更・
再wrapせず、そのままvalidation/ranked sessionと公開runnerを通じて伝播させる。

このhierarchyは、lisjong Issue #39/#44/#45で確立したcontractをbehavior-
preservingにArenaへcanonical physical migrationしたものである(Arena Issue
#23)。lisjong側legacy copyは`lisbun/lisjong#91` / PR #92で削除され、
Arena Issue #25でcleanup merge SHAへのdependency pin syncも完了した。
"""


class RiichiLabClientError(Exception):
    """RiichiLab lower-level runtime境界のfail closed例外の基底class。"""


class ProtocolError(RiichiLabClientError):
    """server messageがtransport lifecycle契約に違反している場合。

    JSON parse不能、既知lifecycle eventの必須field欠落・型不正、
    `start_game`前の`request_action`、seat不一致、`request_id`
    lifecycle違反(duplicate/old/decreasing/response mismatch)、
    `action_ack`のprotocol不整合(unknown request_id、unknown status、
    `rejected`/`unparseable`)、`validation_result`のmalformed `passed`、
    ranked `end_game`のmalformed scores、response serialization失敗を含む。
    """


class TransportError(RiichiLabClientError):
    """WebSocket接続そのものの送受信が失敗した場合。"""


class UnexpectedDisconnectError(TransportError):
    """mode固有terminal event受信前にconnectionが切断された場合。

    公式protocol上mid-game reconnectはサポートされないため、この例外は
    成功として扱わない。自動的なreconnectやretryは行わない。
    """


__all__ = [
    "ProtocolError",
    "RiichiLabClientError",
    "TransportError",
    "UnexpectedDisconnectError",
]
