"""Signals from outside the process: a small HTTP server for ``signal:`` triggers.

``PlanRunner.fire()`` is the hook for anything the bridge cannot know about,
but it is a Python call. A plan run as a daemon needs the same hook reachable
from a shell, a cron job or a home-automation box, so this serves it over HTTP
on the loopback interface: ``POST /signals/<name>`` fires, ``GET /signals``
lists what the plan listens for.

It is the one place outside the client where ``aiohttp`` appears, and it is a
*server*: it never talks to a bridge, and needs only a callable ``fire`` and
the set of names the plan knows. Loopback by default, and binding to anything
else requires a bearer token, so a plan is reachable from another machine on
purpose or not at all.

Typical usage example:

    async with PlanRunner(hue, plan, changes=hue.state) as runner:
        async with SignalServer(runner.fire, runner.signals):
            await runner.run()
"""

import hmac
import ipaddress
import logging
from collections.abc import Callable, Collection
from typing import Any, Self, cast

from aiohttp import web

from huepy.exceptions import PlanError

logger = logging.getLogger(__name__)

DEFAULT_SIGNAL_HOST = "127.0.0.1"
"""Where the signal server listens unless told otherwise: this machine only."""

DEFAULT_SIGNAL_PORT = 8757
"""The port ``huepy plan run`` and ``huepy plan signal`` agree on by default."""

type Fire = Callable[[str], tuple[str, ...]]
"""What the server calls with a signal name; ``PlanRunner.fire`` fits."""


def _is_loopback(host: str) -> bool:
    """Whether an address reaches only this machine.

    Args:
        host: A host name or address.

    Returns:
        True for ``localhost`` and any loopback IP.

    """
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class SignalServer:
    """Serves a plan's ``signal:`` triggers over HTTP.

    Attributes:
        host: The interface it listens on.

    """

    def __init__(
        self,
        fire: Fire,
        known: Collection[str],
        *,
        host: str = DEFAULT_SIGNAL_HOST,
        port: int = DEFAULT_SIGNAL_PORT,
        token: str | None = None,
    ) -> None:
        """Prepare a server. Nothing listens until it starts.

        Args:
            fire: What to call with a signal name; returns what it did.
            known: The names the plan listens for. Anything else is refused
                with this list, which is the diagnosis a typo needs.
            host: The interface to bind. Loopback unless a token is given.
            port: The port to bind; ``0`` picks a free one, read back from
                :attr:`port` once started.
            token: When given, every request must carry it as a bearer
                token. Required for a host that is not loopback.

        Raises:
            PlanError: If ``host`` reaches beyond this machine and no token
                guards it.

        """
        if token is None and not _is_loopback(host):
            msg = (
                f"listening on {host} would accept signals from other machines; "
                f"give a token to do that on purpose, or listen on "
                f"{DEFAULT_SIGNAL_HOST}"
            )
            raise PlanError(msg)
        self.host: str = host
        self._fire: Fire = fire
        self._known: frozenset[str] = frozenset(known)
        self._requested_port: int = port
        self._token: str | None = token
        self._runner: web.AppRunner | None = None
        self._port: int | None = None

    @property
    def port(self) -> int:
        """The port actually bound.

        Returns:
            The port, which differs from the one asked for only when that
            was ``0``.

        Raises:
            RuntimeError: If the server has not been started.

        """
        if self._port is None:
            msg = "the signal server has not been started; use `async with`"
            raise RuntimeError(msg)
        return self._port

    async def start(self) -> None:
        """Bind and start serving."""
        app = web.Application()
        _ = app.add_routes(
            [
                web.get("/signals", self._list),
                web.post("/signals/{name}", self._fire_one),
            ]
        )
        # No access log: the plan's own log already says what each signal
        # did, and a line per poll from a home-automation box is noise.
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self._requested_port)
        await site.start()
        # The bound address, for when the port asked for was 0.
        bound = cast("list[tuple[str, int]]", self._runner.addresses)
        self._port = bound[0][1]
        logger.info("signals accepted at http://%s:%d/signals", self.host, self._port)

    async def close(self) -> None:
        """Stop serving and release the port."""
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
        self._port = None

    async def __aenter__(self) -> Self:
        """Start serving and return the server.

        Returns:
            This server, listening.

        """
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        """Stop serving."""
        await self.close()

    def _refuse(self, request: web.Request) -> web.Response | None:
        """Check the bearer token, when one is required.

        Args:
            request: The incoming request.

        Returns:
            A 401 response to send back, or None when the request may proceed.

        """
        if self._token is None:
            return None
        given = request.headers.get("Authorization", "")
        if hmac.compare_digest(given, f"Bearer {self._token}"):
            return None
        return web.json_response(
            {"error": "a bearer token is required"},
            status=401,
            headers={"WWW-Authenticate": "Bearer"},
        )

    async def _list(self, request: web.Request) -> web.Response:
        """Answer ``GET /signals`` with the names the plan listens for.

        Args:
            request: The incoming request.

        Returns:
            The list, as JSON.

        """
        refused = self._refuse(request)
        if refused is not None:
            return refused
        return web.json_response({"signals": sorted(self._known)})

    async def _fire_one(self, request: web.Request) -> web.Response:
        """Answer ``POST /signals/{name}`` by firing the signal.

        Args:
            request: The incoming request.

        Returns:
            What the signal did; 404 with the known names for one the plan
            does not listen for; 500 when the plan's handler raised, which
            is logged and does not stop the server.

        """
        refused = self._refuse(request)
        if refused is not None:
            return refused
        name = request.match_info["name"]
        if name not in self._known:
            body: dict[str, Any] = {
                "error": "unknown signal",
                "signal": name,
                "known": sorted(self._known),
            }
            return web.json_response(body, status=404)
        try:
            outcomes = self._fire(name)
        except Exception:
            logger.exception("signal:%s: the plan could not act on it", name)
            return web.json_response(
                {"error": "the plan could not act on the signal", "signal": name},
                status=500,
            )
        return web.json_response({"signal": name, "outcomes": list(outcomes)})
