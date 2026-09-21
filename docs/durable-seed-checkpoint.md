# Durable per-seed execution checkpoint

Issue #339 adds a durability/audit primitive for a long-run population
executor: `lisjong_arena.durable_seed_checkpoint`. It is the durability layer
under the Issue #326/#328 96-hanchan wait-shape pilot
(`lisjong_arena.wait_shape_qualification.pilot`), the concrete entrypoint this
Issue targets. `run_pilot` always durably checkpoints each seed under
`<output-root>/checkpoints/`.

## Boundary: preservation, not resume

A durable checkpoint is an audit/recovery primitive, not a scientific
checkpoint in the training/resume sense. Publishing a seed's checkpoint:

- **does** let a completed seed's artifact and completion receipt survive an
  interrupted run, so post-mortem audit can see exactly which seeds finished
  before a crash or fail-safe boundary;
- **does not** authorize resuming a run, selectively rerunning seeds,
  aggregating a partial result, or reusing checkpoints from a different run.
  `run_pilot` never reads its own checkpoint directory back to skip seeds; it
  only durably publishes as it goes, then verifies the finished, in-memory
  result against the durable set before deriving F1/F2.

Recovery tooling that reads checkpoints after an interrupted run is Issue
#341's read-only scope, not this Issue's.

## What gets published per seed

As soon as one seed finishes (in canonical `PILOT_SEEDS` order for a
single-worker run, in worker-completion order otherwise), two files are
durably published under the run's `checkpoints/` directory, in order:

1. `seed-<seed>.artifact.json` — the seed's game receipt and support
   observations (the same payload `_run_seed` returns), so it is durable
   before any receipt can point at it.
2. `seed-<seed>.receipt.json` — an immutable completion receipt binding
   `run_id` (the execution lock's `lock_identity`), `seed`,
   `protocol_identity` (the execution lock's `protocol_lock_identity`), the
   artifact's relative path, size, and SHA-256, and `started_at`/`completed_at`
   timestamps. It never serializes a factory, callable, or arbitrary code —
   only JSON-safe scalars.

Both writes are fsynced and use a same-directory temp file plus `os.link`
(never `os.replace`), so a destination that already exists fails closed
instead of being silently overwritten, and the checkpoint directory itself is
fsynced after each publish. A duplicate seed, a pre-existing destination, or a
write/sync failure raises `DurableSeedCheckpointError` and leaves any
already-durable receipts untouched.

## Final verification before scientific aggregation

Before `run_pilot` derives F1/F2/qualification from the in-memory collected
observations, it calls `verify_seed_checkpoint_set` against the durable
`checkpoints/` directory with the full locked `PILOT_SEEDS` set. This fails
closed if any seed's receipt is missing, if the directory contains an
unexpected/extra receipt, or if a receipt's bound hash/size no longer matches
its artifact on disk. Canonical order for the final raw manifest still comes
from `PILOT_SEEDS`, not from checkpoint or worker completion order.

## Operator/progress boundary

Checkpoint receipts and artifacts live in their own namespace, separate from
`aws_execution_observability`'s `operational/progress.json` (issue #329).
Neither carries scientific payload: a completion receipt binds only identity,
path, size, hash, and timestamps — never score, rank, F1/F2, or support
counts.
