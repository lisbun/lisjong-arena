# 聴牌PUSH/FOLDの対比較source 本生成（#475）

owner: lisbun/lisjong#254（設計5節）。wire契約は lisbun/lisjong#288。producerと記録の決め方は
[#476の文書](tenpai-push-fold-source-476.md)のとおりで、ここでは繰り返さない。
Arenaは対局の実行と局の結果の記録だけを担う。表のfit・bucketの境界・supportの下限・中止条件の判定は
lisjongが所有する。**半荘の強さの評価ではない。**

入口は`scripts/generate_tenpai_push_fold_source_475.py`。#476のproducerを、別のseedと予約で実行するだけである。

## 規模の決定（2026-10-09、ユーザー）

#476のpilot（16半荘）と lisjong#288 の件数の確認を見て、**400半荘（train 200 / valid 200）、AWS `c7i.8xlarge` 1台**に決めた。

| 材料 | 値 |
| --- | --- |
| pilotのゲート判断 | 1半荘あたり (A) 3.4、(B) 0.06、(C) 8.8。降りる候補ありは (A) 2.8、(C) 5.4 |
| pilotのvalid 4半荘で`V`が降りると判定した判断 | (A) 8、(B)(C) 2（4半荘分なので粗い） |
| 400半荘での見込み | ゲート判断 約4,900、降りる側の実行 約3,300。validの`V`が降りる判定は (A) 約400、(B)(C) 約100 |
| 計算時間 | pilotで1半荘あたり379秒（WSL）。400半荘で約42 CPU時間 |

見込みはpilotの比率を掛けただけの値で、保証ではない。(B)は件数が少なく、(B)単独の表は作れない可能性が高い。

## 固定値

`scripts/generate_tenpai_push_fold_source_475.py`（引数では変えられない）

| 項目 | 値 |
| --- | --- |
| seed | 939000..939399（engine domain `lisjong-engine-project-standard-v1-hanchan-v1`）。1 seed = 1半荘、rotationなし |
| 分割 | train 939000..939199、valid 939200..939399（半荘単位） |
| 予約 | owner `lisbun/lisjong-arena#475`、protocol `tenpai-push-fold-source-v1`、population `tenpai-push-fold-source-400-hanchan`、split `TRAIN200-VALID200`。`arena_revision`は実行するmerge commit |
| 使用済み範囲 | #476の一覧に 938000..938015（#476のpilot）を加えたもの。pilotのseedは再利用しない |
| Policy / rule | Champion（`PlacementAwareSpeedCallPolicy`）×4、`RuleSet.default()` |
| ゲート | lisjongの`tenpai-push-fold-gate.conditions-1-7.v1`（`evaluate_tenpai_gate()`） |
| 待ちモデル | #245の`selection.json`、SHA-256 `14475264d7fe4137a9ac8a23ee1d27b420bff2f4434c6e93ca72ecfcf24ccc38` |
| lisjong / lisjong-engine | `6be9b906bcde7b1de8b572fd052464e57421469f` / `91af75e3aa11520c3b0543719dc74bb4c517ee06`（pin） |
| native wheel | `lisjong_native-0.1.0-cp314-cp314-manylinux_2_28_x86_64.whl`、SHA-256 `0c0e3dc475807806b0692336c31dff69da1e095741f7f3ce154f0f496cf6833e` |
| runtime | 通常版CPython 3.14、`LISJONG_SHANTEN_BACKEND=rust` |
| worker | 32固定 |

Arenaのrevision（merge commit）とallocation identityは、予約後に#475へ記録する。

## 出力

#476と同じ3 fileと`generation.json`。AWSでは`source/`にこの4 fileと`SHA256SUMS`を置く。
実行中は、1半荘（対照と、その降りる側の実行すべて）が終わるごとに進捗を1行書く。

失敗・中断した実行は`generation.json`を持たない。resume・reuse・部分採用はしない。
対局を開始した後に失敗した場合は、同じallocationで再実行せず、allocationをRETIREDにして
新しいseed範囲で予約し直す。prologue（install・`check-allocation`）で止まった場合はseedを消費していないので、
原因を直して同じallocationで起動し直す。

ダブロンの放銃の扱いは#476のとおり（最も近い和了者と支払いの合計）。件数は`generation.json`の
`multiple_ron_deal_ins`に出る。

## AWS実行

汎用launcher `scripts/aws/lisjong-ec2.ps1`に`scripts/aws/bootstrap-tenpai-push-fold-source-475.sh`を渡す。
`-InputFile`は、wheel・共通prologue `scripts/aws/measurement-source-prologue.sh`・`selection.json`の3つ。
bootstrapはwheelと`selection.json`のSHA-256を照合してから対局を始める。

1. 本経路をmergeし、merge commitへseed registry workflowでRESERVEDを予約する。

   ```text
   gh workflow run seed-registry.yml --repo lisbun/lisjong-arena \
     -f operation=reserve -f owner_issue=lisbun/lisjong-arena#475 \
     -f protocol=tenpai-push-fold-source-v1 \
     -f seed_domain=lisjong-engine-project-standard-v1-hanchan-v1 \
     -f purpose=tenpai-push-fold-source -f population=tenpai-push-fold-source-400-hanchan \
     -f split=TRAIN200-VALID200 -f seeds=939000..939399 \
     -f arena_revision=<merge-sha> -f protocol_revision=tenpai-push-fold-source-v1 \
     -f provenance_reference=https://github.com/lisbun/lisjong-arena/issues/475
   ```

2. #475へ、seed・分割・allocation・3 repositoryのrevision・wheelとモデルのSHA-256を記録する（事前登録）。
3. Preflight（EC2を作らず、課金しない）で構成・料金・quotaを`plan.json`に固定し、その値で起動の承認を取る。

   ```powershell
   .\scripts\aws\lisjong-ec2.ps1 -Action Preflight -AwsProfile <profile> `
     -Label tenpai-push-fold-source-475 -InstanceType c7i.8xlarge -Workers 32 `
     -Bootstrap .\scripts\aws\bootstrap-tenpai-push-fold-source-475.sh `
     -BootstrapArgs @('--arena-revision','<merge-sha>','--allocation-identity','<identity>') `
     -InputFile @('<wheel directory>\lisjong_native-0.1.0-cp314-cp314-manylinux_2_28_x86_64.whl',
                  '.\scripts\aws\measurement-source-prologue.sh',
                  '<selection directory>\selection.json') `
     -EstimatedRuntimeHours @(1.5, 3.5) -FailSafeHours 5 -CostBudgetUsd 12
   ```

   時間は見積りで、実測はない。根拠は、#257の400半荘（Champion×4、32 workers）が1,647秒だったことと、
   pilotで1半荘の合計が対照の約4.9倍だったことである（1,647秒 × 4.9 ≒ 2.2時間）。
4. 承認後に`-Action Launch -Plan <plan.json>`、完了後に`-Action Collect`。
5. 回収した`source/`を`SHA256SUMS`で照合し、lisjongの`read_source()`で読めることを確認する。
   成功artifactを保持してから、allocationをCOMMITTEDにする。
6. #475へ結果を記録する: 件数、時間、費用、3 fileのbytes・行数・SHA-256。

**未実証:** このbootstrapはAWS上で未実行（`bash -n`と、prologueがない場合の拒否だけを確認）。
Launch後は`environment/allocation.json`で、installと`check-allocation`まで進んだことを確認する。

生成物はGitへ入れない。
