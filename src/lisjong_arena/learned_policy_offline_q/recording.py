"""Offline Q teacher execution recording seam (fresh seed population).

`lisjong_arena.learned_policy_stage2.recording`が確立したLocalGameRunner実行
境界とteacher population（`yakuhai-call x4`）をそのまま再利用し、
`lisbun/lisjong-arena #140`がlockしたfresh seed range（245..305）だけを
このmoduleのsplit解決へ結び付ける。Stage 2 locked recordingそのものは
変更しない。

`LocalGameRunner`、`GameTrace`、`LocalGameInspectionRecorder`は変更しない。
`DecisionTrace.analysis`は読まない。
"""

import time

from lisjong_arena.learned_policy_stage2.recording import (
    GameRecording,
    build_teacher_population,
)
from lisjong_arena.riichienv.local_game_runner import (
    LocalGameInspectionRecorder,
    LocalGameRunner,
)

from .protocol import (
    GAME_MODE,
    Split,
    require_replacement_test_seed,
    split_for_seed,
    verify_contract_identity,
)


def _run_teacher_game(seed: int, split: Split) -> GameRecording:
    """teacher x4のfixed-seed hanchanを1本実行し、inspectionごと記録する。"""
    recorder = LocalGameInspectionRecorder()
    runner = LocalGameRunner(
        build_teacher_population(),
        seed=seed,
        game_mode=GAME_MODE,
        inspection_recorder=recorder,
    )
    wall_start = time.perf_counter()
    cpu_start = time.process_time()
    result = runner.run()
    wall_clock = time.perf_counter() - wall_start
    cpu_seconds = time.process_time() - cpu_start
    return GameRecording(
        seed=seed,
        split=split,
        result=result,
        inspection=recorder.snapshot(),
        wall_clock_seconds=wall_clock,
        cpu_seconds=cpu_seconds,
    )


def record_teacher_game(seed: int) -> GameRecording:
    """locked Offline Qデータセット母集団の1 seedを、teacher x4の
    fixed-seed hanchanとして実行する。"""
    verify_contract_identity()
    return _run_teacher_game(seed, split_for_seed(seed))


def record_replacement_test_game(seed: int) -> GameRecording:
    """locked replacement TEST母集団（354..359）の1 seedを実行する。

    teacher population / game mode / execution boundaryはdataset生成と完全に
    同一であり、違うのはseed populationとその意味づけだけである。この
    populationはTEST-onlyなので、row levelのsplitは常に`Split.TEST`になる。
    """
    verify_contract_identity()
    return _run_teacher_game(require_replacement_test_seed(seed), Split.TEST)


__all__ = ["record_replacement_test_game", "record_teacher_game"]
