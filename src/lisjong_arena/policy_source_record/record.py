"""``arena-policy-source-record-v1``: player-safe source rows for any catalog teacher.

Rows use exactly the player-safe row shape of the Offense Foundation source
record (lisbun/lisjong-arena#342): typed ``PolicyInput``, canonical legal
actions and the selected action that passed lisjong legal-action validation and
was handed to the runner. The encoding and row validation are reused from
``lisjong_arena.offense_foundation.source_record``; only the manifest differs,
because this record is bound to a population and teacher instead of an #331
protocol lock and scientific corpus.
"""

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path

from lisjong_arena import seed_registry
from lisjong_arena._artifact_io import parse_json_text
from lisjong_arena.offense_foundation import source_record as offense_source
from lisjong_arena.offense_foundation.qualification import (
    read_document,
    seal,
    unseal,
    write_document,
)
from lisjong_arena.offense_foundation.semantics import OffenseError

from .errors import PolicySourceRecordError

SCHEMA = "arena-policy-source-record-v1"
KIND = "policy-source-record"
POPULATION_SCHEMA = "arena-policy-source-population-v1"
GAME_MODE = "4p-red-half"
SOURCE_FILENAME = offense_source.SOURCE_FILENAME
SPLITS = ("TRAIN", "SELECT", "OFFLINE-EVAL")
DEVELOPMENT = "DEVELOPMENT"
SCIENTIFIC = "SCIENTIFIC"
PURPOSES = (DEVELOPMENT, SCIENTIFIC)

_POPULATION_FIELDS = {"schema", "purpose", "populations", "allocation_bindings"}
_MANIFEST_FIELDS = {
    "schema",
    "kind",
    "purpose",
    "game_mode",
    "source_contract",
    "populations",
    "allocation_bindings",
    "games",
}
_GAME_FIELDS = {
    "game_ordinal",
    "seed",
    "split",
    "game_mode",
    "decision_count",
    "steps",
    "files",
}


def _strict(call, *args, **kwargs):
    try:
        return call(*args, **kwargs)
    except OffenseError as error:
        raise PolicySourceRecordError(str(error)) from error


def validate_population(document: object) -> dict[str, object]:
    """Validate an explicit split population and its seed authority.

    ``DEVELOPMENT`` populations carry no allocation binding and may only be
    used for connection / cost measurement. ``SCIENTIFIC`` populations carry
    one Arena ledger binding per split (shape-checked here; live authority is
    checked by ``require_population_authority()`` before generation).
    """
    if type(document) is not dict or set(document) != _POPULATION_FIELDS:
        raise PolicySourceRecordError("invalid population fields")
    if document["schema"] != POPULATION_SCHEMA:
        raise PolicySourceRecordError("unsupported population schema")
    purpose = document["purpose"]
    if purpose not in PURPOSES:
        raise PolicySourceRecordError(
            "population purpose must be DEVELOPMENT or SCIENTIFIC"
        )
    populations = document["populations"]
    if type(populations) is not dict or not populations:
        raise PolicySourceRecordError("populations must be a nonempty object")
    if set(populations) - set(SPLITS):
        raise PolicySourceRecordError(f"population splits must be among {SPLITS}")
    seen: set[int] = set()
    for split, seeds in populations.items():
        if (
            type(seeds) is not list
            or not seeds
            or any(type(seed) is not int or not 0 <= seed < 2**32 for seed in seeds)
        ):
            raise PolicySourceRecordError(f"{split} seeds must be nonempty uint32s")
        if len(set(seeds)) != len(seeds) or seen.intersection(seeds):
            raise PolicySourceRecordError(
                "population seeds must be unique across splits"
            )
        seen.update(seeds)
    bindings = document["allocation_bindings"]
    if purpose == DEVELOPMENT:
        if bindings is not None:
            raise PolicySourceRecordError(
                "DEVELOPMENT populations must not claim allocation bindings"
            )
    else:
        if type(bindings) is not dict or set(bindings) != set(populations):
            raise PolicySourceRecordError(
                "SCIENTIFIC allocation bindings must cover exactly the splits"
            )
        for split, seeds in populations.items():
            try:
                seed_registry.validate_binding_shape(bindings[split], seeds=seeds)
            except seed_registry.SeedRegistryError as error:
                raise PolicySourceRecordError(
                    f"invalid {split} allocation binding: {error}"
                ) from error
    return document


