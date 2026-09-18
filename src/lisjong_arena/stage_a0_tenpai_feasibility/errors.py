"""Stage A0 Tenpai feasibility固有のerror型。

availability reason codeで表現するknown unsupported stateと、fail closedに
すべきcontract violationを型で区別する。
"""


class StageA0Error(Exception):
    """Stage A0 feasibility pathのbase error。"""


class StageA0ProtocolError(StageA0Error):
    """locked protocol識別子・population・contract identityの違反。"""


class StageA0AlignmentError(StageA0Error):
    """public rowとprivileged label stateが同一decision stateでない。"""


class StageA0SidecarError(StageA0Error):
    """privileged sidecarのschema / identity / readback違反。"""


class StageA0ReportError(StageA0Error):
    """feasibility reportのschema / identity / evidence chain違反。"""


class StageA0LabelError(StageA0Error):
    """canonical label pathのunexpected invariant violation。"""


__all__ = [
    "StageA0AlignmentError",
    "StageA0Error",
    "StageA0LabelError",
    "StageA0ProtocolError",
    "StageA0ReportError",
    "StageA0SidecarError",
]
