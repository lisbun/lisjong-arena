# Stage A0 Tenpai protocol lock (#259)

This document records the pre-result scientific decisions implemented by
`lisjong_arena.stage_a0_tenpai_protocol_lock`.

The lock is intentionally separate from A/T training.  None of the commands in
this package train a model, run VALIDATION model inference, inspect VALIDATION
Tenpai prevalence summaries, read protected TEST payloads, or run A-vs-T games.

## Qualified route

The entry gate is the completed #258 retained route:

- outcome: `RETAINED AUGMENTATION QUALIFIED`
- retained dataset:
  `69094c1b82f2aaedfed57cb3021b90d44642c3978a2368d4d1e2d927c5a7b2f4`
- #258 feasibility report:
  `b6e4a0350b136468ee162c5e60bbb024f62be2a2a18f2f475bdacc3eee48bc8e`
- #258 privileged sidecar:
  `9070f350a5281fc7b7f60edeaded2b931a892a338b11d600ee6ead162add31a2`
- TRAIN: `245..264`
- VALIDATION: `265..270`
- protected TEST: `271..276` (metadata only; payload stays unread)

The scientific materializer replays only the contiguous retained prefix
`245..270`.  Every replayed public decision must exact-align to the retained
feature bytes, legal mask, teacher action, decision identity and round identity
before a privileged label is accepted.  This is annotation of retained rows,
not silent row regeneration.

Materialization must use the #258 source-semantic environment:

- lisjong `a0666d24e66179a45fd6e231a3cbd489b492d162`
- lisjong-engine `8735e89e1aea000ab59368d0368d476787827741`
- RiichiEnv `0.4.8`
- CPython `3.14.6`

Arena instrumentation revision may be newer and is recorded explicitly.

## Baselines and primary learnability gate

Baseline 0 is descriptive only: TRAIN global eligible-cell Tenpai prevalence,
with the Jeffreys `Beta(0.5, 0.5)` estimate retained alongside raw prevalence.

Baseline 1 is the primary comparator.  Its exact public key is:

`live_wall_tiles_remaining x opponent public meld count`

The exact-key estimate is Jeffreys-smoothed.  Missing keys back off
deterministically to live-wall-only and then the TRAIN global estimate.  There
is no result-dependent minimum-support tuning.  Out-of-domain keys fail closed.

The primary metric is eligible-cell Bernoulli log loss.  Probabilities are
clipped to `[1e-7, 1 - 1e-7]`; non-finite/out-of-range values are invalid.

The retained VALIDATION set has only six independent hanchan blocks.  The lock
therefore uses a paired Student-t 95% interval over the six block deltas instead
of a large-sample normal approximation.  A zero-eligible VALIDATION block is
invalid and is never dropped or converted to a zero-loss block.

PASS requires both:

1. lower bound of the paired 95% interval for
   `Baseline1 - mean(T_seed0,T_seed1,T_seed2)` to be strictly above zero; and
2. each of the three T training seeds to have strictly positive overall
   eligible-cell improvement over Baseline 1.

No secondary metric can override this rule.

## A/T training contract handed to Child 3

Training seeds are exactly `0, 1, 2`; the interactive anchor is seed `0`.

Within each seed, shared encoder and Policy-head initialization is constructed
once and byte-cloned into A and T before any optimizer step.  The T-only
auxiliary head is a `Linear(128, 3)` logit head for relative opponents
`+1,+2,+3`, initialized from the separate deterministic namespace
`1_000_000 + training_seed`.

The Policy path keeps the established flat-BC contract:

- public feature width 8204
- shared hidden width 128
- Policy head width 802
- legal-mask-aware Policy cross entropy
- Adam, lr `1e-3`, weight decay `0`
- batch 256
- at most 20 epochs, patience 4
- checkpoint selected only by lowest VALIDATION choice-row masked Policy CE

The T auxiliary loss is unweighted BCE-with-logits averaged over AVAILABLE
non-riichi opponent cells only, with `lambda_tenpai = 1.0`.  There is no class
weighting.  Riichi/unavailable cells never become negatives.  A batch with no
eligible cells contributes zero auxiliary loss; an entire TRAIN epoch with no
eligible cells is invalid.  Non-finite training values are invalid.

## Downstream preflight

The common downstream filler population is
`mechanism-riichi-defense x3`, pinned to lisjong
`f29d129c67e5232d06563c6e457754377734ed14`.  It is independent of A/T,
has ordinary win/ron behavior, and contains defensive logic, making it more
appropriate for a Tenpai-supervision mechanism than the passive-tsumogiri
population used by #252.

Historical planning evidence is #211 and #252.  Their reported 100-block
normal-interval widths imply paired-block SD proxies of approximately 1278.7
and 1234.5 score points.  The lock conservatively uses the larger value.

The predeclared minimum meaningful mean score effect is 250 points and the
acceptable 95% half-width is 200 points.  Classical MDE planning is separately
defined as two-sided alpha 0.05 / power 0.80:

`MDE = (z_0.975 + z_0.80) * s / sqrt(n)`

The minimum block count satisfying both the precision and MDE criteria is 206.
Therefore downstream is locked as **A0 DOWNSTREAM ENABLED** with:

- ordered seeds `37700..37905` (206 blocks)
- 4 focal-seat rotations per arm
- 824 games per arm
- 1,648 games total
- game mode `4p-red-single`
- max steps `10000`
- primary paired statistic: per-seed four-rotation T focal-score mean minus A
  focal-score mean

At the conservative historical SD proxy, projected SE is about 89.09 points,
projected normal 95% half-width about 174.62 points, and projected classical MDE
about 249.61 points.

This budget is fixed before A/T results.  It cannot be extended or changed as a
rescue after result exposure.

## Operator sequence

Lock A must be produced before scientific label materialization:

```powershell
python -m lisjong_arena.stage_a0_tenpai_protocol_lock lock-a `
  --dataset <retained-dataset-dir> `
  --feasibility-report <issue-258-feasibility-report.json> `
  --qualified-sidecar <issue-258-sidecar-dir> `
  --output <lock-a.json>
```

Then, in the same historical source-semantic environment used to qualify #258,
materialize TRAIN+VALIDATION labels and the player-safe baseline-key artifact:

```powershell
python -m lisjong_arena.stage_a0_tenpai_protocol_lock materialize `
  --lock-a <lock-a.json> `
  --dataset <retained-dataset-dir> `
  --sidecar <scientific-sidecar-dir> `
  --public-keys <scientific-public-keys-dir>
```

Return to the pinned downstream/current environment and finalize Lock B:

```powershell
python -m lisjong_arena.stage_a0_tenpai_protocol_lock lock-b `
  --lock-a <lock-a.json> `
  --scientific-sidecar <scientific-sidecar-dir> `
  --public-keys <scientific-public-keys-dir> `
  --output <lock-b.json>

python -m lisjong_arena.stage_a0_tenpai_protocol_lock verify `
  --lock-b <lock-b.json>
```

Lock B contains only TRAIN-derived baseline parameters.  The scientific sidecar
contains VALIDATION labels as locked bytes, but Lock B does not publish
VALIDATION Tenpai prevalence or any model result.

Child 3 must consume the exact Lock B artifact.  Any later change to seeds,
baseline keys, smoothing, lambda, training seed, checkpoint selection, opponent
population, precision criterion or game budget requires invalidating this lock
and opening a new explicit research decision.
