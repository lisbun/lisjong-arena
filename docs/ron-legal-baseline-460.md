# ロン合法baseline用400半荘sourceの生成（lisjong-arena#460）

owner: lisbun/lisjong#262（段階Cの絶対精度測定）。source契約は [ロン合法source producer](ron-legal-source-457.md)
（lisjong 994f529、`docs/ron-legal-source.md`）。Arenaは対局の実行と観測事実の記録だけを担い、
baselineのfit・集約・評価・推論semanticsはlisjong#262が所有する。**強さの評価ではない。**

## 生成script

`scripts/generate_ron_legal_baseline_460.py`

#457のproducer（`play` / `write_population` / base coverage / ron reader / native label builder）を
そのまま使い、2半荘pilot CLIと#257/#258/#259のpopulation・guardは変更しない。1 seed = 1半荘を、
independentな1個のsource（`base/` + `ron/`）として生成する。400半荘を1つのsourceに結合しない。

| 項目 | 値（引数では変えられない） |
|---|---|
| seed | 936000..936399（engine domain `lisjong-engine-project-standard-v1-hanchan-v1`）。rotationなし |
| 分割 | train 936000..936159 / valid 936160..936239 / eval 936240..936399（manifestの分割名ではevalは`test`） |
| 予約 | owner `lisbun/lisjong#262`、protocol `ron-legal-baseline-measurement-v1`、population `ron-legal-baseline-measurement-400-hanchan`、split `TRAIN160-VALID80-EVAL160`。`arena_revision`は実行するmerge commit |
| Policy / rule | `PlacementAwareSpeedCallPolicy`を各席・各半荘で新規生成、`RuleSet.default()`全体、反事実C `project-standard-normal-discard-ron-v1` |
| runtime | 通常CPython 3.14（free-threadedは拒否）、`LISJONG_SHANTEN_BACKEND=rust`、installed lisjongと同じ`SOURCE_REVISION`のnative scorer（`SCORING_API_VERSION=1`） |
| worker | 32固定（CLIは32以外を拒否） |

既に使用済みのseed（920000.., 931000.., 932000.., 933000..933399, 934000..934199, 935000..935001）と
重ならないことを、予約前にscript自身も検査する。live ledgerの再照合が正本で、候補だけでは予約済みとして扱わない。

### 実行前の照合（対局前にすべて通す）

- pin済みlisjong / engineのinstall一致（`verify_environment`）、runtime / nativeの検査
- live ledgerのallocationが、owner・protocol・domain・population・split・**400seedのmembership**に一致し、
  stateが**RESERVED**、`arena_revision`がcleanな実行checkoutと一致すること。`-dirty`、COMMITTED、RETIRED、
  1つでも違う条件はすべて対局前に停止する
- 各workerも対局前に同じruntime / nativeを再検査し、planのruntimeと違えばrunを失敗にする
- `<output>`と`--archive-dir`の`progress-seed-*`が既に存在すれば拒否する（resume / reuseなし）

最初の対局より前に`<output>/plan.json`を排他的に作り、同じ内容をstdoutへflushする。

### 出力と完全性

```text
<output>/plan.json              実行計画（allocation、3 repository revision、runtime、worker数）
<output>/seed-<seed>/           そのseedのbase/ron source（実行環境上の確認用。回収対象ではない）
<archive-dir>/progress-seed-<seed>.tar.zst        source 7ファイル
<archive-dir>/progress-seed-<seed>.complete.json  最後に置く受領record
<output>/generation.json        全400seedを検証した後にだけ作る
```

1 seedは1 workerが「対局 → write_population → base coverage → ron reader → native label builder
→ archive → receipt」まで行う。workerが親へ返すのは小さな要約だけで、journalは返さず、親が
population全体をRAMに持たない。受領record（`.complete.json`）は、allocation・owner・protocol・
population・producer revisions・runtime・split・seed・判断数・局別counter・全fileのbytes/SHA-256・
archiveのbytes/SHA-256・時間を持つ。

親は全workerの終了後、**ディスク上の400組を読み直して**検証する。archive・recordの欠落、重複、
予期しない`progress-seed-*`・`.partial-*`、archiveのSHA-256とbytes、archive内の全memberの
SHA-256、seed・split・allocation・producer・runtime・件数の不一致を、population全体の失敗として扱う。
`generation.json`には検証済みの400件のentry（seed、split、判断数、reaction数、archiveとrecordのdigest）と合計を書く。
`generation.json`がなければ、baseline測定へ進まない。

回収後は同じ検査を再実行できる（`generation.json`と`progress-seed-*`が同じdirectoryにあること）。

