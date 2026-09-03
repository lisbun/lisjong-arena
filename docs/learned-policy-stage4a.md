# Learned Policy Stage 4a — bounded strength screening

`lisjong_arena.learned_policy_stage4a`は、retained Learned Policy candidateを
既存ABBB protocolでboundedにscreeningするArena-local orchestrationである。

本書が扱うのは**再利用するtechnical semantics**（locked protocol、candidate
freeze / retention contract、candidate identity derivation、screening
classification、CLI usage）だけである。individual runのresultはbounded Issue
とimmutable Arena artifactを正本とし、本書へ数値を転記しない。

```text
individual strength result
    = lisbun/lisjong-arena #138 + SingleRoundStrengthArtifact

reusable technical semantics
    = 本書
```

## Scope

Stage 4aは**promotion gateではない**。目的はvariance、runtime、effect
directionをsmall screeningで確認することであり、このscreening単独を
promotion evidenceとして扱わない。model architecture変更、HPO、rare-action
rescue、teacher / comparator変更、production adoptionはscope外である。

## Locked protocol

`protocol.py`がlocked valueをcodeとして固定する。結果を見てここを変更しない。

```text
evaluation protocol   abbb-single-round-v1 (既存)
game mode             4p-red-single        (protocol invariant)
ordered seeds         220..244
seed blocks           25
rotations / seed      4
games / comparator    100

primary   baseline    yakuhai-call   (Stage 2 teacher = current strength baseline)
secondary baseline    two-step       (low-cost comparator)

generation TRAIN      200..209
generation VALIDATION 210..212
held out              213..215 (Stage 2 TEST) / 216..219 (Stage 3 smoke)
model / training      Stage 2 locked protocol (8204 -> 128 ReLU -> 802)
```

`require_candidate_generation_seed()`がheld-out seedをgeneration pathから
fail closedし、`require_screening_seeds()`がordered screening population以外を
拒否する。planning APIとexecution APIはどちらもseed引数を持たないため、
resultを見てからseedを追加・差し替えする入口自体が存在しない。

primaryとsecondaryは無条件のpredeclared planとして実行する。primary resultを
見てsecondaryをskipする経路は持たない。両comparisonはbaseline identityが
異なるため、cumulative artifactへ合成しない（`merge_single_round_artifacts()`
が異なるbaselineをfail closedする）。

## Candidate freeze / retention contract

Gate 0は**strength result exposureより前に**candidateを固定する。順序は
retention先の検証 -> 生成 -> freeze record -> strict readbackであり、
retention先を確定できない場合はcandidateを生成しない。

retained bundleのlayoutは次のとおりである。

```text
<retention root>/<retention key>/
    checkpoint/                 exact retained weights + manifest
    candidate-freeze.json       Stage 4a-owned freeze record
    primary-yakuhai-call.json   ABBB strength artifact (immutable)
    secondary-two-step.json     ABBB strength artifact (immutable)
    stage4a-result.json         screening result document
```

`resolve_retention_target()`が機械的にfail closedするのは次である。

- declared retention backendが非空のexplicit identityであること
- retention rootが実在するabsolute directoryであること
- retention rootがtemporary directory配下でないこと
- retention rootがGit work tree内でないこと（repositoryへweights /
  artifactを置かない）
- bundle destinationがまだ存在しないこと（write-once）

**storage自体のdurabilityはcodeでは証明できない。** declared backendは
caller / operatorの宣言であり、freeze recordへlogical identityとして記録して
監査可能にする。machine-localなabsolute rootはfreeze recordにもIssueにも
出さない。宣言できるnon-ephemeral rootが無い場合、`freeze`は
`ARTIFACT RETENTION BLOCKED`で停止し、strength runへ進まない。

### Checkpoint formatの再利用

on-diskのcheckpoint directory formatとそのstrict loaderは、Stage 3が確立した
implementation primitiveをそのまま使う。generic checkpoint schemaの再設計は
行わない。

Stage 4a固有のfreeze identityとpurposeは`candidate-freeze.json`が所有する。
したがって次を混同しない。

| | 由来 | 役割 |
| --- | --- | --- |
| historical Stage 3 fixture | `#136` / PR #137 | serving integration専用。strength claimを載せない |
| Stage 4a candidate | Stage 4a Gate 0で新規生成 | freeze recordがpurposeとidentityを宣言したscreening candidate |

freeze recordは`strength_claim: null`を明示し、strength evidenceがABBB
artifact側にしか存在しないことをschemaとして固定する。

### Freeze record

`candidate-freeze.json`は次をfreezeする。missing / unknown fieldとschema・
purpose・population mismatchはload時にfail closedする。

```text
freeze_record_schema_version / protocol_id / purpose
candidate_identity
checkpoint  schema_version / identity / dataset_identity
            weights_sha256 / weights_bytes / parameter_count
            selected_epoch / selected_validation_choice_masked_ce
            feature / vocabulary / model / training
generation  train_seeds / validation_seeds
            excluded_stage2_test_seeds / excluded_stage3_serving_seeds
            teacher_identity / teacher_source_revision / row_count
source_revisions  lisjong-arena / lisjong / lisjong-engine ほか
retention   backend / key / checkpoint_relative_path
strength_claim = null
```

