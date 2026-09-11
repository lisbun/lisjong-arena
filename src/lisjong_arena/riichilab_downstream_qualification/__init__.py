"""Issue #203のoffline downstream reconstruction qualification。

Issue #170で取得済みのimmutable RiichiLab corpusを、player-safeなdecision-time
reconstructionへ変換できるか、そのうえでstrong-bot behavior supervisionと
server-truth hidden-state supervisionを独立に供給できるかを、offlineで技術判定
するためのpackageである。

このpackageは次を行わない。

```text
Learned Policy training
HandBelief training
新しいRiichiLab acquisition / network access
generic third-party dataset framework
PolicyInput redesign / 新model / 新head
```
"""

from lisjong_arena.riichilab_downstream_qualification.classification import (
    OverallOutcome,
    SurfaceClassification,
)
from lisjong_arena.riichilab_downstream_qualification.mjai_events import (
    MjaiReplayError,
    UnsupportedReason,
)
from lisjong_arena.riichilab_downstream_qualification.qualification import (
    EXPECTED_CORPUS_IDENTITY,
    EXPECTED_MANIFEST_SHA256,
    qualify_local_corpus,
)
from lisjong_arena.riichilab_downstream_qualification.report import (
    DownstreamQualificationReport,
    write_report,
)

__all__ = [
    "EXPECTED_CORPUS_IDENTITY",
    "EXPECTED_MANIFEST_SHA256",
    "DownstreamQualificationReport",
    "MjaiReplayError",
    "OverallOutcome",
    "SurfaceClassification",
    "UnsupportedReason",
    "qualify_local_corpus",
    "write_report",
]
