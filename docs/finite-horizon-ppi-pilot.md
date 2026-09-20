# Issue #279 finite-horizon PPI feasibility pilot

This is one bounded Prediction-Powered Inference feasibility pilot. It is not a
general statistics framework and does not change Champion or formal Arena
evaluation protocols.

## Fixed diagnostic task

The source distribution is deterministic TwoStepUkeirePolicy x4 under
4p-red-half. Every source hanchan is one statistical observation.

At each choice-discard opportunity:

- fixed candidate: HandValueAwareTwoStepUkeirePolicy
- fixed reference policy action: the action actually selected by source two-step
- same action: contribution 0
- cheap measurement: candidate-minus-reference exact structural-completion
  probability difference over two future self-draw slots (H2)
- high-fidelity reference measurement: the same exact deterministic quantity at
  three future self-draw slots (H3)

The hanchan score is the mean over the predefined opportunities. A hanchan with
no eligible opportunity contributes zero. H3 is a high-fidelity reference
measurement, not ground truth.

This estimand concerns the locked source-state distribution. It is not
candidate-vs-reference match strength.

## Sampling and timing-only calibration

U and L use disjoint fixed seed ranges. U computes H2 only. L computes both H2
and H3. Selection into L never depends on an H2 result. Any execution failure
aborts the run; there is no result-dependent replacement sampling.

The lock command first runs three separate timing-only calibration hanchans.
Their counterfactual outcomes and decision counts are not exposed by the
calibration artifact. The operator supplies a total single-worker-equivalent
compute budget. The lock chooses the maximum labeled budget affordable together
with a predeclared cheap-only multiplier, then freezes the nested budget grid.

## Statistical methods

For each nested labeled prefix the result reports:

- reference-only mean and normal-approximation interval
- cheap-only mean as an uncorrected diagnostic
- basic mean PPI (lambda = 1)
- power-tuned PPI++ using a plug-in variance-minimizing lambda clipped to [0, 1]
- residual SD/bias diagnostic, H2/H3 correlation, interval widths, efficiency
  ratios, and approximate high-fidelity sample equivalent

The independent-U/L mean estimator is:

    lambda * mean_U(X) + mean_L(Y - lambda * X)

Therefore lambda = 0 is the reference-only estimator and lambda = 1 is basic
PPI.

Before any real protocol is lockable, deterministic synthetic repeated-sampling
validation must pass four cases: informative, biased-informative,
uninformative, and negatively associated/pathological. Coverage tolerance is
derived from the binomial Monte Carlo standard error rather than a fixed
percentage-point allowance.

## Run sequence

After the implementation PR is merged and the worktree is clean on main:

    python -m lisjong_arena.finite_horizon_ppi_pilot lock ^
      --out C:\Dev\lisjong-artifacts\issue-279-ppi\protocol.json ^
      --total-budget-hours 8

    python -m lisjong_arena.finite_horizon_ppi_pilot run ^
      --lock C:\Dev\lisjong-artifacts\issue-279-ppi\protocol.json ^
      --out C:\Dev\lisjong-artifacts\issue-279-ppi\result.json ^
      --report C:\Dev\lisjong-artifacts\issue-279-ppi\report.md ^
      --workers 8

The lock binds the exact Arena/dependency provenance, sample membership,
estimand, evaluator horizons, confidence procedure, budget grid, timing
calibration, and synthetic validation. A changed revision requires a new pilot
identity rather than mutation after result exposure.

## Interpretation guardrails

1. This is a diagnostic local counterfactual estimand.
2. It does not establish candidate-vs-reference match strength.
3. H3 is not necessarily ground truth.
4. Statistical efficiency does not establish evaluator correctness beyond the
   locked target.
5. The output must not be used as formal promotion or holdout evidence.