def require_population_authority(population: Mapping, ledger: object) -> None:
    """Resolve every SCIENTIFIC split binding against the live Arena ledger."""
    if population["purpose"] != SCIENTIFIC:
        return
    for split, seeds in population["populations"].items():
        try:
            seed_registry.require_allocation_binding(
                ledger,
                population["allocation_bindings"][split],
                seeds=seeds,
                seed_domain=seed_registry.RIICHIENV_HALF_HANCHAN_SEED_DOMAIN,
            )
        except seed_registry.SeedRegistryError as error:
            raise PolicySourceRecordError(
                f"{split} allocation is not authorized: {error}"
            ) from error


def ordered_games(population: Mapping) -> list[tuple[str, int]]:
    """Protocol game order: splits in ``SPLITS`` order, seeds in listed order."""
    populations = population["populations"]
    return [
        (split, seed)
        for split in SPLITS
        if split in populations
        for seed in populations[split]
    ]


def _file_info(path: Path) -> dict[str, object]:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return {"bytes": path.stat().st_size, "sha256": digest.hexdigest()}


def write_game(path: Path, *, game_ordinal, split, seed, result, inspection):
    """Write every seat decision of one executed hanchan, in execution order."""
    if result.seed != seed or result.game_mode != GAME_MODE:
        raise PolicySourceRecordError("executed game identity mismatch")
    path.mkdir()
    payload = path / SOURCE_FILENAME
    ordinal = 0
    with payload.open("x", encoding="utf-8", newline="\n") as stream:
        for expected_step, step in enumerate(inspection.step_observations):
            if step.step_ordinal != expected_step:
                raise PolicySourceRecordError("noncontiguous execution steps")
            previous_seat = -1
            for observation in step.seat_decisions:
                if int(observation.seat) <= previous_seat:
                    raise PolicySourceRecordError("noncanonical execution seat order")
                previous_seat = int(observation.seat)
                row = _strict(
                    offense_source.encode_observation,
                    observation,
                    game_ordinal=game_ordinal,
                    split=split,
                    seed=seed,
                    step_ordinal=step.step_ordinal,
                    decision_ordinal=ordinal,
                )
                stream.write(offense_source._canonical_line(row))
                ordinal += 1
    if ordinal != result.decisions or len(inspection.step_observations) != result.steps:
        raise PolicySourceRecordError("incomplete game/decision accounting")
    return seal(
        {
            "game_ordinal": game_ordinal,
            "seed": seed,
            "split": split,
            "game_mode": GAME_MODE,
            "decision_count": ordinal,
            "steps": result.steps,
            "files": {SOURCE_FILENAME: _file_info(payload)},
        }
    )


def build_manifest(population, contract, game_summaries):
    return seal(
        {
            "schema": SCHEMA,
            "kind": KIND,
            "purpose": population["purpose"],
            "game_mode": GAME_MODE,
            "source_contract": contract,
            "populations": population["populations"],
            "allocation_bindings": population["allocation_bindings"],
            "games": game_summaries,
        }
    )


def write_manifest(path: Path, population, contract, game_summaries):
    manifest = build_manifest(population, contract, game_summaries)
    _strict(write_document, path / "manifest.json", manifest)
    return manifest


def iter_rows(path: Path, game: Mapping):
    """Yield ``(row, policy_input, legal_actions, selected)`` for one game."""
    with (path / SOURCE_FILENAME).open(encoding="utf-8") as rows:
        for line in rows:
            row = _strict(parse_json_text, line)
            if line != offense_source._canonical_line(row):
                raise PolicySourceRecordError("source row is not canonical JSON")
            policy_input, legal_actions, selected = _strict(
                offense_source._read_row, row
            )
            if (
                row["game_ordinal"] != game["game_ordinal"]
                or row["seed"] != game["seed"]
                or row["split"] != game["split"]
            ):
                raise PolicySourceRecordError("source row provenance mismatch")
            yield row, policy_input, legal_actions, selected


