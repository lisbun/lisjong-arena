# Arena #291 — frozen E160 prevalence-offset linear probe

Issue: `lisbun/lisjong-arena#291`  
Parent: `lisbun/lisjong-project#36`

## Question

This bounded Phase 11 diagnostic asks whether the exact frozen #167/#172 E160
same-step latent contains linearly accessible structural-wait signal beyond the
exact TRAIN per-tile prevalence baseline.

The primary model is fixed as:

```text
logit P(wait[row, tile])
  = logit(p_train(tile))
    + W[row, tile] · (z - mu_train[row])
```

with:

```text
z                 exact #172 same-step next_latent, dimension 128
mu_train[row]     TRAIN-only mean for the exact #172 output row
W                 [3,34,128], 13,056 parameters
bias              none
hidden layer      none
activation        none
E160 update       none
```

Zero correction therefore reproduces the prevalence baseline exactly.

## Retained evidence

The implementation strict-reads the existing retained chain:

```text
#150 retained corpus / dataset
#157 predecessor evidence required by the #167 loader
#167 exact E160 artifact
#172 latent extraction / target / eligibility semantics
#222 result identity as context only
```

Population:

```text
TRAIN        360..423   64 hanchan
VALIDATION   424..439   16 hanchan
formal TEST  none
```

Expected coverage:

```text
TRAIN       11,102 eligible rows / 377,468 cells
VALIDATION   2,447 eligible rows /  83,198 cells
unavailable rows 0
all-zero rows    0
```

The exact frozen E160 state digest remains:

```text
581f4d20138291ea7c6b22508105b2ac2ed40cc3b3668e680376f3b9adf0885e
```

Any mismatch is `STOP / INVALID`.

## TRAIN-only centering

A raw bias-free linear map can still create a static shift when the latent mean
is nonzero. The primary formulation therefore locks one TRAIN-only mean vector
per exact #172 output row:

```text
mu_train[row]
  = mean next_latent over TRAIN examples where that target row is eligible
```

VALIDATION does not influence centering. There is no scaling, PCA, whitening,
feature search, or alternate centering path.

The pre-execution lock records the three 128-vectors, their eligible counts,
the centering identity, the complete retained latent fingerprint, the frozen
E160 byte digest, and TRAIN / VALIDATION centered-latent aggregate summaries.
Those summaries use latent values and public eligibility only; they do not
include structural-wait label counts.

## Solver

Only the detached linear correction is trainable.

```text
optimizer                torch.optim.LBFGS
dtype                    float64
device                   CPU
torch threads            1
deterministic            true
initialization           all zero
learning rate            1.0
max_iter                 100
max_eval                 125
history_size             100
tolerance_grad           1e-9
tolerance_change         1e-12
line_search              strong_wolfe
L2                       0
training seed            none
```

E160 remains in its exact retained rollout dtype and has `requires_grad=False`.
Its complete state is checked byte-for-byte after fitting.

## Evaluation

Primary:

```text
Delta = logloss(prevalence) - logloss(E160 offset probe)
positive = probe better
```

Paired uncertainty reuses the established whole-hanchan bootstrap:

```text
replicates   10,000
seed         148
percentiles  2.5 / 97.5
order stats  249 / 9750
```

Exhaustive result:

```text
lower > 0   E160 OFFSET SIGNAL
upper < 0   E160 OFFSET REGRESSION
otherwise   E160 OFFSET INCONCLUSIVE
```

The #222 classical result and #172 nonlinear readout are historical context
only. They do not alter the primary classification.

Development selection exposure changes from 5 to 6 only when this Issue's
single locked VALIDATION evaluation is exposed.

## Artifacts

Generated artifacts stay outside Git:

```text
execution-lock.json
latent-summary.json
e160-offset-probe/
  model.json
result.json
```

All are write-once. JSON artifacts use canonical bytes and self-identifying
model/result payloads. `verify` recomputes the evaluation and requires exact
canonical result re-derivation.

## Post-merge runbook

Do not execute the real VALIDATION result from this implementation PR. After
merge, use a clean reviewed `main` with the exact retained artifact roots.

Example PowerShell:

```powershell
$root = 'C:/Dev/lisjong-artifacts/issue-291-e160-offset-probe'
$p150 = 'C:/Dev/lisjong-artifacts/issue-150-phase10'
$p157 = 'C:/Dev/lisjong-artifacts/issue-157-epoch-budget'
$p167 = 'C:/Dev/lisjong-artifacts/issue-167-optimization-saturation'
$arenaRevision = git rev-parse HEAD

python -m lisjong_arena.phase11_e160_offset_probe lock `
  --out-root $root `
  --corpus-root $p150 `
  --phase157-root $p157 `
  --phase167-root $p167 `
  --arena-revision $arenaRevision `
  --artifact-audit 'Issue #291 retained artifact audit YYYY-MM-DD'

python -m lisjong_arena.phase11_e160_offset_probe preflight `
  --out-root $root `
  --corpus-root $p150 `
  --phase157-root $p157 `
  --phase167-root $p167

python -m lisjong_arena.phase11_e160_offset_probe train `
  --out-root $root `
  --corpus-root $p150 `
  --phase157-root $p157 `
  --phase167-root $p167
```

At this point no new #291 VALIDATION prediction has been exposed. Record the
execution-lock identity, centering identity, latent fingerprint, model identity,
and `result_exposed=false` on Issue #291.

Then perform exactly one result exposure:

```powershell
python -m lisjong_arena.phase11_e160_offset_probe evaluate `
  --out-root $root `
  --corpus-root $p150 `
  --phase157-root $p157 `
  --phase167-root $p167

python -m lisjong_arena.phase11_e160_offset_probe verify `
  --out-root $root `
  --corpus-root $p150 `
  --phase157-root $p157 `
  --phase167-root $p167
```

Do not change centering, solver, regularization, architecture, seeds, or
evaluation rules after the lock. There is no rescue path in #291.
