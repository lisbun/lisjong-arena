"""RiichiLab ranked resilient / continuous participation runner (Issues #47 / #232).

`lisjong_arena.riichilab.ranked.run_ranked_game()` は意図的に

    1 connection -> 1 ranked hanchan -> end_game -> return / disconnect

だけを担当するone-game primitiveであり、本moduleはこのcontractを変更しない
まま、その上位layerとして次を追加する。

- successful completion後のautomatic requeue(新しい`run_ranked_game()`
  invocation = 新しいconnectionとして扱う。same-game resumeは行わない)
- `TransportError`(`UnexpectedDisconnectError`を含む)hierarchyだけを対象
  にしたbounded backoff付きretry。`ProtocolError` / `ProtocolTraceError` /
  profile・credential failure / Policy・Adapter例外 / その他unexpected
  exceptionはcatch-allせず、そのまま伝播させてfail closedする
- 連続failureを追跡するfailure budget(到達後は追加requeueしない)
- 各gameごとに`RuntimeProfile.policy_factory()`から生成したfresh Policy
  instance(cross-game reuseはしない)
- `--games N`相当のcompleted-hanchan数によるbounded stop
- monotonic elapsed timeによるgraceful duration bound。cutoff時に進行中の
  hanchanは中断せず、完了後は新しいgameへrequeueしない
- opt-in durable ranked record acquisition(Issue #168)のper-game composition
- opt-in live presentation(Issue #381)。game attemptごとに
  `ContinuousRankedPresentationFeed`から新しいbufferを開いてone-game
  primitiveへ渡すだけで、presentationの有無でexecution semanticsは変わらない
- secret-safeなper-game event(Issue #404)。completed gameと各
  `TransportError`ごとに`ContinuousRunEvent`をopt-inの`on_event`へ渡す。
  CLIはこれを`continuous-event:`行としてstderrへ即時出力し、fail-closedな
  例外で終了する場合もそれまでのrunner stateを`terminal ...`行として残す
- transport failureのconnection timing evidence(Issue #416)。
  `continuous-event:`行にはscalar数個だけを載せ、ring全体はopt-inの
  `--transport-evidence PATH`へbounded JSON Linesとして書く
- mid-game transport failure後のoriginal-game wait(Issue #419)。in-game
  failureの後、reconnectが明示的な`same_bot_already_active`で拒否される間は
  「元の半荘がまだこのbot identityを保持している」とみなし、ordinary failure
  budgetとは別のfinite boundで待つ。rejectionはfailed game / consecutive
  failureへcountしない
- 停止要求後は新しいgameへrequeueしない graceful shutdown。CLIの
  `--stop-file PATH`は、そのpathが存在することを停止要求として扱う
  (Issue #383。AWS運用で外部から「今の半荘を終えたら止める」を指示する)。
  `asyncio.CancelledError`はretryせずcatchもせずそのまま伝播させ、
  標準のasyncio cancellation semanticsを維持する。Ctrl-Cを正常終了として
  扱うUXは`_run_cli()`の`asyncio.run()` boundaryだけが担う

generic daemon / scheduler / retry frameworkは導入しない。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from urllib.parse import quote

from lisjong.policy_contract.policy import Policy

from lisjong_arena.riichilab.cli import (
    build_arg_parser,
    resolve_ranked_record_path,
    resolve_trace_path,
)
from lisjong_arena.riichilab.durable_ranked_game_record import (
    DurableRankedGameRecordError,
)
from lisjong_arena.riichilab.errors import RiichiLabClientError, TransportError
from lisjong_arena.riichilab.live_presentation import (
    BoundedRankedPresentationBuffer,
    ContinuousRankedPresentationFeed,
)
from lisjong_arena.riichilab.profile import (
    ProfileError,
    RuntimeProfile,
    resolve_credential,
    resolve_profile,
)
from lisjong_arena.riichilab.ranked import run_ranked_game
from lisjong_arena.riichilab.transport import DEFAULT_RANKED_URL
from lisjong_arena.riichilab.transport_diagnostics import (
    PHASE_IN_GAME,
    REASON_SAME_BOT_ALREADY_ACTIVE,
    TransportDiagnostics,
)
from lisjong_arena.riichilab.transport_timing import TransportTimingEvidence

#: backoff baseline (実装前レビュー): 5s -> 10s -> 20s -> 40s -> 60s cap。
_INITIAL_BACKOFF_SECONDS = 5.0
_MAX_BACKOFF_SECONDS = 60.0
#: 連続failureがこの回数へ到達したら追加requeueせずfail closedする。
_FAILURE_BUDGET = 5
#: in-game failure後、`same_bot_already_active` rejectionを待ち続ける上限秒数
#: (Issue #419)。切断された元の半荘がserver側で通常どおり終わるのに十分長く、
#: stuckしたserver/sessionで無限retryしないfinite値とする。wait開始(in-game
#: failure時点)からのmonotonic elapsed timeで、rejectionごとに確認する。
_ORIGINAL_GAME_WAIT_SECONDS = 3600.0


def _backoff_seconds(consecutive_failures: int) -> float:
    """`consecutive_failures`(1始まり)からbounded backoff秒数を求める。

    `min(5 * 2 ** (consecutive_failures - 1), 60)`。upper boundを持ち、
    zero-delay retryにはならない。
    """
    return min(
        _INITIAL_BACKOFF_SECONDS * (2 ** (consecutive_failures - 1)),
        _MAX_BACKOFF_SECONDS,
    )


def _positive_completed_games(value: str) -> int:
    """CLIの`--games`をstrictなpositive integerへ変換する。"""
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "--games must be a positive integer"
        ) from error
    if parsed < 1:
        raise argparse.ArgumentTypeError("--games must be a positive integer")
    return parsed


def _validate_max_completed_games(max_completed_games: int | None) -> None:
    """library APIでもbool-like / non-positive targetをfail closedにする。"""
    if max_completed_games is None:
        return
    if isinstance(max_completed_games, bool) or not isinstance(
        max_completed_games, int
    ):
        raise ValueError("max_completed_games must be a positive integer or None")
    if max_completed_games < 1:
        raise ValueError("max_completed_games must be a positive integer or None")


def _positive_duration_seconds(value: str) -> int:
    """CLIの`--duration-seconds`をstrictなpositive integerへ変換する。"""
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "--duration-seconds must be a positive integer"
        ) from error
    if parsed < 1:
        raise argparse.ArgumentTypeError(
            "--duration-seconds must be a positive integer"
        )
    return parsed


def _validate_max_duration_seconds(max_duration_seconds: int | None) -> None:
    """library APIでもbool-like / non-positive durationをfail closedにする。"""
    if max_duration_seconds is None:
        return
    if isinstance(max_duration_seconds, bool) or not isinstance(
        max_duration_seconds, int
    ):
        raise ValueError("max_duration_seconds must be a positive integer or None")
    if max_duration_seconds < 1:
        raise ValueError("max_duration_seconds must be a positive integer or None")


async def _acquire_ranked_record(
    policy: Policy,
    token: str,
    *,
    destination: str | os.PathLike,
    url: str,
    profile_identity: str,
    policy_identity: str,
    presentation: BoundedRankedPresentationBuffer | None = None,
) -> object:
    """Issue #168 public acquisition contractをimport-cycleなしでcompositionする。"""
    # durable record moduleはranked moduleをimportするため、ranked.py自身のCLIと
    # 同様にcall boundaryでだけimportする。
    from lisjong_arena.riichilab.durable_ranked_game_record import (
        acquire_ranked_game_record,
    )

    presentation_kwargs = {} if presentation is None else {"presentation": presentation}
    return await acquire_ranked_game_record(
        policy,
        token,
        destination=destination,
        url=url,
        profile_identity=profile_identity,
        policy_identity=policy_identity,
        **presentation_kwargs,
    )


