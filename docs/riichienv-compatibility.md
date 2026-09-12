# RiichiEnv backend compatibility boundary

`lisjong-arena`はRiichiEnvをcurrent external execution backendとして利用するが、RiichiEnvの内部state、`Observation`、`Action`、feature、RNG実装をlisjong ecosystemのcanonical contractとして所有しない。

本書は、ArenaがRiichiEnvへ依存するうえでupgrade時に再確認すべきsmall compatibility boundaryを記録する。repository-wide ownership / dependency directionは[`architecture.md`](architecture.md)、individual research / decision historyはGitHub Issuesを正本とする。

## Current pinned backend

Current Arena dependency:

```text
riichienv == 0.4.10
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

RiichiEnv v0.4.10でも、同じinstanceの2回目の`reset(seed=12345)`はfresh `RiichiEnv(seed=12345)`の初回`reset()`とwall、hands、dora indicators、serialized Observationが一致しなかった。`reset(seed=...)`をArena側のgeneric reseed / identical-game restart primitiveとして採用できるcontractではない。

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

## 0.4.10 preflight (#228, 2026-09-12)

v0.4.10 tag identityは`479c1faeb33d082965eef8198f63261a79c0fce3`。Windows CPython 3.14に公開wheelを導入し、`importlib.metadata.version("riichienv") == "0.4.10"`を実測した。fresh instanceを同じconstructor seed `12345`で作った場合、`4p-red-single` / `4p-red-east` / `4p-red-half`の各modeで初回wall、hands、MJAI log、base64 Observationが一致した。mode identityはそれぞれ`0` / `1` / `2`。既存のreal-backend round-result / round-stats integrationはsingle-roundとhalf-gameの局遷移・終了を検証する。同一instanceの`reset(seed=12345)`は上記のfresh初回stateを再現しなかったため、one-shot boundaryを維持する。

Installed runtimeで`RiichiEnv`、`Action` / `ActionType`、`Meld` / `MeldType`、`Observation`、`HandEvaluator`を確認した。初回および進行中のObservationは、Arenaが使う`player_id`、`hands`、`melds`、`discards`、`dora_indicators`、`scores`、`riichi_declared`、`honba`、`riichi_sticks`、`round_wind`、`oya`、`kyoku_index`、`last_discard`、`last_tedashis`、`drawn_tile`を提供する。`new_events()`、`legal_actions()`、base64 serialize/deserializeも実行で確認した。legal `Action`には`action_type`、`actor`、`tile`、`consume_tiles`がある。RiichiLabのoffline request-action parserとdurable ranked-record strict readbackはこのdeserialize pathを使う。

Upstream [v0.4.8...v0.4.10 compare](https://github.com/smly/RiichiEnv/compare/v0.4.8...v0.4.10) / [v0.4.10 release](https://github.com/smly/RiichiEnv/releases/tag/v0.4.10)をArenaの使用面で分類する。

| 分類 | 変更とArenaへの影響 |
| --- | --- |
| not used by Arena | WASM/UIのyakuman表示、3-playerのriichi/kita legal-action変更、ObservationのML feature encoding、Mahjong Soul専用のdealer opening-discard metadata。Arenaのcurrent execution modeは4-playerで、RiichiLab request-actionのObservationをserverから復元する。 |
| compatible with current Arena assumptions | `Action` / `Meld`の形、base64 Observation復元、MJAI event channel、Chi / Pon / Daiminkan / Ron response window、Kakanのlegal candidate形とpost-call progression。実RiichiEnvでChi / Pon / Daiminkan / Kakan / Ron candidateを観測した。Kakanの短縮入力をupstreamがlegal candidateへ正規化する変更は、Arenaが元のlegal `Action` objectを返す経路を変えない。response claim retirement、temporary furitenのdiscard時解除もupstream rule修正であり、Arenaはそのruleを再実装しない。 |
| intentional upstream semantic drift requiring update | non-red fiveを優先する`find_action()`、discard/riichi historyと`last_discard` projection、初回dealer tedashi、abortive drawとmatch-end renchan、riichi deposit settlement、special-hand yakuman scoringが変わり得る。同一seedの0.4.8 trajectory / scoreとの一致は要求しない。new runのprovenanceは0.4.10とし、historical 0.4.8 identityは保持する。 |
| blocking incompatibility | 0.4.10の実Observationで`last_discard`はphysical tile id（例: Chi候補で`12`）なのに、Arena `_resolve_call_target()`はseat indexとして読む。既存fixed-seed `LocalGameRunner` single / half integrationが`seat_from_player_index(12)`で失敗する。これは別Issue [#227](https://github.com/lisbun/lisjong-arena/issues/227)のsemantic repairであり、#228には混ぜない。 |

`last_discard`は0.4.10のraw Chi / Pon / Daiminkan / Ron candidateの`Action.tile`と一致した。Kakan candidateはadded tileと既存Ponの3 physical idsを持ち、実行後の`MeldType.Kakan`は4枚を持った。Chi / Pon後はcaller自身のdiscard decisionへ、Daiminkan後はrinshan drawを伴うdiscard decisionへ進んだ。chankanについてはupstreamがKakanを完全なlegal candidateへ正規化してresponse処理へ渡す。Arena側のRon target translationは#227が未修正のため、end-to-end互換を主張しない。

Windowsでのfull local suite（PyTorch導入済み）は3408件中、15 errorsが#227のresolver、9 errorsがtest固定の`/tmp` directory不在、2 failuresが同一unittest process内のPyTorch import汚染によるものだった。0.4.10のraw round-result / round-stats、offline RiichiLab request-action / durable ranked-record、追加したupstream characterizationはpassした。ML-focused CI相当の14 pattern / 431 testsは独立processで全件passした。#227を含むfull-suite greenとLinux CI gateは未達である。

PR #225（Issue #211）は本preflight時点ではopenであり、その未マージの`riichilab_source_pilot` replay/materialization pathはこのgateの対象外。PR #225 must rebase/revalidate its RiichiEnv 0.4.8 assumptions against 0.4.10 before merge. 特に`apply_event()` / `get_observations()` / legal-action / meld provenance / physical tile / first-turn / kakan-chankan replayを再確認する必要がある。

### Post-#227 landing status

上記は#228単独preflight時点の記録である。その後、#227を独立PR #230として実装・レビューし、`issue-228-riichienv-0410` branchへsquash mergeした。current branchではcall-target resolverが0.4.10のphysical tile `last_discard` contractへ適合し、通常discardは相手の最新捨て牌、chankan Ronはactive Kakan meldからtargetをexactly one opponentとして解決する。physical tile id `119`と`0..3`の回帰、Chi / Pon / Daiminkan / Ron、kakan/chankan、ambiguity fail-closedを固定している。

PR #230 headではLinux CIの`quality` / `phase6-ml`がともに成功した。#230をstackへ取り込んだ後の#229 headでも同じcanonical CI gateを再実行し、greenをmerge条件とする。preflightで記録したWindows `/tmp` 9 errorsとsame-process PyTorch 2 failuresは#227 / #228と無関係であり、このupgradeのlanding blockerとはしない。live RiichiLab rankedはまだ再実行していない。

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
