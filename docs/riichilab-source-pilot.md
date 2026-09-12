# RiichiLab source pilot (Issue #211)

`lisjong_arena.riichilab_source_pilot`は、Arena Issue #211 1本のための
experiment-local harnessである。実験の目的・acceptance criteria・結果は
[lisjong-arena #211](https://github.com/lisbun/lisjong-arena/issues/211)と
親roadmap [lisjong-project #45](https://github.com/lisbun/lisjong-project/issues/45)
を正本とし、本書は**再利用するtechnical semantics**だけを記述する。

```text
intervention   data source strategy（1軸）

Arm Y          exact retained #140/#190 yakuhai-call source
Arm R          exact qualified #170 RiichiLab source

student        arena-policy-input-feature-v1 / 8204
               lisjong-action-vocabulary-1 / 802
               8204 -> 128 ReLU -> 802
               masked cross entropy over exact legal actions
budget         TRAIN 9,116 / VALIDATION 2,555 per arm
evaluation     ABBB / 4p-red-single / seeds 23000..23099 / 400 games
```

次は導入しない。

- generic external-data framework / generic replay engine / 新しい麻雀rules engine
- model registry / dataset registry / generic experiment framework
- architecture abstraction hierarchy / future P2・RL infrastructure

## Locked protocol

`protocol.py`がlocked valueをcodeとして固定する。結果を見てここを変更しない。

| 項目 | Locked value |
| --- | --- |
| protocol ID | `arena-riichilab-source-pilot-v1` |
| Arm Y source | retained #140/#190 dataset `69094c1b…c5a7b2f4` |
| Arm Y TRAIN / VALIDATION seeds | `245..264` / `265..270` |
| Arm Y TEST seeds（本Issueで読まない） | `271..276` |
| Arm R source | #170 corpus `064b9497…0718b865` / manifest `378edce0…2b9b0ca` |
| row budget | TRAIN `9,116` / VALIDATION `2,555`（両arm） |
| eligible row | exact materialization済みかつ legal action count `>= 2` |
| feature / vocabulary | `arena-policy-input-feature-v1` `8204` / `lisjong-action-vocabulary-1` `802` |
| model | `Linear(8204,128) + ReLU + Linear(128,802)`、parameter count `1,153,698` |
| loss / optimizer | masked cross-entropy / Adam `lr=1e-3`, `weight_decay=0` |
| batch / epochs / patience | `256` / `20` / `4` |
| seeds / workers / threads | training `0`, dataloader `0`, workers `0`, torch threads `1` |
| checkpoint selection | lowest own-source VALIDATION choice-row masked CE |
| evaluation | `abbb-single-round-v1` / `4p-red-single` |
| ordered seeds | `23000..23099`（100 seed blocks × 4 rotations = 400 games） |
| candidate / baseline | Arm R / Arm Y |
| formal TEST exposure | なし |

`require_evaluation_seeds()`はlocked populationそのもの以外を拒否する。
planning APIもexecution APIもseed引数を持たないため、resultを見てからseedを
追加・差し替える入口が存在しない。

## Gate 0 — RiichiLab MJAI -> exact current `DecisionContext`

Arena側へ新しい麻雀rules engineを実装せず、current dependencyである
`riichienv==0.4.10`のreplay APIをauthoritative seamとして使う。

```text
MJAI jsonl events
    -> MjaiReplay / Kyoku.steps()              # decision / implicit Pass authority
    -> RiichiEnv.apply_event() / get_observations()
         # 同じprefixのpublic history / canonical legal snapshot
    -> Observation.legal_actions() / select_action_from_mjai()
         # observed teacher intentをexact candidateへ解決
    -> current Arena adapter
         SeatMaterializedState / RiichiEnvActionMappingSession / build_policy_input
    -> DecisionContext(PolicyInput, legal_actions)
    -> 8204 feature + 802 legal mask + canonical teacher action index
```

### purpose-specific hybrid authority

`Kyoku.steps()`は`(pid, Observation, Action)`でdecision opportunityと
implicit Passを提示する。iteratorの`pid`をseat authorityとし、各stepを
round・actor・teacher event ordinal・pre-decision event ordinalへ厳密に結合する。
stepを1件でも消費できない場合、game全体をunsupportedにする。replayの
Action objectはcanonical teacher actionとして使用しない。

通常decisionの公開stateとlegal candidateは、同じprefixの
`RiichiEnv.apply_event()` / `get_observations()`から得る。replay Observationは
kan後の公開前doraを先取りし得て、Ponのconsumeが鳴いた3牌となり得る。
そのため両APIのseat・round・score・hand・河・riichi等のpre-decision stateが
一致することを検査し、特徴量へ入れる**履歴**はseat-visible event streamから
構築する。

```text
discardのglobal order / tsumogiri / called_by
riichi段階（NONE / DECLARED / ACCEPTED）
公開済みdora indicator
live wall残数（84 - kyoku内tsumo event数）
```

`Observation.new_events()`のseat-projected event列だけを既存の
`SeatMaterializedState`へ渡す。槍槓responseのみ、`apply_event()`がdecisionを
提示しないため、Kakan直後の`Kyoku.steps()` observationを使用する。
multi-ronの2人目はstepに出ないため、同じ打牌直後に凍結した
`apply_event()` response snapshotとexact Ron候補を使う。

### 0.4.10再検証と既知のreplay seam limitation

0.4.10上で`apply_event()` / `get_observations()`のdecision、
`Observation.legal_actions()` / `select_action_from_mjai()`とbase64
serialize / deserializeを実行で確認した。syntheticのChi / Pon /
Daiminkan / Kakan / Ron、post-call discard、round進行に加え、Arenaが
生成したfixed-seed 245・246のpublic-format logをcurrent adapterと照合した。
`last_discard`は牌IDだが、replayは別seatの過去の物理牌を同じIDへaliasし得る。
公開MJAIの直前打牌者とengineの直近河を照合し、call mappingへは現在の
triggerだけを示すviewを渡す。PolicyInput用の全河・公開履歴は保持する。
物理copy IDやmeldの出所を候補順・任意の牌IDから推測しない。

いずれも推測補完せず、contractとして扱う。`reason code`付きで計上する。

| # | limitation | 扱い |
| --- | --- | --- |
| 1 | `apply_event()`が作るmeldは`Meld.from_who == -1`で、`PublicMeld.from_seat`を満たせない | chi / pon / daiminkanの`target`はpublic MJAI record自身が持つ公開事実なので、call eventからmeld provenanceをprojectし、engineのmeld snapshotとkind / 件数 / 順序が一致することをfail closedで確認する |
| 2 | `apply_event()`は同じsemantic tileを1つのcanonical physical IDへaliasするため、`drawn_tile`がhandの既存copyと同一IDになり、tedashi / tsumogiriという公開上区別可能な2つのlegal discardが1つへ潰れる | engine自身が返した**slotごとの**legal discard action列とhand multisetから、一意に戻せる場合（`offered == held`）だけ戻す。drawn tile IDのslotがengine側で制限されていて一意に戻せない場合は、choice rowであれば`drawn_tile_discard_slots_restricted_under_collapsed_tile_identity`としてunresolvedにする |
| 3 | synthetic九種九牌のterminal recordでは`Kyoku.steps()`がstepを出さない | 同じprefixの`apply_event()`が提示した`KYUSHU_KYUHAI` legal candidateと`select_action_from_mjai()`でのみretainする。dealer / non-dealer初巡とeligibility終了後のabsenceを0.4.10で確認した |
| 4 | `apply_event()`はkakanに対する槍槓response windowを提示しない | `Kyoku.steps()`の同じKakan prefixのRon / Pass stepがある場合だけそのObservationでmaterializeする。future result / doraをhistoryへ入れない |
| 5 | `Kyoku.steps()`はmulti-ronの先頭winnerだけを出す | 2人目以降は同じpre-response prefixの`apply_event()` snapshotにexact Ronがある場合だけretainする。なければgame全体をunsupportedにする |
| 6 | 別seatの過去の河と現在の打牌が同じreplay physical IDになる | MJAI公開打牌者とengineの河・`last_discard`を照合し、call-target mappingへ現在のtriggerだけを渡す。照合不能なら`call_target_provenance_mismatch`でunresolvedにする |

Issue #233のArena-generated bounded sample（各mode seed 245・246）では、
`4p-red-single`が2 game / live 130 / hybrid 130 / exact choice 115 /
forced 15 / unresolved 0、`4p-red-half`が2 game / live 1,215 /
hybrid 1,215 / exact choice 1,136 / forced 77 / unresolved 2だった。
halfの2件はどちらもphysical copy slotが一意に解決できない打牌である。
retainしたrowでは8204 feature、802 legal mask、teacher indexのlive比較に
不一致がなかった。このsampleは実#170 Gate 0の成否を示すものではない。

kan宣言と補充drawの間のような中間状態は、`RiichiEnv.needs_tsumo`と
`Phase.WaitResponse`から判定してdecision opportunityへ計上しない
（live実行では1 stepの内側で通過するためdecisionとして露出しない）。

### decision opportunityの帰属

```text
apply_event() ごとに get_observations() を読み、legal actionsを持つseatを
open decisionとして登録する。

action eventのMJAI `actor`がそのactionを宣言したseatのauthorityであり、
attributionはそのseatのopen decisionだけを対象にする。

engineがもうそのseatへactionを求めていない、あるいは求めているdecision
自体が別のものへ変わった時点でそのdecisionは閉じる。multi-ronのように
同じtriggerへ後続actionが続く場合のため1 eventだけ猶予を置き、そこでも
帰属しなければ閉じる。
```

`reach_accepted`のようにresponse windowを閉じないstate eventでは、
`(last_discard, drawn_tile, legal actions)`から作るdecision signatureが
変わらないため、同じdecisionを二重に計上しない。明示`none`のようにengineが
消費しないaction eventについても、解決済みdecisionがactingへ残り続ける間は
再登録しない。

Gate 0 reportのdecision opportunityはtarget seatだけを数える。non-target seatの
open decisionはreplayとaction attributionのため保持するが、row / forced /
unresolvedと同様にsupervision populationの件数へ含めない。

### implicit pass

implicit Passをmaterializeするのは次の3条件がすべて成立する場合だけである。

1. `Kyoku.steps()`がそのseat・prefixにPass stepを提示している
2. 同じprefixのexact legal actionsへ`PASS`が含まれる
3. 明示`none` eventへ既に結び付いていない

他のseatがclaimしていてPass stepも存在しない場合、そのseatがPassを選んだのか
priorityで上書きされたのかは決まらないため、
`ambiguous_pass_after_competing_claim`としてunresolvedにする。明示`none`は
stepとeventが一致したときだけexplicit passとして扱う。

### Gate 0 hard outcome

```text
games_unsupported      == 0
unresolved_rows        == 0
leakage_failures       == 0
eligible_rows          >  0
```

counterは母数として整合している。`decision_opportunities`はtarget seatの
decision populationだけを数え、`eligible_rows + forced_rows + unresolved_rows`
と一致する。一致しない場合はmaterializationがfail closedする。
`leakage_failures`は`seat_visible_event_leaks_hidden_truth`として
unsupportedになったgame数であり、固定値ではない。

いずれかを満たさない場合、`build_row_budget()`はrow budgetを作らず、
orchestrationは`SOURCE MATERIALIZATION BLOCKED`を記録して model trainingの
前に停止する。unmasked CEや別feature schemaへの切り替えはこのIssueに存在しない。

### 情報境界

`Observation.new_events()`はRiichiEnv自身がseat単位でprojectしたMJAI event列で
あり、他家の`start_kyoku.tehais`と他家の`tsumo.pai`は`"?"`でmaskされる。
materializationはそのmaskが実際に成立していることをfail closedで確認してから
trackerへ渡す。

```text
student featureへ入れないもの
    opponent concealed hand / future draw / future action
    future score result / terminal game result
    ura-dora等のdecision時点で非公開のtruth
    #203 hidden-state supervision truth
```

未解決のresponse decisionを持つseatは、同一triggerへの後続responseを観測する
前のprefixからstateをfreezeする。

## Matched row budget

```text
canonical game order = sha256(domain | corpus_identity | game_id) 昇順
    -> VALIDATION partition = cumulative eligible rows >= 2,555 となる最短prefix
    -> TRAIN partition      = 残り全部
    -> 各partition内でcanonical order prefixをdeterministicに切り出す
```

- partition単位はraw gameであり、同じraw gameが両partitionへ跨がらない。
  shared game（複数target seat）もpartitionを跨がない
- canonical orderはsource identityへbindしたhashだけから決まる。row数、label、
  model loss、downstream score、bot strength、action rarity、validation resultを
  selection criterionに使わない
- partition内のpartial useはdeterministic row-budget truncationだけである
- budgetを満たせない場合は`DATA BUDGET NOT MATCHABLE`であり、sourceを増やして
  救済しない

両arm差はdescriptiveに記録するだけで、reweightしない。

```text
unique raw games / target participations
per-bot row counts / action-family counts / decision-kind counts
forced / choice row counts / legal-action-count distribution
open / closed hand / riichi / non-riichi
```

## Student / trainer

両armはまったく同じtrainer（`learned_policy_offline_q.bc_training.
train_from_split_tensors()`）を1回ずつ通る。Arm Yはhistorical #190 checkpointを
aliasせず、exact retained datasetから同じcurrent trainerで新しいpilot
checkpointを作る。

Arm Yのsource読み込みは`learned_policy_data_sufficiency.source`のTEST-blind
loaderを再利用する。本packageはTEST rowを読む入口を持たない。exact retained
bytesをstrict-readできない場合は、新しいyakuhai-call gameをregenerateせず
artifact blockerとして停止する。

## Serving

`serving.py`の`SourcePilotServingPolicy`が唯一の実装であり、Arm YとArm Rの
両方がこの同じclassで実行される。arm固有のfallback、guard、scaffold、
activation conditionを持たない。

```text
actual PolicyInput
 -> build_policy_input_feature() / tensor_values()
 -> frozen model
 -> build_legal_action_mask()（current decisionのexact legal actions）
 -> masked argmax
 -> resolve_legal_action()
 -> canonical InternalAction
 -> execute_policy()のvalidation境界
```

次はいずれもfail closedである。

```text
illegal selection / resolve failure / validation failure
non-finite logits / schema mismatch / vocabulary mismatch
```

## Artifact

artifactはrepository外を前提とする。generated dataset row、feature tensor、
legal mask、trained weights、large diagnosticsをGitへcommitしない。

```text
<retention root>/<retention key>/
    checkpoints/ARM_Y/          manifest.json + weights.pt
    checkpoints/ARM_R/          manifest.json + weights.pt
    seed-plan.json              result exposure前にlockするseed plan
    candidate-r-vs-baseline-y.json   既存ABBB strength artifact（immutable）
    source-pilot-result.json    exactly one predeclared outcome
```

checkpoint manifestは次をbindし、strict readbackで全件照合する。

```text
arm / source identity / materialized dataset identity
TRAIN row identity / VALIDATION row identity
feature schema identity + fingerprint
action vocabulary identity + fingerprint
training config identity
model config / parameter count
selected epoch / training diagnostics / runtime
weights byte count + sha256
checkpoint identity (= 上記logical fieldのsha256)
```

### Bundle-level strict readback

個々のfileが単体としてvalidでも、互いに無関係なfileを1つのbundleへ集めた
状態はそれだけでは検出できない。`verify`はbundleを1つのevidence unitとして
読み、次のcross-bindingを全件照合する（`riichilab_source_pilot.bundle`）。

```text
bundle直下のentry集合            既知のartifactのみ（未知entryはfail closed）
checkpoints/ARM_R, ARM_Y
    arm                        == directory名
    source_identity            == Issue #211のlocked source identity
    ARM_R corpus identity / manifest sha256 == locked #170 values
    ARM_Y dataset identity     == exact retained #140/#190 identity
    policy identity            == derive_policy_identity(arm, checkpoint identity)
result.sources                 == locked source identity block
result.gate0                   == ARM_R checkpointが束ねるGate 0 report
result.budget                  == matched 9,116 / 2,555
result.arms[arm].checkpoint    == 各checkpointのidentity document
seed-plan.candidate / baseline == ARM_R / ARM_Y のpolicy identity
strength artifact
    candidate / baseline       == seed planと同じidentity
    seeds / games / game mode / rotations / seed blocks == locked population
    summary                    == raw game resultsからの再集計
result.strength                == 再集計したcanonical summary document
result.outcome                 == classify_outcome(...)の再計算結果
```

自己整合hashであることでは足りない。locked valueそのものと一致しなければ、
そのfileは別実験の成果物として扱う。

comparison前に停止したoutcomeのbundleはresult 1 fileだけを許し、
checkpoint / seed plan / strength artifactを同梱した状態をfail closedにする。
さらにdecision orderを再計算して照合する。

```text
SOURCE MATERIALIZATION BLOCKED  <-> gate0.gate_passed == false
DATA BUDGET NOT MATCHABLE       <-> gate0.gate_passed == true
budget / arms / strength        == null
```

self-consistentに再hashされていても、記録されたoutcomeとGate 0 reportが
矛盾するresultは受け付けない。

`STOP / INVALID`は途中で止まった実行痕跡を許すが、実行順序を前提として固定する。

```text
checkpoints -> seed plan -> strength artifact

seed plan exists        -> 両armのcheckpointが存在し、policy identityがbindする
strength artifact exists -> seed planが存在し、candidate / baselineが一致する
```

artifact自身が宣言したidentityをexpected valueとして使わない。したがって、
単体としてvalidな別比較のstrength artifactをSTOP bundleへ入れてもfail closedする。

### Terminal outcomeのdurability

authoritative executionが始まった後のterminal outcomeは、例外経路を含めて
すべてwrite-once result artifactとして残る。

```text
plan         read only
materialize  NON-AUTHORITATIVE diagnostic。artifactを残さない。
             この出力だけではIssue #211のoutcomeは成立しない。
run          authoritative completion command
             Gate 0 blocked / budget not matchable / 実行中のprotocol error /
             artifact corruption / I-O failureをすべてdurableに記録する
verify       retained bundleのbundle-level strict readback
```

`run`は実行開始後に失敗した場合も`STOP / INVALID`をbundleへ書いてから例外を
伝播させる。先に書かれたterminal recordは後続handlerが上書きしない。bundle
destinationが既に存在する実行は`resolve_retention_target()`とwrite-once
artifact writerが拒否するため、同じretention keyでの都合のよいrerunはできない。

## Exhaustive outcome

```text
1. protocol / identity / leakage / corrupted evidence -> STOP / INVALID
2. exact DecisionContext / legal-mask materialization不能
                                                     -> SOURCE MATERIALIZATION BLOCKED
3. matched 9,116 / 2,555 budgetを形成できない          -> DATA BUDGET NOT MATCHABLE
4. valid ABBB result
       95% interval lower > 0 -> RIICHILAB SOURCE SIGNAL
       95% interval upper < 0 -> YAKUHAI-CALL SOURCE SIGNAL
       otherwise              -> SOURCE PILOT INCONCLUSIVE
```

interpretation境界はresultへ必ず同梱する。

```text
RIICHILAB SOURCE SIGNAL
    != pure teacher-strength causality
    != formal generalization
    != hanchan strength established
    != authorization for larger RiichiLab acquisition
    != automatic Champion promotion

YAKUHAI-CALL SOURCE SIGNAL
    != RiichiLab unusable for HandBelief
    != external strong-bot data globally harmful

SOURCE PILOT INCONCLUSIVE
    != sources equivalent
```

source-specific validation CEはdiagnosticであり、cross-source primary resultと
して比較しない。result exposure後のseed extension / rerun / hanchan escalation /
row budget変更 / source mixingはこのpackageに入口がない。

## Compliance / provenance boundary

#170 / #203から継承する。

```text
personal non-commercial analysis / ML training / local retention   GO
large-scale use / commercial use                                   HOLD
raw-log redistribution / public raw dataset                        NO-GO
public trained-weight distribution                                 unresolved
```

- 実#170 corpusはoperatorのlocal machineでだけ処理する
- raw third-party corpusをClaude / ChatGPT / CI / GitHubへuploadしない
- PR / CIはsynthetic / 明示的にpublic-formatなfixtureだけを使う
- GitHub commentはaggregate metric / identityだけを載せ、raw MJAI recordや
  concealed-hand recordを載せない

## Operator flow (post-merge only)

以下はPR merge後にoperatorがlocalで実行する。本PR / CIではlive commandを
実行せず、実corpusにも触れない。

```powershell
$root = "C:\private-research\riichilab-corpus"

# 0. locked protocolを確認する。
python -m lisjong_arena.riichilab_source_pilot plan

# 1. exact #170 corpus identityを確認する。
python -m lisjong_arena.riichilab_corpus validate `
  --snapshot "$root\snapshot.json" `
  --output-dir "$root\cache"

# 2. Gate 0だけを実行し、exact materializationが成立するか確認する。
python -m lisjong_arena.riichilab_source_pilot materialize `
  --snapshot "$root\snapshot.json" `
  --output-dir "$root\cache"
```

`materialize`はNON-AUTHORITATIVEなGate 0 diagnosticである。artifactを残さない
ため、**この出力だけではIssue #211のoutcomeは成立しない。** 出力には
`authoritative: false`と、その旨の`notice`が必ず付く。Gate 0が成立しなければ
exit codeは`2`、`gate0.gate_passed`は`false`、`observed_outcome`は
`SOURCE MATERIALIZATION BLOCKED`になる。そのときはtrainingへ進まず、
`gate0.unresolved_reasons`と`gate0.game_unsupported_reasons`をaggregateとして
#211 / #45へ報告する。Issue #211のoutcomeとして記録できるのは`run`が残した
write-once result artifactだけである。

Gate 0が成立した場合だけ、two-arm pilotを1回実行する。

```powershell
python -m lisjong_arena.riichilab_source_pilot run `
  --snapshot "$root\snapshot.json" `
  --output-dir "$root\cache" `
  --arm-y-dataset "$root\retained\offlineq-140-dataset" `
  --retention-backend "local-nas" `
  --retention-root "D:\research-retention" `
  --retention-key "riichilab-source-pilot-211/run-1"

python -m lisjong_arena.riichilab_source_pilot verify `
  --bundle "D:\research-retention\riichilab-source-pilot-211\run-1"
```

- `--output-dir`はlocal cache（read only）である
- `--arm-y-dataset`はexact retained #140/#190 dataset directoryである
- retention rootはGit work tree外かつtemporary directory外でなければならない
- 既存bundleを上書きしない（write-once）
- `run`はどのterminal outcomeでもbundleへresultを残す。失敗時の出力は
  `durable: true`と`artifact`を含む。`durable: false`の場合はretention先へ
  書けていないので、その旨をIssueへ記録する
- `verify`はbundle全体のcross-bindingを照合する。`outcome`と
  `verified`を#211へ転記する

### Issueへ転記するcompact summary

```text
gate0.replay_seam
gate0.games_processed / games_replayable / games_unsupported (+ reason別件数)
gate0.decision_opportunities / explicit_action_rows / implicit_pass_rows
gate0.forced_rows / eligible_rows
gate0.unresolved_rows (+ reason別件数)
gate0.leakage_check_failures
gate0.gate_passed

budget.train_rows / validation_rows
budget.train_game_count / validation_game_count
budget.distribution（per-bot / action family / decision kind / legal-action count /
                    open-closed / riichi-non-riichi）

arms.training_config_identity
arms.ARM_Y.checkpoint / arms.ARM_R.checkpoint（identity / weights sha256 / selected epoch）
arms.*.diagnostics（TRAIN / VALIDATION choice-row masked CE / runtime）

strength.mean_seed_block_delta / interval_lower / interval_upper
strength.positive_seed_blocks / zero_seed_blocks / negative_seed_blocks

outcome（ちょうど1つ）
result_identity

verify.outcome / verify.verified（bundle-level strict readbackの結果）
```

## Relationship to other contracts

| contract | 関係 |
| --- | --- |
| `docs/learned-policy-input-schema.md` | 8204 featureのsingle source of truth。本packageは独自encoderを持たない |
| `lisjong.action_vocabulary` | 802 legal maskとcanonical resolveのsingle source of truth |
| `docs/learned-policy-stage2.md` | model / training configのlocked value元 |
| `docs/learned-policy-offline-q.md` | flat-BC trainerとcheckpoint selectionの実装元 |
| `docs/learned-policy-data-sufficiency-preflight.md` | Arm YのTEST-blind source loaderと#190 S20 identity |
| `docs/riichilab-downstream-qualification.md` | #170 source identity gateとplayer-safe replayの先行結果 |
| `docs/policy-strength-evaluation.md` | ABBB / seed-block statisticsのsingle source of truth |

本Issueはこれらのidentity、dimension、fingerprint、artifact schemaを変更しない。
