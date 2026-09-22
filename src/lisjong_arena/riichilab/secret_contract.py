"""RiichiLab bot token のSecrets Manager `SecretString` raw-token contract
(Issue #336)。

canonicalな契約:

    SecretString = raw RiichiLab bot token (単一のopaque文字列)

過去の#313 live実行では、`SecretString`がJSON object
(`{"LISJONG_DEV_BOT_TOKEN": "<token>"}`)として保存されており、bootstrapが
その全文をbearer tokenとして使用したためRiichiLabがHTTP 401を返した。この
moduleはranked WebSocket接続を開く前に、instance-side `GetSecretValue`
直後の値がこのcontractへ違反していないかをfail closedで検証する。

trailing LF/CRLFを含む違反secretは、POSIX/Bashのcommand substitution
(`$(...)`)がtrailing newlineを正規化・除去してしまうため、その正規化を
経由する前に検出する必要がある。`extract_and_validate_secret_string()`は
`aws secretsmanager get-secret-value --output json`の応答全文をそのまま
受け取りJSON parseする。`SecretString`内部のCR/LFはJSON文字列内では
`\\n` / `\\r`のescape sequenceとして表現されるため、応答全文の末尾に
シェルが正規化し得るraw newlineバイトがあっても、`SecretString`自体が
保持していたCR/LFの有無は保たれる。

validationはtrim・repair・JSON nested fieldからの自動抽出を一切行わない。
違反時は値を含まないsecret-safeな例外だけを送出する。
"""

from __future__ import annotations

import json
import sys
from collections.abc import Sequence

SECRET_FORMAT_BLOCKER_MESSAGE = (
    "RIICHILAB SECRET FORMAT BLOCKER\n"
    "expected: raw opaque token string\n"
    "observed: structured/invalid secret value"
)


class SecretFormatError(Exception):
    """`SecretString`がraw single-token contractに違反する場合のfail closed例外。

    例外メッセージには固定のsecret-safeな定型文だけを含み、渡された値・
    その一部・prefix/suffix/hashはいずれも含めない。
    """

    def __init__(self, message: str = SECRET_FORMAT_BLOCKER_MESSAGE) -> None:
        super().__init__(message)


def validate_secret_string(secret_string: object) -> str:
    """`secret_string`がraw single-token contractを満たす場合、無変換で返す。

    次のいずれかに該当する場合は`SecretFormatError`を送出する。

    - 文字列でない(missing / JSON null等)
    - 空文字列
    - CR (`\\r`) または LF (`\\n`) を含む
    - JSON object またはJSON arrayとしてparse可能

    trim、JSON nested fieldの抽出、その他の暗黙変換は行わない。
    """
    if not isinstance(secret_string, str) or secret_string == "":
        raise SecretFormatError()
    if "\n" in secret_string or "\r" in secret_string:
        raise SecretFormatError()
    try:
        parsed = json.loads(secret_string)
    except json.JSONDecodeError, ValueError:
        parsed = None
    if isinstance(parsed, (dict, list)):
        raise SecretFormatError()
    return secret_string


def extract_and_validate_secret_string(get_secret_value_response_text: str) -> str:
    """`get-secret-value --output json`の応答全文から`SecretString`を取り出し検証する。

    応答全文自体が不正なJSONの場合、またはtop-levelがobjectでない場合も
    `SecretFormatError`とする。
    """
    try:
        response = json.loads(get_secret_value_response_text)
    except json.JSONDecodeError, ValueError:
        raise SecretFormatError() from None
    if not isinstance(response, dict):
        raise SecretFormatError()
    return validate_secret_string(response.get("SecretString"))


def _run_cli(argv: Sequence[str] | None = None) -> int:
    """`aws secretsmanager get-secret-value --output json`の応答をstdinから
    読み込み、raw-token contractを検証するCLI entry point。

    成功時はtoken値をtrailing newlineなしでstdoutへ一度だけ書き込む。
    失敗時はsecret-safeなblocker文をstderrへ出力し、stdoutへは何も
    書き込まない。呼び出し側は`$(...)`でstdoutを捕捉して環境変数へ
    exportすることを想定する。
    """
    del argv
    response_text = sys.stdin.read()
    try:
        token = extract_and_validate_secret_string(response_text)
    except SecretFormatError as error:
        print(str(error), file=sys.stderr)
        return 1
    sys.stdout.write(token)
    return 0


if __name__ == "__main__":
    sys.exit(_run_cli())


__all__ = [
    "SECRET_FORMAT_BLOCKER_MESSAGE",
    "SecretFormatError",
    "extract_and_validate_secret_string",
    "validate_secret_string",
]
