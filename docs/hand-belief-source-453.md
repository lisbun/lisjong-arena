# HandBelief手牌正解sourceのproducer（lisjong-arena#453）

owner: lisbun/lisjong#255（HandBeliefの推定精度測定）。契約: lisbun/lisjong#256（v1、lisjong
`docs/hand-belief-accuracy-source.md`、`lisjong.learning.hand_belief_source`）。

Arenaは対局の実行と観測事実の記録だけを担う。ラベル（`HandBelief`の正解）はlisjongが
読み込み時に計算し、Arenaはラベルを作らない。**推定精度のための入力データであり、強さの評価ではない。**

## 生成script

`scripts/generate_hand_belief_source_255.py`

- lisjong-engine上で`PlacementAwareSpeedCallPolicy`（Champion）×4の自己対局。RuleSetはengineの既定
  （`RuleSet.default()` = `PROJECT_STANDARD_RULES`）。1 seed = 1半荘
- 対象: 合法手に打牌を含む観測者の判断すべて（`all-observer-discard-decisions.v1`）。`sequence`は
  その半荘でengineがselectorを呼んだ順の通し番号（全席・反応判断を含む）
- 他家3席の手牌: Policyを包む記録用wrapperが、selector呼び出しの中（engineが選択を適用する前）で
  `RoundState.hand_tiles()` / `melds()`を読む。値はその判断と同じ`sequence`のsnapshotとして書く（v1契約）。
  wrapperは手牌をPolicyへ渡さず、内側のPolicyは`DecisionContext`だけを受け取る
- 同一状態の検査（fail closed）: 観測者のengine手牌と`PolicyInput`の手牌が一致すること、全席の副露が
  `PolicyInput`の公開副露と一致すること

## 記録漏れの検出（coverage）

lisjongの読み込み検査は、同じ判断を両ファイルから落としてmanifestもその行数で作ると漏れを検出できない。
そこで次を行う。

1. wrapperが、呼び出し時点で対象判断の`(seat, sequence)`を記録する（行の抽出とは独立）
2. 半荘ごとに、出力行のkey集合・件数がその記録と一致し、重複がなく、各手牌行が他家3席をちょうど
   1回ずつ判断と同じ`sequence`で持つことを検査する（`verify_coverage()`）。書き出し直前にも全半荘で再検査する
3. `coverage.json`（Arenaのproducer証跡。lisjongは読まない）へ半荘ごとの対象判断数とkey集合の
   SHA-256を書く。`verify-coverage`で、書いた後のファイルをこれと照合できる

```sh
python scripts/generate_hand_belief_source_255.py verify-coverage <source>
```

## 段階とseed

| 段階 | 目的 | seed |
|---|---|---|
| producer確認用pilot | 生成・読み込み・coverageの動作、費用、層別support（リーチ者・門前非リーチ者・副露者）の確認。**lisjong#257の測定には使わない** | RETIRED 931000..931999のうち未使用の931400..931409（10半荘）。931100..931399（lisjong#236 / #237 / #245の開発）はscriptが拒否する |
| 測定用population | lisjong#257の測定 | 933000..933399（400半荘）。pilot用scriptは受け付けず、下の測定用scriptだけが、seed registryの予約と一致する場合に生成する |

生成前に記録する値: Policy identity（`PlacementAwareSpeedCallPolicy`）とlisjong revision、
lisjong-engine / Arena revision（manifestの`producer`にも入る）、RuleSet、seed範囲。

## 測定用population（lisjong#257）

条件はlisjong#257の事前登録（comment 6034792131）で固定したもので、引数では変えられない。

| 項目 | 値 |
|---|---|
| seed | 933000..933399（engine domain `lisjong-engine-project-standard-v1-hanchan-v1`）。1 seed = 1半荘、座席rotationなし |
| 分割 | train 933000..933159 / valid 933160..933239 / eval 933240..933399。manifestの分割名ではevalは`test` |
| 生成単位 | 100半荘ずつ4個のv1 source。`unit-k`（k = 0〜3）は train 933000+40k〜+39、valid 933160+20k〜+19、eval 933240+40k〜+39 |
| seed予約 | owner issue `lisbun/lisjong#257`、protocol `hand-belief-accuracy-baseline-measurement-v1`、population `hand-belief-accuracy-baseline-measurement`、split `TRAIN-VALID-EVAL`。`arena_revision`は実行するmerge commit |
| backend | `LISJONG_SHANTEN_BACKEND=rust`（Arena pinのwheel）。Pythonでは生成しない |

