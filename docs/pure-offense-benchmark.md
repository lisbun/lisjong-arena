# Pure-offense benchmark v1 (Issue #389)

[Issue #389](https://github.com/lisbun/lisjong-arena/issues/389) is the
scientific authority for purpose, scope and interpretation. This document
records the implemented contract and the operator commands. Numeric results
belong in the Issue, not here.

Benchmark identity: `arena-pure-offense-passive-tsumogiri-v1`
(`src/lisjong_arena/pure_offense_benchmark/`).

## What it measures

One focal Policy against three fixed passive tsumogiri opponents. It measures
**offense under passive opposition**. It says nothing about overall
4-player strength, defense, call strategy, placement, Champion promotion or
RiichiLab strength. v1 is descriptive: it produces **no** PASS / FAIL /
IMPROVED label (`terminal_classification: null`).

## Frozen v1 semantics

```text
backend / mode      RiichiEnv 4p-red-single
seed domain         riichienv-4p-red-single-v1
execution shape     existing ABBB single-round path
                    rotation r: focal at seat r, passive tsumogiri at the others
opponent            arena-p1-gate-b-passive-tsumogiri-v1 (reused unchanged)
max_steps           10000
statistical unit    seed block = mean over the 4 focal-seat rotations
```

All of these are protocol invariants recorded in the benchmark manifest
(`protocol.protocol_manifest()`); readback fails closed if the manifest
differs.

- **turn** = the focal player's own discard count. Turn 0 is the state before
  the first focal discard.
- **first formal tenpai turn** = the existing `SeatRoundStats.first_tenpai_turn`
  (the discard count after which the hand is first formal tenpai; opening-hand
  tenpai is 0).
- **formal tenpai** = `HandEvaluator.is_tenpai()`. It includes no-yaku and
  furiten tenpai and is reported as `formal_tenpai`, not as winnable tenpai.
- **win turn** = the winner's discard count at the moment of the win.
  Tenhou / chiihou is 0. A tsumo after the Nth discard is N. A ron on an
  opponent's discard is the winner's current discard count.
- **riichi turn** = the discard count after the riichi declaration discard.
  `riichi_accepted` is false when the declaration tile is ronned.
- **termination** = `win`, `exhaustive_draw` or `abortive_draw`. Every
  `ryukyoku` whose reason is not `exhaustive_draw` is classified as
  `abortive_draw`. The raw RiichiEnv reason is kept as `draw_reason`. No
  termination type is removed from any denominator.

## Records

Each arm is one write-once directory:

```text
<arm>/strength.json   existing single-round artifact schema v1, unchanged
<arm>/offense.json    benchmark-owned offense record v1
```

`SeatRoundStats` and single-round artifact v1 are **not** modified.
`offense.json` holds only the facts that the existing schema lacks:

- per seat: discard count, win / win turn / tsumo-vs-ron, dealt-in, riichi
  turn / accepted;
- per kyoku: dealer, termination and raw draw reason.

These facts are derived from the same objective MJAI events that
`LocalGameRunner` publishes as the `GameTrace`, through its `trace_sink`.
The benchmark does not read Policy-internal analysis.

`offense.json` also binds the following:

- the SHA-256 of `strength.json`;
- the benchmark manifest;
- the focal identity and reference;
- the Seed Registry allocation binding.

On readback the loader re-checks every record against `SeatRoundStats`
(win, deal-in, exhaustive draw, and tenpai-before-win / before-riichi) and
fails closed on any disagreement.

Focal Arena, lisjong and lisjong-engine revisions come from the existing
`strength.json` provenance. That provenance requires a clean, committed Arena
checkout.

## Metrics

### Arm profile (descriptive)

- **Score:** mean raw kyoku score delta (end − start of the kyoku; no
  uma / oka).
- **Formal tenpai:**
  - reached rate;
  - cumulative incidence by turn 5 / 8 / 12, and the full curve 0..last
    discard;
  - mean first turn among reached kyoku.
- **Win:**
  - rate;
  - cumulative incidence by turn 5 / 8 / 12, and the full curve;
  - mean turn among wins;
  - mean points among wins;
  - tsumo / ron counts.
- **Riichi:**
  - declared rate and accepted count;
  - mean turn among declared kyoku.
- **Deal-in:** rate and mean loss. Always reported, because passive opponents
  can ron the focal player.
- **Termination counts:** focal win, opponent win, exhaustive draw, and
  abortive draw by reason.
- **Exhaustive-draw tenpai rate.**
- **Dealer / non-dealer split.**

Cumulative incidences use **all benchmark kyoku** as the denominator. The
conditional means always appear next to their rate and curve. They are never
paired endpoints.

### Paired comparison

Only unconditional per-kyoku quantities are paired:

- `score_delta`
- `win`
- `formal_tenpai_by_turn_{5,8,12}`
- `win_by_turn_{5,8,12}`

For each pair of arms, the per-seed-block means are differenced:
`D_s = other_s − reference_s`. The summary reports the mean paired
difference, the paired SD, the standard error, a normal-approx 95% CI and
N seed blocks. The mechanics are the existing `lisjong_arena.paired_evaluation`.

The arms must share:

- the same ordered seeds;
- the same Seed Registry allocation;
- the same RiichiEnv version.

The 4,000 rotated kyoku of a 1,000-block run are **not** treated as
independent samples.

### Sample size over the predeclared grid

The grid is fixed in the manifest before any execution:

```text
score_delta            50 / 100 / 200 / 300 points per kyoku
rate metrics           1 / 2 / 5 percentage points
```

For each paired metric and each grid delta, the summary reports two sizes,
both with a minimum of 2:

```text
ci_half_width_seed_blocks  = ceil((1.96 * paired_sd / delta)^2)
power_80_seed_blocks       = ceil(((1.96 + 0.8416) * paired_sd / delta)^2)
```

The first is the number of seed blocks for which the 95% CI half-width is at
most delta. The second is the number for two-sided 5% / 80% power.

## Seeds

Seeds come only from the Seed Registry. Reserve the calibration population
through the **Arena Seed Registry** workflow after this implementation is
merged:

```text
owner_issue  lisbun/lisjong-arena#389
seed_domain  riichienv-4p-red-single-v1
population   (e.g.) pure-offense-calibration
split        (e.g.) DEVELOPMENT
seeds        1,000 fresh seeds
```

The CLI accepts only an active (`RESERVED` / `COMMITTED`) allocation owned by
#389 in that domain, read from a live-ledger snapshot (see
[seed-registry.md](seed-registry.md)).