`strict_readback()`はretained checkpointをStage 3 strict loaderで読み直し、
`verify_freeze_binding()`でschema version、checkpoint identity、dataset
identity、weights digest、weights byte countを照合する。1つでも食い違えば
Gate 0はpassしない。

## Candidate identity

ABBB candidate identityはfree-form aliasにしない。strict loadした
checkpoint identityから機械的に導出する。

```text
learned-stage4a:<checkpoint identity>
```

`derive_candidate_identity()`はlowercase sha256 hex以外を拒否するため、
checkpointを差し替えればcandidate identityも必ず変わる。artifact保存後の
readbackでは、artifactの`candidate_identity`がloaded checkpointからの導出値と
一致することをfail closedで確認する。

## Policy integration

candidate PolicySpecの`factory`はStage 3 serving runtimeの
`create_policy`である。

- checkpointはruntime構築時に1回だけloadし、decisionごとにreloadしない
- Policy instanceはgame / seatごとにfactoryからfresh生成する
- 共有するのはimmutableなeval-mode modelだけである
- legal mask、canonical `resolve_legal_action()`、`execute_policy()`の
  validation境界はStage 3実装をそのまま通る

Stage 4aのためにlisjong-owned Policy実装へ何も移さない。

## Execution mode

serial実行（workers=1）を第一選択とする。既存ABBB semanticsを最小変更で
再利用でき、Learned runtimeのspawn serialization / process-local cacheという
別scopeを導入せず、wall-clock / process CPU measurementの境界も明瞭になる。
performanceだけを理由に新しいparallel abstractionを導入しない。

## Metrics and screening classification

canonical summaryは既存`summarize_single_round_strength()`が所有し、Stage 4a
では再実装しない。artifact保存後にraw game resultsから再生成し、保存値と
一致することを確認する。

candidate-only Mahjong metrics（win / deal-in / tenpai等）は
`candidate_only_mahjong_metrics`として分離し、baselineとの差として表現しない。
strengthとruntime costも別axisとして保持し、単一scoreへ混ぜない。

screening classificationは既存seed-block normal-approx 95% intervalを使う。

```text
POSITIVE SIGNAL   interval lower bound > 0
NEGATIVE SIGNAL   interval upper bound < 0
UNRESOLVED        otherwise
```

これはStage 4a限定のdescriptive effect-direction classificationであり、
universal promotion thresholdではない。intervalが定義されない場合は
UNRESOLVEDへ丸めずfail closedする。

## Exhaustive outcomes

```text
primary POSITIVE
    -> ADVANCE TO CONFIRMATORY STRENGTH EVIDENCE
primary NEGATIVE かつ secondary POSITIVE
    -> LOW-COST VALUE CANDIDATE
primary NEGATIVE かつ secondary NEGATIVE
    -> DO NOT ADVANCE
その他のvalid combination
    -> INCONCLUSIVE

retained candidateをrun前に固定できない
    -> ARTIFACT RETENTION BLOCKED
checkpoint-bound candidateを既存ABBB contractへ安全に接続できない
    -> EVALUATION CONTRACT REFORMULATE
provenance / legality / determinism / artifact integrity / protocol validityが壊れる
    -> STOP / INVALID
```

`decide_outcome()`が機械的に決めるのは前半4つだけである。残りは
measurementだけでは判定できないため、Issue上のresult recordで明示する。

`LOW-COST VALUE CANDIDATE`はadoption decisionではない。positive / unresolved
から追加evidenceが必要な場合は、**fresh non-overlapping seedsを使う別の
bounded Issue**を作る。同じIssue内でseedを追加しない。

## Running the experiment

生成checkpoint、strength artifact、result documentはrepository外の
non-ephemeral retention先へ出力し、Gitへcommitしない。

```bash
python -m lisjong_arena.learned_policy_stage4a freeze \
    --retention-backend <operator-declared backend identity> \
    --retention-root    /non-ephemeral/root/outside/git \
    --retention-key     learned-stage4a/<run name>

python -m lisjong_arena.learned_policy_stage4a screen \
    --bundle /non-ephemeral/root/outside/git/learned-stage4a/<run name>
```

`freeze`と`screen`はどちらも`collect_execution_provenance()`を経由するため、
source treeがdirtyな場合にfail closedする。実行はcommit後に行う。

## Current status

`lisbun/lisjong-arena #138`のStage 4a screeningは、実行environmentに宣言可能な
non-ephemeral retention先が無かったため`ARTIFACT RETENTION BLOCKED`で停止した。
strength evaluationは実行しておらず、Stage 4a strength resultは存在しない。
実行条件、確認したpreflight値、unblock条件はIssue #138を正本とする。
