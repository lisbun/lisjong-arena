"""fresh current-revision live-label pathのbounded technical smoke。

retained augmentationが資格化できない場合にだけ使う代替経路であり、decision
timeに次を同一row identityでco-emitできることだけを示す。

```text
public / player-safe current flat-BC row
+
training-only privileged Tenpai annotation
```

このpathはlisjong-engine execution seamではなく、current retained flat-BCと
同じRiichiEnv実行境界（`LocalGameRunner` + teacher x4 + `4p-red-half`）を使う。
したがってPolicyInput semantics、8204 feature、legal actions、802 legal mask、
teacher action、game / rule semanticsはcurrent flat-BC contractそのものである。

populationは実行前にlockしたbounded technical smokeだけであり、full Stage A0
corpusは生成しない。
"""

from dataclasses import dataclass

from .emission import emit_decision
from .errors import StageA0ProtocolError
from .execution import observed_decisions_for_seed
from .protocol import (
    MAXIMUM_SMOKE_GAME_COUNT,
    SMOKE_POPULATION_IDENTITY,
    SMOKE_SEEDS,
    require_smoke_seed,
)

FRESH_LIVE_LABEL_PATH_QUALIFIED = "FRESH LIVE-LABEL PATH QUALIFIED"
FRESH_LIVE_LABEL_PATH_NOT_QUALIFIED = "FRESH LIVE-LABEL PATH NOT QUALIFIED"


@dataclass(frozen=True, slots=True)
class FreshQualification:
    """fresh live-label path qualificationの機械可読な結果。"""

    outcome: str
    population_identity: str
    seeds: tuple[int, ...]
    row_count: int
    cell_count: int
    rejection_reason: str | None
    rejection_detail: str | None
    cells: tuple

    def __post_init__(self) -> None:
        if self.outcome not in (
            FRESH_LIVE_LABEL_PATH_QUALIFIED,
            FRESH_LIVE_LABEL_PATH_NOT_QUALIFIED,
        ):
            raise StageA0ProtocolError("unsupported fresh qualification outcome")
        if self.outcome == FRESH_LIVE_LABEL_PATH_QUALIFIED:
            if self.rejection_reason is not None:
                raise StageA0ProtocolError(
                    "a qualified fresh route must not carry a rejection reason"
                )
            if self.row_count == 0:
                raise StageA0ProtocolError(
                    "a qualified fresh route must emit at least one decision row"
                )
        elif self.rejection_reason is None:
            raise StageA0ProtocolError(
                "a rejected fresh route must state its rejection reason"
            )

    @property
    def is_qualified(self) -> bool:
        return self.outcome == FRESH_LIVE_LABEL_PATH_QUALIFIED

    def to_document(self) -> dict[str, object]:
        return {
            "outcome": self.outcome,
            "population_identity": self.population_identity,
            "seeds": list(self.seeds),
            "row_count": self.row_count,
            "cell_count": self.cell_count,
            "rejection_reason": self.rejection_reason,
            "rejection_detail": self.rejection_detail,
        }


def qualify_fresh_live_label(
    *,
    seeds=SMOKE_SEEDS,
    observed_decision_source=observed_decisions_for_seed,
) -> FreshQualification:
    """bounded technical smokeでlive co-emission pathを資格判定する。

    `observed_decision_source`は`seed -> Iterable[ObservedDecision]`のcallable
    である。alignment
    failureはlabelへ丸めず、`FRESH LIVE-LABEL PATH NOT QUALIFIED`として理由
    つきで返す。
    """
    seeds = tuple(seeds)
    for seed in seeds:
        require_smoke_seed(seed)
    if len(seeds) > MAXIMUM_SMOKE_GAME_COUNT:
        raise StageA0ProtocolError(
            "the bounded technical smoke must not exceed the locked game count"
        )

    rows = 0
    cells = []
    for seed in seeds:
        for decision in observed_decision_source(seed):
            try:
                emission = emit_decision(
                    decision,
                    source_identity=SMOKE_POPULATION_IDENTITY,
                    seed=seed,
                )
            except Exception as error:
                return FreshQualification(
                    outcome=FRESH_LIVE_LABEL_PATH_NOT_QUALIFIED,
                    population_identity=SMOKE_POPULATION_IDENTITY,
                    seeds=seeds,
                    row_count=rows,
                    cell_count=len(cells),
                    rejection_reason="same-state-co-emission-failed",
                    rejection_detail=f"{type(error).__name__}: {error}",
                    cells=(),
                )
            rows += 1
            cells.extend(emission.cells)

    return FreshQualification(
        outcome=FRESH_LIVE_LABEL_PATH_QUALIFIED,
        population_identity=SMOKE_POPULATION_IDENTITY,
        seeds=seeds,
        row_count=rows,
        cell_count=len(cells),
        rejection_reason=None,
        rejection_detail=None,
        cells=tuple(cells),
    )


__all__ = [
    "FRESH_LIVE_LABEL_PATH_NOT_QUALIFIED",
    "FRESH_LIVE_LABEL_PATH_QUALIFIED",
    "FreshQualification",
    "qualify_fresh_live_label",
]
