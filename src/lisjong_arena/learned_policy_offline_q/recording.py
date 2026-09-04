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

from .protocol import GAME_MODE, split_for_seed, verify_contract_identity


def record_teacher_game(seed: int) -> GameRecording:
    """locked Offline Qデータセット母集団の1 seedを、teacher x4の
    fixed-seed hanchanとして実行する。"""
    verify_contract_identity()
    split = split_for_seed(seed)
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


__all__ = ["record_teacher_game"]