#: `ContinuousRunEvent.kind`の値。
EVENT_GAME_COMPLETED = "game_completed"
EVENT_TRANSPORT_FAILURE = "transport_failure"
#: transport failure eventの`outcome`の値。
OUTCOME_RETRY = "retry"
OUTCOME_FAILURE_BUDGET_EXHAUSTED = "failure_budget_exhausted"
OUTCOME_DURATION_REACHED = "duration_reached"
#: Issue #419: in-game failure後のsame-bot rejectionでoriginal gameを待つretry。
OUTCOME_AWAITING_ORIGINAL_GAME = "awaiting_original_game"
#: Issue #419: original-game waitのboundへ到達した(stopped reasonも同じ値)。
OUTCOME_ORIGINAL_GAME_WAIT_EXHAUSTED = "original_game_wait_exhausted"
#: Issue #419: original-game wait中に新しいgameがcompletedした
#: (`game_completed` eventのoutcome)。
OUTCOME_RECOVERED_FROM_ORIGINAL_GAME_WAIT = "recovered_from_original_game_wait"
#: `format_continuous_event()`が出力する行のprefix。
CONTINUOUS_EVENT_PREFIX = "continuous-event:"
#: transport failure行へ載せるIssue #416 timing scalarのkey(出力順)。
TIMING_EVENT_FIELDS = (
    "max_event_loop_lag_seconds",
    "max_lag_ending_in_close_window_seconds",
    "material_lag_ended_in_close_window",
    "max_lag_overlapping_decision_seconds",
    "max_lag_outside_decision_seconds",
    "max_recent_decision_seconds",
    "max_recent_keepalive_latency_seconds",
    "defaulted_acks",
    "stale_acks",
)


