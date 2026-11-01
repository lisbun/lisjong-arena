# pon-first-pass-branch-v1：P1 pilot（#553の実装）

設計: [lisjong #362](https://github.com/lisbun/lisjong/issues/362)（[設計案](https://github.com/lisbun/lisjong/issues/362#issuecomment-6545854838)・
[改訂](https://github.com/lisbun/lisjong/issues/362#issuecomment-6546073400)・[完了記録](https://github.com/lisbun/lisjong/issues/362#issuecomment-6546102219)、DESIGN-READY）。
D arm wrapper・分岐点の共有判定: [lisjong #363](https://github.com/lisbun/lisjong/issues/363)（`lisjong.policies.pon_first_pass_branch`、`pon-first-pass-branch-wrapper-v1`）。
**実行は後続の実行Issueで行う**（本Issueでは対局・rehearsal・seed予約・課金をしていない。Rust-only gateも実行Issueの最初の段階）。
DEVELOPMENTのpilotで、強さの結果ではない。

| module | 内容 |
| --- | --- |
| `pon_first_pass_worker` | R観測（候補の検出・成立 / ロン取消）とD arm（wrapper・Passの適用・分岐後の鳴き） |
| `pon_first_pass_plan` | write-once plan（固定24 seeds・上限・開始条件・判定の固定値） |
| `pon_first_pass_allocation` | 本学習の配分判定（SUPPORT）とブロック開始条件 |
| `pon_first_pass` | 照合・局収支・分散・配分判定・2 seed再実行のstrict verify・rehearsal照合・見積もり |

CLI: `python -m lisjong_arena.heuristic_candidate_engine_aabb pon-first-{plan,run,verify,rehearsal-compare,estimate}`。

## 1. 対象と比較

- **候補** = 各席・各局で、その席が門前（Chi / Pon / 大明槓 / 加槓なし。暗槓は門前）のときにChampionがPonを選んだ最初の判断。判定はlisjongの`is_pon_branch_candidate`を使い、Arenaで門前の判定を再実装しない
- 候補のPonの解決は、局の終了時に分岐席のplayer-safe evidenceから読む（#534の`resolve_call`）
  - `ESTABLISHED` → **分岐**（D armを実行する）
  - `CANCELLED-RON` → 分岐にしない。件数と位置を分母とともに記録する（INVALIDではない）
  - それ以外（優先鳴き・理由なしの消失）は起こりえない（ポン同士の競合は牌の枚数上ない）のでINVALID
- D arm: 半荘の先頭から再実行し、分岐席だけwrapperへ替える。wrapperは対象席にだけ装着し、未到達の判定（`PENDING` / `MISSED`）はArenaがINVALIDにする
- Δ = G_D − G_R（分岐席の対象局の局収支、100点単位）。精算の定義はR1（#544）と同じ

## 2. 記録とfield分離（情報境界）

1行 = 1候補（`branches.jsonl`）。

| field | 内容 | 用途 |
| --- | --- | --- |
| `decision_input` | `context`（判断時点のpublic `DecisionContext`）、`context_sha256`、`pon`、`call_analysis`（lisjong #350、`call-value-shadow-v1`）、`call_analysis_error` | 将来の特徴入力の元 |
| `selection` | `called_discard_order`、`resolution` | 標本選択と照合だけ。**特徴にしない** |
| `d`（成立した候補だけ） | 分岐点の行動（Pass）、`pass_applied`、`calls_after_branch`、`wrapper_calls_after_branch`、digest、精算 | 照合・結果 |

- 2つのfield集合は固定で、strict readerが混在（例: `decision_input`へ解決結果を入れる）を拒否する
- 同じwindowの他席の選択・windowの解決結果・分岐後の情報は`decision_input`に入れない
- `wrapper_calls_after_branch`は分岐後にinnerが鳴きを**選んだ**回数（ロンで取り消された鳴きを含みうる）。**成立**は`calls_after_branch[].resolution`（evidence）で別に記録し、選択回数は行動列から数えた数と一致することを照合する
- `pass_applied`: 対象局のevidenceで、Passした打牌に分岐席の副露がないこと

## 3. INVALID

R: contextの保存失敗・鳴き解析の拒否・解決の不整合（優先鳴き・消失）・局の不一致・取消なのに他家のロンがない。
D: wrapperが`branched`でない、分岐点の位置・prefix digest・contextがRと違う、分岐点の行動がPassでない、
`pass_applied`がfalse、分岐後の鳴きの順序・解決が不正または選択回数がwrapperと違う、別の局を精算した、
rehearsalのD arm 2回実行の不一致。1件でもあれば判定はSTOP-INVALID。

## 4. 判定（実行前に固定）

| 順 | 判定 | 条件 |
| --- | --- | --- |
| 1 | STOP-INVALID | INVALIDが1件以上（rehearsal照合不一致・2 seed再実行不一致はverify / compareで停止） |
| 2 | STOP-INCOMPLETE | 未完了（stop記録だけ。完了分で正式な集計・判定をしない） |
| 3 | NOT-VIABLE-SUPPORT | 下の配分でT*·r̂ < 500、有効な配分がない、または成立分岐が0件 |
| 4 | DESIGN-POSSIBLE | それ以外。品質・データ量・費用から本学習を設計できるという判断に限る（学習開始の承認ではない） |

配分（`pon_first_pass_allocation`）: c = R̄ + r̂·D̄、n_blk = ⌊(9,900 − 2c) / c⌋、
T(m) = min(m·n_blk, ⌊(21,600 − m(2c + 900)) / c⌋)。mは正の整数で、n_blk ≥ 2かつ各ブロックのseed数 ≥ 2の配分だけを有効とし、
T*はその最大（最大を与える最小のm、ブロックのseed数の差は1以内）。500は将来のTESTで確保できる分岐数の見込みの下限（資源配分の基準）で、
統計的な十分条件ではない。成立分岐0件ではD̄・分散を計算できないので未定義値（null）として記録する。

- **平均差の規模計算**（K = ⌈((z_0.95 + z_0.8)·σ_u(P90) / 5)²⌉）は記録だけで、停止条件ではない。**E2の検出力ではなく、未取得**
- ブロック開始条件（`block_start_condition`）: 本学習の各ブロックは配分で固定したseed数のまま、開始時の最新見積もりで
  (n + 2)c + 900 ≤ min(3時間, 4時間 − sessionの経過)を再確認し、満たさなければ開始せず停止する（結果を見て配分を縮めない）。
  本学習のsource protocolで使う
- 生成費用の削減（記録行動の再生計測）は、NOT-VIABLE-SUPPORTで分岐前の再実行がD armの費用の70%以上を占める場合だけ提案する（自動では着手しない）

### 解釈の注意

- 学習・評価の母集団は**成立分岐**であり、門前でPonを選んだ全判断ではない。将来のPolicyは取消を判断時に知れないので全対象判断に適用され、
  取消された判断の価値差は0。全対象判断あたりの効果は (1 − 取消率) × 成立分岐あたりの効果で、成立分岐だけの平均を全対象判断あたりの効果として扱わない
- 取消を除いた学習の目標はE[Δ | s, 取消なし]。真の条件付き期待値については、P(取消なし | s) > 0 のとき
  E[Δ | s] = P(取消なし | s)·E[Δ | s, 取消なし] で符号が一致する。**これは学習モデルの予測が正しいことを保証しない**
- 平均Δ・分布・局の結果・鳴き解析のroute・他家リーチの内訳・分岐後の鳴きは記述で、判定・対象選択・特徴選択に使わない

## 5. plan・上限

| 項目 | 値 |
| --- | --- |
| allocated | 553000..553023（24 seeds）× 1半荘、Champion×4、worker 1、split DEVELOPMENT、population `heuristic-candidate-engine-aabb-pon-first-pass-pilot-v1`。verifyで再実行するのは553000・553023 |
| rehearsal | 472005・472006（registry外）。観測あり / なしを1回ずつ、観測ありのD armを別processで2回 |
| 上限 | 1半荘 / 1 arm 600秒、process tree 4 GiB、本実行＋verify＋回収3時間、全工程4時間。開始条件・上限はmax(monotonic, realtime) |
| 見積もり | rehearsalから。分岐率はmax(rehearsalの率, 155/96 seat-hanchan（#536のChampionのPon全体）)で、時間を過小に見積もらない |
| 記述用の内訳 | lisjongの鳴き解析のroute（`PARENT_DECISION` = 役牌Ponの親経路 / `SPEED_CALL`）と他家リーチの有無。Arenaで役牌を再判定しない |
| bootstrap | `random.Random(553)`、10,000回、seed cluster |

owner_issueは後続の実行Issue（#554以降）。seed・判定・上限はprotocol invariantで、callerは変えられない。
