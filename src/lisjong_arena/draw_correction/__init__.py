"""通常ツモDLVAT型補正のArena側（#598、設計 lisjong#390、価値と補正 lisjong#391）。

```text
read_live_draw_capture()  ──> projection.project_record()  ──> lisjong.draw_correction
  （#596のprivileged artifact）     pre_draw → p用・V用の射影       p・V1・単位補正（κ = 1）
                                    realized → lookup用カテゴリ
         ↓
join.join_capture_with_hanchans()   (seed, placement)・round_ordinalで半荘結果（engine最終rank）に結ぶ
         ↓                          全6配置の完全なseed blockだけ。D = (R_B − R_A) / 12、X（同じcontrast）
kappa.estimate_kappa()              TRAIN: κ̂ = max(0, Ĉov(D, X) / V̂ar(X))
kappa.write_frozen_kappa()          write-onceの凍結artifact（strict readback）
         ↓
analysis.analyze_correction()       評価: D' = D − κ̂X、r = Var(D') / Var(D)、paired block bootstrap
```

診断用の別経路であり、正式protocolの主指標・結果・判定は変えない。閾値や判定規則は
持たない（pilot計画で事前に固定する）。詳細は`docs/draw-correction.md`を正本とする。
"""
