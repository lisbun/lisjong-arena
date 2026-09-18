"""Stage A0 privileged annotation sidecar: 最小contract、write、strict readback。

1 sidecar = 1 immutable directoryとし、既存pathを上書きしない。

```text
<sidecar>/
    manifest.json   canonical JSON identity / provenance / totals / digests
    cells.jsonl     1行 = 1 opponent-target cell
```

sidecarは#258 Stage A0 Tenpai（および後続A1 Waitのre-use）に必要な最小限
だけを持つ。universal oracle snapshotは作らない。次はStage A0 sidecar
contractの外であり、意図的に保持しない。

```text
wall order / future action / future draw / final result
Oracle Guiding用のfull hidden state
furiten reconstruction state
Ron counterfactual branch state
future reward
```

raw sidecar（concealed handを含む）はGitへcommitしない。Gitへ置くのは
schema / code / tests / aggregate report / digestsだけである。
"""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from shutil import rmtree
from tempfile import mkdtemp

from lisjong.policy_contract import Seat, Wind
from lisjong.policy_contract.meld import MeldKind, PublicMeld
from lisjong.policy_contract.riichi import RiichiState

from lisjong_arena._artifact_io import canonical_json_text
from lisjong_arena.riichienv.adapter.tile_conversion import tile_from_mjai, tile_to_mjai

from .errors import StageA0SidecarError
from .labels import (
    OpponentIdentity,
    OpponentTenpaiTarget,
    TargetAvailability,
    build_opponent_target,
    opponent_identity,
)
from .protocol import (
    CANONICAL_WAIT_BUILDER,
    CANONICAL_WAIT_ENTRY_POINT,
    GAME_MODE,
    ISSUE_IDENTITY,
    PROTOCOL_ID,
    TEACHER_IDENTITY,
    TEACHER_POLICY_CLASS,
    TEACHER_POPULATION,
    TEACHER_SOURCE_REVISION,
    TILE_KIND_COUNT,
    exact_wait_implementation_identity,
)
from .public_row import DecisionRowIdentity

SIDECAR_SCHEMA_VERSION = "arena-stage-a0-tenpai-sidecar-v1"
MANIFEST_FILENAME = "manifest.json"
CELLS_FILENAME = "cells.jsonl"

ROUTE_RETAINED = "retained-augmentation"
ROUTE_FRESH = "fresh-live-label"
ROUTES = (ROUTE_RETAINED, ROUTE_FRESH)

_CELL_FIELDS = {
    "source_identity",
    "seed",
    "step_ordinal",
    "decision_ordinal",
    "actor_seat",
    "public_row_digest",
    "relative_offset",
    "opponent_seat",
    "opponent_wind",
    "dealer_seat",
    "concealed_tiles",
    "melds",
    "public_riichi_state",
    "privileged_riichi_declared",
    "availability",
    "tenpai",
    "wait_mask",
}
_MELD_FIELDS = {"kind", "tiles", "from_seat", "called_tile"}
_MANIFEST_FIELDS = {
    "sidecar_schema_version",
    "sidecar_identity",
    "protocol",
    "canonical_wait_implementation",
    "provenance",
    "totals",
    "files",
}
_PROTOCOL_FIELDS = {
    "protocol_id",
    "issue",
    "route",
    "source_identity",
    "game_mode",
    "teacher_identity",
    "teacher_policy_class",
    "teacher_population",
    "teacher_source_revision",
}
_CANONICAL_FIELDS = {"builder", "entry_point", "implementation_identity"}
_TOTALS_FIELDS = {"row_count", "cell_count", "availability_counts"}
_FILE_FIELDS = {"bytes", "sha256"}


def _error(message: str) -> StageA0SidecarError:
    return StageA0SidecarError(message)


def _expect(value: object, expected: type, context: str):
    if type(value) is not expected:
        raise _error(f"{context} must be a {expected.__name__}")
    return value


def _expect_object(value: object, fields: set[str], context: str) -> dict:
    if type(value) is not dict:
        raise _error(f"{context} must be an object")
    if set(value) != fields:
        raise _error(f"{context} fields are invalid")
    return value


def _digest_text(value: object, context: str) -> str:
    text = _expect(value, str, context)
    if len(text) != 64 or any(
        character not in "0123456789abcdef" for character in text
    ):
        raise _error(f"{context} must be a lowercase sha256 digest")
    return text


