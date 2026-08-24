"""first-party engine上でlisjong Policy 4体の半荘を1回実行する薄いcomposition。

```text
MatchState(seed, rules)
        ↓
build Policy selectors
        ↓
lisjong-engine.run_hanchan()
        ↓
CompletedMatch
```

だけを行う。engineのgame progressionをArenaへ複製せず、`CompletedMatch`を
別のArena resultへコピーもしない。evaluation protocol、seed suite、seat
rotation、metricsはこのmoduleの責務ではない。
"""

from lisjong_engine.driver import run_hanchan
from lisjong_engine.match_state import CompletedMatch, MatchState
from lisjong_engine.rules import RuleSet

from lisjong_arena.lisjong_engine.policy_selector import build_seat_selectors


def run_policy_hanchan(
    policies: object,
    *,
    seed: int,
    rules: RuleSet | None = None,
) -> CompletedMatch:
    """4席のlisjong Policyでfixed seedの半荘を完走させ、`CompletedMatch`を返す。"""
    if type(seed) is not int:
        raise TypeError("seed must be an int")
    if rules is not None and not isinstance(rules, RuleSet):
        raise TypeError("rules must be a RuleSet or None")

    selectors = build_seat_selectors(policies)
    match_state = MatchState(seed=seed, rules=rules)
    return run_hanchan(match_state, selectors)
