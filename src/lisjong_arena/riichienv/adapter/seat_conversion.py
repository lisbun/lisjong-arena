"""RiichiEnv player indexからlisjong `Seat`への薄い値変換。"""

from lisjong.policy_contract.seat import Seat


def seat_from_player_index(player_index: int) -> Seat:
    """RiichiEnvのplayer index(0..3)をlisjongの`Seat`へ変換する。"""
    if type(player_index) is not int:
        raise TypeError("player_index must be an int")
    try:
        return Seat(player_index)
    except ValueError:
        raise ValueError(
            f"player_index must be between 0 and 3, got {player_index}"
        ) from None
