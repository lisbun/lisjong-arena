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

optionalな観測hook（#514、#540）:

- `on_round_evidence_complete`: engine `run_hanchan()`の同名callbackへそのまま渡す
  （局ごとのplayer-safe evidence）。privilegedな`on_transaction_observation`は渡さない
- `on_delivery`: engine `run_hanchan()`の同名callbackへそのまま渡す（player-safeな
  progress fact・局と半荘のcompletion fact）。callbackの例外はengineの契約どおり
  そのまま呼び出し元へ伝播する（#540のD armは対象局の精算後にこれで打ち切る）
- `on_selector_call`: selectorが呼ばれるたびにengine `Seat`を渡す。selectorの入出力は
  変えない

すべて`None`なら従来と同じ呼び出しになる。
"""

from collections.abc import Callable

from lisjong_engine.driver import run_hanchan
from lisjong_engine.match_state import CompletedMatch, MatchState
from lisjong_engine.rules import RuleSet
from lisjong_engine.seat import Seat as EngineSeat

from lisjong_arena.lisjong_engine.policy_selector import build_seat_selectors


def _observed(seat: EngineSeat, selector, observer: Callable[[EngineSeat], None]):
    def select(observation, options):
        observer(seat)
        return selector(observation, options)

    return select


def run_policy_hanchan(
    policies: object,
    *,
    seed: int,
    rules: RuleSet | None = None,
    on_round_evidence_complete: Callable[[object], None] | None = None,
    on_selector_call: Callable[[EngineSeat], None] | None = None,
    on_delivery: Callable[[tuple], None] | None = None,
) -> CompletedMatch:
    """4席のlisjong Policyでfixed seedの半荘を完走させ、`CompletedMatch`を返す。"""
    if type(seed) is not int:
        raise TypeError("seed must be an int")
    if rules is not None and not isinstance(rules, RuleSet):
        raise TypeError("rules must be a RuleSet or None")
    if on_selector_call is not None and not callable(on_selector_call):
        raise TypeError("on_selector_call must be callable or None")
    if on_delivery is not None and not callable(on_delivery):
        raise TypeError("on_delivery must be callable or None")

    selectors = build_seat_selectors(policies)
    if on_selector_call is not None:
        selectors = {
            seat: _observed(seat, selector, on_selector_call)
            for seat, selector in selectors.items()
        }
    match_state = MatchState(seed=seed, rules=rules)
    callbacks = {}
    if on_round_evidence_complete is not None:
        callbacks["on_round_evidence_complete"] = on_round_evidence_complete
    if on_delivery is not None:
        callbacks["on_delivery"] = on_delivery
    return run_hanchan(match_state, selectors, **callbacks)
