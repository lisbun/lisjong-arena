"""RiichiEnv Adapter専用の例外。"""


class AdapterSyncError(Exception):
    """materialized state、Observation、decisionのいずれかが同期していない場合。

    duplicate event適用、start_kyoku欠落、call対象discardの不一致、riichi段階の
    順序異常、kyoku identityの不一致、未知のevent種別等、fail closedすべき
    状況全般で送出する。未検証の状態からPolicyInputを生成しないための単一の
    例外型とする。
    """
