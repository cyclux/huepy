"""History routed to a caller-supplied :mod:`logging` logger.

The "or not" answer to "into a DB or not". huepy attaches only a
``NullHandler`` to its own logger and this sink keeps that stance: it takes the
logger and level from the caller and installs no handler, so a library never
decides the shape of a host application's logs.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, final, override

from huepy.recording.records import ChangeEntry

if TYPE_CHECKING:
    from collections.abc import Sequence

    from huepy.recording.records import HistoryEntry


@final
class LoggingSink:
    """Bridge history emitted as one log record per entry.

    Unlike the file sinks, this one does no work off the event loop, because
    the handlers it dispatches to belong to the caller: a file or network
    handler will block the loop that folds the event stream for the length of
    every batch. Attach a ``QueueHandler`` if that matters.
    """

    def __init__(
        self,
        logger: logging.Logger | None = None,
        level: int = logging.INFO,
    ) -> None:
        """Bind the sink to a logger and level.

        Args:
            logger: Where to emit. Defaults to ``huepy.recording.log``.
            level: The level each entry is emitted at.

        """
        self.logger = logger if logger is not None else logging.getLogger(__name__)
        self.level = level

    async def start(self) -> None:
        """Nothing to acquire; a logger is always usable."""

    async def write(self, entries: Sequence[HistoryEntry]) -> None:
        """Emit one record per entry, lazily formatted."""
        for entry in entries:
            if isinstance(entry, ChangeEntry):
                self.logger.log(
                    self.level,
                    "%s %s %s %s",
                    entry.change.at.isoformat(),
                    entry.name,
                    entry.change.kind,
                    entry.change.delta,
                )
            else:
                self.logger.log(
                    self.level,
                    "%s gap %s (%s dropped)",
                    entry.resync.gap_ended.isoformat(),
                    entry.resync.reason,
                    entry.resync.dropped,
                )

    async def close(self) -> None:
        """Nothing to release; the logger belongs to the caller."""

    @override
    def __repr__(self) -> str:
        """Name the sink by the logger it writes to."""
        return f"LoggingSink({self.logger.name!r})"
