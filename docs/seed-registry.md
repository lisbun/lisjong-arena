# Arena seed allocation ledger

`src/lisjong_arena/seed-ledger.json` is the canonical owner ledger for populations
that **lisjong-arena executes or evaluates**. It implements the Arena side of
`lisbun/lisjong-project#77`; it is not an ecosystem-global seed registry and it
does not own populations produced by `lisjong` itself.

## Seed domains

Collision authority is scoped by `seed_domain`. Equal integers in different
domains do not collide merely because their numeric value is equal. Current
explicit domains are:

- `riichienv-4p-red-half-hanchan-v1` — one Arena hanchan seed under the current
  RiichiEnv half-game seed semantics;
- `riichienv-4p-red-single-v1` — one Arena single-round seed block under the
  current RiichiEnv single-round semantics;
- `arena-legacy-declared-v1` — bootstrap-only historical quarantine for
  allocations whose exact overlapping producer/environment boundaries cannot
  be reconstructed safely. New allocations must not use this legacy domain.

Two explicit non-legacy domains do not collide merely because their integers
match. The legacy domain is the one conservative exception: because its finer
semantics are unknown, a legacy-quarantined integer blocks reuse in every
Arena seed domain. This is an explicit ambiguity rule, not an ecosystem-global
integer namespace.

Purpose-specific protocols may still be stricter than domain collision.
Historical scientific code that deliberately excludes every known Arena
allocation uses the common union lookup.

## Record lifecycle

Every allocation has a stable `allocation_identity`, a deterministic
`seed_membership_identity`, and one of:

- `RESERVED` — assigned before execution and unavailable to other allocations;
- `COMMITTED` — execution/evidence adopted for the allocation;
- `RETIRED` — permanently retained as consumed / never reuse.

A failed or incomplete execution does not free seeds. Transitions only advance
`RESERVED -> COMMITTED/RETIRED -> RETIRED`; deletion and regression are invalid.
Historical bootstrap entries are `RETIRED` because they must never become free.
Unknown historical revisions/timestamps are represented as `null` rather than
invented retroactively. Active allocations require an exact Arena commit,
protocol revision, and UTC allocation timestamp.

## CLI

Run from a clean Arena checkout whose `main` is current:

```text
python -m lisjong_arena.seed_registry validate-ledger
python -m lisjong_arena.seed_registry list
python -m lisjong_arena.seed_registry show <allocation-identity>
python -m lisjong_arena.seed_registry check \
  --seed-domain riichienv-4p-red-half-hanchan-v1 \
  --seeds 70000..70019
```

To reserve a population, use the exact merged-main Arena revision and a stable
protocol revision/provenance reference:

```text
python -m lisjong_arena.seed_registry reserve \
  --owner-issue lisbun/lisjong-arena#332 \
  --protocol offense-foundation-v1 \
  --seed-domain riichienv-4p-red-half-hanchan-v1 \
  --purpose "offense foundation P2 qualification" \
  --population offense-foundation \
  --split QUALIFICATION \
  --seeds 70000..70019 \
  --arena-revision <merged-main-sha> \
  --protocol-revision <protocol-identity-or-reviewed-revision> \
  --provenance-reference <issue-comment-or-lock-reference>
```

`reserve` edits the ledger but does **not** itself grant allocation authority.
The reservation becomes authoritative only after the ledger change is reviewed
and merged to current `main`. A local branch being collision-free is not
allocation authority.

After successful adoption:

```text
python -m lisjong_arena.seed_registry commit <allocation-identity>
```

Use `--state RETIRED` when a reservation must remain permanently consumed
without being treated as successful/adopted evidence.

## Concurrency and CI

The quality workflow validates canonical serialization and compares the PR
ledger against the latest fetched base branch. A stale branch that omits a
main allocation, regresses state, mutates immutable allocation fields, or adds
a same-domain overlap fails closed. This catches two branches reserving the same
population even when both were locally free when they started.

The ledger revision is the SHA-256 of canonical ledger JSON. Protocol locks bind
`owner_repository`, `allocation_identity`, `seed_domain`,
`seed_membership_identity`, and `ledger_revision`. A lock may remain readable
after a later state transition; current-ledger authority is required when the
lock is created, not retroactively during historical readback.

## Historical bootstrap

The initial ledger records known Arena-owned historical allocations, including:

- pre-ledger reused strength populations covering `0..2499`, represented as
  non-overlapping legacy-quarantine intervals around exact later records;
- #252 / #263 single-round populations `647..850`;
- fixed raw-corpus seeds `1000..1007`;
- historical fresh strength populations `10000..12499`,
  `20000..20099`, `20100..20199`, and `20200..22699` from
  `lisbun/lisjong#121`;
- the failed / incomplete #326 half-game qualification population
  `2000..2095`;
- #196 exposed single-round populations `22700..22899`;
- #211 completed source-pilot population `23000..23099`;
- the Arena-hosted `lisbun/lisjong#161` screen `23100..23199`;
- #216's consumed invalid predecessor plus valid Gate 1 successor
  `35000..35199`;
- #217 completed Gate 2 population `35200..37699`;
- #259's write-once locked downstream population `37700..37905`, retired after
  the downstream route never ran;
- #270 confirmation `50000..52199`;
- completed #297 screen `52200..52299`; and
- completed #281 formal Overall half-game population `60000..60099`.

Legacy history reused some seed integers before this ledger contract existed.
The bootstrap therefore preserves **future non-reuse** rather than inventing
multiple overlapping canonical records for those old runs.

Failed, incomplete, or locked-but-never-executed populations remain retired:
failure or a terminal route does not return an authoritative historical
allocation to FREE.

Conditional #322 scientific TRAIN/SELECT/EVAL ranges `2100..2259` are not
bootstrapped as allocations: F1/F2 never qualified, generation never started,
and #322 explicitly requires a newly reviewed allocation plan before any future
attempt.

`repository_declared_allocated_seeds()`-style recent helpers now delegate to the
common ledger lookup while retaining their purpose-specific exclusion boundary.
Older protocol-local historical constants remain unchanged for reproducibility;
they are provenance sources for bootstrap rather than being retroactively
rewritten.
