"""Teacher-selectable player-safe policy source record (lisbun/lisjong-arena#442).

See ``docs/policy-source-record.md``. The Offense Foundation v1/v2 source record
and the #331 scientific corpus are unchanged by this package.
"""

from .archive import archive_source_record, restore_source_record
from .errors import PolicySourceRecordError
from .generation import generate
from .record import (
    DEVELOPMENT,
    POPULATION_SCHEMA,
    SCHEMA,
    SCIENTIFIC,
    population_document,
    read_source_record,
)
from .replay import replay_verify

__all__ = [
    "DEVELOPMENT",
    "POPULATION_SCHEMA",
    "SCHEMA",
    "SCIENTIFIC",
    "PolicySourceRecordError",
    "archive_source_record",
    "generate",
    "population_document",
    "read_source_record",
    "replay_verify",
    "restore_source_record",
]