@dataclass(frozen=True, slots=True)
class ContinuousRunEvent:
    """completed gameまたはretryable transport failure 1件のsecret-safe event。

    例外はclass名だけを保持し、例外message・protocol payload・token /
    Authorization / credential値・fingerprintは含まない。counterはevent
    発生直後のrunner stateである。

    `transport_failure`では`outcome`がretry継続(`retry`)、failure budget
    到達(`failure_budget_exhausted`)、deadline到達(`duration_reached`)、
    original-game wait中のsame-bot rejection retry(`awaiting_original_game`)、
    original-game wait bound到達(`original_game_wait_exhausted`)の
    いずれかを示す。`backoff_seconds`は実際にsleepする秒数で、sleepしない
    場合は`None`である。`game_completed`の`outcome`はoriginal-game waitから
    回復した場合だけ`recovered_from_original_game_wait`で、それ以外は`None`。

    `awaiting_original_game` / `same_bot_rejections`(Issue #419)はevent直後の
    original-game wait状態と、run全体でoriginal-game wait中に受けたsame-bot
    rejection数である(failed gameとは別に数える)。

    `transport`(Issue #411)はtransport layerがsanitize済みの
    `TransportDiagnostics`で、例外に無い場合は`None`である。
    """

    kind: str
    elapsed_seconds: float
    profile: str
    completed_games: int
    failed_games: int
    consecutive_failures: int
    exception_type: str | None = None
    backoff_seconds: float | None = None
    outcome: str | None = None
    transport: TransportDiagnostics | None = None
    awaiting_original_game: bool = False
    same_bot_rejections: int = 0


def _event_value(value: object) -> str:
    return "none" if value is None else str(value)


def _seconds_value(value: float | None) -> str:
    return "none" if value is None else f"{value:.3f}"


def _timing_fields(timing: TransportTimingEvidence | None) -> list[str]:
    """Issue #416のscalar summary。ring全体はtransport evidence fileへ出す。

    timing evidenceを持たないfailure(handshake失敗等)ではfieldを出さない。
    """
    if timing is None:
        return []
    ended = timing.material_lag_ended_in_close_window
    values = {
        "max_event_loop_lag_seconds": _seconds_value(timing.max_event_loop_lag_seconds),
        "max_lag_ending_in_close_window_seconds": _seconds_value(
            timing.max_lag_ending_in_close_window_seconds
        ),
        "material_lag_ended_in_close_window": (
            "none" if ended is None else str(ended).lower()
        ),
        "max_lag_overlapping_decision_seconds": _seconds_value(
            timing.max_lag_overlapping_decision_seconds
        ),
        "max_lag_outside_decision_seconds": _seconds_value(
            timing.max_lag_outside_decision_seconds
        ),
        "max_recent_decision_seconds": _seconds_value(
            timing.max_recent_decision_seconds
        ),
        "max_recent_keepalive_latency_seconds": _seconds_value(
            timing.max_recent_keepalive_latency_seconds
        ),
        "defaulted_acks": str(timing.defaulted_ack_count),
        "stale_acks": str(timing.stale_ack_count),
    }
    return [f"{name}={values[name]}" for name in TIMING_EVENT_FIELDS]


def format_continuous_event(event: ContinuousRunEvent) -> str:
    """eventを`continuous-event: key=value ...`の1行へformatする。"""
    fields = [
        f"kind={event.kind}",
        f"elapsed_seconds={event.elapsed_seconds:.1f}",
        f"profile={event.profile}",
        f"completed_games={event.completed_games}",
        f"failed_games={event.failed_games}",
        f"consecutive_failures={event.consecutive_failures}",
    ]
    if event.kind != EVENT_TRANSPORT_FAILURE and event.outcome is not None:
        fields.append(f"outcome={event.outcome}")
    if event.awaiting_original_game:
        fields.append("awaiting_original_game=true")
    if event.same_bot_rejections:
        fields.append(f"same_bot_rejections={event.same_bot_rejections}")
    if event.kind == EVENT_TRANSPORT_FAILURE:
        backoff = (
            "none" if event.backoff_seconds is None else f"{event.backoff_seconds:g}"
        )
        fields += [
            f"exception={event.exception_type}",
            f"backoff_seconds={backoff}",
            f"outcome={event.outcome}",
        ]
        diagnostics = event.transport
        if diagnostics is not None:
            decision = diagnostics.last_decision_elapsed_seconds
            excerpt = diagnostics.server_reason_excerpt
            fields += [
                f"phase={diagnostics.phase}",
                f"operation={diagnostics.operation}",
                f"http_status={_event_value(diagnostics.http_status)}",
                f"close_code_received={_event_value(diagnostics.close_code_received)}",
                f"close_code_sent={_event_value(diagnostics.close_code_sent)}",
                f"server_reason_class={diagnostics.server_reason_class}",
                # 値に空白を含み得るためpercent-encodeして1 tokenにする。
                "server_reason_excerpt="
                + ("none" if excerpt is None else quote(excerpt, safe="")),
                f"local_close_reason_class={diagnostics.local_close_reason_class}",
                f"requests_received={diagnostics.requests_received}",
                "last_decision_elapsed_seconds="
                + ("none" if decision is None else f"{decision:.3f}"),
                *_timing_fields(diagnostics.timing),
            ]
    return " ".join([CONTINUOUS_EVENT_PREFIX, *fields])


TRANSPORT_EVIDENCE_SCHEMA_ID = "lisjong-arena-riichilab-transport-evidence"
TRANSPORT_EVIDENCE_SCHEMA_VERSION = 1
#: transport evidence fileへ書くfailure数の上限。以降のfailureは書かない
#: (最初のfailureが連鎖の起点であるため先頭を残す)。
MAX_TRANSPORT_EVIDENCE_ENTRIES = 32


