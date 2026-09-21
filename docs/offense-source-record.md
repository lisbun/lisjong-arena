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

Schema identity:

```text
arena-offense-o0-player-safe-source-record-v1
```

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
- the exact qualification `binding` used by that lock (Arena revision,
  installed lisjong/lisjong-engine identities, teacher identity, feature and
  vocabulary fingerprints, and runtime identities when present);
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
