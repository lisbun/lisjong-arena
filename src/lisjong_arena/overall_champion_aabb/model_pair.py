"""#583 exact two-artifact Learning Champion binding for Overall lock v2.

The historical Overall v1 checkpoint contract remains untouched. This module
only handles the exact #582-promoted L2a M1+R1, with independent manifest and
weights digests for both heads. Never collapse the pair into one checkpoint.
"""

from __future__ import annotations

from collections.abc import Mapping

from .protocol import OverallChampionProtocolError, resolve_binding_callable

BINDING = (
    "lisjong_arena.current_learning_champion:"
    "served_current_learning_champion_artifacts"
)
FACTORY = (
    "lisjong_arena.current_learning_champion:"
    "create_current_learning_champion_policy"
)
POLICY_IDENTITY = (
    "lisjong-l2a:"
    "c976ed4a5cb667c406eb57f5cc79c128b1a221fb9b478f0dd017fcf48bde6e03:"
    "d565713f51a2b9f9cbdd2146bf01cb4bfb13ff859b639de962c74562eaf01efc"
)
PAIR_VERSION = "exact-l2a-m1-r1-582-v1"

# #582 formal lock / #127 operator-local four-file readback, immutable values.
EXPECTED_COMPONENTS = {
    "discard": {
        "identity": "c976ed4a5cb667c406eb57f5cc79c128b1a221fb9b478f0dd017fcf48bde6e03",
        "manifest_sha256": "37ddf3b89f9a814341cb3ef067c5faf0322df3fe28df3b321a5e335c21d8bc5c",
        "weights_sha256": "74bd966ecdfeebee21eb09a53fdbf495d29eb22fcba382115aad53ff2e88ba8c",
    },
    "reaction": {
        "identity": "d565713f51a2b9f9cbdd2146bf01cb4bfb13ff859b639de962c74562eaf01efc",
        "manifest_sha256": "70962c2e620326b804eda78402bada06b1405684cb5aa381a438ba232b2367a9",
        "weights_sha256": "b996ea98c35aec7693e07d23e0fb886c6797f7490b3a2f2833bde16c74f94a13",
    },
}


class OverallModelPairError(ValueError):
    """The L2a artifact declaration or live served bytes are not exact."""


def require_components(value: object) -> dict[str, dict[str, str]]:
    """Validate independent, strictly typed M1 and R1 component attestations."""
    if type(value) is not dict or set(value) != set(EXPECTED_COMPONENTS):
        raise OverallModelPairError("learning model pair must contain discard and reaction")
    for role, expected in EXPECTED_COMPONENTS.items():
        actual = value[role]
        if type(actual) is not dict or set(actual) != set(expected):
            raise OverallModelPairError(f"{role} model binding fields differ")
        if any(type(actual[k]) is not str or actual[k] != v for k, v in expected.items()):
            raise OverallModelPairError(f"{role} model binding differs from #582")
    return value


def require_pair_document(value: object) -> dict[str, object]:
    """Strict v2 lock document readback, without accessing private artifacts."""
    if type(value) is not dict or set(value) != {"pair_version", "binding", "components"}:
        raise OverallModelPairError("learning_artifacts v2 fields differ")
    if value["pair_version"] != PAIR_VERSION or type(value["pair_version"]) is not str:
        raise OverallModelPairError("unsupported learning artifact pair version")
    if value["binding"] != BINDING or type(value["binding"]) is not str:
        raise OverallModelPairError("learning artifact resolver differs from current Champion")
    require_components(value["components"])
    return value


def live_pair_document() -> dict[str, object]:
    """Ask the actual serving factory for both loaded artifacts and hash bytes."""
    try:
        resolver = resolve_binding_callable(BINDING)
        live = resolver()
    except (OverallChampionProtocolError, Exception) as exc:
        raise OverallModelPairError("cannot attest current Learning Champion models") from exc
    require_components(live)
    return {
        "pair_version": PAIR_VERSION,
        "binding": BINDING,
        "components": live,
    }


def require_live_pair(document: object) -> None:
    """Fail closed on any mutated/missing/swapped file at start and end of run."""
    expected = require_pair_document(document)
    if live_pair_document() != expected:
        raise OverallModelPairError("live served L2a artifacts differ from v2 lock")


__all__ = [
    "BINDING",
    "FACTORY",
    "POLICY_IDENTITY",
    "PAIR_VERSION",
    "EXPECTED_COMPONENTS",
    "OverallModelPairError",
    "live_pair_document",
    "require_components",
    "require_live_pair",
    "require_pair_document",
]