def transport_evidence_record(
    event: ContinuousRunEvent, sequence: int
) -> dict[str, object]:
    """transport failure 1件のbounded evidence record(JSON-ready)。

    例外class名、固定vocabulary、数値だけを持ち、server reason excerptを
    含むfree text・payload・token / Authorization値は含めない。
    """
    diagnostics = event.transport
    record: dict[str, object] = {
        "schema_id": TRANSPORT_EVIDENCE_SCHEMA_ID,
        "schema_version": TRANSPORT_EVIDENCE_SCHEMA_VERSION,
        "sequence": sequence,
        "profile": event.profile,
        "elapsed_seconds": round(event.elapsed_seconds, 3),
        "completed_games": event.completed_games,
        "failed_games": event.failed_games,
        "consecutive_failures": event.consecutive_failures,
        "exception_type": event.exception_type,
        "outcome": event.outcome,
        "transport": None,
    }
    if diagnostics is not None:
        decision = diagnostics.last_decision_elapsed_seconds
        record["transport"] = {
            "phase": diagnostics.phase,
            "operation": diagnostics.operation,
            "http_status": diagnostics.http_status,
            "close_code_received": diagnostics.close_code_received,
            "close_code_sent": diagnostics.close_code_sent,
            "server_reason_class": diagnostics.server_reason_class,
            "local_close_reason_class": diagnostics.local_close_reason_class,
            "requests_received": diagnostics.requests_received,
            "last_decision_elapsed_seconds": (
                None if decision is None else round(decision, 3)
            ),
            "timing": (
                None
                if diagnostics.timing is None
                else diagnostics.timing.to_evidence_dict()
            ),
        }
    return record


class _TransportEvidenceWriter:
    """transport failureごとに1行のJSONを`path`へ追記する(Issue #416)。

    fileは新規作成のみとし、既存fileへは追記しない。書き込みに失敗した場合は
    1回だけstderrへclass名を出して以降の記録を止める。evidenceは観測専用で
    あり、ranked runのbehaviorを変えない。
    """

    def __init__(self, path: str) -> None:
        self._path = path
        self._written = 0
        self._disabled = False

    def create(self) -> None:
        with open(self._path, "x", encoding="utf-8"):
            pass

    def write(self, event: ContinuousRunEvent) -> None:
        if self._disabled or event.kind != EVENT_TRANSPORT_FAILURE:
            return
        if self._written >= MAX_TRANSPORT_EVIDENCE_ENTRIES:
            return
        line = json.dumps(
            transport_evidence_record(event, self._written + 1),
            sort_keys=True,
            separators=(",", ":"),
        )
        try:
            with open(self._path, "a", encoding="utf-8") as evidence:
                evidence.write(line + "\n")
        except OSError as error:
            self._disabled = True
            print(
                f"transport evidence write failed: {type(error).__name__}",
                file=sys.stderr,
                flush=True,
            )
            return
        self._written += 1


@dataclass(frozen=True, slots=True)
class ContinuousRunSummary:
    """continuous runner終了時のsecret-safeな運用summary。

    token / Authorization / credential値・fingerprintは一切含まない。
    """

    profile: str
    completed_games: int
    failed_games: int
    consecutive_failures: int
    last_failure_type: str | None
    stopped_reason: str
    requested_completed_games: int | None = None
    requested_duration_seconds: int | None = None
    records_enabled: bool = False
    awaiting_original_game: bool = False
    same_bot_rejections: int = 0


