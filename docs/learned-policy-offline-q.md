# Learned Policy Offline Q vertical slice — BC-vs-Offline-Q controlled comparison

`lisjong_arena.learned_policy_offline_q`は、`yakuhai-call`のfixed scaffold下で
ordinary discardに限定し、既存のminimal Behavior Cloning (BC) recipeと
support-restricted Offline Q recipeをcontrolled comparisonする
`lisbun/lisjong-arena #140`のexperiment実装である。

本書が扱うのは**再利用するtechnical semantics**（locked protocol、
macro-transition dataset契約、support gate、training / checkpoint契約、
serving hybrid契約、CLI usage）だけである。individual runのresultは
bounded Issueとimmutable Arena artifactを正本とし、本書へ数値を転記しない。

```text
individual result
    = lisbun/lisjong-arena #140 + SingleRoundStrengthArtifact

reusable technical semantics
    = 本書
```

## Scope

本Issueは「Behavior CloningとOffline Q、どちらのlearning objectiveが
game-coherentなpolicyを作れるか」というobjective questionだけを切り分ける。
representation、model scale、Mortal-style structural feature、HandBelief、
online RL、production adoptionはscope外である。

## Stable contracts kept unchanged

Stage 2 (`lisjong_arena.learned_policy_stage2`) がlockした次の契約を
そのまま再利用し、変更しない。

```text
PolicyInput / InternalAction
arena-policy-input-feature-v1 (8204)
lisjong-action-vocabulary-1   (802)
teacher identity              yakuhai-call
                              (YakuhaiCallGenbutsuDefenseFiniteHorizonHandValueAwarePolicy)
game mode                     4p-red-half (dataset生成 / smoke)
                              4p-red-single (ABBB strength screen, protocol invariant)
network shape                 8204 -> 128 ReLU -> 802 (両arm共通)
```

## Locked seed population

Stage 2 / Stage 3 / Stage 4aの既存populationと重複しない、fresh rangeだけを
使う（`protocol.py`が起動時に重複を検出しfail closedする）。

```text
Stage 2 dataset       200..215  (既存、変更しない)
Stage 3 serving smoke 216..219  (既存、変更しない)
Stage 4a screening    220..244  (既存、変更しない)

Issue #140 dataset    245..276  (32 hanchan、whole-hanchan split)
    TRAIN                 245..264  (20 hanchan)
    VALIDATION             265..270  (6 hanchan)
    TEST                   271..276  (6 hanchan)
Issue #140 serving smoke 277..280
Issue #140 screening      281..305

Arena #146 kan coverage   306..329  (既存、development-only、変更しない)
Arena #148 mix pilot      330..353  (既存、development-only、変更しない)

Issue #140 replacement TEST 354..359  (6 hanchan、TEST-only)
```

`354..359`はrebuilt candidate pairのfresh offline diagnostic populationである
（`replacement_test.py`）。当初は`306..311`がlockされていたが、そのlock記録後に
mergeされたArena #147が`306..329`を、#149が`330..353`をdevelopment populationと
して取得したため、`REPLACEMENT TEST SEED PLAN REFORMULATE`として`330..353`直後の
fresh contiguous rangeへre-lockしている。`protocol.py`のimport-time collision
assertionに加えて、sibling experimentのlocked populationとのcross-checkを
testで固定している。

## Macro-transition dataset contract

`transitions.py`が、同一actor・同一roundで次のeligible ordinary-discard
decisionまでを1 macro-transitionとするsupport-restricted fitted TD/Q-style
datasetを構築する。

```text
eligible discard state s_t
    -- yakuhai-call selected discard -->
    zero or more opponent / scaffold / ineligible decisions
    -->
next eligible discard state s_{t+1} for the same seat
or round terminal
```

**eligible ordinary discard**の定義（`activation.py`が正本）:

```text
1. legal_actionsの全要素がDiscardAction
2. legal action count >= 2
```

**score binding**は、hanchan最終局だけを表す
`LocalGameResult.seat_round_stats`（`RoundStatsCollector`が`start_kyoku`ごとに
resetするため）を使わない。roundごとのsettled scoreは、次のroundの最初の
decisionが持つ`PolicyInput.players[...].score`（全seat公開情報）、または
そのroundがhanchan最終局である場合は`LocalGameResult.scores`から一意に
bindする（`transitions.py`の`_group_by_round` / `build_macro_transitions`）。

```text
reward = (score_at_next_boundary - score_at_current_boundary) / 10000.0
gamma  = 1.0
```

datasetそのものはeligibility判定だけを行い、support gateはここでは適用しない
（`OfflineQDatasetWriter` / `arena-learned-policy-offlineq-dataset-v1`）。

## Behavior support gate

`support.py`がTRAIN上で1回以上selectされたexact discard vocabulary index
集合を`supported_indices`として固定し、TRAIN / VALIDATIONのeligible stateに
ついて「全legal discard indicesがTRAIN-supportedか」を報告する
（`build_support_gate_report()`）。TEST split rowはここで一切読まない。

