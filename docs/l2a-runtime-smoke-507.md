# L2a合成Policyの動作確認（#507）

親：lisbun/lisjong#318（L2a-F、lisjong `docs/learning-l2a.md` 5.4節）。
凍結したL2aの合成Policy（lisjong `lisjong.learning.l2a_policy`）を1席、
C0（`placement-aware-speed-call`）を3席に置き、RiichiEnvで16半荘を実行する。
**強さ・局素点の比較はしない。結果を強さの根拠に使わない。**

## 固定値（protocol `l2a-runtime-smoke-v1`）

| 項目 | 値 |
| --- | --- |
| seed | `506900..506915`（16半荘、`riichienv-4p-red-half-hanchan-v1`、DEVELOPMENT） |
| allocation | `f536a79cfb9c02d382e809795fee54d49f13620e7df0234499897f56bd1cbba7`（実行前はRESERVED） |
| L2a席 | 半荘の順番`i`（0始まり）で`Seat(i % 4)`。`0,1,2,3`の順に回す |
| 打牌artifact（M1） | `c976ed4a5cb667c406eb57f5cc79c128b1a221fb9b478f0dd017fcf48bde6e03` |
| 反応artifact（R1） | `d565713f51a2b9f9cbdd2146bf01cb4bfb13ff859b639de962c74562eaf01efc` |
| lisjong | `999553b`（L2aの学習・OFFLINE-EVALと同じrevision）、Rust backend |

seed・allocation・seatの回し方はcode（`lisjong_arena.l2a_runtime_smoke.smoke`）の定数で、CLIから変えられない。

## 実装

- artifactは`load_l2a_policy_factory(m1, r1)`で1回だけ読み、`runtime.identity`がCLIで明示した
  2つのidentityと一致しなければ対局を始めない。L2a席のidentityはartifact identityから作り、class名から導かない
- 各半荘・各席でfactoryから新しいPolicyを作る（L2aはruntimeの`create_policy()`、C0はcatalogのfactory）
- L2a席は`RecordingL2aPolicy`で包む。`decide()`の結果をそのまま返し、そのactionがdecisionの
  `legal_actions`にあるobjectでなければ例外にする。合法性はlisjongの`execute_policy()`と
  RiichiEnv Adapterの外部Action変換でも再検証される
- 記録するのは判断の種類、scorerを呼んだか、`decide()`全体の時間、`L2aDecision.timings_ms`
  （候補encoding・shared context・forward）だけである。候補・scoreは保存しない。
  時間は`GameTrace`やPolicy入力へ流さない
- 1半荘でも例外・非合法があればそこで止め、`FAIL`と原因を記録する。成功した半荘だけでPASSにしない

## 実行（WSL / Linux x86_64、Rust backend）

```bash
LISJONG_SHANTEN_BACKEND=rust python -m lisjong_arena.l2a_runtime_smoke \
  --discard-artifact <m1-w256> --reaction-artifact <r1-w256> \
  --discard-identity c976ed4a5cb667c406eb57f5cc79c128b1a221fb9b478f0dd017fcf48bde6e03 \
  --reaction-identity d565713f51a2b9f9cbdd2146bf01cb4bfb13ff859b639de962c74562eaf01efc \
  --wheel <lisjong_native wheel> --output <新規directory>
```

venvにはこのArena revision（editable、clean checkout）、`torch==2.13.0`（CPU）、
[現行wheel](lisjong-native-wheel-current.md)を入れる。開始前にRust backend・pin・wheelとimport済みfileの一致、
Arena checkoutがcleanであること、artifact identityを確認する。
終了コードはPASSで0、FAILで1、開始前の確認で止まれば2。

出力directoryには次の2つを書く（既存directoryは拒否する）。

- `report.json`：環境、load時間、半荘ごとの完走・L2a席・最終点・順位・L2a判断数（種類別）・時間・RSS、
  合計（判断数・非合法・例外）、推論時間（中央値 / p95（nearest-rank）/ 最大）、process RSS
- `decisions.jsonl`：L2a席の判断ごとの種類・時間

RSSは`/proc/self/status`の`VmRSS`（load直後・各半荘後・終了時）と、process全体のpeak（`ru_maxrss`）。

`outcome`が`PASS`で結果を保存・確認できた後にだけ、allocationをCOMMITTEDにする（Arena Seed Registry
workflowの`commit`）。`FAIL`ならCOMMITTEDにせず、原因を#507へ記録する。
