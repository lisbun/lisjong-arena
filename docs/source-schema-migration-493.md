# 宣言牌位置を保持するproducer移行（#493）

lisjong pin `200566fd9a583bc70919feff5d94850545e32a83`（PR #499）を前提に、
新規生成の保存形式を明示的に選ぶ。既存artifact、固定population、過去の評価結果は維持する。
生成済みsourceの再生成やformal評価の再実行はこの移行に含めない。

| 保存経路 | 新規生成のschema | 変更箇所 |
| --- | --- | --- |
| policy source record | `arena-policy-source-record-v2` | `policy_source_record.generation`がv2を選び、共通row encoder/readerへdurable v3を指定する |
| hand belief | `lisjong-hand-belief-decision-record-v2` | `generate_hand_belief_source_255.extract_records`。#257/#473の上位producerとengineロン合法sourceのbaseも共有する |
| riichi deal-in | `lisjong-riichi-deal-in-decision-record-v2` | `generate_riichi_deal_in_source_237.extract_records`。#245の上位producerも共有する |
| 聴牌PUSH/FOLD | `lisjong-tenpai-push-fold-decision-record-v2` | `generate_tenpai_push_fold_source_476`がlisjong writerへv2を明示指定する。入力はengine経由 |
| ロン合法history | `lisjong-ron-legal-history-record-v2` | `generate_ron_legal_source_457`のoffline checkpoint writer。複数stepの全viewをdurable v3で保持する |

派生sourceのmanifest、hand fact、label fact、outcome、ron factのschemaは従来どおり。
durable v3のPolicyInput wire shapeはlisjong v2 codecと一致する。
4席の手牌を含むロン合法checkpointは学習専用のままで、online Policyへ渡さない。

## 互換性と検証

policy source recordはmanifestのschemaからcodecを選択する。
新規生成はv2、明示選択したv1は従来のbyte列と宣言牌位置の拒否を維持する。
入力の値に応じた形式変更や、位置を除去して旧形式へ保存するfallbackは行わない。
新形式はArenaとlisjongの両reader、teacher replay、archive保存・復元まで確認する。

各派生sourceでは、位置を持つ既知development入力の保存・再読み込みを検証する。
hand beliefとriichi deal-inは学習用readerまで通す。
聴牌PUSH/FOLDは既知のRETIRED development seedのengine fixtureと保存用入力を使い、
ロン合法はengine fixtureのcheckpointから位置を持つ保存用viewを構成してwriter/readerを照合する。
初期のproducer移行（#500）ではserializationをfixtureで検証した。
続く供給段階では実engine fixtureの宣言factを使い、ロン合法historyの全viewの位置保持と
base判断記録のPolicyInput全体の一致、聴牌PUSH/FOLDの保存・読み戻しを検証する。
実native scorerによるロン合法sourceの完全な読込は専用CIで確認する。

対象外のoffense O0とfocal outcome sourceは旧schemaと位置の拒否を維持する。
policy sourceが共用するO0 row helperには明示的なcodec選択だけを追加し、
O0 producer自身は常に従来のversion 2を使う。historical artifactのidentityは変更しない。

## 供給と整合検証

producer移行PR #500（`9bbb96a`）とdurable既定v3化PR #501（`810d26e`）をmerge済み。
供給段階では以下を同時に有効にする。

- RiichiEnv: seat-visible `reach`後の本人の最初の`dahai`の局内orderを保存する。
  宣言牌は手出し・ツモ切りの両方を許可し、鳴かれても位置を保持する。
  宣言前はNone、打牌後はDECLAREDの応答時点から値を供給する。局開始でresetする。
- lisjong-engine: `PublicDiscard.is_riichi_declaration`のorderを使う。
  pendingで打牌前はNone、打牌後はDECLARED、成立後はACCEPTEDとして同じ位置を保持する。
  ロンで成立しなかった宣言は、engineが公開フラグを保持していてもRiichiState.NONEなので位置を供給しない。
- 両経路: 宣言後の本人の打牌に非ツモ切りがあれば停止し、ACCEPTEDで位置なしも停止する。
  engineの宣言フラグ複数も拒否する。入力の修正や位置の除去は行わない。
- ロン合法offline checkpoint: engineのpending declaration・finalizationの公開宣言factから
  物理牌を照合し、既存の公開打牌orderへ射影する。両engine経路は同じ検証関数を使う。
  4席の非公開手牌を含むcheckpointはofflineだけで扱い、online Policyへ渡さない。

局面テストは宣言牌の手出し/ツモ切り、鳴かれた宣言牌、第一打のリーチ、複数席、
暗槓前後、宣言直後、局reset、hidden hand/wallの違い、異常入力の拒否を対象とする。
既存`learned_policy_input`のfeatureとtensor値も位置の有無で同一であることを固定する。

## #489の固定入力による照合

`scripts/validate_declaration_positions_493.py`は、#489と同じChampion入力だけをoffline replayする。
`_select_games`でhistory identity・対象IDのhash・対局数を検証し、既存#489 v3集計の
全game log digestとの一致も確認する。既存診断のtrackerは新fieldを読まずに位置の候補範囲を求める。
範囲が1つの行だけをAdapterの局内orderと照合し、不一致・入力不一致・replay失敗は停止する。
個別行・対局IDは出力せず、集計件数だけをstdoutへ返す。既存入力・集計への書込はない。

2026-10-17の確認（変更branch、RiichiEnv 0.4.10）:

| 入力 | 対局×席 | 判断行 | 一意な位置の一致行 | 診断側が複数候補の行 |
| --- | ---: | ---: | ---: | ---: |
| dev | 209 | 33,945 | 7,433 | 15 |
| baseline | 214 | 33,145 | 8,084 | 15 |
| 席合計 | 423 | 67,090 | 15,517 | 30 |

共有66対局を含む。不一致は0件。一致行は判断時点×他家の行であり、独立した宣言の数ではない。
複数候補の30行は一致数に含めない。これは取得位置の検証で、強さ・安全牌の統計的主張や
過去の評価の再実行ではない。既存artifact・固定population・評価結果は維持する。

実行例（pathは各localの固定入力を指定）:

```text
python scripts/validate_declaration_positions_493.py --dev-history <dev-history> --baseline-history <baseline-history> --reference <existing-issue-489-champion-play.json>
```

#493はこの供給PRのmerge後、lisjong側の基盤Issueへ供給revisionを引き渡して完了確認する。
