# Learned Policy P6 higher-fidelity — exact P6 vs yakuhai-call x3

Issue: `lisbun/lisjong-arena #185`

This protocol changes only the comparator used by Issue #183. It asks whether
the unchanged, unguarded P6 conservative-Q hybrid shows a fresh-development
single-round score signal against exact revision-bound `yakuhai-call` x3.
It does not retrain or promote a Policy, consume a formal holdout, or start a
hanchan evaluation.

## Reuse and purpose-specific boundary

The implementation reuses:

- Issue #183 strict #181 checkpoint/evidence loading, P6 runtime, P1 8241
  serving feature, TRAIN support gate, and activation diagnostics;
- Issue #179 yakuhai-call factory/class/module semantics, canonical Mahjong
  diagnostics, and higher-fidelity result discipline;
- Issue #177 write-once destination preflight;
- `SingleRoundEvaluationPlan`, `run_single_round_evaluation()`,
  `SingleRoundStrengthArtifact`, and canonical aggregation.

The Issue #185 module owns distinct experiment, lock, result, classified-result,
source Issue/PR, lock-comment, execution-target, and retention identities. It
does not use Issue #179's `require_baseline_provenance()`, because that validator
binds incompatible later package revisions.

## Locked candidate and runtime

The candidate must strict-read as:

```text
checkpoint schema  arena-learned-policy-p6-conservative-q-checkpoint-v1
candidate          learned-p6-conservative-q:9d5bf5afc164dd49314fbb6cca3338426c7cf35384f36401cfb6b3a329b22ac7
weights            679366f11c30e9dda39ca6fcf6e745da5a561e65a0d32b1576a39bd059627499
dataset            69094c1b82f2aaedfed57cb3021b90d44642c3978a2368d4d1e2d927c5a7b2f4
P1 fingerprint     beae3eb8d3dc79b4f837ec98ed3b90eb7530e6f931102b8cdf4854bcf0a15409
TRAIN support      230b2f07dc95d169ebfb85b9deb6174f22909b52025979ae64da541bd9481d9e
vocabulary         543c6bca832069dd88b22554b8546ddcd958840a7be7ed291b4ebab6302d7952
selected epoch     20 / fixed_final_iteration
temperature/alpha  1.0 / 0.1
```

The retained #181 Gate A and #183 Gate B identities listed in Issue #185 are
also strictly bound. Their scores are not pooled with the new population.

The exact dependency family is:

```text
lisjong         99a30c267a3c3e301e132c8799726eb10e012a95
lisjong-engine  8735e89e1aea000ab59368d0368d476787827741
Python          3.14.6
PyTorch         2.13.0+cpu
RiichiEnv       0.4.8
```

Package or runtime drift stops before game 1. The later Issue #179 lisjong and
engine revisions are explicitly not substitutes.

## Serving and baseline

The P6 arm preserves Issue #183 unguarded serving:

```text
all legal actions ordinary discard
+ at least two legal actions
+ exact TRAIN support complete
    -> P1 8241 feature
    -> exact P6 Q model
    -> legal-masked argmax

otherwise
    -> exact yakuhai-call fallback
```

There is no Issue #173 keep-shanten guard, soft guard, or extra tie-break.
The checkpoint is loaded once per runtime, while every game/seat gets a fresh
Policy instance. Action resolution remains
`lisjong.action_vocabulary.resolve_legal_action`.

The three baseline seats use fresh instances from
`lisjong_arena.policy_catalog.create_yakuhai_call`. The exact class, module, and
implementation source revision `a0666d24e66179a45fd6e231a3cbd489b492d162`
are lock fields; a mutable champion alias is not accepted.

## Population and inference

The provisional first-choice shape is fixed:

```text
ordered seeds    622..646 inclusive
seed blocks      25
rotations/seed   4
total games      100
game mode        4p-red-single
max steps/game   10000
workers          1
formal TEST      false
seat assignment  ABBB
```