def _meld_document(meld: PublicMeld) -> dict[str, object]:
    return {
        "kind": meld.kind.value,
        "tiles": [tile_to_mjai(tile) for tile in meld.tiles],
        "from_seat": None if meld.from_seat is None else int(meld.from_seat),
        "called_tile": (
            None if meld.called_tile is None else tile_to_mjai(meld.called_tile)
        ),
    }


def _meld_from_document(document: object, context: str) -> PublicMeld:
    payload = _expect_object(document, _MELD_FIELDS, context)
    try:
        kind = MeldKind(payload["kind"])
    except ValueError:
        raise _error(f"{context}.kind is not a canonical meld kind") from None
    tiles = payload["tiles"]
    if type(tiles) is not list or any(type(item) is not str for item in tiles):
        raise _error(f"{context}.tiles must be an array of tile strings")
    from_seat = payload["from_seat"]
    if from_seat is not None and type(from_seat) is not int:
        raise _error(f"{context}.from_seat must be an int or null")
    called_tile = payload["called_tile"]
    if called_tile is not None and type(called_tile) is not str:
        raise _error(f"{context}.called_tile must be a tile string or null")
    try:
        return PublicMeld(
            kind=kind,
            tiles=tuple(tile_from_mjai(text) for text in tiles),
            from_seat=None if from_seat is None else Seat(from_seat),
            called_tile=None if called_tile is None else tile_from_mjai(called_tile),
        )
    except (TypeError, ValueError) as error:
        raise _error(f"{context} is not a valid public meld") from error


@dataclass(frozen=True, slots=True)
class PrivilegedTargetCell:
    """1 opponent-target cellのsidecar record。

    stored inputs（concealed tiles / melds / riichi binding）だけからcanonical
    implementationでlabelを再計算でき、storedなavailability / T / wait maskは
    その再計算と一致しなければならない。
    """

    row_identity: DecisionRowIdentity
    public_row_digest: str
    dealer_seat: int
    identity: OpponentIdentity
    concealed_tiles: tuple | None
    melds: tuple | None
    public_riichi_state: RiichiState
    privileged_riichi_declared: bool
    target: OpponentTenpaiTarget

    def __post_init__(self) -> None:
        if not isinstance(self.row_identity, DecisionRowIdentity):
            raise TypeError("row_identity must be a DecisionRowIdentity")
        if not isinstance(self.identity, OpponentIdentity):
            raise TypeError("identity must be an OpponentIdentity")
        if not isinstance(self.target, OpponentTenpaiTarget):
            raise TypeError("target must be an OpponentTenpaiTarget")
        if self.target.identity != self.identity:
            raise _error("target identity does not match the cell identity")
        if not 0 <= self.dealer_seat <= 3:
            raise ValueError("dealer_seat must be in 0..3")
        expected_seat = (
            self.row_identity.actor_seat + self.identity.viewer_relative_offset
        ) % 4
        if int(self.identity.seat) != expected_seat:
            raise _error(
                "the opponent canonical seat is not the actor seat rotated by "
                "the recorded relative offset"
            )
        _digest_text(self.public_row_digest, "public_row_digest")

    @property
    def availability(self) -> TargetAvailability:
        return self.target.availability

    def to_document(self) -> dict[str, object]:
        document = dict(self.row_identity.to_document())
        document.update(
            {
                "public_row_digest": self.public_row_digest,
                "dealer_seat": self.dealer_seat,
                "relative_offset": self.identity.viewer_relative_offset,
                "opponent_seat": int(self.identity.seat),
                "opponent_wind": self.identity.wind.value,
                "concealed_tiles": (
                    None
                    if self.concealed_tiles is None
                    else [tile_to_mjai(tile) for tile in self.concealed_tiles]
                ),
                "melds": (
                    None
                    if self.melds is None
                    else [_meld_document(meld) for meld in self.melds]
                ),
                "public_riichi_state": self.public_riichi_state.value,
                "privileged_riichi_declared": self.privileged_riichi_declared,
                "availability": self.target.availability.value,
                "tenpai": self.target.tenpai,
                "wait_mask": (
                    None
                    if self.target.wait_mask is None
                    else list(self.target.wait_mask)
                ),
            }
        )
        return document


