"""The pump between one state stream and every configured sink.

The recorder holds an ordinary bounded subscriber rather than a queue of its
own. That is deliberate: when a sink cannot keep up, the subscriber overflows
and coalesces the loss into a ``Resync(LAGGED)`` marker, so the persisted
history states in its own tables exactly where and how much of itself is
missing -- the same "never silently claim complete history" rule the state
layer applies in memory.

Typical usage example:

    async with Hue(state=True, record=SQLiteSink("history.sqlite3")):
        await asyncio.Event().wait()
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Self, final

from huepy.recording.records import ChangeEntry, HistoryEntry, ResyncEntry
from huepy.state.records import Change, Resync, ResyncReason

if TYPE_CHECKING:
    from collections.abc import Sequence

    from huepy.recording.protocol import HistorySink, HistorySource

logger = logging.getLogger(__name__)

DEFAULT_BATCH_SIZE = 64
DEFAULT_FLUSH_INTERVAL = 1.0
DEFAULT_QUEUE_SIZE = 4096
# How long close() lets a sink finish before giving up on the buffered tail.
# A wedged sink must not hold the HTTP session open indefinitely.
DRAIN_TIMEOUT = 30.0
# How long close() waits for a sink to release its worker thread.
SINK_CLOSE_TIMEOUT = 10.0
# How long the pump keeps taking the backlog after close() before giving up.
# Bounded, because a producer faster than the pump would otherwise keep it
# looping until DRAIN_TIMEOUT cancelled it and the tail was lost unmarked.
STOP_DRAIN_GRACE = 5.0

UNKNOWN_NAME = "Unknown"


@dataclass(frozen=True)
class RecorderStats:
    """A point-in-time summary of what the recorder persisted and lost."""

    written: int
    batches: int
    dropped: int
    failures: int
    last_error: str | None
    last_error_at: datetime | None


@dataclass
class _Tracked:
    """One sink plus the loss the recorder still owes its history."""

    sink: HistorySink
    written: int = 0
    batches: int = 0
    dropped: int = 0
    failures: int = 0
    last_error: str | None = None
    last_error_at: datetime | None = None
    marker: Resync | None = None
    reported: bool = False

    def take_marker(self) -> Resync | None:
        """Hand over the pending loss marker, clearing it."""
        marker, self.marker = self.marker, None
        return marker

    def record_failure(
        self,
        lost: Sequence[HistoryEntry],
        exc: Exception,
        carried: Resync | None,
    ) -> None:
        """Count a dropped batch and widen the marker owed to this sink.

        Args:
            lost: The batch itself, excluding any marker prepended to it. A
                marker describes loss already counted; counting it again as a
                lost record would inflate every repeated failure.
            exc: What the sink raised.
            carried: The marker this batch was carrying, whose window and count
                must survive the failed attempt to hand it over.

        """
        self.failures += 1
        self.dropped += len(lost)
        self.last_error = f"{type(exc).__name__}: {exc}"
        self.last_error_at = datetime.now(UTC)
        now = datetime.now(UTC)
        # Coalesced, like subscriber lag: a disk that fills for ten minutes and
        # recovers should leave one honest row, not one per failed flush.
        self.marker = Resync(
            reason=ResyncReason.INCONSISTENT,
            gap_started=carried.gap_started if carried is not None else now,
            gap_ended=now,
            dropped=(carried.dropped if carried is not None else 0) + len(lost),
            detail={"source": "sink", "sink": type(self.sink).__name__},
        )


def _grace(stopping: bool, current: float | None, now: float) -> float | None:
    """Start the shutdown grace clock on the first turn spent stopping."""
    if not stopping or current is not None:
        return current
    return now + STOP_DRAIN_GRACE


def _expired(deadline: float | None, now: float) -> bool:
    """Whether a deadline is set and has passed."""
    return deadline is not None and now >= deadline


def _abandoned() -> Resync:
    """Mark history the pump could not take before shutdown gave up."""
    now = datetime.now(UTC)
    return Resync(
        reason=ResyncReason.INCONSISTENT,
        gap_started=now,
        gap_ended=now,
        # The count is genuinely unknown: the items were never pulled from the
        # subscriber. The marker's presence is the honest part.
        dropped=0,
        detail={"source": "shutdown", "note": "drain grace expired"},
    )


def _error_time(tracked: _Tracked) -> datetime:
    """Sort key for the most recent sink failure."""
    at = tracked.last_error_at
    return at if at is not None else datetime.min.replace(tzinfo=UTC)


async def _next_item(stream: AsyncIterator[Change | Resync]) -> Change | Resync | None:
    """Pull one item, returning None once the stream is exhausted."""
    return await anext(stream, None)


def _wait_timeout(
    *,
    stopping: bool,
    deadline: float | None,
    now: float,
) -> float | None:
    """How long to wait for the next item.

    Zero while stopping, so the pump takes the backlog the state broadcast on
    its way out rather than waiting on a stream that may still be live. The
    caller bounds how long that phase may last. Otherwise the time left before
    the partial batch ages out, or forever when there is no batch to age.
    """
    if stopping:
        return 0.0
    if deadline is None:
        return None
    return max(0.0, deadline - now)


@final
class Recorder:
    """Persists one state stream into every configured sink."""

    def __init__(
        self,
        state: HistorySource,
        sinks: Sequence[HistorySink],
        *,
        batch_size: int = DEFAULT_BATCH_SIZE,
        flush_interval: float = DEFAULT_FLUSH_INTERVAL,
        queue_size: int = DEFAULT_QUEUE_SIZE,
    ) -> None:
        """Bind the recorder to a state source and its sinks.

        Args:
            state: The stream and enrichment source, normally ``hue.state``.
            sinks: Destinations written in order, each isolated from the others.
            batch_size: Entries per write. A scene recall bursts 20-40 changes.
            flush_interval: Seconds before a partial batch is written anyway.
            queue_size: Bounded subscriber depth before loss is marked.

        """
        self._state = state
        self._tracked = [_Tracked(sink) for sink in sinks]
        self._batch_size = batch_size
        self._flush_interval = flush_interval
        self._queue_size = queue_size
        self._stream: AsyncIterator[Change | Resync] | None = None
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()

    @property
    def stats(self) -> RecorderStats:
        """Totals across every sink, including history known to be lost."""
        failed = [t for t in self._tracked if t.last_error_at is not None]
        latest = max(failed, key=_error_time, default=None)
        return RecorderStats(
            written=sum(t.written for t in self._tracked),
            batches=sum(t.batches for t in self._tracked),
            dropped=sum(t.dropped for t in self._tracked),
            failures=sum(t.failures for t in self._tracked),
            # From one sink, not two: reporting the first sink's message
            # beside another's timestamp describes a failure that never
            # happened.
            last_error=latest.last_error if latest is not None else None,
            last_error_at=latest.last_error_at if latest is not None else None,
        )

    async def start(self) -> None:
        """Open every sink, then subscribe and begin draining.

        A sink that cannot open raises: an unwritable path is a configuration
        bug the caller can fix now, and a recorder that silently records
        nothing is worse than a refused start. Sinks already opened are closed
        before the error propagates.

        Raises:
            RuntimeError: If the recorder is already running.

        """
        if self._task is not None:
            msg = "Recorder is already running"
            raise RuntimeError(msg)
        opened: list[HistorySink] = []
        try:
            for tracked in self._tracked:
                # Recorded *before* the await: a sink that raises part-way
                # through start() may already hold a thread or a handle, and
                # would otherwise never be closed.
                opened.append(tracked.sink)
                await tracked.sink.start()
        except BaseException:
            for sink in reversed(opened):
                with suppress(Exception):
                    await sink.close()
            raise
        # `changes()` registers on call, not on first advance, so nothing
        # published between here and the first loop turn is missed.
        self._stopping.clear()
        stream = self._state.changes(maxsize=self._queue_size)
        self._stream = stream
        self._task = asyncio.create_task(self._run(stream), name="huepy-recorder")

    async def close(self) -> None:
        """Drain what is buffered, flush every sink, then release them."""
        self._stopping.set()
        task, self._task = self._task, None
        try:
            if task is not None:
                await self._drain_task(task)
        finally:
            # In a `finally`: a stream that ended in a terminal observer error
            # re-raises out of the drain, and letting that skip the release
            # below would leak a sqlite connection, a file handle and a worker
            # thread on the one path where the client is shutting down anyway.
            stream, self._stream = self._stream, None
            if isinstance(stream, AsyncGenerator):
                with suppress(Exception):
                    await stream.aclose()
            for tracked in self._tracked:
                try:
                    # Bounded: a blocking sink's close() waits on its worker
                    # thread, so a hung filesystem would otherwise hold the
                    # HTTP session open as long as the write takes.
                    await asyncio.wait_for(
                        tracked.sink.close(), timeout=SINK_CLOSE_TIMEOUT
                    )
                except TimeoutError:
                    # Only the await is bounded. The sink's worker thread keeps
                    # running and is non-daemon, so it can still delay
                    # interpreter exit; what this buys is releasing the HTTP
                    # session now instead of whenever the disk responds.
                    logger.error(  # noqa: TRY400 - the timeout is the whole story
                        "Sink %r did not close within %ss; leaving its worker running",
                        tracked.sink,
                        SINK_CLOSE_TIMEOUT,
                    )
                except Exception:
                    logger.exception("Sink %r failed to close", tracked.sink)

    @staticmethod
    async def _drain_task(task: asyncio.Task[None]) -> None:
        """Wait for the pump to finish, bounding a sink that never returns."""
        try:
            await asyncio.wait_for(task, timeout=DRAIN_TIMEOUT)
        except TimeoutError:
            # A wedged sink must not hold the transport open forever.
            logger.exception("Recorder did not drain in time; cancelling")
            _ = task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        except asyncio.CancelledError:
            raise
        except Exception:
            # The stream itself failed. Nothing left to record, and shutdown
            # must continue; `hue.state.ensure_healthy()` re-raises the cause.
            logger.exception("Recorder stream stopped with an error")

    async def __aenter__(self) -> Self:
        """Start recording."""
        await self.start()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        """Stop recording, flushing whatever is buffered."""
        await self.close()

    async def _run(self, stream: AsyncIterator[Change | Resync]) -> None:
        """Batch the stream by size or age, flushing to every sink.

        Args:
            stream: The subscriber to drain, passed in rather than read from
                the instance so the loop has no un-startable state to guard.

        """
        loop = asyncio.get_running_loop()
        buffer: list[HistoryEntry] = []
        deadline: float | None = None
        stop_deadline: float | None = None
        # The pull lives in its own task so a flush timeout never cancels it
        # mid-await: cancelling inside `changes()` would unwind its `finally`,
        # deregister the subscriber and lose an item.
        pending = asyncio.ensure_future(_next_item(stream))
        stop = asyncio.ensure_future(self._stopping.wait())
        aborted = False
        try:
            while True:
                # Once stopping, take only what the subscriber already holds:
                # `Hue.close()` closes the state first, which broadcasts the
                # tail plus `_CLOSED` into our queue, and breaking on the stop
                # event alone would abandon it and record nothing to say so.
                # A zero timeout drains that backlog without waiting on a
                # stream that may still be live under a standalone close().
                stopping = stop.done()
                stop_deadline = _grace(stopping, stop_deadline, loop.time())
                timeout = _wait_timeout(
                    stopping=stopping, deadline=deadline, now=loop.time()
                )
                done, _ = await asyncio.wait(
                    {pending, stop},
                    timeout=timeout,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if pending in done:
                    item = pending.result()
                    if item is None:
                        break
                    buffer.append(self._enrich(item))
                    pending = asyncio.ensure_future(_next_item(stream))
                    if deadline is None:
                        deadline = loop.time() + self._flush_interval
                    if len(buffer) < self._batch_size and not _expired(
                        stop_deadline, loop.time()
                    ):
                        continue
                elif stopping:
                    break
                if _expired(stop_deadline, loop.time()):
                    # The producer is outrunning the drain. Give up, but say so
                    # -- an archive that stops mid-stream while claiming to be
                    # complete is the failure this layer exists to prevent.
                    await self._flush([*buffer, ResyncEntry(resync=_abandoned())])
                    buffer.clear()
                    break
                await self._flush(buffer)
                buffer.clear()
                deadline = None
        except asyncio.CancelledError:
            # close() gave up on a sink that never returned. Flushing the tail
            # here would call that same sink and block forever, defeating the
            # timeout that got us here.
            aborted = True
            raise
        finally:
            _ = pending.cancel()
            _ = stop.cancel()
            with suppress(asyncio.CancelledError):
                await pending
            with suppress(asyncio.CancelledError):
                await stop
            if not aborted:
                await self._flush(buffer)

    def _enrich(self, item: Change | Resync) -> HistoryEntry:
        """Resolve topology for a change; markers carry none to resolve."""
        if isinstance(item, Resync):
            return ResyncEntry(resync=item)
        try:
            context = self._state.describe(item)
        except Exception:
            # Enrichment is a convenience; losing the row would not be.
            logger.exception("Could not resolve topology for %s", item.resource_id)
            return ChangeEntry(change=item, name=UNKNOWN_NAME, room=None)
        room = context.room
        return ChangeEntry(
            change=item,
            name=context.name,
            room=room.name if room is not None else None,
        )

    async def _flush(self, buffer: Sequence[HistoryEntry]) -> None:
        """Write one batch to every sink, isolating each sink's failures."""
        if not buffer:
            return
        batch = tuple(buffer)
        for tracked in self._tracked:
            carried = tracked.take_marker()
            entries = (
                batch if carried is None else (ResyncEntry(resync=carried), *batch)
            )
            try:
                await tracked.sink.write(entries)
            except asyncio.CancelledError:
                # Put the marker back: the gap it describes is still unrecorded,
                # and every other failure path is careful to preserve it.
                tracked.marker = carried
                raise
            except Exception as exc:  # noqa: BLE001 - a sink never stops tracking
                self._report(tracked, batch, exc, carried)
            else:
                tracked.written += len(entries)
                tracked.batches += 1
                if tracked.reported:
                    tracked.reported = False
                    logger.info("Sink %r is writing again", tracked.sink)

    @staticmethod
    def _report(
        tracked: _Tracked,
        lost: Sequence[HistoryEntry],
        exc: Exception,
        carried: Resync | None,
    ) -> None:
        """Log a dropped batch once per outage, then count it."""
        if not tracked.reported:
            tracked.reported = True
            logger.error(
                "Sink %r failed; dropping the batch",
                tracked.sink,
                exc_info=exc,
            )
        else:
            # Flushes are capped near one a second, but 86,000 tracebacks a day
            # is not a diagnostic.
            logger.error("Sink %r still failing: %s", tracked.sink, exc)
        tracked.record_failure(lost, exc, carried)
