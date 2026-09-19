# Arena #222 — classical public-state structural-wait baseline

Issue: `lisbun/lisjong-arena#222`

This package implements the predeclared Phase 11 diagnostic that asks whether a
small, fixed set of **player-visible state-dependent features** improves exact
TRAIN per-tile structural-wait prevalence on the retained #172 development
surface.

It is not a HandBelief estimator, a Policy-strength experiment, or a replacement
for #172.

## Scientific contract

The primary model is:

```text
logit P(wait)
  = logit(p_train(tile))
    + w · x_state
```

The per-tile Jeffreys-smoothed TRAIN prevalence is frozen. The correction has no
free intercept. Therefore an all-zero correction reproduces the prevalence
baseline exactly.

The primary feature vector is fixed before result exposure:

```text
candidate_unseen_count / 4

penchan_possible
kanchan_possible
ryanmen_low_possible
ryanmen_high_possible

penchan_support
kanchan_support
ryanmen_low_support
ryanmen_high_support

candidate_seen_in_opponent_river
ryanmen_low_counterpart_seen_in_opponent_river
ryanmen_high_counterpart_seen_in_opponent_river

riichi_declaration_turn_normalized
```

For every valid two-tile local sequence mechanism:

```text
support = min(unseen(required_a), unseen(required_b)) / 4
```

The timing feature is fixed as:

```text
riichi_junme / 18
```

Static `is_honor` / `is_terminal` / `is_simple` terms are not model
features. Tile class is diagnostic-only.

The implementation deliberately does not import #163's final danger score or
weights. Opponent-river / suji-like observations are predictors only and never
hard-force structural wait to zero.

## Retained evidence

The implementation strict-reads the retained #150 population and reuses the
exact #172 target / eligibility semantics.

```text
TRAIN        360..423
VALIDATION   424..439
formal TEST  none
```

Before any new result is exposed, the lock must reproduce:

```text
VALIDATION eligible hanchan   16
eligible rows                 2,447
eligible cells                83,198
unavailable rows              0
all-zero rows                 0

exact prevalence log loss
0.16936299644382038
```

Mismatch is `STOP / INVALID`; data are not regenerated.

## Deterministic fit

Only the correction weights are optimized, using TRAIN only.

```text
solver             torch.optim.LBFGS
dtype              float64
objective          unweighted binary log loss
initialization     zero weights
free intercept     none
learning rate      1.0
max iterations     100
max evaluations    125
history size       100
tolerance_grad     1e-9
tolerance_change   1e-12
line search        strong_wolfe
L2                 0
feature scaling    none
device             CPU
torch threads      1
```

VALIDATION is not used for fitting, checkpoint selection, feature selection,
solver choice, regularization, or early stopping.

## Evaluation

Primary:

```text
Delta
  = logloss(prevalence)
    - logloss(classical)
```

Uncertainty uses the existing deterministic whole-hanchan paired bootstrap:

```text
replicates   10,000
seed         148
percentiles  2.5 / 97.5
```

Exactly one valid scientific classification is produced:

```text
95% interval lower > 0
  CLASSICAL BASELINE SIGNAL

95% interval upper < 0
  CLASSICAL BASELINE REGRESSION

otherwise
  CLASSICAL BASELINE INCONCLUSIVE
```

Protocol / provenance / artifact failure is `STOP / INVALID`.

The retained #172 E160 result is shown only as historical context and cannot
change this classification.

## Operator flow

Do not run the real VALIDATION evaluation from an environment that is being used
by another locked scientific run.

After this implementation is merged, start from a clean reviewed `main`.
Choose a new output directory that does not yet exist.

PowerShell example:

```powershell
$corpus = "C:\Dev\lisjong-artifacts\issue-150-phase10"
$out = "C:\Dev\lisjong-artifacts\issue-222-classical-wait-baseline"
$arenaRevision = git rev-parse HEAD

python -m lisjong_arena.phase11_classical_wait_baseline lock `
  --out-root $out `
  --corpus-root $corpus `
  --arena-revision $arenaRevision `
  --artifact-audit "2026-09-XX retained #150/#172 artifact audit"

python -m lisjong_arena.phase11_classical_wait_baseline preflight `
  --out-root $out `
  --corpus-root $corpus

python -m lisjong_arena.phase11_classical_wait_baseline train `
  --out-root $out `
  --corpus-root $corpus
```

At this point the model has used TRAIN only. The first new #222 VALIDATION result
is exposed by:

```powershell
python -m lisjong_arena.phase11_classical_wait_baseline evaluate `
  --out-root $out `
  --corpus-root $corpus
```

Then strict-read and re-derive the persisted result:

```powershell
python -m lisjong_arena.phase11_classical_wait_baseline verify `
  --out-root $out `
  --corpus-root $corpus
```

Generated artifacts remain outside Git:

```text
execution-lock.json
feature-summary.json
classical-model/model.json
result.json
```

The output root and each result-bearing destination are write-once. Do not add
features, alter solver settings, rerun with another model seed, or perform
result-driven rescue after exposure.

## Interpretation boundary

A positive result supports only this statement:

> On the retained development surface, the locked state-dependent public
> features contain incremental structural-wait signal beyond exact TRAIN
> per-tile prevalence.

It does not by itself establish improved defense, improved game strength,
HandBelief integration readiness, or formal generalization.
