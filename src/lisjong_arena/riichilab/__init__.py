"""Arena-owned entry points for external RiichiLab execution (Issue #15)。

concrete environment namespaceとしてRiichiLab固有のfirst-party entry point
をここへ置く。generic runtime hierarchyや汎用backend abstractionはここへ
持ち込まない。

``python -m lisjong_arena.riichilab.ranked``実行時に不要な``RuntimeWarning``
を出さないため、submodule(``ranked``)をこのpackage-level importで事前import
しない。
"""
