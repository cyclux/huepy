"""Contract tests for the recording layer.

Sinks are faked at the Protocol, like the transport is, and the two shipped
file sinks run against real files in `tmp_path` -- `sqlite3` is stdlib, not the
I/O these tests isolate from.
"""

import asyncio
import json
import logging
import sqlite3
from collections.abc import AsyncGenerator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, override
from zoneinfo import ZoneInfo

import pytest

from huepy import models
from huepy.recording import (
    ChangeEntry,
    HistoryEntry,
    JSONLSink,
    LoggingSink,
    Recorder,
    ResyncEntry,
    SQLiteSink,
)
from huepy.state.records import (
    Change,
    ChangeContext,
    ChangeKind,
    Resync,
    ResyncReason,
)

AT = datetime(2026, 8, 24, 18, 30, tzinfo=UTC)


def light(brightness: float, *, on: bool = True) -> models.Light:
    """One light resource, detached like the ones a Change carries."""
    return models.Light.model_validate(
        {
            "id": "light-1",
            "type": "light",
            "metadata": {"name": "Desk lamp"},
            "on": {"on": on},
            "dimming": {"brightness": brightness},
        }
    )


def change(
    brightness: float = 40.0,
    *,
    on: bool = True,
    kind: ChangeKind = ChangeKind.UPDATE,
    at: datetime = AT,
) -> Change:
    """One complete transition, ready to enrich."""
    return Change(
        kind=kind,
        received_at=at,
        observed_at=at,
        resource_id="light-1",
        resource_type="light",
        before=light(10.0),
        after=None if kind is ChangeKind.DELETE else light(brightness, on=on),
        delta={"dimming": {"brightness": brightness}},
    )


def marker(dropped: int = 3) -> Resync:
    """One continuity marker."""
    return Resync(
        reason=ResyncReason.LAGGED,
        gap_started=AT,
        gap_ended=AT + timedelta(seconds=5),
        dropped=dropped,
    )


class FakeState:
    """The `HistorySource` seam: a queue-backed stream plus enrichment."""

    def __init__(self) -> None:
        self.queue: asyncio.Queue[Change | Resync | BaseException | None] = (
            asyncio.Queue()
        )
        self.describe_calls = 0
        self.room: models.Room | None = None

    def changes(self, *, maxsize: int = 4096) -> AsyncGenerator[Change | Resync]:
        del maxsize
        return self._drain()

    async def _drain(self) -> AsyncGenerator[Change | Resync]:
        while True:
            item = await self.queue.get()
            if item is None:
                return
            if isinstance(item, BaseException):
                raise item
            yield item

    def describe(self, change: Change) -> ChangeContext:
        self.describe_calls += 1
        return ChangeContext(change=change, name="Desk lamp", room=self.room)


class FakeSink:
    """Collects batches, and can be told to fail."""

    def __init__(self, *, fail_writes: int = 0, fail_start: bool = False) -> None:
        self.batches: list[tuple[HistoryEntry, ...]] = []
        self.events: list[str] = []
        self.fail_writes = fail_writes
        self.fail_start = fail_start

    async def start(self) -> None:
        self.events.append("start")
        if self.fail_start:
            msg = "cannot open"
            raise RuntimeError(msg)

    async def write(self, entries: Sequence[HistoryEntry]) -> None:
        if self.fail_writes > 0:
            self.fail_writes -= 1
            msg = "disk is full"
            raise RuntimeError(msg)
        self.batches.append(tuple(entries))

    async def close(self) -> None:
        self.events.append("close")

    @property
    def entries(self) -> list[HistoryEntry]:
        return [entry for batch in self.batches for entry in batch]


@pytest.fixture
def state() -> FakeState:
    return FakeState()


async def settle() -> None:
    """Give the recorder's pump turns until it has caught up."""
    for _ in range(20):
        await asyncio.sleep(0)


class TestBatching:
    async def test_flushes_when_the_batch_fills(self, state: FakeState) -> None:
        sink = FakeSink()
        async with Recorder(state, [sink], batch_size=2, flush_interval=60):
            for _ in range(4):
                state.queue.put_nowait(change())
            await settle()

        assert [len(batch) for batch in sink.batches] == [2, 2]

    async def test_flushes_a_partial_batch_on_the_interval(
        self, state: FakeState
    ) -> None:
        sink = FakeSink()
        async with Recorder(state, [sink], batch_size=100, flush_interval=0.01):
            state.queue.put_nowait(change())
            await asyncio.sleep(0.1)

        assert len(sink.entries) == 1

    async def test_close_flushes_the_buffered_tail(self, state: FakeState) -> None:
        """A partial batch at shutdown must not be dropped."""
        sink = FakeSink()
        async with Recorder(state, [sink], batch_size=100, flush_interval=60):
            state.queue.put_nowait(change())
            await settle()

        assert len(sink.entries) == 1


