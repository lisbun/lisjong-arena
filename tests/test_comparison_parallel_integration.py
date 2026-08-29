"""Real RiichiEnv serial / spawned-parallel comparison coverage is consolidated.

The integrated real-boundary contract now lives in ``test_comparison_integration``
and runs the same fixed-seed plan once serially and once through
``run_comparison_parallel(max_workers=4)``. Focused process-pool spawn semantics,
worker-count forwarding, canonical ordering, and failure behavior remain covered by
``test_parallel_execution`` and ``test_comparison_parallel`` without repeating full
RiichiEnv hanchan execution here.
"""
