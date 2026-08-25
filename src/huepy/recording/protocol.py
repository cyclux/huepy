"""The seams the recording layer is built on.

Both are structural: :class:`~huepy.state.HueState` satisfies
:class:`HistorySource` without knowing this package exists, which is what keeps
the import graph acyclic and makes every recorder test a ~30-line fake.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Sequence

    from huepy.recording.records import HistoryEntry
    from huepy.state.records import Change, ChangeContext, Resync


@runtime_checkable
class HistorySink(Protocol):
    """A destination for enriched bridge history.

    Implementations own their own blocking work. The recorder awaits
    :meth:`write` on the event loop that services the bridge event stream, so a
    sink that touches a disk must hand that work to a thread of its own.
    """

    async def start(self) -> None:
        """Acquire resources, failing loudly if the destination is unusable."""
        ...

    async def write(self, entries: Sequence[HistoryEntry]) -> None:
        """Durably append one batch, in order."""
        ...

    async def close(self) -> None:
        """Release resources. Safe to call more than once."""
        ...


class HistorySource(Protocol):
    """The state operations a recorder needs: a stream and enrichment."""

    def changes(self, *, maxsize: int = ...) -> AsyncGenerator[Change | Resync]:
        """Yield an isolated, bounded stream of changes and markers."""
        ...

    def describe(self, change: Change) -> ChangeContext:
        """Resolve the display name and containing room for one change."""
        ...
