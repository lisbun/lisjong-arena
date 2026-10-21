"""第2層: 公開event fact（#513、仕様§10.6）。

出所は``run_hanchan(on_round_evidence_complete=...)``が局ごとに1回渡す
``RoundEvidenceCompletion``だけである。``on_transaction_observation``
（全席の手牌を含むprivileged observation）は使わない。

```text
RoundEvidenceCompletion（局ごと、4 viewer）
    -> viewerごとに公開factだけを射影
         MeldCalled / KanDeclared / KanConfirmed
         RiichiDeclared / RiichiEstablished / RiichiFailed
    -> 4 viewerの列が順序を含めて一致することを確認
    -> 第1層の局へ (prevailing_wind, hand_number, honba) と発生順で一意に対応
    -> event fact row（1半荘1行）
    -> validate_event_fact_row()      ← 読込時も同じ関数
```

- 保存しない: ``RoundEvidence``全体、viewer自身のツモ牌などviewer-private
  evidence、打牌列、ドラ表示牌、Policy analysis
- 立直宣言の「自席の打牌数」は、同じviewerの公開``DiscardEvidence``から
  宣言牌を含めて数える（打牌そのものは保存しない）
- 槓factの実取得は#472で未確認（現Champion系列が槓を選ばない）。合成factや
  engine直結のselectorでの試験は、candidate経由の槓適用の確認にはならない
"""

from collections.abc import Sequence

from lisjong_engine.public_state import PublicMeld, PublicTile
from lisjong_engine.round_evidence import (
    DiscardEvidence,
    KanConfirmedEvidence,
    KanDeclaredEvidence,
    MeldCalledEvidence,
    RiichiDeclaredEvidence,
    RiichiEstablishedEvidence,
    RiichiFailedEvidence,
)
from lisjong_engine.round_evidence_completion import RoundEvidenceCompletion

from lisjong_arena.lisjong_engine.completed_match_facts import seat_index

from .errors import EventFactError
from .kyoku_record import SEAT_COUNT, roundtrip

EVENT_FACT_SCHEMA = "arena-heuristic-candidate-engine-aabb-event-fact-v1"

FACT_TYPES = (
    "meld_called",
    "kan_declared",
    "kan_confirmed",
    "riichi_declared",
    "riichi_established",
    "riichi_failed",
)
MELD_CALLED_TYPES = ("chi", "pon", "daiminkan")
KAN_DECLARED_TYPES = ("ankan", "kakan")
_MELD_TILE_COUNT = {"chi": 3, "pon": 3, "daiminkan": 4, "ankan": 4, "kakan": 4}
_RED_TILE_TYPES = frozenset({4, 13, 22})
"""赤牌になり得る牌種ID（5m / 5p / 5s）。"""

_ROW_FIELDS = frozenset({"kyokus", "placement", "seed"})
_KYOKU_FIELDS = frozenset(
    {"dealer_seat", "facts", "hand_number", "honba", "kyoku_index", "prevailing_wind"}
)
_FACT_FIELDS = {
    "meld_called": frozenset({"called_discard_order", "meld", "seat", "type"}),
    "kan_declared": frozenset({"meld", "seat", "type"}),
    "kan_confirmed": frozenset({"meld", "seat", "type"}),
    "riichi_declared": frozenset(
        {"declaration_discard_order", "seat", "self_discard_count", "type"}
    ),
    "riichi_established": frozenset({"seat", "type"}),
    "riichi_failed": frozenset({"seat", "type"}),
}
_MELD_FIELDS = frozenset({"called_tile", "from_seat", "meld_type", "tiles"})
_TILE_FIELDS = frozenset({"is_red", "tile_type"})


# ---------------------------------------------------------------------------
# projection（作成時のみ）
# ---------------------------------------------------------------------------


def _tile(tile: PublicTile) -> dict[str, object]:
    return {"is_red": tile.is_red, "tile_type": tile.tile_type.id}