runtime（serving hybrid）とtraining（Q armのTD target）はどちらも同じ
`is_support_complete()`判定を使う。Q armのTD targetでnext stateの全legal
actionがTRAIN-supportedでない場合は、silent extrapolationせずfail closedする
（`q_training.compute_td_targets`）。

## Training / checkpoint contracts

両armとも同じsplit tensor（`split_tensors.py`）と同じmodel capacity
（`lisjong_arena.learned_policy_stage2.network.create_model()`）、同じ
optimizer / batch size / epoch budget / dataloader seedを使う。

```text
Arm A (BC control)                         arena-learned-policy-offlineq-bc-checkpoint-v1
    loss       masked cross entropy over legal actions
    selection  lowest VALIDATION choice-row masked CE

Arm B (support-restricted Offline Q)       arena-learned-policy-offlineq-q-checkpoint-v1
    target     y = r                                              (terminal)
               y = r + gamma * max_a' Q_target(s', a')            (nonterminal;
                     a' restricted to next-state legal actions
                     that are also TRAIN-supported)
    target sync   epoch-level hard sync (target = online weights at each epoch start)
    loss       Huber(Q(s, a_behavior), y), delta = 1.0
    selection  fixed MAXIMUM_EPOCHS outer fitted-Q iterations、
               final iterationを無条件に採用する
               VALIDATION Huber lossはdiagnosticのみ
```

Arm Bのcheckpoint selectionは**cross-iteration VALIDATION loss比較を使わない**。
fitted-Qではouter iterationごとにbootstrap targetそのものが変わるため、
iteration間でVALIDATION Huber lossを比較して最小値を選ぶのはmethodologically
invalidである（PR #141 review round 1で確定）。したがって`train_q_model()`は常に
`MAXIMUM_EPOCHS`回のouter iterationを完走し、最終iterationのmodelを無条件に
採用する。`TrainingRun.selected_epoch`は常に`MAXIMUM_EPOCHS`と一致する。

Arm A (BC)側は分類objectiveであり、VALIDATION choice-row masked CEによる
checkpoint selectionは有効なまま維持する。

CQL等のconservative offline-RL regularizationはこのfirst childへ導入しない
（Issue #140 self-review）。

## TEST — one-shot

`exposure_evaluation.py`がTEST split上のdiagnosticsだけを計算する。

```text
BC   choice-row masked CE, choice exact agreement       (diagnostic; strength proxyではない)
Q    selected-action Huber residual, finite Q rate,
     predicted Q / target distribution                  (diagnostic; strength proxyではない)
```

TEST結果を見てobjective / model / seed / support ruleを変更しない。

## Replacement TEST — checkpoint-bound one-shot diagnostic

`replacement_test.py`は、TEST-onlyのpurpose-specific artifact
（`arena-learned-policy-offlineq-replacement-test-v1`）と、そのcheckpoint-bound
評価pathを所有する。original training datasetへappendせず、独立した
`artifact_identity`を持つ。

```text
locked replacement TEST seeds 354..359  (yakuhai-call x4 / 4p-red-half)
        |
        v
ReplacementTestWriter   ->  immutable / versioned artifact
        |                   purpose / protocol id / seeds / teacher identity /
        |                   source revisions / feature / vocabulary /
        |                   transition schema / row / terminal / nonterminal /
        |                   non-finite counts / artifact identity
        v
strict-loaded Q checkpoint
    +-- model
    +-- checkpoint identity-bound supported_indices
        |
        v
BC / Q one-shot diagnostics
```

**support setの正本はcheckpointである。** replacement TESTのsupport setを
TEST rowから計算し直さず、TRAIN `245..264`をregenして再計算することもしない。
`support_mask_from_checkpoint()`がQ checkpointへidentity-boundされた
`supported_indices` / `supported_indices_digest`からmaskを作る。これにより
replacement TESTは、実際にservingされるQ hybridと同一のsupport boundaryを
評価する。

`exposure_evaluation.evaluate_q_test()`はoriginal TRAIN tensorsから
support maskを再構成するoriginal TEST path専用であり、replacement TESTでは
使用しない。共通の計算部分は`evaluate_q_with_support_mask()`が持ち、
評価時にTRAIN rowsを要求しない。

CLI:

```text
generate-replacement-test    --artifact DIR --report FILE
evaluate-replacement-test    --artifact DIR --bc-checkpoint DIR
                             --q-checkpoint DIR --result FILE
```

`REPLACEMENT_TEST_SEEDS`だけをfail closedで受け付ける。generic arbitrary-seed
evaluation frameworkへ拡張しない。

### Hard validity gates

```text
checkpoint strict readback       PASS
feature identity                 PASS
vocabulary identity              PASS
transition validation            PASS
non-finite feature count         0
finite Q rate                    100%
unsupported bootstrap            0
```

いずれかがFAILなら`REPLACEMENT TEST INVALID`として停止し、strength screenへ
進まない。diagnostic数値（Huber loss、predicted Q / target distribution）には
performance thresholdを後付けせず、値を理由にretraining / epoch追加 /
reward変更 / CQL追加 / architecture変更 / seed追加を行わない。

## Serving hybrids

