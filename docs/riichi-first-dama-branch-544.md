# riichi-first-dama-branch-v1：R1介入の小規模pilot（#544の実装）

設計: [lisjong #356](https://github.com/lisbun/lisjong/issues/356)（[対象比較・pilot設計案](https://github.com/lisbun/lisjong/issues/356#issuecomment-6395530392)と
[決定記録](https://github.com/lisbun/lisjong/issues/356#issuecomment-6395799219)）。R1 wrapper: [lisjong #357](https://github.com/lisbun/lisjong/issues/357)
（`RiichiFirstDamaBranchPolicy`、`riichi-first-dama-branch-wrapper-v1`、merge `d22c4c3`）。
本書は#544で実装したplan・観測・照合・summary・strict verify・rehearsal照合・時間見積もりの契約である。
**実行は後続の実行Issueで行う**（本Issueでは対局・rehearsal・seed予約・課金を行っていない）。

DEVELOPMENTのpilotで、強さ比較・候補Policy・正式評価・Championの変更の根拠ではない。
[riichi-dama-branch-v1](riichi-dama-branch-539.md)（#540）の契約・保存物・判定は変えない。

## 1. 目的と比べるもの

Championが最初にリーチを選んだ判断（分岐点）で、Rの宣言牌をリーチせずに打ち、**その後はリーチを含め
Championへ戻した**場合の、分岐席の局収支の対（G_R, G_D）を記録する。pilotの目的は次の3つだけである。

1. 新介入の対の分散（Var(G_R)・Var(G_D)・Cov・比・seed単位のσ_u）
2. 生成時間（R・D armのwall、1 seedあたりの費用）
3. データ品質（INVALID・照合・2 seedの再実行一致・分岐後に再びリーチした割合）

**効果の有無・大きさの判定、特徴・部分集合・モデルの選択には使わない。**平均Δは記述として出るが、
対象選択・続行判断・特徴選択に使わない。

| arm | 内容 |
| --- | --- |
| R | Champion（`placement-aware-speed-call`、設定`{}`）×4で半荘を最後まで実行する |
| D arm | 分岐点ごとに同じseedで半荘の先頭から再実行し、分岐席だけlisjongの`RiichiFirstDamaBranchPolicy`（inner = 新しいChampion）へ替える。他の3席はChampion。対象局の精算で打ち切る |

v1との違いはD armのwrapperだけで、分岐点・宣言牌・Δの定義・打ち切りはv1と同じ（[v1 §1〜§3](riichi-dama-branch-539.md)）。

## 2. v1から変えた点

| 項目 | v1 | 本protocol |
| --- | --- | --- |
| D armの分岐席 | `RiichiDamaBranchPolicy`（対象局の残りもリーチ抑止） | `RiichiFirstDamaBranchPolicy`（差し替えは分岐点の1回だけ。分岐後はリーチを含めChampionのまま） |
| 分岐後の分岐席のリーチ | INVALID | **正常**（記録する） |
| R の追加記録 | 席ごとのAction列digest | 全席の判断ごとの行動記録（§3の`actions.jsonl`） |
| D armの追加記録 | 差し替え回数 | 分岐点で実際に打った行動、分岐後のリーチの数（行動列とwrapperの両方）・最初の位置 |
| 判定 | STOP-INVALID → … → STOP-NO-SIGNAL → PROCEED | STOP-INVALID → STOP-INCOMPLETE → PILOT-COMPLETE。平均Δに基づく判定はしない |
| summary | 部分集合・bootstrap・判定 | 分散・σ_u・収集費用の一次判定・品質の記述 |
| 未完了 | stop記録だけ | stop記録に完了済みtaskの実行記録を含める（正式な集計・判定はしない） |

## 3. 保存物（write-once）

| file | schema | 内容 |
| --- | --- | --- |
| `hanchans.json` | `arena-riichi-first-dama-branch-v1-hanchans-v1` | Rの客観値（v1と同じ項目。局記録は本protocolのschema） |
| `actions.jsonl` | `arena-riichi-first-dama-branch-v1-actions-v1` | 1行目header、以降1半荘1行: 判断順の`[round ordinal, seat, 行動, 合法手の中の位置, 合法手の数, 合法手のdigest]`。合法手のdigestはengineが渡した元の順序の`encode_action`列の`canonical_digest` |
| `branches.jsonl` | `arena-riichi-first-dama-branch-v1-branches-v1` | 1分岐1行（v1と同じ項目。`d`は§2の追加記録を持つ） |
| `features.jsonl` | `arena-riichi-first-dama-branch-v1-features-v1` | lisjong `riichi-dama-features-v2`のJSON（判定には使わない） |
| `summary.json` | `arena-riichi-first-dama-branch-v1-summary-v1` | 判定・分散・費用判定・品質・記述・分岐ごとの行 |
| `timings.json` | `arena-riichi-first-dama-branch-v1-timings-v1` | taskごとのwall・peak RSS、stageの経過（再導出しない計測値） |
| `SHA256SUMS` | — | 上の全fileのSHA-256 |

`actions.jsonl`は、将来の記録行動の再生（Policyを呼ばずに既存対局をengineで再実行する計測）で、
行動の順序・判断位置・合法手の一致・選んだ行動・最終結果を照合するための記録である（その計測の実行は
lisjong #356でpilot後に判断する）。objectiveな行動記録だけで、Policy内部の解析・非公開情報は含めない。

保存時とverifyで次の整合を要求する: 判断数 = Policy呼び出し数、席ごとの判断数・Action列digestが
`hanchans.json`と一致、局ordinalが単調で局数以内、合法手の位置 < 合法手の数、各分岐点の行動がRのリーチで
直後が宣言牌、分岐のprefix / post digestが行動記録からの再計算と一致。

## 4. INVALID（1件でもあれば判定はSTOP-INVALID）

v1のR・特徴・精算・rehearsalの条件（[v1 §2](riichi-dama-branch-539.md)）に加えて、D armは次の場合INVALID。

| 条件 |
| --- |
| D armがない、wrapperの状態が`branched`でない、分岐点のdecision index・直前digest・contextがRと違う |
| **分岐点で実際に打った行動がRの宣言牌でない**（分岐点でリーチが適用された場合を含む） |
| 分岐後のリーチの数が、行動列とwrapperで違う。最初のリーチの記録の有無が数と合わない、または分岐点より前 |
| 別の局で精算した、D armの局開始点がRと違う、局収支が100点単位でない、rehearsalの2回実行が一致しない |

分岐後のリーチ（局の残りでChampionが選んだリーチ）はINVALIDにしない。

## 5. 統計・判定（実行前に固定。planの`criteria`）

| 項目 | 固定値 |
| --- | --- |
| 局収支 G | 分岐席の`point_deltas[seat]`（100点単位）。Δ = G_D − G_R。v1と同じ（Rで場に残ったリーチ棒は−1000、終局時の供託精算は別列） |
| 分岐単位 | Var(G_R)・Var(G_D)・Cov・Var(Δ)（n − 1）、比 Var(Δ) / (Var(G_R) + Var(G_D))、相関（記述） |
| σ_u | σ_u² = seedの標本分散(ΣΔ_s − μ̂·n_s) / n̄²（μ̂ = ΣΔ / Σn、n̄ = Σn / seed数）。`Fraction`で厳密に |
| bootstrap | 有効な分岐が1件以上あるseedをcluster（昇順、m個）。`random.Random(544)`をsummaryごとに1回初期化し、`randrange(m)`をm回の復元抽出を10,000回。比とσ_u²のP10 / P50 / P90（nearest rank） |
| 判定 | INVALIDが1件以上 → STOP-INVALID。未完了 → STOP-INCOMPLETE（summaryを作らない）。それ以外 → PILOT-COMPLETE |
| 収集費用の一次判定 | PILOT-COMPLETEのときだけ。K = ⌈(z_0.95 + z_0.8)² × σ_u²(P90) / d*²⌉（d* = 5、zは`statistics.NormalDist`）、1 seedの費用 = Rの平均wall + (分岐数 / seed数) × D armの平均wall、所要 = (K + 2) × 1 seedの費用 + 900秒。所要 ≤ 12時間（43,200秒）→ `COST-WITHIN-BUDGET`、超過 → `COST-OVER-BUDGET`、seedが2未満 → `COST-NOT-ESTIMABLE` |

- 収集費用の一次判定は**学習・採用へ進む判定ではない**。全分岐Δの分散は学習後の限定Policyの検出力を
  決めない。d*は規模を見積もる基準で、その改善が期待できるという意味ではない（lisjong #356の決定）
- rehearsalのsummaryは分散・費用判定を持たない（rehearsalの統計は持ち越さない）
- 判断行を独立標本とした検定は出さない。結果を見て指標・閾値・seedを変えず、追加run・retry・延長はしない

## 6. protocol invariantと開始条件

| 項目 | 値 |
| --- | --- |
| 構成 | Champion×4、`project-standard-v1`、配置入替なし、worker 1、先頭から再実行 |
| seed | **544000..544015（16）**。Seed Registryの既存engine hanchan domain、protocol `riichi-first-dama-branch-v1`、population `heuristic-candidate-engine-aabb-riichi-first-dama-pilot-v1`、split DEVELOPMENT、owner = 実行Issue。重複があれば開始しない（別seedへ移さない） |
| verifyで再実行するseed | **544000と544015**（予約範囲の最小と最大。結果を見る前に固定） |
| rehearsal | 472005 / 472006（registry外、動作確認だけ）。観測あり / なしの照合、D armの2回実行の一致 |
| 上限 | 1半荘 / 1 arm 600秒、process tree 4 GiB・swapなし。rehearsal 1時間。本実行＋verify＋回収は3時間、**全工程（gate・rehearsal含む）4時間を優先** |
| revision | planはArena clean merged mainのHEAD、lisjong・native・engine（環境のprovenance）、RuleSetを具体的な値で束縛し、run前後のlive gateで再照合する |

**開始条件**（allocated planの作成時に判定。満たさなければNOT-STARTED-BUDGETでplanを作らない）:

- 見積もり = 16 × Rの平均wall + n̂ × D armの平均wall（本実行）＋ 2 × Rの平均wall + n̂_verify × D armの平均wall
  （verifyの再実行）＋ 900秒（再導出・hash・回収）。n̂ = max(rehearsalの分岐率, 286/96) × 64、
  n̂_verify = 同じ率 × 8
- 経過 = 本計測より前の全段階（gate・rehearsal等）のwatchdog記録 max(monotonic, realtime) の合計を
  `--elapsed-seconds`で渡す。rehearsalの記録より小さい値は拒否する
- 上限 = min(3時間, 4時間 − 経過)。**見積もり ≤ 上限のときだけ開始**。planの`resource_limits.wall_seconds`は
  この上限で、OS側のscope（`RuntimeMaxSec`）も同じ値以下にする。各段階の上限を足して4時間を超える運用はしない

## 7. 未完了時の扱い

timeout / OOM / worker失敗 / 上限超過では、outputを公開せず、diagnosticsへwrite-onceのstop記録
（`arena-riichi-first-dama-branch-v1-stop-v1`: 判定STOP-INCOMPLETE、error、完了済みtaskの実行記録（wall・RSS）、
開始時刻・経過）を書く。**成功分だけを使った正式な集計・判定はしない**が、実行状況・保存物（stop記録・
driver / watchdogのlog）は実行Issueへ記録する。再試行・追加seed・延長はしない。

## 8. strict verify

`riichi-first-verify`（別process、保存物だけを読む）はv1と同じ検査（hash・file集合・schedule・局記録・
一対一・環境・特徴の再導出・summaryの再導出・timingsの上限）に加えて、§3の行動記録の整合を要求する。
allocatedは544000・544015のRと全D armを再実行し、局記録・**行動記録**・分岐記録・特徴・D armの記録の
完全一致を要求する。

## 9. CLI

```bash
python -m lisjong_arena.heuristic_candidate_engine_aabb riichi-first-plan \
  --run-kind {allocated,rehearsal} --observation {observed,unobserved} \
  --owner-issue lisbun/lisjong-arena#N [--allocation-identity ID] \
  [--time-estimate E --elapsed-seconds S] \
  --limit-enforcement "<OS / process treeの強制方法>" --wheel W \
  --run-output <label>/output --diagnostics <label>/diagnostics --output <label>/plan.json
python -m lisjong_arena.heuristic_candidate_engine_aabb riichi-first-run --plan P
python -m lisjong_arena.heuristic_candidate_engine_aabb riichi-first-verify --plan P --output V
python -m lisjong_arena.heuristic_candidate_engine_aabb riichi-first-rehearsal-compare \
  --observed P1 --unobserved P2 --output C
python -m lisjong_arena.heuristic_candidate_engine_aabb riichi-first-estimate \
  --observed P1 --unobserved P2 --output E
```

- seeds・timeout・RSS・判定・費用判定の固定値・verifyで再実行するseedはprotocol invariantでCLIから変えられない
- `riichi-first-estimate`は見積もりだけで3時間を超えると終了コード3（記録は書く）。全工程4時間の判定はallocated planで行う

## 10. 情報境界

v1と同じ（[v1 §7](riichi-dama-branch-539.md)）。wrapperの入力はその判断のpublic `DecisionContext`と自分の
宣言牌だけ。行動記録は全席の選択行動と合法手のdigestで、他家の手牌・山・privileged observer data・
Policy内部の解析を含めない。

## 11. testで固定したこと

[test_engine_aabb_riichi_first_dama.py](../tests/test_engine_aabb_riichi_first_dama.py)（実engineの対局・実RiichiEnvは
使わない。R1 wrapperと特徴APIは実lisjong）:

- worker: 全判断の行動記録（合法手の位置・数・digest）、D armで分岐点は宣言牌・次のリーチは通る、対象局外の
  リーチを数えない、lisjongのwrapper version
- 照合: 分岐後のリーチは正常、分岐点でのリーチ適用・リーチ数の不一致・最初のリーチの位置の矛盾はINVALID
- 統計: 分散・比・σ_u²の参照実装一致、bootstrapの参照実装一致と決定性、seed 1つでのCOST-NOT-ESTIMABLE、
  費用判定の境界（予算ちょうど / 超過）
- plan: 固定seed・verify seed（最小・最大）・上限・具体的revisionの束縛とdrift検出、全工程4時間の開始条件の境界、
  経過がrehearsalより小さい場合の拒否、見積もりの式・改ざん・別環境、owner、重複allocation、write-once
- run / verify: PILOT-COMPLETEと費用判定、STOP-INVALID（分岐点のリーチ・リーチ数の不一致）、未完了時のstop記録
  （完了済みtaskを含み、outputなし）、write-once、行動記録の改ざん（順序・位置・席・欠落）、再実行での
  行動記録・D armの不一致、file集合
- rehearsal: 観測あり / なしの一致と不一致、D armの2回実行の不一致、見積もりからallocated planまで

## 12. 実行Issueへの引継ぎ（未確定の実値）

実行前に次を実値で揃え、planへ束縛する（本書の時点では存在しない値を捏造しない）:

1. **revision**: 本Issueのmerge後Arena main full SHAとclean checkout、lisjong pin
   `d22c4c3e3c00a72b35705bda3220d5598884ffa1`、engine `91af75e3aa11520c3b0543719dc74bb4c517ee06`
2. **wheel**: lisjong main CI run [40184471486](https://github.com/lisbun/lisjong/actions/runs/40184471486)の
   `lisjong-native-wheel-d22c4c3…`、`lisjong_native-0.1.0-cp314-cp314-manylinux_2_28_x86_64.whl`（301,069 bytes、
   SHA-256 `69bf2efff99d6e87cf57d78c25f5e74c0a004a317cc774fdada43b70296f440f`、[現行の組み合わせ](lisjong-native-wheel-current.md)）。
   実行環境で`sha256sum --strict -c SHA256SUMS`、`verify-wheel`、`aws_backend --backend rust --wheel`を取り直す
3. **Rust-only採用gate**: この新pinで[Rust-only gate](engine-aabb-evaluation.md#84-rust-only-gaterust-gate-531)をPASSさせる
   （過去のPASSを流用しない）
4. **環境**: v1と同じWSL2 Ubuntu-24.04の専用user scope（`MemoryMax=4G`・`MemorySwapMax=0`・`OOMPolicy=kill`・
   `TimeoutStopSec=3`・`RuntimeMaxSec`）と外側のwatchdog（max(realtime, monotonic)）。AWSは使わない
5. **順序と時間**: gate → rehearsal（観測あり / なし → run → verify → compare → estimate）→ 経過の合計を確定 →
   544000..544015を全domainの既存allocationと既知のregistry外seed（472000..472006・513000・101..107）と照合して
   reserve / readback → allocated plan（`--time-estimate`・`--elapsed-seconds`。開始条件を満たさなければ
   NOT-STARTED-BUDGETでseedを予約しない）→ run → verify（544000・544015の再実行）→ 回収とhash照合。
   全工程4時間。使用後はCOMMITTED、途中で失敗してもFREEへ戻さない
6. **保存先**: `C:\Dev\lisjong-artifacts\issue-<実行Issue>-riichi-first-dama-pilot\`（write-once）
7. **結果の記録**: 判定・分散・費用判定（`not_a_learning_or_adoption_decision`）・品質・時間を実行Issueと
   lisjong #356へ記録する。費用判定で予算超過ならR1はDESIGN-NOT-VIABLE、P1（Pon）は次に設計を検討する候補で、
   自動で実装・実行へ移らない
