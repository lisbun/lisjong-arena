# Yakuhai Call-aware evaluation

## Purpose

`lisbun/lisjong#145` / PR #146で追加された
`YakuhaiCallGenbutsuDefenseFiniteHorizonHandValueAwarePolicy`を、Arenaの既存ABBB
`single_round_compare` protocolでno-call parentの
`GenbutsuDefenseFiniteHorizonHandValueAwarePolicy`と直接比較するためのconsumer wiringを記録する。

ArenaはYakuhai / shanten / kuikae / defense / call semanticを再実装しない。
selection semanticの正本はlisjong側であり、Arenaは明示catalogからPolicy factoryを
解決して既存evaluation pathへ渡すだけである。

## Dependency

Arenaはlisjongを次のfull commit SHAへexact pinする。

```text
84e905d252d65eb37b722f195f2774fd5661d5af
```

これはlisjong PR #146のactual merge commitで、
`YakuhaiCallGenbutsuDefenseFiniteHorizonHandValueAwarePolicy`を含むrevisionである。

## Policy catalog

`lisjong_arena.policy_catalog.POLICY_CATALOG`の関連mappingは次のとおり。

```text
extended-combined -> GenbutsuDefenseFiniteHorizonHandValueAwarePolicy
yakuhai-call      -> YakuhaiCallGenbutsuDefenseFiniteHorizonHandValueAwarePolicy
```

`yakuhai-call`はmodule top-level factoryを使用し、既存parallel executionの
spawn-serialization preflightを満たす。dynamic import、plugin discovery、
config-driven Policy loadingは導入しない。

## Gate 1 command

Gate 1では、既存strength evaluationsで未使用のfresh 100-seed rangeを選ぶ。
以下の`START:END`は100 seedsになるinclusive rangeへ置き換える。

```powershell
python -m lisjong_arena.single_round_compare `
  --candidate yakuhai-call `
  --baseline extended-combined `
  --seeds START:END `
  --workers 4 `
  --progress
```

protocolは既存の `ABBB / 4p-red-single` で、100 seeds × 4 rotations = 400 games。
このconsumer wiring自体はrotation、game mode、metrics、parallel semanticsを変更しない。

400局はcheap screenでありstrength proofではない。明確なregressionが見える場合は単純な
sample追加で救済しない。有望な場合のみ、Gate 1とは別のfresh seed集合で
2,500 seeds × 4 rotations = 10,000 gamesのGate 2へ進む。

strength結果とbaseline昇格判断は `lisbun/lisjong#121` へ記録する。
