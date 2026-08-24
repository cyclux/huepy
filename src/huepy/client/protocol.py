"""Transport-neutral client protocols and event/write records.

Resource handlers need a transport and nothing else. Depending on this
protocol rather than on the concrete :class:`~huepy.client.base.Hue` keeps the
import graph acyclic: ``client.base`` imports ``resources``, and ``resources``
must therefore not import ``client.base`` back.
"""

from collections.abc import AsyncGenerator, AsyncIterator, Callable
from dataclasses import dataclass
from datetime import datetime
from types import TracebackType
from typing import Any, ClassVar, Literal, Protocol, runtime_checkable
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, JsonValue


@dataclass(frozen=True)
class SSEFrame:
    """One complete server-sent event frame from the bridge."""

    event_id: str | None
    received_at: datetime
    events: list[dict[str, Any]]


@dataclass(frozen=True)
class EventConnection:
    """One established event-stream connection and its frame iterator."""

    opened_at: datetime
    resumed_from: str | None
    frames: AsyncIterator[SSEFrame]


class PendingWrite(BaseModel):
    """One observed PUT and its current transport outcome."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    command_id: UUID
    path: str
    payload: dict[str, Any]
    sent_at: AwareDatetime
    completed_at: AwareDatetime | None = None
    status: Literal["pending", "accepted", "rejected", "unknown"] = "pending"


WriteObserver = Callable[[PendingWrite], None]


@runtime_checkable
class Transport(Protocol):
    """The HTTP and event-stream operations used by the public client."""

    async def get(self, path: str) -> JsonValue:
        """Send a GET request and return the decoded body."""
        ...

    async def put(self, path: str, data: dict[str, Any]) -> JsonValue:
        """Send a PUT request and return the decoded body."""
        ...

    async def post(self, path: str, data: dict[str, Any]) -> JsonValue:
        """Send a POST request and return the decoded body."""
        ...

    async def delete(self, path: str) -> JsonValue:
        """Send a DELETE request and return the decoded body."""
        ...

    async def authenticate(
        self,
        app_name: str = ...,
        timeout: int = ...,  # noqa: ASYNC109 - public API exposes caller-configured timeout
    ) -> str:
        """Obtain an application key from the bridge."""
        ...

    def subscribe_events(
        self,
        *,
        max_retries: int | None = ...,
    ) -> AsyncGenerator[dict[str, Any]]:
        """Yield individual event dictionaries across reconnects."""
        ...

    def subscribe_event_frames(
        self,
        *,
        max_retries: int | None = ...,
    ) -> AsyncGenerator[SSEFrame]:
        """Yield complete event-stream frames across reconnects."""
        ...

    def event_connections(
        self,
        *,
        max_retries: int | None = ...,
    ) -> AsyncGenerator[EventConnection]:
        """Yield established event-stream connections."""
        ...

    def add_write_observer(self, observer: WriteObserver) -> Callable[[], None]:
        """Observe PUT lifecycle records until the returned callback is called."""
        ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Release the transport's resources."""
        ...


class HueClient(Protocol):
    """A client that can issue requests to a bridge."""

    @property
    def http(self) -> Transport:
        """The open transport."""
        ...
