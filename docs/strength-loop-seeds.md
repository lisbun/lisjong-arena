# Strength Loopのseed予約準備（#480）

Owner: Arena。[#480](https://github.com/lisbun/lisjong-arena/issues/480)の途中成果。
`python -m lisjong_arena.strength_loop_seeds`は既存Seed Registryへ渡すS1/S2の予約要求を
機械生成し、workflow実行後のallocationを照合する。workflow dispatch、対局、commit、
S3予約は行わない。ローカル出力をlive allocation authorityとして扱わない。

常設ownerは`lisbun/lisjong-arena#480`、populationは`strength-loop-{run_id}-s1/s2`、
splitは`DEVELOPMENT-A` / `DEVELOPMENT-B`。domainは既存の
`lisjong-engine-project-standard-v1-hanchan-v1`に固定する。
S3は同domainの別poolとして残量だけを検査し、予約要求・bindingを生成しない。
同じseedをsplit名だけ変えて使い回すことはできない。

## 入力の固定

#481のprotocolと#297の探索対象を固定してから、次のspecを作る。これはschema例であり、
数値・protocol・revision・poolを承認済みの実行条件として使わない。

```json
{
  "schema": "arena-strength-loop-seed-preparation-v1",
  "run_id": "example-001",
  "arena_revision": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "authority_commit": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "protocol": "example-development-v1",
  "protocol_revision": "example-v1",
  "provenance_reference": "承認されたpoolと事前登録を参照するURL",
  "pools": {"S1": [10, 11, 12], "S2": [20, 21, 22], "S3": [30, 31, 32]},
  "counts": {"S1": 2, "S2": 2},
  "formal_min_remaining": 2
}
```

- poolは明示的membership。S1/S2/S3間の重複・重複member・bool・範囲外seedを拒否する。
- registry上のRESERVED / COMMITTED / RETIREDとlegacy quarantineをすべて除外する。
  残ったpool memberを整数昇順で選ぶ。終了候補の使用済みseedを独立再確認へ流用しない。
- formal残量が指定閾値**未満**ならS1/S2の準備も停止する。閾値と等しい場合は通過。
  S1/S2の不足も停止。poolや閾値を自動で広げない。
- pool・閾値は評価基盤側が事前固定する。候補作成LLMが変更できる設定にはしない。
  このmoduleは任意のspecを承認する機構ではなく、入力元の信頼性はcallerが確保する。

## live予約からlockまで

1. [Seed Registry](seed-registry.md)の手順でlive `seed-registry` branchを取得し、
   exact commitとcanonical ledgerを保存する。mainのbootstrapだけで空きを判断しない。
   `authority_commit`はその取得commit。CLI自体にはGitHub照会・commitとの照合機能はなく、
   callerが取得経路とcommitの対応を検証・記録する。
2. 保存したplanning ledgerを入力に準備する。

   ```sh
   python -m lisjong_arena.strength_loop_seeds prepare \
     --spec spec.json --planning-ledger planning-ledger.json --output requests.json
   ```

   出力の`PREPARED_NOT_RESERVED`は空きのsnapshot確認だけで、予約成功を意味しない。
3. 実行条件を満たしたcontrollerが出力`requests.S1`、`requests.S2`を既存
   `seed-registry.yml`のworkflow_dispatch inputsとして順番に渡す。値をshell codeへ
   連結せず、構造化APIまたは引数配列を使う。workflowがmerged main revisionと最新台帳を
   再検査し、serialized/fast-forwardで予約する。競合したら停止し、失敗を無視しない。
4. 各workflowの成功とpublish後のauthority commitを確認して、新しいlive ledgerを取得する。
   workflowの出力allocation IDを`{"S1":"...", "S2":"..."}`として保存する。

   ```sh
   python -m lisjong_arena.strength_loop_seeds bind \
     --spec spec.json --planning-ledger planning-ledger.json \
     --current-ledger current-ledger.json --allocation-ids ids.json --output bindings.json
   ```

   元snapshotから予約要求を再導出し、membership、owner、population、split、protocol、
   Arena/protocol revision、provenance、purposeを照合する。元allocationの消失・改変・state逆行、
   取り違え、RETIRED/COMMITTEDの新規実行、formal残量低下は拒否する。
   無関係な追加予約はbindingを無効にしない。
5. `RESERVED_NOT_EXECUTED`のbindingを#481のprotocol lockへ渡す。実行直前にlive bindingと
   #100の停止/予算条件を再確認する。本CLIは実行直前チェックやprotocol lockを代替しない。

すべての出力は新規fileだけに書き、既存fileを上書きしない。unknown fields/schemaは拒否する。
2つの予約はatomicではない。片方だけ成功した場合も予約を自動解放せず、残ったallocationを
記録して停止する。再開・seedの差し替えはこの実装に含めない。

## 実行後のcommitと停止

#481の局単位artifactとstrict readbackが成功した後だけ、controllerが各allocation IDを
既存workflowの`operation=commit`へ渡す。失敗/中断/未使用の扱いは事前登録に従ってretireし、
FREEへ戻さない。commit成功のauthority commitとartifact identityを同じrunに記録する。
formal残量不足などの停止時には非zero exitと`STOP / INVALID`をcontrollerが記録・通知する。
通知先・定期実行は#98/#99/#100の運用決定後に接続する。

## 検証と未完了範囲

`tests/test_strength_loop_seeds.py`は合成台帳でprepare→予約→binding、衝突・隔離、
残量、state遷移後の再実行拒否、書込拒否を確認する。live workflowや実対局の実施証拠ではない。
既存`test_seed_registry.py`がreserve/commit/retireの状態遷移を検査する。

#480全体の完了には、固定された既知development入力でのlive予約→protocol lock→実行→
strict readback→commitの接続記録が残る。#481の対象/経路固定前に、正式seedを動作確認で消費しない。
