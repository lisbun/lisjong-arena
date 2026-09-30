# AABB半荘比較の局単位診断（#432）

Issue: [lisbun/lisjong-arena#432](https://github.com/lisbun/lisjong-arena/issues/432)

#423（`arena-heuristic-candidate-aabb-half-v1`、400半荘）を同じ条件で決定的に再生し、
局単位の客観的結果を記録して、A−Bの差の内訳を記述的に分析するための診断ツールである。

- 結果既知seedを使う**記述的DEVELOPMENT分析**であり、正式評価の延長・再判定・新しい標本ではない。
- 主指標・判定・Champion designation・seed ledger・allocationは変更しない。
- 正式runner（`comparison.run_comparison*`）、lock、comparison / result artifactのschemaと挙動は変更しない。
- 局単位記録の恒久的な仕組みは、今後のlisjong-engine版protocolで扱う（本ツールは#423分析専用）。

実装は`src/lisjong_arena/aabb_kyoku_diagnostic/`にある。

## 手順

```bash
export LISJONG_SHANTEN_BACKEND=rust   # #423と同じbackend

# 1. 再生（出力は新規fileのみ。正式証跡と別の場所へ）
python -m lisjong_arena.aabb_kyoku_diagnostic run \
  --policy-a placement-aware-speed-call-kobalab-0004-belief-paijia \
  --policy-b placement-aware-speed-call \
  --seeds 423100..423199 --workers "$WORKERS" --out "$OUT/records.jsonl"

# 2. 正式comparison artifactとの照合（1件でも差があればSTOP、exit 1）
python -m lisjong_arena.aabb_kyoku_diagnostic verify \
  --records "$OUT/records.jsonl" --comparison "$BUNDLE/comparison.json" \
  --out "$OUT/verification.json"

# 3. 集計（PASSした照合と同一の記録でなければ拒否）
python -m lisjong_arena.aabb_kyoku_diagnostic summarize \
  --records "$OUT/records.jsonl" --verification "$OUT/verification.json" \
  --out "$OUT/summary.json"
```

seat assignmentとPolicy生成は既存`comparison._seat_assignment` / `_create_policies`をそのまま使う
（rotation 0..3 = `[A,A,B,B]`の巡回、seat・gameごとにfresh instance）。1 gameは`LocalGameRunner`の
公開`trace_sink`でraw eventを受け取るだけで、Policyへ渡る情報と行動は変わらない。これは照合
（再生最終点・順位・seat identityと正式記録の全件一致）で確認する。

各記録は実行環境（Python、RiichiEnv / lisjong / Arenaのversion、`LISJONG_SHANTEN_BACKEND`、選択された
backend、nativeの`SOURCE_REVISION` / `API_VERSION`）を持ち、全workerで同一であることを要求する。
raw event列そのものは保存せず、件数とSHA-256だけを残す。

記録schemaは`arena-aabb-kyoku-diagnostic-record-v2`（1 game 1行のJSON Lines）で、seed、rotation、
game mode、`max_steps`、seat 0..3のPolicy identity、最終点・順位、局単位記録を持つ。

### 中断と再開

完了したgameは`<out>.partial`へ1行ずつ追記・fsyncする。中断後に同じ引数で`run`を再実行すると、
完了分を再利用して残りだけを再生する。

- 再利用する記録は、各rotationの期待seat配置（`[A,A,B,B]`の巡回）、game mode、`max_steps`、seedが
  完全一致しなければ拒否する（A/Bの入れ替えや別条件の途中結果を混ぜない）。重複gameも拒否する。
- `.partial`の改行で終わっていない最終行は、書きかけとして捨てて（fileを最後の完全な行まで
  truncateし）そのgameを再生し直す。途中の行の破損はfail closedする。
- 全件がそろうと`<out>.writing`へ(seed, rotation)順に書き、`<out>`へrenameしてから`.partial`を削除する。
  `.writing`が残っていれば捨てて`.partial`から書き直す。rename後・`.partial`削除前に止まった場合は、
  `<out>`と`.partial`の内容が一致するときだけ`.partial`を削除して完了とする（一致しなければ拒否）。

## 点数の正本と#364

局の点数は局境界を正本とする。

| 局 | 正本 |
| --- | --- |
| 非最終局 | 次局`start_kyoku`の`scores` / `kyotaku` |
| 最終局 | `env.scores()`から半荘終了時の供託配分を分離した値 |

`hora.deltas` / `ryukyoku.deltas`は点数移動の内訳として使い、正本と照合する。

- 局ごとの保存則：`sum(after) + 1000 × sticks_after == sum(before) + 1000 × sticks_before`
- 最終局：`final − after`が0（残存供託0本）か、「残存供託n本をちょうど1 seatへ+1000n」であること。
  このseatと点数を`final_award`として別に記録する。
- 各seatで、`win_gain − deal_in_loss − tsumo_loss + draw_transfer − riichi_deposit + final_award`が
  局開始点から局終了点（最終局は配分後）までの差に一致すること。

照合の不一致で許容するのは、[#364](https://github.com/lisbun/lisjong-arena/issues/364)
（upstream smly/RiichiEnv#247）の既知署名だけである。RiichiEnv 0.4.10は全員聴牌の通常荒牌流局で、
`reach_accepted`で既に記録した供託を`ryukyoku.deltas`にも重複して含む。実際の点数と供託の推移は
正しいため、主評価の最終点には影響しない。

- 署名：`reason == "exhaustive_draw"`、その局で`reach_accepted`があり、`deltas`が「その局で
  `reach_accepted`した各seatにちょうど−1000、それ以外0」であること。
- 署名に一致し、点数移動0とみなすと正本と一致し、かつ再構成した手牌が全員聴牌の場合に限り、
  `known_riichienv_364_signature = true`として記録する（`draw_transfer`は0）。
- それ以外の不一致、保存則違反、未知のevent構造はすべて`KyokuAccountingError`でfail closedする。
  `deltas`を書き換える補正はしない。

## 記録する事実

1局・1seatごとに次を記録する（Policy内部の分析は含めない）。

| field | 意味 |
| --- | --- |
| `won` / `win_count` / `tsumo_win` | 和了の有無・回数・ツモ和了 |
| `dealt_in` / `deal_in_count` | ロンで放銃したか（ダブロンは回数2） |
| `win_gain` | 自分の`hora.deltas[self]`合計（本場・供託を含む） |
| `deal_in_loss` | 放銃したロンの`−deltas[self]`合計（本場を含む） |
| `tsumo_loss` | 他家ツモ和了で支払った点数 |
| `draw_transfer` | 流局時の点数移動（ノーテン罰符等。#364署名の局では0） |
| `riichi_deposit` | `reach_accepted`で供託した点数 |
| `final_award` | 最終局のみ、半荘終了時に受け取った残存供託 |
| `riichi_declared` / `riichi_accepted` / `riichi_turn` | 立直宣言・成立、成立時点の自分の打牌数 |
| `open_call_count` | チー・ポン・大明槓の回数 |
| `yakuhai_pon_count` | 成立した役牌ポンの回数（下記） |
| `kan_count` | 大明槓・加槓・暗槓の回数 |
| `calls` | 副露・槓ごとの`kind`（chi / pon / daiminkan / kakan / ankan）、`pai`、`consumed`、`target`（加槓・暗槓は`null`）、`yakuhai`（event順） |
| `tenpai_at_exhaustive_draw` | 通常荒牌流局時の聴牌（下記。それ以外の局は`null`） |

局としては終了種別（`hora` / `exhaustive_draw` / `abortive_draw`）、途中流局の理由、本場、供託、
#364署名フラグ、聴牌再構成の不一致フラグを持つ。

### 流局時の聴牌

`ryukyoku`イベントに聴牌の項目はない。また複数局gameではRiichiEnvが同じstep内で次局へ進むため、
既存`RoundStatsCollector`のように`env.hands`から読むことはできない。

- 1〜3人聴牌：ノーテン罰符（聴牌者に`3000/t`、ノーテン者に`−3000/(4−t)`）を正本とする。
  この形でない移動はfail closedする。
- 罰符なし（全員聴牌か全員ノーテン）：`deltas`では区別できないため、`start_kyoku.tehais`と
  ツモ・打牌・副露eventから再構成した純手牌で判定する（lisjong `calculate_shanten() == 0`）。
  4人の判定が一致しなければfail closedする。
- 1〜3人聴牌の局で再構成結果が罰符と異なる場合は、正本（罰符）を採用し、
  `tenpai_reconstruction_mismatch`を立てて件数を報告する。

### 役牌ポン

測るのは**成立した役牌ポンの回数**だけで、ポン機会の有無や見送りは区別しない。

- 役牌：三元牌、その局の場風、ポンしたseatの自風（`(seat − oya) mod 4`）。
- 連風牌のポンは1回。ポン後の加槓は新たなポンとして数えない（`kan_count`へ）。
- 役牌の大明槓はポンに含めず`kan_count`へ。暗槓は副露に含めない。

## 集計（記述値のみ）

重み付けはIssueで固定した方式である。

1. seed `s`の4 rotations内で、arm別に分子と分母（参加seat局数）を合算し、率`A(s)` / `B(s)`を求める。
   1局に同armが2 seatいれば分母は2増える。
2. `D(s) = A(s) − B(s)`。
3. seedを等重みで平均し、`mean ± 1.96 × SD(N−1) / √N`（N = seed数）。seedごと・armごとの分子と分母も保存する。

paired対象（分母は全参加seat局）：和了率、放銃率、1局あたり和了収入・放銃支払・ツモられ支払、
立直率、副露率（1回以上）、役牌ポン回数、「通常荒牌流局かつ聴牌」の割合。

条件付き記述値（区間なし、全seed合算の分子/分母、分母0は`null`）：和了1回あたり収入、
放銃1回あたり支払、流局時聴牌率（聴牌局 / 通常荒牌流局、#61と同じ分母）、立直巡目の平均、
立直時の和了率、副露時の和了率・放銃率。

点数分解：主指標の`D(s)`を、素点成分（1半荘・1seatあたり`(final − 30000)/1000`の差）と
順位成分（ウマ・オカの差）へ分け、さらに素点成分を`win_gain` / `deal_in_loss` / `tsumo_loss` /
`draw_transfer` / `riichi_deposit` / `final_award`の差（1000点単位、符号付き）へ加法的に分解する。
各seedで合計が一致しなければfail closedする。

区間は多重比較を補正しない探索的な目安である。判定には使わない。

## 不正時の扱い

- 局単位記録の整合検査に失敗した場合、または照合でSTOPの場合は、副指標を出さない。
  部分出力や、不整合な局・半荘の除外はしない。
- #423の主評価の成否と判定には影響しない。
