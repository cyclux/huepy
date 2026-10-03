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
"""The lowest level a lit light dips to, however dim it already is."""

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


async def put_together(
    client: PlanClient, writes: Sequence[tuple[str, dict[str, Any]]], *, what: str
) -> list[str]:
    """Write several lights in one batch, so they move in step.

    One batch, not one write each: the transport spaces single light writes
    a tenth of a second apart across the bridge, and three lights each a
    tenth behind the last were visibly out of step in a breath. A batch
    spends the same budget up front and goes out at once (measured: in step
    on three spots, lit and dark). A refusal is read per light, so one bulb
    that cannot do it does not fail the others.

    Args:
        client: The client to write through.
        writes: Each light's id and body.
        what: What the writes were for, for the log.

    Returns:
        The ids of the lights whose write failed, each already logged.

    """
    if not writes:
        return []
    batch = [(f"/clip/v2/resource/light/{light_id}", body) for light_id, body in writes]
    for path, body in batch:
        logger.debug("PUT %s %s", path, body)
    try:
        bodies = await client.http.put_batch(batch)
    except HueError:
        logger.exception(
            "%d light%s could not %s",
            len(writes),
            "" if len(writes) == 1 else "s",
            what,
        )
        return [light_id for light_id, _ in writes]
    failed: list[str] = []
    for (light_id, _), body in zip(writes, bodies, strict=True):
        try:
            _ = unwrap(body, ResourceIdentifier)
        except HueError:
            logger.exception("light %s could not %s", light_id, what)
            failed.append(light_id)
    return failed


async def flash(
    client: PlanClient, light_ids: Iterable[str], *, blinks: int
) -> list[str]:
    """Blink lights with the bridge's ``on_off`` signal.

    Sent to each light, not to a room's ``grouped_light``: the group allows
    about one command a second, and one bulb that cannot signal must not
    stop the others blinking. The writes go out as one batch, so the lights
    blink in step.

    Args:
        client: The client to write through.
        light_ids: The lights to blink.
        blinks: How many times.

    Returns:
        The ids of the lights the bridge refused, each already logged.

    """
    duration = round(flash_seconds(blinks) * MILLISECONDS_PER_SECOND)
    payload = {"signaling": {"signal": str(Signal.ON_OFF), "duration": duration}}
    writes = [(light_id, payload) for light_id in dict.fromkeys(light_ids)]
    return await put_together(client, writes, what="flash")


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


def _inhale(light: Resting, seconds: float) -> dict[str, Any]:
    """Compose a breath in: a lit light dips, a dark one rises.

    A dark light is switched on and faded to its level in the one write: the
    bridge fades it in from dark (measured), so the rise needs no separate
    switch-on that a batch's budget would hold back.

    Args:
        light: Where the light rests.
        seconds: How long the half takes.

    Returns:
        The light's body.

    """
    if light.on:
        low = max(BREATH_FLOOR, light.brightness * BREATH_DIP)
        return build_light_payload(brightness=low, transition=seconds)
    return build_light_payload(on=True, brightness=light.brightness, transition=seconds)


def _exhale(light: Resting, seconds: float) -> dict[str, Any]:
    """Compose a breath out: a lit light comes back, a dark one goes out.

    A fade to off leaves ``dimming`` where it was (measured: three spots at
    100 % breathed twice from dark and read off at 100 % after), so a dark
    light keeps the level the plan stored for its next switch-on.

    Args:
        light: Where the light rests.
        seconds: How long the half takes.

    Returns:
        The light's body.

    """
    if light.on:
        return build_light_payload(brightness=light.brightness, transition=seconds)
    return build_light_payload(on=False, transition=seconds)


async def breathe(
    client: PlanClient,
    lights: Sequence[Resting],
    *,
    breaths: int,
    period: float,
    sleep: Sleeper = asyncio.sleep,
) -> None:
    """Breathe lights together, then leave each where it rests.

    Each half is one batch of writes and the bridge runs the fades, so the
    lights move in step. A light the bridge refuses is logged, put back, and
    left out of the rest; the others go on. Cancelled part-way -- the runner
    closing -- every light is put back at once rather than left dipped, or
    lit when it was dark.

    Args:
        client: The client to write through.
        lights: Where each light rests, from :func:`read_lights`.
        breaths: How many.
        period: How long one takes, down and back up.
        sleep: How to wait for a half to land. Injectable for tests.

    """
    half = period / 2
    active = list(lights)

    async def phase(compose: Callable[[Resting, float], dict[str, Any]]) -> None:
        nonlocal active
        writes = [(light.light_id, compose(light, half)) for light in active]
        failed = set(await put_together(client, writes, what="breathe"))
        if failed:
            await settle(
                client, [light for light in active if light.light_id in failed]
            )
            active = [light for light in active if light.light_id not in failed]

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
    writes = [
        (
            light.light_id,
            build_light_payload(brightness=light.brightness, transition=0)
            if light.on
            else build_light_payload(on=False, transition=0),
        )
        for light in lights
    ]
    _ = await put_together(client, writes, what="be put back")


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
