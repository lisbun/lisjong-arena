# Testing policy

The test suite uses `unittest` discovery over `tests/test*.py`. Keep a test close to the contract it protects. A filename describes its **current responsibility**; an Issue, Phase, or Stage name is appropriate when the locked experiment identity, source population, checkpoint, or protocol provenance is part of the assertion. Do not rename those files just to erase history.

| Responsibility | Placement and scope |
| --- | --- |
| Core/unit | `tests/test_<contract>.py`; validate value construction, mapping, ordering, error paths, and fail-closed behavior with small fixtures and a substituted execution boundary. Extend an existing contract module when the same public contract and fixtures are involved. |
| Integration | `tests/test_<boundary>_integration.py`; cross a real RiichiEnv, lisjong-engine, process-spawn, or external-adapter boundary only when a unit test cannot prove the behavior. Keep fixed seeds and bounded work; avoid wall-clock pass thresholds. |
| ML/Torch | `tests/test_<contract>_ml.py` where possible; test tensor/model semantics, checkpoint loading, training determinism, and strict artifact binding with Torch actually installed. Non-ML protocol assertions belong in the companion non-ML module so they also run in `quality`. |
| Protocol/research regression | Keep locked population, holdout, source/data binding, one-shot execution, artifact identity, and classification tests even after an experiment has concluded. A new module is warranted for a distinct protocol or artifact contract; extend the existing module for another assertion on the same contract. |
| Fixture/helper | `tests/_<family>_fixtures.py`; no discovered test cases. Share concrete fixture builders without creating a generic test framework. |

The current flat `tests/` layout is compatible with both CI jobs. If a future directory split is useful, preserve `unittest discover -s tests`, module isolation, fixture imports, and exact CI selection before moving files. No directory move is required for this policy.

PyTorch remains an optional Arena dependency. `quality` runs discovery without Torch and may skip explicitly Torch-gated cases. `ml-tests` installs and verifies locked CPU `torch==2.13.0+cpu`, then enumerates **every** `tests/**/test*.py` and runs each module in an isolated interpreter. Any new Torch-dependent test must be discovered there and actually run, never count as covered solely because `quality` skipped it. Every discovered test module must contain at least one `unittest` case; CI fails closed on zero cases without exceptions. Do not replace exhaustive discovery with a hand-maintained research-family list.

Zero-test `test*.py` modules are not allowed. Migration and research-provenance notes belong in ordinary documentation, not in a test module without executable cases. The [September 2026 audit](test-suite-audit.md) retains the historical explanation of the removed comparison-parallel stub and names its executable successor coverage.

Before consolidating or removing a test, identify its durable invariant, name exact successor cases, show why they are equivalent or stronger, account for historical/protocol provenance, and verify relevant CI paths. Keep the test if equivalence is uncertain. Runtime cost, age, and filename alone are insufficient evidence. Follow the [September 2026 audit](test-suite-audit.md) for the baseline inventory and measured cost.

Run `python -m ruff format --check .`, `python -m ruff check .`, focused tests when code changes, and `git diff --check` before a PR. GitHub Actions is the pre-merge authority for the full suite; execute a local full suite when a cross-cutting change or an audit specifically requires it. Record any unexecuted check and why.
