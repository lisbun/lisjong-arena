# HandBelief S2 epoch throughput profile (#166)

This measures the current sequential CPU S2 training path. It does not optimize or change the scientific protocol. The runner uses strict readback of the retained #150 S64 corpus and #157 E80 evidence, then constructs the same S64 TRAIN view and shared VALIDATION view as #167. It never generates games or seeds and does not expose formal TEST data.

Run only from a clean committed Arena checkout with the pinned CPU environment:

```powershell
.\.venv\Scripts\python.exe -m lisjong_arena.handbelief_throughput_cli run `
  --corpus-root C:\Dev\lisjong-artifacts\issue-150-phase10 `
  --predecessor-root C:\Dev\lisjong-artifacts\issue-157-epoch-budget `
  --output C:\Dev\lisjong-artifacts\issue-166-throughput\profile.json
.\.venv\Scripts\python.exe -m lisjong_arena.handbelief_throughput_cli verify C:\Dev\lisjong-artifacts\issue-166-throughput\profile.json
```

Create the output directory before running. The destination file must not exist. Exact retained identity and 64/16 hanchan membership are mandatory; there is no substitute population. The result records current Arena HEAD, installed lisjong and engine VCS revisions, Python/PyTorch/RiichiEnv versions, OS, CPU, Torch threads, deterministic-algorithm setting, free-threaded status, and retained identities. The versioned JSON has exact field validation, finite non-negative timing and count checks, derived-summary checks, a SHA-256 logical identity, canonical readback, and no executable action field.

Each invocation performs one unprofiled warm-up epoch and three reference/profiled pairs. Every epoch starts from the same seed-0 model, Adam state, and dataloader generator state; no warm-up update flows into a measured epoch. Each pair verifies identical TRAIN MSE, VALIDATION MAE, and resulting model tensors. The profiled member differs only by timer calls. Its ratio to the unprofiled reference is the observed instrumentation overhead ratio. Report the median and range across all three pairs; if overhead is material, treat profiled absolute seconds as instrumented cost rather than production runtime.

The measured unit is epoch 1 of the S2/E160-family path. The E160 `max_epochs` cap does not affect the first epoch's model initialization, ordering, objective, optimizer update, self-rollout, or checkpoint branch. This does not measure later epochs' cache or allocator effects; it is an equivalent representative epoch, not a claim about every E160 epoch.

All component intervals are **exclusive and non-overlapping**. `train_tensor` includes initial/recurrent input and target tensor materialization; `train_forward_constraint` includes the S2 recurrent/head forward and physical allocation constraint together; `train_backward` covers loss backward; `train_optimizer` covers the single Adam step. Loss construction, gradient checks, and TRAIN loop bookkeeping are in `train_other`. `validation_tensor` includes recurrent input materialization and prior-belief value conversion; `validation_forward_constraint` is the S2 forward plus physical constraint; `validation_remap` covers prediction conversion, recurrent row assignment, and canonical reference remap; `validation_metrics` covers expected-count metric construction. Model mode changes, loop bookkeeping, and rollout setup are in `validation_other`. Epoch order construction and checkpoint-copy bookkeeping are in `epoch_other`.

The TRAIN, VALIDATION, and epoch totals are enclosing intervals, **not additional components**. The sum of all leaf component wall times equals the profiled epoch wall time, subject only to floating-point rounding. Each `share_of_epoch_wall_time` divides one leaf's wall time by the profiled epoch wall time; summing shares does not double count. The `largest_component`, `second_largest_component`, and top-two share are derived from measured leaf intervals. CPU time is process CPU time, so concurrent native threads can make CPU seconds exceed wall seconds. The result is a throughput observation for this CPU path, not a HandBelief quality result or an automatic recommendation for a future architecture/GPU path.
