"""Arena-owned RiichiEnv execution/observation namespace (Issue #31)。

``local_game_runner``と``adapter``のcanonical physical implementationを
ここへ置く。RiichiEnv Adapterは Issue #39でArena-local canonical
implementation(``lisjong_arena.riichienv.adapter``)へmigration済みである。
GameTrace(``lisjong.game_trace``)はまだArenaへmigrationしておらず、この
packageは一時的にlisjongへdependする。
"""