The calibration population is DEVELOPMENT / CALIBRATION evidence. It may be
reused for later descriptive regression benchmarking. It must not be
presented as fresh confirmatory evidence after it has been used to estimate
variance or inspect Policy differences.

## Operator commands

Run from a clean checkout of merged `main`:

```text
git fetch --no-tags origin +refs/heads/seed-registry:refs/remotes/origin/seed-registry
git show origin/seed-registry:src/lisjong_arena/seed-ledger.json > <ledger.json>

python -m lisjong_arena.pure_offense_benchmark run \
    --focal lisjong.policies.shanten:ShantenPolicy --focal-identity shanten \
    --ledger <ledger.json> --allocation-identity <sha256> \
    --workers <N> --out <arms-dir>/shanten

python -m lisjong_arena.pure_offense_benchmark run \
    --focal two-step \
    --ledger <ledger.json> --allocation-identity <sha256> \
    --workers <N> --out <arms-dir>/two-step

python -m lisjong_arena.pure_offense_benchmark summarize \
    <arms-dir>/shanten <arms-dir>/two-step [...] \
    --out <arms-dir>/summary.json
```

**`run`:**

- `--focal` accepts a curated catalog alias or an explicit first-party
  `lisjong.<module>:<attribute>` reference. An explicit reference also needs
  `--focal-identity`.
- The provenance check runs before any game. A failed or partial run writes
  nothing.

- `--focal canonical-first` and `--focal outcome-q --focal-artifact DIR` run
  the lisjong semantic-envelope Policy with a residual runtime. canonical-first
  uses `ConstantResidualRuntime`; outcome-q uses the outcome-Q artifact
  (`manifest.json` / `weights.f32`) loaded by
  `lisjong.learning.load_outcome_q_policy_factory`. Neither option takes
  `--focal-identity`.
  - The focal identity is `<kind>@<runtime identity>`.
  - The focal reference records the runtime identity. For outcome-q it also
    records the artifact identity and the SHA-256 of `manifest.json` and
    `weights.f32`.
  - Each worker process loads the runtime once, runs torch with one thread and
    fails closed if the runtime identity differs from the resolved focal.
  - This adds focal Policies only. The benchmark protocol, manifest and record
    schemas are unchanged.

**`summarize`:**

- It re-derives everything from the saved arms.
- Arms are compared as `arm_j − arm_i` for every `i < j`, in the order given.
  List them in lineage order.
- Retain arm directories and `summary.json` outside Git.

## AWS calibration run (#389)