def cell_from_document(document: object, context: str) -> PrivilegedTargetCell:
    """1 cell documentをstrictに検証して値へ戻す。"""
    payload = _expect_object(document, _CELL_FIELDS, context)
    row_identity = DecisionRowIdentity(
        source_identity=_expect(
            payload["source_identity"], str, f"{context}.source_identity"
        ),
        seed=_expect(payload["seed"], int, f"{context}.seed"),
        step_ordinal=_expect(payload["step_ordinal"], int, f"{context}.step_ordinal"),
        decision_ordinal=_expect(
            payload["decision_ordinal"], int, f"{context}.decision_ordinal"
        ),
        actor_seat=_expect(payload["actor_seat"], int, f"{context}.actor_seat"),
    )
    dealer_seat = _expect(payload["dealer_seat"], int, f"{context}.dealer_seat")
    relative_offset = _expect(
        payload["relative_offset"], int, f"{context}.relative_offset"
    )
    if relative_offset not in (1, 2, 3):
        raise _error(f"{context}.relative_offset must be 1, 2 or 3")
    try:
        identity = opponent_identity(
            Seat(row_identity.actor_seat), relative_offset, Seat(dealer_seat)
        )
    except ValueError as error:
        raise _error(f"{context} seat mapping is not resolvable") from error
    if int(identity.seat) != _expect(
        payload["opponent_seat"], int, f"{context}.opponent_seat"
    ):
        raise _error(
            f"{context}.opponent_seat does not match the recorded relative offset"
        )
    if identity.wind is not Wind(
        _expect(payload["opponent_wind"], str, f"{context}.opponent_wind")
    ):
        raise _error(f"{context}.opponent_wind does not match the canonical seat wind")

    concealed = payload["concealed_tiles"]
    if concealed is not None:
        if type(concealed) is not list or any(
            type(item) is not str for item in concealed
        ):
            raise _error(f"{context}.concealed_tiles must be an array of tile strings")
        try:
            concealed = tuple(tile_from_mjai(text) for text in concealed)
        except (TypeError, ValueError) as error:
            raise _error(f"{context}.concealed_tiles is not decodable") from error

    melds = payload["melds"]
    if melds is not None:
        if type(melds) is not list:
            raise _error(f"{context}.melds must be an array or null")
        melds = tuple(
            _meld_from_document(item, f"{context}.melds[{index}]")
            for index, item in enumerate(melds)
        )

    try:
        availability = TargetAvailability(
            _expect(payload["availability"], str, f"{context}.availability")
        )
    except ValueError:
        raise _error(f"{context}.availability is not a known reason code") from None

    tenpai = payload["tenpai"]
    if tenpai is not None and type(tenpai) is not int:
        raise _error(f"{context}.tenpai must be 0, 1 or null")
    wait_mask = payload["wait_mask"]
    if wait_mask is not None:
        if type(wait_mask) is not list or len(wait_mask) != TILE_KIND_COUNT:
            raise _error(
                f"{context}.wait_mask must contain exactly {TILE_KIND_COUNT} values"
            )
        if any(value not in (0, 1) for value in wait_mask):
            raise _error(f"{context}.wait_mask values must be exactly 0 or 1")
        wait_mask = tuple(wait_mask)

    try:
        riichi_state = RiichiState(
            _expect(
                payload["public_riichi_state"], str, f"{context}.public_riichi_state"
            )
        )
    except ValueError:
        raise _error(f"{context}.public_riichi_state is not a RiichiState") from None

    target = OpponentTenpaiTarget(
        identity=identity,
        availability=availability,
        wait_mask=wait_mask,
        tenpai=tenpai,
    )
    return PrivilegedTargetCell(
        row_identity=row_identity,
        public_row_digest=_expect(
            payload["public_row_digest"], str, f"{context}.public_row_digest"
        ),
        dealer_seat=dealer_seat,
        identity=identity,
        concealed_tiles=concealed,
        melds=melds,
        public_riichi_state=riichi_state,
        privileged_riichi_declared=_expect(
            payload["privileged_riichi_declared"],
            bool,
            f"{context}.privileged_riichi_declared",
        ),
        target=target,
    )


def recompute_target(cell: PrivilegedTargetCell) -> OpponentTenpaiTarget:
    """storedなsidecar inputsだけからcanonical targetを再計算する。

    canonical implementationはlisjong側であり、この関数はStage A0 availability
    contractを同じ順序で再適用するだけである。
    """
    return build_opponent_target(
        cell.identity,
        seat_resolved=True,
        public_riichi=cell.public_riichi_state,
        privileged_riichi_declared=cell.privileged_riichi_declared,
        concealed_tiles=cell.concealed_tiles,
        melds=cell.melds,
    )