class TestEnrichment:
    async def test_changes_are_enriched_and_markers_are_not(
        self, state: FakeState
    ) -> None:
        sink = FakeSink()
        state.room = models.Room.model_validate(
            {"id": "room-1", "type": "room", "metadata": {"name": "Study"}}
        )
        async with Recorder(state, [sink], batch_size=2, flush_interval=60):
            state.queue.put_nowait(change())
            state.queue.put_nowait(marker())
            await settle()

        entries = sink.entries
        assert isinstance(entries[0], ChangeEntry)
        assert entries[0].name == "Desk lamp"
        assert entries[0].room == "Study"
        assert isinstance(entries[1], ResyncEntry)
        assert state.describe_calls == 1


class TestFailureSemantics:
    async def test_a_failing_sink_does_not_stop_the_others(
        self, state: FakeState
    ) -> None:
        broken = FakeSink(fail_writes=1)
        healthy = FakeSink()
        async with Recorder(state, [broken, healthy], batch_size=1, flush_interval=60):
            state.queue.put_nowait(change())
            await settle()

        assert healthy.entries
        assert broken.batches == []

    async def test_dropped_history_is_marked_in_the_next_batch(
        self, state: FakeState
    ) -> None:
        """The archive must say where and how much of itself is missing."""
        sink = FakeSink(fail_writes=1)
        recorder = Recorder(state, [sink], batch_size=1, flush_interval=60)
        async with recorder:
            state.queue.put_nowait(change())
            await settle()
            state.queue.put_nowait(change())
            await settle()

        first = sink.batches[0]
        assert isinstance(first[0], ResyncEntry)
        assert first[0].resync.reason is ResyncReason.INCONSISTENT
        assert first[0].resync.detail is not None
        assert first[0].resync.detail["source"] == "sink"
        assert first[0].resync.dropped == 1
        assert recorder.stats.dropped == 1
        assert recorder.stats.failures == 1

    async def test_repeated_failures_coalesce_into_one_marker(
        self, state: FakeState
    ) -> None:
        sink = FakeSink(fail_writes=3)
        recorder = Recorder(state, [sink], batch_size=1, flush_interval=60)
        async with recorder:
            for _ in range(4):
                state.queue.put_nowait(change())
                await settle()

        markers = [e for e in sink.entries if isinstance(e, ResyncEntry)]
        assert len(markers) == 1
        assert markers[0].resync.dropped == 3

    async def test_start_failure_closes_sinks_already_opened(
        self, state: FakeState
    ) -> None:
        healthy = FakeSink()
        broken = FakeSink(fail_start=True)
        recorder = Recorder(state, [healthy, broken])

        with pytest.raises(RuntimeError, match="cannot open"):
            await recorder.start()

        assert healthy.events == ["start", "close"]

    async def test_enrichment_failure_still_records_the_change(
        self, state: FakeState, caplog: Any
    ) -> None:
        """Losing the name is a nuisance; losing the row is not acceptable."""
        sink = FakeSink()

        def boom(change: Change) -> ChangeContext:
            del change
            msg = "topology exploded"
            raise RuntimeError(msg)

        state.describe = boom
        async with Recorder(state, [sink], batch_size=1, flush_interval=60):
            state.queue.put_nowait(change())
            await settle()

        entry = sink.entries[0]
        assert isinstance(entry, ChangeEntry)
        assert entry.name == "Unknown"
        assert "topology exploded" in caplog.text


