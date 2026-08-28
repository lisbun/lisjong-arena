"""lisjong-project #22 Phase 0.5のdisposable end-to-end vertical slice。

`lisjong-engine` self-playの`TURN` anchorから、

```text
seat-safe PolicyInput
    -> experiment-local feature
    +  omniscient exact expected-count label
    -> game-grouped split
    -> conditional-uniform baseline / disposable bucketed estimator
    -> prediction metrics + Track B decision-linked metrics
```

を1本だけ通すためのexperiment-local packageである。ここのfeature表現、
estimator、harnessは長期成果物ではなく、後続Phaseで捨ててよい。canonical
raw corpus format、production dataset framework、generic model serving、
`GameBackend`等のbackend abstractionはここでも導入しない。

omniscient stateはlabel生成・offline evaluation・leakage検証のためだけに
Arena-side observerが読み、online Policy pathへ流さない。生成したdataset、
model、temporary artifactはrepositoryへcommitしない。
"""
