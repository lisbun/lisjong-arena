"""Serial request pacing shared by the metadata and MJAI acquisition phases."""

from __future__ import annotations

import time
from typing import Callable

# Issue #269は成功request間に最低おおむね0.5秒を要求する。Issue #170
# acquisitionと同じ低負荷方針だが、#170 contractへ依存せず#269側で固定する。
SERIAL_REQUEST_INTERVAL_SECONDS = 0.5


class RequestPacer:
    """Sleep between serial live GETs, counting every phase of one sync."""

    __slots__ = ("_interval", "_sleeper", "request_count")

    def __init__(
        self,
        *,
        interval_seconds: float = SERIAL_REQUEST_INTERVAL_SECONDS,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if type(interval_seconds) not in (int, float) or not 0 <= interval_seconds <= 5:
            raise ValueError("interval_seconds must be from 0 through 5")
        self._interval = float(interval_seconds)
        self._sleeper = sleeper
        self.request_count = 0

    def before_request(self) -> None:
        if self.request_count:
            self._sleeper(self._interval)
        self.request_count += 1


__all__ = ["RequestPacer", "SERIAL_REQUEST_INTERVAL_SECONDS"]