Immediately before lock generation, re-check current repository declarations,
open and closed relevant Issues, and known local/private allocations. A
collision before result exposure means `SEED PLAN REFORMULATE`. After result
exposure there is no seed extension, replacement, or rescue run.

The primary unit is one four-rotation seed block. Classification uses the
normal-approximation 95% interval over exactly 25 block deltas:

```text
lower > 0  -> P6 HIGHER-FIDELITY SIGNAL
upper < 0  -> P6 HIGHER-FIDELITY NEGATIVE
otherwise  -> P6 HIGHER-FIDELITY INCONCLUSIVE
```

Unavailable evidence is `P6 HIGHER-FIDELITY EVIDENCE BLOCKED`; an invalid
protocol is `STOP / INVALID`. Mahjong and serving diagnostics are descriptive
and cannot change the primary outcome.

## Pre-execution prerequisites

Do not generate the real lock or run from the implementation PR branch. The
required order is:

```text
implementation and tests
-> PR #187 quality and phase6-ml green
-> user-approved merge
-> fresh clean local main fetched from origin
-> explicitly prepared durable output parent
-> final population freshness review
-> pre-execution lock posted to Issue #185
-> one 100-game run
```

Lock generation requires a clean worktree where HEAD equals both collected
Arena provenance and fetched `origin/main`. It records that exact merged-main
revision. At run time, clean HEAD and provenance must still equal the locked
revision. An unrelated later advance of `origin/main` does not invalidate the
already locked revision.

Prepare the durable parent explicitly; the command never creates it:

```powershell
$root = "C:\Dev\lisjong-artifacts\offlineq-185-p6-higher-fidelity"
New-Item -ItemType Directory -Path $root
```

Each final output must be absent, have an existing directory parent, and have
an observably writable parent. Preflight does not create, truncate, or overwrite
a final target.

After merge and the final freshness review, build the lock with retained #181
and #183 evidence paths:

```powershell
python -m lisjong_arena.learned_policy_offline_q.p6_higher_fidelity lock `
  --candidate-checkpoint C:\Dev\lisjong-artifacts\offlineq-181-p6-gate-a\checkpoint `
  --gate-a-result C:\Dev\lisjong-artifacts\offlineq-181-p6-gate-a\gate-a-result.json `
  --gate-a-classified C:\Dev\lisjong-artifacts\offlineq-181-p6-gate-a\gate-a-classified.json `
  --gate-b-result C:\Dev\lisjong-artifacts\offlineq-183-p6-gate-b\result.json `
  --gate-b-classified C:\Dev\lisjong-artifacts\offlineq-183-p6-gate-b\classified.json `
  --strength-artifact "$root\strength.json" `
  --result "$root\result.json" `
  --classified-result "$root\classified.json" `
  --lock-output C:\Dev\lisjong-artifacts\offlineq-185-p6-higher-fidelity-lock.json `
  --external-freshness-confirmed
```

Review the rendered lock and post it to Issue #185. Preserve its exact comment
URL, then run once:

```powershell
python -m lisjong_arena.learned_policy_offline_q.p6_higher_fidelity run `
  --lock-file C:\Dev\lisjong-artifacts\offlineq-185-p6-higher-fidelity-lock.json `
  --lock-comment-url https://github.com/lisbun/lisjong-arena/issues/185#issuecomment-XXXXXXXXXX
```

The successful flow is write-once strength artifact, strict readback, canonical
summary and diagnostics re-derivation, write-once unclassified result, strict
artifact/result rebinding, classification re-derivation, and write-once
classified result with another strict readback. Stdout is not evidence authority.

## No-rescue and interpretation boundary

Do not retrain, add epochs, change alpha/temperature/features/support/reward/
gamma/architecture, add the #173 guard, substitute the checkpoint, fallback,
baseline, or dependency family, append an arm, or start hanchan under this Issue.

A signal means only that this exact P6 candidate showed a positive fresh-
development single-round signal against this exact yakuhai-call baseline. Every
outcome returns to parent project Issue #45 for the next bounded decision.