`scripts/generate_hand_belief_measurement_257.py`は、pilot用scriptの`_play` / `write_source` /
`verify_source_coverage`をそのまま使う。pilot用scriptのseed制限は変更しない。生成前にlive ledgerの
allocationを照合し、owner issue・protocol・domain・population・split・seed membership・state
（RESERVED / COMMITTED）・`arena_revision`のどれかが違えば、対局を始めずに止まる。

```sh
python scripts/generate_hand_belief_measurement_257.py check-allocation \
  --seed-ledger <live ledger> --allocation-identity <sha256> --arena-revision <full sha>
LISJONG_SHANTEN_BACKEND=rust python scripts/generate_hand_belief_measurement_257.py run \
  --seed-ledger <live ledger> --allocation-identity <sha256> --workers 32 \
  --output <new directory> --archive-dir <directory> [--reuse-dir <完成単位のあるdirectory>]
```

出力は`unit-0`〜`unit-3`（各v1 source）と`generation.json`（allocation、実行環境、単位の記録）。
半荘別のCPU時間はworker process内で測る。

### 単位ごとの確定と、失敗後の再実行

- 単位が完成するたびに、`--archive-dir`へ`unit-k.tar.zst`（その単位のsource）を置き、最後に
  `unit-k.complete.json`（fileのSHA-256、半荘別の時間、producer、allocation、backend、worker数、
  archiveのSHA-256）を置く。両方がそろい、互いに一致する単位だけを完成とみなす
- 後の単位が失敗しても、完成した単位は`--archive-dir`に残る。`generation.json`は書かれない
- 再実行は、同じ予約・同じrevision・同じ条件で、完成単位のあるdirectoryを`--reuse-dir`に渡す。
  完成単位は、archiveのSHA-256、展開したfileのSHA-256、coverage、manifestの分割とproducer、
  記録のallocation・producer revision・backend・worker数が今回の実行と同じことを検査してから復元する。
  どれかが違えば対局を始めずに止まる。未完成の単位（固定の0〜3のうち記録がないもの）だけを生成する
- **`generation.json`は4単位すべてがそろったときだけ書く。** 単位の記録は母集団の完成を意味しない。
  `generation.json`がない状態では評価しない
- 任意のseed指定や、単位内の途中からの再開はできない

### AWSでの実行

汎用launcher `scripts/aws/lisjong-ec2.ps1`に`scripts/aws/bootstrap-hand-belief-measurement-257.sh`
を渡す（`c7i.8xlarge` 1台、`-Workers 32`、入力はwheel、引数は`--arena-revision`と`--allocation-identity`）。
bootstrapはwheelの照合、live ledgerの取得、allocationの照合、生成を行う。seedの予約・commit・retireはしない。

- 完成単位は、出力directory直下の`progress-unit-k.tar.zst`と`progress-unit-k.complete.json`になる。
  runnerは直下の`progress*`を約1分ごとにS3へ送り、終了時にも全出力を送る
- 単位の生成が失敗して終了した場合は、通常の終了処理が走り、完成単位はすべて回収できる
- 強制終了（fail-safeによる電源断、instanceの障害）では終了処理が走らない。この場合に回収できない
  可能性があるのは、**強制終了の直前およそ1分以内に完成した単位**と、**その時点で生成中だった単位**。
  それより前に完成した単位はS3に残り、`-Action Collect`で回収できる
- 再実行では、回収した`progress-unit-k.tar.zst`と`progress-unit-k.complete.json`の組を、wheelと一緒に
  `-InputFile`へ渡す。bootstrapは入力にそれらがあれば`--reuse-dir`として使う

評価では4個の単位をすべて読み、seedの重複・欠落・producer identityの不一致を失敗にする（lisjong側）。
生成データはcommitしない。

## 完全性検査

lisjongの読み込み検査は、`hand_belief_source`を含むlisjong（main `c9f7d0b`以降）を入れた別のvenvで行う
（Arenaのpinはそれより古い。#245と同じ方式）。

```sh
python -c "from lisjong.learning.hand_belief_source import read_labelled_source as r; m, rows = r('<source>'); print(len(rows))"
```