class TestSQLiteSink:
    async def test_schema_is_idempotent_across_reopen(self, tmp_path: Path) -> None:
        path = tmp_path / "history.sqlite3"
        for _ in range(2):
            sink = SQLiteSink(path)
            await sink.start()
            await sink.close()
        assert path.is_file()

    async def test_a_future_schema_version_refuses_to_open(
        self, tmp_path: Path
    ) -> None:
        """Never corrupt a newer format's data by writing the old shape."""
        path = tmp_path / "history.sqlite3"
        sink = SQLiteSink(path)
        await sink.start()
        await sink.close()
        connection = sqlite3.connect(path)
        _ = connection.execute("UPDATE meta SET value = '99'")
        connection.commit()
        connection.close()

        with pytest.raises(RuntimeError, match="schema version 99"):
            await SQLiteSink(path).start()

    async def test_rows_round_trip_and_answer_the_documented_queries(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "history.sqlite3"
        sink = SQLiteSink(path)
        await sink.start()
        await sink.write(
            [
                ChangeEntry(change=change(30.0), name="Desk lamp", room="Study"),
                ResyncEntry(resync=marker()),
            ]
        )
        await sink.close()

        connection = sqlite3.connect(path)
        try:
            last_on = """
                SELECT at FROM change
                WHERE name = ? AND on_state = 1
                ORDER BY at DESC LIMIT 1
            """
            row = connection.execute(last_on, ("Desk lamp",)).fetchone()
            assert row is not None

            payload = connection.execute("SELECT payload FROM change").fetchone()[0]
            assert Change.model_validate_json(payload).delta == {
                "dimming": {"brightness": 30.0}
            }

            gaps = connection.execute("SELECT reason, dropped FROM resync").fetchall()
            assert gaps == [("lagged", 3)]

            current = connection.execute(
                "SELECT resource_id, name, on_state, brightness FROM current"
            ).fetchall()
            assert current == [("light-1", "Desk lamp", 1, 30.0)]
        finally:
            connection.close()

    async def test_current_tracks_the_latest_value_and_a_delete(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "history.sqlite3"
        sink = SQLiteSink(path)
        await sink.start()
        await sink.write([ChangeEntry(change=change(30.0), name="Desk lamp")])
        await sink.write([ChangeEntry(change=change(80.0), name="Desk lamp")])

        connection = sqlite3.connect(path)
        try:
            assert connection.execute("SELECT brightness FROM current").fetchone() == (
                80.0,
            )
            assert connection.execute("SELECT count(*) FROM change").fetchone() == (2,)

            await sink.write(
                [ChangeEntry(change=change(kind=ChangeKind.DELETE), name="Desk lamp")]
            )
            assert connection.execute("SELECT count(*) FROM current").fetchone() == (0,)
        finally:
            connection.close()
            await sink.close()

    async def test_timestamps_normalise_so_string_order_is_time_order(
        self, tmp_path: Path
    ) -> None:
        """Lexicographic order must equal chronological order across offsets."""
        path = tmp_path / "history.sqlite3"
        sink = SQLiteSink(path)
        await sink.start()
        berlin = datetime(2026, 8, 24, 20, 0, tzinfo=UTC).astimezone(
            ZoneInfo("Europe/Berlin")
        )
        await sink.write(
            [
                ChangeEntry(
                    change=change(at=datetime(2026, 8, 24, 19, 0, tzinfo=UTC)), name="a"
                ),
                ChangeEntry(change=change(at=berlin), name="b"),
            ]
        )
        await sink.close()

        connection = sqlite3.connect(path)
        try:
            order = [
                row[0]
                for row in connection.execute("SELECT name FROM change ORDER BY at")
            ]
        finally:
            connection.close()
        assert order == ["a", "b"]


class TestJSONLSink:
    async def test_one_json_object_per_line_with_a_discriminator(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "history.jsonl"
        sink = JSONLSink(path)
        await sink.start()
        await sink.write(
            [
                ChangeEntry(change=change(), name="Desk lamp"),
                ResyncEntry(resync=marker()),
            ]
        )
        await sink.close()

        lines = [json.loads(line) for line in path.read_text().splitlines()]
        assert [line["record"] for line in lines] == ["change", "resync"]

    async def test_appends_across_a_restart(self, tmp_path: Path) -> None:
        path = tmp_path / "history.jsonl"
        for _ in range(2):
            sink = JSONLSink(path)
            await sink.start()
            await sink.write([ChangeEntry(change=change(), name="Desk lamp")])
            await sink.close()

        assert len(path.read_text().splitlines()) == 2

    async def test_writing_before_start_is_an_error(self, tmp_path: Path) -> None:
        with pytest.raises(RuntimeError, match="not started"):
            await JSONLSink(tmp_path / "h.jsonl").write([])


class TestLoggingSink:
    async def test_emits_one_record_per_entry(self, caplog: Any) -> None:
        logger = logging.getLogger("test.huepy.recording")
        sink = LoggingSink(logger, level=logging.INFO)
        await sink.start()
        with caplog.at_level(logging.INFO, logger="test.huepy.recording"):
            await sink.write(
                [
                    ChangeEntry(change=change(), name="Desk lamp"),
                    ResyncEntry(resync=marker()),
                ]
            )
        await sink.close()

        assert "Desk lamp" in caplog.text
        assert "gap lagged" in caplog.text


class TestWedgedSink:
    async def test_close_gives_up_rather_than_hanging_forever(
        self, state: FakeState, monkeypatch: Any, caplog: Any
    ) -> None:
        """A sink that never returns must not hold the transport open."""
        monkeypatch.setattr("huepy.recording.recorder.DRAIN_TIMEOUT", 0.05)
        started = asyncio.Event()

        class WedgedSink(FakeSink):
            @override
            async def write(self, entries: Sequence[HistoryEntry]) -> None:
                started.set()
                await asyncio.Event().wait()

        sink = WedgedSink()
        recorder = Recorder(state, [sink], batch_size=1, flush_interval=60)
        await recorder.start()
        state.queue.put_nowait(change())
        await asyncio.wait_for(started.wait(), 1)

        await asyncio.wait_for(recorder.close(), 5)

        assert "did not drain in time" in caplog.text
        assert sink.events[-1] == "close"


class TestRestart:
    async def test_a_sink_survives_a_close_and_start_cycle(
        self, tmp_path: Path
    ) -> None:
        """`HueState` is re-enterable, so `record=` must not be the exception."""
        path = tmp_path / "history.sqlite3"
        sink = SQLiteSink(path)
        for _ in range(2):
            await sink.start()
            await sink.write([ChangeEntry(change=change(), name="Desk lamp")])
            await sink.close()

        connection = sqlite3.connect(path)
        try:
            assert connection.execute("SELECT count(*) FROM change").fetchone() == (2,)
        finally:
            connection.close()

    async def test_jsonl_sink_survives_a_restart(self, tmp_path: Path) -> None:
        path = tmp_path / "history.jsonl"
        sink = JSONLSink(path)
        for _ in range(2):
            await sink.start()
            await sink.write([ChangeEntry(change=change(), name="Desk lamp")])
            await sink.close()

        assert len(path.read_text().splitlines()) == 2

    async def test_a_sink_that_fails_to_open_is_still_closed(
        self, state: FakeState, tmp_path: Path
    ) -> None:
        """A part-way start() may already hold a thread; it must be released."""
        path = tmp_path / "history.sqlite3"
        await SQLiteSink(path).start()
        connection = sqlite3.connect(path)
        _ = connection.execute("UPDATE meta SET value = '99'")
        connection.commit()
        connection.close()

        sink = SQLiteSink(path)
        recorder = Recorder(state, [sink])
        with pytest.raises(RuntimeError, match="schema version 99"):
            await recorder.start()

        # Closing an already-released sink must be safe, which is what proves
        # the failed one was reached by the cleanup at all.
        await sink.close()


class TestCurrentOrdering:
    async def test_an_out_of_order_row_does_not_rewind_current(
        self, tmp_path: Path
    ) -> None:
        """`current` is documented as authoritative; it must not go backwards."""
        path = tmp_path / "history.sqlite3"
        sink = SQLiteSink(path)
        await sink.start()
        later = datetime(2026, 8, 24, 20, 0, tzinfo=UTC)
        earlier = datetime(2026, 8, 24, 18, 0, tzinfo=UTC)
        await sink.write([ChangeEntry(change=change(90.0, at=later), name="Desk")])
        await sink.write([ChangeEntry(change=change(10.0, at=earlier), name="Desk")])
        await sink.close()

        connection = sqlite3.connect(path)
        try:
            assert connection.execute("SELECT brightness FROM current").fetchone() == (
                90.0,
            )
            # The history itself keeps both rows; only `current` is guarded.
            assert connection.execute("SELECT count(*) FROM change").fetchone() == (2,)
        finally:
            connection.close()


class TestTerminalStream:
    async def test_close_releases_sinks_when_the_stream_fails(
        self, state: FakeState, caplog: Any
    ) -> None:
        """A failed drain must not leak the connection, handle and thread."""
        sink = FakeSink()
        recorder = Recorder(state, [sink], batch_size=1, flush_interval=60)
        await recorder.start()
        state.queue.put_nowait(RuntimeError("event observer stopped"))
        await settle()

        await recorder.close()

        assert sink.events == ["start", "close"]
        assert "stream stopped with an error" in caplog.text
