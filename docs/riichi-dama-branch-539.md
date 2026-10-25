# riichi-dama-branch-v1：リーチ / ダマの分岐比較（#540の実装）

設計（正本）: [#539](https://github.com/lisbun/lisjong-arena/issues/539)の「設計確定」節。特徴API・D arm wrapper:
[lisjong #352](https://github.com/lisbun/lisjong/issues/352)（`lisjong.policies.riichi_dama_branch`、merge `a2b2dc9`、
[API文書](https://github.com/lisbun/lisjong/blob/a2b2dc97ff1e6657dbf34b50b29a7e8936613e11/docs/riichi-dama-branch-api.md)）。
本書は#540で実装した分岐実行・記録・plan・判定・strict verify・rehearsal照合・時間見積もりの契約である。
**実行は後続の実行Issueで行う**（本Issueでは実対局smokeを含む新規対局・rehearsal・seed予約・課金を行っていない）。

Heuristic仮説のDEVELOPMENT診断で、強さ比較・S1/S2・正式評価・Championの変更の根拠ではない。

## 1. 問いと比べるもの

Championが最初にリーチを選んだ判断（分岐点）で、同じ宣言牌をリーチせずに打ってその局をダマで続けた場合の、
分岐席の局収支の差 Δ = D − R を、事前に固定した部分集合S1 / S3で見る（#539 §1〜§5）。

- **R**: Champion（`placement-aware-speed-call`、設定`{}`）×4で半荘を最後まで実行する
- **D arm**: 分岐点ごとに同じseedで半荘の先頭から再実行し、分岐席だけlisjongの`RiichiDamaBranchPolicy`
  （inner = 新しいChampion）へ替える。他の3席はChampion
- 状態の複製（`MatchState`のdeepcopy等）はしない。engineは変更しない（pin `91af75e`のまま）
- 点数・待ち・役・振聴はlisjongの値をそのまま使い、Arenaで再計算しない

## 2. 分岐点・D arm・照合

`riichi_dama_branch_worker`（観測）と`riichi_dama_branch`（照合）に分ける。workerは観測値を返すだけで、
INVALIDの判定は親processの`evaluate_branch`が行う（verifyも同じ関数で再導出する）。

### R（`RObserver`）

| 判断 | 記録 |
| --- | --- |
| 全席の全判断 | (seat, 行動) を判断順に記録。decision index = 半荘内の通し番号。局の境界はengine `on_delivery`の局completion factで数える |
| `RiichiAction`が合法でChampionが選んだ | **分岐点**（その席・その局で最初の1回）。public context（`durable_local_game_record`のPolicyInput v3 lossless・合法手の元の順序。読み戻しが等しいことを記録時に要求）、局identity（場風・局・本場・親）、分岐直前までの全席の行動列digest |
| 分岐点の直後の判断（同じ席・次のdecision index、`RiichiAction`なし） | その`DiscardAction`を宣言牌として記録（赤のidentityとツモ切りflagを含む） |
| `RiichiAction`が合法でChampionが選ばなかった | 席ごとの件数だけ数える（無効にしない） |

半荘の後に、保存したcontextと宣言牌からlisjong `analyze_riichi_decision`で特徴を計算する（対局中には呼ばず、
Rの進行に影響させない）。ダマ選択の診断（`dama_choice`）もlisjongが新しいChampionで計算する。

### D arm（`d_arm`）

- 対象局の局completion factを`on_delivery`で受けたら、Arena専用の`BranchArmSettled`を送出して打ち切る
  （engineはcallback例外の伝播を契約にしている）。`run_policy_hanchan`へ`on_delivery`の受け渡しを追加した
- wrapperの`RiichiDamaBranchError`・`PolicyActionValidationError`はINVALIDとして記録する。半荘が対象局の精算を
  経ずに終わった場合もINVALID。それ以外の例外はworkerの失敗（STOP-INCOMPLETE）
- 記録: 分岐点のdecision index、分岐直前までの全席の行動列digest、分岐点のcontext digest、分岐後の行動列digest、
  wrapperの状態と差し替え回数、対象局で分岐席に適用された`RiichiAction`の数、対象局のcompletion fact
  （局の結果・和了者・放銃者・point deltas・精算後の点数、最終局なら終局時の素点）

### INVALID（1件でもあれば判定はSTOP-INVALID）

| 区分 | 条件 |
| --- | --- |
| R | 宣言牌の欠落（局の終了・直後でない判断）、同じ局で2回目のリーチ、contextの往復不一致、分岐contextの局identityが局記録と違う |
| 特徴 | lisjongの解析拒否（`RiichiDamaAnalysisError`・tile conservation等）、特徴の席・宣言牌が分岐と違う |
| D arm | D armがない、wrapperの状態が`branched`でない（`pending` / `missed`）、分岐点のdecision index・直前digest・contextがRと違う、分岐席のリーチが適用された、別の局で精算した、wrapperの前提違反 |
| 精算 | D armの局開始点（精算後の点数 − point deltas）がRの局開始点と違う、Δが100点単位でない、Rの精算が局記録と違う |
| rehearsal | D armの2回実行が一致しない |

R分岐とD armは`branch_ordinal`で一対一に結ぶ（席・seedの一致も要求する）。

## 3. Δ・部分集合・判定（#539 §3・§5・§6）

| 項目 | 固定値 |
| --- | --- |
| Δ | 分岐席の局収支（armごとに`point_deltas[seat]` = 精算後の点数 − 局の開始時点の持ち点。リーチ棒の支払い、供託の獲得、本場を含む）のD − R、100点単位の整数。Rで場に残ったリーチ棒は−1000に含まれる |
| 終局時の供託精算 | Δに含めず、armごとの別列（`final_riichi_stick_awards`）に記録する |
| W_live | 見えない残り枚数が1枚以上の待ち |
| S1 | 振聴でない、W_liveが空でない、W_liveの全ての待ちでダマのロンが`SCORED`かつ`ScoreLimit`が`MANGAN`以上 |
| 待ちの点数（#542で改訂） | lisjong `riichi-dama-features-v2`の値をそのまま使う。和了牌は原則として通常牌で、ルール上の通常牌の上限（赤ありでは各色の5は通常3枚）を評価入力（宣言後の手牌・副露・ドラ表示牌）が超える場合だけ赤牌で評価する（lisjong #354）。評価した和了牌（`winning_tile`）と赤の加点（`red_dora`）も保存する。赤の加点でS1の満貫判定が変わりうる。見えない残り枚数は公開情報から別に数える |
| S3 | 公開リーチ状態が`NONE`でない他家が1人以上、かつWの見えない残り枚数の合計が4枚以下 |
| 本実行 | seeds 539000..539023（24 seeds）× 1半荘、Champion×4、配置の入替なし = 96 seat-hanchans |
| seed authority | Seed Registryの既存engine hanchan domain、protocol `riichi-dama-branch-v1`、population `heuristic-candidate-engine-aabb-riichi-dama-branch-v1`、split DEVELOPMENT、owner = 後続の実行Issue |
| bootstrap | 部分集合に分岐が1件以上あるseedをcluster（m個、昇順）。部分集合ごとに`random.Random(539)`を新しく初期化し、`randrange(m)`をm回の復元抽出を10,000回。統計量 = 抽出clusterの全Δの合計 / 件数。P10 / P50 / P90 = 昇順の1000 / 5000 / 9000番目（nearest rank）。`Fraction`で厳密に計算 |

判定は次の順に1つだけ（allocatedのみ。rehearsalは判定なし）:

1. **STOP-INVALID**: INVALIDが1件以上
2. **STOP-INCOMPLETE**: timeout / OOM / 上限超過 / 未完了。保存物を公開せず、summaryを作らない（stop記録はdiagnostics）
3. **STOP-LOW-ACTIVATION**: 件数24以上かつseed数8以上の部分集合がない
4. **STOP-NO-SIGNAL**: 下限を満たした部分集合のどれでも「平均Δ > 0 かつ P10 > 0」が成り立たない
5. **PROCEED-TO-CANDIDATE-DESIGN**: 上記に当たらない。条件を満たした部分集合（`signal_subsets`）を記録する

summaryは部分集合ごとの件数・seed数・平均・中央値・Δの分布・P10 / P50 / P90、S1とS3の重なり、記述統計（全分岐・
ダマのロンに役がある待ちの有無別。判定に使わない）、armごとの分岐席の和了 / 放銃 / 流局 / その他の件数、
D armの差し替え回数、ダマ選択が宣言牌と一致した数、リーチが合法なのに選ばなかった判断の数、分岐ごとの行
（Δ・部分集合・INVALID理由）を持つ。判断行を独立標本とした検定は出さない。結果を見て指標・閾値・部分集合・
seedを変えず、追加run・retry・延長はしない。

## 4. 保存物（write-once）とstrict verify

推奨layout（plan digestは保存先を含むので、directory名は実行Issueで決める）:

```text
C:\Dev\lisjong-artifacts\issue-<実行Issue>-riichi-dama-branch-run\
  <label>/plan.json        riichi-dama-plan の出力（write-once）
  <label>/output/          公開する保存物（下表。全件揃ったときだけ公開）
  <label>/diagnostics/     STOP-INCOMPLETEのstop記録（成功outputへ昇格しない）
  <label>/verify.json      riichi-dama-verify --output
```

| file | schema | 内容 |
| --- | --- | --- |
| `hanchans.json` | `arena-riichi-dama-branch-v1-hanchans-v1` | Rの客観値: #513と同じ検査を通した局記録（`kyoku_record`。局ごとの結果・和了者・放銃者・移動・供託・本場・精算、終局）、終了理由・最終点・`CompletedMatch` digest・呼び出し数・席ごとのAction列digest・リーチが合法なのに選ばなかった判断の席ごとの件数 |
| `branches.jsonl` | `arena-riichi-dama-branch-v1-branches-v1` | 1行目header（plan digest・protocol / features version）、以降1分岐1行: seed・round ordinal・席・分岐点のdecision index・局identity・public context・Rの行動（`riichi`と`declaration`）・Dの行動（= `declaration`をリーチせずに打つ）・分岐直前のdigest・armごとの分岐後のdigest・精算・局の結果（`r_settlement` / `d`）・Dの差し替え回数 |
| `features.jsonl` | `arena-riichi-dama-branch-v1-features-v2` | lisjongの解析（`riichi-dama-features-v2`のJSON、またはlisjongの拒否理由）。branch idで結ぶ。客観記録とは別file |
| `summary.json` | `arena-riichi-dama-branch-v1-summary-v1` | 判定・分母・分位、上の3 fileのdigest |
| `timings.json` | `arena-riichi-dama-branch-v1-timings-v1` | taskごと（R / D / D-repeat）のwall・peak RSS、stageの経過（再導出しない計測値） |
| `SHA256SUMS` | — | 上の全fileのSHA-256 |

- `branches.jsonl`はLearning（#74）がリーチ / ダマの反実仮想labelとして再利用できるversioned source record
  （player-safe context、合法手、armごとの選択 / 適用行動、客観outcome、provenance）。encode済みの特徴やtraining用の
  labelを正本にしない。schemaはArenaがversionで管理し、未知のheader / fieldは拒否する。学習は範囲外
- 観測なしrehearsalは`hanchans.json`だけを持つ（branches / featuresなし）
- 局単位の客観記録（AGENTS.md「対局評価の実行前確認」）: Rの全局は`kyoku_record`に残り、和了率・放銃率・平均和了打点・
  平均放銃失点は局記録の`result`・`transfers`から集計できる。D armは対象局だけの客観値で、局を独立標本と扱わない

`riichi-dama-verify`（別process、保存物だけを読む）は次をすべて要求する:

- `SHA256SUMS`と実fileの完全一致（欠落・追加・変更を拒否）、期待するfile集合
- full schedule（seed順・不足・重複・順序の入替）、局記録の#513検査、分岐行の順序・一意性・R / D armの一対一、
  featuresとbranchesの一対一、判断数 = Policy呼び出し数
- 再導出に使うlisjong / native / Python / engineがplanと同じこと
- 保存したcontextと宣言牌から同じrevisionのlisjong特徴を再導出し、記録と完全一致
- 保存した精算からΔ・部分集合・bootstrap・判定を再導出し、summaryと完全一致。timingsの上限
- **再実行**（allocatedのみ）: 固定2 seed（539000・539023）のRと全D armを再実行し、局記録・分岐記録・特徴・
  D armの記録との完全一致を要求する（全分岐の再実行は本実行の2回目と同じ費用になるため、#539 §8のとおり2 seedだけ）

生成側と回収先で`SHA256SUMS`・verify出力の`files`を照合する。

## 5. rehearsal・時間見積もりgate（#539 §7）

| 段階 | 内容 |
| --- | --- |
| rehearsal（観測あり） | 開発seed 472005 / 472006（registry外）でRを実行し、全D armを別process（新しいworker）で2回実行する。2回の記録が違えばその分岐はINVALID |
| rehearsal（観測なし） | 同じseedでRだけを実行する（行動の列だけ記録） |
| `riichi-dama-rehearsal-compare` | 両planをverifyし、seedごとのAction列digest・`CompletedMatch` digest・局記録・終局・最終点・呼び出し数・判断数の一致と、観測ありplanのINVALID 0件を要求する。不一致はSTOP-INVALID（照合記録は書く） |
| `riichi-dama-estimate` | compareを通したrehearsalの実測から P = 24 × Rの平均wall + n̂ × D armの平均wall、n̂ = max(seat-hanchanあたりの分岐数, 3.45) × 96 を計算する（n̂は分数で記録）。**P > 3時間ならNOT-STARTED-BUDGET**（記録を書いて終了コード3） |
| allocated plan | WITHIN-BUDGETの見積もりdocumentを必須にし、記録した値からの再計算・判定・digest、rehearsalと同じ環境・Arena revisionであることを要求する |

| 上限 | rehearsal | 本実行 |
| --- | --- | --- |
| 1半荘（R）/ 1 arm（D） | 600秒 | 600秒 |
| stageのwall（max(monotonic, realtime)） | 1時間 | 4時間 |
| process tree | 4 GiB（親 + workerのpeak RSSの和、OS側でも強制） | 同左 |
| worker | 1 | 1 |

## 6. CLI

```bash
python -m lisjong_arena.heuristic_candidate_engine_aabb riichi-dama-plan \
  --run-kind {allocated,rehearsal} --observation {observed,unobserved} \
  --owner-issue lisbun/lisjong-arena#N [--allocation-identity ID] [--time-estimate E] \
  --limit-enforcement "<OS / process treeの強制方法>" --wheel W \
  --run-output <label>/output --diagnostics <label>/diagnostics --output <label>/plan.json
python -m lisjong_arena.heuristic_candidate_engine_aabb riichi-dama-run --plan P
python -m lisjong_arena.heuristic_candidate_engine_aabb riichi-dama-verify --plan P --output V
python -m lisjong_arena.heuristic_candidate_engine_aabb riichi-dama-rehearsal-compare \
  --observed P1 --unobserved P2 --output C
python -m lisjong_arena.heuristic_candidate_engine_aabb riichi-dama-estimate \
  --observed P1 --unobserved P2 --output E
```

- seeds・timeout・RSS・wall・判定・verifyで再実行するseedはprotocol invariantでCLIから変えられない。
  `--allocation-identity`と`--time-estimate`はallocatedだけ
- planはclean merged mainのHEAD・実行環境（lisjong / native / engine）・RuleSet全field / digestを束縛する。
  run前後のlive gateでHEAD・環境・allocationを再照合する

## 7. 情報境界

- 特徴APIとwrapperの入力は、その判断のpublic `DecisionContext`と自分の宣言牌だけ。他家の手牌・山・裏ドラ・
  privileged observer data（`on_transaction_observation`）をlisjongへ渡さない
- 宣言牌はRの判断から得るが、同じ席が同じ情報状態で行った自分の選択であり、隠れた情報の流入ではない
- 局の終わりと精算はplayer-safeな`on_delivery`の局completion factだけから読む。客観記録（branches / hanchans）と
  lisjongの解析（features）は別fileにする

## 8. testで固定したこと

[test_engine_aabb_riichi_dama_branch.py](../tests/test_engine_aabb_riichi_dama_branch.py)（実engineの対局・実RiichiEnvは
使わない。半荘は#513のscript X（engineの`MatchState`へ局の結果を直接与えて精算し、Policyは呼ばない）、D armの局
completion factはengineの`project_round_completion`で作る。特徴APIとwrapperは実lisjong）:

- 分岐点の検出: 最初のリーチ選択と直後の宣言牌、合法なのに選ばなかった判断の件数、直後でない宣言・局の終了・
  別局のcontext・同じ局の2回目のリーチのINVALID、局数の不一致
- D arm: 分岐点で指定の打牌・以降のリーチの差し替え・精算後の打ち切り、対象局外のリーチの素通し、指定の打牌が合法で
  ない場合のINVALID（例外で落ちない）、打ち切り以外の例外の伝播、対象局の精算なしの終了
- 照合: 直前digest・decision index・context・wrapperの状態・適用されたリーチ・別局の精算・D armの記録の欠落・2回実行の
  不一致・局開始点の不一致・Rの精算の改ざん・lisjongの拒否のINVALID
- Δと精算: Rの立直供託（−1000）、流局で場に残ったリーチ棒と終局時の供託精算（別列）、和了 / 放銃 / 流局の分類
- 部分集合の境界: 満貫と7700（切り上げ満貫なし）、W_live外の待ち、振聴、見えない残り枚数4枚 / 5枚、他家リーチなし
- 判定: 24件 / 8 seedsの境界（含む）、P10 = 0のSTOP-NO-SIGNAL、INVALID優先、下限を満たした部分集合だけのsignal
- bootstrap: 参照実装（nearest rank）との一致、部分集合ごとの`random.Random(539)`初期化（評価順序に依存しない）
- plan: 固定seeds・上限・判定・verify seeds、drift・digestの検出、owner、観測なしallocated・rehearsalの見積もり拒否、
  見積もりの必須・NOT-STARTED-BUDGET・別環境・改ざんの拒否、見積もりの式、他domainのallocationとの重複、write-once
- run / verify: PROCEED / LOW / NO-SIGNAL / INVALID、宣言牌の欠落でD armを実行しない、部分実行の非公開とstop記録、
  write-once、2 seedの再実行一致と不一致の検出、hash・欠落・追加・順序・重複・特徴・scheduleの改ざん検出、環境不一致
- rehearsal: 観測あり / なしの一致と不一致、D armの2回実行の不一致、見積もりからallocated planまで

## 9. 実行Issueへの引継ぎ（未確定の実値）

実行前に次を実値で揃え、planへ束縛する（本書の時点では存在しない値を捏造しない）:

1. **revision**: Arena #540のmerge後main full SHAとclean checkout、lisjong pin `84e91932cc65428d8a5d0c3f3063ccb6911dc51d`（#542でv2へ更新。旧`a2b2dc9`）、
   engine `91af75e3aa11520c3b0543719dc74bb4c517ee06`
2. **wheel**: lisjong main CI run [40156527731](https://github.com/lisbun/lisjong/actions/runs/40156527731)の
   `lisjong-native-wheel-84e9193…`、`lisjong_native-0.1.0-cp314-cp314-manylinux_2_28_x86_64.whl`（301,069 bytes、
   SHA-256 `ed439a9dfa194bf3fa5d4858f0066c68080cdee0901180dcc7bfc88249f1cfc6`）。実行環境で`sha256sum --strict -c SHA256SUMS`、
   `verify-wheel`、`aws_backend --backend rust --wheel`（SOURCE_REVISION / API 3 / numeric・R5 probe）を取り直す
3. **Rust-only採用gate**: この新pinで[Rust-only gate](engine-aabb-evaluation.md#84-rust-only-gaterust-gate-531)をPASSさせる
   （上限1時間・1半荘300秒。過去のPASSを流用しない）
4. **環境**: #539 §7のとおりWSL2 Ubuntu-24.04（ユーザーPC）の専用user scope（`MemoryMax=4G`・`MemorySwapMax=0`・
   `OOMPolicy=kill`・`TimeoutStopSec=3`・`RuntimeMaxSec`）と外側のwatchdog（max(realtime, monotonic)）。AWSは使わない。
   kill / timeout / memoryの合成試験を実行環境で確認し、`--limit-enforcement`に方法を書く。他の重い処理と同時に実行しない
5. **順序**: Rust-only gate → rehearsal（観測あり / なしの各plan → run → verify → compare → estimate、1時間以内）→
   **P ≤ 3時間のときだけ**seed lock（全domainの既存allocationと既知のregistry外seed 472000..472006・513000・101..107を
   539000..539023と照合し、既存workflowでreserve / readback。owner = 実行Issue、protocol `riichi-dama-branch-v1`）→
   本実行plan（`--time-estimate`）→ run → verify（2 seedの再実行を含む）→ 回収とhash照合（4時間以内）。全実行工程6時間。
   使用後は既存規則でCOMMITTED、途中で失敗してもFREEへ戻さない。P > 3時間ならseedを予約せず、NOT-STARTED-BUDGETを
   記録して設計からやり直す
6. **保存先**: `C:\Dev\lisjong-artifacts\issue-<実行Issue>-riichi-dama-branch-run\`（write-once。WSLから`/mnt/c`へ直接書く）
