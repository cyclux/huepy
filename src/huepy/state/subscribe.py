"""Handler registration for the bridge state stream.

Reacting to a change should not require owning the loop that reads it. These
pieces let a caller register a function and get an unsubscribe token back,
while the state keeps one shared reader behind them.

Typical usage example:

    async with Hue(state=True) as hue:
        hue.state.on_change(lambda change: print(change.delta), name="Desk lamp")
        await asyncio.Event().wait()
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Self, final

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    from huepy.models.common import HueResource
    from huepy.state.records import Change, ChangeKind, Resync

# One alias covers `def handler(c)` and `async def handler(c)` alike: the union
# return type is the runtime contract, so dispatch awaits whatever is not None
# instead of inspecting the callable.
type ChangeHandler = Callable[[Change], Coroutine[Any, Any, None] | None]
type ResyncHandler = Callable[[Resync], Coroutine[Any, Any, None] | None]


@final
@dataclass(frozen=True, slots=True)
class ChangeFilter:
    """The conditions a change must meet to reach one handler.

    Every supplied field must match; unset fields do not constrain.
    """

    name: str | None = None
    model: type[HueResource] | None = None
    resource_id: str | None = None
    kind: ChangeKind | None = None

    def matches(self, change: Change, name_for: Callable[[Change], str]) -> bool:
        """Report whether ``change`` satisfies every supplied condition.

        Args:
            change: The record to test.
            name_for: Display-name resolver taking the whole record, so a
                delete resolves from what it carried rather than from a graph
                it has already been folded out of. Consulted only when ``name``
                is set, so an id-only filter costs no topology lookup.

        Returns:
            True when the change should reach the handler.

        """
        if self.resource_id is not None and change.resource_id != self.resource_id:
            return False
        if self.kind is not None and change.kind is not self.kind:
            return False
        if self.model is not None:
            # `after` first, falling back to `before`, so a delete still
            # matches the model of the resource that was removed.
            resource = change.after or change.before
            if not isinstance(resource, self.model):
                return False
        if self.name is not None:
            wanted = self.name.strip().casefold()
            return name_for(change).strip().casefold() == wanted
        return True


@final
class Subscription:
    """One registered handler, cancellable and scopeable.

    Used as a context manager it cancels on exit, which is how a handler is
    scoped to a block rather than to the client's whole lifetime.
    """

    def __init__(self, cancel: Callable[[], None]) -> None:
        """Wrap the callable that removes this registration."""
        self._cancel = cancel
        self._active = True

    @property
    def active(self) -> bool:
        """Whether this handler is still registered."""
        return self._active

    def cancel(self) -> None:
        """Stop delivering to this handler. Safe to call more than once."""
        if self._active:
            self._active = False
            self._cancel()

    def __enter__(self) -> Self:
        """Return this subscription for use inside a scoped block."""
        return self

    def __exit__(self, *_exc: object) -> None:
        """Cancel the subscription when the block ends."""
        self.cancel()
