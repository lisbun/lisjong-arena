# Rust-only移行のconsumer互換契約（D3）

Owner: Arena。Issue [#527](https://github.com/lisbun/lisjong-arena/issues/527)、
親 [lisjong #328](https://github.com/lisbun/lisjong/issues/328) /
[project #87](https://github.com/lisbun/lisjong-project/issues/87)。

調査基準: Arena `8ad451e517cfd86ec2d89c3e1cfe9630555cfba2`、
lisjong pin `d2469f2519b947fb27bf8c1622a2854c200e0aae`。
本書は移行前の契約で、現行のbackend、pin、protocol、digestを変更しない。

## 決定D3

最新readerで保持する対象は、下表に列挙した保存物の読取・整合性検査・保存値からの再集計。
旧Python計算を使うPolicy再実行・判断再導出は、その記録が指定する旧環境で行う。
未知schema、改変digest、未対応revisionを無言変換・読み飛ばし・成功扱いしない。
現在のimport graphでnativeを要求する場合、読取環境にも対応wheelは必要になり得る。
「新しい対局を起動しない」と「Python/nativeの導入が不要」は別の保証である。

## CLI・schema別の境界

| consumer / 入口 | 保存物の扱い | 新規実行・後続の変更 |
| --- | --- | --- |
| `shanten_backend_verification compare / report` | `measure.py` / `report.py`が受理する既存record・固定planの比較を保持。全revisionの再実行保証ではない | `probe / games / startup`は計算・process起動側。採用PRで新規実行をRustへ限定し、旧Python要求を拒否する |
| `pure_offense_benchmark summarize` | 現行`OFFENSE_RECORD_VERSION`のarmを`load_benchmark_arm`で検査して再集計。旧schemaを自動変換しない | `run`の未指定backendと明示pythonを別々に点検。Python要求を黙ってRustに置き換えない |
| `riichilab.aws_backend.read_backend_evidence` | schema_version=1。現行`EXPECTED_LISJONG_REVISION`・API・wheel hashへの一致を要求するため、旧revision全般のreaderではない。旧receiptは旧Arena環境で検査 | `probe_backend`・bot起動は実行側。読取側のpython値受理を削除理由にしない。稼働bot切替は別作業 |
| engine AABB `backend-compare` | `arena-heuristic-candidate-engine-aabb-backend-check-v1`のPython/Rust記録を読み、保存されたmatch/action digestを比較。結果はcomparison-v1。P2を新規対局なしで読み直す | `backend-check`は実対局側。旧P2の再実行が必要なら記録済み旧環境へ戻す |
| engine AABB `verify` | `parse_lock_document`→`read_kyoku_records`→`build_result`で保存物から再導出。protocol/digest・schedule・集計条件の検査を保持 | `lock / run`は新規計画・実行側。Rust-only gateを旧v1へ上書きしない |
| engine AABB `calibration-verify / calibration-report` | 保存plan/raw/summary/timingsを照合。`verify_calibration`はlive ledger・Policy生成・対局再実行を行わない | `calibration-plan / calibration-run`は実行側。旧#517のfixed pair・P2証拠を変更しない |
| engine AABB各開発`*-verify / *-report` | paijia/focal/genbutsuの既存schema・plan identityに沿う検証を保持。各実験の終了判断を変更しない | `*-plan / *-run`は実行側。移行を理由に終了済み実験を再実施しない |
| `l2a_runtime_smoke` | 専用の一般historical-reader CLIではない。保存成果物の再現は実行時環境を用いる | 既にRust専用。新pin/wheelで推論identity・ML optional境界を確認する |

この表の保証はschemaを越えた互換性の追加ではない。既存parserが未知schemaや旧revisionを拒否する
箇所を、移行のためだけに緩めない。旧環境を必要とする場合はその理由と必要revisionを案内する。

## 新gateと旧protocol

新規Rust-only評価gateは新versionの仕様・identityとして定義する。旧v1のPython/Rust照合要件や
protocol digestをその場で書き換えない。旧lock/resultの読取には旧定数を保持する。
新versionで保持するのは仕様fixture、合法手・情報境界・決定性の回帰、同revision wheelの
hash/SOURCE_REVISION/API/実ロードfile/native counter、P3、保存物のverifyである。
候補固有の未確認事項・seed割当・標本数・費用・正式実行条件は残る。
Python全対局比較の恒常的な要求を外すことは、これらの検査の免除ではない。

新versionの具体的定数・schemaとgateの実装はArena採用の子Issueで固定する。
その実装前にRust-only正式eventを開始しない。既存結果の分類や歴史的証拠は変更しない。

## 旧環境の復元と切替順

1. 旧記録が指定するArena/lisjong/engine revision、source archive、wheel、hash、Python/platform、
   protocol・依存環境を組で保持する。git SHAだけを復元可能性の証拠にしない。
2. 旧環境を新規venv等へ復元し、identityを照合する。新環境を上書きしない。
   lisjongの同revision bundleは`tools/native_bundle.py`で検証する。
3. 本互換準備のmerge後、lisjong側でRust必須化・numeric/batched/R5のPython計算削除、
   仕様検証への対応付け・CI更新を行う。Policy/学習/binding、artifact reader/generatorは保持する。
4. 切替後のmain CI wheelと同revision sourceを保存して導入確認し、Arenaのpackage/native pin、
   wheel hash、CI、新規実行CLI、新gateを一体で更新する。
5. rollbackは旧revision・wheel・環境の組で行う。廃止後にenvをpythonへ戻すだけでは復元できない。

旧bundleの現物保存・全過去artifactの復元成功は、本書のsource調査だけでは確認できない。
切替時に使用する組と保存先を記録し、不足なら切替を停止する。

## 検証の範囲

`tests/test_engine_aabb_calibration.py`のBackendCheckTestで、合成保存記録の比較が
backend probe・environment probe・schedule実行を呼ばないこと、未知schema・digest改変を拒否することを検査する。
実Policyでの対局や旧artifact現物の全件検証を行ったという意味ではない。
`tests/test_riichilab_aws_backend.py`はschema_version=1とcurrent revisionの拒否境界を保護する。

後続の採用PRでは本表の各入口について、Rust-only環境でのimport/読取、旧python実行拒否、
既存record fixtureの回帰を確認する。現在のPython計算backendでのtest passを、その検証の代替にしない。
