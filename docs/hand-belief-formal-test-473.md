# lisjong#260 formal test用200半荘sourceの生成（lisjong-arena#473）

owner: lisbun/lisjong#260（形別待ちテーブルの推定器）。source契約は lisbun/lisjong#256 v1
（[HandBelief手牌正解source](hand-belief-source-453.md)）。Arenaは対局の実行と観測事実の記録だけを担い、
ラベル・評価・合否はlisjongが所有する。**強さの評価ではない。**

生成経路は[#464](hand-belief-formal-test-464.md)と同じで、seedと予約用の文字列だけが違う。
実行前の照合、出力の形、`generation.json`がなければ評価へ進まないこと、resume・reuseがないことは
#464の文書のとおりで、ここでは繰り返さない。

## 固定値

`scripts/generate_hand_belief_formal_test_473.py`（引数では変えられない）

| 項目 | 値 |
|---|---|
| seed | 937000..937199（engine domain `lisjong-engine-project-standard-v1-hanchan-v1`）。1 seed = 1半荘、rotationなし |
| 分割 | 全件`test`。train / validは空 |
| 生成単位 | 100半荘ずつ2個のv1 source。`unit-0` = 937000..937099、`unit-1` = 937100..937199 |
| 予約 | owner `lisbun/lisjong#260`、protocol `wait-shape-formal-test-260-v1`、population `wait-shape-formal-test-260-200-hanchan`、split `TEST200`。`arena_revision`は実行するmerge commit |
| 使用済み範囲 | #464の一覧に 934000..934199（lisjong#258 / #259 のformal test）を加えたもの |
| Policy / rule | `PlacementAwareSpeedCallPolicy`（Champion）×4、`RuleSet.default()` |
| runtime | `LISJONG_SHANTEN_BACKEND=rust`、[現行pin](lisjong-native-wheel-current.md)のwheel、pin済みlisjong / lisjong-engine |
| worker | 32固定 |

## producer identity

manifestの`producer`には実際に実行したrevisionを書く。lisjong#260 のselectionを作った#257のproducerとは
revisionの3 fieldが異なるので、lisjong#279 の方式で、**生成前に** lisjong#260 へformal test用producerを
登録する。登録値は、本経路のmerge commit、現行pinのlisjong / lisjong-engine、`PlacementAwareSpeedCallPolicy`
の4 fieldである。lisjongは、この登録と全fieldで一致するsourceだけを評価する。

現行producerと#257のproducerの同一性の確認（`compare-reference`）は#464で実施済みで、このIssueでは
再実行しない。#464以降、Arenaの生成codeとpinは変わっていない。

## 失敗時の扱い

#464と同じ。対局を開始した後に失敗・中断した場合は、同じallocationでの再実行はせず、allocationを
RETIREDにし、新しいseed範囲を予約して lisjong#260 の事前登録を更新する。prologue（install・
`check-allocation`）で止まり対局が始まっていない場合は、seedを消費していないので、原因を直して
同じallocationで起動し直す。

## AWS実行

汎用launcher `scripts/aws/lisjong-ec2.ps1`に`scripts/aws/bootstrap-hand-belief-formal-test-473.sh`を渡す。
構成は#464と同じ（`c7i.8xlarge`、`-Workers 32`、wheelと共通prologueを`-InputFile`で渡す）。

1. 本経路をmergeし、merge commitへseed registry workflowでRESERVEDを予約する。

   ```text
   gh workflow run seed-registry.yml --repo lisbun/lisjong-arena \
     -f operation=reserve -f owner_issue=lisbun/lisjong#260 \
     -f protocol=wait-shape-formal-test-260-v1 \
     -f seed_domain=lisjong-engine-project-standard-v1-hanchan-v1 \
     -f purpose=wait-shape-formal-test -f population=wait-shape-formal-test-260-200-hanchan \
     -f split=TEST200 -f seeds=937000..937199 \
     -f arena_revision=<merge-sha> -f protocol_revision=wait-shape-formal-test-260-v1 \
     -f provenance_reference=https://github.com/lisbun/lisjong-arena/issues/473
   ```

2. lisjong#260 へ、seed・allocation・formal test用producer（fileのSHA-256）を記録する。
3. Preflight（EC2を作らず、課金しない）で構成・料金・quotaを`plan.json`に固定する。

   ```powershell
   .\scripts\aws\lisjong-ec2.ps1 -Action Preflight -AwsProfile <profile> `
     -Label hand-belief-formal-test-473 -InstanceType c7i.8xlarge -Workers 32 `
     -Bootstrap .\scripts\aws\bootstrap-hand-belief-formal-test-473.sh `
     -BootstrapArgs @('--arena-revision','<merge-sha>','--allocation-identity','<identity>') `
     -InputFile @('<wheel directory>\lisjong_native-0.1.0-cp314-cp314-manylinux_2_28_x86_64.whl',
                  '.\scripts\aws\measurement-source-prologue.sh') `
     -EstimatedRuntimeHours @(0.25, 1) -FailSafeHours 2 -CostBudgetUsd 5
   ```

   参考実績は#464の200半荘・32 workersで生成wall 831秒、上限算定0.57 USD（同じpin・同じ構成）。
4. `-Action Launch -Plan <plan.json>`、完了後に`-Action Collect`。
5. 成功artifactを保持・hash確認してから、allocationをCOMMITTEDにする。

生成物はGitへ入れない。
