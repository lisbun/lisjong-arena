"""通常ツモ補正（#598）の共通例外。"""

from lisjong_arena._artifact_io import ArtifactValidationError


class DrawCorrectionPlanError(ArtifactValidationError):
    """planが契約を満たさない、又はartifact・実行がplanに結び付かない場合。"""


__all__ = ["DrawCorrectionPlanError"]