def availability_counts(
    cells,
) -> dict[str, int]:
    """全reason codeを0埋めしたcountable aggregateを返す。"""
    counts = {availability.value: 0 for availability in TargetAvailability}
    for cell in cells:
        counts[cell.availability.value] += 1
    return counts


def sidecar_identity(manifest: dict[str, object]) -> str:
    """`sidecar_identity`自身を除いたcanonical manifestのsha256を返す。"""
    logical = {
        name: value for name, value in manifest.items() if name != "sidecar_identity"
    }
    return hashlib.sha256(canonical_json_text(logical).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class LoadedSidecar:
    """strict readback済みのprivileged sidecar。"""

    path: Path
    manifest: dict
    cells: tuple

    @property
    def identity(self) -> str:
        return self.manifest["sidecar_identity"]

    @property
    def route(self) -> str:
        return self.manifest["protocol"]["route"]


def write_sidecar(
    destination,
    cells,
    *,
    route: str,
    source_identity: str,
    provenance: dict[str, str],
) -> LoadedSidecar:
    """cellsをimmutable sidecar directoryへ書き、その場でstrict readbackする。"""
    if route not in ROUTES:
        raise _error(f"unsupported sidecar route: {route!r}")
    destination = Path(destination)
    if destination.exists():
        raise FileExistsError("sidecar destination already exists")

    cells = tuple(cells)
    if any(not isinstance(cell, PrivilegedTargetCell) for cell in cells):
        raise TypeError("cells must contain only PrivilegedTargetCell values")
    for cell in cells:
        if cell.row_identity.source_identity != source_identity:
            raise _error("every cell must belong to the declared source identity")

    lines = "".join(
        json.dumps(
            cell.to_document(),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
        for cell in cells
    ).encode("utf-8")

    manifest: dict[str, object] = {
        "sidecar_schema_version": SIDECAR_SCHEMA_VERSION,
        "protocol": {
            "protocol_id": PROTOCOL_ID,
            "issue": ISSUE_IDENTITY,
            "route": route,
            "source_identity": source_identity,
            "game_mode": GAME_MODE,
            "teacher_identity": TEACHER_IDENTITY,
            "teacher_policy_class": TEACHER_POLICY_CLASS,
            "teacher_population": TEACHER_POPULATION,
            "teacher_source_revision": TEACHER_SOURCE_REVISION,
        },
        "canonical_wait_implementation": {
            "builder": CANONICAL_WAIT_BUILDER,
            "entry_point": CANONICAL_WAIT_ENTRY_POINT,
            "implementation_identity": exact_wait_implementation_identity(),
        },
        "provenance": dict(provenance),
        "totals": {
            "row_count": len({cell.row_identity.decision_key for cell in cells}),
            "cell_count": len(cells),
            "availability_counts": availability_counts(cells),
        },
        "files": {
            "cells": {
                "bytes": len(lines),
                "sha256": hashlib.sha256(lines).hexdigest(),
            }
        },
    }
    manifest["sidecar_identity"] = sidecar_identity(manifest)

    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        mkdtemp(prefix=f".{destination.name}-staging-", dir=destination.parent)
    )
    try:
        (staging / CELLS_FILENAME).write_bytes(lines)
        (staging / MANIFEST_FILENAME).write_text(
            canonical_json_text(manifest), encoding="utf-8", newline="\n"
        )
        staging.rename(destination)
    except BaseException:
        rmtree(staging, ignore_errors=True)
        raise
    return load_sidecar(destination)


def load_sidecar(path) -> LoadedSidecar:
    """sidecarを読み、identity / digest / cell整合をfail closedで検証する。"""
    path = Path(path)
    if not path.is_dir():
        raise _error("sidecar path is not a directory")
    if {item.name for item in path.iterdir()} != {MANIFEST_FILENAME, CELLS_FILENAME}:
        raise _error("sidecar contains missing or extra files")

    manifest_text = (path / MANIFEST_FILENAME).read_text(encoding="utf-8")
    try:
        manifest = json.loads(manifest_text)
    except json.JSONDecodeError as error:
        raise _error("sidecar manifest is not valid JSON") from error

    document = _expect_object(manifest, _MANIFEST_FIELDS, "manifest")
    if document["sidecar_schema_version"] != SIDECAR_SCHEMA_VERSION:
        raise _error(
            f"unsupported sidecar schema: {document['sidecar_schema_version']!r}"
        )
    protocol = _expect_object(document["protocol"], _PROTOCOL_FIELDS, "protocol")
    if protocol["protocol_id"] != PROTOCOL_ID or protocol["issue"] != ISSUE_IDENTITY:
        raise _error("sidecar protocol identity is not supported")
    if protocol["route"] not in ROUTES:
        raise _error("sidecar route is not supported")
    for name, expected in (
        ("game_mode", GAME_MODE),
        ("teacher_identity", TEACHER_IDENTITY),
        ("teacher_policy_class", TEACHER_POLICY_CLASS),
        ("teacher_population", TEACHER_POPULATION),
        ("teacher_source_revision", TEACHER_SOURCE_REVISION),
    ):
        if protocol[name] != expected:
            raise _error(f"sidecar protocol.{name} does not match the locked contract")

    canonical = _expect_object(
        document["canonical_wait_implementation"],
        _CANONICAL_FIELDS,
        "canonical_wait_implementation",
    )
    if (
        canonical["builder"] != CANONICAL_WAIT_BUILDER
        or canonical["entry_point"] != CANONICAL_WAIT_ENTRY_POINT
    ):
        raise _error("sidecar canonical wait implementation identity is not supported")
    _digest_text(
        canonical["implementation_identity"],
        "canonical_wait_implementation.implementation_identity",
    )

    provenance = document["provenance"]
    if type(provenance) is not dict or not provenance:
        raise _error("sidecar provenance must be a non-empty object")
    if any(type(value) is not str or not value for value in provenance.values()):
        raise _error("sidecar provenance values must be non-empty strings")

    totals = _expect_object(document["totals"], _TOTALS_FIELDS, "totals")
    files = _expect_object(document["files"], {"cells"}, "files")
    cells_file = _expect_object(files["cells"], _FILE_FIELDS, "files.cells")

    payload = (path / CELLS_FILENAME).read_bytes()
    if len(payload) != cells_file["bytes"]:
        raise _error("cells byte count differs from the manifest")
    if hashlib.sha256(payload).hexdigest() != _digest_text(
        cells_file["sha256"], "files.cells.sha256"
    ):
        raise _error("cells sha256 differs from the manifest")

    text = payload.decode("utf-8")
    if text and not text.endswith("\n"):
        raise _error("cells.jsonl must end with a newline")
    cells = []
    for index, line in enumerate(text.splitlines()):
        try:
            record = json.loads(line)
        except json.JSONDecodeError as error:
            raise _error("cells.jsonl contains malformed JSON") from error
        cell = cell_from_document(record, f"cell[{index}]")
        if cell.row_identity.source_identity != protocol["source_identity"]:
            raise _error(f"cell[{index}] does not belong to the declared source")
        cells.append(cell)

    cells = tuple(cells)
    if totals["cell_count"] != len(cells):
        raise _error("totals.cell_count differs from the actual cells")
    if totals["row_count"] != len({cell.row_identity.decision_key for cell in cells}):
        raise _error("totals.row_count differs from the actual decision rows")
    if totals["availability_counts"] != availability_counts(cells):
        raise _error("totals.availability_counts differ from the actual cells")

    identity = _digest_text(document["sidecar_identity"], "sidecar_identity")
    if identity != sidecar_identity(document):
        raise _error("sidecar_identity does not match the manifest content")
    if canonical_json_text(document) != manifest_text:
        raise _error("sidecar manifest bytes are not canonical JSON")

    return LoadedSidecar(path=path, manifest=document, cells=cells)


def verify_deterministic_recomputation(sidecar: LoadedSidecar) -> int:
    """全cellのlabelをstored inputsから再計算し、storedな値と一致させる。

    一致しないcellが1つでもあればfail closedし、deterministic recomputation
    qualificationを通さない。
    """
    if not isinstance(sidecar, LoadedSidecar):
        raise TypeError("sidecar must be a LoadedSidecar")
    for index, cell in enumerate(sidecar.cells):
        recomputed = recompute_target(cell)
        if recomputed != cell.target:
            raise _error(
                f"cell[{index}] does not recompute to its stored Stage A0 target"
            )
    return len(sidecar.cells)


__all__ = [
    "CELLS_FILENAME",
    "MANIFEST_FILENAME",
    "ROUTES",
    "ROUTE_FRESH",
    "ROUTE_RETAINED",
    "SIDECAR_SCHEMA_VERSION",
    "LoadedSidecar",
    "PrivilegedTargetCell",
    "availability_counts",
    "cell_from_document",
    "load_sidecar",
    "recompute_target",
    "sidecar_identity",
    "verify_deterministic_recomputation",
    "write_sidecar",
]