```sh
python scripts/generate_ron_legal_baseline_460.py verify-collected --directory <collected output directory>
```

### 失敗時の扱い

初版は部分成功の採用、失敗seedの同seed再実行、resume / reuseを許可しない。いずれかのseedが失敗した
run、中断したrun、完全に採用しなかったrunのallocationは**RETIRED**にする。回収できたfileは診断用で、
測定には使わない。新しく生成する場合は新しいseed範囲と新しい予約が必要になる。

## AWS実行（準備まで。起動と課金は別承認）

汎用launcher `scripts/aws/lisjong-ec2.ps1`に`scripts/aws/bootstrap-ron-legal-baseline-460.sh`を渡す。
AL2023 x86_64、`c7i.8xlarge`（32 vCPU / 64 GiB）、On-Demand、IMDSv2、inboundなし、hard fail-safe、
S3回収とstrict hash照合はlauncherの既存機能のまま。入力は固定SHA-256のlisjong 994f529 native CI wheel
（`lisjong_native-0.1.0-cp314-cp314-manylinux_2_28_x86_64.whl`、
SHA-256 `170ef3489ef5ae843dfadd727628f66ebbd5f30c667b8fce8621685dffae2afc`、
[現行pin](lisjong-native-wheel-current.md)）。bootstrapは、wheelの照合、merge済みrevisionのclone、
pin一致、live ledger取得、`check-allocation`、生成、`verify-collected`を行う。
seedの予約・commit・retireはしない。

### 手順

1. 本PRをmergeする（ユーザー承認）。
2. merge commitへ、live ledgerを再照合したうえでseed registry workflowでRESERVEDを予約する。

   ```text
   gh workflow run seed-registry.yml --repo lisbun/lisjong-arena \
     -f operation=reserve -f owner_issue=lisbun/lisjong#262 \
     -f protocol=ron-legal-baseline-measurement-v1 \
     -f seed_domain=lisjong-engine-project-standard-v1-hanchan-v1 \
     -f purpose=ron-legal-baseline-measurement -f population=ron-legal-baseline-measurement-400-hanchan \
     -f split=TRAIN160-VALID80-EVAL160 -f seeds=936000..936399 \
     -f arena_revision=<merge-sha> -f protocol_revision=ron-legal-baseline-measurement-v1 \
     -f provenance_reference=https://github.com/lisbun/lisjong-arena/issues/460
   ```

3. Preflight（EC2を作らず、課金しない）。構成・料金・quotaをplan.jsonに固定する。

   ```powershell
   .\scripts\aws\lisjong-ec2.ps1 -Action Preflight -AwsProfile <profile> `
     -Label ron-legal-baseline-460 -InstanceType c7i.8xlarge -Workers 32 `
     -MinMemoryMiBPerWorker 2048 `
     -Bootstrap .\scripts\aws\bootstrap-ron-legal-baseline-460.sh `
     -BootstrapArgs @('--arena-revision','<merge-sha>','--allocation-identity','<identity>') `
     -InputFile @('<wheel directory>\lisjong_native-0.1.0-cp314-cp314-manylinux_2_28_x86_64.whl') `
     -EstimatedRuntimeHours @(0.5, 2) -FailSafeHours 6 -CostBudgetUsd 10
   ```

   `-EstimatedRuntimeHours`と`-CostBudgetUsd`は、実測予測ではなく**上限を決めるための仮の範囲**である。
   根拠は#457 pilotの2半荘・逐次実行 wall 150.46秒（起動を含む）・最大RSS 906,104 KiBだけで、
   32-worker matched calibrationは未取得。32 vCPUは16 physical coreなので、単純に32倍速になるとは
   仮定しない。実測が必要な場合は、Launch後にworker別の半荘wall / CPU時間（受領record）を見る。
   実際の時間単価・quota・合計見積もりは、Preflightが出力する`plan.json`の値を提示して承認を取る。
4. 課金の承認後に`-Action Launch -Plan <plan.json>`、完了後に`-Action Collect`。
   成功したrunはrunnerが自己terminateする。回収後に`verify-collected`を再実行する。
5. 成功artifactを保持・hash確認してから、allocationをCOMMITTEDにする。失敗・不採用はRETIREDにする。

runnerは実行中、出力直下の`progress*`を60秒ごとにS3へ送る（best effort）。400組（800 file）が溜まると
この周期の送信は繰り返しになるが、launcherは変更しない。強制終了では回収が保証されない。

生成物はGitへ入れない。測定（baseline fit・集約・評価）はlisjong#262が所有し、このrunには含めない。