async def run_continuous_ranked(
    profile: RuntimeProfile,
    token: str,
    *,
    url: str = DEFAULT_RANKED_URL,
    trace_path: str | os.PathLike | None = None,
    record_dir: str | os.PathLike | None = None,
    max_completed_games: int | None = None,
    max_duration_seconds: int | None = None,
    stop_requested: Callable[[], bool] | None = None,
    presentation: ContinuousRankedPresentationFeed | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    failure_budget: int = _FAILURE_BUDGET,
    original_game_wait_seconds: float = _ORIGINAL_GAME_WAIT_SECONDS,
    on_event: Callable[[ContinuousRunEvent], None] | None = None,
) -> ContinuousRunSummary:
    """one-game ranked primitiveを繰り返すresilient / bounded-capable loop。

    `profile` / `token` / `trace_path`はprocess開始時に一度resolveされた
    値をそのまま各one-game invocationへ渡す(同一resolved contractを維持する)。
    gameごとに`profile.policy_factory()`から新しいPolicy instanceを生成し、
    複数gameで使い回さない。

    `max_completed_games`はcompleted hanchan数を表す。Transport failureはこの
    countへ含めない。`record_dir`有効時はIssue #168のacquisitionがrecordを
    finalizeしてstrict-readbackまで成功した後だけcompletedへcountする。

    `max_duration_seconds`はrunner開始時点からのmonotonic elapsed timeによる
    boundである。cutoffは進行中のone-game primitiveをcancelせず、完了または
    failureでcontrolがrunnerへ戻った後に新しいgame / retryを開始しない。
    retry backoffよりdeadlineまでのremaining timeが短い場合はsleepをremaining
    timeへcapして、deadline到達後にretryしない。

    `record_dir`とdiagnostic traceは同時利用しない。durable record自身が
    authoritativeなper-game protocol traceを持つため、二重trace semanticsを
    このrunnerへ導入しない。

    停止要求(`stop_requested()`が`True`を返す)は次のgame開始前にだけ
    確認し、進行中のgameを中断しない。停止要求後は新しい`policy_factory()`
    を呼ばない。

    `presentation`(default `None`・opt-in、Issue #381)を渡した場合は、
    game attemptごとに`presentation.open_game()`で新しいbufferを開き、その
    one-game primitiveへだけ渡す。bufferをgame間で使い回さない。未指定時は
    one-game primitiveの呼び出し引数も含めて従来どおりである。

    retryするのは`TransportError`(`UnexpectedDisconnectError`を含む)
    hierarchyだけである。それ以外の例外(`ProtocolError`、
    `ProtocolTraceError`、durable record finalization/readback failure、
    profile/credential failure、Policy/Adapter例外、`asyncio.CancelledError`を
    含むその他unexpected exception)はcatch-allせずそのまま伝播させる。

    `on_event`(default `None`・opt-in、Issue #404)を渡した場合は、completed
    gameごとと`TransportError`ごとに`ContinuousRunEvent`を渡す。transport
    failure eventはbackoff sleepの前に渡す。未指定時はbehaviorを変えない。

    original-game wait(Issue #419): diagnosticsの`phase`が`in_game`である
    `TransportError`はordinary failureとしてcountしたうえでwait状態へ入る。
    wait中に`server_reason_class`が`same_bot_already_active`(phaseは
    `in_game`以外)の`TransportError`を受けた場合は、元の半荘がまだbotを保持
    しているとみなし、`failed_games` / `consecutive_failures`を増やさず
    (failure budgetを消費せず)、rejection数だけを数えてbounded backoffで
    retryする。wait開始から`original_game_wait_seconds`以上経過したrejectionで
    `original_game_wait_exhausted`として停止する。wait中のそれ以外の
    transport failureはordinary semanticsで扱う(wait状態は維持)。wait外の
    same-bot rejectionは特別扱いしない。gameがcompletedするとwait状態を
    clearする。duration / 停止要求はwait中も通常どおり次のretryを止める。
    """
    _validate_max_completed_games(max_completed_games)
    _validate_max_duration_seconds(max_duration_seconds)
    if (
        isinstance(original_game_wait_seconds, bool)
        or not isinstance(original_game_wait_seconds, (int, float))
        or not original_game_wait_seconds > 0
        or original_game_wait_seconds == float("inf")
    ):
        raise ValueError("original_game_wait_seconds must be a positive finite number")
    if record_dir is not None and trace_path is not None:
        raise ValueError(
            "durable record acquisition cannot be combined with trace output"
        )
    if record_dir is not None and not os.fspath(record_dir):
        raise ValueError("record_dir must be a non-empty path or None")
    if presentation is not None and not isinstance(
        presentation, ContinuousRankedPresentationFeed
    ):
        raise TypeError("presentation must be a ContinuousRankedPresentationFeed")

    completed_games = 0
    failed_games = 0
    consecutive_failures = 0
    last_failure_type: str | None = None
    awaiting_original_game = False
    original_game_wait_started = 0.0
    same_bot_rejections = 0
    total_same_bot_rejections = 0
    stopped_reason = "stop_requested"
    records_enabled = record_dir is not None
    started = monotonic()
    deadline = (
        started + max_duration_seconds if max_duration_seconds is not None else None
    )

    def emit(kind: str, **fields: object) -> None:
        if on_event is None:
            return
        on_event(
            ContinuousRunEvent(
                kind=kind,
                elapsed_seconds=monotonic() - started,
                profile=profile.name,
                completed_games=completed_games,
                failed_games=failed_games,
                consecutive_failures=consecutive_failures,
                awaiting_original_game=awaiting_original_game,
                same_bot_rejections=total_same_bot_rejections,
                **fields,
            )
        )

    async def backoff_before_retry(
        exception_type: str,
        diagnostics: TransportDiagnostics | None,
        backoff: float,
        retry_outcome: str,
    ) -> bool:
        """failure eventを出してbackoffする。deadline到達なら`False`を返す。

        remaining timeがbackoffより短い場合はremaining timeだけsleepし、
        deadline後にretryしない。
        """
        if deadline is not None:
            remaining = deadline - monotonic()
            if remaining <= 0:
                emit(
                    EVENT_TRANSPORT_FAILURE,
                    exception_type=exception_type,
                    transport=diagnostics,
                    outcome=OUTCOME_DURATION_REACHED,
                )
                return False
            if remaining < backoff:
                emit(
                    EVENT_TRANSPORT_FAILURE,
                    exception_type=exception_type,
                    transport=diagnostics,
                    backoff_seconds=remaining,
                    outcome=OUTCOME_DURATION_REACHED,
                )
                await sleep(remaining)
                return False
        emit(
            EVENT_TRANSPORT_FAILURE,
            exception_type=exception_type,
            transport=diagnostics,
            backoff_seconds=backoff,
            outcome=retry_outcome,
        )
        await sleep(backoff)
        return True

    while True:
        if max_completed_games is not None and completed_games >= max_completed_games:
            stopped_reason = "target_completed_games_reached"
            break
        if deadline is not None and monotonic() >= deadline:
            stopped_reason = "duration_reached"
            break
        if stop_requested is not None and stop_requested():
            stopped_reason = "stop_requested"
            break

        policy = profile.policy_factory()
        # presentationの有無でone-game primitiveの呼び出し引数を変えない。
        presentation_kwargs = (
            {} if presentation is None else {"presentation": presentation.open_game()}
        )
        try:
            if record_dir is None:
                await run_ranked_game(
                    policy,
                    token,
                    url=url,
                    trace_path=trace_path,
                    **presentation_kwargs,
                )
            else:
                destination = resolve_ranked_record_path(os.fspath(record_dir))
                if (
                    destination is None
                ):  # defensive: non-empty record_dir was validated above
                    raise ValueError("record_dir did not resolve to a destination")
                await _acquire_ranked_record(
                    policy,
                    token,
                    destination=destination,
                    url=url,
                    profile_identity=profile.name,
                    policy_identity=type(policy).__name__,
                    **presentation_kwargs,
                )
        except TransportError as error:
            failure_diagnostics = (
                error.diagnostics
                if isinstance(error.diagnostics, TransportDiagnostics)
                else None
            )
            in_game = (
                failure_diagnostics is not None
                and failure_diagnostics.phase == PHASE_IN_GAME
            )
            if (
                awaiting_original_game
                and not in_game
                and failure_diagnostics is not None
                and failure_diagnostics.server_reason_class
                == REASON_SAME_BOT_ALREADY_ACTIVE
            ):
                # 元の半荘がまだbot identityを保持している。failed game /
                # consecutive failureではない(failure budgetを消費しない)。
                same_bot_rejections += 1
                total_same_bot_rejections += 1
                rejection_type = type(error).__name__
                if monotonic() - original_game_wait_started >= (
                    original_game_wait_seconds
                ):
                    stopped_reason = OUTCOME_ORIGINAL_GAME_WAIT_EXHAUSTED
                    emit(
                        EVENT_TRANSPORT_FAILURE,
                        exception_type=rejection_type,
                        transport=failure_diagnostics,
                        outcome=OUTCOME_ORIGINAL_GAME_WAIT_EXHAUSTED,
                    )
                    break
                if await backoff_before_retry(
                    rejection_type,
                    failure_diagnostics,
                    _backoff_seconds(same_bot_rejections),
                    OUTCOME_AWAITING_ORIGINAL_GAME,
                ):
                    continue
                stopped_reason = OUTCOME_DURATION_REACHED
                break

            failed_games += 1
            consecutive_failures += 1
            last_failure_type = type(error).__name__
            if in_game:
                # 新しい半荘が開始済みだった。そのgameを待つwaitを始め直す。
                awaiting_original_game = True
                original_game_wait_started = monotonic()
                same_bot_rejections = 0
            if consecutive_failures >= failure_budget:
                stopped_reason = OUTCOME_FAILURE_BUDGET_EXHAUSTED
                emit(
                    EVENT_TRANSPORT_FAILURE,
                    exception_type=last_failure_type,
                    transport=failure_diagnostics,
                    outcome=OUTCOME_FAILURE_BUDGET_EXHAUSTED,
                )
                break

            if await backoff_before_retry(
                last_failure_type,
                failure_diagnostics,
                _backoff_seconds(consecutive_failures),
                OUTCOME_RETRY,
            ):
                continue
            stopped_reason = OUTCOME_DURATION_REACHED
            break

        completed_games += 1
        consecutive_failures = 0
        recovered = awaiting_original_game
        awaiting_original_game = False
        same_bot_rejections = 0
        emit(
            EVENT_GAME_COMPLETED,
            outcome=OUTCOME_RECOVERED_FROM_ORIGINAL_GAME_WAIT if recovered else None,
        )

    return ContinuousRunSummary(
        profile=profile.name,
        completed_games=completed_games,
        failed_games=failed_games,
        consecutive_failures=consecutive_failures,
        last_failure_type=last_failure_type,
        stopped_reason=stopped_reason,
        requested_completed_games=max_completed_games,
        requested_duration_seconds=max_duration_seconds,
        records_enabled=records_enabled,
        awaiting_original_game=awaiting_original_game,
        same_bot_rejections=total_same_bot_rejections,
    )


