"""effective `RuleSet` configurationのdeterministic provenance value。

engine `RuleSet`自身が`name` / `version`を「識別・version管理・ログ・再現性の
補助情報であり、ゲームmechanicsの分岐条件には使用しない」と定義している。
したがって`name` / `version`だけをeffective rules identityとして扱うと、
同じname/versionのまま個別mechanics fieldが変わったcorpusを区別できない。

本moduleは外部dependencyを追加せず、frozen `RuleSet`のeffective field値一式を
stableに正規化し、その正規化表現のSHA-256をfingerprintとして持つ最小のvalueを
提供する。

```text
RuleSet (frozen dataclass)
    -> deterministic normalized field rendering
    -> SHA-256 fingerprint
```

project-wide generic provenance frameworkにはしない。Phase 2のanchorが
「どのeffective rulesの下で観測されたか」をbindingするために必要な最小値だけを
持つ。
"""

import hashlib
import json
from dataclasses import dataclass, fields
from enum import Enum

from lisjong_engine.rules import RuleSet


def _render_value(value: object) -> object:
    """`RuleSet` fieldの値を、deterministicにJSON化できる表現へ落とす。

    未知のfield型はfail closedする。silentに`repr()`へ丸めると、型が増えた
    ときにfingerprintが意味を失ったまま通ってしまう。
    """
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, Enum):
        # Enumのidentityはmember名で固定する。値だけだと、別Enumの同一値と
        # 衝突し得る。
        return f"{type(value).__name__}.{value.name}"
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return value
    if isinstance(value, frozenset | set):
        # setは反復順序が保証されないため、renderした文字列でsortして固定する。
        return sorted(json.dumps(_render_value(item), sort_keys=True) for item in value)
    if isinstance(value, tuple | list):
        return [_render_value(item) for item in value]
    raise TypeError(
        f"unsupported RuleSet field value type for provenance: {type(value).__name__}"
    )


def normalize_effective_rules(rules: RuleSet) -> str:
    """effective `RuleSet`のstable normalized representationを返す。

    field名でsortしたうえでJSONへserializeするため、dataclass上の宣言順序が
    変わってもrepresentationは変わらない。
    """
    if not isinstance(rules, RuleSet):
        raise TypeError("rules must be a lisjong-engine RuleSet")

    rendered = {
        field.name: _render_value(getattr(rules, field.name))
        for field in sorted(fields(rules), key=lambda field: field.name)
    }
    return json.dumps(rendered, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class EffectiveRuleProvenance:
    """anchorがどのeffective rulesの下で観測されたかを表すimmutable value。

    `name` / `version`は補助identityとして保持するが、identityの正本は
    effective field値一式から導かれる`fingerprint`である。
    """

    name: str
    version: int
    fingerprint: str

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("name must be a non-empty str")
        if type(self.version) is not int:
            raise TypeError("version must be an int")
        if not isinstance(self.fingerprint, str) or len(self.fingerprint) != 64:
            raise ValueError("fingerprint must be a 64-character SHA-256 hex digest")


def effective_rule_provenance(rules: RuleSet) -> EffectiveRuleProvenance:
    """frozen `RuleSet`からdeterministic provenance valueを導出する。

    fingerprintは`name` / `version`を含むeffective field値一式から計算する。
    そのため、name/versionが同一でもmechanics fieldが1つ違えばfingerprintは
    変わり、effective rulesが完全に同一なら常に同じ値になる。
    """
    normalized = normalize_effective_rules(rules)
    return EffectiveRuleProvenance(
        name=rules.name,
        version=rules.version,
        fingerprint=hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
    )
