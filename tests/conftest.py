"""Shared fixtures for the huepy test suite.

Resource tests exercise request *shaping* -- which path is hit, what payload is
sent, and how the response parses -- so they run against an in-memory stand-in
for the HTTP client rather than a socket. Responses are shaped like real v2
bodies (``{"errors": [...], "data": [...]}``) so the envelope handling is
exercised too.
"""

from collections.abc import AsyncGenerator, Callable
from datetime import UTC, datetime
from types import TracebackType
from typing import Any, Self
from uuid import uuid4

import pytest

from huepy.client.base import Hue
from huepy.client.http import EventConnection, PendingWrite, SSEFrame, WriteObserver

Call = tuple[str, str, dict[str, Any] | None]

RESOURCE_ROOT = "/clip/v2/resource"


def envelope(*data: dict[str, Any], errors: list[str] | None = None) -> dict[str, Any]:
    """Build a v2 response body around the given resources."""
    return {
        "errors": [{"description": e} for e in (errors or [])],
        "data": list(data),
    }


class FakeHttp:
    """Records every request and replays canned responses."""

    def __init__(self) -> None:
        self.calls: list[Call] = []
        self._responses: dict[str, Any] = {}
        self.write_result: Any = envelope({"rid": "updated-id", "rtype": "light"})
        self.closed = False
        self.events: list[dict[str, Any]] = []
        self._write_observers: set[WriteObserver] = set()

    def queue(self, path: str, payload: Any) -> None:
        """Register the body returned for a GET on ``path``."""
        self._responses[path] = payload

    def queue_resource(
        self,
        resource_type: str,
        resource_id: str,
        body: dict[str, Any],
    ) -> None:
        """Register a single-resource GET."""
        self.queue(f"{RESOURCE_ROOT}/{resource_type}/{resource_id}", envelope(body))

    def queue_collection(
        self,
        resource_type: str,
        bodies: list[dict[str, Any]],
    ) -> None:
        """Register a collection GET."""
        self.queue(f"{RESOURCE_ROOT}/{resource_type}", envelope(*bodies))

    async def get(self, path: str) -> Any:
        self.calls.append(("GET", path, None))
        return self._responses.get(path, envelope())

    async def put(self, path: str, data: dict[str, Any]) -> Any:
        self.calls.append(("PUT", path, data))
        write = PendingWrite(
            command_id=uuid4(),
            path=path,
            payload=data,
            sent_at=datetime.now(UTC),
        )
        for observer in tuple(self._write_observers):
            observer(write.model_copy(deep=True))
        completed = write.model_copy(
            update={
                "completed_at": datetime.now(UTC),
                "status": "accepted",
            },
            deep=True,
        )
        for observer in tuple(self._write_observers):
            observer(completed.model_copy(deep=True))
        return self.write_result

    async def post(self, path: str, data: dict[str, Any]) -> Any:
        self.calls.append(("POST", path, data))
        return self.write_result

    async def delete(self, path: str) -> Any:
        self.calls.append(("DELETE", path, None))
        return self.write_result

    async def authenticate(
        self,
        app_name: str = "huepy",
        timeout: int = 60,  # noqa: ASYNC109 - mirrors the Transport protocol
    ) -> str:
        self.calls.append(("AUTH", app_name, {"timeout": timeout}))
        return "fake-app-key"

    async def subscribe_events(
        self,
        *,
        max_retries: int | None = 10,
    ) -> AsyncGenerator[dict[str, Any]]:
        del max_retries
        for event in self.events:
            yield event

    async def subscribe_event_frames(
        self,
        *,
        max_retries: int | None = 10,
    ) -> AsyncGenerator[SSEFrame]:
        del max_retries
        for index, event in enumerate(self.events):
            yield SSEFrame(
                event_id=f"fake:{index}",
                received_at=datetime.now(UTC),
                events=[event],
            )

    async def event_connections(
        self,
        *,
        max_retries: int | None = 10,
    ) -> AsyncGenerator[EventConnection]:
        del max_retries
        yield EventConnection(
            opened_at=datetime.now(UTC),
            resumed_from=None,
            frames=self.subscribe_event_frames(),
        )

    def add_write_observer(self, observer: WriteObserver) -> Callable[[], None]:
        self._write_observers.add(observer)

        def unsubscribe() -> None:
            self._write_observers.discard(observer)

        return unsubscribe

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None = None,
        exc_val: BaseException | None = None,
        exc_tb: TracebackType | None = None,
    ) -> None:
        self.closed = True

    @property
    def last(self) -> Call:
        """The most recent call, as ``(method, path, payload)``."""
        return self.calls[-1]

    @property
    def paths(self) -> list[str]:
        return [path for _, path, _ in self.calls]

    @property
    def writes(self) -> list[Call]:
        """Only the mutating calls -- a read often precedes the write."""
        return [call for call in self.calls if call[0] in {"PUT", "POST", "DELETE"}]


@pytest.fixture
def http() -> FakeHttp:
    return FakeHttp()


@pytest.fixture
def hue(http: FakeHttp, tmp_path) -> Hue:
    """Build a started Hue client wired to the fake transport."""
    client = Hue(
        bridge_ip="10.0.0.1",
        app_key="test-app-key",
        config_path=tmp_path / "config.json",
    )
    client._http = http
    return client


@pytest.fixture
def bare_hue(tmp_path) -> Hue:
    """Build a Hue client that was never started -- no transport attached."""
    return Hue(
        bridge_ip="10.0.0.1",
        app_key="test-app-key",
        config_path=tmp_path / "config.json",
    )
