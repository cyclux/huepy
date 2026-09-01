# huepy

[![Python](https://img.shields.io/badge/python-3.13%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](https://spdx.org/licenses/MIT.html)

A modern async Python wrapper for the **Philips Hue v2 CLIP API**.

- Async-only, built on `aiohttp`
- Finds the bridge for you: `await huepy.discover()` over mDNS or Hue's cloud endpoint
- Verifies the bridge's TLS certificate against Signify's bundled roots by default, and pins the bridge id
- Ask for what you named: `await hue.rooms.get("Kitchen")`, not an opaque id
- Issue a one-shot command without fetching first: `await hue.rooms.set("Kitchen", brightness=40)`
- Whatever you fetch acts on itself -- `await light.turn_on()`, `await scene.activate()`
- One state change is one write: `set()` composes power, brightness, colour
  and transition into a single PUT
- Colour in human units -- `rgb=`, `hex_color=`, `kelvin=` -- clamped to the
  gamut the bulb itself reports
- The full light surface -- effects (tinted, paced), timed sunrise/sunset,
  gradients, signalling, powerup -- plus smart scenes and recall by name
- Transitions in seconds on every light command
- Paces writes to the bridge's documented throughput budget so a burst can't clog it
- 35 typed resource types, including pairing a new device via `zigbee_device_discovery`
- Every response is a validated **pydantic** model, not a bare dict
- `Hue(state=True)` tracks the whole resource graph in the background
- `Hue(record=SQLiteSink(...))` persists every change to a queryable file
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

Don't know the bridge's address yet? `await huepy.discover()` finds it on the
local network via mDNS, falling back to Hue's cloud discovery endpoint:

```python
import huepy

for bridge in await huepy.discover():
    print(bridge.bridge_id, bridge.ip)
```

Runnable as [`examples/discover_bridge.py`](examples/discover_bridge.py); see
[`examples/from_discovery.py`](examples/from_discovery.py) to discover and
connect in one call with `Hue.from_discovery()`.

An **application key** is a credential the bridge issues to your application,
not a Hue account password. What authorizes it is physical: pressing the
bridge's **link button** proves whoever is pairing has local access to it, so
`authenticate()` polls the bridge for up to 60 seconds waiting for that press.

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

TLS is verified by default: a real bridge's certificate is signed by Signify's
private root CA (bundled with huepy) and carries the bridge id as its common
name. Supply `bridge_id=` (or `HUE_BRIDGE_ID`) to pin that identity; without it,
huepy still proves the peer is a genuine Hue bridge but warns that the exact
bridge is not pinned. For development against a proxy or emulator, pass
`tls=TlsMode.INSECURE` to skip verification.

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
`zones`, `scenes`, `smart_scenes`, `devices`, and `service_groups`. Every
collection uses the same `get(name)`, `list()`, `names()`, `rename(name,
new_name)`, and `delete(name)` vocabulary. Matching ignores case and
surrounding whitespace.

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
For several names, call `list()` once and match locally. With `Hue(state=True)`,
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
included. `set_effect()` (optionally tinted with a colour or colour
temperature), `set_timed_effect()`, `set_gradient()`, `set_powerup()`,
`signal()`, `identify()` and `alert()` are lights only -- a `grouped_light`
service does not accept them.

A room or zone also resolves its own membership, and can save and put back the
state of everything in it:

```python
kitchen = await hue.rooms.get("Kitchen")
lights = await kitchen.lights()

before = await kitchen.capture()
await kitchen.set(brightness=30, kelvin=2200, transition=2.0)
await kitchen.restore(before, transition=2.0)
```

A group's children are references, not lights -- devices for a room, light
services for a zone -- so `lights()` does that join for you. `restore()`
deliberately sends one concurrent request per light rather than one to the
group: a `grouped_light` reports no aggregate colour temperature, so restoring
through it drops the colour temperature and leaves the room the wrong colour.
Those per-light writes are still safe on a large room, because the client paces
writes to the bridge's throughput budget (~10/s per light, ~1/s to the shared
broadcast budget that groups and scene recalls draw from); pass
`rate_limit=False` to `Hue(...)` to manage pacing yourself.

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

Reading a colour back uses the same words: `light.hex_color`, `light.rgb`,
`light.kelvin` and `light.mirek` are the read side of the arguments above.

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
await hue.smart_scenes.activate("Daily rhythm")  # follows its weekly schedule
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

### Same task, two ways

Neither level is the "old" one. The named layer is where the work is already
done for you; `hue.api` and `huepy.color` are where you go when you hold an id,
need a type with no collection, or want to see the conversion happen. Three
examples solve one task both ways in a single file, over the same input:

| Task | Lower level | Named layer |
| --- | --- | --- |
| Describe an event | walk every optional section by hand | `resource.summary` |
| Describe a change | read `change.delta` as a nested dict | `change.summary` |
| Dim and warm a room | resolve the name, hop to `grouped_light`, convert Kelvin and seconds, build the payload | `await room.set(brightness=30, kelvin=2200, transition=2.0)` |
| List a room's lights | join `room.children` to each light's `device_id` | `await room.lights()` |
| Save and put a room back | a dict of `capture()` plus a restore loop | `await room.capture()` / `await room.restore(before)` |
| Set a hex colour | hex to RGB to xy, clamp to the bulb's gamut, build the payload | `await light.set(hex_color="#3366ff")` |
| Read a colour back | `rgb_to_hex(xy_to_rgb((x, y), brightness))` | `light.hex_color` |
| Wait for one change | loop with a break, and remember to close the iterator | `await hue.state.wait_for(name="Desk lamp", timeout=5)` |
| Only changes in one room | resolve the room inside every handler | `on_change(handler, room="Kitchen")` |

Run them with [`two_ways_events.py`](examples/two_ways_events.py),
[`two_ways_room.py`](examples/two_ways_room.py) and
[`two_ways_color.py`](examples/two_ways_color.py); the events one prints both
descriptions side by side and flags any disagreement.

### Resources

`hue.api.lights`, `grouped_lights`, `light_levels`, `grouped_light_levels`,
`rooms`, `zones`, `scenes`, `smart_scenes`, `devices`, `device_powers`,
`bridges`, `bridge_homes`, `service_groups`, `motions`, `grouped_motions`,
`temperatures`, `buttons`, `contacts`, `relative_rotaries`, and
`zigbee_connectivities` expose the complete typed CLIP v2 resource API.
`hue.api` also reaches entertainment areas, automation and presence,
device firmware, extra connectivity services, and HomeKit/Matter/Hue Secure
integrations -- see [`API_REFERENCE.md`](API_REFERENCE.md#resource-handlers)
for the full table. Pairing a new light or other Zigbee device goes through
`hue.api.zigbee_device_discoveries`; there is no other route to it in the v2
API.

### Events

The stream yields parsed events, not dicts:

```python
async with Hue(state=True) as hue:
    async for event in hue.get_event_stream():
        for resource in event.data:
            print(hue.get_name(resource.id), resource.summary)
```

`resource.summary` describes whichever sections the event carries -- `"on, 62%,
2700 K"`, `"motion"`, `"22.4 °C"` -- so following a stream does not mean
checking every optional section by hand. `huepy.summarize` is the same function
over a plain payload, for a raw event or a section huepy has no model for yet.

`event.resource_ids` lists the ids an event touches, and `hue.get_name(...)`
turns any of them into the name you gave it. The stream reconnects on its own
with exponential backoff, and drops an unparseable event with a warning rather
than ending. For the raw decoded payloads,
`hue.api.raw.subscribe_events()` is the escape hatch.

Event deltas are typed for lights, motion, temperature, ambient light, buttons,
contact sensors, battery state and relative rotary input. Unknown future
sections remain available through `model_extra`, and `summary` renders those too.

### Last-reported state

By default every lookup is a fresh bridge read; pass `state=True` and huepy
instead maintains a local graph from one snapshot plus the event stream, so
reads become local and reflect what the bridge last reported, not a guarantee
of a light's physical state. Startup takes an aggregate snapshot while
buffering the event stream, so the graph has no snapshot/event gap.

```python
async with Hue(state=True) as hue:
    desk = hue.state.lights.get("Desk lamp")
    print(hue.state.connected, desk.brightness)
    print(hue.state.room_of(desk.id))
```

`hue.state` exists from construction and is never `None`, so nothing has to be
threaded through your call stack. Reading it before tracking starts raises
`StateNotStartedError` rather than reporting an empty bridge. To scope it
yourself instead, `async with hue.state as state:` enters the same object.

The local `lights`, `rooms`, `zones`, `scenes` and `devices` views provide
synchronous `get(name)`, `by_id(id)`, `list()` and `names()` lookup. `resources`,
`by_id(id)`, `list(Model)`, `lights_in`, `room_of`, `zones_of`, `device_of` and
`name_of` support generic and topology queries. Returned models are fresh,
bound copies and cannot mutate the canonical state.

Register a handler instead of owning the loop:

```python
hue.state.on_change(lambda change: print(change.at, change.summary), name="Desk lamp")
hue.state.on_change(lambda change: print(change.summary), room="Kitchen")
hue.state.on_resync(lambda marker: print("gap", marker.reason))
```

`on_change` takes `name`, `model`, `resource_id`, `kind` and `room` filters, all
ANDed, and returns a `Subscription` you can `cancel()` or scope with `with`.
`room=` resolves through the resource's owning device, and still matches a
delete, whose resource has already left the graph. It is the costliest of the
filters, since resolving topology revalidates the room set per change; reach
for another one when it will do. Markers reach `on_resync`
only, so no `isinstance` guard is needed. A handler that raises is logged and
skipped. `state.watch(...)` is the same filtering as an async iterator, for
callers who want the loop, and `change.summary` renders the delta the same way
an event's does.

To wait for one change rather than follow all of them:

```python
await hue.lights.turn_on("Desk lamp")
change = await hue.state.wait_for(name="Desk lamp", timeout=5.0)
```

`wait_for` registers before it yields to the event loop, so the change caused by
the write above it is not missed, and it closes its iterator on every exit --
including a `TimeoutError`.

`state.changes()` yields `huepy.state.Change` records plus `Resync` markers.
Each subscriber has bounded independent history; a marker records reconnect,
inconsistency, or lag where complete history cannot be proved. State does not
apply writes optimistically. Changes from this client may be correlated as
`origin="self"`; `state.fading` exposes active locally issued fades.

### Recording history

Persisting the stream is one argument. `record=` implies `state=True`.

```python
from huepy import Hue, SQLiteSink

async with Hue(record=SQLiteSink("hue-history.sqlite3")):
    await asyncio.Event().wait()
```

`SQLiteSink`, `JSONLSink` and `LoggingSink` ship with the library, all
stdlib-only. The file sinks do their writing on a thread of their own, so a slow
disk never stalls the event stream. SQLite gets `change`, `resync` and `current`
tables, so "when was the Desk lamp last on?" and "what is it now?" are both one
indexed query. When a sink cannot keep up or fails, the loss is written into the
history as a `Resync` row rather than silently dropped. Write your own sink by
satisfying the `HistorySink` protocol.

### Declarative plans

Describe the day in a TOML file and let the library run it.

```toml
version = 1

[location]
latitude = 48.137
longitude = 11.575
timezone = "Europe/Berlin"

[[scenario]]
name = "living-room-day"
scope = ["room:Living Room"]

[[scenario.step]]
at = "sunrise-15m"
ramp = "45m"
set = { on = true, brightness = 40, kelvin = 2200 }

[[scenario.step]]
at = "sunset+30m"
ramp = "2h"
set = { brightness = 60, kelvin = 2700 }
```

```python
from huepy import Hue, PlanRunner, load_plans

async with Hue(state=True) as hue:
    async with PlanRunner(hue, load_plans("./plans"), changes=hue.state) as runner:
        await runner.run()
```

Or from the shell: `huepy plan explain ./plans` prints the day with every solar
anchor resolved and never touches the bridge; `huepy plan run ./plans` executes
it.

Sunrise is computed in-process, because the bridge cannot help — its
`geolocation` resource reports a sunset but no sunrise, and smart scene
timeslots accept no offsets. Fades are handed to the bridge whole rather than
stepped: it runs a transition of up to 6,000 seconds from one PUT, so a
ninety-minute sunset is a single request. Longer ramps are chained, a room is
written through its `grouped_light`, and a scope someone changes by hand is left
alone until its next scheduled step or trigger.

Rules react to the bridge's sensors and to signals from your own code:

```toml
[[scenario]]
name = "hall-night-light"
scope = ["room:Hallway"]
priority = 10

[[scenario.rule]]
when = "motion:Hall sensor"
between = ["sunset", "sunrise"]
hold = "90s"
set = { on = true, brightness = 15 }
```

The hall comes up dim when the sensor fires at night, stays while the sensor
keeps reporting motion and for ninety seconds after it stops, then hands back
to whatever lower-priority scenario is underneath, over that scenario's ramp.
`button:` fires on a press, `contact:` when a door opens, and `signal:name`
when your code calls `runner.fire("name")`.

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

A write can come back HTTP 207 (Multi-Status) with an `errors[]` array in the
body even though the request itself succeeded. huepy treats two advisory
codes, `communication_error` and `attribute_may_have_no_effect`, as warnings --
they are logged and the call returns normally -- and raises `HueResponseError`
for every other, blocking error. See
[Partial failures](API_REFERENCE.md#partial-failures) for the full breakdown.

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

The runnable examples are grouped by the API level they are meant to teach:

- Same task, two ways: [`two_ways_events.py`](examples/two_ways_events.py),
  [`two_ways_room.py`](examples/two_ways_room.py) and
  [`two_ways_color.py`](examples/two_ways_color.py) each solve one task twice in
  one file -- once against the raw or id-addressed API, once against the named
  one -- over the same input, so the two can be compared line for line.
- High level: [`basic.py`](examples/basic.py) lists bound models through the
  name-oriented collections; [`control_room.py`](examples/control_room.py) and
  [`control_zone.py`](examples/control_zone.py) resolve and control a room or
  zone by name in one composed command; [`color_light.py`](examples/color_light.py)
  uses human colour units; and [`scenes.py`](examples/scenes.py) recalls a scene
  or runs a smart scene's schedule.
- Light features: [`effects.py`](examples/effects.py) runs the bridge's own
  candle, sunrise and gradient animations; [`attention.py`](examples/attention.py)
  covers identify, signal, alert and relative brightness/colour nudges; and
  [`powerup.py`](examples/powerup.py) sets what a light does when its mains
  power returns.
- Devices: [`pair_device.py`](examples/pair_device.py) pairs a new bulb through
  the `zigbee_device_discovery` search -- the only way to add one in the v2 API.
- Lower level: [`low_level.py`](examples/low_level.py) shows typed, ID-addressed
  handlers and the raw decoded transport side by side, without changing state.
- Live state and events: [`listen_events.py`](examples/listen_events.py) prints
  typed event deltas; [`track_state.py`](examples/track_state.py) queries and
  follows `HueState`; and [`record_history.py`](examples/record_history.py)
  persists changes and uncertainty markers.
- Setup: [`authenticate.py`](examples/authenticate.py) performs the one-time
  bridge registration used by every other example;
  [`discover_bridge.py`](examples/discover_bridge.py) finds bridges on the
  network; and [`from_discovery.py`](examples/from_discovery.py) discovers and
  connects in one call.

See [`API_REFERENCE.md`](API_REFERENCE.md) for the full surface and
[`examples/README.md`](examples/README.md) for the complete, categorised script
index. The reconnect, state-folding, and write-correlation design is recorded
in [`STATE_LAYER.md`](STATE_LAYER.md).

## License

`huepy` is distributed under the terms of the [MIT](https://spdx.org/licenses/MIT.html) license.
