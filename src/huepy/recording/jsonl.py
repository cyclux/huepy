"""Append-only history as one JSON object per line.

The escape hatch for questions the SQLite schema does not anticipate: greppable,
pipeable into `jq` or DuckDB, and lossless because :class:`Change` already round
trips through ``model_dump_json()``.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, TextIO, final, override

from huepy.recording._thread import SinkThread

if TYPE_CHECKING:
    from collections.abc import Sequence

    from huepy.recording.records import HistoryEntry


@final
class JSONLSink:
    """Bridge history appended to one newline-delimited JSON file."""

    def __init__(self, path: str | Path) -> None:
        """Record where to append. No file is opened until :meth:`start`."""
        self.path = Path(path)
        self._file: TextIO | None = None
        self._thread = SinkThread("huepy-jsonl")

    async def start(self) -> None:
        """Open the file for appending, creating parents as needed."""
        self._file = await self._thread.run(self._open)

    def _open(self) -> TextIO:
        """Open the append handle on the worker thread that will write it."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        return self.path.open("a", encoding="utf-8")

    async def write(self, entries: Sequence[HistoryEntry]) -> None:
        """Append one line per entry and flush the batch once.

        Raises:
            RuntimeError: If the sink was not started.

        """
        handle = self._file
        if handle is None:
            msg = f"JSONLSink({self.path}) is not started"
            raise RuntimeError(msg)
        batch = tuple(entries)

        def append() -> None:
            # Serialise on the worker too: dumping 64 entries is milliseconds
            # that would otherwise run on the event loop folding the stream.
            handle.writelines(f"{entry.model_dump_json()}\n" for entry in batch)
            handle.flush()

        await self._thread.run(append)

    async def close(self) -> None:
        """Flush and close the file, then release the worker thread."""
        handle, self._file = self._file, None
        if handle is not None:
            await self._thread.run(handle.close)
        await self._thread.close()

    @override
    def __repr__(self) -> str:
        """Name the sink by its destination."""
        return f"JSONLSink({str(self.path)!r})"
