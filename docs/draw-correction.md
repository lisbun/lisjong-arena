# 通常ツモ補正の射影・join・rank contrast・κの推定/凍結と未公開牌置換fixture（#598）

Issue: [lisbun/lisjong-arena#598](https://github.com/lisbun/lisjong-arena/issues/598)
（設計 [lisbun/lisjong#390](https://github.com/lisbun/lisjong/issues/390)、
価値と補正のAPI [lisbun/lisjong#391](https://github.com/lisbun/lisjong/issues/391)、
capture [#596](engine-live-draw-capture.md)、parent [lisbun/lisjong-project#134](https://github.com/lisbun/lisjong-project/issues/134)）

> Status: **実装とfixtureだけ**。pilot・TRAINの実行・seed予約・新規対局・AWSは行っていない。
> 正式protocolの主指標・結果・判定、既存の正式経路は変えない（opt-inも追加しない）。
> 初回の対象は **learning engine AABB（`arena-learning-candidate-engine-aabb-v1`）の順位差だけ**。

## 1. 構成と責任分界

```text
read_live_draw_capture()（#596、privileged-offline-only）
  └─ projection.project_record()       pre_draw → lisjongのp用・V用の射影、realized → lookup用カテゴリ
       └─ lisjong.draw_correction       p・V1・単位補正（κ = 1、lisjongが所有）
join.join_capture_with_hanchans()      captureの行 × 同じ実行の第1層の半荘row
  → JoinedBlocks（seed blockごとの D と X）
kappa.estimate_kappa()                 TRAIN blockから κ̂
kappa.write_frozen_kappa()             write-onceの凍結artifact（strict readback）
analysis.analyze_correction()          凍結κ̂で評価blockの D'、r、paired block bootstrap、補正の記述統計
```

| 責務 | 所有 |
| --- | --- |
| 37カテゴリ・p・V1・単位補正（κ = 1）・V1の特徴定義 | lisjong（`lisjong.draw_correction`） |
| `pre_draw`からの射影、join、rank contrast、κの推定・凍結・適用、分析、置換不変性fixture | Arena（本module `lisjong_arena.draw_correction`） |

Arenaは価値と補正の意味を再定義・再計算しない。lisjongのAPIを呼ぶだけである。

## 2. 射影（`projection.py`）

- 入力は`read_live_draw_capture()`が検査済みで返す`LiveDrawRecord`だけ
- p用: 全席の`hand_tiles`・副露の`tiles`・**鳴かれていない**河（`called_by`が`null`）・公開表示牌を、
  37カテゴリ（34牌種の赤でない牌 + 赤5m / 5p / 5s）の枚数へ。鳴かれた河の牌は鳴いた席の副露として数える
  （二重に数えない）。`live_tiles_remaining`はlisjongがNの照合に使う
- V用: `pre_draw.current_seat`（drawer）とdrawerの`hand_tiles`
- 物理牌ID（0..135）は`tile_id // 4`の34牌種へ畳み込み、赤五（`STANDARD_TILES[id].is_red`、copy 0）は
  34 / 35 / 36へ分ける。copy IDはlisjongへ渡さない
- `identity`・`realized`・post digestはlisjongへ渡さない。実現牌のカテゴリは別の値として、support全体の
  Vを計算したあとのlookupにだけ使う

## 3. join（`join.py`）

| 検査 | 失敗時 |
| --- | --- |
| captureの計画が、seedごとにplacement 0..5を順に並べた完全なseed blockの列（seedは重複しない） | fail closed |
| 半荘row（局recordの`validate_hanchan_row`、AABB layoutの鍵・seat）の`(seed, placement)`列が計画と順序まで一致 | fail closed（欠け・計画外・順序違い・別seed） |
| 半荘ごとに、captureの局数と局recordの`round_ordinal` 1..nが一致 | fail closed |
| 各行の`pre_draw`の親・場風が、同じ`round_ordinal`の局recordの値と一致 | fail closed |
| 各半荘のrankが1..4の並べ替え、roleごとに12 seat-result、R_A + R_B = 60 | fail closed（`block_rank_totals`） |

部分的なblockは採用しない。同じseedの一部のplacementだけを持つartifactは、それだけで拒否される。

### rank contrastと補正contrast

```text
D(s) = (R_B(s) − R_A(s)) / 12      learning engine AABBの主指標（定義・向きのまま、正 = candidate有利）
X(s) = (X_B(s) − X_A(s)) / 12      X_role = roleの12 seat-resultについて Σ_t c_t^{(seat)}
D'(s) = D(s) − κ X(s)
```

c_tはlisjong #391の単位補正（κ = 1）。V1はdrawerに+g（向聴）、他家に−g/3で、gも順位も小さいほど良いので、
Xが正（Bに悪いツモ・Aに良いツモ）ならDも正に寄る。したがってκ ≥ 0。D・X・D'はFractionで厳密に持つ。

## 4. κの推定と凍結（`kappa.py`）

```text
κ̂ = max(0, Ĉov(D, X) / V̂ar(X))      標本共分散・標本分散（分母 K − 1）
  K < minimum_blocks   -> κ̂ = 0、KAPPA-NOT-ESTABLISHED（insufficient-blocks）
  V̂ar(X) = 0           -> κ̂ = 0、KAPPA-NOT-ESTABLISHED（zero-variance）
  Ĉov(D, X) ≤ 0        -> κ̂ = 0、ESTABLISHED（non-positive-covariance）
  それ以外             -> κ̂ = Ĉov / V̂ar、ESTABLISHED
```

- `minimum_blocks`は2以上で、defaultを持たない。実際の値はpilot計画で固定して渡す
- 値はFractionで、floatやNaNは型として拒否する（非有限値は入らない）。上限・縮小は設けない
- 凍結artifact（`arena-draw-correction-frozen-kappa-v1`、canonical JSON、write-once）に記録するもの:
  protocol id・rules digest、参加Policyのidentity（candidate / incumbent）、V1の特徴定義（lisjongの`v1_definition()`）、
  splitのidentity（TRAINのseed一覧とそのSHA-256）、推定規則id、`minimum_blocks`、K、Ĉov・V̂ar・κ̂（既約分数）、
  status・分岐、TRAIN captureのpayload SHA-256とproducer revision
- readbackは、未知・欠落field、schema・規則・protocol・rules digest、V1の定義の一致、seed一覧の整列・一意性・digest、
  Kとseed数の一致、分数の既約性を検査し、**Ĉov・V̂ar・Kから分岐とκ̂を再導出して一致を要求**する。canonical bytesも照合する
- 書き込み時にも、渡された推定値がTRAIN blockから再計算した値と一致することを要求する

## 5. split

- TRAINと評価のseedは互いに素で、どちらの中でも重複しない（`require_disjoint_split`）
- seed blockはjoinが全6配置の完全なblockだけを作るので、seedが互いに素なら同じseedの全placementは同じsplitにある
- 評価（`analyze_correction` / `corrected_differences`）は、凍結artifactを読み戻した`FrozenKappa`だけを受け取り、
  TRAINのseedを含む評価、V1の定義・protocolが違う評価を拒否する

## 6. 補正の分析（`analysis.py`、実行はしない）

- D'_b = D_b − κ̂X_b、r = V̂ar(D') / V̂ar(D)（厳密値と、その浮動小数）
- 区間はpaired block bootstrap: seed blockを(D_b, X_b)の組のまま復元抽出し、各resampleの r* の
  95% percentile区間（nearest rank）。resample数とseedは引数で、同じ入力・seedなら同じ結果
- 補正の記述統計: blockのκ̂Xの平均・最小・最大、drawerの単位補正の件数・平均・最小・最大、|κ̂X|の大きいblock
- 閾値や判定規則は持たない（pilot計画で事前に固定する）
- **退化は正常な結果として明示し、NaNを出さない**
  - K < 2、または V̂ar(D) = 0 → `R-NOT-ESTABLISHED`（r = null、区間も出さない）
  - どれかのresampleで D* が全部同じ（V̂ar(D*) = 0）→ `CI-NOT-ESTABLISHED`（区間 = null）。退化は厳密値で判定する

## 7. 未公開牌の置換不変性fixture（pのgate、`tests/test_draw_correction_permutation_invariance.py`）

engineの既存pin（`91af75e`）を使うintegration test。testだけで`MatchState`の山を差し替え
（`lisjong_engine.match_state.create_round_wall`をpatch）、productionにabstractionを追加しない。

1. 山Wで局を進め、各decision時点t（打牌・立直宣言牌・3種の反応window）までの選択列を記録する
2. 時点tで誰にも見えていない牌の集合H（未来の山、王牌のうち未公開の表示牌・裏表示牌・未使用の嶺上牌・補充牌）を求め、
   H上の固定点のない巡回置換σで初期の山を写したW'を作る（H以外の位置は変えない。136枚の完全集合を保つ）
3. W'で同じ選択列をtまで再生し、次が一致することを要求する
   - 全4席の`legal_actions`
   - decisionを求められている席の`build_seat_observation`と、Policyへ渡る選択肢の射影（`project_legal_actions`）
   - 全席の手牌・河・副露・立直・一発・フリテン・自風と、公開面（phase・revision・手番・公開表示牌・残り枚数・
     嶺上の残り・ツモ牌・反応対象・保留中の槓ドラ・立直の解決）
4. 置換が自明でないこと（未公開の牌が実際に動いたこと）も毎回確かめる

| scenario | 内容 |
| --- | --- |
| 九種九牌 | 親の第1ツモで九種のprobeが評価される時点 |
| ツモ | 天和の形。公開中の表示牌の裏を4p（5pが裏ドラ）/ 中に変えた2通り |
| ロン（役なし） | 南の9s単騎（役なし）。未公開の表示牌・裏表示牌を8s（9sがドラ）にしても、ロンは合法にならない |
| ロン（役あり） | 南の5s単騎（断么九）。ロンが合法な反応window |
| 遅延槓ドラ | 大明槓（`DELAY_OPEN_KAN_DORA`）。槓ドラは南の打牌がロン以外で解決するまで未公開としてHに入り、公開後は外れる |
| 無作為の山 | 2つの山で局の終わりまで、全decision時点 |

最後のtestは、全scenarioを通してツモ・ロン・九種・大明槓のprobe、保留中の槓ドラ、未公開の表示牌の変更を
実際に比べたことを確かめる。終局の精算は比べない（一致する必要はない）。

**このfixtureは条件の検査であって、pの不偏性の完全な証明ではない。** #390の仮定（局ごとの山を独立な一様順列と
理想化すること、各placementを単独で見たfiltration、E[N_h] / E[N_h²]の有限性）は証明していない。
fixtureがpassしない場合、pは NOT-ESTABLISHED であり、pilotへ進まない。

## 8. 情報境界

- capture artifact全体はprivileged（identityと実現牌を合わせると未来を再構成できる）。本moduleはofflineの分析だけで、
  Policy・selector・delivery・online decision pathへ何も渡さない
- lisjongへ渡すのは`pre_draw`から射影したallowlistの値だけで、実現牌はlookup用の別の値にする
- 凍結artifactと分析結果はseed・κ̂・分散等の集約値だけを持ち、牌の並びや手牌を持たない
- 分析結果は診断用の別artifactで、正式resultのfieldやclassificationを置き換えない
