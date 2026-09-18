# Targeted honor-release development evaluation (#263)

This package implements the Arena handoff for `lisbun/lisjong#174` without changing the heuristic semantics.

## Fixed identities

- H: `TargetedHonorReleaseTerminalProgressionPolicy`
- C: `MechanismRiichiDefenseYakuhaiCallPolicy` / Arena identity `mechanism-riichi-defense`
- T: the existing deterministic passive-tsumogiri comparator reused from #252
- lisjong revision: `f29d129c67e5232d06563c6e457754377734ed14`

## Phase A

Phase A replays the retained #252 parent population (`651..750`, four rotations, 400 games, `4p-red-single`) as diagnostic reuse only. C remains authoritative for every replayed decision. H is evaluated exactly once on the same focal decision context through the normal typed trace boundary; H never changes the replayed trajectory.

The artifact reports activation-stage, branch, closed/open, shanten, ukeire, retained-value, target-candidate, honor-peer, HVA decisive-stage, R5/tie/action-change and runtime diagnostics. Counts, denominators and derived rates are persisted. The continuation gate requires trajectory identity, 400/400 games, no execution failure, at least one R5 activation, at least one action change and projected 400-game H-arm wall clock at or below eight hours.

## Phase B

Phase B is allowed only after a passing Phase-A artifact. The pre-execution lock accepts exactly one contiguous 100-seed development range and requires the operator to confirm that relevant Issues and known local/private allocations have been audited. Repository-declared allocations are checked as well. A collision before result exposure requires seed-plan reformulation; there is no post-exposure replacement or extension path.

H and C each run the same 100 ordered seeds and four focal-seat rotations against T x3, for 400 games per arm. H is executed through a trace-preserving wrapper that calls H once, records the existing `TargetedHonorReleaseAnalysis`, and returns H's selected action unchanged. Thus Phase-B H trace diagnostics describe the actual H trajectory rather than the Phase-A parent trajectory.

The primary unit is one ordered seed block. For each seed, H and C focal scores are averaged over the four rotations and the paired delta is H minus C. Classification uses only the normal-approximation 95% interval over the 100 paired deltas:

- lower bound > 0: `TARGETED HONOR-RELEASE DEVELOPMENT SIGNAL`
- upper bound < 0: `TARGETED HONOR-RELEASE DEVELOPMENT NEGATIVE`
- otherwise: `TARGETED HONOR-RELEASE DEVELOPMENT INCONCLUSIVE`

Secondary Mahjong and H-trace diagnostics are descriptive only and cannot alter classification.

## Operator flow

Real execution is post-merge only, from clean reviewed merged `main`. Generated artifacts stay outside Git.

1. Build a write-once pre-execution lock with the retained #252 parent artifact, audited fresh Phase-B range, worker count and all output destinations.
2. Post the lock details to Issue #263 before exposing Phase-A results.
3. Run `python -m lisjong_arena.targeted_honor_release_development phase-a ...`.
4. Run Phase B only if Phase A reports `TARGETED HONOR-RELEASE DIAGNOSTIC COMPLETE — PROCEED`.
5. Preserve the H/C strength artifacts, paired result, classified result and Phase-A diagnostic artifact.

A positive development signal is not an automatic Champion promotion or formal generalization claim.