The 1,000-seed calibration runs on one EC2 instance through the existing
[`scripts/aws/lisjong-ec2.ps1`](../scripts/aws/lisjong-ec2.ps1) lifecycle
wrapper (#379). The workload is
[`scripts/aws/bootstrap-pure-offense-389.sh`](../scripts/aws/bootstrap-pure-offense-389.sh).
There is no benchmark-specific AWS launcher.

```text
allocation   df8460868ac26cc3505f04b5f4f8f524ccc14dc2423a114487775a1cb69ccb0f
             riichienv-4p-red-single-v1, 389000..389999, DEVELOPMENT
arms         shanten -> ukeire -> two-step -> canonical-first -> outcome-q
             (sequential, lineage order, 4,000 games each, 20,000 in total)
instance     c7i.4xlarge, 16 workers, one instance
```

The arm order separates two effects. `two-step -> canonical-first` shows the
envelope / O0 guard effect. `canonical-first -> outcome-q` shows the learned
residual effect.

The bootstrap fails closed on each of these checks:

- **Arena revision:**
  - the checkout is exact and clean;
  - the revision is merged into `main`;
  - it descends from the allocation's `arena_revision` (`7257e0c`);
  - `pure_offense_benchmark/protocol.py` is unchanged since that revision.
- **lisjong pin and torch:**
  - the lisjong pin is `2a9debe`;
  - torch is `2.13.0+cpu`;
  - `environment_verify` passes.
- **Outcome-Q artifact:** the `manifest.json` / `weights.f32` bytes match the
  frozen #385 digests.
- **Focal identities:** each arm's focal identity matches the frozen identity.

The bootstrap fetches the live ledger into the evidence directory. It never
reserves, commits or retires seeds.

```powershell
$artifact = 'C:\Dev\lisjong-artifacts\issue-203-step-d\outcome-q-artifact'
$run = @{ AwsProfile = 'lisjong'; Label = 'lisjong-389-cal'; InstanceType = 'c7i.4xlarge'; Workers = 16
          Bootstrap = 'scripts\aws\bootstrap-pure-offense-389.sh'
          BootstrapArgs = @('--arena-revision', '<merged main sha>',
                            '--allocation-identity', 'df8460868ac26cc3505f04b5f4f8f524ccc14dc2423a114487775a1cb69ccb0f')
          InputFile = @("$artifact\manifest.json", "$artifact\weights.f32")
          EstimatedRuntimeHours = @(0.5, 1); FailSafeHours = 2; CostBudgetUsd = 3 }
.\scripts\aws\lisjong-ec2.ps1 -Action Preflight @run
.\scripts\aws\lisjong-ec2.ps1 -Action Launch @run -Plan <run dir>\plan.json
.\scripts\aws\lisjong-ec2.ps1 -Action Status  -RunId <run-id> -AwsProfile lisjong
.\scripts\aws\lisjong-ec2.ps1 -Action Collect -RunId <run-id> -AwsProfile lisjong
```

`Status` shows `progress.txt` (arm start / done) and the per-arm
`progress-<arm>.log`. The runner syncs both to S3 every minute.

`Collect` downloads the following under the run directory and checks their
SHA-256:

- `arms/<arm>/`
- `summary.json` / `summary.txt`
- `seed-ledger.json` / `allocation.json`
- `arm-timings.tsv`
- the logs

Re-run `summarize` locally on the downloaded arms as the strict readback. Then
retain the run outside Git.

The allocation moves to `COMMITTED` through the Seed Registry workflow only
after that readback passes and the result is recorded on #389. If the run
fails, decide the seed handling from the cause and whether any result was
exposed.

## Heuristic Champion descriptive reference (#393)

This run measures one arm of the unchanged v1 benchmark:
`--focal placement-aware-speed-call`, the current Heuristic Champion. It uses a
separate DEVELOPMENT allocation, so it is a descriptive reference. It is not
paired with the #389 arms. `summarize` refuses to mix arms whose seed
allocations differ.

```text
allocation   eb07ae2ba8317e4b4c46e9154a1fd12149b2e87b3f16750e11cdabe4e71769bf
             riichienv-4p-red-single-v1, 390000..390499, DEVELOPMENT
             population pure-offense-heuristic-champion-reference
owner        lisbun/lisjong-arena#389
provenance   lisbun/lisjong-arena#393
workload     scripts/aws/bootstrap-pure-offense-champion-393.sh (no input files)
```

The owner is #389 because the v1 manifest contains that owner. #393 manages
the execution and result lifecycle.

The bootstrap reuses the #389 checks. In addition, it fails closed when:

- the whole `pure_offense_benchmark` package differs from the allocation's
  `arena_revision` (`0d4ae36`);
- the installed RiichiEnv is not the pinned `0.4.10`;
- the allocation's range, owner, provenance, population, split or `RESERVED`
  state is not the frozen value;
- the saved arm fails its strict readback. The readback checks the focal
  identity, the 500 seeds, the 2,000 games, the allocation, and the Arena,
  lisjong and RiichiEnv provenance.

```powershell
$run = @{ AwsProfile = 'lisjong'; Label = 'lisjong-393-champ'; InstanceType = 'c7i.4xlarge'; Workers = 16
          Bootstrap = 'scripts\aws\bootstrap-pure-offense-champion-393.sh'
          BootstrapArgs = @('--arena-revision', '<merged main sha>',
                            '--allocation-identity', 'eb07ae2ba8317e4b4c46e9154a1fd12149b2e87b3f16750e11cdabe4e71769bf')
          EstimatedRuntimeHours = @(0.75, 1.5); FailSafeHours = 3; CostBudgetUsd = 4 }
```

`Preflight`, `Launch`, `Status` and `Collect` work as in the #389 run.
