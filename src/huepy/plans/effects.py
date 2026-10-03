"""Carrying out a rule's one-shot effect: a blink, a breath, or a command.

The arbiter decides *that* an effect is due and records it; this is the I/O
half, kept apart for the reason :mod:`huepy.plans.executor` is. A blink is the
bridge's own signal, which leaves ``on`` and ``dimming`` alone and restores
the light by itself, and a command does not touch the lights at all. A breath
is the one effect that writes a light's state, so it starts by reading where
each light is and ends by being there again; keeping the plan out of the way
meanwhile is the runner's job.

Typical usage example:

    failed = await flash(client, ("light-1", "light-2"), blinks=3)
    lights = await read_lights(client, ("light-1", "light-2"))
    await breathe(client, lights, breaths=2, period=2.0)
    result = await run_command(["/usr/local/bin/notify", "door"], kill_after=30.0)
"""

import asyncio
import contextlib
import logging
import os
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from huepy.exceptions import HueError
from huepy.models.common import ResourceIdentifier, unwrap, unwrap_one
from huepy.models.light import Light, Signal
from huepy.models.state import MILLISECONDS_PER_SECOND, build_light_payload
from huepy.plans.protocol import PlanClient

logger = logging.getLogger(__name__)

SECONDS_PER_BLINK = 1.0
"""How long one blink of the bridge's ``on_off`` signal takes.

Measured, not documented: a four-second signal blinked four times on three
LTG002 spots, and the bridge reported nothing but ``signaling.status`` --
first the signal, then ``null`` when it ended.
"""

BREATH_DIP = 0.15
"""How far a lit light dips in a breath, as a fraction of its level.

To 15 % and back over two seconds read as a breath on LTG002 spots at 100 %:
deep enough to see in a lit room, short of going dark.
"""

BREATH_FLOOR = 1.0
"""The lowest level a breath writes: a dark light starts its rise here."""

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


type Sleeper = Callable[[float], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class Resting:
    """Where a light was before a breath, and so where it ends.

    Attributes:
        light_id: The light.
        on: Whether it was on.
        brightness: Its level -- for a dark light, the level it holds for
            the next switch-on, which is also how high it rises.

    """

    light_id: str
    on: bool
    brightness: float


async def read_lights(client: PlanClient, light_ids: Iterable[str]) -> list[Resting]:
    """Read where each light is, before a breath moves it.

    Read from the bridge rather than from the plan's beliefs: a light set by
    hand, or one whose fade the plan never saw land, is where the bridge says.

    Args:
        client: The client to read through.
        light_ids: The lights.

    Returns:
        One entry per light that could be read and has a level. A plug has
        none, so it does not breathe; a failed read is logged and skipped.

    """
    found: list[Resting] = []
    for light_id in dict.fromkeys(light_ids):
        try:
            body = await client.http.get(f"/clip/v2/resource/light/{light_id}")
            light = unwrap_one(body, Light)
        except HueError:
            logger.exception(
                "light %s could not be read; it will not breathe", light_id
            )
            continue
        if light.dimming is None or light.on is None:
            logger.info("light %s has no level to breathe with", light_id)
            continue
        found.append(
            Resting(
                light_id=light_id,
                on=light.on.on,
                brightness=light.dimming.brightness,
            )
        )
    return found


async def _put(client: PlanClient, light_id: str, payload: dict[str, Any]) -> None:
    """Write one light, raising on a refusal reported in the body.

    Args:
        client: The client to write through.
        light_id: The light.
        payload: The body.

    """
    path = f"/clip/v2/resource/light/{light_id}"
    logger.debug("PUT %s %s", path, payload)
    _ = unwrap(await client.http.put(path, payload), ResourceIdentifier)


async def _inhale(client: PlanClient, light: Resting, seconds: float) -> None:
    """Breathe in: dip a lit light, raise a dark one.

    Args:
        client: The client to write through.
        light: Where the light rests.
        seconds: How long the half takes.

    """
    if light.on:
        low = max(BREATH_FLOOR, light.brightness * BREATH_DIP)
        await _put(
            client,
            light.light_id,
            build_light_payload(brightness=low, transition=seconds),
        )
        return
    # Switched on at the floor first, so the rise starts from nothing
    # rather than from the stored level.
    await _put(
        client,
        light.light_id,
        build_light_payload(on=True, brightness=BREATH_FLOOR, transition=0),
    )
    await _put(
        client,
        light.light_id,
        build_light_payload(brightness=light.brightness, transition=seconds),
    )


async def _exhale(client: PlanClient, light: Resting, seconds: float) -> None:
    """Breathe out: bring a lit light back to its level, a dark one out again.

    A fade to off leaves ``dimming`` where it was (measured: three spots at
    100 % breathed twice from dark and read off at 100 % after), so a dark
    light keeps the level the plan stored for its next switch-on.

    Args:
        client: The client to write through.
        light: Where the light rests.
        seconds: How long the half takes.

    """
    if light.on:
        payload = build_light_payload(brightness=light.brightness, transition=seconds)
    else:
        payload = build_light_payload(on=False, transition=seconds)
    await _put(client, light.light_id, payload)


async def breathe(
    client: PlanClient,
    lights: Sequence[Resting],
    *,
    breaths: int,
    period: float,
    sleep: Sleeper = asyncio.sleep,
) -> None:
    """Breathe lights together, then leave each where it rests.

    Every light's half goes out at once and the bridge runs the fades, so the
    lights move in step (measured: three spots, in sync). A light the bridge
    refuses is logged, put back, and left out of the rest; the others go on.
    Cancelled part-way -- the runner closing -- every light is put back at
    once rather than left dipped, or lit when it was dark.

    Args:
        client: The client to write through.
        lights: Where each light rests, from :func:`read_lights`.
        breaths: How many.
        period: How long one takes, down and back up.
        sleep: How to wait for a half to land. Injectable for tests.

    """
    half = period / 2
    active = list(lights)

    async def phase(
        step: Callable[[PlanClient, Resting, float], Awaitable[None]],
    ) -> None:
        nonlocal active

        async def one(light: Resting) -> bool:
            try:
                await step(client, light, half)
            except HueError:
                logger.exception("light %s could not breathe", light.light_id)
                return False
            return True

        done = await asyncio.gather(*(one(light) for light in active))
        failed = [light for light, ok in zip(active, done, strict=True) if not ok]
        active = [light for light, ok in zip(active, done, strict=True) if ok]
        if failed:
            await settle(client, failed)

    try:
        for _ in range(breaths):
            await phase(_inhale)
            await sleep(half)
            await phase(_exhale)
            await sleep(half)
    except asyncio.CancelledError:
        await asyncio.shield(settle(client, active))
        raise


async def settle(client: PlanClient, lights: Sequence[Resting]) -> None:
    """Put lights back where they rest, at once.

    Args:
        client: The client to write through.
        lights: Where each light rests.

    """

    async def one(light: Resting) -> None:
        payload = (
            build_light_payload(brightness=light.brightness, transition=0)
            if light.on
            else build_light_payload(on=False, transition=0)
        )
        try:
            await _put(client, light.light_id, payload)
        except HueError:
            logger.exception("light %s could not be put back", light.light_id)

    _ = await asyncio.gather(*(one(light) for light in lights))


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
