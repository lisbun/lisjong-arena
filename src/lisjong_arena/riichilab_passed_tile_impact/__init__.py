"""Issue #503: 確定安全牌候補の影響範囲を、固定した423席の入力で数える集計。"""

from .impact import SCHEMA_VERSION, ImpactCounter, run_impact_count

__all__ = ["SCHEMA_VERSION", "ImpactCounter", "run_impact_count"]
