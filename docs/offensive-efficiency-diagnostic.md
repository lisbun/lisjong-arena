# Offensive-efficiency diagnostic (#256)

This is a purpose-specific offline diagnostic for the current
`mechanism-riichi-defense` heuristic baseline. It does not define a new strength
evaluation and does not allocate fresh seeds.

## Source population

The run replays the exact Arena #252 Phase-B parent-C population:

- seeds `651..750`
- four focal-seat rotations per seed
- 400 `4p-red-single` games
- `mechanism-riichi-defense` vs passive-tsumogiri x3

The operator supplies the retained `phase-b-parent-C.json`. It is strict-read and
bound by file digest. The replay must produce exactly the same canonical
`SingleRoundGameResult` sequence. Any difference stops the analysis with
`TRAJECTORY IDENTITY FAILURE`.

## Phase 1

Only focal-seat discard choices with at least two legal discard actions enter the
choice-quality population. Forced discards are counted separately. For every
choice discard, Arena calls the supported lisjong #172 seam exactly once with
`include_terminal_progression=False` and transports R1-R4 values without
reimplementing their semantics.

Full-legal and baseline-eligible universes remain separate. Aggregation reports
applicability, non-zero incidence, mean, median, nearest-rank p90 and maximum, and
single-axis clusters by branch, selected post-discard shanten, open/closed,
turn bucket, and normal-turn/post-call decision kind.

Turn bucket is fixed by focal prior self-discard count:

- early: 0..5
- middle: 6..11
- late: 12+

## Phase 2

Only Phase-1 decisions whose full-legal or baseline-eligible R4 universe is
completion-all-zero are eligible. The sample is deterministic and capped at 64.
It is stratified by selected post-discard shanten (`0`, `1`, `2`, `3+`) x
open/closed. Each stratum is ordered by `(seed, rotation, decision ordinal)` and
non-empty strata are consumed in stable round-robin order.

Only games containing selected sample decisions are replayed. At each selected
stable decision ordinal, Arena calls the same #172 seam with
`include_terminal_progression=True`. Arena does not implement #169 terminal
progression itself. Sampled-game raw outcomes must again match the retained #252
parent artifact.

## Operator command

Run only from clean merged `main` after the implementation PR is merged:

```powershell
python -m lisjong_arena.offensive_efficiency_diagnostic `
  --parent-artifact C:\Dev\lisjong-artifacts\issue-252-progression-development\phase-b-parent-C.json `
  --out C:\Dev\lisjong-artifacts\issue-256-offensive-efficiency\result.json `
  --workers 8
```

The output is write-once and generated artifacts are not committed to Git.
Strict readback re-derives Phase-1 aggregates, clusters, deterministic Phase-2
sample membership, and Phase-2 aggregates, and rechecks the bound parent artifact
digest and #252 population identity.
