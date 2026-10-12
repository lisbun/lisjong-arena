# R1の現物選択可否・向聴維持と実戦損失の追加診断

作業範囲・完了条件の正本は[Arena #483](https://github.com/lisbun/lisjong-arena/issues/483)。
集計は `lisjong_arena.riichilab_r1_choice_loss_diagnostic` に実装済み。実行結果は#483へ記録する。

## 目的

[#455の完了結果](riichilab-deal-in-risk-disagreement.md)を受けた探索的診断として、
単独リーチ下の不一致を分解し、Champion実戦で損失が集中する分類を別表で確認する。
強豪botの選択を正解にせず、降りすぎ・押しすぎや改修の採否を確定しない。

## 分類の要点

対象は#455と同じR1（他家のリーチ成立者1人、自席リーチNONE）の打牌選択行。
一致・打牌同士の不一致・comparator非打牌を分け、全R1選択行の件数を示す。

| 軸 | 定義 |
|---|---|
| 現物あり | 合法打牌候補に既知の現物が1つ以上ある |
| 現物なし | 合法打牌候補すべてが既知の非現物 |
| 判定不能 | 上記のどちらにも確定できない。なしへ混ぜない |
| 向聴維持 | 選択打牌の打牌後向聴数が合法打牌候補の最小値と等しい |
| 向聴悪化 | 選択打牌の打牌後向聴数が同最小値より大きい |

現物の定義・曖昧な宣言位置の扱いは#455を継承する。向聴数は既存lisjong公開APIを使う。
両者維持・Championのみ悪化・強豪botのみ悪化・両者悪化を区別する。
#455の「相手より向聴数が大きい」という相対比較は別項目として残す。
打牌前向聴数は枚数状態と計算定義を明示し、合法打牌後の最小値と混同しない。

門前/副露、自席1〜6/7〜12/13打目以降、最小向聴数0/1/2以上の内訳を示す。
各表に分子・分母・不明・除外数を付け、bot別と合算を併記する。

## Champion実戦側との照合

#441の既存対象期間・版の根拠を継承し、実打牌を同じ定義で分類する。
各区分の対象打牌数、実打牌のロン被害件数、放銃支出合計とR1放銃支出内の構成比を
別表で示す。打牌あたりのロン被害割合を局単位放銃率と呼ばない。
複数ロン・供託/本場・支出の定義を既存規則と照合し、追加集計前に固定する。
dev/baselineを併記し、共有卓を独立2対局として数えない。

両入力は到達局面・期間・相手が異なるため、率を単純に引き算・統合しない。
同じ定義で再現できない部分は不足として記録し、照合済みとは扱わない。
小区分（選択行100未満）は件数・率の記録にとどめ、区間推定・検定は行わない。

## 実行前の固定と境界

- #170は#455と同じidentity・3体・comparator・依存条件。別run identity・別出力にする。
- #170全体coverageは112対局/19,874判断機会/18,992選択行/882強制行と照合し、
  R1件数も#455と照合する。不一致なら停止する。
- self-historyのsnapshot/manifest・対象件数・provenance・実行revision・定義・出力先を
  追加集計前に固定する。local実行担当と既存入力の利用可能性を確認してから実行する。
- raw・判断行・第三者botの個別判断は既存local保管境界から出さない。
  GitHubには集計値と限界だけを記録する。AWS起動、新規取得、対局、corpus拡張はしない。
- 分類は判断時点のplayer-safeな情報だけを使う。公開horaは実打牌の結果集計だけに使う。
  他家の伏せ牌・山・将来情報やPolicy内部理由を分類・objective traceへ混ぜない。
- #455/#251の既存結果・locked inputsを変更しない。追加実装では分類と母数のfocused testを行う。

## 後続判断

結果と限界は#441とproject #84へ引き渡す。H2（lisjong #254）は設計・後続評価まで
完了し初版不通過であり、本診断を理由に再openしたり、既存の再開条件を変更したりしない。
#297等の必須依存にも設定しない。F/R2の掘り下げ、Policy改修・昇格は本診断の対象外。

## 実装と実行

`lisjong_arena.riichilab_r1_choice_loss_diagnostic` は2つのcommandを持ち、分類codeを共有する。
出力はどちらも集計値だけのJSONで、#455の出力とは別identityである。

| command | 入力 | 内容 |
|---|---|---|
| `strong-bots` | exact #170 corpus | 強豪botの打牌とChampionの選択を分類する。#170 coverageと、#455に記録したbot別のR1件数が一致しなければ停止する |
| `champion-play` | #441のdev / baseline self-history | Championの実打牌を分類し、公開`hora`のロンと放銃支出を結合する。件数・放銃支出は実戦の打牌から集計する。固定したChampionはR1打牌選択行の再現・分岐確認のために実行する（#489）。代替打牌の結果は評価しない |

固定した入力（`champion-play`）:

- dev: bot 313、history identity `49575871…5056`、209対局、2,161局、放銃局307
- baseline: bot 394、history identity `72e4cab8…c095`、214対局、2,117局、放銃局251
- 対象は`played_at >= 2026-09-30T15:00:50`（#441と同じくUTCとして読む推定を継承）。共有66対局
- history identity、対象対局IDのSHA-256、局数、放銃局数が上と一致しなければ停止する

定義の補足:

- 分類する行は#455のR1と同じ（打牌の選択行、自席リーチ`NONE`、他家のリーチ成立者1人）。
- 打牌前向聴数は、ツモ牌または鳴いた後の牌を含む打牌前のconcealed hand（3n+2枚）に
  公開`calculate_shanten`を適用した値。和了形は`-1`。合法打牌後の最小向聴数とは別の軸で出す。
- 選ばれた牌が現物かどうか（`selected_genbutsu`）も、現物選択可否ごとに別表で出す。
- 放銃支出は、その打牌への全`hora`の`deltas`のうち自席分の符号を反転した合計。
  複数ロンは1打牌として合算し、本場を含む。R1では自席がリーチしていないので供託の支出は含まれない。
  槍槓は打牌ではないので打牌単位の表に入れず、放銃局数の照合にだけ数える。
- 再現できない対局は途中までの行を捨てて別計上し、未解決の判断も件数を出す。
  公開eventだけで数えたR1のロン件数・支出も出し、分類できた分との差を確認できるようにする。

実行は実装PRのmerge後、merge revisionのclean checkoutから、#455と同じWSL環境
（lisjong `51e832e`、rust backend、RiichiEnv 0.4.10）で行う。

    export LISJONG_SHANTEN_BACKEND=rust
    python -m lisjong_arena.riichilab_r1_choice_loss_diagnostic strong-bots         --snapshot <path-to-170-snapshot.json>         --output-dir <path-to-170-corpus-directory>         --arena-revision "$(git rev-parse HEAD)"
    python -m lisjong_arena.riichilab_r1_choice_loss_diagnostic champion-play         --dev-history <path-to-lisjong-dev-history>         --baseline-history <path-to-baseline-history>         --arena-revision "$(git rev-parse HEAD)"

## 追加：向聴別・放銃先別の分解（Issue #487）

[#487](https://github.com/lisbun/lisjong-arena/issues/487)は、#483の結果を見た後の探索的な
追加で、`champion-play`だけを拡張する。強豪bot側は再実行しない。出力のschema versionは
`arena-riichilab-r1-choice-loss-champion-play-v2`で、#483の出力（v1）とは別fileに置く。

- 軸`genbutsu_availability|selected_genbutsu|minimum_post_discard_shanten`を追加した
  （現物選択可否 × 選ばれた牌 × 合法打牌後の最小向聴数）。
- すべての区分に、ロンした席の区別を付けた。打牌時点のリーチ成立者（R1なのでちょうど1人）
  かどうかで、ロンされた打牌を「リーチ者だけ」「リーチ者以外だけ」「両方（複数ロン）」へ
  排他的に分け、放銃支出を支払先ごとに「リーチ者へ」「リーチ者以外へ」へ分ける。
  リーチ者以外を副露者・ダマへさらに分けることはしない。
- dev / baselineのR1の打牌数・ロンされた打牌数・放銃支出が#483の記録
  （3,724 / 82 / 496,200、4,154 / 63 / 373,100）と一致しなければ停止する。

リーチ者以外へのロンは、リーチ者の現物を選んでも避けられたとは限らない。測るのは放銃支出
だけで、押して得た和了収入は含まない。この結果だけで改修の良否は判断しない。

## 追加：現物の出所とChampionの現行経路（Issue #489）

[#489](https://github.com/lisbun/lisjong-arena/issues/489)は、lisbun/lisjong#299の設計の前提として
`champion-play`をもう一度拡張する。出力のschema versionは
`arena-riichilab-r1-choice-loss-champion-play-v3`で、#483 / #487の出力とは別fileに置く。

- 軸`…|genbutsu_source`を追加した。合法打牌候補の現物の出所を、`river`（リーチ成立者の
  捨て牌と同じ牌種の候補がある）、`passed_only`（現物の候補はあるが河の現物はない）、
  `none`、`unavailable`（判定不能）に分ける。
- 軸`…|genbutsu_source|champion_view`を追加した。固定したChampionを同じ`DecisionContext`で
  実行し、lisjongが判断と一緒に返す分析の`branch`をそのまま数える。Arena側でPush / Foldや
  守備filterを再実装しない。あわせて、Championが認識したリーチ者の数（宣言済み・成立前を
  含む）、`StructuralShantenEvaluator`による合法打牌後の最小向聴数、オーラスのトップ目か
  どうか、再現した行動が実戦の打牌と一致するかを付ける。
- 対象打牌（現物あり・非現物選択・最小向聴数1以上）がdev 330・baseline 390でなければ停止する。

Championの実行は現行経路の確認のためである。再現した行動が実戦の打牌と違う行は
「再現不一致」として数え、実戦の判断の説明には使わない。行動が一致した行についても、
実戦時の内部経路まで同一だったことの証明としては扱わない。分岐名の件数以外の分析値
（候補評価、選択理由）は出力に含めない。