def _meld(meld: PublicMeld) -> dict[str, object]:
    return {
        "called_tile": None if meld.called_tile is None else _tile(meld.called_tile),
        "from_seat": None if meld.from_seat is None else seat_index(meld.from_seat),
        "meld_type": meld.meld_type.value,
        "tiles": [_tile(tile) for tile in meld.tiles],
    }


def _public_facts(evidence: Sequence[object], context: str) -> list[dict]:
    """1 viewerのevidence列から、§10.6の公開factだけを順序どおり取り出す。"""
    discards = [
        (seat_index(item.seat), item.order, item.is_riichi_declaration)
        for item in evidence
        if isinstance(item, DiscardEvidence)
    ]
    rows: list[dict] = []
    for item in evidence:
        if isinstance(item, MeldCalledEvidence):
            rows.append(
                {
                    "called_discard_order": item.called_discard_order,
                    "meld": _meld(item.meld),
                    "seat": seat_index(item.seat),
                    "type": "meld_called",
                }
            )
        elif isinstance(item, KanDeclaredEvidence):
            rows.append(
                {
                    "meld": _meld(item.meld),
                    "seat": seat_index(item.seat),
                    "type": "kan_declared",
                }
            )
        elif isinstance(item, KanConfirmedEvidence):
            rows.append(
                {
                    "meld": _meld(item.meld),
                    "seat": seat_index(item.seat),
                    "type": "kan_confirmed",
                }
            )
        elif isinstance(item, RiichiDeclaredEvidence):
            seat = seat_index(item.seat)
            order = item.declaration_discard_order
            if (seat, order, True) not in discards:
                raise EventFactError(
                    f"{context} riichi declaration has no matching public discard"
                )
            rows.append(
                {
                    "declaration_discard_order": order,
                    "seat": seat,
                    "self_discard_count": sum(
                        1
                        for discard_seat, discard_order, _ in discards
                        if discard_seat == seat and discard_order <= order
                    ),
                    "type": "riichi_declared",
                }
            )
        elif isinstance(item, RiichiEstablishedEvidence):
            rows.append({"seat": seat_index(item.seat), "type": "riichi_established"})
        elif isinstance(item, RiichiFailedEvidence):
            rows.append({"seat": seat_index(item.seat), "type": "riichi_failed"})
    return rows


def project_event_facts(completions: object, hanchan_row: dict) -> dict[str, object]:
    """1半荘分の``RoundEvidenceCompletion``列を第2層rowへ射影し、検査する。

    ``hanchan_row``は検査済みの第1層rowである。completionの件数が局数と違う、
    局へ0件・複数件対応する、発生順と合わない、viewer間で公開factが違う場合は
    ``EventFactError``でfail closedする。
    """
    seed = hanchan_row["seed"]
    placement = hanchan_row["placement"]
    context = f"seed {seed} placement {placement} event facts"
    if completions is None:
        raise EventFactError(f"{context}: round evidence was not captured")
    if isinstance(completions, (str, bytes)) or not isinstance(completions, Sequence):
        raise EventFactError(f"{context}: completions must be a sequence")
    kyokus = hanchan_row["kyokus"]
    if len(completions) != len(kyokus):
        raise EventFactError(
            f"{context}: {len(completions)} round evidence completions for "
            f"{len(kyokus)} kyoku"
        )
    rows = []
    for index, completion in enumerate(completions):
        kyoku_context = f"{context} kyoku {index}"
        if not isinstance(completion, RoundEvidenceCompletion):
            raise EventFactError(f"{kyoku_context} is not a RoundEvidenceCompletion")
        identity = (
            completion.prevailing_wind.value,
            completion.hand_number,
            completion.honba,
        )
        matches = [
            position
            for position, kyoku in enumerate(kyokus)
            if (kyoku["prevailing_wind"], kyoku["hand_number"], kyoku["honba"])
            == identity
        ]
        if len(matches) != 1:
            raise EventFactError(
                f"{kyoku_context} matches {len(matches)} kyoku by round identity"
            )
        if matches[0] != index:
            raise EventFactError(f"{kyoku_context} is out of kyoku order")
        if seat_index(completion.dealer_seat) != kyokus[index]["dealer_seat"]:
            raise EventFactError(f"{kyoku_context} dealer seat contradicts its kyoku")
        projections = completion.projections
        if [seat_index(item.viewer_seat) for item in projections] != list(
            range(SEAT_COUNT)
        ):
            raise EventFactError(f"{kyoku_context} must have viewers 0..3 in order")
        per_viewer = [
            _public_facts(item.evidence, f"{kyoku_context} viewer {viewer}")
            for viewer, item in enumerate(projections)
        ]
        if any(item != per_viewer[0] for item in per_viewer[1:]):
            raise EventFactError(f"{kyoku_context} public facts differ between viewers")
        kyoku = kyokus[index]
        rows.append(
            {
                "dealer_seat": kyoku["dealer_seat"],
                "facts": per_viewer[0],
                "hand_number": kyoku["hand_number"],
                "honba": kyoku["honba"],
                "kyoku_index": index,
                "prevailing_wind": kyoku["prevailing_wind"],
            }
        )
    row = {"kyokus": rows, "placement": placement, "seed": seed}
    return validate_event_fact_row(roundtrip(row), hanchan_row)


