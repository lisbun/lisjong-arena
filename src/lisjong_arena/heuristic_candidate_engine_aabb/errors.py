"""engine版AABB局単位recordのerror型（#513）。

第1層（局精算record）と第2層（公開event fact）の失敗を型で分ける。第2層の
失敗だけを第2層の無効化として扱い、第1層の破損を第2層の失敗へ分類し直さない
（仕様§10.6）。
"""


class EngineAabbRecordError(Exception):
    """engine版AABB局単位recordの作成・保存・読込・集計が契約を満たさない場合。"""


class KyokuRecordError(EngineAabbRecordError):
    """第1層（局精算record）、manifest、またはartifact全体の検査に失敗した場合。"""


class EventFactError(EngineAabbRecordError):
    """第2層（公開event fact）の取得・射影・検査に失敗した場合。"""


class SecondaryMetricError(EngineAabbRecordError):
    """副指標の再集計が保存物と整合しない場合（加法分解の不一致等）。"""


__all__ = [
    "EngineAabbRecordError",
    "EventFactError",
    "KyokuRecordError",
    "SecondaryMetricError",
]
