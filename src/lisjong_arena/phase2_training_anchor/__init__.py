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
- `training_labels`     : privileged omniscient stateを読む唯一のmodule
- `training_sample`     : 両pathをcomposeするだけでanchorを変更しない
- `rule_provenance`     : effective RuleSetのdeterministic fingerprint
- `pipeline_provenance` : source revision / semantics identityのbinding
- `extraction`          : trusted declassifierを呼ぶorchestration point

`extraction`と`training_labels`だけがomniscient stateを参照し、それより下流の
player-safe pathへ`MatchState` / `RoundState` / internal omniscient eventを
渡さない。

## Same-anchor alignment

`ExactTrainingLabels`は、label builderが実際に読んだprivileged stateから導出
した`LabelAnchorIdentity`（hand / honba / round revision / viewer / dealer /
prevailing wind）を持つ。`TrainingSample`はこれをanchor側のplayer-safe value
から構成した期待値と突き合わせ、state positionが一致しないcompositionを
fail closedで拒否する。labelのidentityはanchorからのcopyではないため、この
検証は自明には成立しない。

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
