"""lisjong-project #24 Phase 2のleakage-safe training anchor pipeline。

first-party `lisjong-engine` executionのTURN / pre-action anchorで、

```text
player-safe path
    trusted SeatObservation
    + trusted ordered RoundEvidence
    + effective rule provenance
        -> FrozenPlayerSafeAnchor

training-only omniscient path
    same-anchor realized concealed truth
        -> exact expected-count / red-five truth
        -> per-opponent exact structural wait + availability

両pathが独立に完成したあとで
        -> TrainingSample
```

を成立させる最小pipelineである。

## Information-flow boundary

module構成そのものがboundaryの宣言になっている。

- `player_safe_anchor`  : hidden truth / label / availabilityをimportしない
- `training_labels`     : privileged `RoundState`を読む唯一のmodule
- `training_sample`     : 両pathをcomposeするだけでanchorを変更しない
- `extraction`          : trusted declassifierを呼ぶorchestration point

`extraction`だけがactive `RoundState`を参照し、それより下流のplayer-safe path
へ`MatchState` / `RoundState` / internal omniscient eventを渡さない。

## Phase 0.5との関係

`phase05_belief_slice`はauthoritative measurement済みのdisposable experimentで
あり、本packageはそのtype、label exclusion semantics、seed split、bucketed
estimator、measurement protocolを再利用も変更もしない別pathである。共有するの
はpattern / implementation techniqueだけである。

## Non-goals

model training、feature tensor設計、learned estimator、train/validation/test
split、raw corpus persistence、shard format、generic dataset / event-sourcing /
replay framework、red-five head、wait mechanism head、ron-legal targetは
本Phaseの対象外である。
"""
