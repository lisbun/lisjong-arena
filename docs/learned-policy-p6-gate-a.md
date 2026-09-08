# Learned Policy P6 Gate A — fixed conservative Offline Q

Issue: `lisbun/lisjong-arena #181`

This document defines the operator workflow and scientific boundary for the first
bounded P6 formulation selected after Arena #179.

## Research question

Keep the exact retained #140/#158 data, P1 representation, model, TD target,
support, optimizer, deterministic settings, training budget, and Gate A rows fixed.
Change only one learner term:

```text
A_cql(s) = current legal ordinary-discard actions
           intersect exact TRAIN-supported indices

CQL_gap(s)
  = T * logsumexp(Q(s,a) / T, a in A_cql(s))
    - Q(s,a_behavior)

T     = 1.0
alpha = 0.1

loss
  = mean Huber(Q(s,a_behavior), TD_target)
    + alpha * mean CQL_gap(s)
```

`alpha=0.1` is one pre-registered formulation. It is not an optimized value and
must not be tuned from Gate A results.

## What stays exact

The P6 implementation does not modify the historical #140/#158 training modules.
It reuses their primitives and verifies the locked delta.

```text
P1 feature             8241 / keep-shanten 37 appended to locked v1
model                  8241 -> 128 ReLU -> 802
parameters             1,158,434
selected-action TD     Huber delta 1.0
optimizer              Adam, lr 1e-3, weight_decay 0
batch                   256
gamma                   1.0
target sync             epoch-level hard sync
bootstrap actions       next legal intersect TRAIN support
checkpoint selection    fixed_final_iteration
training/dataloader     exact #158 seeds/settings
maximum epochs          exact #158 budget
```

"Same TD semantics" means the calculation contract stays the same. Candidate
weights naturally differ after the P6 intervention, so later bootstrap target
*numbers* are not required to equal the historical control.

## No new game population

```text
new game generation   0
new hanchan           0
new seed allocation   0
new trajectory data   0
formal holdout         0
```

Gate A reuses the retained `dataset TEST` and `replacement TEST` surfaces. They
are already-exposed development evidence, not a fresh formal test.

## Exact control and common-row comparison

The control is the exact retained #158 P1 serving candidate, including its
canonical weights and TRAIN support identity. The #173 keep-shanten selection
guard is intentionally not applied because it would mask the learner-ranking
pathology under test.

Candidate and control are evaluated on the exact same row identity, legal mask,
and support-complete population. Candidate-specific row dropping or support
re-derivation is forbidden.

Primary per-role conditions are:

```text
SIGNAL direction
  P6 worsen-shanten rate < exact P1 control
  P6-lower paired post-discard shanten count > P6-higher count
  P6 behavior top-1 agreement > exact P1 control

REGRESSION direction
  exact reverse of all three
```

Both primary roles must satisfy the same direction for `SIGNAL` or `REGRESSION`.
Otherwise valid evidence is `INCONCLUSIVE`. If a primary role cannot produce a
valid hand-progression comparison, the result is `P6 EVIDENCE INSUFFICIENT`.

Behavior agreement is a mechanism diagnostic. Because the conservative penalty
anchors observed actions, higher behavior agreement is not a strength metric.

## Retention

Generated model weights and Gate A results are never committed to Git. The
operator declares a non-ephemeral absolute retention root. Existing Stage 4a
retention validation is reused to reject temporary roots, Git work trees, and
pre-existing destinations.

A successful run retains:

```text
<retention-root>/<retention-key>/
  checkpoint/
    manifest.json
    weights.pt
  gate-a-result.json
  gate-a-classified.json
```

The checkpoint identity binds the source dataset, P1 feature, vocabulary, exact
TRAIN support, model, base training contract, CQL formula/alpha/temperature,
canonical model weights, and selected epoch.

## Pre-result lock

The scientific run happens only after implementation has been reviewed, merged,
and the operator has synchronized `main`.

Lock generation is fail-closed unless:

```text
Arena worktree is clean
HEAD == fetched refs/remotes/origin/main
exact #140 dataset / BC / Q / replacement TEST strict-read
exact #158 P1 serving control strict-read
retention destination is unused and non-ephemeral
result_exposed = false
```

Generate the lock from the repository root. Example paths below follow the
existing local artifact layout but are not repository defaults:

```powershell
python -m lisjong_arena.learned_policy_offline_q.p6_gate_a lock `
  --dataset C:\Dev\lisjong-artifacts\issue-140-rebuild\dataset `
  --bc-checkpoint C:\Dev\lisjong-artifacts\offlineq-140-rebuild\candidate-pair\bc-checkpoint `
  --q-checkpoint C:\Dev\lisjong-artifacts\offlineq-140-rebuild\candidate-pair\q-checkpoint `
  --replacement-test C:\Dev\lisjong-artifacts\issue-140-rebuild\replacement-test `
  --p1-control C:\Dev\lisjong-artifacts\offlineq-162-p1-gate-b\candidate `
  --retention-root C:\Dev\lisjong-artifacts `
  --retention-backend operator-local-durable `
  --retention-key offlineq-181-p6-gate-a `
  --lock-output C:\Dev\lisjong-artifacts\offlineq-181-p6-gate-a-lock.json
```

Review the rendered lock and post it as an Issue #181 comment. Preserve that
comment URL. Do not begin candidate training before the lock comment exists.

## Locked run

Run only with the exact posted lock:

```powershell
python -m lisjong_arena.learned_policy_offline_q.p6_gate_a run `
  --dataset C:\Dev\lisjong-artifacts\issue-140-rebuild\dataset `
  --bc-checkpoint C:\Dev\lisjong-artifacts\offlineq-140-rebuild\candidate-pair\bc-checkpoint `
  --q-checkpoint C:\Dev\lisjong-artifacts\offlineq-140-rebuild\candidate-pair\q-checkpoint `
  --replacement-test C:\Dev\lisjong-artifacts\issue-140-rebuild\replacement-test `
  --p1-control C:\Dev\lisjong-artifacts\offlineq-162-p1-gate-b\candidate `
  --retention-root C:\Dev\lisjong-artifacts `
  --lock-file C:\Dev\lisjong-artifacts\offlineq-181-p6-gate-a-lock.json `
  --lock-comment-url https://github.com/lisbun/lisjong-arena/issues/181#issuecomment-XXXXXXXXXX
```

The runner rechecks merged-main provenance, runtime, exact input identities, and
unused retention destination before training. It trains the one fixed P6
candidate, publishes checkpoint + unclassified result, strict-reads them back,
re-derives the exhaustive classification, then exclusive-creates the classified
result.

## Exhaustive outcomes

```text
P6 CONSERVATIVE-Q GATE A SIGNAL
P6 CONSERVATIVE-Q GATE A REGRESSION
P6 CONSERVATIVE-Q GATE A INCONCLUSIVE
P6 EVIDENCE INSUFFICIENT
STOP / INVALID
```

A signal means only that this fixed conservative formulation improved the
predeclared retained same-state ranking/hand-progression surface. It does not
establish single-round strength, hanchan strength, `yakuhai-call` superiority,
CQL optimality, P6 universal necessity, or production readiness.

Negative/inconclusive evidence must not be rescued in #181 with a different
alpha, temperature, IQL/AWR, reward, feature, support rule, data, seed, or epoch
budget. Return the result to `lisjong-project #45` for the next research choice.
