# Arena seed allocation ledger

Arena owns the allocation authority for populations that **lisjong-arena executes
or evaluates**, even when the scientific owner Issue lives in another repository.
This is the Arena child of `lisbun/lisjong-project#77`; it is not an
ecosystem-global integer registry.

The implementation deliberately separates two Git identities:

```text
Arena main
    code / schema / historical bootstrap
    changes only through normal reviewed PRs

seed-registry branch
    live RESERVED / COMMITTED / RETIRED authority
    updated directly by the Arena Seed Registry workflow
```

Ordinary seed allocation therefore does **not** change the Arena scientific code
revision and does not require a PR/merge.

## Canonical files and refs

`src/lisjong_arena/seed-ledger.json` on `main` is the immutable bootstrap
snapshot. CI requires that this snapshot contain no live `RESERVED` or
`COMMITTED` rows.

The same path on `refs/heads/seed-registry` is the live authority. The first
mutating registry workflow creates that branch from the merged bootstrap when
the branch does not yet exist. Later mutations change only the ledger file on
that branch.

A local checkout, feature branch, or locally edited ledger is never allocation
authority.

## Seed domains

Collision authority is scoped by `seed_domain`. Equal integers in different
explicit domains do not collide merely because the numeric value is equal.

Current domains are:

- `riichienv-4p-red-half-hanchan-v1`
- `riichienv-4p-red-single-v1`
- `arena-legacy-declared-v1`

The legacy domain is bootstrap-only. It quarantines historical allocations whose
original producer/environment boundaries cannot be reconstructed safely. A
legacy-quarantined integer conservatively blocks future reuse in every Arena
domain. New allocations cannot use the legacy domain.

Purpose-specific protocols may impose stricter exclusions than domain collision.
Those scientific rules remain protocol-owned.

## Allocation lifecycle

Each allocation has a stable `allocation_identity`, deterministic
`seed_membership_identity`, ownership/protocol metadata and one of:

- `RESERVED` — assigned and unavailable for any other allocation;
- `COMMITTED` — retained/adopted execution evidence exists;
- `RETIRED` — permanently consumed / never reuse.

Allowed transitions are:

```text
RESERVED -> COMMITTED
RESERVED -> RETIRED
COMMITTED -> RETIRED
```

There is no FREE transition. A failed, incomplete, invalid, or abandoned run
does not return seeds to the pool.

The record's `arena_revision` is the exact reviewed Arena code revision the
allocation was authored for. It is independent of the Git commit on the
`seed-registry` branch.

## Allocation binding semantics

A protocol binding contains:

```text
owner_repository
allocation_identity
seed_domain
seed_membership_identity
ledger_revision
```

`ledger_revision` is the SHA-256 of the canonical live-ledger snapshot that
authorized the binding. It is retained for audit; it is **not** required to equal
the current global ledger revision forever.

When a retained binding is checked against current live authority, validation
requires:

- the same immutable `allocation_identity`;
- exact seed domain and membership identity;
- exact expected owner/protocol/population/split when requested; and
- current state `RESERVED` or `COMMITTED`.

Therefore unrelated reservations and `RESERVED -> COMMITTED` transitions do
not invalidate a scientific lock. A transition to `RETIRED` does make the
allocation unusable for a new execution. Historical artifact readback does not
retroactively consult a newer ledger.

## Normal operation — no PR required

Use the **Arena Seed Registry** GitHub Actions workflow for live operations.

Read-only operations:

```text
validate
list
show
check
```

Mutating operations:

```text
reserve
commit
retire
```

For example, after the #346 implementation itself has merged:

```text
gh workflow run seed-registry.yml \
  -f operation=reserve \
  -f owner_issue=lisbun/lisjong-arena#332 \
  -f protocol=offense-foundation-v1 \
  -f seed_domain=riichienv-4p-red-half-hanchan-v1 \
  -f purpose="offense foundation P2 qualification" \
  -f population=offense-foundation \
  -f split=QUALIFICATION \
  -f seeds=70000..70019 \
  -f arena_revision=<exact-reviewed-main-sha> \
  -f protocol_revision=<stable-protocol-revision> \
  -f provenance_reference=<issue-comment-or-protocol-reference>
```

The workflow verifies that `arena_revision` is a commit already merged into
Arena `main`. It validates the current `main` bootstrap first, then requires
the live authority to contain that bootstrap without deletion, immutable-field
changes, state regression, or newly introduced collision. It then performs the
normal fail-closed collision/schema checks, writes the new canonical ledger
atomically, and pushes a normal fast-forward commit to
`refs/heads/seed-registry`.

