# lisjong#258・#259 共用formal test用200半荘sourceの生成（lisjong-arena#464）

owner: lisbun/lisjong#258（期待枚数・赤5）と lisbun/lisjong#259（副露者の待ち）。両者のformal testが
同じsourceを共用する（[#258の準備コメント](https://github.com/lisbun/lisjong/issues/258#issuecomment-6047898044)）。
source契約は lisbun/lisjong#256 v1（[HandBelief手牌正解source](hand-belief-source-453.md)）。
Arenaは対局の実行と観測事実の記録だけを担い、ラベル・評価・合否はlisjongが所有する。**強さの評価ではない。**

**現状: 生成経路の準備まで。seedの予約、対局、AWS起動は行っていない。下の「producer identity」が
解決するまで生成しない。**

## 生成script

`scripts/generate_hand_belief_formal_test_464.py`

pilot producer（`generate_hand_belief_source_255.py`）の`_play` / `write_source` / `verify_source_coverage`を
そのまま使う。pilotのseed制限と、#257 / #460のscript・population・guardは変更しない。

| 項目 | 値（引数では変えられない） |
|---|---|
| seed | 934000..934199（engine domain `lisjong-engine-project-standard-v1-hanchan-v1`）。1 seed = 1半荘、rotationなし |
| 分割 | 全件`test`。train / validは空 |
| 生成単位 | 100半荘ずつ2個のv1 source。`unit-0` = 934000..934099、`unit-1` = 934100..934199 |
| 予約 | owner `lisbun/lisjong#258`（#259と共用）、protocol `hand-belief-formal-test-258-259-v1`、population `hand-belief-formal-test-258-259-200-hanchan`、split `TEST200`。`arena_revision`は実行するmerge commit |
| Policy / rule | `PlacementAwareSpeedCallPolicy`（Champion）×4、`RuleSet.default()` |
| runtime | `LISJONG_SHANTEN_BACKEND=rust`（Arena pinのwheel）、pin済みlisjong / lisjong-engine |
| worker | 32固定（CLIは32以外を拒否） |

owner issue・protocol・population・splitの文字列は本Issueで決めた値で、予約前なら変更できる。
変更する場合はscriptの定数と予約を同時に変える。

### 実行前の照合（対局前にすべて通す）

live ledgerの読込・allocation照合・population骨格検査は、共通の`lisjong_arena.measurement_allocation_guard`
（#463）を使う。値はこのscriptの固定presetとして渡す。

- allocationがowner・protocol・domain・population・split・**200 seedのmembership**に一致し、stateが
  **RESERVED**、`arena_revision`がcleanな実行checkoutと一致すること。`-dirty`、COMMITTED、RETIRED、
  1つでも違う条件は対局前に停止する
- seedが使用済み範囲（920000.., 931000.., 932000.., 933000..933399, 935000..935001, 936000..936399）と
  重ならないこと
- pin済みlisjong / engineのinstall一致、tracked sourceが未変更であること、backendがrustであること
- `<output>`と、`--archive-dir`の`progress-unit-*`が既に存在すれば拒否する

### 出力

```text
<output>/unit-0, unit-1/                       v1 source（manifest / decisions / hand_facts / coverage）
<archive-dir>/progress-unit-<k>.tar.zst        その単位のsource 4ファイル
<archive-dir>/progress-unit-<k>.complete.json  最後に置く単位の記録（fileとarchiveのbytes/SHA-256、
                                               半荘別の時間、producer、allocation、backend、worker数）
<output>/generation.json                       2単位がそろったときだけ作る
```

`generation.json`がなければ評価へ進まない。単位の記録は母集団の完成を意味しない。

### 失敗時の扱い

resume・reuse・部分採用はない。失敗・中断したrunは`generation.json`を残さず、回収できたfileは診断用とする。
scriptはラベルを読まず、生成は決定的なので、同じallocation（RESERVEDのまま）・同じrevisionで**全量を最初から**
再実行することは技術的に可能である。再実行するかallocationをRETIREDにするかは、lisjong#258 / #259の
事前登録に照らして実行前に決める。強制終了後の回収は実証していない。

## producer identity（生成前に解決が必要）

manifestの`producer`には、実際に実行したrevisionを書く。#257のproducerとは3 fieldが異なる。

| field | #257（#258 / #259のselectionが登録） | 本経路 |
|---|---|---|
| `arena_revision` | `07554c24bfcabb2ded6cf81991b9ea58a20bc74d` | 本scriptを含む実行merge commit |
| `lisjong_revision` | `e6346ed2bb9e992138c05c4be367bd6a05ed00bc` | `994f529bd3a7d7d36a3ae0f6d1795d9413a3ce97`（現行pin） |
| `lisjong_engine_revision` | `8735e89e1aea000ab59368d0368d476787827741` | `91af75e3aa11520c3b0543719dc74bb4c517ee06`（現行pin） |
| `policy` | `PlacementAwareSpeedCallPolicy` | 同じ |

- Arenaの生成code（`generate_hand_belief_source_255.py`、engine接続、source record）は07554c2から変わっていない。
  変わったのはpinだけである
- pinの間で、lisjong-engineの`driver.py` / `round_state.py`（transaction観測hook、selector decision delivery）と、
  lisjongのPolicy 1件（`mechanism_riichi_defense_yakuhai_call.py`）が変わっている。Championの挙動が同じかは
  差分の閲覧だけでは確定しない
- lisjongの`check_producer`は全fieldの一致を要求するので、**このままでは本経路の生成物を拒否する**。
  新しいArena commitに置くentry pointでは`arena_revision`を一致させられないため、lisjong側で
  formal test用のproducerを登録・照合する変更（と、#258 / #259の事前登録への追記）が必要になる。
  これはlisjongが所有する判断で、Arenaは決めない

### 同一性の確認（`compare-reference`）

lisjong側の判断材料として、使用済みの#257 seedを現行producerで再生し、#257のsourceと行をbyte単位で比べる。

```sh
python scripts/generate_hand_belief_formal_test_464.py compare-reference \
  --reference <#257のunit directory> [--reference <別のunit directory>] \
  --seeds 933000..933002 --output <新しいreport.json> [--workers N]
```

- 再生できるseedは#257の使用済みpopulation 933000..933399だけで、本populationのseedは再生しない
- referenceは、展開済みの#257 unit（`progress-unit-k.tar.zst`の中身）。fileがmanifestの記録と一致すること、
  全referenceのproducerが同じこと、各seedがちょうど1つのreferenceにあることを検査してから対局する
- seedごとに`decisions.jsonl` / `hand_facts.jsonl`の該当行の行数とSHA-256を、referenceと再生結果の両方で
  reportへ書く。全seedが一致すればexit 0、1つでも違えばexit 1（reportは書く）
- 比べるのはbyteだけで、ラベルの計算も評価もしない。一致は**比べたseedについての事実**で、全seedの証明ではない
- backendは固定しない（reportの`runtime`に記録する）。正式な生成と同じ条件で示すならrustで実行する

**未実施:** #257のsourceが作業環境にないため、実データでの比較はまだ行っていない。

## AWS実行（準備まで。起動と課金は別承認）

汎用launcher `scripts/aws/lisjong-ec2.ps1`に`scripts/aws/bootstrap-hand-belief-formal-test-464.sh`を渡す。
`c7i.8xlarge`（32 vCPU）、`-Workers 32`。bootstrapは共通prologue `scripts/aws/measurement-source-prologue.sh`
（#463）をsourceするので、**wheelとprologueの両方を`-InputFile`で渡す**。引数は`--arena-revision`と
`--allocation-identity`。wheelは[現行pin](lisjong-native-wheel-current.md)のもの
（SHA-256 `170ef3489ef5ae843dfadd727628f66ebbd5f30c667b8fce8621685dffae2afc`）。

1. producer identityをlisjong側で解決する（上記）。
2. 本経路をmergeし、merge commitへseed registry workflowでRESERVEDを予約する（台帳を直接書き換えない）。

   ```text
   gh workflow run seed-registry.yml --repo lisbun/lisjong-arena \
     -f operation=reserve -f owner_issue=lisbun/lisjong#258 \
     -f protocol=hand-belief-formal-test-258-259-v1 \
     -f seed_domain=lisjong-engine-project-standard-v1-hanchan-v1 \
     -f purpose=hand-belief-formal-test -f population=hand-belief-formal-test-258-259-200-hanchan \
     -f split=TEST200 -f seeds=934000..934199 \
     -f arena_revision=<merge-sha> -f protocol_revision=hand-belief-formal-test-258-259-v1 \
     -f provenance_reference=https://github.com/lisbun/lisjong-arena/issues/464
   ```

3. Preflight（EC2を作らず、課金しない）で構成・料金・quotaを`plan.json`に固定し、その値で承認を取る。

   ```powershell
   .\scripts\aws\lisjong-ec2.ps1 -Action Preflight -AwsProfile <profile> `
     -Label hand-belief-formal-test-464 -InstanceType c7i.8xlarge -Workers 32 `
     -Bootstrap .\scripts\aws\bootstrap-hand-belief-formal-test-464.sh `
     -BootstrapArgs @('--arena-revision','<merge-sha>','--allocation-identity','<identity>') `
     -InputFile @('<wheel directory>\lisjong_native-0.1.0-cp314-cp314-manylinux_2_28_x86_64.whl',
                  '.\scripts\aws\measurement-source-prologue.sh') `
     -EstimatedRuntimeHours @(0.25, 1) -FailSafeHours 3 -CostBudgetUsd 5
   ```

   時間と費用は上限を決めるための仮の範囲である。参考実績は#257の400半荘・32 workersで wall 1,647秒・
   1.17 USD（別pin）。本経路の実測はない。
4. 承認後に`-Action Launch -Plan <plan.json>`、完了後に`-Action Collect`。
5. 成功artifactを保持・hash確認してから、allocationをCOMMITTEDにする。

**未実証:** このbootstrapと共通prologueはAWS上で未実行（`bash -n`と起動直後の拒否だけを確認）。
Launch後は`bootstrap.log`と`environment/allocation.json`で、installと`check-allocation`まで進んだことを確認する。
prologueは対局前に終わるので、そこで失敗しても対局は始まらない。

生成物はGitへ入れない。
