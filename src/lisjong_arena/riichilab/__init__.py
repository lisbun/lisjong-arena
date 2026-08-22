"""Arena-owned entry points for external RiichiLab execution (Issues #15, #19)。

concrete environment namespaceとしてRiichiLab固有のfirst-party entry point
をここへ置く。generic runtime hierarchyや汎用backend abstractionはここへ
持ち込まない。

``ranked`` / ``validation``が使用するexecution profile / credential / CLI
compositionは``lisjong_arena.riichilab.profile`` / ``lisjong_arena.riichilab.cli``
として共有し、ranked / validationで定義を重複させない。

``python -m lisjong_arena.riichilab.ranked`` / ``python -m
lisjong_arena.riichilab.validation``実行時に不要な``RuntimeWarning``を出さ
ないため、submodule(``ranked`` / ``validation``)をこのpackage-level import
で事前importしない。
"""
