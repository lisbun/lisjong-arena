"""`test_riichilab_session_adapter_integration.py`が共有する、実RiichiEnv
Observationからserver-style`request_action`を組み立てるhelper。

lisjong側`RiichiLabSeatAdapter`のtest内部実装への依存を作らず、公式
`possible_actions` schemaのidentityでdedupeし、hora/call系へ`pai`を補う
という同じ正規化方針を独立して実装する。
"""

import json

from lisjong.riichienv_adapter.tile_conversion import (
    tile_from_physical_id,
    tile_to_mjai,
)
from riichienv import ActionType

_CALL_TYPES = {"chi", "pon", "daiminkan"}


def server_style_request_action(observation, request_id: int) -> dict:
    legal = observation.legal_actions()
    seen = set()
    possible_actions = []
    for action in legal:
        candidate = json.loads(action.to_mjai())

        if candidate["type"] in _CALL_TYPES:
            candidate["pai"] = tile_to_mjai(tile_from_physical_id(action.tile))
        elif candidate["type"] == "hora":
            candidate["pai"] = tile_to_mjai(tile_from_physical_id(action.tile))

        dedupe_key = json.dumps(candidate, sort_keys=True)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)

        candidate["actor"] = action.actor
        if candidate["type"] == "dahai":
            candidate["tsumogiri"] = action.tile == observation.drawn_tile
        elif candidate["type"] in _CALL_TYPES:
            candidate["target"] = observation.last_discard
        elif candidate["type"] == "hora":
            candidate["target"] = (
                action.actor
                if action.action_type == ActionType.TSUMO
                else observation.last_discard
            )

        possible_actions.append(candidate)

    return {
        "type": "request_action",
        "request_id": request_id,
        "possible_actions": possible_actions,
        "observation": observation.serialize_to_base64(),
    }
