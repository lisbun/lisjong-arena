# Offense Foundation player-safe source record (#342)

## Purpose

The Offense Foundation generator retains a reusable, player-safe source record
beside every #331 P2 or scientific corpus. The source record exists so future
lisjong-owned Learning code can rematerialize features or other learner-specific
representations from the original teacher decisions without replaying the
expensive hanchan.

The source record is a **separate artifact**. It is not part of the locked #331
scientific corpus and does not change corpus rows, `features.f32`, legal masks,
support accounting, dataset split membership, or corpus identity.

## Schema and layout

Schema identity (current generation):

```text
arena-offense-o0-player-safe-source-record-v2
```

`v2` adds an `allocation_bindings` manifest field (below) that records the
exact Arena seed-allocation ledger provenance (lisbun/lisjong-arena#346/#347)
behind the request population. The historical
`arena-offense-o0-player-safe-source-record-v1` schema (no allocation
provenance) remains readable for existing artifacts generated before this
field existed; it is frozen and readback-only — the generator never writes it
again.

A complete artifact is:

```text
<source-record>/
  manifest.json
  game-000/
    source-record.jsonl
  game-001/
    source-record.jsonl
  ...
```

`manifest.json` is sealed canonical JSON. It binds:

- schema and artifact kind;
- exact #331 protocol-lock identity and game mode;
- exact scientific corpus identity produced by the same generation run;
- the exact qualification `binding` used by that lock (Arena revision,
  installed lisjong/lisjong-engine identities, teacher identity, feature and
  vocabulary fingerprints, and runtime identities when present);
- (`v2` only) `allocation_bindings`: one seed-allocation binding per
  population split (`QUALIFICATION`, or `TRAIN`/`SELECT`/`OFFLINE-EVAL`),
  copied verbatim from the locked `request.allocation_bindings`. Each binding
  is the exact `seed_registry.allocation_binding()` shape:
  `allocation_identity`, `ledger_revision`, `owner_repository`, `seed_domain`,
  `seed_membership_identity`;
- one sealed game summary per protocol-ordered hanchan;
- source payload byte lengths and SHA-256 digests.

Each JSONL row represents one actual teacher decision and contains exactly:

| Field | Meaning |
| --- | --- |
| `game_ordinal` | protocol-ordered hanchan ordinal |
| `seed` / `split` | source hanchan provenance |
| `step_ordinal` / `decision_ordinal` | stable execution position |
| `actor_seat` | decision actor |
| `policy_input` | the exact lisjong `PolicyInput`-equivalent player-safe snapshot |
| `legal_actions` | typed canonical `InternalAction` values, canonical-sorted |
| `teacher_selected_action` | typed canonical action actually selected by the teacher |

The action objects use explicit lisjong action variants and semantic fields. They
are not stored as Python objects, factories, callables, pickle, `repr()`, or
generic dataclass serialization.

## Player-safe boundary

`policy_input` is serialized through the same explicit typed projection used by
Arena's durable local game record. It contains only:

- own seat and public round state;
- all players' public score/discard/meld/riichi state;
- the acting player's own concealed hand and drawn-tile metadata.

It does not contain opponent concealed hands, wall/dead-wall contents, future
events, oracle labels, shanten/ukeire/hand-value analysis, HandBelief labels,
rewards, Q targets, or encoded learner tensors.

Encoded `arena-policy-input-feature-v1` tensors are deliberately not the
canonical source representation. Future lisjong Learning code must derive its
own representation from the typed source row.

## Allocation provenance binding (`v2`)

Arena remains the sole owner and generator of the seed-allocation ledger
(lisbun/lisjong-arena#346/#347, `seed_registry.py`). The source record does
not re-derive, re-allocate, or independently authorize any seed; it only
republishes the exact per-split binding that the protocol lock's
`request.allocation_bindings` already recorded when `require_request_allocations`
authorized that population against the live ledger.

This exists so a **standalone reader of the source record alone** — with no
access to the live seed ledger, no access to the full protocol lock object,
and no re-query of current ledger state — can still see and propagate the
allocation provenance that backs the decisions it reads. Before `v2`,
`lock_identity` alone cryptographically bound the manifest to a lock that
*happened* to contain `allocation_bindings`, but a downstream consumer that
only has the source-record directory could not read those bindings out of it.

Strict readback of a `v2` manifest:

- requires `allocation_bindings` to have exactly one entry per population
  split (`set(allocation_bindings) == set(populations)`); missing or extra
  splits fail closed;
- validates each split's binding shape with `seed_registry.validate_binding_shape()`
  (SHA-256 identity fields, non-empty domain-pattern `seed_domain`, exact
  `owner_repository`, and `seed_membership_identity` matching that split's
  seed population) — this is the same shape check a standalone downstream
  reader performs, and it does not consult the live ledger;
- requires the manifest's binding to equal, field for field, the binding
  already recorded in `expected_lock["request"]["allocation_bindings"]` for
  that split (defense in depth alongside the existing `lock_identity` check).

None of this re-queries live ledger state or freshness at read time: a
historical artifact's readback never depends on later ledger churn
(reservation, commit, or retirement of unrelated allocations do not affect
an already-published source record).

## Canonical action binding

Legal actions are serialized as typed semantic actions and sorted by their
canonical JSON representation. This ordering is independent of worker completion
order and does not use action-vocabulary indices as the source identity.

Strict readback reconstructs the typed actions and PolicyInput, requires exact
round trips, rejects duplicate/noncanonical actions, and then binds every source
decision to the corresponding locked scientific `rows.jsonl` entry:

- game/seed/split, step, decision and actor must match;
- encoded source legal actions must equal the existing `legal_indices`;
- the encoded selected action must equal the existing
  `teacher_action_index`;
- missing or extra decisions fail closed.

This cross-check proves that the sidecar describes the same decisions as the
unchanged #331 artifact while keeping typed actions as the reusable source form.

## Generation and publication

The canonical generator executes each hanchan once. The same completed
`LocalGameInspection` is projected into both artifacts; the teacher is not
rerun for the sidecar.

Corpus and source record are built under one same-parent staging root. Both are
strict-read before publication. The source record is renamed into place first
and the corpus second, so an interruption cannot expose a completed scientific
corpus without its reusable source record. Existing destinations are never
overwritten.

If `--source-record-output` is omitted, the Python/CLI generator uses
`<corpus-output>-source-record`. #332 passes explicit retained paths.

```text
python -m lisjong_arena.offense_foundation generate \
  --lock <lock.json> \
  --output <corpus> \
  --source-record-output <source-record>

python -m lisjong_arena.offense_foundation source-readback \
  --lock <lock.json> \
  --corpus <corpus> \
  --source-record <source-record>
```

For a scientific lock, the existing `--p2-corpus` prerequisite remains
unchanged. The source record does not authorize seed reuse, population changes,
resume/adoption, training changes, or any reinterpretation of #331 outcomes.
