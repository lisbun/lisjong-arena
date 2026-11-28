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
plan.write_plan()                    事前固定plan（write-once）: participant・rules・revision・V1・下限・TRAIN / EVALのseed列
paired_run.produce_paired_split()    planの1 splitを1回ずつ実行し、captureと半荘結果を同じ実行からpairで保存
paired_run.read_paired_split()       planとの一致と、同じ実行のbinding（半荘ごとのdigest）を検査して読む
  └─ projection.project_record()     pre_draw → lisjongのp用・V用の射影、realized → lookup用カテゴリ
       └─ lisjong.draw_correction    p・V1・単位補正（κ = 1、lisjongが所有）
join.join_paired()                   seed blockごとの D と X（JoinedBlocks: planのidentity・split role付き）
kappa.estimate_kappa(plan, TRAIN)    planのTRAIN splitだけから κ̂（下限はplanの値）
kappa.freeze_kappa()                 planとTRAINのpaired filesを読み戻してjoin・推定し、write-onceで凍結
analysis.analyze_correction()        plan・凍結κ（事前に記録したdigest）・EVALを自分で読み戻して照合し、
                                     D'、r、paired block bootstrap、補正の記述統計
```

| 責務 | 所有 |
| --- | --- |
| 37カテゴリ・p・V1・単位補正（κ = 1）・V1の特徴定義 | lisjong（`lisjong.draw_correction`） |
| plan、paired producer、`pre_draw`からの射影、join、rank contrast、κの推定・凍結・適用、分析、置換不変性fixture | Arena（本module `lisjong_arena.draw_correction`） |

Arenaは価値と補正の意味を再定義・再計算しない。lisjongのAPIを呼ぶだけである。
paired producerは対局を実行する。実際のseedでの実行はpilot計画（別承認）で行う。

## 1.1 事前固定plan（`plan.py`、`arena-draw-correction-plan-v1`）

planはcanonical JSONのwrite-once fileで、identityはそのbytesのSHA-256。次を固定する。

| field | 内容 |
| --- | --- |
| `protocol_id` / `rules_digest` | learning engine AABBの値（protocol invariant、callerは変えられない） |
| `participants` | `candidate` / `incumbent`のbinding: `policy_identity`、`kind`（`constructor` / `loader`）、`factory`（`module:qualname`）、`artifacts`（name → SHA-256、loaderのとき）。両roleは異なる値 |
| `revisions` | `arena` / `engine` / `lisjong`（40桁） |
| `value_definition` | lisjongの`v1_definition()` |
| `minimum_blocks` | κ推定の下限（2以上） |
| `splits` | `TRAIN` / `EVAL`のordered seed list（空でない、重複なし、互いに素） |

具体的なseed番号・下限はpilot計画で固定する（testはfixture用の値だけを使う）。

### 実行前のgate（`participants.py`）

planがbindするのはcallerが渡すlabelではなく、実際に使うものから作り直したbindingである
（learning engine AABBの`participants.py`と同じ考え方）。`produce_paired_split`は、1半荘目を始める前に次を要求し、
どれかが違えば何も実行・保存しない。

- 渡された`ParticipantSource`（実際のfactory objectとartifact path）から、`module:qualname`（objectから読み、
  importし直して同じobjectであること。lambdaや局所関数は拒否）とartifact fileのbytesのSHA-256を計算し、
  planのbindingと完全一致する。同じplanでcandidate / incumbentのfactoryを取り違えた場合や、同じloaderに
  別のweightsを渡した場合は、ここで拒否される
- 測った実行環境（`measure_environment`: cleanなArena worktreeのHEAD、installedなlisjong-engine / lisjongの
  VCS commit、native `SOURCE_REVISION` = lisjong）がplanの`revisions`と一致する
- `loader`は`factory(**artifact_paths)`が「各半荘・各席で新しいPolicyを返すfactory」を返す契約。実pilotの
  participant（L2a・Arm Y等）のloaderとartifactは、実行Issueでplanに固定する

## 1.2 同じ実行のpair（`paired_run.py`、`arena-draw-correction-paired-run-v1`）

```text
<destination>/              write-once（stagingで読み戻し検査に通った場合だけ公開）
  paired-manifest.json      planのidentity・split role・seed列・participant・rules・revision・V1、
                            captureのmanifest / payloadのSHA-256、結果fileのbytes・SHA-256・行数、
                            半荘ごとの capture行のdigest と 結果rowのdigest
  capture/                  #596のprivileged artifact（そのままの検査で読む）
  results.jsonl             第1層の半荘row（canonical JSON、1半荘1行）