def format_continuous_summary(summary: ContinuousRunSummary) -> str:
    """token / Authorization / credential値を含まないsummary文字列を作る。"""
    requested = (
        str(summary.requested_completed_games)
        if summary.requested_completed_games is not None
        else "unbounded"
    )
    requested_duration = (
        str(summary.requested_duration_seconds)
        if summary.requested_duration_seconds is not None
        else "unbounded"
    )
    return "\n".join(
        [
            f"profile: {summary.profile}",
            f"requested completed games: {requested}",
            f"requested duration seconds: {requested_duration}",
            f"completed games: {summary.completed_games}",
            f"failed games: {summary.failed_games}",
            f"consecutive failures: {summary.consecutive_failures}",
            f"last failure type: {summary.last_failure_type or 'none'}",
            f"records: {'on' if summary.records_enabled else 'off'}",
            "awaiting original game: "
            + ("yes" if summary.awaiting_original_game else "no"),
            f"same-bot rejections: {summary.same_bot_rejections}",
            f"stopped reason: {summary.stopped_reason}",
        ]
    )


class _RunnerProgress:
    """CLIがeventから保持する最新のrunner state(terminal facts用)。"""

    def __init__(self, profile: str) -> None:
        self.profile = profile
        self.completed_games = 0
        self.failed_games = 0
        self.consecutive_failures = 0
        self.last_failure_type: str | None = None
        self.awaiting_original_game = False
        self.same_bot_rejections = 0

    def record(self, event: ContinuousRunEvent) -> None:
        self.completed_games = event.completed_games
        self.failed_games = event.failed_games
        self.consecutive_failures = event.consecutive_failures
        self.same_bot_rejections = event.same_bot_rejections
        self.awaiting_original_game = event.awaiting_original_game
        if event.exception_type is not None:
            self.last_failure_type = event.exception_type


