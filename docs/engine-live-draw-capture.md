# engine経路の通常ツモopt-in capture（#596）

Issue: [lisbun/lisjong-arena#596](https://github.com/lisbun/lisjong-arena/issues/596)
（設計 [#594](https://github.com/lisbun/lisjong-arena/issues/594)、
parent [lisbun/lisjong-project#134](https://github.com/lisbun/lisjong-project/issues/134)）

> Status: **captureと保存・読込の実装だけ**。補正値の計算、価値関数、条件付き分布の
> 射影はlisjongの次段階で扱う。容量・時間・RSSの実測は、別に承認される
> fresh development pilotで行う。既存の評価経路と正式protocolは変えない。
>
> 射影・join・κの推定と凍結・置換不変性fixtureは#598で実装した（[`draw-correction.md`](draw-correction.md)）。

## 1. 目的と構成

DLVAT型の通常ツモ補正の入力として、lisjong-engine経路の半荘から
**通常ツモ（`DrawSource.LIVE_WALL`）ごとに、ツモ直前の盤面と実現牌**を、
同じ実行中にopt-inで保存する。

```text
capture_live_draw_hanchan(policies, seed, placement)
  └─ run_policy_hanchan(..., on_transaction_observation=capture, on_round_evidence_complete=capture)
       on_transaction_observation  privileged（全席の手牌）→ 行を作る
       on_round_evidence_complete  viewer別projectionの束（別経路）→ 完全性・局identityの照合
  └─ LiveDrawCapture.finish(CompletedMatch) → CapturedHanchan
write_live_draw_capture(destination, hanchans, plan=..., ...) → privileged artifact（計画と完全一致した場合だけ公開）
read_live_draw_capture(source) → LiveDrawArtifact（行をidentity / pre_draw / realizedに分けて返す）
```

| module | 役割 |
| --- | --- |
| `lisjong_engine/hanchan.py` | `run_policy_hanchan`の任意引数`on_transaction_observation`。省略時はengineへ渡さない（従来どおり） |
| `lisjong_engine/live_draw_capture.py` | 一方向のobserver `LiveDrawCapture`、行の作成と検査（`validate_row`）、`capture_live_draw_hanchan` |
| `lisjong_engine/live_draw_store.py` | write-onceの保存とstrict readback |

Arenaは手牌の畳み込みや状態遷移を実装しない。ツモ直前の盤面は、DRAW
transactionの**直前に受け取った観測**の最後のstepの`RoundCheckpoint`を
そのまま使う。事後checkpointから実現牌を取り除いて逆算しない。

## 2. engineの通知（`lisjong-engine` pin `91af75e`）

- 局開始時、driverは`ROUND_STARTED`を1 stepのtransactionとして渡す。配牌は
  各席13枚で、その後は`phase = AWAITING_DRAW`、`current_seat = 親`
- 通常ツモ・嶺上ツモは、それぞれ`DRAW` step 1つだけの独立したtransaction。
  eventは`TileDrawnEvent`ちょうど1つで、`source`が`LIVE_WALL` / `RINSHAN`を区別する
- `revision`は成功transactionごとに1ずつ増える（局開始が1）
- checkpointは山の残りの並び・未公開の表示牌・乱数状態を含まない

## 3. 行のschema（`arena-engine-live-draw-capture-row-v1`）

1通常ツモ1行のcanonical JSON。

| 部分 | field | V / pへ渡してよいか |
| --- | --- | --- |
| `identity` | `seed`、`placement`（呼び出し側が決める0以上の整数）、`round_ordinal`、`revision`（DRAW transaction）、`draw_index`（局内の通常ツモの番号、0始まり）、`drawer_seat` | **不可**（照合と結合のためだけ） |
| `pre_draw` | `revision`・`phase`・`current_seat`（直前の観測）、`checkpoint`（下記） | 許可されたfieldだけを射影して渡す（射影の定義はlisjong） |
| `realized` | `tile`（実現牌）、`source`（常に`live_wall`）、`post_draw_checkpoint_sha256`（事後盤面のcanonical JSONのSHA-256） | **不可**。反実仮想の候補 a とは別の引数として扱う |

`post_draw_checkpoint_sha256`は、将来同じrevisionで再実行したときの照合用である。
事後盤面そのものは保存しないので、読込時に事後盤面の意味（「ツモ直前 + 実現牌」との整合）を
再検証することはできない。その整合は**作成時の検査**（§4）で保証する。

`checkpoint`は`RoundCheckpoint`をversioned capture projectionとして射影したもの
（下記）。pending状態は保存せず、`riichi_finalizations`は必要なfieldだけに射影する。

- 牌は136枚ID（`Tile.id`、0..135）。赤五は標準牌集合の`copy_index == 0`で、
  `STANDARD_TILES[id]`から牌種と赤を復元できる
- 席は0..3（EAST..NORTH）、風・立直状態・フリテン理由はengine enumの`value`
- 全席: `hand_tiles`（engineの順序）、`drawn_tile` / `drawn_tile_source`
  （ツモ直前は常に`null`）、`discards`（`tile`、`is_tsumogiri`、`called_by`）、
  `melds`（`meld_type`、`tiles`、`called_tile`、`source_seat`。暗槓は後ろ2つが`null`）、
  `riichi_status`、`is_ippatsu`、`missed_ron_furiten`、`seat_wind`
- 局: `dealer_seat`、`prevailing_wind`、`live_tiles_remaining`、
  `rinshan_tiles_remaining`、`revealed_dora_indicators`、`riichi_finalizations`
  （`seat`、`outcome`、`contribution_points`）
- `pending_riichi_declaration` / `pending_kakan` / `pending_ankan`は、ツモ直前に
  空であることを作成時に検査し、保存しない

点数・本場・局開始時の供託は保存しない。局記録（`kyoku_record`の
`riichi_sticks_before`等）と`round_ordinal`で結ぶ。**ツモ時点の供託**は、
局開始時の供託に、`pre_draw.checkpoint.riichi_finalizations`のうち
`contribution_points`が`null`でないものの合計（1,000点単位で本数）を足して導く。
立直宣言の反応が未解決の状態ではツモが起きない（pending検査）ため、この値は
ツモ時点で成立している供託と一致する。どの時点の値をVの入力に使うかは
lisjong側で固定する。

## 4. 検査（fail closed）

### 観測列（作成時）

- 各局の最初の観測が`ROUND_STARTED`（単独のtransaction）で、`round_ordinal`は1から連続。
  `revision == 1`、`phase == AWAITING_DRAW`、`current_seat`が局開始checkpointの親、
  どの席も`drawn_tile`を持たない。途中から始まった観測・局をまたぐ観測を拒否する
- 局の終わりは`ROUND_ENDED`が最後のstepで、`phase == FINISHED`。`ROUND_ENDED`のない
  `FINISHED`も拒否する
- 同じ局の観測は`revision`が連続する（欠落・重複・順序違いを拒否）
- DRAW stepは単独のtransactionで、`TileDrawnEvent`ちょうど1つ。DRAW以外のstepに
  ツモeventがあれば拒否する
- 通常ツモの直前の観測は`phase == AWAITING_DRAW`かつ`current_seat == drawer`
- 事後盤面との整合: drawerの`hand_tiles` = ツモ直前の手牌 + 実現牌（多重集合）、
  `drawn_tile`が実現牌、`live_tiles_remaining`が1減る。ほかの席・河・副露・表示牌・
  立直の解決は変わらない。engineはツモ時に必ず同巡見逃しフリテンを解除するので、
  drawerの`missed_ron_furiten`は`temporary`なら`null`になり、それ以外の値は変わらない。
  `hand_tiles`はツモ牌を含むので二重に数えない
- 嶺上ツモは行を作らず、局ごとに件数だけを数える

### 行（作成時と読込時で共通の`validate_row`）

- 未知・欠落field、型、enum、席・牌IDの範囲
- `pre_draw.revision == identity.revision - 1`、phaseと席
- どの席も`drawn_tile`を持たない。`drawn_tile`と`drawn_tile_source`は両方`null`か
  両方値を持つ
- 物理tileの一意性: 手牌・副露・**鳴かれていない**河・公開表示牌の間で重複しない
- 鳴かれた河の牌（`called_by`）は、鳴いた席の副露の`called_tile` / `source_seat`と
  1対1で対応する
- 実現牌はツモ直前に見えていない
- 枚数の不変条件: `136 − |見えている牌| == live_tiles_remaining + 14 − len(revealed_dora_indicators)`。
  王牌は嶺上ツモのあとも通常の山の末尾から補充されて常に14枚
  （未公開の表示牌・裏表示牌・補充牌はすべて未見として数える）

赤牌を(tile_type, is_red)の別カテゴリとして未見牌を数えると、Σ n_a / N = 1 で、
実現牌は n_a ≥ 1 になる（testで固定）。

### 完全性（`finish`、別経路との照合）

captureの自己カウンタではなく、`on_round_evidence_complete`の`DrawEvidence`と照合する。
`RoundEvidenceCompletion`の各viewer projectionは**そのviewerに対してだけ**player-safeで、
4席分を束ねたbundle全体は単一playerにsafeではない（他家のツモ牌を集合として含む）。
captureはbundleを照合にだけ使い、保存しない。

- 局数 = `CompletedMatch.history`の局数 = evidenceの局数
- 局identity: `CompletedRound.random_provenance.round_ordinal`がcaptureの局と一致し、
  evidenceの場風・局番号・親・本場が`CompletedRound.position_before`と一致し、
  局開始checkpointの親・場風も`position_before`と一致する
- 局ごとに、通常ツモの席の列がevidenceと一致する（4 viewerのevidenceが互いに一致することも要求）
- drawer自身のviewerのevidenceが持つ牌（牌種と赤）が実現牌と一致する。evidenceは
  物理copyを識別しないので、copy ID（`Tile.id`）まではこの経路で照合できない
- 嶺上ツモの件数がevidenceと一致する

### artifact（読込時）

- 書き込み時: 実行前に決めた計画（`plan`、`(seed, placement)`の順序つき列）を
  capture結果とは独立に渡す。captureした半荘が計画と過不足・重複・順序違いなく
  一致しない場合は、何も公開しない（完全なseed blockだけを公開する）。1半荘のfixtureは
  1件だけの計画として扱う
- file集合が`manifest.json`と`live-draws.jsonl`だけ
- manifestの`plan`と半荘の列が一致する
- manifestのschema、`information_class == "privileged-offline-only"`、`access` /
  `retention` / `rules`が空でない、producerのrevisionが40桁
- payloadのbytes・SHA-256・行数、各行がcanonical JSON
- `(seed, placement)`の一意性、半荘・局ごとの件数、行の順序と`draw_index`の連番、
  局内の`revision`の増加

## 5. 情報境界（high-risk）

- observerは一方向。受け取った値は`LiveDrawCapture`の内部bufferにだけ保持し、
  Policy factory・selector・`PolicyInput`の生成・`on_delivery`へ渡さない。
  captureの有無で4席の`DecisionContext`・選んだ行動・`CompletedMatch`が一致することを
  testで固定している
- 既存の経路（`heuristic_candidate_engine_aabb/executor.py`、
  `riichi_dama_branch_worker.py`等）はこの引数を使わず、従来どおり
  privileged observationをengineへ要求しない
- observerの例外はengineの契約どおり実行全体を失敗させる。成功分だけの結果を作らない
- checkpoint単体は未来の情報を含まないが、行は`identity`（seed等）と`realized`
  （実現牌）を含むため、**artifact全体は未来を再構成できる情報を持つ**。
  そのため次を守る:
  - 正式result・局記録・`GameTrace`とは別のschema・保存先にする
  - manifestに`access`（誰が読めるか）と`retention`（いつまで保持するか）を必ず記録する
  - readerは行を`identity` / `pre_draw` / `realized`に分けて返す。V / pの入力へは
    `pre_draw`から射影したfieldだけを渡し、seed・identity・実現牌・事後盤面・
    他のrotation・終局結果・将来の行を渡さない
- 補正の計算・価値関数・射影の定義はlisjongが所有する。既存の正式結果をsidecarで
  再分類しない

### 保存先・access・retention（gate）

本実装はcaptureと保存・読込の関数だけを提供し、保存先の基盤（S3・ACL等）は
実装しない。writerは`access` / `retention`の記述を必須にするが、その内容の正しさは
検査できない。そのため利用を次のように区切る。

| 利用 | 条件 |
| --- | --- |
| fixture・unit test | 一時directoryだけに書き、test終了時に削除する。repositoryへcommitしない |
| それ以外（development pilot等） | **実行計画のIssueで、実行前に次を固定し、ユーザーの承認を得てからでなければ書かない**: (1) 正式artifact・局記録・既存の評価保存物と分離した保存root、(2) 読める主体の範囲（ACL）、(3) 保持期限と削除の責任者、(4) V / pの学習・評価でのTRAIN / 評価のseed block分離。固定した値をそのまま`access` / `retention`に記録する |

保存rootはrepositoryの外に置き、正式評価のartifact directoryと共有しない。

## 6. 範囲外

- 容量・時間・RSSの実測（未圧縮で1半荘0.3〜0.5 MBという#594の見積もりは仮定）。
  新規対局・seed予約・AWSを伴う実測は別承認
- RiichiEnv / Overall経路、再実行による方式A、嶺上・配牌・裏ドラの補正
- 正式評価protocolの変更
