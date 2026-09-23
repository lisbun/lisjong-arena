# L0.3 B focal outcome source

Issue: lisbun/lisjong-arena#359（parent lisbun/lisjong-project#79 step B、
counterpart lisbun/lisjong#193）。

Arenaが所有する新しいversioned source contract
`arena-offense-l0.3-focal-outcome-source-v1`のproducerである。Arenaは事実だけを
記録し、`target_q`の算出、training、paired evaluationはlisjong / 後続stepが扱う。
既存#342 player-safe source record（v1 / v2）、`RoundResult`、durable local game
recordのschemaと意味は変更しない。

実装は`src/lisjong_arena/focal_outcome_source/`にある。

## Pinned consumer

producerはlisjong `aed9c840bc120471e557fc0c8444965c0b81a9c3`
（`PINNED_LISJONG_REVISION`、`pyproject.toml`のpinと一致させる）の
`lisjong.learning.outcome_source`をstrict consumerとする。
`verify_focal_outcome_source()`はそのconsumerでstrict readしたうえで、次の
Arena-owned auditを再検証する。

- exploration tokenの再導出（lisjongはtokenを再計算しない）
- kyokuごとの保存則
- hanchan最終調整の差分
- `source_contract`とpopulation / allocation bindingのshape

## 対局構成（A4）

```text
focal seat       = source_game_ordinal % 4
                   FocalExplorationPolicy（generation-only adapter）
                     -> lisjong select_residual_exploration(decision, token)
other 3 seats    lisjong ConstantResidualRuntime().create_policy()（game / seatごとにfresh）
game_ordinal     source全体で0..N-1。SCIENTIFICのTRAIN / SELECT境界でもrestartしない
```

adapterはserving Policyではない。既存`LocalGameRunner`をそのまま使い、focal seatの
`DecisionContext`ごとにselectorをちょうど1回呼び、同じ呼び出しのselector結果
（token / survivor）をcaptureする。captureは`DecisionTrace` / `GameTrace`へ入れない。

## Exploration token（A3）

```text
token = lowercase hex SHA-256(UTF-8(
  {"focal_decision_ordinal":<int>,"focal_seat":<0..3>,"game_seed":<int>,
   "identity":"lisjong-arena-l0.3-focal-decision-token-sha256-v1"}))
```

key順はsorted、区切り文字は`,` / `:`、空白・改行なし、ASCII、整数は10進表記。
`focal_decision_ordinal`はhanchanごとに0から始まり、focal seatのPolicyへ実際に
渡された`DecisionContext`ごとに1増える（O0 guard、single-survivor、およびRiichiEnv /
Adapterが`DecisionContext`としてsurfaceしたforced tsumogiriを含む）。

## Score boundary（A1）

全kyokuで同じevent-accountingを使い、RiichiEnv 0.4.10のscore-transfer factを
GameTrace順に適用する。点数計算は再実装しない。

```text
points = start_kyoku.scores; sticks = start_kyoku.kyotaku
reach_accepted(actor)  points[actor] -= 1000; sticks += 1
hora.deltas            points += deltas; 最初のhoraで sticks = 0
ryukyoku.deltas        points += deltas
```

- 非最終kyoku: derived値が次の`start_kyoku.scores` / `kyotaku`と完全一致すること
  をintegration oracleにする
- 最終kyoku: `points_after_kyoku` / `riichi_sticks_after`はhanchan最終調整**前**の値。
  `hanchan_final_scores`（`env.scores()`）と`hanchan_final_riichi_sticks`は
  audit factとして別保存し、差分が「残存供託n本をexactly 1 seatへ+1000n」だけで
  説明できること、および`hanchan_final_riichi_sticks == 0`を検証する
- 保存則`sum(after) + 1000 * sticks_after == sum(before) + 1000 * sticks_before`
- eventの欠落・重複・順序矛盾、`RoundResult`との不一致はfail closed

既知のbackend不整合（#364）: RiichiEnv 0.4.10は、全員聴牌の流局（exhaustive draw）で、
`reach_accepted`で既に記録済みの供託1000点を`ryukyoku.deltas`にも再度含める
（1〜3人聴牌の流局や四家立直では含めない）。producerはこのeventを保存則違反として
rejectする（fail closed）。補正はせず、upstream smly/RiichiEnv#247で追跡している。
このため、このkyoku patternを含むpopulationはRiichiEnv修正または依存version更新まで
sourceを生成できない。regression testは`tests/test_focal_outcome_source.py`の
minimal synthetic caseで固定している。

## Wire layout

```text
<root>/
  manifest.json                   sealed canonical JSON
  game-NNN/kyokus.jsonl           全kyoku row
  game-NNN/focal-decisions.jsonl  focal seatの全decision row
```

field一覧はlisjong `outcome_source.py`のmodule docstringと一致する。
`source_contract`はArena-owned objectで、`arena_revision`、`dependencies`
（installed internal VCS revision。`lisjong`はpinと一致必須）、`backend`
（`riichienv`とversion）、`game_mode`（`4p-red-half`）、`python`を持つ。
`build_source_contract()`はclean checkoutと検証済み環境からだけ作成する。

## 生成

`generate_focal_outcome_source(destination, population_role=..., games=[(seed, split), ...],
allocation_bindings=..., source_contract=...)`はexecution前にpopulation / binding /
contractを検証し、staging directoryへ書いてstrict readback後にだけ`destination`へ
renameする。1 gameでも失敗した場合は何も公開しない。

このIssueではCALIBRATION / TRAIN / SELECTのscientific allocationを消費しない。
testはledgerのどのallocationとも重ならないtest専用seed（900000台）だけを使う。
calibration pilot（#79 C0）とscientific source生成（#79 C2）は後続Issueで扱う。
