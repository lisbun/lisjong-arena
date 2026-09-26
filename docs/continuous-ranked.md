# RiichiLab continuous ranked runner

`lisjong_arena.riichilab.continuous_ranked` is the Arena-owned orchestration layer above the one-game `run_ranked_game()` contract.

It keeps the Issue #47 behavior: each attempt uses a fresh Policy instance and a fresh ranked connection; only `TransportError` is retried with bounded backoff; protocol, Policy, Adapter, trace, and other unexpected failures fail closed.

## Bounded completed-game count

Use `--games N` to stop after exactly `N` completed hanchan:

```powershell
python -m lisjong_arena.riichilab.continuous_ranked `
  --profile lisjong-dev `
  --games 5
```

`N` must be a positive integer. A retryable transport failure does not increment the completed-game count. If the failure budget is exhausted before the requested count is reached, the process terminates non-zero.

When `--games` is omitted, the existing unbounded-until-stop behavior is preserved.

## Graceful duration bound

Use `--duration-seconds N` to stop starting new hanchan after `N` seconds of monotonic elapsed runtime:

```bash
python -m lisjong_arena.riichilab.continuous_ranked \
  --profile lisjong-dev \
  --duration-seconds 43200 \
  --record-dir /path/to/ranked-records
```

`N` must be a positive integer. The duration starts when the continuous runner begins. The cutoff is checked only at safe orchestration boundaries: an in-progress hanchan is **not** cancelled. If the cutoff arrives while a hanchan is running, that hanchan finishes normally; with `--record-dir`, its durable record is finalized and strict-read before the runner stops. Therefore actual process runtime can exceed the requested duration by roughly the remainder of one hanchan plus finalization time.

After the cutoff, the runner does not create a fresh Policy, open a new ranked connection, start a new hanchan, or begin a retry. If the cutoff falls during retry backoff, the wait is capped at the remaining duration rather than sleeping the full backoff interval.

A duration stop reports:

```text
stopped reason: duration_reached
```

and exits normally.

`--games` and `--duration-seconds` may be combined. The first observed bound stops the runner; if both are satisfied at the same safe boundary, the existing completed-game target check keeps precedence.

When `--duration-seconds` is omitted, the existing unbounded-until-stop behavior is preserved. This option is a graceful orchestration bound, not a process-kill timeout and not an absolute-time scheduler.

## Per-hanchan durable records

Use `--record-dir` to acquire each completed hanchan through the existing Issue #168 durable ranked game record contract:

```powershell
python -m lisjong_arena.riichilab.continuous_ranked `
  --profile lisjong-dev `
  --games 5 `
  --record-dir C:\Dev\lisjong-artifacts\riichilab
```

The composition is intentionally simple:

```text
continuous runner
  -> fresh Policy / fresh connection
  -> one Issue #168 acquisition
  -> finalization + strict readback
  -> completed count +1
  -> fresh destination for the next game
```

One completed hanchan produces one independent durable record directory. Existing record bytes are never appended to or silently overwritten. A game is counted as completed only after record finalization and strict readback succeed.

A `TransportError` raised while the one-game acquisition is in progress retains the Issue #47 retry behavior and does not count toward `--games`. Record serialization, finalization, filesystem, or strict-readback failures are not converted into transport retries; they fail closed.

## Trace interaction

A durable record already owns its per-game protocol trace. To avoid ambiguous double-recording semantics, continuous ranked does not allow `--record-dir` together with any diagnostic trace source:

```text
--trace
--trace-path
RIICHILAB_TRACE_PATH
```

The conflict is rejected before ranked execution starts.

## Live presentation (opt-in, Issue #381)

`run_continuous_ranked(..., presentation=feed)` accepts a
`ContinuousRankedPresentationFeed`. For every one-game attempt, including a
`TransportError` retry, the runner opens a fresh
`BoundedRankedPresentationBuffer` from the feed and passes it only to that
attempt's `run_ranked_game()` / durable acquisition. A buffer is never reused
across games, so the one-run terminal contract of the buffer is preserved.

```text
continuous runner
  -> feed.open_game()            (new buffer, game_ordinal + 1)
  -> one-game primitive(presentation=buffer)
       -> decision / completion / failure facts
  -> primitive returns           (terminal fact already published)
  -> next feed.open_game()
```

The feed keeps only the latest game handle. A consumer that notices a new
`game_ordinal` can drain the buffer it already holds one last time to receive
the previous game's terminal fact. `detach()` makes the current and all later
buffers no-op publishers and never stops the run. Without `presentation` the
one-game primitive receives exactly the same arguments as before.

`run_continuous_ranked_cli(argv, *, presentation=None, stop_requested=None)`
is the module CLI as a public function, so a same-process presentation consumer
(for example the lisjong-play HTML live viewer) reuses the Arena profile /
credential resolution, output, and exit code instead of duplicating them. The
summary lines stay in the format that `aws_run_verify` parses.

## Manual smoke after merge

The first live smoke for Issue #232 is intentionally small:

```powershell
python -m lisjong_arena.riichilab.continuous_ranked `
  --profile lisjong-dev `
  --games 2 `
  --record-dir C:\Dev\lisjong-artifacts\riichilab
```

Confirm that exactly two hanchan complete, exactly two independent durable record directories are published and strict-loadable, and the runner stops without a third requeue. This is an orchestration/acquisition smoke, not Policy-strength evidence.
