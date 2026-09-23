"""Arena-owned focal exploration token derivation（#359、#79 A3）。

```text
token = lowercase hex SHA-256( canonical JSON of exactly
  {"focal_decision_ordinal": <int>, "focal_seat": <0..3>,
   "game_seed": <int>, "identity": EXPLORATION_TOKEN_IDENTITY} )
```

canonical JSONのencoding規則はここで固定する。

- keyは上の4つだけで、Unicode code point順（``sort_keys``）
- 区切り文字は``,``と``:``で、空白・末尾改行を含まない
- 整数は符号付き10進表記（先頭0なし）。``bool``は整数として受け付けない
- ASCIIだけで表現し、UTF-8 bytesのSHA-256をlowercase hexで返す

tokenは1 hanchanの(seed, focal seat)と、そのhanchan内でfocal seatのPolicyへ
渡された``DecisionContext``の0-based ordinalだけから決まる。他seatのdecision数や
timing、ambient randomnessには依存しない。lisjongはtokenを再計算せず、
selector（``select_residual_exploration``）へ渡された値として照合するだけである。
"""

import hashlib
import json

EXPLORATION_TOKEN_IDENTITY = "lisjong-arena-l0.3-focal-decision-token-sha256-v1"


def exploration_token_payload(
    *, game_seed: int, focal_seat: int, focal_decision_ordinal: int
) -> str:
    """token digestの入力となるcanonical JSON textを返す。"""
    for name, value in (
        ("game_seed", game_seed),
        ("focal_seat", focal_seat),
        ("focal_decision_ordinal", focal_decision_ordinal),
    ):
        if type(value) is not int or value < 0:
            raise ValueError(f"{name} must be a non-negative int")
    if focal_seat > 3:
        raise ValueError("focal_seat must be between 0 and 3")
    return json.dumps(
        {
            "focal_decision_ordinal": focal_decision_ordinal,
            "focal_seat": focal_seat,
            "game_seed": game_seed,
            "identity": EXPLORATION_TOKEN_IDENTITY,
        },
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def exploration_token(
    *, game_seed: int, focal_seat: int, focal_decision_ordinal: int
) -> str:
    """1 focal decisionのexploration token（lowercase 64-hex）を返す。"""
    payload = exploration_token_payload(
        game_seed=game_seed,
        focal_seat=focal_seat,
        focal_decision_ordinal=focal_decision_ordinal,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


__all__ = [
    "EXPLORATION_TOKEN_IDENTITY",
    "exploration_token",
    "exploration_token_payload",
]