If a reviewed future PR changes the bootstrap/schema on `main`, live registry
operations fail closed until the authority branch is migrated consistently.
Ordinary reserve/commit/retire operations are not a bootstrap-migration path.

The workflow has a repository-wide concurrency group. It never force-pushes.
A manual or concurrent authority update that races the workflow causes a
non-fast-forward failure rather than silently overwriting state.

The resulting Action summary contains the allocation result. Use `show` against
that allocation to obtain the binding for a protocol request.

## Local read-only inspection

To inspect live state from a checkout:

```text
git fetch --no-tags origin \
  +refs/heads/seed-registry:refs/remotes/origin/seed-registry

git show \
  origin/seed-registry:src/lisjong_arena/seed-ledger.json \
  > <temporary-ledger-path>

python -m lisjong_arena.seed_registry \
  --ledger <temporary-ledger-path> list
```

The local CLI can mutate a supplied file for testing, but a local mutation is
not authoritative. Production mutations go through the workflow.

## Concurrency / freshness authority

The workflow serializes normal registry operations and loads the current live
branch at the start of every run. Collision, duplicate membership, malformed
state, immutable-field mutation, or state regression fails closed.

The final Git push is also a concurrency boundary: it is fast-forward only. Thus
two branches or operators cannot both establish authority merely because the same
range looked locally free.

The `main` CI separately validates:

- deterministic/canonical bootstrap serialization;
- no active live rows in the bootstrap;
- bootstrap history is not silently deleted or regressed by a PR.

## #332 exact-revision boundary

The live branch removes the revision-churn trap discovered during #346.

For #332 the intended flow is:

```text
merge #346
    -> Arena code revision X

reserve P2 on seed-registry branch
    arena_revision = X
    seeds = 70000..70019
    -> Arena main remains X

generate / strict-read final P0/P1 qualification on X
    -> build P2 request from canonical allocation binding
    -> launcher fetches live seed-registry read-only
    -> local preflight validates against that ledger
    -> the exact same ledger snapshot is carried to AWS
    -> Phase A executes code revision X

after Phase A
    RESERVED -> COMMITTED or RETIRED on seed-registry
    -> Arena main still X

if P2 qualifies
    reserve TRAIN / SELECT / OFFLINE-EVAL exactly once on seed-registry
    with arena_revision = X
    -> build scientific request
    -> Phase B executes the same exact code revision X
```

Phase-B allocation is not a result-driven rescue. It is created only after the
predeclared P2 gate permits Phase B and must obey the locked fresh/non-overlap
rules. No failed or exposed population is replaced or extended.

The #332 launcher defaults to fetching `seed-registry` read-only. It materializes
that ledger without switching the Arena worktree, validates the request, and
passes the exact authority snapshot into the AWS bootstrap. The scientific
Arena checkout remains pinned to X throughout.

#339 remains separate: #346 records population allocation authority; #339 records
per-seed durable execution completion.

## Historical bootstrap

The bootstrap records known Arena-owned historical allocations, including:

- pre-ledger reused strength populations covering `0..2499`, represented by
  conservative non-overlapping legacy quarantine;
- #252 / #263 single-round populations `647..850`;
- fixed raw-corpus seeds `1000..1007`;
- historical fresh strength populations `10000..12499`,
  `20000..20099`, `20100..20199`, and `20200..22699`;
- failed/incomplete #326 half-game qualification `2000..2095`;
- #196 populations `22700..22899`;
- #211 source-pilot `23000..23099`;
- the Arena-hosted `lisbun/lisjong#161` screen `23100..23199`;
- #216 populations `35000..35199`;
- #217 Gate 2 `35200..37699`;
- #259 write-once locked downstream population `37700..37905`;
- #270 confirmation `50000..52199`;
- #297 screen `52200..52299`; and
- #281 formal Overall half-game population `60000..60099`.

Legacy history reused some integers before this contract existed. Bootstrap
preserves future non-reuse rather than inventing overlapping canonical records.

Conditional #322 scientific TRAIN/SELECT/OFFLINE-EVAL ranges `2100..2259` are
not bootstrapped as allocations: F1/F2 never qualified, generation never
started, and #322 requires a newly reviewed allocation plan.

Recent `repository_declared_allocated_seeds()` helpers delegate to common
ledger lookup while retaining purpose-specific scientific exclusion boundaries.
Older constants remain unchanged for historical reproducibility.
