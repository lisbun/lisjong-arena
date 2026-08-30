# Extended Combined evaluation

## Purpose

`lisbun/lisjong#143` / PR #144で追加された
`GenbutsuDefenseFiniteHorizonHandValueAwarePolicy`を、Arenaの既存ABBB
`single_round_compare` protocolでcurrent strength baselineの
`GenbutsuDefenseFiniteHorizonValueAwarePolicy`と直接比較するためのconsumer wiringを記録する。

ArenaはPolicy semanticを再実装しない。selection semanticの正本はlisjong側であり、
Arenaは明示catalogからPolicy factoryを解決して既存evaluation pathへ渡すだけである。

## Dependency

Arenaはlisjongを次のfull commit SHAへexact pinする。

```text
140d0d6c88b4f1c0c78ca2413b2e93128645cd4c
```

これはlisjong PR #144のactual squash merge commitで、
`GenbutsuDefenseFiniteHorizonHandValueAwarePolicy`を含むrevisionである。

## Policy catalog

`lisjong_arena.policy_catalog.POLICY_CATALOG`の対応は次のとおり。

```text
two-step         -> TwoStepUkeirePolicy
finite-horizon   -> FiniteHorizonCompletionPolicy
combined         -> GenbutsuDefenseFiniteHorizonValueAwarePolicy
hand-value-aware -> HandValueAwareTwoStepUkeirePolicy
extended-combined -> GenbutsuDefenseFiniteHorizonHandValueAwarePolicy
```

`extended-combined`はmodule top-level factoryを使用し、既存parallel executionの
spawn-serialization preflightを満たす。dynamic import、plugin discovery、config-driven
Policy loadingは導入しない。

## Gate 1 command

Gate 1では、current Combinedとの過去の大規模評価で未使用のfresh seed rangeを選ぶ。
以下の`START:END`は、100 seedsになるinclusive rangeへ置き換える。

```powershell
python -m lisjong_arena.single_round_compare `
  --candidate extended-combined `
  --baseline combined `
  --seeds START:END `
  --workers 4 `
  --progress
```

protocolは既存の `ABBB / 4p-red-single` で、100 seeds × 4 rotations = 400 games。
このconsumer wiring自体はrotation、game mode、metrics、parallel semanticsを変更しない。

Gate 1で明確なregressionが見える場合は単純なsample追加で救済しない。有望な場合のみ、
Gate 1とは別のfresh seed集合で2,500 seeds × 4 rotations = 10,000 gamesのGate 2へ進む。

strength結果とbaseline昇格判断は `lisbun/lisjong#121` へ記録する。
