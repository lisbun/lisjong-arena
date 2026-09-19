# RiichiLab longitudinal diagnostic

## Purpose and interpretation boundary

`lisjong_arena.riichilab_longitudinal` consumes the canonical local output of
[`riichilab_self_history`](riichilab-self-history.md) and produces a reproducible,
network-free description of `lisjong-dev` ranked history.

```text
#269 history.json + games/*.jsonl.gz
    + #168/#232 durable provenance map
    + explicit legacy Policy epochs
    + optional local opponent metadata cache
        -> offline analysis
        -> summary JSON + local game/opponent CSV
```

This is observational evidence: it describes what happened in a cohort. It is
not a causal Policy-strength estimate, a controlled Policy comparison, or a
Champion decision. Policy period, time, self rating, opponent population,
matchmaking and table composition may co-vary. Grouped slices are exploratory;
an extreme post-hoc slice is not promoted to confirmation on the same cohort.

## Canonical inputs

### Self history and MJAI

`--history-root` is the completed #269 root containing `history.json` and
`games/<game_id>.jsonl.gz`. The analyzer does not call a RiichiLab endpoint,
paginate self history, download MJAI, or use `recent_games` as self history.

The #269 history strict loader owns metadata/schema validation and rejects
duplicate `game_id`. Existing `riichilab_corpus.validation.validate_mjai_log()`
owns gzip, strict JSONL and lifecycle validation. Issue #253 adds only the
self-perspective round aggregation needed by this diagnostic. A missing MJAI
file is reported and excluded from round denominators; `--require-complete-mjai`
makes any selected missing file fatal. A present but malformed file is always
fatal.

Timezone-naive RiichiLab `played_at` stays timezone-naive. Date and epoch bounds
must have the same timezone-awareness as the games they compare with; the
analyzer never assumes JST or UTC. Date filters use `[from,to)`.

### Exact durable provenance

The #168/#232 durable ranked record v1 has exact Policy/revision/profile
provenance but deliberately has no RiichiLab server `game_id`. Therefore the
analyzer never guesses a join from timestamp, directory name, score, or
operator memory. An explicit local map supplies the missing key:

```json
{
  "schema": "lisjong-arena-riichilab-durable-provenance-map",
  "schema_version": 1,
  "records": [
    {
      "game_id": "exact-server-game-id",
      "record_path": "../ranked-records/record-directory"
    }
  ]
}
```

Relative record paths are resolved from the map file. Every mapped bundle is
read through `load_ranked_game_record()` and its bound seat and final self score
must match #269 metadata. Duplicate IDs, unknown games, corrupt bundles, seat
conflicts and score conflicts fail closed. The output stores the durable record
identity and provenance values, not the local record path.

### Explicit legacy epochs

Historical games without a durable record may use a CSV fallback:

```csv
policy,from,to,note,lisjong_revision,lisjong_arena_revision,profile_identity
MechanismRiichiDefenseYakuhaiCallPolicy,2026-09-14 00:00:00,2026-09-15 00:00:00,operator-confirmed same-policy cohort,,,
```

Intervals use `[from,to)`. Overlap, `from >= to`, mixed timezone-awareness and
the reserved `<unmapped>` Policy name are rejected. Optional revision/profile
columns may be omitted entirely. A durable mapping always has priority over an
epoch. A game matched by neither remains `<unmapped>`; no Policy is inferred.

### Opponent metadata cache

The analyzer uses only historical opponent `rating_before` obtained by exact
`game_id`. Missing values stay missing. Current leaderboard rating,
interpolation and inferred values are neither accepted nor produced.

Opponent enrichment is a separate subcommand. Its candidate-universe file is a
sanitized leaderboard-derived list:

```json
{
  "schema": "lisjong-arena-riichilab-opponent-candidates",
  "schema_version": 1,
  "source": "operator snapshot description or identity",
  "bots": [
    {"bot_id": 123, "bot_name": "display-only-name"}
  ]
}
```

