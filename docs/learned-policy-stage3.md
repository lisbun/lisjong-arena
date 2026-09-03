# Learned Policy Stage 3 — serving-realistic candidate

本書は`lisjong_arena.learned_policy_stage3`が実装するbounded serving
integrationの契約を示す。実験そのものの目的・acceptance criteria・結果は
[lisjong-arena #136](https://github.com/lisbun/lisjong-arena/issues/136)と
親roadmap [lisjong-project #40](https://github.com/lisbun/lisjong-project/issues/40)を正本とする。

## Scope

このpackageはStage 3 serving integration 1本のためのexperiment-local harnessである。

```text
explicit retained checkpoint path
    -> strict artifact loader
    -> experiment-local Learned Policy adapter
    -> actual player-safe PolicyInput
    -> arena-policy-input-feature-v1 (8204)
    -> learned logits (802)
    -> legal mask from the current decision
    -> resolve_legal_action() -> canonical InternalAction
    -> execute_policy() validation boundary
    -> actual 4p-red-half game runner
```

**game strength、decision quality、production promotion、model improvementは
本packageの目的ではない。** 検証対象はartifact -> serving boundaryだけである。

次は導入しない。

- generic ML serving framework / model registry / artifact registry / cloud storage
- automatic latest-checkpoint discovery
- production lisjong Policy adoption
- value head、self-play、RL、HandBelief integration
- strength comparison、baseline promotion

`torch`はloader / adapter / fixture pathだけのlazy importである。protocol値の
参照とseed guardはML runtimeを要求しない。

## Locked protocol

`protocol.py`がlocked valueをcodeとして固定する。結果を見てここを変更しない。

| 項目 | Locked value |
| --- | --- |
| protocol ID | `arena-learned-policy-stage3-serving-v1` |
| ordered seeds | `216..219`（4 hanchan） |
| game mode | `4p-red-half` |
| population | `learned candidate x4` |
| role | `SERVING-INTEGRATION ONLY` |
| determinism run count | `2` |
| fixture TRAIN / VALIDATION | `200..209` / `210..212` |
| excluded Stage 2 TEST seeds | `213..215` |

feature schema、action vocabulary、model / training configはStage 3が所有せず、
`lisjong_arena.learned_policy_input`、`lisjong.action_vocabulary`、
`lisjong_arena.learned_policy_stage2`をsingle source of truthとして参照する。

serving seedはStage 2 population `200..215`と交差しない。`require_serving_seed()`と
`require_fixture_seed()`がこれをcodeとして固定し、`require_fixture_seed()`は
Stage 2 TEST hanchanをfail closedで拒否する。

## Artifact handoff — Path A / Path B

Stage 2 artifactはrepository外で生成されたため、exact checkpoint bytesが現存
するとは限らない。`artifact_class`がその由来を区別し、両者をidentity上で混同
させない。

| artifact class | checkpoint schema version | 意味 |
| --- | --- | --- |
| `STAGE2_RETAINED` | `arena-learned-policy-stage2-checkpoint-v1` | Path A: exact Stage 2 retained checkpoint |
| `STAGE3_FIXTURE` | `arena-learned-policy-stage3-serving-fixture-v1` | Path B: Stage 3 development-only serving fixture |

`artifact_class`はschema versionから導出したうえで、そのclassのprovenanceを
exact一致で検証してはじめて確定する。呼び出し側の申告では決めない。

### Path Aのexact identity

Path Aとして受理するのは、次の3値をすべて満たすexact Stage 2 artifactだけである。
Stage 2 schema versionとlocked model configを名乗るだけの別checkpointは
`STAGE2_RETAINED`にならない。

```text
checkpoint_identity   bca0a813296a41737acd2460b846d69b5165a2941fbc1d9a741914ef874714de
weights_sha256        8955144775b067f4767088b23cac97d391b6acfb6ae9a587f52d1aa4c50cfe6d
dataset_identity      bdd83880c9d588f2566608377d081935f1f6792f4fbff56c3b69a82ac0ecb29c
```

### Path B fixtureの制約

`fixture.py`が構築するfixtureは次を守る。

- Stage 2 locked architecture / training configを変更しない
- TRAIN / VALIDATION membership `200..212`だけを再生成・再学習する
- Stage 2 TEST hanchan `213..215`をrecord / load / selection / validationの
  いずれでも使わない
- checkpoint selectionはVALIDATION choice-row masked CEのみ
- **新しいStage 3 identity**（別schema version + `fixture` provenance block）を
  与え、`stage2_checkpoint_identity`は必ず`null`とする
- **このfixtureからprediction quality / agreement / strength claimを作らない**

loaderは`fixture` blockのprovenanceをexact一致で検証する。excluded TEST seeds
との非交差だけでは足りない。self-consistentなmanifestを作るだけで別population
のartifactをserving candidateとして通せてしまうためである。

```text
protocol_id                   == arena-learned-policy-stage3-serving-v1
train_seeds                   == 200..209
validation_seeds              == 210..212
excluded_stage2_test_seeds    == 213..215
teacher_identity              == yakuhai-call
teacher_source_revision       == locked Stage 2 teacher revision
origin                        == stage3-development-only-serving-fixture
stage2_checkpoint_identity    is null
```

Stage 2 schemaが`fixture` blockを持つ場合もfail closedする。

## Strict artifact loader

`load_serving_checkpoint(path)`はexplicit pathだけを読む。implicitな
latest-file discoveryもparent directory探索も行わない。

writeとreadの双方でfail closedに検証する。

- directory shapeが`manifest.json` + `weights.pt`と完全一致すること
- manifest bytesがcanonical JSONであること
- checkpoint schema versionがsupported classのいずれかであること
- feature schema ID / fingerprint / dimensionがlocked identityと一致すること
- action vocabulary ID / size / fingerprintがlocked identityと一致すること
- locked model / training config、parameter count `1,153,698`
- actual parameter shapeが`8204 -> 128 -> 802`であること
- parameterがすべてfiniteであること
- weights byte count / sha256の一致
- `checkpoint_identity`のself-consistency
- corrupt / truncated weightsは、digestを壊れたbytesへ合わせてもstrict
  `state_dict` loadで拒否すること

loadしたmodelは`.eval()`かつ全parameterが`requires_grad=False`であり、CPU上に
ある。

## Serving adapter

`LearnedServingPolicy`はlisjongの`Policy` structural protocolへ適合する
Arena-local experiment実装である。

- 意思決定入力は`DecisionContext`だけであり、feature化には当該seatの
  player-safe `PolicyInput`しか渡さない
- featureはStage 1 encoder（`build_policy_input_feature()` / `tensor_values()`）を
  single source of truthとする。Stage 3側でfeatureを再定義・再計算しない
- legal maskはcurrent decisionの`build_legal_action_mask()`から生成する
- 選択はmasked log-softmax上のargmaxであり、illegal indexへ確率を割り当てない
- 選択indexは`resolve_legal_action()`でcanonical actionへ解決する。
  `InternalAction`を自前でconstructしない
- 返却値は`decision.legal_actions`側のobjectそのものであり、`execute_policy()`の
  validation境界を迂回しない

学習例0件のaction familyがlegalになった場合でも、mask対象から除外しない。
model logitsはlegal set上で比較し、その結果のqualityはStage 3の評価対象外と
して扱う。

### Lifecycle

- model weightsは`ServingRuntime`が1回だけloadし、decisionごとにreloadしない
- `LearnedServingPolicy`はgame / seatごとにfactoryからfresh instanceを生成し、
  seat間・game間で共有しない。共有するのはimmutableなeval-mode modelだけである
- 実行は`torch.inference_mode()`、CPU固定、torch threads `1`、deterministic
  algorithms有効。CUDAは暗黙利用しない

Policy instanceが保持するのは不変なmodel referenceと、最終選択へ影響しない
latency measurementだけである。前回decisionの結果、呼び出し順序、hidden PRNG
状態には依存しない。

## Serving smoke

`smoke.py`はlocked planを`DETERMINISM_RUN_COUNT`回実行する。

- seeds `216..219` / learned candidate x4 / `4p-red-half`
- 各hanchan・各seatでPolicy instanceをfactoryから新規生成する
- 失敗gameをskipして成功分だけを返さない。1 hanchanでも完走しなければsmoke
  全体をfail closedする
- infrastructure failure以外でseedを置換しない。rare family 0件でもseedを
  追加しない

### Independent re-verification

実行後、記録された`LocalGameInspection`から独立に再照合する。adapter側の主張を
そのまま信じない。

- 実行されたactionが当該decisionの`legal_actions`のobjectそのものであること
- `encode_action()`したindexが当該decisionのlegal mask上でlegalであること
- `resolve_legal_action()`が同じactionを返すこと
- seatごとのexecuted index列が、そのseatのPolicyが選択したindex列と一致すること

trace digestは`(step_ordinal, actor_seat, vocabulary index)`というobjective
execution factだけのsha256である。shanten、ukeire、danger、候補評価、選択理由
などPolicy-internal analysisは観測へ混ぜない。

### Determinism

同一planを2回実行し、次の一致を要求する。

```text
scores / ranks / steps / decisions / trace_digest
```

環境差でbitwise logitsが保証できない場合、推測でtoleranceを広げず、実測を
resultへ分類する。

## Measurements

```text
artifact load wall-clock / CPU time
artifact bytes
peak process RAM after load / after smoke (best-effort)
first-decision latency
warm feature encode latency
warm model-forward latency
warm mask + select + resolve latency
warm full choose-action latency
per-hanchan wall-clock / CPU-seconds
```

first decisionはwarm統計から分離する。peak RAMはStage 2と同じ
`peak_process_ram_bytes()`のbest-effort値であり、process全体のpeak RSSなので
`resource`を利用できないplatformでは`None`になる。

process-wide peak RSSは単調増加するため、測定点を分けないとlabelが実態と
ずれる。`peak_process_ram_bytes_after_load`は`create_serving_runtime()`が
checkpoint load直後に確定させ、`peak_process_ram_bytes_after_smoke`はsmoke
完走後に取得する。前者をrunner実行後に測ると、artifact load後のRAMではなく
game execution込みのprocess peakになってしまう。

runner costとinference costのmeasurement boundaryを明記し、training costとは
混ぜない。

## Decision rule

```text
hard gate failed（safety / provenance / determinism invalid）
    -> STOP / INVALID
artifactがrecover / reconstructできずserving検証不能
    -> ARTIFACT HANDOFF BLOCKED
correctness passだがlocal serving costが明確なblocker
    -> LATENCY BLOCKED
retained model contract自体にconcrete flaw
    -> ARTIFACT CONTRACT REFORMULATE
stable Policy interfaceへの接続にlisjong-owned変更が必要
    -> POLICY INTEGRATION REFORMULATE
all hard gates pass
    -> SERVING CANDIDATE READY
```

`run_serving_smoke()`が機械的に判定するのは`SERVING CANDIDATE READY`と
`STOP / INVALID`だけである。他のoutcomeはmeasurementだけでは自動判定できない
judgement outcomeであり、Issue上のresult recordで明示する。

## Running the experiment

生成checkpoint、fixture report、smoke resultはrepository外へ出力し、Gitへ
commitしない。

```bash
# Path B: exact Stage 2 checkpointが失われている場合のみ
python -m lisjong_arena.learned_policy_stage3 fixture \
    --checkpoint /path/outside/git/stage3-fixture \
    --report     /path/outside/git/fixture.json

python -m lisjong_arena.learned_policy_stage3 smoke \
    --checkpoint /path/outside/git/stage3-fixture \
    --result     /path/outside/git/smoke.json
```

`fixture`は`collect_execution_provenance()`を経由するためsource treeがdirtyな
場合にfail closedする。fixture生成はcommit後に実行する。`provenance`の明示
指定はfixture / testのためだけの入口である。

Path Aのexact Stage 2 checkpointが存在する場合、`fixture`は実行せず`smoke`へ
そのpathを渡す。loaderが`artifact_class`を`STAGE2_RETAINED`として解決する。