```

- `produce_paired_split`は、planのsplitの各seed × placement 0..5を1回だけ実行し、その実行の
  `CompletedMatch`から`CapturedHanchan`（#596の`finish`がhistoryと照合済み）と第1層の半荘rowを同時に作る。
  Policyは各半荘・各席ごとにfactoryから新しく作り、seatのidentityはplanの`participants`を記録する
- `read_paired_split`の検査（どれも失敗時はfail closed）
  - file集合、manifestのschema・information class・canonical bytes
  - planのidentity・role・seed列・participant・rules・revision・V1がplanと一致
  - captureは#596の`read_live_draw_capture`で読み、manifest bytes・payloadのSHA-256、計画（seed列 × 0..5）、
    producerのrevision、rulesがpaired manifest・planと一致
  - 結果fileのbytes・SHA-256・行数、各行のcanonical JSONと`validate_hanchan_row`、`(seed, placement)`列が計画と一致、
    **全rowの全seatのPolicy identityがplanの同じroleのidentityと一致**
  - 半荘ごとに、captureの行と結果rowからdigestを再計算してmanifestと一致
- 同じseed・全6配置・同じ局進行でも、別の実行（別participant等）の結果やcaptureへ差し替えたものは
  digestが合わずに拒否される。manifestのdigestまで書き直した内部では整合したartifactも、participantがplanと
  違えば拒否される。署名基盤ではないので、manifestごと正しいplanの値で作り直す改ざんまでは防がない
- 通常ツモは各局の最初に親が必ず行うので、captureの行がない局はない。bindingは半荘単位のdigestで、
  行による局ごとの照合（親・場風）とは独立に働く

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

公開の入口は`join_paired()`だけで、planと照合済みの`PairedSplit`だけを受け取る。以下はその内部でも行う照合である。

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

- `estimate_kappa(plan, train)`は、そのplanのTRAIN roleのpaired artifactからjoinしたblockだけを受け付け、
  seed列がplanのTRAIN seedと一致することを要求する。下限はplanの`minimum_blocks`（callerは変えられない）
- 値はFractionで、floatやNaNは型として拒否する（非有限値は入らない）。上限・縮小は設けない
- 凍結artifact（`arena-draw-correction-frozen-kappa-v2`、canonical JSON、write-once）に記録するもの:
  planのSHA-256、protocol id・rules digest・participant・revision・V1の定義・下限・TRAINのseed列（planと同じ値）、
  推定規則id、K、Ĉov・V̂ar・κ̂（既約分数）、status・分岐、TRAINのpaired manifestのSHA-256
- 凍結artifactを公開する入口は`freeze_kappa(path, plan_path=..., train_path=...)`だけで、planとTRAINの
  paired filesを自分で読み戻し、join・推定してから書く。呼び出し側が作ったblockや推定値は受け取らないので、
  凍結値の由来は実ファイルに結び付く（`estimate_kappa`は読み戻したblockに対する算術）
- readbackは**bytes**でcanonical JSONを照合し、未知・欠落field、schema・規則、planの値との一致、
  Kとseed数の一致、分数の既約性を検査して、**Ĉov・V̂ar・Kから分岐とκ̂を再導出**する

## 5. split

- TRAINとEVALのseed列はplanが事前に固定し、互いに素で重複しない
- TRAIN推定・凍結・評価は、それぞれplanの同じsplit（role・seed列）と一致することを要求する
  （roleの取り違え、別planのartifact、seedの部分集合を拒否する）
- seed blockはjoinが全6配置の完全なblockだけを作るので、同じseedの全placementは同じsplitにある
- 凍結前に評価結果を開かない、という時系列の独立性はコードだけでは証明できない。pilotの運用gate
  （凍結artifactのdigestを記録してから評価を実行する）として計画に書く

## 6. 補正の分析（`analysis.py`、実行はしない）

- 入口`analyze_correction(plan_path, frozen_path, frozen_sha256, evaluation_path, ...)`は、planを読み、
  凍結fileのbytesが**事前に記録した`frozen_sha256`**と一致することと凍結κがそのplanに属することを確かめ、
  評価のpaired artifactをそのplanで読み戻してEVAL splitであることを要求する。呼び出し側が作ったdataclassを
  受け取らないので、凍結値の差し替えや別participant・別rules・別revisionの評価はここで拒否される
- D'_b = D_b − κ̂X_b、r = V̂ar(D') / V̂ar(D)（厳密値と、その浮動小数）
- 区間はpaired block bootstrap: seed blockを(D_b, X_b)の組のまま復元抽出し、各resampleの r* の
  95% percentile区間（nearest rank）。比はFractionで厳密に求めてからfloatにし、非有限値は拒否する。
  resample数とseedは引数で、同じ入力・seedなら同じ結果
- 補正の記述統計: blockのκ̂Xの平均・最小・最大、drawerの単位補正の件数・平均・最小・最大、|κ̂X|の大きいblock
- 閾値や判定規則は持たない（pilot計画で事前に固定する）。算術だけの`summarize_blocks`はgateを持たない
- **退化は正常な結果として明示し、NaNを出さない**
  - K < 2、または V̂ar(D) = 0 → `R-NOT-ESTABLISHED`（r = null、区間も出さない）
  - どれかのresampleで D* が全部同じ（V̂ar(D*) = 0）→ `CI-NOT-ESTABLISHED`（区間 = null）。退化は厳密値で判定し、
    resampleを捨てない。小Kでは1つでも退化resampleがあると区間が出ないので、扱いはpilot計画で事前に固定する

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
| 立直後のツモ（裏ドラ有効） | 親が第1ツモで立直し、第2ツモで和了。公開中の表示牌の裏を4p（裏ドラ2枚）/ 中（0枚）にした2つの合法な状態で、どちらも立直成立後にツモがofferされ、裏ドラ枚数が実際に変わることをassertし、局全体を置換比較する |
| ロン（役なし） | 南の9s単騎（役なし）。未公開の表示牌・裏表示牌を8s（9sがドラ）にしても、ロンは合法にならない |
| ロン（役あり） | 南の5s単騎（断么九）。ロンが合法な反応window |
| 遅延槓ドラ | 大明槓（`DELAY_OPEN_KAN_DORA`）。槓ドラは南の打牌がロン以外で解決するまで未公開としてHに入り、公開後は外れる |
| 無作為の山 | 2つの山で局の終わりまで、全decision時点 |

各scenarioは、自分の中で実際に比べた時点に該当するprobe（九種・ツモ・立直後のツモ・ロン・大明槓と保留中の槓ドラ）が
現れたことと、未公開の表示牌が変わった比較があったことをassertする（test順やskipに依存しない）。
終局の精算は比べない（一致する必要はない）。

**このfixtureは条件の検査であって、pの不偏性の完全な証明ではない。** #390の仮定（局ごとの山を独立な一様順列と
理想化すること、各placementを単独で見たfiltration、E[N_h] / E[N_h²]の有限性）は証明していない。
fixtureがpassしない場合、pは NOT-ESTABLISHED であり、pilotへ進まない。

## 8. 情報境界

- capture artifact全体はprivileged（identityと実現牌を合わせると未来を再構成できる）。本moduleはofflineの分析だけで、
  Policy・selector・delivery・online decision pathへ何も渡さない
- lisjongへ渡すのは`pre_draw`から射影したallowlistの値だけで、実現牌はlookup用の別の値にする
- paired artifactは#596のcaptureを含むのでprivileged（`information_class`、`access` / `retention`を記録する）
- 凍結artifactと分析結果はseed・κ̂・分散等の集約値とdigestだけを持ち、牌の並びや手牌を持たない
- 分析結果は診断用の別artifactで、正式resultのfieldやclassificationを置き換えない