`serving.py`の`HybridPolicy`は、BC hybrid / Q hybridで完全に同じactivation /
fallback semanticsを1つの実装として共有する。

```text
DecisionContext
    |
    +-- eligible ordinary discard + support complete
    |       -> learned model (BC logits / Q values)
    |
    +-- otherwise
            -> yakuhai-call scaffold (game / seatごとにfresh instance)
```

checkpointはruntime構築時に1回だけloadし、decisionごとにreloadしない。
modelから`InternalAction`を直接constructせず、必ず`resolve_legal_action()`が
返す`decision.legal_actions`側のobjectをそのまま返す。BC hybridのsupport set
は外部から明示的に渡し、Q hybridはcheckpoint内蔵のsupport setが外部からの
期待値と一致することを要求する（`create_q_hybrid_runtime`）。

## Serving smoke

`smoke.py`が各smoke seedをdeterministic repeat（同一seedを2回実行して
一致を確認）し、activation / scaffold fallback / support fallback rateと
hanchan runtimeを報告する。smokeをmodel tuningへは使わない。

## Artifact retention gate

`retention.py`は`lisbun/lisjong-arena #138`（Stage 4a）が確立した
`resolve_retention_target()`のfail-closed判定（non-ephemeral / non-git /
write-once）をそのまま再利用し、本Issue固有のfreeze record
（BC + Q checkpoint identityを1つのdataset identityへbindし、
`strength_claim: null`を明示する）だけを所有する。宣言できる
non-ephemeral rootが無い実行環境では`ARTIFACT RETENTION BLOCKED`となる。

## Controlled Q-vs-BC ABBB strength screen

`strength.py`は既存`SingleRoundEvaluationPlan` / `run_single_round_evaluation()`
/ `SingleRoundStrengthArtifact`をそのまま再利用する。ABBB protocol semantics、
rotation、statistics、artifact schemaを再実装しない。

```text
candidate = Q hybrid   (offlineq-q:<Q checkpoint identity>)
baseline  = BC hybrid  (offlineq-bc:<BC checkpoint identity>)
game mode = 4p-red-single (protocol invariant)
seeds     = 281..305 (25 seed blocks x 4 rotations = 100 games)
```

`classify_value_q_signal()`が既存seed-block normal-approx 95% intervalを
分類する。

```text
VALUE/Q OBJECTIVE SIGNAL        interval lower bound > 0
VALUE/Q OBJECTIVE NEGATIVE      interval upper bound < 0
VALUE/Q OBJECTIVE INCONCLUSIVE  otherwise
```

## CLI usage

```bash
python -m lisjong_arena.learned_policy_offline_q generate  --dataset DIR --report FILE
python -m lisjong_arena.learned_policy_offline_q train-bc  --dataset DIR --checkpoint DIR
python -m lisjong_arena.learned_policy_offline_q train-q   --dataset DIR --checkpoint DIR
python -m lisjong_arena.learned_policy_offline_q test      --dataset DIR \
    --bc-checkpoint DIR --q-checkpoint DIR --result FILE
python -m lisjong_arena.learned_policy_offline_q smoke     \
    --bc-checkpoint DIR --q-checkpoint DIR --report FILE
python -m lisjong_arena.learned_policy_offline_q freeze    \
    --bc-checkpoint DIR --q-checkpoint DIR \
    --retention-backend <operator-declared backend identity> \
    --retention-root    /non-ephemeral/root/outside/git \
    --retention-key     offlineq/<run name>
python -m lisjong_arena.learned_policy_offline_q screen    \
    --bundle /non-ephemeral/root/outside/git/offlineq/<run name> \
    --artifact FILE --result FILE
```

生成したdataset、checkpoint、artifactはrepository外のnon-ephemeral
retention先へ出力し、Gitへcommitしない。

## Exhaustive outcomes

```text
VALUE/Q OBJECTIVE SIGNAL
VALUE/Q OBJECTIVE NEGATIVE
VALUE/Q OBJECTIVE INCONCLUSIVE
OFFLINE Q DATA COVERAGE BLOCKED
OBJECTIVE REFORMULATE
ARTIFACT RETENTION BLOCKED
SERVING CONTRACT INVALID
STOP / INVALID
```

`VALUE/Q OBJECTIVE SIGNAL`は「同じbounded representation / scaffold条件で、
outcome-aware Q objectiveを次段へ進める根拠が得られた」という意味だけであり、
`yakuhai-call`超え、production adoption、Mortal-style architecture adoptionを
意味しない。個別runのexhaustive outcomeとnumeric resultはIssue #140を正本とする。

## Successor — artifact-only failure diagnosis

`#140`が`VALUE/Q OBJECTIVE NEGATIVE`で終了した後のfailure-mechanism診断は、
`lisbun/lisjong-arena #152`が別Issueとして扱う。新しい学習・game生成・strength
evidenceを作らず、`#140`がretainしたartifactだけを使うbounded diagnosticであり、
contractは[`docs/learned-policy-offline-q-diagnosis.md`](learned-policy-offline-q-diagnosis.md)
を正本とする。`#140`のprotocol、seed population、objective semanticsは
そちらでも変更しない。
