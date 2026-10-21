# engine版AABB評価の局単位record（#513）

Issue: [lisbun/lisjong-arena#513](https://github.com/lisbun/lisjong-arena/issues/513)
（parent [lisbun/lisjong-project#83](https://github.com/lisbun/lisjong-project/issues/83)）
仕様: [`heuristic-candidate-engine-aabb-half.md`](heuristic-candidate-engine-aabb-half.md) §7、§10、§14

> Status: **record・検査・副指標再集計の実装のみ**。runner、protocol / lock / result、
> seed予約、AWS、対局実行、校正は行っていない（#514以降）。このrecordが作成・読込の
> 検査に通っても、正式評価のPASS・判定ではない。

`lisjong_arena.heuristic_candidate_engine_aabb` は、同じ対局実行が返す値から
局単位recordを作り、保存し、読み戻して検査し、保存物だけから副指標を再集計する。

```text
run_hanchan(...)                      ← runner（#514）
  CompletedMatch                      → 第1層 kyoku_record
  on_round_evidence_complete の出力    → 第2層 event_facts
  selector / Policy 呼び出し数（seat別）→ 第1層 K12
        ↓ HanchanCapture
write_kyoku_records()                 write-once directory + manifest
        ↓
read_kyoku_records()                  作成時と同じ検査
        ↓
verify_schedule()                     K1 / K2（期待値は#514が渡す）
summarize_secondary_metrics()         §10.5・§10.6の記述値
```

## 1. 構成

| module | 役割 |
| --- | --- |
| `kyoku_record` | 第1層。`CompletedMatch.history` → 1半荘1行のrow。§10.4 K3〜K12、§7.1 C1〜C10。K1 / K2の照合API |
| `event_facts` | 第2層。`RoundEvidenceCompletion` → 公開fact row。4 viewerの一致、局への対応、立直と供託の照合 |
| `record_store` | manifest、write-once I/O、strict readback、第2層の無効化 |
| `secondary_metrics` | 保存物だけからの副指標、素点の加法分解、最終得点成分 |
| `errors` | `KyokuRecordError`（第1層・manifest・artifact）、`EventFactError`（第2層）、`SecondaryMetricError` |

局境界の射影と保存則・連続性・最終調整の検査は、#370のsource（`focal_outcome_source/engine_source.py`）
から `lisjong_arena/lisjong_engine/completed_match_facts.py` へ抽出し、両方が使う。
#370のsourceはschema・意味・固定revision・error型（`FocalOutcomeSourceError`）を変えていない。
新しい経路は `lisjong.learning` をimportしない（testで固定）。

## 2. 保存形式

```text
<destination>/
  manifest.json          arena-heuristic-candidate-engine-aabb-record-manifest-v1
  kyoku-records.jsonl    arena-heuristic-candidate-engine-aabb-kyoku-record-v1（1半荘1行）
  event-facts.jsonl      arena-heuristic-candidate-engine-aabb-event-fact-v1（第2層がVALIDのときだけ）
```

- 各行はkey順固定・区切りなしのcanonical JSON。seatはengineのseat index 0..3
  （EAST / SOUTH / WEST / NORTH）。行の順序は渡された半荘の順序のまま
- 第1層のfieldは仕様§10.3のとおり。`final_score` はseat index順、`kyokus` は `history` 順
- 第2層のfactは `meld_called` / `kan_declared` / `kan_confirmed` / `riichi_declared` /
  `riichi_established` / `riichi_failed` の6種。牌は `{"tile_type": 0..33, "is_red": bool}`。
  `riichi_declared.self_discard_count` は宣言牌を含む自席の打牌数（公開の打牌evidenceから数え、
  打牌そのものは保存しない）
- 保存しない: 和了の翻・符・役内訳、手牌、牌山、和了牌、ドラ表示牌、`RoundEvidence` 全体、
  viewer自身のツモ牌、Policy analysis。`on_transaction_observation` は使わない
- manifest: `schema`、`protocol`、`rules`（`RuleSet.default` / `project-standard-v1` / 1）、
  `producer.arena_revision`（40桁）、`bindings`（`lock_digest`、`environment_digest`。64桁または `null`）、
  `hanchan_count`、各payloadの `schema` / `file` / `rows` / `bytes` / `sha256`、第2層の `status`
  （`VALID`、または `INVALID` と `reason`）
- destinationは新規に限る。同じparentのstagingへ書き、読み戻し検査に通った場合だけ公開する

## 3. 検査

作成時（`project_hanchan` / `project_event_facts`）と読込時（`read_kyoku_records`）は同じ関数で
rowを検査する。作成時もcanonical JSONを経由した値を検査する。

| 範囲 | 内容 |
| --- | --- |
| 形 | 未知・欠落field、不正型（`bool` を `int` として受けない、`float` の点数を受けない）、未知enum、未知schemaを拒否 |
| K2（record内） | roleが§2.2の配置表と一致。roleごとに `policy_identity` が1つで、AとBが異なる |
| K3〜K9、K11、K12 | 仕様§10.4のとおり。K3は `round_ordinal == kyoku_index + 1` とengineの `derive_round_seed(seed, ordinal)` も照合する。K9は `dealer_continues` と結果、`next_position` の局・本場の推移を照合する |
| K10 | §7.1 C1〜C10。lockする `RuleSet.default()` の値（配給原点25000、返し30000、uma 30/10/-10/-30、供託1000、飛び0点未満）を入力にした算術照合だけ |
| 一意性 | `(seed, placement)` がrecord内で重複しない |
| payload | bytes・SHA-256・行数・canonical形式・manifestの件数 |
| 第2層 | 局数の一致、`(prevailing_wind, hand_number, honba)` と発生順での一意な対応、親の一致、4 viewerの公開fact列の順序を含む一致、副露の形、`kan_confirmed` に先行する `kan_declared`、立直の宣言→成立 / 不成立の解決、成立seat = 第1層の `riichi_contributions` |

検査はengine値どうしの照合であり、役・点数計算や状態遷移を再実装しない。

### K1 / K2: `verify_schedule(rows, expected)`

`expected` は `ExpectedHanchan(seed, placement, seats)` の列で、後続（#514）がlockの
ordered seeds、§2.2の配置表、participant identityから作る。本関数はlockを読まない。
欠落・重複・余分・順序違い・role / identity違いをそれぞれ拒否する。`expected` 自体も
配置表と一致しなければ拒否する。

## 4. 第2層の失敗

| 場面 | `event_facts_required` | 第2層が失敗したとき |
| --- | --- | --- |
| 実行前確認（§10.7） | `True` | `EventFactError`。何も公開しない |
| 正式実行 | `False` | 第2層全体を `INVALID` にし、最初に失敗した半荘と理由を `reason` に残す。`event-facts.jsonl` を書かない。以後の第2層rowも作らない |
| 読込（正式） | `False` | 第2層の破損・欠落・不一致は `INVALID`（`read-time verification failed: ...`）。第1層は独立に判定する |

- 第2層の失敗には、callback出力の未取得（`round_evidence=None`）を含む。未取得を0件として扱わない
- 第1層・manifest・artifact全体の失敗は、どの場面でも `KyokuRecordError` で止める。
  第2層の失敗へ読み替えない
- 正式実行で第2層が `INVALID` でも、結果を見たあとの再実行・seed追加で第2層を補わない（§10.6）

## 5. 副指標（`summarize_secondary_metrics`）

入力は検査済みの `VerifiedKyokuRecords` だけである。recordは6配置のseed blockが
placement 0..5の順で連続していることを要求する（満たさなければ `SecondaryMetricError`）。

- 率（分母は参加seat局数、または半荘単位の指標ではseat-result数）: seedごとにrole別の分子・分母を
  合算して率を作り、`D(s) = A(s) - B(s)` をseed等重みで平均し、`mean ± 1.96 × SD(K-1) / sqrt(K)`。
  K = block数。K = 1では区間を `null` にする
- 条件付きの値（和了1回あたり等）: 全seed合算の分子 / 分母。分母0は `null`。seed別の分子・分母も出す
- 第1層: 和了率、放銃率（ダブロンでも1局1回）、平均和了打点・平均和了収入（和了回数あたり）、
  平均放銃失点・平均放銃支払（放銃回数あたり。ダブロンは2回）、ツモられ支出、パオ支払、流局収支、
  立直供託・立直成立率、流局時聴牌率、終局調整、平均順位、順位分布
- 第2層（`VALID` のときだけ）: 副露率、槓回数、副露あり局の和了率・放銃率、立直巡目（成立した立直の
  宣言時の自席打牌数）、立直局の和了率・放銃率。`INVALID` では `event_fact_metrics` 全体が `null`
- 素点の加法分解: seatごとに `和了収入 - 放銃支払 - ツモられ - パオ支払 + 流局収支 - 立直供託 + 終局調整
  == 最終素点 - 25000` を確認し、一致しなければ `SecondaryMetricError`。分類できないtransferも
  `SecondaryMetricError`。seed別・role別の各成分と、`base_points` / `uma_points` / `oka_points` /
  `bankruptcy_points` / `final_points` の合計・差を出す
- ロン局で放銃者以外が払う `honba` はパオ支払に入れる
- 出力は `descriptive_only: true` の記述値で、主指標・classificationを計算・変更しない

## 6. 後続（#514）が渡す値と残る実行前gate

| 値 | 内容 |
| --- | --- |
| `HanchanCapture.match` | 同じ実行の `CompletedMatch` |
| `round_evidence` | 同じ実行で `on_round_evidence_complete` が渡した値の列（取得していなければ `None`） |
| `selector_call_counts` / `policy_call_counts` | seat index順。runnerが数える（現在のbridgeは数を持たない） |
| `seed` / `placement` / `seats` | lockのseed、配置番号、§2.2どおりのrole と `policy_identity` |
| `producer_revision` | 実行したArenaのcommit（40桁） |
| `RecordBindings` | lockのdigest、環境provenanceのdigest。正式実行では両方を渡し、`require_bindings()` で照合する |
| `verify_schedule()` の `expected` | lockから作った期待schedule |

未完了（本Issueの範囲外）:

- 実runnerでの§10.7 end-to-end（取得 → 保存 → 回収 → 検証 → 集計）。本Issueはfixtureと、test用selectorでの
  実engine 1半荘で取得相当から集計までを確認しただけである
- lock / result artifactへの結合（manifestのdigestの記録、主指標の再計算、recorded値との完全一致の `verify`）
- 槓・九種九牌を選んだ後の適用、槓factのcandidate経由の取得（#472の未確認を維持）。
  合成factとengine直結selectorでのtestは、serializer / validatorの契約の確認にとどまる
- Python / Rust照合、校正、正式seedの予約
