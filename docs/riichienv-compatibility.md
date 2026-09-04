# RiichiEnv backend compatibility boundary

`lisjong-arena`はRiichiEnvをcurrent external execution backendとして利用するが、RiichiEnvの内部state、`Observation`、`Action`、feature、RNG実装をlisjong ecosystemのcanonical contractとして所有しない。

本書は、ArenaがRiichiEnvへ依存するうえでupgrade時に再確認すべきsmall compatibility boundaryを記録する。repository-wide ownership / dependency directionは[`architecture.md`](architecture.md)、individual research / decision historyはGitHub Issuesを正本とする。

## Current pinned backend

Current Arena dependency:

```text
riichienv == 0.4.8
```

このversion identityはevaluation / training artifactのprovenanceと合わせて扱う。RiichiEnvをupgradeした場合、本書に記録したobserved behaviorを新versionへ機械的に持ち越さない。

## Supported reproducibility unit

Arenaのstandard `LocalGameRunner`でsupportするfixed-seed reproducibility unitは次である。

```text
one reproducible game execution
    = fresh RiichiEnv instance
    + constructor seed
    + explicit game_mode
    + current pinned rule semantics
    + one LocalGameRunner.run()
```

Current implementationは概念的に:

```python
env = RiichiEnv(seed=seed, game_mode=game_mode)
observations = env.reset()
```

とし、`LocalGameRunner` instance自体もone-shotである。

同一seedを再実行するときは、同じenvironment instanceをresetして使い回すのではなく、fresh `LocalGameRunner` / fresh `RiichiEnv` instanceを作る。

## `reset(seed=...)` is not an Arena contract

Arenaは次を保証しない。

```text
reset(seed=x) == constructorから同じgameをrestartする
same RiichiEnv instance + same reset seed == identical wall sequence restart
reset(seed=x) == fresh RiichiEnv(seed=x)
```

RiichiEnv v0.4.8のseed / wall lifecycleは、`reset(seed=...)`をArena側のgeneric reseed / identical-game restart primitiveとして採用できるcontractではない。

したがって、future batching、environment pooling、duplicate evaluation、replay、self-play corpus generation等で、performance上の都合だけから:

```python
env.reset(seed=next_seed)
```

へlifecycleを変更しない。そのようなconsumer requirementが生じた場合は、対象RiichiEnv versionでwall / hand-index / match progressionを含むsemanticsを改めて検証してからbounded Issueで設計する。

これはRiichiEnv API一般の評価ではなく、Arenaが再現性を主張するboundaryを狭く固定するためのconsumer-side contractである。

## Existing regression coverage

Current test suiteはこのboundaryを新しいframeworkなしで直接保護している。

### Constructor seed wiring

`tests/test_riichienv_local_game_runner.py`は、`LocalGameRunner(seed=7, game_mode="4p-red-half")`がexactly:

```python
RiichiEnv(seed=7, game_mode="4p-red-half")
```

をconstructすることをassertする。

同じfocused unit pathのfake environmentは`reset(self)`だけを提供するため、runnerがsilentに`reset(seed=...)`へ変更されればtest failureになる。

### Real-backend fixed-seed reproducibility

`tests/test_riichienv_local_game_runner_integration.py`は、同じconstructor seedを持つfresh `LocalGameRunner`を2回実行し、real RiichiEnv上で次が一致することを確認する。

- `LocalGameResult`
- `LocalGameInspection`
- `GameTrace`

このintegrationはsame-instance reset reproducibilityをclaimしない。

## Upgrade gate

`riichienv` pinを変更する場合、少なくとも次をpreflightで再確認する。

1. **constructor seed determinism**
   - fresh instance + same seed + same mode / rulesでcurrent fixed-seed integrationが再現するか
2. **reset semantics**
   - seed、wall sequence、round progression、internal RNG stateに関するupstream behaviorが変わっていないか
3. **single-round / hanchan progression**
   - fixed seedでround transitionを含むgame result / traceが再現するか
4. **mode / rule identity**
   - `game_mode` presetまたはdefault rule semanticsにmaterialな変更がないか
5. **adapter-sensitive behavior**
   - `Observation.new_events()`、legal actions、call / kan / chankan、riichi lifecycle等、Arena Adapterが明示的に扱うupstream semanticsに変更がないか
6. **provenance**
   - evaluation / training artifactがnew RiichiEnv identityを旧versionと区別できるか

upstream implementationが改善され、`reset(seed=...)`の意味が将来変わった場合も、自動的にArena contractを拡張しない。具体的consumer benefitとcompatibility evidenceがある場合だけ、current fresh-instance boundaryを再評価する。

## Relationship to other work

このcontractは以下を実装しない。

- reusable environment pool
- wall injection
- duplicate-mahjong protocol
- RNG abstraction
- generic backend abstraction
- RiichiEnv fork
- `lisjong-engine` seed contract変更
- batching / vectorized self-play

future high-throughput execution workはこのboundaryを前提にし、reuseが必要になった時点でRiichiEnv current APIを再評価する。

## Decision summary

```text
KEEP
    fresh RiichiEnv instance + constructor seed + one-shot runner

DO NOT ASSUME
    reset(seed=...) is an identical-game restart primitive

REVALIDATE ON UPGRADE
    seed / wall / reset / mode / adapter-sensitive semantics

DO NOT GENERALIZE
    RiichiEnv RNG or lifecycle into project-wide canonical contracts
```
