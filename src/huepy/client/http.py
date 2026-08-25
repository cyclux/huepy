"""Thin async HTTP layer over the bridge's v2 CLIP API.

Owns the aiohttp session, maps non-success statuses to :class:`HueAPIError`,
and turns the bridge's server-sent event stream into an async iterator that
reconnects with exponential backoff.

Typical usage example:

    async with HueHttpClient(config) as client:
        payload = await client.get("/clip/v2/resource/light")
"""

import asyncio
import json
import logging
import socket
import ssl
from collections.abc import AsyncGenerator, Callable
from contextlib import aclosing
from datetime import UTC, datetime
from types import TracebackType
from typing import Any, Literal, Never, Self, cast
from uuid import uuid4

import aiohttp
from pydantic import JsonValue

from huepy.client.protocol import (
    EventConnection,
    PendingWrite,
    SSEFrame,
    WriteObserver,
)
from huepy.config import HueConfig
from huepy.exceptions import AuthenticationError, BridgeConnectionError, HueAPIError

logger = logging.getLogger(__name__)

__all__ = [
    "EventConnection",
    "HueHttpClient",
    "PendingWrite",
    "SSEFrame",
    "WriteObserver",
]

TIMEOUT_AUTH = 60
"""Seconds to keep polling for the bridge link button."""

DELAY_INITIAL = 1
"""Seconds to wait before the first reconnect attempt."""

DELAY_MAX = 60
"""Upper bound on the reconnect backoff, in seconds."""

RETRIES_MAX = 10
"""Consecutive failed reconnects before the event stream gives up."""

GET_RETRIES_MAX = 3
"""Retries for a GET throttled or temporarily unavailable at the bridge."""

_HTTP_OK = 200
_HTTP_MULTI_STATUS = 207
_HTTP_REDIRECT = 300
_LINK_BUTTON_NOT_PRESSED = 101
_RETRYABLE_GET_STATUSES = frozenset({429, 503})


def _is_success(status: int) -> bool:
    """Whether an HTTP status counts as a successful bridge response.

    The v2 API uses 207 Multi-Status for requests that partly failed; the
    detail lives in the body, so those must reach the envelope rather than
    being raised as transport errors.

    Args:
        status: The HTTP status code.

    Returns:
        True for any 2xx status.

    """
    return _HTTP_OK <= status < _HTTP_REDIRECT


def _write_status(
    payload: JsonValue,
) -> Literal["accepted", "rejected", "unknown"]:
    """Classify a successful HTTP PUT from the bridge envelope, when present."""
    if not isinstance(payload, dict):
        return "unknown"
    typed = cast("dict[str, object]", payload)
    errors = typed.get("errors")
    data = typed.get("data")
    if not isinstance(errors, list) or not isinstance(data, list):
        return "unknown"
    if not errors:
        return "accepted"
    blocking = any(
        not isinstance(error, dict)
        or cast("dict[str, object]", error).get("error_code") != "communication_error"
        for error in cast("list[object]", errors)
    )
    rejected: bool = blocking or not data
    return "rejected" if rejected else "accepted"


def _raise_api_error(status: int, body: str) -> Never:
    """Raise the error for a non-successful bridge response."""
    raise HueAPIError(status, body)


def backoff_delay(retry_count: int) -> float:
    """Seconds to wait before reconnect attempt ``retry_count``.

    Doubles per attempt, capped at :data:`DELAY_MAX`.

    Args:
        retry_count: The 1-based attempt number.

    Returns:
        The delay in seconds.

    """
    return float(min(DELAY_INITIAL * (2 ** (retry_count - 1)), DELAY_MAX))


