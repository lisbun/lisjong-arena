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
これらはserializationと接続の検証であり、Adapterが実際に求める宣言位置の正しさや
Policyの強さを主張するものではない。実native scorerによるロン合法sourceの完全な読込は専用CIで確認する。

対象外のoffense O0とfocal outcome sourceは旧schemaと位置の拒否を維持する。
policy sourceが共用するO0 row helperには明示的なcodec選択だけを追加し、
O0 producer自身は常に従来のversion 2を使う。historical artifactのidentityは変更しない。

## 後続の順序

1. producer移行PRのmergeとCI結果を確認する。
2. durable local game recordの保存の既定をv3へ変更する。旧versionのidentityと明示的な互換読込は維持する。
3. その後にRiichiEnvとlisjong-engine両Adapterの供給・整合検証を有効にする。
   現在のengine offline checkpoint projectionも位置はNoneであり、最終供給段階では
   `generate_ron_legal_source_457.Recorder.project_checkpoint`も公開の宣言factから同じ位置を供給する。
   base判断記録とhistoryのPolicyInput全体が一致することを実engine fixtureで再確認する。
4. #493の局面テストと#489の固定入力で位置を照合する。個別のraw行を外部へ出さない。

producerのwire移行だけでは両Adapterの供給は有効にならない。
