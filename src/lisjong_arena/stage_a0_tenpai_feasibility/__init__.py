"""`lisbun/lisjong-arena #258` Stage A0 feasibility — non-riichi Tenpai label path。

current flat-BC decision rowに対応するhidden opponent stateから、canonicalな
non-riichi structural-Tenpai targetを正確かつ再現可能に生成できるかだけを
feasibility-onlyで資格確認する。modelは学習せず、Stage A0のscientific
TRAIN / VALIDATION corpusも生成しない。

```text
decision row
    -> same-state exact hidden opponent hand / melds
    -> canonical exact structural waits
    -> non-riichi mask
    -> T[j] = OR_t W[j, t]
```

## module構成がinformation-flow boundaryの宣言になっている

- `public_row`    : player-safe flat-BC rowとそのdigest。privileged値を持たない
- `hidden_state`  : privileged pre-action snapshotとsame-state binding検証
- `labels`        : canonical Tenpai target availability contract
- `emission`      : 同一row identityでのpublic row + privileged cellのco-emission
- `sidecar`       : 最小privileged annotation contract（Gitへ置かない生成物）
- `retained`      : retained #140/#190 corpusのexact augmentation qualification
- `fresh`         : bounded technical smokeによるlive co-emission qualification
- `report`        : machine-readable feasibility artifact

Tenpai semanticsはArenaが所有しない。canonical authorityは
`lisjong.belief.exact_wait_ground_truth.exact_hand_belief_with_waits()`である。

## Non-goals

A/T model training、Stage A0 VALIDATION learnability、Baseline 1、
lambda_tenpai、class weighting、3-seed model comparison、strength games、
Wait / Ron auxiliary、furiten reconstruction、Oracle Guiding / VLOG、
privileged critic、Champion promotion、formal TEST exposure、full Stage A0
corpus generationは本Issueの対象外である。
"""
