# Sequential paired evaluation v1

Issue #296 adds one bounded repeated-look protocol for future paired Arena
evaluations.  It is intentionally separate from the existing fixed-N
`paired_evaluation.py` mechanics and does not reinterpret any already-locked
experiment.

## Statistical method

v1 has exactly one method:

```text
bonferroni-normal-approx-alpha-spending-v1
```

For a predeclared schedule of `K` looks and familywise error budget
`alpha`, every look receives the fixed two-sided allocation:

```text
alpha_look = alpha / K
z = Phi^-1(1 - alpha_look / 2)
```

At each allowed look, the paired seed-block deltas are summarized by their
ordered sample mean, sample standard deviation with `n - 1` denominator, and
standard error.  The look interval is:

```text
mean_delta +/- z * standard_error
```

This is not the ordinary fixed-sample 95% interval reused repeatedly.  For
example, with `alpha = 0.05` and four looks, each look uses
`alpha_look = 0.0125`, so its critical value is larger than 1.96.

Bonferroni spending is deliberately conservative.  If each individual look
has its locked marginal error level, the union bound limits the probability
that any of the predeclared looks misses simultaneously covered truth to at
most `alpha`, regardless of dependence between looks.  The current marginal
interval is still a normal approximation over the paired-block mean.  v1 does
not claim that Bonferroni makes that approximation exact for arbitrary
distributions.  Consumers must therefore predeclare a sample schedule for
which the normal approximation is an accepted part of the scientific
contract; the shared primitive enforces a minimum first look of 20 paired
units.

The deterministic test suite additionally exercises repeated-look Type-I
behavior under a fixed-RNG Gaussian null simulation.  The simulation seed,
schedule, trial count, and tolerance are fixed before observing the test
result.

## Practical-effect boundary

Every protocol locks a strictly positive `delta_min`.  The sign convention
and estimand are caller-owned and must be explicitly bound into the protocol.

At a look with repeated-look-adjusted interval `[L, U]`:

```text
L > +delta_min     STOP_POSITIVE_EFFECT_ESTABLISHED
U < -delta_min     STOP_NEGATIVE_EFFECT_ESTABLISHED
otherwise          CONTINUE
```

At the locked maximum sample size, if neither effect boundary has fired:

```text
FINAL_INCONCLUSIVE
```

Boundary equality is non-terminal: `L == +delta_min` and
`U == -delta_min` do not establish an effect.

Statistical significance around zero is therefore not enough.  The interval
must clear the predeclared practical-effect threshold.

## Futility

v1 deliberately has no futility rule:

```text
futility_rule_id = none-v1
```

A point-estimate heuristic would not provide a defensible generic statistical
meaning.  A future protocol may add futility only through a separate locked
method whose interpretation and error behavior are explicit.  v1 never labels
an inconclusive result as equivalence.

## Locked protocol inputs

Before the first result-bearing look, `SequentialPairedProtocol` binds:

- protocol id;
- estimand id;
- sign convention;
- paired statistical-unit id;
- exact ordered seed population;
- exact look schedule;
- maximum paired-unit count;
- familywise alpha;
- derived equal per-look alpha;
- method id;
- deterministic numeric implementation id;
- derived critical value;
- `delta_min`; and
- the explicit absence of a futility rule.

The canonical protocol document is SHA-256 hashed into
`protocol_identity`.  Changing `delta_min`, the look schedule, the paired
unit, the seed population, or any boundary parameter changes or invalidates
that identity.

## Paired unit and execution order

The primitive consumes `PairedSeedDelta`, the existing immutable paired
seed-block summary.  It does not treat the four games inside a seed block as
independent observations.

The submitted units must match the locked ordered-seed prefix exactly.
Reordered, duplicated, missing, non-finite, or algebraically inconsistent
paired deltas fail closed.

`iter_sequential_look_results()` consumes a lazy paired-unit source.  It
yields only at predeclared looks and returns immediately after a terminal
decision, so a caller can persist each yielded immutable snapshot before
requesting more compute.  It never consumes a post-stop paired unit.

## Artifact and replay contract

Each cumulative result snapshot contains:

```text
schema
protocol
protocol_identity
paired_units
looks[]
current_look_index
stopped
terminal_decision
result_identity
```

Every look records:

- one-based look index;
- cumulative paired units;
- cumulative mean effect;
- sample standard deviation;
- standard error;
- allocated look alpha;
- critical value;
- adjusted interval bounds;
- deterministic decision;
- stopping flag and reason; and
- remaining maximum budget.

Strict readback reconstructs the protocol, replays every allowed look from the
stored ordered paired units, and compares the entire document with the
deterministically re-derived result.  The reader therefore rejects missing or
duplicated looks, reordered units, changed boundaries, changed
`delta_min`, changed paired-unit semantics, invalid look indices, and any
artifact that represents continuation after an earlier terminal decision.

Serialization uses the existing canonical JSON plumbing, so the same protocol
and ordered paired summaries produce byte-for-byte identical snapshots.

## Why naive repeated 95% CI peeking is invalid

A fixed-sample 95% confidence interval has its coverage guarantee for the
single predeclared analysis for which it was designed.  Looking after several
sample sizes and stopping at the first ordinary 95% interval that excludes a
boundary creates several opportunities for a chance excursion.  The overall
probability of at least one false stop is then larger than the nominal
single-look error rate.

v1 prevents that specific optional-stopping leak by fixing all looks in
advance and splitting the total error budget across them before any
result-bearing look is exposed.

## Scope

This primitive is forward-looking.  It does not modify existing fixed-N
evaluations and must not be retrofitted onto an experiment whose protocol was
already locked before sequential stopping was declared.

The first bounded integration is synthetic and uses a lazy stream of immutable
paired seed-block summaries.  Its test establishes a strong positive effect at
the first allowed look and verifies that the source is consumed only through
that look, not through `Nmax`.  Protocol-specific migration of an expensive
mahjong evaluation remains separate work.
