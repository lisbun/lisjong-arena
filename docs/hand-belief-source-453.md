# HandBelief手牌正解sourceのproducer（lisjong-arena#453）

owner: lisbun/lisjong#255（HandBeliefの推定精度測定）。契約: lisbun/lisjong#256（v1、lisjong
`docs/hand-belief-accuracy-source.md`、`lisjong.learning.hand_belief_source`）。

Arenaは対局の実行と観測事実の記録だけを担う。ラベル（`HandBelief`の正解）はlisjongが
読み込み時に計算し、Arenaはラベルを作らない。**推定精度のための入力データであり、強さの評価ではない。**

## 生成script

`scripts/generate_hand_belief_source_255.py`

- lisjong-engine上で`PlacementAwareSpeedCallPolicy`（Champion）×4の自己対局。RuleSetはengineの既定
  （`RuleSet.default()` = `PROJECT_STANDARD_RULES`）。1 seed = 1半荘
- 対象: 合法手に打牌を含む観測者の判断すべて（`all-observer-discard-decisions.v1`）。`sequence`は
  その半荘でengineがselectorを呼んだ順の通し番号（全席・反応判断を含む）
- 他家3席の手牌: Policyを包む記録用wrapperが、selector呼び出しの中（engineが選択を適用する前）で
  `RoundState.hand_tiles()` / `melds()`を読む。値はその判断と同じ`sequence`のsnapshotとして書く（v1契約）。
  wrapperは手牌をPolicyへ渡さず、内側のPolicyは`DecisionContext`だけを受け取る
- 同一状態の検査（fail closed）: 観測者のengine手牌と`PolicyInput`の手牌が一致すること、全席の副露が
  `PolicyInput`の公開副露と一致すること

## 記録漏れの検出（coverage）

lisjongの読み込み検査は、同じ判断を両ファイルから落としてmanifestもその行数で作ると漏れを検出できない。
そこで次を行う。

1. wrapperが、呼び出し時点で対象判断の`(seat, sequence)`を記録する（行の抽出とは独立）
2. 半荘ごとに、出力行のkey集合・件数がその記録と一致し、重複がなく、各手牌行が他家3席をちょうど
   1回ずつ判断と同じ`sequence`で持つことを検査する（`verify_coverage()`）。書き出し直前にも全半荘で再検査する
3. `coverage.json`（Arenaのproducer証跡。lisjongは読まない）へ半荘ごとの対象判断数とkey集合の
   SHA-256を書く。`verify-coverage`で、書いた後のファイルをこれと照合できる

```sh
python scripts/generate_hand_belief_source_255.py verify-coverage <source>
```

## 段階とseed

| 段階 | 目的 | seed |
|---|---|---|
| producer確認用pilot | 生成・読み込み・coverageの動作、費用、層別support（リーチ者・門前非リーチ者・副露者）の確認。**lisjong#257の測定には使わない** | RETIRED 931000..931999のうち未使用の931400..931409（10半荘）。931100..931399（lisjong#236 / #237 / #245の開発）はscriptが拒否する |
| 測定用population | lisjong#257の測定 | 933000..933399（400半荘）。lisjong#257の事前登録で分割・実行方法を固定し、生成前にseed registryで予約する |

1回の実行でpilotのseedと測定用のseedを混ぜない（scriptが拒否する）。

生成前に記録する値: Policy identity（`PlacementAwareSpeedCallPolicy`）とlisjong revision、
lisjong-engine / Arena revision（manifestの`producer`にも入る）、RuleSet、seed範囲。

## 完全性検査

lisjongの読み込み検査は、`hand_belief_source`を含むlisjong（main `c9f7d0b`以降）を入れた別のvenvで行う
（Arenaのpinはそれより古い。#245と同じ方式）。

```sh
python -c "from lisjong.learning.hand_belief_source import read_labelled_source as r; m, rows = r('<source>'); print(len(rows))"
```
