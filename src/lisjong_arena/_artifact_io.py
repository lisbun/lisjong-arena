"""artifact contractが共有するJSON serialization / parse / file書き込みのplumbing。

このmoduleはevaluation semanticsもartifact schemaも所有しない。既存AABB
``lisjong_arena.artifact``とABBB ``lisjong_arena.single_round_artifact``が
それぞれ独立したschemaを持ったまま、``1 artifact = 1 immutable file``の
書き込み規則、canonical JSON表現、fail-closedなfield検証だけを共通化する。

ここで提供するのは低レベルのplumbingだけであり、どのfieldが必要か、どの
derived valueが正本かといったcontract自体は各artifact moduleが決める。
"""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Iterator


class ArtifactValidationError(ValueError):
    """artifact documentのfieldをcontractとして解釈できない場合。

    各artifact moduleはこのclassをbaseにした専用error型を公開し、readerが
    module固有のerrorだけをcatchできるようにする。
    """


def expect_object(
    value: object,
    expected_keys: set[str],
    context: str,
) -> dict[str, object]:
    """JSON objectであり、keyの集合が完全に一致することを検証する。"""
    if type(value) is not dict:
        raise ArtifactValidationError(f"{context} must be an object")
    if set(value) != expected_keys:
        raise ArtifactValidationError(f"{context} fields are invalid")
    return value


def expect_list(value: object, context: str) -> list[object]:
    if type(value) is not list:
        raise ArtifactValidationError(f"{context} must be an array")
    return value


def expect_str(value: object, context: str) -> str:
    if type(value) is not str:
        raise ArtifactValidationError(f"{context} must be a string")
    return value


def expect_int(value: object, context: str) -> int:
    if type(value) is not int:
        raise ArtifactValidationError(f"{context} must be an integer")
    return value


def expect_bool(value: object, context: str) -> bool:
    if type(value) is not bool:
        raise ArtifactValidationError(f"{context} must be a boolean")
    return value


def expect_float(value: object, context: str) -> float:
    """JSON numberのうちfloatとして書かれた値だけを受理する。

    ``25000``のような整数literalをsilentに``25000.0``へ広げると、derived
    metricsを再集計値とexact比較できなくなるため受理しない。
    """
    if type(value) is not float:
        raise ArtifactValidationError(f"{context} must be a JSON number with decimals")
    return value


def expect_optional_int(value: object, context: str) -> int | None:
    return None if value is None else expect_int(value, context)


def expect_optional_bool(value: object, context: str) -> bool | None:
    return None if value is None else expect_bool(value, context)


def expect_optional_float(value: object, context: str) -> float | None:
    return None if value is None else expect_float(value, context)


def canonical_json_text(document: dict[str, Any]) -> str:
    """同一artifactが常に同一bytesへserializeされるcanonical JSON textを返す。"""
    return (
        json.dumps(
            document,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            indent=2,
        )
        + "\n"
    )


def sha256_bytes(data: bytes) -> str:
    """bytesのSHA-256をlowercase hexで返す。digest対象の意味はcallerが所有する。"""
    return hashlib.sha256(data).hexdigest()


@contextmanager
def staged_artifact_directory(destination: Path) -> Iterator[Path]:
    """same-parent staging directoryを成功時だけdestinationへpublishする。

    callerはdestinationのwrite-once preflightとparent directory作成を所有する。
    このhelperはartifact schema / required files / readback ruleを知らず、bodyが
    例外を送出した場合はstagingをTemporaryDirectoryのcleanupへ委ねて公開しない。
    """
    with TemporaryDirectory(
        prefix=f".{destination.name}-staging-", dir=destination.parent
    ) as staging_name:
        staging = Path(staging_name)
        yield staging
        staging.rename(destination)


def write_new_artifact_file(path: Path, text: str) -> None:
    """新しいfileだけへUTF-8 textを書き、既存pathを上書きしない。

    ``1 run = 1 immutable artifact``とするため、pathが存在する場合は
    ``FileExistsError``を送出する。書き込み途中で失敗した場合はpartialな
    fileを残さない。
    """
    created = False
    try:
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            created = True
            stream.write(text)
    except Exception:
        if created:
            try:
                path.unlink()
            except OSError:
                pass
        raise


def _reject_json_constant(value: str) -> None:
    raise ArtifactValidationError(f"non-finite JSON number is not allowed: {value}")


def _reject_duplicate_object_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    """JSON objectのduplicate keyをlast-winsで解釈せず拒否する。"""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ArtifactValidationError(f"duplicate JSON object key: {key!r}")
        result[key] = value
    return result


def parse_json_text(serialized: str) -> object:
    """JSON textを、非有限数とduplicate keyを拒否してparseする。

    ``json.JSONDecodeError``はここでcatchせず、caller側のfail-closedな
    error契約へそのまま伝える。1 file = 1 documentのartifactと、1 line =
    1 documentのJSON Lines payloadが同じ厳格さでparseされるようにする。
    """
    return json.loads(
        serialized,
        parse_constant=_reject_json_constant,
        object_pairs_hook=_reject_duplicate_object_keys,
    )


def read_json_document(path: Path) -> object:
    """UTF-8 JSON fileを、非有限数とduplicate keyを拒否して読み込む。

    ``json.JSONDecodeError``はここでcatchせず、caller側のfail-closedな
    error契約へそのまま伝える。
    """
    try:
        serialized = path.read_text(encoding="utf-8")
    except UnicodeError as exc:
        raise ArtifactValidationError("artifact is not valid UTF-8") from exc
    return parse_json_text(serialized)


__all__ = [
    "ArtifactValidationError",
    "canonical_json_text",
    "expect_bool",
    "expect_float",
    "expect_int",
    "expect_list",
    "expect_object",
    "expect_optional_bool",
    "expect_optional_float",
    "expect_optional_int",
    "expect_str",
    "parse_json_text",
    "read_json_document",
    "sha256_bytes",
    "staged_artifact_directory",
    "write_new_artifact_file",
]
