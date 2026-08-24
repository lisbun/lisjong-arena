"""first-party `lisjong-engine` bridge専用の例外。

いずれもfail closedのための例外であり、unknown value、解決できないprovenance、
一意でないmappingを`None` / `0` / `False` / 先頭候補等へ黙って丸めない。
"""


class EngineBridgeError(Exception):
    """first-party engine bridge境界のcontract不整合を表すbase例外。"""


class UnsupportedEngineValueError(EngineBridgeError):
    """engineのpublic enum / descriptor variantをlisjong契約へ変換できない場合。"""


class ObservationProjectionError(EngineBridgeError):
    """`SeatObservation`をlisjong `PolicyInput`へ射影できない場合。"""


class KakanProvenanceError(EngineBridgeError):
    """加槓の元Ponをcurrent snapshotから一意に解決できない場合。

    元Pon候補が0件の場合と2件以上の場合の双方で送出する。added tileと
    tile typeが一致するPonを推測で選ばない。
    """


class AmbiguousActionMappingError(EngineBridgeError):
    """複数のengine descriptorが同じ`InternalAction`へcollapseした場合。

    representativeを勝手に選ばず、decision全体をfail closedする。
    """


class UnmappedActionError(EngineBridgeError):
    """canonical `InternalAction`を元のengine descriptorへ戻せない場合。"""


class SeatIdentityError(EngineBridgeError):
    """observation viewer seat / mapping actor / legal action actorが不一致の場合。"""