The exact bot row has only `bot_id` and display-only `bot_name`; a current
rating field is rejected. `--max-games` and `--max-bots` are mandatory and are
checked before requests. Each candidate Bot public endpoint is read at most
once, serially with bounded retry, timeout and pacing. Only exact selected
`game_id` participations are retained. 404, 429, network and schema failures
are reported rather than bypassed. An existing cache with the same self Bot,
selected games and candidate-universe identity is strictly read and reused
without a request.

```powershell
python -m lisjong_arena.riichilab_longitudinal enrich-opponents `
  --history-root C:\Dev\lisjong-artifacts\riichilab\lisjong-dev-history `
  --candidate-universe C:\path\opponent-candidates.json `
  --game-id GAME_A --game-id GAME_B `
  --max-games 2 --max-bots 100 `
  --output C:\Dev\lisjong-artifacts\riichilab\opponents.json
```

The cache records candidate-universe source/identity, bots queried, API
failures, selected games, complete/partial/unknown games and complete-game
coverage. Complete means exactly three opponents with three historical ratings.

## Offline analysis

```powershell
python -m lisjong_arena.riichilab_longitudinal analyze `
  --history-root C:\Dev\lisjong-artifacts\riichilab\lisjong-dev-history `
  --policy-epochs C:\path\policy-epochs.csv `
  --durable-record-map C:\path\durable-map.json `
  --opponent-cache C:\path\opponents.json `
  --from "2026-09-14 00:00:00" `
  --to "2026-09-15 00:00:00" `
  --output-dir C:\Dev\lisjong-artifacts\riichilab\analysis `
  --name september-14
```

Supported filters are date, Policy, disconnected-game exclusion, complete
opponents, self rating, opponent average rating and self-minus-opponent rating
gap. Numeric ranges use `[minimum,maximum)`. Opponent/rating-gap bounds exclude
games whose required historical opponent value is missing.

Outputs are required to live outside a Git worktree and existing outputs are
not overwritten:

```text
<name>-summary.json
<name>-games.csv
<name>-opponents.csv
```

The JSON is the versioned aggregate contract. CSV files are local diagnostic
tables and must not be committed. Raw MJAI, third-party state, credentials,
tokens, Authorization material, absolute durable-record paths and leaderboard
current ratings are not copied into any output.

## Metrics and uncertainty

Per-hanchan output retains rank, score, self rating before/after/delta,
provenance, opponent coverage/average/range/gap and MJAI coverage. Round
aggregation includes wins (tsumo/ron), deal-in rounds/loss, riichi, open calls,
chi/pon/kan, draws, opponent tsumo, dealer diagnostics and terminal delta.

For a discard with multiple `hora` events, deal-in rounds increase once while
all applicable loss deltas accumulate. Riichi/open state resets at every
`start_kyoku`. Ankan counts as a kan but does not make an open-call round.

Average rank, rank rates, average final score, win/deal-in/riichi/open-call
rates report estimate, numerator, denominator, hanchan count and a deterministic
95% whole-hanchan cluster percentile bootstrap interval:

```text
replicates  2,000
seed        253
percentiles 2.5 / 97.5 (nearest-rank)
unit        whole hanchan, retaining all its rounds
```

Round count is a denominator, not an independent sample size. Small cohorts
remain visible through hanchan/round/event counts and typically wide intervals.
Metrics with fewer than 30 eligible hanchan carry a `few_hanchan_clusters`
warning. This fixed v1 flag is descriptive, not a universal sufficiency gate or
an exclusion rule.
The bootstrap describes sampling variation only; it does not repair selection,
missingness or opponent-strength confounding.

Grouped diagnostics are emitted by Policy, 100-point self-rating band,
100-point opponent-average-rating band and fixed rating-gap band. Every group
contains sample size, round denominator and opponent coverage. All grouped
slices are labelled exploratory. Multi-Policy output is not ranked by raw
average rank.
