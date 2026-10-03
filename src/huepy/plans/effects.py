"""Carrying out a rule's one-shot effect: a blink, or a command.

The arbiter decides *that* an effect is due and records it; this is the I/O
half, kept apart for the reason :mod:`huepy.plans.executor` is. Neither effect
touches what the plan believes about a scope. A blink is the bridge's own
signal, which leaves ``on`` and ``dimming`` alone and restores the light by
itself; a command does not touch the lights at all.

Typical usage example:

    failed = await flash(client, ("light-1", "light-2"), blinks=3)
    result = await run_command(["/usr/local/bin/notify", "door"], kill_after=30.0)
"""

import asyncio
import contextlib
import logging
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from huepy.exceptions import HueError
from huepy.models.common import ResourceIdentifier, unwrap
from huepy.models.light import Signal
from huepy.models.state import MILLISECONDS_PER_SECOND
from huepy.plans.protocol import PlanClient

logger = logging.getLogger(__name__)

SECONDS_PER_BLINK = 1.0
"""How long one blink of the bridge's ``on_off`` signal takes.

Measured, not documented: a four-second signal blinked four times on three
LTG002 spots, and the bridge reported nothing but ``signaling.status`` --
first the signal, then ``null`` when it ended.
"""

STDERR_TAIL = 400
"""How many characters of a failed command's error output reach the log."""


def flash_seconds(blinks: int) -> float:
    """How long a flash of so many blinks lasts.

    Args:
        blinks: The number of blinks.

    Returns:
        The signal's duration, in seconds.

    """
    return blinks * SECONDS_PER_BLINK


async def flash(
    client: PlanClient, light_ids: Iterable[str], *, blinks: int
) -> list[str]:
    """Blink lights with the bridge's ``on_off`` signal.

    Sent to each light, not to a room's ``grouped_light``: a light names the
    signals it supports, a group does not, and one bulb that cannot signal
    must not stop the others blinking. The writes go out together so the
    lights blink in step; the transport paces them.

    Args:
        client: The client to write through.
        light_ids: The lights to blink.
        blinks: How many times.

    Returns:
        The ids of the lights the bridge refused, each already logged.

    """
    ids = tuple(dict.fromkeys(light_ids))
    duration = round(flash_seconds(blinks) * MILLISECONDS_PER_SECOND)
    payload = {"signaling": {"signal": str(Signal.ON_OFF), "duration": duration}}

    async def one(light_id: str) -> bool:
        path = f"/clip/v2/resource/light/{light_id}"
        logger.debug("PUT %s %s", path, payload)
        try:
            _ = unwrap(await client.http.put(path, payload), ResourceIdentifier)
        except HueError:
            logger.exception("light %s could not flash", light_id)
            return False
        return True

    sent = await asyncio.gather(*(one(light_id) for light_id in ids))
    return [light_id for light_id, ok in zip(ids, sent, strict=True) if not ok]


@dataclass(frozen=True, slots=True)
class RunResult:
    """How a command ended.

    Attributes:
        returncode: Its exit status; None when it was killed for taking too
            long, or could not be started.
        stderr: The tail of what it wrote to standard error.

    """

    returncode: int | None
    stderr: str

    @property
    def ok(self) -> bool:
        """Whether it ran and exited zero."""
        return self.returncode == 0


async def run_command(
    argv: list[str],
    *,
    kill_after: float,
    env: Mapping[str, str] | None = None,
) -> RunResult:
    """Run a command without a shell, and kill it if it overstays.

    Its output is captured, never inherited: a chatty script must not
    interleave with the runner's own log, and only a failure's error output
    is worth keeping.

    Args:
        argv: The program and its arguments.
        kill_after: Seconds it may take before it is killed. Not called
            ``timeout``: the caller does not wait on a deadline of its own.
        env: Variables to add to the runner's own environment.

    Returns:
        How it ended. A program that cannot be started is reported the way
        a timeout is -- no exit status -- with the reason as its error output.

    """
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, **(env or {})},
        )
    except OSError as error:
        return RunResult(returncode=None, stderr=str(error))
    try:
        _, stderr = await asyncio.wait_for(process.communicate(), timeout=kill_after)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        _ = await process.wait()
        return RunResult(returncode=None, stderr=f"killed after {kill_after:g} s")
    except asyncio.CancelledError:
        # The runner is closing. A command left running would outlive the
        # daemon that started it, unseen.
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        raise
    text = stderr.decode(errors="replace").strip()
    return RunResult(returncode=process.returncode, stderr=text[-STDERR_TAIL:])
