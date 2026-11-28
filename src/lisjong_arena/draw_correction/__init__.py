"""通常ツモDLVAT型補正のArena側（#598、設計 lisjong#390、価値と補正 lisjong#391）。

```text
plan.write_plan()                     事前固定plan（participant・rules・revision・V1・下限・TRAIN / EVALのseed列）
paired_run.produce_paired_split()     planのsplitを1回ずつ実行し、captureと半荘結果を同じ実行からpairで保存
paired_run.read_paired_split()        planとの一致・同じ実行のbinding（半荘ごとのdigest）を検査して読む
projection.project_record()           pre_draw → p用・V用の射影、realized → lookup用カテゴリ（lisjongへ渡す）
join.join_paired()                    seed blockごとの D = (R_B − R_A) / 12 と同じcontrastの X
kappa.estimate_kappa()                planのTRAIN splitで κ̂ = max(0, Ĉov(D, X) / V̂ar(X))
kappa.write_frozen_kappa()            write-onceの凍結artifact（bytesでstrict readback）
analysis.analyze_correction()         plan・凍結κ（記録したdigest）・EVALを読み戻して照合し、D' = D − κ̂X、
                                      r = Var(D') / Var(D)、paired block bootstrap
```

診断用の別経路であり、正式protocolの主指標・結果・判定は変えない。閾値や判定規則は
持たない（pilot計画で事前に固定する）。詳細は`docs/draw-correction.md`を正本とする。
"""
