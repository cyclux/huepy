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
import ssl
from collections.abc import AsyncGenerator
from contextlib import aclosing
from types import TracebackType
from typing import Any, Protocol, Self, cast, runtime_checkable

import aiohttp
from pydantic import JsonValue

from huepy.config import HueConfig
from huepy.exceptions import AuthenticationError, BridgeConnectionError, HueAPIError

logger = logging.getLogger(__name__)

TIMEOUT_STREAM = 3600
"""Seconds a single event-stream connection may stay open before recycling."""

TIMEOUT_AUTH = 60
"""Seconds to keep polling for the bridge link button."""

DELAY_INITIAL = 1
"""Seconds to wait before the first reconnect attempt."""

DELAY_MAX = 60
"""Upper bound on the reconnect backoff, in seconds."""

RETRIES_MAX = 10
"""Consecutive failed reconnects before the event stream gives up."""

_HTTP_OK = 200
_HTTP_MULTI_STATUS = 207
_HTTP_REDIRECT = 300
_LINK_BUTTON_NOT_PRESSED = 101
_SSE_DATA_PREFIX = "data:"


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


@runtime_checkable
class Transport(Protocol):
    """The transport surface :class:`~huepy.client.base.Hue` depends on.

    Declaring it as a protocol keeps the client decoupled from aiohttp and
    gives tests a typed seam: any object with these methods is a valid
    transport, no casting required.
    """

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
        timeout: int = ...,  # noqa: ASYNC109 - link-button budget, not a cancel scope
    ) -> str:
        """Obtain an application key from the bridge."""
        ...

    def subscribe_events(self) -> AsyncGenerator[dict[str, Any]]:
        """Yield events pushed by the bridge."""
        ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Release the transport's resources."""
        ...


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
            connector=aiohttp.TCPConnector(ssl=ssl_context),
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

    async def _request(
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
            HueAPIError: If the bridge answers with a non-200 status.

        """
        session = self._active_session
        try:
            async with session.request(method, path, json=data) as response:
                # The bridge answers partial failures with 207 Multi-Status and
                # puts the detail in the body's errors[]. Accept any 2xx here and
                # let the envelope decide -- treating 207 as a transport error
                # would hide the description behind a raw JSON string.
                if not _is_success(response.status):
                    raise HueAPIError(response.status, await response.text())
                if response.content_length == 0:
                    return None
                return await response.json()
        except aiohttp.ClientError as exc:
            msg = f"Failed to reach bridge at {self.config.bridge_ip}: {exc}"
            raise BridgeConnectionError(msg) from exc
        except TimeoutError as exc:
            msg = f"Timed out reaching bridge at {self.config.bridge_ip}"
            raise BridgeConnectionError(msg) from exc

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

    async def subscribe_events(self) -> AsyncGenerator[dict[str, Any]]:
        """Yield events from the bridge's server-sent event stream.

        Reconnects with exponential backoff on network failures, giving up
        after :data:`RETRIES_MAX` consecutive failures.

        Yields:
            Each decoded event object the bridge pushes.

        Raises:
            HueAPIError: If the bridge rejects the subscription request.

        """
        session = self._active_session
        retry_count = 0

        while True:
            reason = "closed by the bridge"
            try:
                # aclosing, not a bare `async for`: closing this generator must
                # also finalise the inner one, or its streaming response stays
                # suspended and the socket leaks until garbage collection.
                async with aclosing(self._read_event_stream(session)) as stream:
                    async for event in stream:
                        retry_count = 0
                        yield event
            except asyncio.CancelledError:
                logger.info("Event subscription cancelled")
                raise
            except (TimeoutError, aiohttp.ClientError) as exc:
                reason = str(exc) or type(exc).__name__

            # Reached both when the connection errored and when it ended
            # cleanly; backing off in either case avoids a reconnect storm
            # against a bridge that closes the stream immediately.
            retry_count += 1
            if retry_count > RETRIES_MAX:
                logger.error(
                    "Giving up after %d failed reconnect attempts", RETRIES_MAX
                )
                return
            delay = backoff_delay(retry_count)
            logger.warning(
                "Event stream lost (%s); reconnecting in %.0fs (attempt %d of %d)",
                reason,
                delay,
                retry_count,
                RETRIES_MAX,
            )
            await asyncio.sleep(delay)

    async def _read_event_stream(
        self,
        session: aiohttp.ClientSession,
    ) -> AsyncGenerator[dict[str, Any]]:
        """Read one event-stream connection until it closes.

        Args:
            session: The open aiohttp session.

        Yields:
            Each decoded event object.

        Raises:
            HueAPIError: If the bridge rejects the subscription request.

        """
        logger.info("Subscribing to the bridge event stream...")
        async with session.get(
            "/eventstream/clip/v2",
            headers={"Accept": "text/event-stream"},
            timeout=aiohttp.ClientTimeout(total=TIMEOUT_STREAM),
        ) as response:
            # Exiting the `async with` only *releases* the response, which for a
            # half-read stream leaves the socket to the garbage collector. Force
            # it shut instead: an abandoned stream can never be pooled.
            try:
                if response.status != _HTTP_OK:
                    raise HueAPIError(response.status, await response.text())
                logger.info("Event stream connected")

                async for raw_line in response.content:
                    line = raw_line.decode().strip()
                    if not line.startswith(_SSE_DATA_PREFIX):
                        continue
                    body = line[len(_SSE_DATA_PREFIX) :].strip()
                    if not body:
                        continue
                    try:
                        payload: object = json.loads(body)
                    except json.JSONDecodeError:
                        logger.warning("Discarding malformed event payload: %r", body)
                        continue
                    # A `data:` line carries a JSON *array* of event objects, so
                    # flatten it: callers want one event at a time, not a batch.
                    events: list[object] = (
                        cast("list[object]", payload)
                        if isinstance(payload, list)
                        else [payload]
                    )
                    for event in events:
                        if isinstance(event, dict):
                            yield event
                        else:
                            logger.warning("Discarding unexpected event: %r", event)
            finally:
                response.close()
