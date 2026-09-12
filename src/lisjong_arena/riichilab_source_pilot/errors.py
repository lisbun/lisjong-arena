"""Issue #211 source-pilot fail-closed例外階層。

このpackageはsilent fallbackを持たない。source identity、Gate 0
materialization、row budget、training symmetry、serving、evaluation
protocolのいずれが成立しない場合も、近い値で代替せず例外にする。
"""


class SourcePilotError(Exception):
    """Issue #211 source pilot境界のfail closed例外の基底class。"""


class SourcePilotProtocolError(SourcePilotError):
    """locked protocol値・identity・contractから逸脱した場合。"""


class SourcePilotArtifactError(SourcePilotError):
    """artifactのwrite / strict readbackが成立しない場合。"""


class SourceIdentityError(SourcePilotError):
    """source identity gateを通過できない場合。"""


class MaterializationError(SourcePilotError):
    """Gate 0のexact materializationが成立しない場合。"""


class BudgetNotMatchableError(SourcePilotError):
    """matched row budgetを形成できない場合。"""


class ServingError(SourcePilotError):
    """serving pathがfail closedした場合。"""


__all__ = [
    "BudgetNotMatchableError",
    "MaterializationError",
    "ServingError",
    "SourceIdentityError",
    "SourcePilotArtifactError",
    "SourcePilotError",
    "SourcePilotProtocolError",
]