# ---------------------------------------------------------------------------
# validation（作成時・読込時）
# ---------------------------------------------------------------------------


def _fields(value: object, fields: frozenset[str], context: str) -> dict:
    if type(value) is not dict or set(value) != fields:
        raise EventFactError(f"{context} fields do not match the schema")
    return value


def _int(value: object, context: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise EventFactError(f"{context} must be an int >= {minimum}")
    return value


def _seat(value: object, context: str) -> int:
    if type(value) is not int or not 0 <= value < SEAT_COUNT:
        raise EventFactError(f"{context} must be a seat index 0..3")
    return value


def _tile_value(value: object, context: str) -> tuple[int, bool]:
    _fields(value, _TILE_FIELDS, context)
    tile_type = value["tile_type"]
    if type(tile_type) is not int or not 0 <= tile_type <= 33:
        raise EventFactError(f"{context}.tile_type must be 0..33")
    if type(value["is_red"]) is not bool:
        raise EventFactError(f"{context}.is_red must be a bool")
    if value["is_red"] and tile_type not in _RED_TILE_TYPES:
        raise EventFactError(f"{context} only suited fives can be red")
    return (tile_type, value["is_red"])


def _validate_meld(value: object, *, actor: int, allowed, context: str) -> dict:
    _fields(value, _MELD_FIELDS, context)
    meld_type = value["meld_type"]
    if type(meld_type) is not str or meld_type not in allowed:
        raise EventFactError(f"{context}.meld_type {meld_type!r} is not allowed here")
    if type(value["tiles"]) is not list:
        raise EventFactError(f"{context}.tiles must be a list")
    tiles = [
        _tile_value(tile, f"{context}.tiles[{index}]")
        for index, tile in enumerate(value["tiles"])
    ]
    if len(tiles) != _MELD_TILE_COUNT[meld_type] or tiles != sorted(tiles):
        raise EventFactError(f"{context}.tiles has the wrong count or order")
    if meld_type == "ankan":
        if value["from_seat"] is not None or value["called_tile"] is not None:
            raise EventFactError(f"{context} ankan must not have a source")
    else:
        source = _seat(value["from_seat"], f"{context}.from_seat")
        if source == actor:
            raise EventFactError(f"{context}.from_seat must be another seat")
        if value["called_tile"] is None or (
            _tile_value(value["called_tile"], f"{context}.called_tile") not in tiles
        ):
            raise EventFactError(f"{context}.called_tile must be one of the tiles")
    return value


def _validate_facts(facts: object, *, contributions: set[int], context: str) -> None:
    if type(facts) is not list:
        raise EventFactError(f"{context}.facts must be a list")
    pending: dict[int, bool] = {}
    declared: set[int] = set()
    established: set[int] = set()
    open_kans: list[tuple[int, object]] = []
    for index, fact in enumerate(facts):
        fact_context = f"{context}.facts[{index}]"
        if type(fact) is not dict or fact.get("type") not in _FACT_FIELDS:
            raise EventFactError(f"{fact_context}.type is unsupported")
        kind = fact["type"]
        _fields(fact, _FACT_FIELDS[kind], fact_context)
        seat = _seat(fact["seat"], f"{fact_context}.seat")
        if kind == "meld_called":
            _validate_meld(
                fact["meld"],
                actor=seat,
                allowed=MELD_CALLED_TYPES,
                context=fact_context,
            )
            _int(fact["called_discard_order"], f"{fact_context}.called_discard_order")
        elif kind == "kan_declared":
            _validate_meld(
                fact["meld"],
                actor=seat,
                allowed=KAN_DECLARED_TYPES,
                context=fact_context,
            )
            open_kans.append((seat, fact["meld"]))
        elif kind == "kan_confirmed":
            _validate_meld(
                fact["meld"],
                actor=seat,
                allowed=KAN_DECLARED_TYPES,
                context=fact_context,
            )
            if (seat, fact["meld"]) not in open_kans:
                raise EventFactError(f"{fact_context} confirms an undeclared kan")
            open_kans.remove((seat, fact["meld"]))
        elif kind == "riichi_declared":
            order = _int(
                fact["declaration_discard_order"],
                f"{fact_context}.declaration_discard_order",
            )
            count = _int(
                fact["self_discard_count"],
                f"{fact_context}.self_discard_count",
                minimum=1,
            )
            if count > order + 1:
                raise EventFactError(f"{fact_context}.self_discard_count is too large")
            if seat in declared:
                raise EventFactError(f"{fact_context} repeats a riichi declaration")
            declared.add(seat)
            pending[seat] = True
        else:
            if not pending.pop(seat, False):
                raise EventFactError(
                    f"{fact_context} resolves a riichi that was not declared"
                )
            if kind == "riichi_established":
                established.add(seat)
    if pending:
        raise EventFactError(f"{context} riichi declaration is never resolved")
    # 立直成立 = 第1層の riichi_contributions（供託）
    if established != contributions:
        raise EventFactError(
            f"{context} established riichi seats differ from riichi contributions"
        )


def validate_event_fact_row(row: object, hanchan_row: dict) -> dict:
    """第2層の1半荘rowを、検査済みの第1層rowに対して検査する。"""
    _fields(row, _ROW_FIELDS, "event fact row")
    seed = hanchan_row["seed"]
    placement = hanchan_row["placement"]
    context = f"seed {seed} placement {placement} event facts"
    if row["seed"] != seed or row["placement"] != placement:
        raise EventFactError(f"{context}: row is not bound to its hanchan")
    kyokus = row["kyokus"]
    if type(kyokus) is not list or len(kyokus) != len(hanchan_row["kyokus"]):
        raise EventFactError(f"{context}: kyoku count differs from the kyoku record")
    for index, (kyoku, source) in enumerate(
        zip(kyokus, hanchan_row["kyokus"], strict=True)
    ):
        kyoku_context = f"{context} kyoku {index}"
        _fields(kyoku, _KYOKU_FIELDS, kyoku_context)
        if [
            kyoku["kyoku_index"],
            kyoku["prevailing_wind"],
            kyoku["hand_number"],
            kyoku["honba"],
            kyoku["dealer_seat"],
        ] != [
            source["kyoku_index"],
            source["prevailing_wind"],
            source["hand_number"],
            source["honba"],
            source["dealer_seat"],
        ] or type(kyoku["kyoku_index"]) is not int:
            raise EventFactError(f"{kyoku_context} is not bound to its kyoku")
        _validate_facts(
            kyoku["facts"],
            contributions={item["seat"] for item in source["riichi_contributions"]},
            context=kyoku_context,
        )
    return row


__all__ = [
    "EVENT_FACT_SCHEMA",
    "FACT_TYPES",
    "KAN_DECLARED_TYPES",
    "MELD_CALLED_TYPES",
    "project_event_facts",
    "validate_event_fact_row",
]