def _print_event(progress: _RunnerProgress, event: ContinuousRunEvent) -> None:
    progress.record(event)
    print(format_continuous_event(event), file=sys.stderr, flush=True)


def _print_terminal_facts(
    progress: _RunnerProgress, error: BaseException, category: str
) -> None:
    """fail-closedな終了時、例外前までのrunner stateをsecret-safeに出力する。

    例外はclass名とcategoryだけを出し、messageは含めない。
    """
    print(
        "\n".join(
            [
                f"terminal profile: {progress.profile}",
                f"terminal completed games: {progress.completed_games}",
                f"terminal failed games: {progress.failed_games}",
                f"terminal consecutive failures: {progress.consecutive_failures}",
                f"terminal last failure type: {progress.last_failure_type or 'none'}",
                "terminal awaiting original game: "
                + ("yes" if progress.awaiting_original_game else "no"),
                f"terminal same-bot rejections: {progress.same_bot_rejections}",
                f"terminal exception type: {type(error).__name__}",
                f"terminal exception category: {category}",
                "terminal stopped reason: runner_exception",
            ]
        ),
        file=sys.stderr,
        flush=True,
    )


def _combine_stop_requested(
    stop_requested: Callable[[], bool] | None,
    stop_file: str | None,
) -> Callable[[], bool] | None:
    """injectされた停止要求と`--stop-file`の存在をORで合成する。"""
    if stop_file is None:
        return stop_requested

    def requested() -> bool:
        if stop_requested is not None and stop_requested():
            return True
        return os.path.lexists(stop_file)

    return requested


