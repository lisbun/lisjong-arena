# Heuristic candidate engine AABB half-game protocol v1 — 仕様（#452）

Issue: [lisbun/lisjong-arena#452](https://github.com/lisbun/lisjong-arena/issues/452)
（parent [lisbun/lisjong-project#83](https://github.com/lisbun/lisjong-project/issues/83)）

Protocol identity:

```text
arena-heuristic-candidate-engine-aabb-half-v1
```

> Status: **仕様固定のみ**。実装、seed予約、AWS起動、対局実行、校正、wheel / pin変更、
> Champion変更は行っていない。本書の仕様を満たす実装と校正が終わり、親#83のgateが
> 完了するまで、このprotocolによる正式評価は開始しない。旧protocolへの自動fallbackもしない。

本書は、Heuristic family-internalの正式半荘評価をlisjong-engine上で行う新protocolと、
その前提になるbridge / backend検証・局単位記録の仕様を固定する。旧
[`arena-heuristic-candidate-aabb-half-v1`](heuristic-candidate-aabb-half.md)（RiichiEnv 0.4.10）
へのbackend option追加ではなく、独立したprotocol identityである。旧protocolの結果・lock・
artifact・判定、Overall protocol（`arena-overall-champion-aabb-half-v1`）は変更しない。

記述の区別:

| 表記 | 意味 |
| --- | --- |
| **確定** | ユーザー判断（2026-10-09）または既存codeから確定した事項 |
| **未確認** | sourceは読んだがfixture・実行で確かめていない事項。推測で埋めていない |
| **判断待ち** | 値や方針をユーザーが決める必要がある事項 |

未確認・判断待ちは§12へまとめる。

## 1. 決定事項（2026-10-09 ユーザー判断）

| 項目 | 決定 |
| --- | --- |
| protocol ID | `arena-heuristic-candidate-engine-aabb-half-v1` |
| engine revision | `91af75e3aa11520c3b0543719dc74bb4c517ee06` を予定値として固定（§5.1） |
| RuleSet | `RuleSet.default()`（`project-standard-v1` version 1）をそのまま使う |
| 主指標 | lockしたRuleSetでのengine `FinalPlayerScore.final_points`。protocol側で再計算しない |
| block構成 | 1 block = 1 seed × 6配置（4席から候補の2席を選ぶ全通り） |
| CI・採否 | 旧v1と同じ型（block平均Dの両側95%正規近似区間）。非劣性判定は本versionでは採らない |
| lisjong backend | Rustを正式実行、Pythonを照合基準 |
| 副露fact | 局精算recordとは分離した第2層recordとして必須 |

主指標はRuleSetの値（端数処理、飛び賞罰、uma、返し点、同点規則）に従ってengineが計算する。
別のRuleSetで評価する場合は、実行時optionで切り替えず、別のprotocol ID・rules digest・
seed domainを割り当てる。異なるRuleSetのDは混ぜない。

## 2. Protocol shape

### 2.1 Participants

```text
A = candidate   lockがbindするHeuristic candidate
B = incumbent   lockがbindする比較相手（現Heuristic Champion、または固定C0）
```

- participantはprotocolへhard-codeせず、lockがidentity・factory・provenanceをbindする（§5.4）
- Policy instanceは半荘ごと・seatごとにfactoryから新規生成し、seat間・半荘間で共有しない
- candidateとincumbentのidentity、factory bindingはそれぞれ異なっていなければならない
- 累積進捗（対C0）と昇格判断（対現Champion）は別のeventとして扱い、標本を共有しない。
  1つのeventのincumbentは1つだけである

### 2.2 Block / 配置

seat index 0..3はengine `Seat` のEAST / SOUTH / WEST / NORTH（東1局の起家はseat 0固定）。
1 seed blockは次の6配置を、この順序で実行する。

| placement | seat 0 | seat 1 | seat 2 | seat 3 | Aのseat |
| ---: | :---: | :---: | :---: | :---: | --- |
| 0 | A | A | B | B | 0, 1 |
| 1 | A | B | A | B | 0, 2 |
| 2 | A | B | B | A | 0, 3 |
| 3 | B | A | A | B | 1, 2 |
| 4 | B | A | B | A | 1, 3 |
| 5 | B | B | A | A | 2, 3 |

```text
1 block            = 1 seed × 6 hanchan = 24 seat-results
role別seat-results = 12 / block
role別seat exposure = 各seatを3回 / block
block数 K           = lockがbindする（protocol定数にしない。§2.3）
hanchan数           = 6K
```

- 同じblockの6半荘は同じmatch seedを使う。engineの牌山は `(match_seed, round_ordinal)` だけから
  決まるため、同じengine revisionでは配置によらず同じround ordinalの牌山が一致する。
  これを§6.3のfixtureで確認する
- 実行順序は「lockのordered seedsの順 → placement 0..5の順」を正本とする。並列実行しても、
  rawの記録順と検証順はこの順序へ正規化する
- #385のfocal paired設計（focal seat 1席、8半荘 / seed）はこのprotocolの根拠にしない。
  #385のseed、budget、SD仮定も流用しない

### 2.3 Block数

旧v1は100 blockをprotocol定数にしていたが、本protocolではblock数Kをeventごとに決める
（project #84「毎回400半荘とは固定しない」）。

- Kは校正工程（§11）の結果から、**結果を見る前に**決めてlockへ書く
- lockは、Kの根拠（狙う効果、block差SDの推定とその出所、検出力、費用見積り）を記録する
- Kはcaller defaultを持たない。lockのK、ordered seedsの件数、seed allocationの件数が
  一致しなければfail closedする
- `K >= 2` を要求する（SDが定義できない）。それ以上の下限は**判断待ち**
- result exposure後のblock追加・差し替え・部分採用はしない

## 3. 主指標

### 3.1 seat-resultの得点

```text
final_points(seat) = CompletedMatch.final_score.for_seat(seat).final_points
```

- lockした `RuleSet.default()` でengineが計算した値をそのまま使う。Arena側で素点式
  （旧v1の `(final_points - 30000) / 1000 + uma + oka`）を重ねて適用しない
- 内部単位は **1 = 0.1 pt**。保存・集計は内部単位の整数で行い、表示だけ `/ 10` してptにする
- 端数処理（非首位を0方向へ整数pt化し残差を首位へ）と飛び賞罰（±10 pt）を含む。
  したがって旧v1のDとは別のendpointであり、新旧のD・SD・標本を混ぜない
- rankは `FinalPlayerScore.rank` を使う（`SEAT_ORDER` では常に一意な1〜4）

### 3.2 Block統計

seed `s` の6半荘について:

```text
S_A(s) = candidate 12 seat-resultsの final_points 合計（内部単位）
S_B(s) = incumbent 12 seat-resultsの final_points 合計（内部単位）
D(s)   = (S_A(s) - S_B(s)) / 12 / 10        [pt / seat-result、> 0 で candidate advantage]
```

- 各半荘で4席の `final_points` 合計は0なので、`S_A(s) + S_B(s) = 0` が常に成り立つ。
  これをblockごとに検証し、成り立たなければfail closedする
- 統計の単位は **seed block**。統計Nは `K` であり、`6K` 半荘や `12K` seat-resultsではない
- rawには `S_A(s) - S_B(s)` を内部単位の整数で保存し、割り算は集計時に1回だけ行う

### 3.3 区間

```text
point estimate  mean D = (1/K) Σ D(s)
interval        mean D ± 1.96 × SD(K-1) / sqrt(K)      両側95%正規近似
```

`1.96` は既存 `paired_evaluation.INTERVAL_Z`。block数が小さいeventでの近似の妥当性は
校正工程で確認する（§12）。

## 4. 採否とfail-closed

### 4.1 Classification

```text
interval lower > 0   -> CANDIDATE SUPERIOR
interval upper < 0   -> CHAMPION SUPERIOR
otherwise            -> INCONCLUSIVE   （境界0を含む）
invalid evidence     -> STOP / INVALID （result artifactを書かない）
```

- labelは旧v1と同じ文字列を使うが、classification rule IDは新しく割り当てる:
  `heuristic-candidate-engine-seed-block-final-points-normal-approx-95-v1`
- incumbentがC0のeventでも同じlabelを使い、resultがincumbentのroleを併記する
- 非劣性判定は本versionにない。`INCONCLUSIVE` を「同等」「非劣性」と読み替えない
- 副指標（§10）は主指標の区間・classificationを上書きしない
- Champion昇格判断はこのprotocolの外（lisjong-project governance）で扱う

### 4.2 Fail-closed条件

次のいずれかが1件でもあれば、event全体を `STOP / INVALID` とする。失敗した半荘を
skip・drop・再seed・選択的再実行しない。成功分だけの結果は返さない。

| 区分 | 条件 |
| --- | --- |
| 環境 | installed lisjong / lisjong-engine / Arenaのrevision、Python、native wheelのhash・source revision・API versionがlockと異なる。worker間で異なる |
| rules | 実行時の `RuleSet` の全field値、rules digestがlockと異なる。未知field・欠落fieldがある |
| population | seedの件数・順序・重複、allocation binding、live ledgerとの照合がlockと異なる |
| 配置 | 半荘の件数が `6K` でない。`(seed, placement)` の欠落・重複がある。seatのrole / identityが§2.2と異なる |
| 実行 | engine / bridge / Policyの例外、`PolicyActionValidationError`、mapping失敗、runnerのtimeout |
| 最終得点 | §7の整合検査に失敗 |
| 局単位record | §10.4の検査に失敗（第1層）。局recordの欠落・重複 |
| 統計 | `S_A(s) + S_B(s) != 0`、recorded statisticsがrawからの再導出と一致しない |
| artifact | write-once destinationに既存fileがある。payloadのhash・件数がmanifestと異なる |

- fallback行動（Policy失敗時の既定動作への置き換え）は実装しない。発生し得ない設計とし、
  recordにも「置き換えゼロ」を検証できる形で残す（selector呼び出し数とPolicy呼び出し数の一致）
- engineにはstep上限がない（旧v1の `max_steps = 10,000` に当たる値を持たない）。西4局で
  必ず終局するが、それ以前の連荘回数に上限はない。打ち切りはprotocolで定義せず、runnerの
  timeoutはevent全体の `STOP / INVALID` として扱う。timeoutの値は校正後に決める（**判断待ち**）
- 第2層record（副露等のevent fact）だけが不正な場合の扱いは§10.6に定める

### 4.3 No-rescue boundary

result exposure後に、seedの追加・差し替え、block数・主指標・区間方法・classification rule・
RuleSet・participantの変更、部分採用、副指標による救済を行わない。result artifactを書く前に
abortした実行は、lock・seed・destinationを変えずにlocked schedule全体をやり直す場合だけ
再実行できる。

## 5. Manifest / lockに固定する項目

lockは実行前に書くwrite-once documentで、`result_exposed = false` を持つ。resultとrawの
manifestはlockのdigestを参照する。

### 5.1 Engine revision

```text
lisjong-engine  91af75e3aa11520c3b0543719dc74bb4c517ee06   （予定値）
```

- installed distributionの `direct_url.json` のexact VCS revisionと照合する。
  `pyproject.toml` のpinを暗黙に継承しない（pinが動いてもprotocolの値は動かない）
- 2026-10-09時点のArena通常pinは同じ `91af75e`（#457 / PR #458で更新）。#452本文とproject文書が
  「通常pin」として挙げる `8735e89e1aea000ab59368d0368d476787827741`、#370 / #385が使った
  `96b9796c76ef5db8f3968f689a1ca6f3dfc9aa3b` とは別の版である
- 3版の `src` 差分は `driver.py`、`round_state.py`、`selector_decision.py`、
  `transaction_observation.py` だけで、`rules.py` / `final_score.py` / `match_state.py` /
  `settlement.py` は同一（`git diff --stat` で確認）。ただし、これは対局結果が3版で一致する
  ことの確認ではない。#366 / #367 / #385の実行実績を `91af75e` の実績として扱わない
- revisionを変える場合は、§6〜§9のfixtureを新revisionで再検証し、protocol versionを改める

### 5.2 RuleSet

`RuleSet.default()` の全fieldを値として保存する。名前（`project-standard-v1`）だけで一致と
みなさない。rules documentは `dataclasses.fields(RuleSet)` を列挙して作り、下表のfield集合と
一致しなければfail closedする（engine側でfieldが増減した場合に気付くため）。

| 区分 | field | 値 |
| --- | --- | --- |
| 識別 | `name` / `version` | `project-standard-v1` / `1` |
| 形式 | `match_format` / `player_count` | `hanchan` / `4` |
| 基準点 | `starting_points` / `return_points` / `first_place_target_points` | `25000` / `30000` / `30000` |
| 順位点 | `uma` | `(30, 10, -10, -30)` |
| 終局 | `bankruptcy_enabled` / `bankruptcy_threshold` | `True` / `0` |
| 終局 | `west_round_enabled` | `True` |
| 終局 | `dealer_win_end_enabled` / `dealer_tenpai_end_enabled` | `True` / `True` |
| 得点 | `rounded_mangan_enabled` | `False` |
| 得点 | `counted_yakuman_enabled` / `multiple_yakuman_enabled` | `True` / `True` |
| 本場 | `ron_honba_points` / `tsumo_honba_points_per_payer` | `300` / `100` |
| 供託・罰符 | `riichi_stick_points` / `noten_penalty_total` | `1000` / `3000` |
| 流局 | `nagashi_mangan_enabled` | `True` |
| パオ | `pao_enabled` / `pao_yaku` | `True` / `{DAISANGEN, DAISUUSHII}` |
| パオ | `pao_compound_yakuman_policy` | `full_hand` |
| 複数ロン | `double_ron_enabled` / `ron_resolution_policy` | `True` / `multiple_ron` |
| 複数ロン | `triple_ron_abortive_draw` | `True` |
| 複数ロン | `multiple_ron_honba_policy` / `multiple_ron_riichi_stick_policy` | `nearest_winner_to_discarder`（両方） |
| 途中流局 | `nine_terminals_abortive_draw_enabled` / `four_winds_abortive_draw_enabled` | `True` / `True` |
| 途中流局 | `four_kans_abortive_draw_enabled` / `four_riichi_abortive_draw_enabled` | `True` / `True` |
| 最終順位 | `final_points_rounding` | `toward_zero_remainder_to_first` |
| 最終順位 | `final_rank_tie_policy` | `seat_order` |
| 最終順位 | `bankruptcy_bonus_points` / `bankrupt_player_penalty_points` | `10` / `-10` |
| 立直・槓 | `riichi_ankan_policy` | `preserve_wait_and_decomposition` |
| 立直・槓 | `kan_dora_reveal_policy` | `delay_open_kan_dora` |
| 立直・槓 | `kokushi_ankan_chankan_enabled` | `False` |
| 立直・槓 | `riichi_minimum_points` / `riichi_minimum_live_wall_tiles` | `1000` / `4` |
| 役・符 | `double_yakuman_variants` / `double_wind_pair_fu` | `{}`（空集合）/ `4` |

- 実行時は `run_policy_hanchan(policies, seed=seed, rules=<lockから復元したRuleSet>)` のように
  RuleSetを明示して渡し、`rules=None` の既定値に依存しない
- 赤牌の枚数、喰いタン・後付けの可否など、`RuleSet` のfieldになくengine実装が固定している
  項目は、rules documentでは表現できない。engine revisionの固定で拘束し、内容は
  §6.3で確認する（**未確認**）

### 5.3 Game modeと終局条件

```text
game mode       lisjong-engine hanchan / 4 players（MatchFormat.HANCHAN）
開始            東1局、起家 seat 0（EAST）固定、全員 25000点（starting_scoresを渡さない）
進行            MatchState(seed, rules) -> run_hanchan()
```

終局判定はengine `_match_end_reason()` の順序をそのまま使う。manifestは下記を終局条件として
記録し、各半荘の `end_reason` を保存する。

1. 局精算後に0点未満の席があれば `bankruptcy`（局位置によらず最優先）
2. 西4局は親継続の有無によらず `final_round`
3. 南4局・西1〜3局で、親が継続し、かつ親が1位で30000点以上のとき、親和了なら `dealer_win`、
   荒牌流局の親聴牌なら `dealer_tenpai`
4. 南4局・西1〜3局で親が流れ、いずれかの席が30000点以上なら `target_reached`
5. それ以外は続行（南4局で親流れ・30000点未満なら西入）

1位席の判定は、同点なら東1局の席順（seat 0が優先）。途中流局は常に親継続として扱われる。

### 5.4 Participant provenance

participantごとに次を固定する。

| 項目 | 内容 |
| --- | --- |
| `policy_identity` | 明示的なidentity文字列（class名から暗黙導出しない） |
| `factory_binding` | module / callableの参照。factory・callable自体はartifactへ保存しない |
| `family` | `heuristic` であること |
| `lisjong_revision` | installed lisjongのexact VCS revision |
| `behavior_configuration` | factoryへ渡す設定値の全量と、そのcanonical JSON digest。設定を持たない場合は空objectを明示 |
| `backend` | `rust` と、native wheelのfilename・SHA-256・`SOURCE_REVISION`・`API_VERSION` |
| `artifact` | 持たない（下記） |

- 学習artifact（checkpoint、weights等）を使うparticipantは **本versionでは受理しない**。
  旧v1が「checkpointなし」を受理範囲としていたことを、新protocolの対応範囲として仮定しない。
  必要になった時点で、artifactのhash・identity・load経路・Python/Rust比較の範囲を定めて
  protocol versionを改める（follow-up候補）
- 1つの実行環境にinstallできるlisjong revisionは1つなので、candidateとincumbentは同じ
  lisjong revisionから生成する。固定C0（`PlacementAwareSpeedCallPolicy`、lisjong
  `2a9debebdbbe4d10841fa4371a6cf6bf19ce9de1`）を、より新しいrevisionの候補と比べるときに
  C0の挙動をどう固定するかは**未確認**（§12）

### 5.5 Seed lock

```text
seed domain   lisjong-engine-project-standard-v1-hanchan-v1   （既存 LISJONG_ENGINE_HANCHAN_SEED_DOMAIN）
owner         eventのowner Issue
protocol      arena-heuristic-candidate-engine-aabb-half-v1
population    heuristic-candidate-engine-aabb-<event番号>
split         FORMAL-EVAL
```

- lockはallocation bindingをlive ledgerへ照合してbindし、実行の前後でも再照合する
- 全domainの他allocation、registry外で使ったdiagnostic seed、校正・開発比較・fixtureで使った
  seedと重ならないことを要求する。正式評価用の未使用seedを動作確認に使わない
- 校正用seedは別allocation（split `CALIBRATION`）とし、正式populationと分離する
- lockはordered seeds全体と、§2.2のscheduleのcanonical JSON digestをbindする
- 本Issueではseedを予約しない

### 5.6 Digestとraw / summaryのprovenance

| digest | 対象 |
| --- | --- |
| `rules_digest` | §5.2のrules document（全field値）のcanonical JSON SHA-256 |
| `protocol_digest` | protocol document（ID、version、配置表、主指標、classification、rules digest、engine revision、game mode、終局条件）のcanonical JSON SHA-256 |
| `schedule_digest` | ordered seeds × placementのschedule |
| `lock_digest` | lock全体。rawとresultのmanifestが参照する |

- canonical JSONは既存artifactと同じ規則（key sort、enumは `.value`、集合はsortしたlist、
  tupleはlist、`None` は `null`）で作る。**digestの値は実装Issueで算出して本書へ追記する**
  （本Issueでは算出しない）
- raw（半荘record、局record、第2層record）のmanifestは、schema identity、lock digest、
  Arena producer revision、実行環境（Python、lisjong、lisjong-engine、native）、
  payload fileごとのSHA-256・行数を持つ
- summary（result、副指標）は、入力rawのmanifest digestと、集計codeのArena revisionを持つ。
  `verify` はrecorded statisticsを信用せず、rawからの再導出との完全一致だけを成功とする
- credential、Authorization情報、Policy内部の推定値、非公開情報（他家の手牌等）をどの
  artifactにも含めない

## 6. RiichiEnv 0.4.10との差分と未確認項目

### 6.1 前提

- 下表の「source確認」は、project `docs/engine-evaluation-transition.md` が参照した
  RiichiEnv v0.4.10 `_advance_round` / `_process_end_game`、engine `rules.py` /
  `match_state.py` / `final_score.py`、Arena旧protocolの読解による。**対局・fixtureによる
  適合確認ではない**
- RiichiEnvとengineは乱数系が別で、同じseedから同じ牌山は得られない。新旧protocolの結果を
  seed単位で対応付けない
- 差分の確認は新protocolの正しさのためであり、旧結果の再判定には使わない

### 6.2 差分表

| # | 項目 | 旧（RiichiEnv 0.4.10 / Arena v1） | 新（engine `project-standard-v1`） | 状態 | 扱い |
| ---: | --- | --- | --- | --- | --- |
| 1 | 持点・返し・順位点 | 25000開始、30000返し、uma 30/10/-10/-30、oka首位+20 | 同じ値 | source確認 | 全RuleSet値を保存。名前で一致としない |
| 2 | 主指標の式 | `(素点 - 30000) / 1000 + uma + oka`（Arenaが計算） | engine `final_points` | 確定 | 別endpoint。旧式を重ねない |
| 3 | 端数処理 | なし（100点刻みを0.1 ptとして保持） | 非首位は `(素点 - 30000)` を1000点単位で0方向へ丸め、残差を首位へ | source確認 | §7 F1〜F3 |
| 4 | 得点の単位 | 1 = 1素点（`FINAL_SCORE_UNITS_PER_POINT = 1000`） | 1 = 0.1 pt | 確定 | 表示は `/ 10`。旧値と桁を取り違えない |
| 5 | 飛び終了 | 負点で終了 | 0点未満で終了（0点は続行） | source確認 | F6 |
| 6 | 飛び賞罰 | 主指標に含まない | 賞 +10 pt / 罰 -10 pt（ゼロサム）。受取人は飛んだ席が実際に支払ったtransferから決まる | source確認 | F7〜F9。複数受取人・複数飛びの配分は**未確認** |
| 7 | 南4以降の親終了 | 連荘・首位・30000以上で終了。途中流局は除外 | 親和了・親聴牌終了が有効。途中流局は親継続だが終了理由にはならない | source確認 | F10〜F12。同点首位を含む |
| 8 | 西4局 | 親流れなら終了。親連荘は終了条件次第で継続し得る | 必ず終了 | source確認（明確な差） | F13 |
| 9 | 終了時の残供託 | 首位に加算、同点は若いseat優先 | `final_riichi_stick_awards` として最終1位席へ加算（`seat_order` では1席） | source確認 | F4〜F5。局精算と二重計上しない |
| 10 | 同点順位 | 終局時の首位選定は若いseat優先。全順位の同点処理は `LocalGameResult` 経路に依存 | `seat_order`（東1局の席順）で一意に分解 | 旧側の全順位は**未確認** | F14 |
| 11 | ロンの成立人数 | 上流 `GameRule` と生成経路に依存 | 複数ロン成立（頭ハネなし）。本場・供託は放銃者に最も近い和了者 | 旧側**未確認** | B11 |
| 12 | 三家和 | `GameRule` のPython constructorと `default_tenhou` で既定が異なる | 途中流局 | 旧側**未確認**（実際の生成経路を追う必要） | B12 |
| 13 | 途中流局 | 上流依存 | 九種九牌・四風連打・四槓・四家立直すべて有効 | 旧側**未確認** | B13 |
| 14 | 流し満貫 | 上流依存 | 有効（荒牌流局の一部として精算） | 旧側**未確認** | B14 |
| 15 | 槓ドラ | 上流依存 | 大明槓・加槓は公開を遅延、暗槓は即時 | 旧側**未確認** | B5〜B7 |
| 16 | リーチ後の暗槓 | 上流依存 | 待ちと面子構成を変えない場合のみ可 | 旧側**未確認** | B9 |
| 17 | 立直の条件 | 上流依存 | 1000点以上、山の残り4枚以上 | 旧側**未確認** | B8 |
| 18 | 役・符の設定 | 上流依存 | 切り上げ満貫なし、数え役満あり、複合役満あり、ダブル役満なし、連風牌雀頭4符、国士の暗槓槍槓なし、パオは大三元・大四喜（全額） | 旧側**未確認** | 差分の列挙のみ。役判定そのものはengine所有 |
| 19 | 赤牌・喰いタン等 | `4p-red-half` | `RuleSet` にfieldがない | 新旧とも**未確認** | §6.3 |
| 20 | 全員聴牌流局の精算 | `ryukyoku.deltas` が立直供託を重複して含む（#364、upstream #247） | `CompletedRound.settlement` が正本 | source確認 | 新protocolは影響を受けない。補正しない |
| 21 | step上限 | `max_steps = 10,000` | なし | 確定 | §4.2 |
| 22 | 起家 | 旧runnerの席割りに依存 | seat 0固定 | 旧側**未確認** | 配置表はengineのseat基準で定義済み |

### 6.3 未確認項目のfixture

新protocol側で、実装Issueが固定入力のfixtureとして確認する項目。期待値はengineの `docs/rules.md`
と手計算から先に書き、実行結果に合わせて期待値を作らない。

| ID | 確認内容 | 固定する入力 | 確認する値 |
| --- | --- | --- | --- |
| U1 | 牌山が配置に依存しない | 同じseed、6配置 | 各round ordinalの `random_provenance` と配牌が一致 |
| U2 | 赤牌の構成 | 配牌前の牌山 | 赤5の種類と枚数 |
| U3 | 喰いタン・後付け | 鳴いた断么九手、後付けの役牌手 | 和了の合法手が出るか、役の内訳 |
| U4 | callbackの有無で進行が変わらない | 同じseed・Policy、callbackあり / なし | `CompletedMatch` の完全一致 |
| U5 | `HONBA` が別transferになる | 本場ありのロン・ツモ | `SettlementTransfer.reason` の内訳（§10.5の前提） |
| U6 | 同じseed・Policyの再現性 | 同一入力を2回、別process | `CompletedMatch` の完全一致 |

旧RiichiEnv側の未確認（#10〜#19、#22）は、新protocolの成立には不要である。新旧の結果を
並べて説明する文書を書くときにだけ、該当項目を確認する。

## 7. 最終得点の検証

`final_points` は内部単位（1 = 0.1 pt）で、`base_points + uma_points + oka_points +
bankruptcy_points` に等しい。次の2段で検証する。

### 7.1 Record単位の整合検査（全半荘、fail closed）

engineの値を正本とし、不一致は補正せず `STOP / INVALID` とする。Arenaは役・点数計算や
状態遷移を再実装しない。ここで行うのは、lockしたRuleSet値を入力とする算術の照合だけである。

| # | 検査 |
| ---: | --- |
| C1 | 4席の `final_points` の合計が0 |
| C2 | 各席で `final_points == base_points + uma_points + oka_points + bankruptcy_points` |
| C3 | `final_raw_scores == 最終局の scores_after_settlement + final_riichi_stick_awards`。awardの合計は `1000 × 最終局の riichi_sticks_after` |
| C4 | `sum(final_raw_scores) == 4 × starting_points`（残供託が全額配られている） |
| C5 | `FinalPlayerScore.score == final_raw_scores[seat]` |
| C6 | rankが `(-score, seat順)` の順序と一致し、1〜4が一意 |
| C7 | `uma_points == 10 × uma[rank - 1]`、`oka_points` は1位が `10 × 20`、他は0 |
| C8 | 非首位の `base_points == 10 × trunc((score - 30000) / 1000)`（0方向）、首位は `10 × (-20 - Σ非首位のpt値)` |
| C9 | `bankruptcy_points` の合計が0。`end_reason != bankruptcy` なら全席0。罰を受ける席は最終局精算後に0点未満の席だけ |
| C10 | pt表示は `final_points / 10`。保存値は整数のまま |

### 7.2 Fixture（実装Issueで作成、期待値は手計算）

| ID | 局面 | 確認する値 |
| --- | --- | --- |
| F1 | 4席とも1000点の倍数 | 端数なし。`base_points` が素点式と一致 |
| F2 | 非首位に100点単位の端数（正側・負側） | 0方向への丸めと首位への残差 |
| F3 | 端数の残差で首位の `base_points` が素点式とずれる例 | 旧v1式との差を数値で示す |
| F4 | 終局時に残供託あり、首位1席 | awardの宛先、`final_raw_scores`、局精算との非重複 |
| F5 | 終局時に残供託あり、首位が同点 | `seat_order` で若いseatへ全額 |
| F6 | ちょうど0点の席、-100点の席 | 0点は続行、0点未満で `bankruptcy` |
| F7 | ロンで1席が飛ぶ | 賞罰 ±100（内部単位）、受取人 = 和了者 |
| F8 | ツモで1席が飛ぶ / 複数席が同時に飛ぶ | 受取人と配分（**未確認**、期待値はengine `docs/rules.md` から先に定める） |
| F9 | ダブロンで1席が飛ぶ | 複数受取人への配分（**未確認**） |
| F10 | 南4局、親和了で親が首位・30000以上 | `dealer_win` |
| F11 | 南4局、荒牌流局で親聴牌・首位・30000以上 | `dealer_tenpai` |
| F12 | 南4局、途中流局 / 親が同点首位（親が若いseatでない） | 続行 |
| F13 | 西4局、親連荘の結果 | `final_round` で必ず終了 |
| F14 | 2席同点・3席同点の最終順位 | `seat_order` による順位と順位点 |
| F15 | 南4局で親流れ、全員30000点未満 | 西入 |

## 8. 現Champion系列のbridge fixtureと責任範囲

### 8.1 責任範囲

```text
engine SeatObservation / ActionDescriptor
    -> Arena bridge: PolicyInput projection、descriptor ↔ InternalAction mapping
    -> lisjong: DecisionContext contract、Policy選択、execute_policy() validation
    -> Arena bridge: 選択されたInternalActionを元のdescriptorへresolve
    -> engine: 適用、状態遷移、精算、終局
```

| 段階 | owner | fixtureで確認すること |
| --- | --- | --- |
| 局面と合法手の生成 | lisjong-engine | Arenaは再実装しない。提示されたdescriptor集合を入力として固定する |
| `SeatObservation` → `PolicyInput` | Arena bridge（projection先contractはlisjong所有） | 自席の手牌・副露・河・ドラ・点数・局位置・赤牌が、viewer seatに見える情報だけで正しく入る |
| descriptor → `InternalAction` 候補 | Arena bridge | 全descriptorが1対1で変換され、順序を保つ。重複・未対応はfail closed |
| 行動の選択 | lisjong（現Champion系列Policy） | Policyが合法候補の中から選び、`execute_policy()` を通る。**選択の内容の良し悪しは検証対象外** |
| `InternalAction` → descriptor | Arena bridge | 提示された元のdescriptor objectへ戻る。他seat・他decisionの行動はresolveしない |
| 適用と精算 | lisjong-engine | 選んだ行動がengineに受理され、`CompletedRound` の精算と整合する |

Arenaはfallback、行動の自動置き換え、retry、独自の合法手validationを実装しない
（`policy_selector.py` の現行契約）。

### 8.2 既存coverageと不足

| 既存test | 範囲 | 不足 |
| --- | --- | --- |
| `test_lisjong_engine_action_mapping.py` | 合成 `SeatObservation` に対する各descriptorの変換、加槓の元ポン解決（赤牌を含む）、fail closed | 実engineが生成した局面ではない。Policyを通していない |
| `test_lisjong_engine_policy_selector.py` | selector境界、立直の2段階decision、例外の伝播 | 現Champion系列Policyではない |
| `test_lisjong_engine_hanchan_integration.py` | `MinimalPolicy` 等で半荘完走、再現性 | 鳴き・槓・立直をほとんど選ばないため、**Champion対応済みの根拠にしない** |

#366 / #367 / #385の400半荘実行は、engine `96b9796` と当時のPolicy構成での実績であり、
現Champion系列・engine `91af75e` の検証には当たらない。

### 8.3 行動別fixture

対象Policy: 現Champion系列（`PlacementAwareSpeedCallPolicy`、catalog identity
`placement-aware-speed-call`）と、eventのcandidate。各fixtureは、実engineを固定seedから
進めて得た局面、またはengineのfixture builderで作った局面を使う。「選択」列は、Policyが
その行動を選ぶ局面を用意することを意味する（選ばせるためにPolicyを変更しない）。

| ID | 行動 | 固定する局面 | PolicyInput | 合法手 → 候補 | engine適用・精算 |
| --- | --- | --- | --- | --- | --- |
| B1 | chi | 上家の打牌に複数のチー形がある（赤5を含む形と含まない形） | 河の最終打牌と打牌者 | 形ごとに別候補。targetは上家だけ | 副露が公開され、次は自席の打牌 |
| B2 | pon | 役牌ポン、赤5を含むポン | 同上 | 赤あり / なしが別候補として区別される | 同上 |
| B3 | daiminkan | 暗刻持ちで他家が4枚目を打牌 | 同上 | pon / daiminkan / passが並ぶ | 嶺上ツモ、槓ドラの遅延公開 |
| B4 | ankan | 自摸で4枚そろう | 自席手牌と自摸牌 | ankan / 打牌が並ぶ | 槓ドラ即時公開、嶺上ツモ |
| B5 | kakan | ポン済みの牌を自摸（追加牌が赤 / 元ポンが赤の両方） | 自席の副露snapshot | 元ポンが一意に解決される | 槍槓の応答機会、成立後に槓ドラ |
| B6 | 槍槓 | 他家の加槓牌で和了できる | 加槓がtriggerの応答 | ron / pass | ロン精算、加槓は不成立 |
| B7 | 赤牌 | 赤5を含む手牌・打牌・副露・ドラ | 赤が牌単位で保持される | 赤5と通常5の打牌が別候補 | 赤ドラが打点へ反映される |
| B8 | reach | 門前聴牌、1000点以上、山4枚以上 | 立直可能の情報 | riichi → 宣言牌の2段階decision | 供託1000点、`riichi_contributions`。宣言牌ロンでは不成立 |
| B9 | リーチ後槓 | 立直後に暗槓できる自摸（待ち・構成を変えない形 / 変える形） | 立直中の状態 | 変えない形だけankanが出る | 槓後の精算、裏ドラ |
| B10 | ron | 単独ロン（子・親）、フリテンで出ない例 | 応答局面 | ron / pass | `WinResult.source_seat`、本場、供託 |
| B11 | tsumo | 門前ツモ、鳴いた手のツモ | 自摸局面 | tsumo / 打牌 | 3家の支払い、親かぶり |
| B12 | pass | 鳴ける・和了れる局面で見送る | 応答局面 | passが必ず候補にある | 見送り後の進行、同巡フリテン |
| B13 | 複数応答 | 同じ打牌に ron + pon、ron + ron（ダブロン）、ron × 3（三家和）、pon + chi | 各seatに自席分だけ見える | seatごとに独立した候補 | 優先順位の解決、ダブロンの本場・供託の宛先、三家和は途中流局 |
| B14 | 九種九牌 | 第1自摸で九種九牌 | 自摸局面 | kyuushu kyuuhai / 打牌 | 途中流局、親継続、本場+1 |
| B15 | 荒牌流局 | 聴牌0〜4人、流し満貫 | — | — | ノーテン罰符、`tenpai_seats`、`nagashi_mangan_seats`、親継続 |
| B16 | 途中流局 | 四風連打、四槓、四家立直 | — | — | 理由、親継続、供託の持ち越し |
| B17 | 終局 | §7.2 F6〜F15 | — | — | `end_reason`、最終得点 |

各fixtureで共通に確認すること:

- selector呼び出しとPolicy呼び出しが1対1で、Policyが受け取る `DecisionContext` が自席のものである
- Policyが返した行動が、提示されたdescriptorの1つへ戻る
- 例外・mapping失敗が握りつぶされない

B1〜B14について、現Champion系列がその行動を実際に選ぶ局面を用意できない場合（Policyが
選ばない行動である場合）は、「候補として正しく提示され、選ばなかった」ことを確認し、その旨を
fixture表へ記録する。未対応・説明できない差が1つでもあれば、正式評価を開始しない。

加えて、開発用seedでの短い半荘試行（現Champion系列を4席、および6配置）で、各行動種別の
発生件数を数え、0件の行動種別を報告する。件数は検証範囲の記録であり、強さの証拠ではない。

## 9. Python/Rust比較計画

正式実行はRust backend（native wheel）、照合基準はPython backendとする。backendはlisjongが
所有する実装の切り替えであり、Arenaは一致を確認するだけで、どちらかの結果を補正しない。

| 段階 | 入力 | 比較するもの | 合格条件 |
| --- | --- | --- | --- |
| P1 固定入力 | §8.3のfixtureと、開発用seedの半荘から採取した固定 `PolicyInput` + 合法候補 | 合法候補（engine生成なのでbackendに依存しない。両backendへ同じ入力を渡したことの確認）、選択された `InternalAction`、Policyが公開する補助出力 | 全件完全一致 |
| P2 短い試行 | 同じengine revision・RuleSet・seed・配置・Policy、backendだけを変える | 局数、各局の `CompletedRound`（結果、精算、transfer、供託、本場）、`end_reason`、`final_score`、decisionごとの選択行動の列 | 全件完全一致 |
| P3 実行環境 | 正式実行と同じimage / wheel | wheelのSHA-256、`SOURCE_REVISION`、`API_VERSION`、選択されたbackend名、worker間の一致 | lockの値と一致 |

- P1の「補助出力」は、lisjongが公開APIとして返す値に限る。対象の一覧は**未確認**で、
  lisjong側の公開範囲を確認して実装Issueで固定する。ArenaがPolicy内部の推定値を再計算・
  再定義することはしない
- P2は同じengine・同じ初期条件の中での比較である。RiichiEnvとengineの間で、同じseedから
  同じ牌山が出ることは仮定しない
- 入力はすべて開発用seed・fixtureとし、正式評価用seedを使わない。P2の規模（半荘数）は
  校正工程で決める（**判断待ち**）
- 浮動小数の許容誤差は置かない。差が出たら原因をlisjong側で調べるまで進めない
- 一致しない場合に「Pythonで正式実行する」へ切り替えるかどうかは、その時点のユーザー判断とする

## 10. `CompletedRound` の保存と再集計

### 10.1 方針

- 正本は、**同じ対局実行**が返す `CompletedMatch.history`（その半荘の全局の `CompletedRound`）
  である。後日の再生で局recordを作ることを標準手順にしない
- 局精算・本場・供託・収支は第1層recordとし、`history` だけから作る。driver callbackの追加を
  局精算取得の前提にしない
- `history` にない事実（副露、立直の宣言巡目・不成立）は第2層recordとし、既存driver callbackの
  player-safeな出力から作る
- recordはobjective execution outcomeだけを持つ。向聴数、受入、HandBelief、危険度・価値の
  推定、候補評価、選択理由などPolicy内部の分析を入れない

### 10.2 再利用する既存code

`focal_outcome_source/engine_source.py` から再利用する範囲を次のとおり固定する。

| 対象 | 扱い |
| --- | --- |
| `engine_game_facts()` の射影（`position_before`、`settlement.point_deltas`、`scores_after_settlement`、`riichi_sticks_after`、`points_before = after - deltas`） | 再利用する |
| `require_kyoku_facts()`（逆算と保存則） | 再利用する |
| `require_continuity()`（局間の点数・供託の連続） | 再利用する |
| `require_final_adjustment()`（最終素点との差を残供託配分だけで説明） | 再利用する |
| round identityの一意性、`next_position` の整合、`end_reason` の列挙 | 再利用する |
| `_end()` の結果要約（`kind` / `winner_seats` / `draw_kind`） | **不足**。放銃者、ツモ / ロン、聴牌席、transfer内訳がないため、本protocol用の射影を別に持つ |
| focal seat、exploration token、focal decision row、`ROLE_SPLITS`、L0.3のschema identity、`PINNED_LISJONG_ENGINE_REVISION = 96b9796…` | 再利用しない（#370 / #79固有） |

共通化の方法（import、小さな共有moduleへの抽出）は実装Issueで決める。実装を複製しない。
#370のsourceのschema・意味・固定revisionは変更しない。

### 10.3 第1層: 局精算record

schema identity（予定）: `arena-heuristic-candidate-engine-aabb-kyoku-record-v1`。1半荘1行の
JSON Linesとし、seatはengineのseat index 0..3で表す。

半荘単位:

| field | 内容 |
| --- | --- |
| `seed` / `placement` | block seedと配置番号0..5 |
| `seats` | seat 0..3のrole（A / B）と `policy_identity` |
| `end_reason` | `MatchEndReason.value` |
| `final_riichi_stick_awards` | 宛先seatと点数 |
| `final_raw_scores` | 4席 |
| `final_score` | 席ごとの `rank` / `score` / `base_points` / `uma_points` / `oka_points` / `bankruptcy_points` / `final_points`（内部単位） |
| `selector_call_count` / `policy_call_count` | seatごと。一致を検証する |
| `kyokus` | 下記の配列（`history` の順） |

局単位:

| field | 出所 |
| --- | --- |
| `kyoku_index` | `history` 内の0始まりの位置 |
| `round_ordinal` / `round_seed` | `random_provenance` |
| `prevailing_wind` / `hand_number` / `honba` / `dealer_seat` / `riichi_sticks_before` | `position_before` |
| `result.kind` | `win` / `exhaustive_draw` / `abortive_draw` |
| `result.win` | `method`（tsumo / ron）、`origin`、`winner_seats`（順序を保つ）、`source_seat`（ロンの放銃者、ツモは `null`）、`is_last_tile` |
| `result.exhaustive_draw` | `tenpai_seats`、`nagashi_mangan_seats` |
| `result.abortive_draw` | `reason` |
| `point_deltas` / `points_before` / `points_after` | `settlement.point_deltas`、逆算値、`scores_after_settlement` |
| `transfers` | `payer` / `recipient` / `amount` / `reason` / `winner_seat` の配列（`settlement.transfers` の順） |
| `riichi_contributions` | `settlement.riichi_contributions` の全field |
| `riichi_stick_awards` | 宛先seatと点数 |
| `riichi_sticks_after` | `settlement.riichi_sticks_after` |
| `dealer_continues` / `next_position` | 最終局の `next_position` は `null` |

和了者ごとの翻・符・役の内訳（`WinningScoreSelection`）は、v1では必須にしない。追加する場合は
schema versionを改める。牌山、手牌、和了牌、ドラ表示牌は保存しない。

局の識別子は `(seed, placement, kyoku_index)` で、event全体で一意である。半荘内では
`(prevailing_wind, hand_number, honba)` も一意で、`round_ordinal == kyoku_index + 1` を要求する。

### 10.4 整合検査

保存時と、保存物からの読み戻し時の両方で、同じ検査を行う。1件でも失敗すればfail closedし、
不整合な局・半荘を除外した集計はしない。

| # | 検査 |
| ---: | --- |
| K1 | 半荘が `6K` 件、`(seed, placement)` がlockのscheduleと過不足なく一致。重複なし |
| K2 | 各半荘の `seats` が配置表と一致し、identityがlockのparticipantと一致 |
| K3 | 局が1件以上。`kyoku_index` が0から連続。round identityが半荘内で一意 |
| K4 | `points_before + point_deltas == points_after`（席ごと） |
| K5 | `sum(point_deltas) + 1000 × (riichi_sticks_after - riichi_sticks_before) == 0` |
| K6 | 次局の `points_before` / `riichi_sticks_before` が前局の `points_after` / `riichi_sticks_after` と一致。第1局は全員25000点・供託0・東1局0本場 |
| K7 | `next_position` が次局の位置と一致。最終局だけ `null` |
| K8 | 席ごとに `point_deltas == Σ受取transfer - Σ支払transfer - 立直供託 + riichi_stick_awards` |
| K9 | `dealer_continues` と本場の推移が `next_position` と矛盾しない（engine値の相互照合。規則の再実装はしない） |
| K10 | 最終局と最終結果の整合（§7.1 C1〜C10） |
| K11 | `result.kind` ごとに必須fieldがそろい、他のkindのfieldがない。ロンは `source_seat` が和了者と異なる |
| K12 | selector呼び出し数とPolicy呼び出し数がseatごとに一致 |

「保存物のみからの再集計」: 主指標・classification・副指標は、lock、rawのmanifest、record
fileだけを入力に再計算でき、実行時のprocess状態・engineの再実行を必要としない。`verify` は
この再計算結果とrecorded値の完全一致を確認する。

### 10.5 副指標の定義

分母は、とくに断らない限り **参加seat局数**（role別。1局に同じroleが2席いれば2）である。

| 指標 | 分子 / 定義 |
| --- | --- |
| 和了率 | 自席が `winner_seats` に含まれるseat局数。ダブロンは和了者それぞれ1。流し満貫は和了に数えない |
| 放銃率 | `method == ron` かつ `source_seat == 自席` のseat局数。ダブロンでも1局は1（放銃回数は別に2と数える）。槍槓の `source_seat` はengine値のまま放銃に数える |
| 平均和了打点 | 和了1回あたりの手の点数 = `reason ∈ {ron, tsumo, pao_ron, pao_tsumo}` かつ `winner_seat == 自席` のtransfer受取合計 ÷ 和了回数。本場・供託を含まない |
| 平均和了収入 | 上記に `honba` のtransferと `riichi_stick_awards` を加えた合計 ÷ 和了回数（#432の `win_gain` に当たる） |
| 平均放銃失点 | 放銃1回あたりの手の点数 = 自席が `source_seat` の局で、自席が支払った `reason == ron` のtransfer合計 ÷ 放銃回数。本場を含まない |
| 平均放銃支払 | 上記に `honba` を加えた合計 ÷ 放銃回数（#432の `deal_in_loss` に当たる） |
| ツモられ支出 | 他家のツモ和了で支払った `tsumo` + `honba` |
| パオ支払 | `pao_ron` / `pao_tsumo` の支払。放銃・ツモられと分けて集計する |
| 流局収支 | `noten_penalty` と `nagashi_mangan` の受取 - 支払 |
| 立直供託 | `riichi_contributions` の合計（立直成立の回数も同じ出所） |
| 流局時聴牌率 | 荒牌流局で `tenpai_seats` に含まれるseat局 ÷ 荒牌流局のseat局 |
| 終局調整 | `final_riichi_stick_awards` の受取。局の収支とは別に集計 |
| 順位分布 | role別の1〜4着回数、平均順位（半荘単位） |

- 本場を手の点数と分けられるのは、`honba` が別transferとして記録される場合に限る。
  §6.3 U5で確認できるまで、「平均和了打点」「平均放銃失点」の
  本場を除く定義は**未確認**とし、分けられない場合は総点数移動をこれらの指標へ読み替えず、
  「平均和了収入」「平均放銃支払」だけを報告する
- 集計の重み付けは#432と同じ: seedごとにrole別の分子・分母を6半荘で合算して率を作り、
  `D(s) = A(s) - B(s)` をseed等重みで平均する。区間は `mean ± 1.96 × SD(K-1) / sqrt(K)`、
  N = block数。seed別・role別の分子と分母も保存する
- 条件付きの値（和了1回あたり等）は全seed合算の分子 / 分母で示し、分母0は `null` とする。
  欠損を0とみなさない
- 主指標の加法分解: 各seedで、素点の差を和了収入・放銃支払・ツモられ・パオ・流局収支・立直供託・
  終局調整へ分解し、合計が素点差に一致しなければfail closedする。`final_points` のうち
  端数処理・順位点・飛び賞罰の成分は、`base_points` / `uma_points` / `oka_points` /
  `bankruptcy_points` の差として別に示す
- 副指標の区間は多重比較を補正しない記述値である。局・seat-resultを独立標本として扱わない。
  副指標で主判定を救済しない

### 10.6 第2層: event fact

第1層にない次の事実だけを対象とする。

| fact | 用途 |
| --- | --- |
| 副露・槓（seat、種類 chi / pon / daiminkan / kakan / ankan、牌、鳴いた相手、局内の順序） | 副露率、副露別の和了率・放銃率、槓回数 |
| 立直宣言（seat、宣言時の自席の打牌数）、成立 / 不成立 | 立直巡目、立直別の和了率・放銃率。成立の有無は第1層の `riichi_contributions` と照合する |

- 出所は、engine `run_hanchan()` の既存callbackのうち **player-safe / publicな出力**に限る。
  第一候補は `on_round_evidence_complete`（局ごとに1回、viewerごとの `RoundEvidence`）で、
  `MeldCalledEvidence` / `KanDeclaredEvidence` / `KanConfirmedEvidence` /
  `RiichiDeclaredEvidence` / `RiichiEstablishedEvidence` / `RiichiFailedEvidence` を使う。
  どのcallbackを使うかの最終確定は実装Issueで行う（**未確認**: 4 viewerの公開factが一致する
  ことをfixtureで確認する）
- `on_transaction_observation`（全席の手牌を含むprivileged observation）は、正式runnerでは
  使わない。observer用のcallbackが受け取った値をPolicyの入力へ流さない
- callbackを付けても対局の進行が変わらないことを§6.3 U4で確認する
- 局への対応付けは `(prevailing_wind, hand_number, honba)` と局の発生順で行い、0件または
  複数件に対応する場合はfail closedする
- 役牌ポン等、役の意味付けが要る派生指標はv1の第2層に含めない（#432の `yakuhai_pon_count` は
  引き継がない）。必要なら定義を決めてschema versionを改める

第2層が不正な場合の扱い:

- 実行前確認（§10.7）では、第2層も含めて全項目がそろうことを開始条件とする
- 正式実行で第2層の保存・検査だけが失敗した場合は、第2層に依存する副指標を一切出さない
  （部分出力しない）。第1層と主指標の有効性は、第1層の検査結果だけで判断する。この場合も、
  結果を見たあとの再実行・seed追加で第2層を補わない

### 10.7 実行前確認

AGENTS.md「対局評価の実行前確認」と
[`heuristic-candidate-aabb-half.md`](heuristic-candidate-aabb-half.md) の
「新規評価の局単位記録チェック」を、新protocolのrunner・revision・backendで満たす。

- 既知の開発用入力（正式seedではない）で、取得 → 保存 → 回収 → 検証 → 集計を1回通す
- 和了率・放銃率・平均和了打点・平均放銃失点を、保存物だけから集計できることを確認する
- 局recordの欠落・重複、participantの取り違えを意図的に作った入力が拒否されることを確認する
- raw・summary・schema・provenanceを回収対象に含め、AWSでは回収・検証の前に転送用の証跡を
  削除しない
- 確認結果をeventのowner Issueへ記録する。既存AWS PreflightのPASSだけで対応済みとしない

## 11. 校正工程への入力仕様

標本数（block数K）は、本書の仕様を満たす実装ができたあと、別の校正工程で決める。校正は
開発用seed（split `CALIBRATION`）で行い、正式populationと分ける。旧400半荘、旧SD（≈15、
#436実績）、#385の計画SD（8〜10 pt）と所要時間、RiichiEnvでの所要時間を流用しない。

校正工程が出力し、lockのK決定に使う項目:

| 区分 | 項目 | 単位・条件 |
| --- | --- | --- |
| 対象 | candidate / incumbentのidentity、lisjong revision、backend、wheel hash | 正式eventと同じ組。別の候補の値を流用しない |
| 環境 | instance type、vCPU、worker数、image、Python、engine revision | 正式実行の予定構成 |
| 時間 | 1半荘あたりのworker時間（平均・中央値・最大）、1 blockあたりの時間、局数 / 半荘 | 6配置を含む |
| メモリ | workerあたりのpeak RSS、全体のpeak | worker数の上限の根拠 |
| 記録 | raw recordの容量 / 半荘、保存・回収・検証・集計の所要時間 | 第1層・第2層を含む |
| 費用 | 単価（取得時点のPricing API）、1 blockあたりの費用、fail-safe window全体のworst-case | USD、見積りベース |
| 分散 | 新protocolのblock差 `D(s)` のSDと、その推定に使ったblock数・信頼区間 | 開発用seed。正式populationの分散推定と分ける |
| 効果量 | 狙う効果（pt / seat-result）と、その根拠 | 結果を見る前に固定 |
| 検出力 | 有意水準（両側5%）、検出力、近似方法、そこから出るK | 正規近似の前提を明記 |
| 予算 | 当月の承認済み総額、当月の既支出、残額、配分の見直しの要否 | 残額を満額と仮定しない |
| 上限 | runnerのtimeout、fail-safe、最小K | **判断待ち**の値を含む |

- 校正で得た `D(s)` の点推定を、候補の強さの証拠や正式結果の先取りとして使わない
- 校正結果を見てcandidateを変更した場合、その校正を変更後candidateの校正として流用しない
- K、効果量、検出力、費用上限、主指標、判定法は、正式実行の結果を見る前にlockへ固定する

## 12. 未確認・判断待ち・follow-up候補

### 12.1 判断待ち（ユーザー）

| # | 事項 | 決める時期 |
| ---: | --- | --- |
| J1 | block数Kと、その最小値 | 校正後、lock前 |
| J2 | runnerのtimeoutとfail-safeの値 | 校正後 |
| J3 | Python/Rust比較P2の規模（半荘数） | 実装Issue |
| J4 | 対C0のeventと対現Championのeventを、候補ごとに両方行うか | project #83 / #84のgovernance |
| J5 | Python/Rustが一致しない場合の正式backend | 不一致が出た時点 |

### 12.2 未確認（fixture・調査で確かめる）

| # | 事項 | 参照 |
| ---: | --- | --- |
| N1 | 飛び賞罰の、複数受取人・複数飛びでの配分 | §7.2 F8、F9 |
| N2 | engineの赤牌構成、喰いタン・後付け | §6.3 U2、U3 |
| N3 | `honba` が常に別transferとして記録されるか | U5 |
| N4 | 同じseedの牌山が6配置で一致すること、callbackの有無で進行が変わらないこと | U1、U4 |
| N5 | 第2層に使うcallbackの確定と、4 viewerの公開factの一致 | §10.6 |
| N6 | Python/Rust比較で照合する「補助出力」の一覧 | §9 |
| N7 | 固定C0を、より新しいlisjong revisionの候補と比べるときのC0の固定方法 | §5.4 |
| N8 | 現Champion系列が各行動種別を実際に選ぶ局面を用意できるか | §8.3 |
| N9 | 小さいKでの正規近似の妥当性 | §3.3 |
| N10 | 旧RiichiEnv側の役・槓・複数ロン・同点順位・起家の実挙動 | §6.2 #10〜#19、#22。新protocolの成立には不要 |
| N11 | `rules_digest` / `protocol_digest` の値 | §5.6 |

### 12.3 Follow-up Issue候補

1. **protocol / lock / result実装**: §2〜§5の
   protocol定数、lock、6配置のschedule executor、統計、`verify`。generic backend、
   `EvaluationBackend`、registryは導入しない
2. **局単位record実装**: §10の第1層・第2層、整合検査、
   保存物からの再集計、`engine_source.py` の検査の共有方法
3. **最終得点・RuleSet fixture**: §6.3、
   §7.2
4. **Champion系列bridge fixture**: §8.3
5. **Python/Rust比較**: §9。lisjong側の補助出力の公開範囲の確認を含む
6. **校正**: §11。課金を伴うため、見積りと承認を先に行う
7. **AWS実行入口とseed予約**: 上記がすべて完了し、親#83のgateを通過したあと
8. **学習artifactを使うparticipantの受理**: 必要になった時点で、protocol versionを改めて定める

### 12.4 project側で別途更新が必要な記述

本Issueではproject repositoryを変更していない。

- `docs/engine-evaluation-transition.md` と#452本文の「Arena通常pinはengine `8735e89…`」は、
  現在のpin（`91af75e…`）と合っていない
- 同文書の検証範囲4「ID、rotation、…を#452で固定する」に対し、block構成が旧v1の4 rotationsでは
  なく6配置になったこと、block数が校正後に決まること
- #84の費用見積りの前提「1ブロック = 4半荘」は、新protocolでは「1 block = 6半荘」になる

## 13. Non-goals

- 実装、seed予約、AWS起動、対局実行、課金、校正の実施
- lisjong / lisjong-engineのpin・wheelの変更、Championの変更
- 旧結果（#375 / #423 / #436）の再判定、旧protocol・Overall protocolの変更
- 旧400半荘、旧SD、#385のfocal paired 8半荘 / seedの流用
- generic backend、`EvaluationBackend`、backend registry、generic match runtimeの導入
- RiichiEnv #364の補正
