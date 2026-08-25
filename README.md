# huepy

[![Python](https://img.shields.io/badge/python-3.13%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](https://spdx.org/licenses/MIT.html)

A modern async Python wrapper for the **Philips Hue v2 CLIP API**.

- Async-only, built on `aiohttp`
- Ask for what you named: `await hue.rooms.get("Kitchen")`, not an opaque id
- Issue a one-shot command without fetching first: `await hue.rooms.set("Kitchen", brightness=40)`
- Whatever you fetch acts on itself -- `await light.turn_on()`, `await scene.activate()`
- One state change is one write: `set()` composes power, brightness, colour
  and transition into a single PUT
- Colour in human units -- `rgb=`, `hex_color=`, `kelvin=` -- clamped to the
  gamut the bulb itself reports
- Transitions in seconds on every light command
- Every response is a validated **pydantic** model, not a bare dict
- `Hue(live=True)` keeps high-level name lookups current from the event stream
- Models tolerate unknown fields, so bridge firmware updates don't break parsing
- Failures reported in a successful response body (`errors[]`) are raised, not silently ignored
- Ships `py.typed` -- your type checker sees the annotations

## Installation

```console
pip install git+https://github.com/cyclux/huepy
```

Requires Python 3.13+.

> Not on PyPI: the name `huepy` there belongs to an unrelated package (terminal
> colours). Install from git.

## Set up once

The bridge issues an application key when its link button is pressed:

```console
python examples/authenticate.py 192.168.1.100
```

This writes **both** the bridge address and the key to
`$XDG_CONFIG_HOME/huepy/config.json` (`~/.config/huepy/config.json` by
default), restricted to your user. Every later run needs no arguments and no
environment, from any directory:

```console
python examples/basic.py
```

`HUE_BRIDGE_IP` and `HUE_APP_KEY` override the stored values, and
`HUE_CONFIG_PATH` moves the file itself.

If the target filesystem ignores `chmod` -- a Windows drive mounted under
WSL, say -- huepy raises an `InsecureConfigWarning` rather than leaving you
believing the key is protected. Point `HUE_CONFIG_PATH` at a native path to
resolve it.

## Usage

```python
import asyncio

from huepy import Hue


async def main() -> None:
    async with Hue() as hue:
        await hue.rooms.set(
            "Kitchen", on=True, brightness=30, kelvin=2200, transition=2.0
        )

        for light in await hue.lights.list():
            print(light.name, light.is_on, light.brightness)


asyncio.run(main())
```

`Hue()` picks up the address and key stored during setup; pass
`Hue(bridge_ip=..., app_key=...)` to override either. Entering the context
opens the session without fetching resources; leaving it closes the session.
`start()` and `close()` are available when you prefer to manage that lifecycle
yourself.

That `set()` is **one PUT**. In stateless mode the collection first performs
one GET to resolve `"Kitchen"`; the room then carries the reference to its own
`grouped_light` service, so switch on, dim, warm and fade travel together.

### Lookup by name

The top-level collections are the human-facing API: `lights`, `rooms`,
`zones`, `scenes`, `devices`, and `service_groups`. Every collection uses the
same `get(name)`, `list()`, `names()`, `rename(name, new_name)`, and
`delete(name)` vocabulary. Matching ignores case and surrounding whitespace.

```python
kitchen = await hue.rooms.get("Kitchen")
desk = await hue.lights.get("Desk lamp")
print(await hue.rooms.names())  # what may I ask for?
```

A miss is not a `None` you have to check for:

```python
from huepy import AmbiguousResourceError, ResourceNotFoundError

try:
    room = await hue.rooms.get("Kithcen")
except ResourceNotFoundError as exc:
    print(f"No room named {exc.name!r}. Known rooms: {', '.join(exc.known)}")
```

In the default stateless mode, each lookup is one round trip: the bridge offers
no server-side name filter, so huepy fetches and matches the collection locally.
For several names, call `list()` once and match locally. With `Hue(live=True)`,
an application key is required; the initial aggregate snapshot and event stream
maintain a local resource graph, so later high-level lookups use local state.
While the event stream is reconnecting, high-level lookups raise
`BridgeConnectionError` instead of resolving commands against stale names.

Creation remains explicitly low-level (`hue.api.rooms.create(...)`, and so on)
because it requires bridge ids or CLIP reference shapes rather than display
names.

### Commands on what you fetched

Anything from `get()` or `list()` is *bound* to the
client that fetched it, and issues its own commands:

```python
light = await hue.lights.get("Desk lamp")
await light.turn_on(transition=1.5)
await light.set_brightness(60)
await light.alert()  # flash once to identify it
await light.refresh()  # a fresh snapshot; the old one is untouched

scene = await hue.scenes.get("Relax")
await scene.activate()
```

`update(data)`, `delete()` and `refresh()` are on every resource; `set()` and
the light commands are on anything that behaves like a light, rooms and zones
included. `set_effect()`, `set_gradient()`, `set_powerup()` and `alert()` are
lights only -- a `grouped_light` service does not accept them.

A model you built by hand has no client to talk to, and says so rather than
failing obscurely:

```python
from huepy import DetachedResourceError, models

try:
    await models.Light(id="abc").turn_on()
except DetachedResourceError as exc:
    print(exc)  # ... fetch it via hue.lights.get(...) to get a bound one
```

### Colour

Give a colour in whichever unit you are thinking in. On a `Light` the value is
clamped to the gamut that particular bulb reports, so a colour it cannot
reproduce lands on the nearest one it can, rather than on whatever the bridge
decides to substitute:

```python
await light.set_rgb((255, 136, 0))
await light.set(hex_color="#ff8800", transition=1.0)
await light.set_kelvin(2200)  # warm white
await light.set_color(0.5, 0.4)  # CIE xy, if you have it already
```

`rgb`, `hex_color` and `xy` are three spellings of one colour, and `kelvin` and
`mirek` two spellings of one colour temperature: passing two of either, or a
colour together with a colour temperature, is a `ValueError` rather than a
silent choice.

`huepy.color` is public and free of any bridge dependency, for converting
before you are anywhere near a light:

```python
from huepy.color import GAMUT_C, clamp_to_gamut, hex_to_rgb, kelvin_to_mirek, rgb_to_xy

xy = clamp_to_gamut(rgb_to_xy(hex_to_rgb("#ff8800")), GAMUT_C)
warm = kelvin_to_mirek(2700)
```

### Transitions

`transition=` takes seconds, and every light command accepts it:

```python
await light.turn_off(transition=5.0)
await kitchen.set(brightness=100, kelvin=4000, transition=2.0)
```

### One-shot commands

For one command, use the same collection vocabulary without separately
fetching a bound model:

```python
await hue.lights.turn_on("Desk lamp", transition=1.5)
await hue.rooms.set("Kitchen", brightness=40, kelvin=2200)
await hue.scenes.activate("Relax")
```

The direct command first resolves the unique name, then delegates to the same
bound-resource command. It returns `CommandResult`, whose `sent` says whether
a request was issued and whose `resources` are the bridge references affected.

### Working with ids

The id-based handlers are still there, and still supported -- they are the
lower-level vocabulary, not a deprecated one. Reach for them when you already
hold an id, when you are writing something generic over resource types, or
when you want a payload the models do not spell:

```python
room = await hue.api.rooms.get(room_id)
service_id = room.service_id("grouped_light")
if service_id is not None:
    await hue.api.grouped_lights.set_brightness(service_id, 40.0)
await hue.api.lights.update(light_id, {"on": {"on": True}})
await hue.api.lights.delete(light_id)
```

`hue.api` is the typed bridge-facing surface. Its plural handlers are strictly
id-addressed and uniformly provide `list()`, `get(resource_id)`,
`update(resource_id, data)`, and `delete(resource_id)`, plus resource-specific
commands. `hue.api.raw` is the decoded-JSON transport escape hatch.

### Resources

`hue.api.lights`, `grouped_lights`, `light_levels`, `grouped_light_levels`,
`rooms`, `zones`, `scenes`, `devices`, `device_powers`, `bridges`,
`bridge_homes`, `service_groups`, `motions`, `grouped_motions`,
`temperatures`, `buttons`, `contacts`, `relative_rotaries`, and
`zigbee_connectivities` expose the complete typed CLIP v2 resource API.

### Events

The stream yields parsed events, not dicts:

```python
async with Hue(live=True) as hue:
    async for event in hue.get_event_stream():
        if not event.is_update:
            continue
        for resource in event.data:
            state = "on" if resource.on is not None and resource.on.on else "-"
            print(hue.get_name(resource.id), resource.type, state)
```

`event.resource_ids` lists the ids an event touches, and `hue.get_name(...)`
turns any of them into the name you gave it. The stream reconnects on its own
with exponential backoff, and drops an unparseable event with a warning rather
than ending. For the raw decoded payloads, `hue.http.subscribe_events()` is
the escape hatch.

Event deltas are typed for lights, motion, temperature, ambient light, buttons,
contact sensors, battery state and relative rotary input. Unknown future
sections remain available through `model_extra`.

### Last-reported state

For a continuously maintained local view, enter `hue.state()` inside the open
client. Startup takes an aggregate snapshot while buffering the event stream,
so the returned view has no snapshot/event gap. It reports what the bridge
last sent, not a guarantee of a light's physical state.

```python
async with Hue() as hue:
    async with hue.state() as state:
        desk = state.lights.get("Desk lamp")
        print(state.connected, desk.brightness)
        print(state.room_of(desk.id))
```

The local `lights`, `rooms`, `zones`, `scenes` and `devices` views provide
synchronous `get(name)`, `by_id(id)`, `list()` and `names()` lookup. `resources`,
`by_id(id)`, `list(Model)`, `lights_in`, `room_of`, `zones_of`, `device_of` and
`name_of` support generic and topology queries. Returned models are fresh,
bound copies and cannot mutate the canonical state.

`state.changes()` yields `huepy.state.Change` records plus `Resync` markers.
Each subscriber has bounded independent history; a marker records reconnect,
inconsistency, or lag where complete history cannot be proved. State does not
apply writes optimistically. Changes from this client may be correlated as
`origin="self"`; `state.fading` exposes active locally issued fades.

### Errors

All errors derive from `HueError`:

| Exception | Raised when |
| --- | --- |
| `AuthenticationError` | No application key, or the bridge refused one |
| `BridgeConnectionError` | The bridge is unreachable |
| `HueAPIError` | Non-2xx HTTP status (carries `status_code`) |
| `HueResponseError` | A successful HTTP response with blocking errors, or no successful data, in the body (carries `errors`) |
| `ResourceNotFoundError` | No resource carries the requested name (carries `name` and `known`) |
| `AmbiguousResourceError` | More than one resource carries a requested name (carries `name` and `resource_ids`) |
| `DetachedResourceError` | A command was issued on a model that was never fetched |

The transport limits itself to three concurrent connections per bridge. It
retries GET responses with status 429 or 503 up to three times; mutating PUT,
POST and DELETE requests are never replayed automatically.

## Development

```console
uv sync
uv run pytest
uv run ruff check .
uv run basedpyright
```

### Integration tests

A second suite runs against a real bridge. It is excluded from `pytest` by
default and additionally refuses to run without an explicit opt-in, because it
physically changes your lights:

```console
HUEPY_INTEGRATION=1 uv run pytest -m integration
```

Every test that changes lights snapshots their state first and restores it
afterwards, including on failure; a suite-wide safety fixture restores all
lights touched through the standard integration fixtures. These are the tests
that catch what unit tests cannot: real firmware sending a shape no fixture
predicted.

The runnable examples include direct control, typed events, the maintained
state view, and a SQLite history recorder:

- [`examples/basic.py`](examples/basic.py) — connect and list resources
- [`examples/listen_events.py`](examples/listen_events.py) — print typed event deltas
- [`examples/track_state.py`](examples/track_state.py) — query and follow `HueState`
- [`examples/record_history.py`](examples/record_history.py) — persist changes and uncertainty markers

See [`API_REFERENCE.md`](API_REFERENCE.md) for the full surface and
[`examples/`](examples/) for runnable scripts. The reconnect, state-folding,
and write-correlation design is recorded in
[`STATE_LAYER.md`](STATE_LAYER.md).

## License

`huepy` is distributed under the terms of the [MIT](https://spdx.org/licenses/MIT.html) license.