def _read_game(path: Path, game: object, *, game_ordinal: int, split: str, seed: int):
    body = _strict(unseal, game)
    if set(body) != _GAME_FIELDS:
        raise PolicySourceRecordError("invalid source game fields")
    for field in ("game_ordinal", "seed", "decision_count", "steps"):
        if type(game[field]) is not int or game[field] < 0:
            raise PolicySourceRecordError("invalid source game count/identity")
    if (game["game_ordinal"], game["seed"], game["split"], game["game_mode"]) != (
        game_ordinal,
        seed,
        split,
        GAME_MODE,
    ):
        raise PolicySourceRecordError("source game population/provenance mismatch")
    if set(game["files"]) != {SOURCE_FILENAME} or {
        child.name for child in path.iterdir()
    } != {SOURCE_FILENAME}:
        raise PolicySourceRecordError("missing/unexpected source game payload")
    if _file_info(path / SOURCE_FILENAME) != game["files"][SOURCE_FILENAME]:
        raise PolicySourceRecordError("source payload checksum/size mismatch")
    total, last_step, last_seat = 0, -1, -1
    for row, _, _, _ in iter_rows(path, game):
        step, seat = row["step_ordinal"], row["actor_seat"]
        if (
            row["decision_ordinal"] != total
            or step not in (last_step, last_step + 1)
            or (step == last_step and seat <= last_seat)
        ):
            raise PolicySourceRecordError(
                "source decision ordering/accounting mismatch"
            )
        last_step, last_seat = step, seat
        total += 1
    if (total, last_step + 1) != (game["decision_count"], game["steps"]):
        raise PolicySourceRecordError("source game accounting mismatch")


def read_source_record(path, *, expected_population=None):
    """Strict-read a complete policy source record and return its manifest."""
    path = Path(path)
    manifest = _strict(read_document, path / "manifest.json")
    body = _strict(unseal, manifest)
    if body.get("schema") != SCHEMA:
        raise PolicySourceRecordError("unsupported policy source record schema")
    if set(body) != _MANIFEST_FIELDS:
        raise PolicySourceRecordError("invalid policy source manifest fields")
    if manifest["kind"] != KIND or manifest["game_mode"] != GAME_MODE:
        raise PolicySourceRecordError("policy source kind/game mode mismatch")
    population = validate_population(
        {
            "schema": POPULATION_SCHEMA,
            "purpose": manifest["purpose"],
            "populations": manifest["populations"],
            "allocation_bindings": manifest["allocation_bindings"],
        }
    )
    if expected_population is not None and population != validate_population(
        expected_population
    ):
        raise PolicySourceRecordError("source record differs from expected population")
    contract = manifest["source_contract"]
    if (
        type(contract) is not dict
        or type(contract.get("teacher")) is not dict
        or contract.get("seat_policies")
        != [contract["teacher"].get("catalog_identity")] * 4
        or contract.get("game_mode") != GAME_MODE
    ):
        raise PolicySourceRecordError("invalid policy source teacher binding")
    games = ordered_games(population)
    if type(manifest["games"]) is not list or len(manifest["games"]) != len(games):
        raise PolicySourceRecordError("source record missing/extra hanchan")
    expected_names = {"manifest.json", *(f"game-{i:03d}" for i in range(len(games)))}
    if {child.name for child in path.iterdir()} != expected_names:
        raise PolicySourceRecordError("missing/unexpected source record files")
    for game_ordinal, ((split, seed), game) in enumerate(
        zip(games, manifest["games"], strict=True)
    ):
        _read_game(
            path / f"game-{game_ordinal:03d}",
            game,
            game_ordinal=game_ordinal,
            split=split,
            seed=seed,
        )
    return manifest


def population_document(purpose, populations, allocation_bindings=None):
    """Build and validate a population document from ``{split: seeds}``."""
    return validate_population(
        {
            "schema": POPULATION_SCHEMA,
            "purpose": purpose,
            "populations": {
                split: list(populations[split])
                for split in SPLITS
                if split in populations
            },
            "allocation_bindings": allocation_bindings,
        }
    )


def load_population(path) -> dict[str, object]:
    return validate_population(json.loads(Path(path).read_text(encoding="utf-8")))


__all__ = [
    "DEVELOPMENT",
    "GAME_MODE",
    "KIND",
    "POPULATION_SCHEMA",
    "SCHEMA",
    "SCIENTIFIC",
    "SOURCE_FILENAME",
    "SPLITS",
    "build_manifest",
    "iter_rows",
    "load_population",
    "ordered_games",
    "population_document",
    "read_source_record",
    "require_population_authority",
    "validate_population",
    "write_game",
    "write_manifest",
]
