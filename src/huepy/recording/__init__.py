"""Configurable, durable history for the bridge state stream.

Recording is one constructor argument. Sinks receive enriched, self-contained
records and own their own blocking work, so the event loop that folds the SSE
stream is never waiting on a disk.

Typical usage example:

    from huepy import Hue
    from huepy.recording import SQLiteSink

    async with Hue(state=True, record=SQLiteSink("hue-history.sqlite3")):
        await asyncio.Event().wait()
"""

from huepy.recording.jsonl import JSONLSink
from huepy.recording.log import LoggingSink
from huepy.recording.protocol import HistorySink, HistorySource
from huepy.recording.recorder import Recorder, RecorderStats
from huepy.recording.records import ChangeEntry, HistoryEntry, ResyncEntry
from huepy.recording.sqlite import SQLiteSink

__all__ = [
    "ChangeEntry",
    "HistoryEntry",
    "HistorySink",
    "HistorySource",
    "JSONLSink",
    "LoggingSink",
    "Recorder",
    "RecorderStats",
    "ResyncEntry",
    "SQLiteSink",
]
