"""Small declared contract probes, never a scientific seed population.

The three hierarchy hands are the lisjong #87 regression examples. Legal sets
are boundary fixtures, not a second implementation of Mahjong legality.
"""

from dataclasses import dataclass, replace

from lisjong.policy_contract import (
    DecisionContext,
    Discard,
    MeldKind,
    OwnHandState,
    PlayerPublicState,
    PolicyInput,
    PublicMeld,
    RiichiState,
    RoundState,
    Seat,
    Tile,
    TileCategory,
    TileType,
    Wind,
)
from lisjong.policy_contract.action import (
    AnkanAction,
    ChiAction,
    DaiminkanAction,
    DiscardAction,
    KakanAction,
    PassAction,
    PonAction,
    RiichiAction,
    RonAction,
    TsumoAction,
)

S = Seat.SEAT_0
OTHER = Seat.SEAT_3


def hand(spec):
    categories = dict(zip("mpsz", TileCategory, strict=True))
    tiles, digits = [], ""
    for c in spec:
        if c.isdigit():
            digits += c
        else:
            for d in digits:
                tiles.append(Tile(TileType(categories[c], int(d) or 5), d == "0"))
            digits = ""
    if digits:
        raise ValueError("trailing fixture ranks")
    return tuple(tiles)


def policy_input(spec):
    return PolicyInput(
        self_seat=S,
        round=RoundState(Wind.EAST, 1, S, 0, 0, (), 70),
        players=tuple(PlayerPublicState(25000, (), (), RiichiState.NONE) for _ in Seat),
        own_hand=OwnHandState(hand(spec), None),
    )


def discard(spec):
    return DiscardAction(S, hand(spec)[0], False)


@dataclass(frozen=True)
class Probe:
    name: str
    context: DecisionContext
    expected: object


def probes() -> tuple[Probe, ...]:
    result = []

    def add(name, value, actions, expected):
        result.append(Probe(name, DecisionContext(value, tuple(actions)), expected))

    winning = policy_input("123m123p123s11122z")
    d, r = discard("2z"), RiichiAction(S)
    tsumo = TsumoAction(S, hand("2z")[0])
    add("tsumo_before_riichi_and_discard", winning, (d, r, tsumo), tsumo)
    ron = RonAction(S, OTHER, hand("2z")[0])
    add("ron_before_riichi_and_discard", winning, (d, r, ron), ron)
    add("riichi_before_discard", winning, (d, r), r)
    for name, spec, a, b, expected in (
        ("minimum_shanten", "234567m234567p5s7z", "4m", "7z", "7z"),
        ("maximum_current_ukeire", "123456789m11123p", "2m", "4m", "4m"),
        ("maximum_second_step", "345m56679s333577z", "9s", "5z", "5z"),
        ("stable_red_five_tie", "234055m234p567s12z", "0m", "5m", "5m"),
        ("tenpai_stable_tie", "123m123p123s11123z", "2z", "3z", "2z"),
    ):
        add(name, policy_input(spec), (discard(a), discard(b)), discard(expected))
    response = policy_input("12m456p789s11555z")
    pas = PassAction(S)
    chi = ChiAction(S, OTHER, hand("3m")[0], hand("12m"))
    pon = PonAction(S, OTHER, hand("5z")[0], hand("55z"))
    kan = DaiminkanAction(S, OTHER, hand("5z")[0], hand("555z"))
    for name, action in (("chi", chi), ("pon", pon), ("daiminkan", kan)):
        add(f"pass_before_{name}", response, (action, pas), pas)
    add("ron_before_calls", response, (pon, pas, ron), ron)
    own = policy_input("1111m234p567s1234z")
    add(
        "discard_before_ankan",
        own,
        (AnkanAction(S, hand("1111m")), discard("4z")),
        discard("4z"),
    )
    opened = policy_input("1m234p567s1234z")
    meld = PublicMeld(MeldKind.PON, hand("111m"), OTHER, hand("1m")[0])
    opened = replace(
        opened, players=(replace(opened.players[0], melds=(meld,)), *opened.players[1:])
    )
    add(
        "discard_before_kakan",
        opened,
        (KakanAction(S, hand("1m")[0], OTHER, hand("1m")[0]), discard("4z")),
        discard("4z"),
    )
    # All three remaining copies of the sole wait are visible: evaluated 0.
    zero = policy_input("123m123p123s11123z")
    visible = tuple(Discard(hand("2z")[0], False, n, None) for n in range(3))
    zero = replace(
        zero,
        players=(
            zero.players[0],
            replace(zero.players[1], discards=visible),
            *zero.players[2:],
        ),
    )
    add("evaluated_zero_current_ukeire", zero, (discard("3z"),), discard("3z"))
    return tuple(result)
