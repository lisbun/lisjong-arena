# Learned Policy P6 Gate B — exact #181 candidate vs passive tsumogiri x3

Issue: `lisbun/lisjong-arena #183`

This document defines the bounded rollout that follows the valid #181 result:

```text
P6 CONSERVATIVE-Q GATE A SIGNAL
```

The purpose is not to prove strength. It asks only whether the exact retained
#181 conservative-Q candidate has basic interactive single-round viability on a
fresh development population.

## Exact candidate

The candidate is strict-read from the retained #181 bundle. No reconstruction,
retraining, extra epoch, alpha search, or substitute checkpoint is permitted.

```text
checkpoint schema
arena-learned-policy-p6-conservative-q-checkpoint-v1

candidate identity
learned-p6-conservative-q:9d5bf5afc164dd49314fbb6cca3338426c7cf35384f36401cfb6b3a329b22ac7

canonical weights
679366f11c30e9dda39ca6fcf6e745da5a561e65a0d32b1576a39bd059627499

source dataset
69094c1b82f2aaedfed57cb3021b90d44642c3978a2368d4d1e2d927c5a7b2f4

TRAIN support
230b2f07dc95d169ebfb85b9deb6174f22909b52025979ae64da541bd9481d9e

selected epoch
20
```

The exact #181 result binding is also required:

```text
unclassified result
01fa261422517868949a58dba717eafc722aa4b84d85ed1282ebdca0c49a90e3

classified result
1ed17d6984bc96347b2461f70a034e55474d3d2f44802f9461198f100801d051

classification
P6 CONSERVATIVE-Q GATE A SIGNAL
```

## Serving family

Gate B deliberately reuses the established #162 P1 hybrid runtime:

```text
all legal actions are ordinary discards
+ at least two choices
+ exact TRAIN support complete
    -> 8241 P1 feature
    -> exact #181 P6 Q model
    -> legal-masked argmax Q

otherwise
    -> exact yakuhai-call scaffold
```

`p1_serving.create_p1_hybrid_runtime()` is reused directly. The #173
keep-shanten selection guard is **not** applied. Adding the guard would confound
whether the #181 learner improvement itself survives rollout.

The fallback is bound through the same `fallback_policy_block()` and execution
provenance checks used by the established higher-fidelity family. If those
bindings cannot be established, execution stops before game 1.

## Comparator

Reuse the exact #162 Gate-B comparator:

```text
arena-p1-gate-b-passive-tsumogiri-v1
```

It wins when Ron/Tsumo is legal, otherwise passes when possible, otherwise makes
the unique drawn-tile tsumogiri. It does not call, riichi, kan, or choose an
arbitrary fallback.

This is a deliberately weak viability comparator, not the current strength
baseline.

## Fresh population

First-choice locked shape:

```text
ordered seeds    597..621 inclusive
seed blocks      25
rotations/seed   4
total games      100
game mode        4p-red-single
workers          1
formal TEST      false
seat assignment  ABBB
```

The operator/assistant must re-check open/closed relevant Issues and any known
private/local allocations immediately before lock generation. A collision before
result exposure means `SEED PLAN REFORMULATE`. After result exposure, seeds are
never extended or replaced.

## Primary classification

Primary unit: seed block.

```text
lower 95% bound > 0
  -> P6 CONSERVATIVE-Q GATE B POSITIVE SIGNAL

upper 95% bound < 0
  -> P6 CONSERVATIVE-Q GATE B NEGATIVE SIGNAL

otherwise
  -> P6 CONSERVATIVE-Q GATE B INCONCLUSIVE
```

Secondary Mahjong metrics and serving diagnostics cannot change this outcome.

## Output retention

Prepare an empty durable parent before lock generation:

```text
C:\Dev\lisjong-artifacts\offlineq-183-p6-gate-b\
```

Expected write-once outputs:

```text
strength.json
result.json
classified.json
```

The #181 input bundle remains immutable:

```text
C:\Dev\lisjong-artifacts\offlineq-181-p6-gate-a\checkpoint
C:\Dev\lisjong-artifacts\offlineq-181-p6-gate-a\gate-a-result.json
C:\Dev\lisjong-artifacts\offlineq-181-p6-gate-a\gate-a-classified.json
```

## Pre-execution lock

Run only from clean merged `main`, after fetching `origin/main`, with the exact
runtime/dependency provenance that satisfies the locked yakuhai-call fallback
binding.

Example:

```powershell
$root = "C:\Dev\lisjong-artifacts\offlineq-183-p6-gate-b"
New-Item -ItemType Directory -Path $root

python -m lisjong_arena.learned_policy_offline_q.p6_gate_b lock `
  --candidate-checkpoint C:\Dev\lisjong-artifacts\offlineq-181-p6-gate-a\checkpoint `
  --gate-a-result C:\Dev\lisjong-artifacts\offlineq-181-p6-gate-a\gate-a-result.json `
  --gate-a-classified C:\Dev\lisjong-artifacts\offlineq-181-p6-gate-a\gate-a-classified.json `
  --strength-artifact "$root\strength.json" `
  --result "$root\result.json" `
  --classified-result "$root\classified.json" `
  --lock-output C:\Dev\lisjong-artifacts\offlineq-183-p6-gate-b-lock.json `
  --external-freshness-confirmed
```

Lock generation strict-reads the #181 checkpoint and both Gate A result files,
checks the exact candidate/result identities, verifies clean fetched merged main,
locks runtime/provenance/serving/comparator/seed plan, and checks all three
write-once output destinations before any game can start.

Review the rendered lock and post it to Issue #183. Preserve the exact comment
URL.

## Locked run

After the reviewed lock comment exists:

```powershell
python -m lisjong_arena.learned_policy_offline_q.p6_gate_b run `
  --lock-file C:\Dev\lisjong-artifacts\offlineq-183-p6-gate-b-lock.json `
  --lock-comment-url https://github.com/lisbun/lisjong-arena/issues/183#issuecomment-XXXXXXXXXX
```

The runner re-validates the posted lock, clean merged-main revision, exact
runtime/provenance, all #181 input identities, and all output destinations before
game 1. It then:

```text
100-game ABBB evaluation
  -> write strength artifact
  -> strict readback
  -> canonical summary re-derivation from raw games
  -> write unclassified result
  -> strict readback
  -> derive locked classification
  -> write classified result
  -> strict readback
```

Stdout is not the source of truth.

## No-rescue boundary

After result exposure, do not change or retry in this Issue:

- seeds or game count;
- alpha or temperature;
- checkpoint, epoch, feature, support, reward, gamma;
- #173 selection guard;
- comparator or fallback;
- P2/P3 mechanism;
- threshold or classification rule.

A positive result returns to project #45 for comparison between a separate
higher-fidelity P6 screen and P3/P2. A negative/inconclusive result returns to
#45 for re-ranking. Neither path automatically starts hanchan evaluation.