def run_continuous_ranked_cli(
    argv: Sequence[str] | None = None,
    *,
    presentation: ContinuousRankedPresentationFeed | None = None,
    stop_requested: Callable[[], bool] | None = None,
) -> int:
    """continuous ranked CLI entry point。

    `python -m lisjong_arena.riichilab.continuous_ranked`と同じprofile /
    credential解決、出力、exit codeを、live presentation consumer
    (`lisjong-play`等)が同じprocess内から再利用するためのpublic関数である
    (Issue #381)。`presentation` / `stop_requested`はそのまま
    `run_continuous_ranked()`へ渡し、未指定時の出力とbehaviorは従来どおり。

    profile / credential / trace pathはprocess開始時に一度だけresolveする。
    別profileへの暗黙fallbackは行わず、resolution failureはfail closed
    (retry loopへ入らず、non-zero exit)とする。

    `--games N` / `--duration-seconds N`未指定時はIssue #47の
    unbounded-until-stop behaviorを維持する。
    `--record-dir`有効時は各completed hanchanをIssue #168の独立durable
    recordとして保存し、diagnostic traceとの同時利用はfail closedにする。
    `--stop-file PATH`指定時はPATHの存在を停止要求として扱い、injectされた
    `stop_requested`とORで合成する。
    `--transport-evidence PATH`指定時はtransport failureごとのtiming evidenceを
    PATH(新規file)へbounded JSON Linesとして書く(Issue #416)。
    """
    parser = build_arg_parser(
        prog="python -m lisjong_arena.riichilab.continuous_ranked",
        ranked_record=True,
    )
    parser.add_argument(
        "--games",
        type=_positive_completed_games,
        default=None,
        metavar="N",
        help=(
            "N completed hanchanで正常終了する。未指定時は既存どおり"
            "stop要求までcontinuous participationを継続する"
        ),
    )
    parser.add_argument(
        "--duration-seconds",
        type=_positive_duration_seconds,
        default=None,
        metavar="N",
        help=(
            "runner開始からN秒後、新しいhanchanへrequeueせず正常終了する。"
            "cutoff時に進行中のhanchanは完了してから停止する"
        ),
    )
    parser.add_argument(
        "--stop-file",
        default=None,
        metavar="PATH",
        help=(
            "PATHが存在したら停止要求として扱う。次のhanchan開始前にだけ確認し、"
            "進行中のhanchanは完了してから停止する"
        ),
    )
    parser.add_argument(
        "--transport-evidence",
        default=None,
        metavar="PATH",
        help=(
            "transport failureごとのconnection timing evidenceをPATHへ"
            "JSON Linesで書く(新規fileのみ、最大"
            f"{MAX_TRANSPORT_EVIDENCE_ENTRIES}件)"
        ),
    )
    args = parser.parse_args(argv)
    if args.stop_file is not None and not args.stop_file:
        print("--stop-file must be a non-empty path", file=sys.stderr)
        return 2
    if args.transport_evidence is not None and not args.transport_evidence:
        print("--transport-evidence must be a non-empty path", file=sys.stderr)
        return 2

    try:
        profile = resolve_profile(args.profile)
        token = resolve_credential(profile)
    except ProfileError as error:
        print(str(error), file=sys.stderr)
        return 2

    trace_path = resolve_trace_path(
        profile, trace_flag=args.trace, trace_path_arg=args.trace_path
    )
    if args.record_dir is not None and trace_path is not None:
        print(
            "--record-dir cannot be combined with --trace, --trace-path, or "
            "RIICHILAB_TRACE_PATH",
            file=sys.stderr,
        )
        return 2

    print(f"profile: {profile.name}")
    print("mode: ranked-continuous")
    print(f"trace: {'on' if trace_path is not None else 'off'}")
    if trace_path is not None:
        print(f"trace path: {trace_path}")
    print(f"records: {'on' if args.record_dir is not None else 'off'}")
    print(f"requested completed games: {args.games or 'unbounded'}")
    print(f"requested duration seconds: {args.duration_seconds or 'unbounded'}")
    evidence_writer = None
    if args.transport_evidence is not None:
        evidence_writer = _TransportEvidenceWriter(args.transport_evidence)
        try:
            evidence_writer.create()
        except OSError as error:
            print(
                "--transport-evidence could not be created as a new file: "
                f"{type(error).__name__}",
                file=sys.stderr,
            )
            return 2

    print(f"stop file: {'on' if args.stop_file is not None else 'off'}")
    print(f"transport evidence: {'on' if evidence_writer is not None else 'off'}")

    progress = _RunnerProgress(profile.name)

    def on_event(event: ContinuousRunEvent) -> None:
        _print_event(progress, event)
        if evidence_writer is not None:
            evidence_writer.write(event)

    optional_kwargs: dict[str, object] = {"on_event": on_event}
    if presentation is not None:
        optional_kwargs["presentation"] = presentation
    effective_stop_requested = _combine_stop_requested(stop_requested, args.stop_file)
    if effective_stop_requested is not None:
        optional_kwargs["stop_requested"] = effective_stop_requested

    try:
        summary = asyncio.run(
            run_continuous_ranked(
                profile,
                token,
                trace_path=trace_path,
                record_dir=args.record_dir,
                max_completed_games=args.games,
                max_duration_seconds=args.duration_seconds,
                **optional_kwargs,
            )
        )
    except DurableRankedGameRecordError as error:
        _print_terminal_facts(progress, error, "durable_record")
        print(
            "RiichiLab continuous ranked durable record was not finalized: "
            f"{type(error).__name__}: {error}",
            file=sys.stderr,
        )
        return 1
    except OSError as error:
        if args.record_dir is None:
            _print_terminal_facts(progress, error, "unexpected")
            raise
        _print_terminal_facts(progress, error, "durable_record")
        print(
            "RiichiLab continuous ranked durable record was not finalized: "
            f"{type(error).__name__}: {error}",
            file=sys.stderr,
        )
        return 1
    except RiichiLabClientError as error:
        _print_terminal_facts(progress, error, "riichilab_client")
        print(
            "RiichiLab continuous ranked runner failed: "
            f"{type(error).__name__}: {error}",
            file=sys.stderr,
        )
        return 1
    except KeyboardInterrupt:
        print("RiichiLab continuous ranked runner stopped by user", file=sys.stderr)
        return 0
    except Exception as error:
        # Policy / Adapter等のunexpected exceptionは従来どおりtracebackで
        # 伝播させ、その前にterminal factsだけを残す。
        _print_terminal_facts(progress, error, "unexpected")
        raise

    print(format_continuous_summary(summary))
    return (
        1
        if summary.stopped_reason
        in (OUTCOME_FAILURE_BUDGET_EXHAUSTED, OUTCOME_ORIGINAL_GAME_WAIT_EXHAUSTED)
        else 0
    )


def _run_cli(argv: Sequence[str] | None = None) -> int:
    """module実行用entry point。`run_continuous_ranked_cli()`と同一実装。"""
    return run_continuous_ranked_cli(argv)


if __name__ == "__main__":
    sys.exit(_run_cli())


__all__ = [
    "CONTINUOUS_EVENT_PREFIX",
    "MAX_TRANSPORT_EVIDENCE_ENTRIES",
    "TIMING_EVENT_FIELDS",
    "TRANSPORT_EVIDENCE_SCHEMA_ID",
    "TRANSPORT_EVIDENCE_SCHEMA_VERSION",
    "ContinuousRunEvent",
    "ContinuousRunSummary",
    "format_continuous_event",
    "format_continuous_summary",
    "run_continuous_ranked",
    "run_continuous_ranked_cli",
    "transport_evidence_record",
]