def _unverified_ssl_context() -> ssl.SSLContext:
    """Build an SSL context that accepts the bridge's self-signed certificate."""
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def _keepalive_socket(addr_info: tuple[Any, ...]) -> socket.socket:
    """Create a TCP socket with keepalive probes tuned where supported."""
    family, sock_type, proto, *_ = addr_info
    sock = socket.socket(family=family, type=sock_type, proto=proto)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    options = (
        ("TCP_KEEPIDLE", 60),
        ("TCP_KEEPINTVL", 10),
        ("TCP_KEEPCNT", 3),
    )
    for name, value in options:
        option = getattr(socket, name, None)
        if option is None:
            continue
        try:
            sock.setsockopt(socket.IPPROTO_TCP, option, value)
        except OSError:
            logger.debug("TCP keepalive option %s is unsupported", name)
    return sock


class HueHttpClient:
    """Async HTTP client for one Hue bridge.

    Attributes:
        config: The bridge connection settings.
        session: The underlying aiohttp session, or None before entry.

    """

    def __init__(self, config: HueConfig) -> None:
        """Initialise the client.

        Args:
            config: The bridge connection settings.

        """
        self.config: HueConfig = config
        self.session: aiohttp.ClientSession | None = None
        self._write_observers: set[WriteObserver] = set()

    async def __aenter__(self) -> Self:
        """Open the HTTP session."""
        ssl_context = (
            ssl.create_default_context()
            if self.config.verify_ssl
            else _unverified_ssl_context()
        )
        headers = (
            {"hue-application-key": self.config.app_key} if self.config.app_key else {}
        )
        self.session = aiohttp.ClientSession(
            base_url=f"https://{self.config.bridge_ip}",
            headers=headers,
            connector=aiohttp.TCPConnector(
                limit_per_host=3,
                ssl=ssl_context,
                socket_factory=_keepalive_socket,
            ),
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Close the HTTP session and drop the reference to it."""
        if self.session is not None:
            await self.session.close()
            self.session = None

    @property
    def _active_session(self) -> aiohttp.ClientSession:
        """The open session.

        Raises:
            RuntimeError: If the client was never entered, or already closed.

        """
        if self.session is None:
            msg = "Client not initialized"
            raise RuntimeError(msg)
        return self.session

    async def _request(  # noqa: C901, PLR0912, PLR0915 - one observed request lifecycle
        self,
        method: str,
        path: str,
        data: dict[str, Any] | None = None,
    ) -> JsonValue:
        """Perform one request and return the decoded body.

        Args:
            method: The HTTP method.
            path: The API path, relative to the bridge root.
            data: A JSON body to send, if any.

        Returns:
            The decoded JSON body, or None for responses without one.

        Raises:
            HueAPIError: If the bridge answers outside the 2xx success range.

        """
        session = self._active_session
        pending: PendingWrite | None = None
        if method == "PUT" and data is not None:
            pending = PendingWrite(
                command_id=uuid4(),
                path=path,
                payload=data,
                sent_at=datetime.now(UTC),
            )
            self._publish_write(pending)
        try:
            retry_count = 0
            while True:
                retry_status: int | None = None
                async with session.request(method, path, json=data) as response:
                    # GET is safe to replay. The bridge uses 429 for transient
                    # load shedding and 503 while temporarily unavailable, but
                    # does not send a useful Retry-After header for either.
                    if (
                        method == "GET"
                        and response.status in _RETRYABLE_GET_STATUSES
                        and retry_count < GET_RETRIES_MAX
                    ):
                        retry_status = response.status
                        _ = await response.text()
                    elif not _is_success(response.status):
                        body = await response.text()
                        if pending is not None:
                            self._complete_write(pending, "rejected")
                            pending = None
                        _raise_api_error(response.status, body)
                    elif response.content_length == 0:
                        if pending is not None:
                            self._complete_write(pending, "accepted")
                            pending = None
                        return None
                    else:
                        result = await response.json()
                        if pending is not None:
                            self._complete_write(pending, _write_status(result))
                            pending = None
                        return result

                retry_count += 1
                delay = backoff_delay(retry_count)
                logger.warning(
                    "GET %s returned %s; retrying in %.0fs",
                    path,
                    retry_status,
                    delay,
                )
                await asyncio.sleep(delay)
        except asyncio.CancelledError:
            if pending is not None:
                self._complete_write(pending, "unknown")
            raise
        except aiohttp.ClientError as exc:
            if pending is not None:
                self._complete_write(pending, "unknown")
            msg = f"Failed to reach bridge at {self.config.bridge_ip}: {exc}"
            raise BridgeConnectionError(msg) from exc
        except TimeoutError as exc:
            if pending is not None:
                self._complete_write(pending, "unknown")
            msg = f"Timed out reaching bridge at {self.config.bridge_ip}"
            raise BridgeConnectionError(msg) from exc
        except Exception:
            if pending is not None:
                self._complete_write(pending, "unknown")
            raise

    def add_write_observer(self, observer: WriteObserver) -> Callable[[], None]:
        """Register a non-awaiting observer for PUT lifecycle records."""
        self._write_observers.add(observer)

        def unsubscribe() -> None:
            self._write_observers.discard(observer)

        return unsubscribe

    def _publish_write(self, write: PendingWrite) -> None:
        """Publish detached records without allowing observers to break a request."""
        for observer in tuple(self._write_observers):
            self._notify_observer(observer, write.model_copy(deep=True))

    @staticmethod
    def _notify_observer(observer: WriteObserver, write: PendingWrite) -> None:
        """Run one non-awaiting observer without allowing it to fail the request."""
        try:
            observer(write)
        except Exception:
            logger.exception("Write observer failed")

    def _complete_write(
        self,
        write: PendingWrite,
        status: Literal["accepted", "rejected", "unknown"],
    ) -> None:
        self._publish_write(
            write.model_copy(
                update={"completed_at": datetime.now(UTC), "status": status},
                deep=True,
            )
        )

    async def get(self, path: str) -> JsonValue:
        """Send a GET request.

        Args:
            path: The API path.

        Returns:
            The decoded JSON body.

        """
        return await self._request("GET", path)

    async def put(self, path: str, data: dict[str, Any]) -> JsonValue:
        """Send a PUT request.

        Args:
            path: The API path.
            data: The JSON body.

        Returns:
            The decoded JSON body.

        """
        return await self._request("PUT", path, data)

    async def post(self, path: str, data: dict[str, Any]) -> JsonValue:
        """Send a POST request.

        Args:
            path: The API path.
            data: The JSON body.

        Returns:
            The decoded JSON body.

        """
        return await self._request("POST", path, data)

    async def delete(self, path: str) -> JsonValue:
        """Send a DELETE request.

        Args:
            path: The API path.

        Returns:
            The decoded JSON body, if the bridge sent one.

        """
        return await self._request("DELETE", path)

    async def authenticate(
        self,
        app_name: str = "huepy",
        timeout: int = TIMEOUT_AUTH,  # noqa: ASYNC109 - link-button budget, not a cancel scope
    ) -> str:
        """Obtain an application key, waiting for the bridge link button.

        Polls the bridge until the button is pressed or ``timeout`` elapses.
        On success the key is stored via :meth:`HueConfig.save` and applied to
        the current session.

        Args:
            app_name: The device type recorded on the bridge.
            timeout: How long to keep polling, in seconds.

        Returns:
            The newly issued application key.

        Raises:
            AuthenticationError: If the bridge refuses, or the timeout expires.
            BridgeConnectionError: If the bridge cannot be reached.

        """
        session = self._active_session
        try:
            async with asyncio.timeout(timeout):
                return await self._poll_for_app_key(session, app_name)
        except TimeoutError:
            msg = "Timed out waiting for the bridge link button to be pressed"
            raise AuthenticationError(msg) from None

    async def _poll_for_app_key(
        self,
        session: aiohttp.ClientSession,
        app_name: str,
    ) -> str:
        """Poll the bridge until it issues an application key.

        Loops forever; the caller bounds it with a timeout.

        Args:
            session: The open aiohttp session.
            app_name: The device type recorded on the bridge.

        Returns:
            The newly issued application key.

        Raises:
            AuthenticationError: If the bridge reports a non-recoverable error.
            BridgeConnectionError: If the bridge cannot be reached.

        """
        while True:
            try:
                async with session.post(
                    "/api",
                    json={"devicetype": app_name, "generateclientkey": True},
                ) as response:
                    payload = await response.json()
            except aiohttp.ClientError as exc:
                msg = f"Failed to connect to bridge: {exc}"
                raise BridgeConnectionError(msg) from exc

            app_key = self._app_key_from(payload)
            if app_key is not None:
                self.config.save(app_key)
                session.headers["hue-application-key"] = app_key
                return app_key

            await asyncio.sleep(1)

    def _app_key_from(self, payload: JsonValue) -> str | None:
        """Extract the issued key from an /api response, if it holds one.

        Args:
            payload: The decoded body of the authentication request.

        Returns:
            The key, or None while the link button has not been pressed yet.

        Raises:
            AuthenticationError: If the bridge reported a non-recoverable error.

        """
        if not isinstance(payload, list) or not payload:
            return None
        entry = payload[0]
        if not isinstance(entry, dict):
            return None

        success = entry.get("success")
        if isinstance(success, dict):
            username = success.get("username")
            return str(username) if username is not None else None

        error = entry.get("error")
        if not isinstance(error, dict):
            return None
        if error.get("type") == _LINK_BUTTON_NOT_PRESSED:
            logger.info("Waiting for the bridge link button to be pressed...")
            return None

        description = error.get("description", "unknown error")
        msg = f"Authentication failed: {description}"
        raise AuthenticationError(msg)

    async def event_connections(  # noqa: C901, PLR0915 - reconnect lifecycle is deliberately linear
        self,
        *,
        max_retries: int | None = RETRIES_MAX,
    ) -> AsyncGenerator[EventConnection]:
        """Yield each established SSE connection after its HTTP response opens."""
        session = self._active_session
        retry_count = 0
        last_event_id: str | None = None

        while True:
            headers: dict[str, str] = {"Accept": "text/event-stream"}
            if last_event_id:
                headers["Last-Event-ID"] = last_event_id  # pyright: ignore[reportUnreachable]
            try:
                logger.info("Subscribing to the bridge event stream...")
                response = await session.get(
                    "/eventstream/clip/v2",
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=None, sock_read=None),
                )
                if response.status != _HTTP_OK:
                    try:
                        raise HueAPIError(response.status, await response.text())
                    finally:
                        response.close()
            except asyncio.CancelledError:
                raise
            except (TimeoutError, aiohttp.ClientError) as exc:
                retry_count += 1
                if max_retries is not None and retry_count > max_retries:
                    msg = (
                        f"Event stream failed to connect after {max_retries} retries: "
                        f"{exc}"
                    )
                    raise BridgeConnectionError(msg) from exc
                delay = backoff_delay(retry_count)
                logger.warning(
                    "Event stream connection failed (%s); retrying in %.0fs",
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)
                continue

            retry_count = 0
            resumed_from = last_event_id or None
            logger.info("Event stream connected")

            async def frames(
                open_response: aiohttp.ClientResponse = response,
            ) -> AsyncGenerator[SSEFrame]:
                nonlocal last_event_id
                try:
                    async for frame in self._read_event_stream(open_response):
                        if frame.event_id is not None:
                            last_event_id = frame.event_id
                        yield frame
                except (TimeoutError, aiohttp.ClientError) as exc:
                    msg = f"Event stream lost: {exc}"
                    raise BridgeConnectionError(msg) from exc
                finally:
                    open_response.close()

            stream = frames()
            try:
                yield EventConnection(
                    opened_at=datetime.now(UTC),
                    resumed_from=resumed_from,
                    frames=stream,
                )
            finally:
                await stream.aclose()
                response.close()

            delay = backoff_delay(1)
            logger.warning(
                "Event stream lost; reconnecting in %.0fs",
                delay,
            )
            await asyncio.sleep(delay)

    async def subscribe_event_frames(
        self,
        *,
        max_retries: int | None = RETRIES_MAX,
    ) -> AsyncGenerator[SSEFrame]:
        """Flatten established connections into complete SSE frames."""
        connections = self.event_connections(max_retries=max_retries)
        async with aclosing(connections):
            async for connection in connections:
                try:
                    async for frame in connection.frames:
                        yield frame
                except asyncio.CancelledError:
                    raise
                except BridgeConnectionError as exc:
                    logger.warning("Event stream lost (%s)", exc)

    async def subscribe_events(
        self,
        *,
        max_retries: int | None = RETRIES_MAX,
    ) -> AsyncGenerator[dict[str, Any]]:
        """Yield decoded event dictionaries across durable SSE connections."""
        frames = self.subscribe_event_frames(max_retries=max_retries)
        async with aclosing(frames):
            async for frame in frames:
                for event in frame.events:
                    yield event

    async def _read_event_stream(  # noqa: C901 - SSE field parser preserves frame ordering
        self,
        response: aiohttp.ClientResponse,
    ) -> AsyncGenerator[SSEFrame]:
        """Parse complete frames from one already-open SSE response."""
        last_event_id: str | None = None
        event_id: str | None = None
        has_event_id = False
        data_lines: list[str] = []

        async for raw_line in response.content:
            line = raw_line.decode().rstrip("\r\n")
            if line:
                if line.startswith(":"):
                    continue
                field, separator, value = line.partition(":")
                if separator and value.startswith(" "):
                    value = value[1:]
                if field == "id":
                    if "\x00" not in value:
                        event_id = value
                        has_event_id = True
                elif field == "data":
                    data_lines.append(value)
                continue

            if has_event_id:
                last_event_id = event_id
            frame = self._decode_sse_frame(
                last_event_id,
                data_lines,
                emit_empty=has_event_id,
            )
            event_id = None
            has_event_id = False
            data_lines = []
            if frame is not None:
                yield frame

        if has_event_id:
            last_event_id = event_id
        frame = self._decode_sse_frame(
            last_event_id,
            data_lines,
            emit_empty=has_event_id,
        )
        if frame is not None:
            yield frame

    @staticmethod
    def _decode_sse_frame(
        event_id: str | None,
        data_lines: list[str],
        *,
        emit_empty: bool = False,
    ) -> SSEFrame | None:
        """Decode one accumulated SSE frame, dropping malformed entries."""
        if not data_lines:
            return (
                SSEFrame(
                    event_id=event_id,
                    received_at=datetime.now(UTC),
                    events=[],
                )
                if emit_empty
                else None
            )
        body = "\n".join(data_lines)
        try:
            payload: object = json.loads(body)
        except json.JSONDecodeError:
            logger.warning("Discarding malformed event payload: %r", body)
            return (
                SSEFrame(
                    event_id=event_id,
                    received_at=datetime.now(UTC),
                    events=[],
                )
                if event_id is not None
                else None
            )
        entries: list[object] = (
            cast("list[object]", payload) if isinstance(payload, list) else [payload]
        )
        events = [
            cast("dict[str, Any]", entry)
            for entry in entries
            if isinstance(entry, dict)
        ]
        if len(events) != len(entries):
            logger.warning("Discarding unexpected values in event frame: %s", body)
        if not events:
            return (
                SSEFrame(
                    event_id=event_id,
                    received_at=datetime.now(UTC),
                    events=[],
                )
                if emit_empty
                else None
            )
        return SSEFrame(
            event_id=event_id,
            received_at=datetime.now(UTC),
            events=events,
        )
