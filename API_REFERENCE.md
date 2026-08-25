# huepy API Reference

Async wrapper for the Philips Hue v2 CLIP API. Bridge I/O is asynchronous and
resource responses are validated pydantic models.

Anything the client hands you is **bound**: it carries the client that fetched
it and can act on itself, so most code never handles a bridge id.

```python
kitchen = await hue.rooms["Kitchen"]
await kitchen.set(brightness=30, kelvin=2200, transition=2.0)
```

That is one request. The id-based handler API underneath is unchanged and is
documented in [Resource handlers](#resource-handlers); reach for it when you
already hold an id, or need a resource type the models do not cover.

- [Client](#client)
- [Fetching resources](#fetching-resources)
- [Lookup by name](#lookup-by-name)
- [Bound and detached models](#bound-and-detached-models)
- [Commands on every resource](#commands-on-every-resource)
- [Light commands](#light-commands)
- [Transitions](#transitions)
- [Colour](#colour)
- [Rooms and zones](#rooms-and-zones)
- [Lights only](#lights-only)
- [Scenes](#scenes)
- [Models](#models)
- [Events](#events)
- [Last-reported state](#last-reported-state)
- [huepy.color](#huepycolor)
- [Resource handlers](#resource-handlers)
- [Configuration](#configuration)
- [Exceptions](#exceptions)
- [Complete example](#complete-example)

## Client

### `Hue`

```
class Hue:
    def __init__(
        self,
        bridge_ip: str = "",
        app_key: str | None = None,
        config_path: str | Path | None = None,
        *,
        verify_ssl: bool = False,
    )
```

Settings are resolved eagerly, at construction, from three sources in this
order:

| Setting | Argument | Environment | Config file | If nothing is found |
| --- | --- | --- | --- | --- |
| Bridge address | `bridge_ip=` | `HUE_BRIDGE_IP` | `bridge_ip` | `ValueError` from the constructor |
| Application key | `app_key=` | `HUE_APP_KEY` | `app_key` | Stays `None`; `ensure_authenticated()` then raises |
| Config file path | `config_path=` | `HUE_CONFIG_PATH` | — | `$XDG_CONFIG_HOME/huepy/config.json` |
| TLS verification | `verify_ssl=` | — | — | `False` — bridges ship a self-signed certificate |

Use the client as an async context manager, which opens the HTTP session and
loads the id-to-name lookup:

```python
async with Hue(bridge_ip="192.168.1.100") as hue:
    for light in await hue.lights.get_all():
        print(light.name, light.is_on)
```

#### Methods

| Method | Description |
| --- | --- |
| `async start(*, load_names=True) -> None` | Open the session and load the name lookup. Called by `__aenter__`. Pass `load_names=False` to skip the five requests the lookup costs; it is skipped anyway when no application key is available, which is what keeps `authenticate()` reachable on an unkeyed bridge. |
| `async close() -> None` | Close every event stream and the session. Safe to call when not started. |
| `async refresh_names() -> dict[str, str]` | Reload the id-to-name lookup. Devices, lights, rooms, zones and scenes are fetched concurrently — five requests. |
| `get_name(resource_id: str) -> str` | Display name for an id, or `"Unknown"`. Local; no request. |
| `ensure_authenticated() -> None` | Raise `AuthenticationError` if no key is available. Never prompts. |
| `async authenticate(app_name="huepy", timeout=60) -> str` | Obtain a key. The bridge link button must be pressed while this runs. |
| `async get_event_stream() -> AsyncGenerator[models.HueEvent]` | Yield typed events pushed by the bridge. See [Events](#events). |
| `async snapshot() -> list[models.AnyResource]` | Fetch all aggregate-visible resources in one request. Known types use their concrete model; future types use `models.HueResource`. |
| `state() -> huepy.state.HueState` | Create a stopped, opt-in last-reported state view. Enter it as an async context manager. |

#### Attributes

| Attribute | Type | Description |
| --- | --- | --- |
| `config` | `HueConfig` | The resolved settings. |
| `http` | `Transport` | The open transport. Raises `RuntimeError` before `start()`. |
| `names` | `dict[str, str]` | The id-to-name lookup, as loaded by `start()`. |

The package itself exposes `huepy.__version__`, read from the installed
distribution metadata, or `"unknown"` when running from a source tree.

## Fetching resources

Every handler offers `get_all()`, its `all()` alias, and `get(id)`. Handlers
for named resources additionally offer `by_name()`, `names()`, and an awaited
subscript lookup.

| Call | Returns | Cost |
| --- | --- | --- |
| `await hue.lights.get_all()` | `list[models.Light]` | one GET |
| `await hue.lights.all()` | `list[models.Light]` | one GET; the short spelling of `get_all()` |
| `await hue.lights.get(light_id)` | `models.Light` | one GET |
| `await hue.rooms.by_name("Kitchen")` | `models.Room` | one GET of the whole collection |

```python
lights = await hue.lights.all()
on_now = [light.name for light in lights if light.is_on]
```

## Lookup by name

The bridge addresses everything by opaque id, but people think in names. Every
handler whose resources carry a `metadata.name` supports lookup by it.

```python
kitchen = await hue.rooms["Kitchen"]
desk = await hue.lights.by_name("desk lamp")
available = await hue.rooms.names()
```

The subscript is asynchronous: `hue.rooms["Kitchen"]` returns the coroutine
`by_name` would have, so it must be awaited. Matching ignores case and
surrounding whitespace. The bridge permits two resources to share a name; the
first in bridge order wins. A miss raises `ResourceNotFoundError`, which
carries both the name asked for and the names that exist.

```python
try:
    kitchen = await hue.rooms["Kitchn"]
except ResourceNotFoundError as exc:
    print(exc.name, exc.known)
```

**Cost.** The v2 API has no server-side name filter, so each of `by_name`,
`names` and the subscript fetches the whole collection: one round trip per
call. Resolving several names in a loop issues one request per iteration —
call `get_all()` once and match locally instead.

These are the handlers that support it, with the plural alias each one also
answers to:

| Handler | Alias | Model |
| --- | --- | --- |
| `hue.light` | `hue.lights` | `models.Light` |
| `hue.room` | `hue.rooms` | `models.Room` |
| `hue.zone` | `hue.zones` | `models.Zone` |
| `hue.scene` | `hue.scenes` | `models.Scene` |
| `hue.device` | `hue.devices` | `models.Device` |
| `hue.service_group` | — | `models.ServiceGroup` |

The plural names are **aliases, not copies**: `hue.lights is hue.light` is
true, and either spelling reaches the same handler object. Both exist because
each reads better in its own place — the singular is the raw, id-based
vocabulary (`hue.light.set_brightness(light_id, 40.0)`), the plural reads
naturally for lookup and iteration (`await hue.rooms["Kitchen"]`). Only the
name-addressable handlers have an alias: the sensor handlers have no plural
spelling, because their resources carry no name to look up.

## Bound and detached models

A model parsed by a handler is bound to the client that fetched it, and can
issue its own requests. A model you construct yourself is detached: it is
plain data, with no bridge to talk to.

```python
light = await hue.lights["Desk lamp"]
print(light.is_bound)  # True
await light.turn_on()

detached = models.Light(id="abc")
await detached.turn_on()  # raises DetachedResourceError
```

`refresh()` returns a new bound model. Commands return the bridge's
`ResourceIdentifier` references instead. `bind(hue, rtype="")` exists for the
rare case of attaching a client to a model you built or cached yourself;
handlers call it for you.

## Commands on every resource

Every addressable resource — light, room, scene, sensor, device — carries
these three.

| Command | Sends | Notes |
| --- | --- | --- |
| `await resource.update(data)` | `PUT` to the resource's own path | `data` is in the bridge's payload shape |
| `await resource.delete()` | `DELETE` | Returns `[]` when the bridge answers with no body |
| `await resource.refresh()` | `GET` | Returns a **new** bound instance; the one you called it on is untouched |

```python
kitchen = await hue.rooms["Kitchen"]
await kitchen.update({"metadata": {"name": "Kitchen (north)"}})
kitchen = await kitchen.refresh()
```

Writes return `list[models.ResourceIdentifier]` — the bridge's references to
what changed, not the changed resource. `refresh()` is how you read the new
state.

## Light commands

`Light`, `GroupedLight`, `Room` and `Zone` share one set of light commands.
`set()` is the whole surface; the rest are thin wrappers over it, so there is
a single code path to the bridge.

```
async set(
    *,
    on: bool | None = None,
    brightness: float | None = None,
    xy: tuple[float, float] | None = None,
    mirek: int | None = None,
    rgb: tuple[int, int, int] | None = None,
    hex_color: str | None = None,
    kelvin: int | None = None,
    gamut: color.Gamut | None = None,
    transition: float | None = None,
) -> list[ResourceIdentifier]
```

| Argument | Type | Meaning |
| --- | --- | --- |
| `on` | `bool` | Power state. |
| `brightness` | `float` | Percentage of the light's usable range, clamped to 0–100. |
| `xy` | `tuple[float, float]` | Colour as a CIE 1931 chromaticity. |
| `rgb` | `tuple[int, int, int]` | Colour as 8-bit channels, each 0–255. |
| `hex_color` | `str` | Colour as `"#rrggbb"` or its `"#rgb"` shorthand. |
| `mirek` | `int` | Colour temperature, 153–500; lower is cooler. |
| `kelvin` | `int` | Colour temperature, 2000–6536; higher is cooler. Out-of-range values are clamped. |
| `gamut` | `color.Gamut` | Triangle the colour is clamped into. Defaults to whatever the resource itself reports. |
| `transition` | `float` | Duration of the change, in seconds. |

Everything supplied goes into **one** PUT:

```python
light = await hue.lights["Desk lamp"]
await light.set(on=True, brightness=40, kelvin=2700, transition=1.5)
```

Rules `set()` enforces before it sends anything:

- `xy`, `rgb` and `hex_color` are three spellings of one colour, and `mirek`
  and `kelvin` two spellings of one colour temperature. Passing two of either
  raises `ValueError` rather than silently preferring one.
- A colour and a colour temperature cannot be combined — a light does one or
  the other. That, too, is a `ValueError`.
- A call that supplies nothing sends no request and returns `[]`.

```python
await light.set(rgb=(255, 136, 0), kelvin=2700)  # raises ValueError
```

The wrappers, all of which end up in `set()`:

| Command | Equivalent |
| --- | --- |
| `await light.turn_on(*, transition=None)` | `set(on=True)` |
| `await light.turn_off(*, transition=None)` | `set(on=False)` |
| `await light.set_brightness(brightness, *, transition=None)` | `set(brightness=...)` |
| `await light.set_color(x, y, *, transition=None)` | `set(xy=(x, y))` |
| `await light.set_rgb(rgb, *, transition=None)` | `set(rgb=...)` |
| `await light.set_color_temperature(mirek, *, transition=None)` | `set(mirek=...)` |
| `await light.set_kelvin(kelvin, *, transition=None)` | `set(kelvin=...)` |

`light` above stands for any of the four: the same call on a `Room` or a
`Zone` reaches that group's lights instead.

## Transitions

`transition` is given in **seconds**, as a float, and is sent to the bridge as
`dynamics.duration` in **milliseconds** (`int(transition * 1000)`). It is
accepted by `set()` and by every wrapper above, on lights, grouped lights,
rooms and zones alike.

```python
await light.set_brightness(10, transition=5)  # fade over five seconds
await light.turn_off(transition=0.4)
await light.set(on=True, transition=-1)  # raises ValueError
```

A negative transition or a duration over 6,000 seconds raises `ValueError`. Omitting it lets the bridge use its
own default fade. The [effect, gradient, powerup and alert](#lights-only)
commands take no transition, and neither do the id-based handler commands.

## Colour

A colour can be given in whichever spelling suits the caller, and both
conversion and gamut clamping happen client-side before the request goes out.

```python
await light.set_rgb((255, 136, 0))
await light.set(hex_color="#f80")
await light.set_kelvin(2200, transition=2.0)
```

**Clamping.** A `Light` clamps every colour to the gamut that particular bulb
reports — first the explicit `color.gamut` corners, then the standard triangle
for its `color.gamut_type`, and if it reports neither, nothing is clamped. This
applies to an `xy` pair you supply directly as well: the bridge substitutes an
unreachable colour without saying so, and clamping locally is the only way to
know which colour was actually sent.

A `GroupedLight`, `Room` or `Zone` spans bulbs with different gamuts, so it has
no single triangle and clamps nothing by default. Pass one explicitly if the
group is uniform:

```python
kitchen = await hue.rooms["Kitchen"]
await kitchen.set(rgb=(255, 136, 0), gamut=color.GAMUT_C)
```

The conversions themselves are public and pure — see [huepy.color](#huepycolor).

## Rooms and zones

A room does not accept light commands itself; it owns a `grouped_light`
service that does. A bound room already carries that reference in its
`services`, so it resolves the service from memory and sends **one** request:

```python
kitchen = await hue.rooms["Kitchen"]
await kitchen.set(brightness=40, kelvin=2200, transition=2.0)
```

| Task | Bound room | Id-based handler |
| --- | --- | --- |
| Dim a room | 1 PUT | 1 GET + 1 PUT |
| Dim and warm a room | 1 PUT | 2 GETs + 2 PUTs |
| Look the room up first | 1 GET, then as above | — |

The id-based `hue.room.set_brightness(room_id, 40.0)` re-fetches the room on
every call to find that service, and cannot compose two changes into one
payload — which is why the bound form is worth reaching for.

Zones behave identically, and carry the same commands:

```python
downstairs = await hue.zones["Downstairs"]
await downstairs.turn_off(transition=3)
```

Rooms and zones also carry:

| Member | Returns | Description |
| --- | --- | --- |
| `room.service_id(rtype)` | `str \| None` | The rid of a service of that type, e.g. `models.ResourceType.GROUPED_LIGHT`. |
| `room.contains_device(device_id)` | `bool` | Whether the device is a direct child. |
| `room.children` | `list[ResourceIdentifier]` | Devices in the room; light services in a zone. |
| `room.services` | `list[ResourceIdentifier]` | Services the group exposes. |

`service_id` and `contains_device` read what the model already carries; they
issue no request. A light command on a group with no `grouped_light` service
raises `ValueError`.

`update()` and `delete()` still address the group itself, not its light
service: `await kitchen.update({"metadata": {"name": "Kitchen"}})` renames the
room, while `await kitchen.set(on=True)` switches its lights on.

## Lights only

These live on `models.Light` and nowhere else. The bridge's `grouped_light`
service accepts none of them, so there is no room-wide or zone-wide form: to
run an effect across a room, iterate its lights.

| Command | Sends | Notes |
| --- | --- | --- |
| `await light.set_effect(effect)` | `{"effects": {"effect": ...}}` | An `models.Effect` member or a raw name. `Effect.NO_EFFECT` stops the running one. |
| `await light.set_gradient(colors, *, mode=None)` | `{"gradient": {...}}` | `colors` is a list of CIE `(x, y)` stops, at most `gradient.points_capable` of them. |
| `await light.set_powerup(preset)` | `{"powerup": {"preset": ...}}` | One of `"safety"`, `"powerfail"`, `"last_on_state"`, `"custom"`. |
| `await light.alert()` | `{"alert": {"action": "breathe"}}` | One pulse to identify a light; it restores itself. |

```python
strip = await hue.lights["Hallway strip"]
await strip.set_effect(models.Effect.CANDLE)
await strip.set_gradient([(0.6, 0.35), (0.2, 0.15)], mode="interpolated_palette")
await strip.set_powerup("last_on_state")
await strip.alert()
```

A light that does not support what was asked for is refused by the bridge, in
the body of an otherwise successful response — that surfaces as
`HueResponseError`. Read `light.effects.effect_values` and
`light.gradient.points_capable` first if you want to check beforehand.

The state behind these commands is reported in these fields, all optional and
all `None` on a plain white bulb:

| Field | Type | Reports |
| --- | --- | --- |
| `effects` | `models.Effects` | The running effect and the ones this light can run. |
| `timed_effects` | `models.TimedEffects` | Same, plus `duration` — the milliseconds left. |
| `gradient` | `models.Gradient` | The colour stops, `points_capable` and `pixel_count`. |
| `powerup` | `models.Powerup` | What the light does when mains power returns. |
| `alert_actions` | `models.Alert` | The alert actions the light accepts. Named for the `alert()` command that would otherwise collide; the wire key is `alert`. |
| `signaling` | `models.Signaling` | The signal being displayed, and the ones available. |

## Scenes

```python
scene = await hue.scenes["Movie night"]
await scene.activate()
```

`activate()` is `update({"recall": {"action": "active"}})` — one PUT to the
scene, which applies it to the room or zone in its `group` field. Scene names
repeat across rooms far more often than room names do, so when several match,
the first in bridge order wins.

The model also exposes stored `actions` (`models.SceneAction`) and optional
bridge `status` (`models.SceneStatus`), including `active` and the aware
`last_recall` timestamp when reported.

## Models

All models allow unknown fields, so a firmware update that adds a key cannot
break parsing. Anything the bridge sent but the model does not declare stays
available in `model_extra`.

### Resources

| Model | Fields beyond `id`, `type`, `id_v1`, `owner` | Commands beyond `update` / `delete` / `refresh` |
| --- | --- | --- |
| `models.Light` | `metadata`, `on`, `dimming`, `color`, `color_temperature`, `mode`, `effects`, `timed_effects`, `gradient`, `powerup`, `alert_actions`, `signaling` | light commands, `set_effect`, `set_gradient`, `set_powerup`, `alert` |
| `models.GroupedLight` | `on`, `dimming`, `color` (`GroupedColor`, whose `xy` may be absent), `color_temperature` | light commands |
| `models.Room` | `metadata`, `children`, `services` | light commands, `service_id`, `contains_device` |
| `models.Zone` | `metadata`, `children`, `services` | light commands, `service_id`, `contains_device` |
| `models.Scene` | `metadata`, `group`, `speed`, `auto_dynamic`, `actions`, `status` | `activate` |
| `models.Device` | `metadata`, `product_data`, `services` | `service_id` |
| `models.Bridge` | `bridge_id`, `time_zone` | — |
| `models.BridgeHome` | `children`, `services` | — |
| `models.ServiceGroup` | `metadata`, `children`, `services` | — |
| `models.DevicePower` | `power_state` | — |
| `models.Motion` | `enabled`, `motion`, `sensitivity` | — |
| `models.GroupedMotion` | as `Motion` | — |
| `models.Temperature` | `enabled`, `temperature` | — |
| `models.LightLevel` | `enabled`, `light` | — |
| `models.GroupedLightLevel` | `enabled`, `light` | — |
| `models.Button` | `metadata`, `button` | — |
| `models.Contact` | `enabled`, `contact_report` | — |
| `models.RelativeRotary` | `relative_rotary` | — |
| `models.ZigbeeConnectivity` | `status`, `mac_address`, `channel`, `extended_pan_id` | — |

"Light commands" is `set`, `turn_on`, `turn_off`, `set_brightness`,
`set_color`, `set_rgb`, `set_color_temperature`, `set_kelvin`.

### Convenience properties

| Model | Property | Type |
| --- | --- | --- |
| `HueResource` | `device_id` | `str \| None` (the owner's rid) |
| `HueResource` | `is_bound` | `bool` |
| `NamedResource` | `name` | `str` |
| `Light`, `GroupedLight` | `is_on` | `bool` |
| `Light` | `brightness` | `float \| None` |
| `Light` | `mirek` | `int \| None` |
| `Light` | `kelvin` | `int \| None`; only when the reported mirek value is valid |
| `Light` | `rgb` | `tuple[int, int, int] \| None`; only when xy colour and brightness are available |
| `Light` | `effect` | `str \| None` |
| `Light` | `is_gradient` | `bool` |
| `Motion` | `motion_detected` | `bool` |
| `Motion` | `last_motion` | `str` (ISO timestamp, or `""`) |
| `Temperature` | `celsius` | `float \| None` |
| `DevicePower` | `battery_level` | `int \| None` |
| `Button` | `last_event` | `str \| None` |
| `Contact` | `is_contact` | `bool` |
| `LightLevel` | `level` | `int \| None` |
| `LightLevel` | `lux` | `float \| None`; only when the reported reading is valid |
| `RelativeRotaryReading` | `value` | `RelativeRotaryReport \| RelativeRotaryEvent \| None`; prefers the timestamped report |
| `ZigbeeConnectivity` | `is_connected` | `bool` |

### Payload models

Exported from `huepy.models` alongside the resources, for typing and for
building payloads by hand.

| Area | Names |
| --- | --- |
| Shared | `HueModel`, `HueResource`, `NamedResource`, `Metadata`, `ResourceIdentifier`, `ResourceType` |
| Light state | `On`, `Dimming`, `Color`, `GroupedColor`, `ColorXY`, `ColorGamut`, `ColorTemperature`, `MirekSchema` |
| Light services | `Effect`, `Effects`, `TimedEffects`, `Gradient`, `GradientPoint`, `Powerup`, `Alert`, `Signaling`, `LightCommands` |
| Sensors and input | `MotionReading`, `MotionReport`, `Sensitivity`, `TemperatureReading`, `TemperatureReport`, `ButtonReading`, `ButtonReport`, `ContactReport`, `LightLevelReading`, `LightLevelReport`, `RelativeRotaryReading`, `RelativeRotaryReport`, `RelativeRotaryEvent`, `RelativeRotaryRotation` |
| Devices | `ProductData`, `PowerState`, `TimeZone` |
| Connectivity | `ZigbeeConnectivity`, `ZigbeeChannel` |
| Groups | `ResourceGroup`, `SceneAction`, `SceneStatus` |
| Events | `HueEvent`, `EventResource`, `EventType`, `parse_events` |
| Envelope | `HueResponse`, `HueErrorDetail`, `unwrap`, `unwrap_one` |
| Payload builder | `build_light_payload` |
| Aggregate resources | `AnyResource`, `RESOURCE_MODELS`, `RESOURCE_LIST`, `parse_resource` |

`ResourceType` is a `StrEnum` of every resource type with a concrete huepy
model or handler. Unknown aggregate types remain usable through the generic
fallback and keep their raw string `type`.

`AnyResource` is the aggregate-resource union used by `Hue.snapshot()` and the
state layer. `parse_resource(payload)` returns the concrete model for a known
`type`, or a generic `HueResource` for a future type. `RESOURCE_MODELS` maps
known type strings to models and `RESOURCE_LIST` validates a list of aggregate
resources.

`Light.capture()` returns detached `models.LightState` with valid power,
brightness and active colour-mode state. `await light.restore(state,
transition=None)` restores it only to the light that captured it; another
light raises `ValueError`. `kelvin`, `rgb` and `lux` are computed fields, so
they are included in normal `model_dump()` output.

### Envelope

Every v2 response is `{"errors": [...], "data": [...]}`, and the bridge reports
many failures *inside* otherwise successful 2xx bodies. Routing responses
through `unwrap` is what turns that into an exception rather than a silent
success.

```python
from huepy.models import unwrap, unwrap_one

lights = unwrap(payload, models.Light)  # HueResponseError if the body has errors[]
light = unwrap_one(payload, models.Light)  # and also if data[] came back empty
```

`HueResponse`, `HueErrorDetail`, `unwrap` and `unwrap_one` are exported from
`huepy.models`; their implementation lives with the shared models in
`models/common.py`.

`build_light_payload(**state)` composes the same payload `set()` sends, without
a client — useful for `update()` calls you assemble yourself.

## Events

`hue.get_event_stream()` yields `models.HueEvent`, not dicts.

```python
async for event in hue.get_event_stream():
    if event.is_update:
        print(event.resource_ids)
```

| Member | Type | Description |
| --- | --- | --- |
| `event.id` | `str` | The event's own id. |
| `event.type` | `str` | Raw type, kept as a string so an unknown one cannot kill the stream. |
| `event.creationtime` | `datetime \| None` | Aware timestamp from the bridge. |
| `event.sse_id` | `str \| None` | SSE frame id used for ordering; excluded from serialization. |
| `event.data` | `list[models.EventResource]` | The changed resources. |
| `event.event_type` | `models.EventType \| None` | The type as an enum, or `None` if unrecognised. |
| `event.resource_ids` | `list[str]` | Ids of every resource in the event. |
| `event.is_update` | `bool` | Whether this reports changed state. |
| `event.is_delete` | `bool` | Whether this reports resources that no longer exist. |

An `EventResource` carries `id`, `type`, `id_v1`, `owner`, and whichever typed
sections changed: light state (`on`, `dimming`, `color`, `color_temperature`),
sensor state (`motion`, `temperature`, `light`, `contact_report`,
`power_state`), or input state (`button`, `relative_rotary`). Future sections
remain on `model_extra`. Grouped and ungrouped sensor reports share the same
reading models. Events are payloads, not resources: they are *not* bound and
cannot issue commands. Fetch the resource by id to act on it.

A payload that will not parse is logged at warning level and skipped — a
stream meant to run for weeks must not die on one malformed event. The stream
reconnects with exponential backoff, and is closed for you by `hue.close()`.

For the raw decoded payloads instead, use the transport directly:

```python
async for payload in hue.http.subscribe_events():
    print(payload)
```

`models.parse_events(payload)` turns such a payload into `list[HueEvent]`.

`hue.http.subscribe_event_frames()` yields complete `SSEFrame` objects. Each
has `event_id`, an aware `received_at`, and decoded `events`; multi-line SSE
data stays one frame. `hue.http.event_connections()` additionally yields an
`EventConnection` with `opened_at`, `resumed_from`, and its `frames` iterator.
Both resume reconnects with the last event id. The compatibility
`subscribe_events()` iterator continues to yield individual decoded event dictionaries.

## Last-reported state

`hue.state()` creates a `huepy.state.HueState`; it does nothing until entered.
The context establishes the stream, buffers it during a one-request aggregate
snapshot, folds the buffered frames without publishing startup history, and
then returns a last-reported view. The bridge gives the snapshot no cursor, so
an event already represented by the snapshot can briefly regress one field;
the next event or reconnect reconciliation repairs it.

```python
from huepy import models

async with hue.state() as state:
    desk = state.lights["Desk lamp"]
    print(state.connected, desk.kelvin)
    all_lights = state.all(models.Light)
```

`state.lights`, `rooms`, `zones`, `scenes` and `devices` are synchronous local
views with `get`, `all`, `by_name`, `names` and `[...]`. `state.resources`,
`get(id)`, `all(Model)`, `lights_in(group)`, `room_of(id)`, `zones_of(id)`,
`device_of(id)` and `name_of(id)` provide generic and topology lookup. Every
read returns a fresh bound model; changing it cannot alter the stored graph.

`state.changes(maxsize=4096)` is an async iterator of frozen
`huepy.state.Change` and `Resync` records. A `Change` includes full before and
after resources, raw `delta`, source timestamps, event id, and write-correlation
fields (`origin`, `command_id`, `command_confirmed`, `observation`, and
`transition_ends_at`). A `Resync` has `RECONNECT`, `LAGGED`, or `INCONSISTENT`
reason and marks history whose continuity cannot be guaranteed. Subscribers are
independent and bounded; overflow is coalesced into `Resync(LAGGED)`.

State is never optimistic. Commands observed through its transport may be
attributed to this client only after their transport outcome is known.
`state.fading` is a read-only mapping of current locally issued
`huepy.state.ActiveFade` records, keyed by resource id; each record includes the
command id, normalized target, end, report-reliability end, and confirmation.
See [`STATE_LAYER.md`](STATE_LAYER.md) for the reconnect, reconciliation,
folding, and write-correlation rationale and the bridge observations behind it.

### State lifecycle and views

| Member | Type | Description |
| --- | --- | --- |
| `state.connected` | `bool` | Whether the event stream is active and startup or reconnect reconciliation is complete. |
| `state.resources` | `list[AnyResource]` | Fresh bound copies of every aggregate-visible resource. |
| `state.fading` | `Mapping[str, ActiveFade]` | Fresh, read-only records for locally issued fades still inside their reporting window. |
| `state.get(resource_id)` | `AnyResource \| None` | Local id lookup. |
| `state.all(Model)` | `list[Model]` | Local lookup by concrete model class. |
| `state.name_of(resource_id)` | `str` | Resource or owner name, or `"Unknown"`. |
| `state.device_of(resource_id)` | `Device \| None` | Owning physical device. |
| `state.room_of(resource_id)` | `Room \| None` | Containing room. |
| `state.zones_of(resource_id)` | `list[Zone]` | Every containing zone. |
| `state.lights_in(group)` | `list[Light]` | Resolvable lights in a room or zone. |
| `state.changes(maxsize=4096)` | `AsyncGenerator[Change \| Resync]` | Independent bounded history stream. Requires the state context to be running. |
| `state.close()` | `Coroutine[Any, Any, None]` | Stop observation and close every subscriber when awaited. Called by context exit. |

Each `StateView` (`lights`, `rooms`, `zones`, `scenes`, and `devices`) has
`get(id)`, `all()`, `by_name(name)`, `names()`, and `[...]`. These operations
are synchronous and issue no bridge request. A missing `by_name` or subscript
lookup raises `ResourceNotFoundError`.

### State records

The state package exports `HueState`, `StateView`, `Change`, `ChangeKind`,
`Resync`, `ResyncReason`, `ActiveFade`, and the transport-level `PendingWrite`.

| Record | Important fields |
| --- | --- |
| `Change` | `kind`, `at`, `observed_at`, `event_at`, `received_at`, `event_id`, `resource_id`, `resource_type`, `before`, `after`, `delta`, `resynced`, `origin`, `command_id`, `command_confirmed`, `observation`, `transition_ends_at` |
| `Resync` | `reason`, `gap_started`, `gap_ended`, `dropped`, `detail` |
| `ActiveFade` | `command_id`, `resource_id`, `target`, `sent_at`, `ends_at`, `unreliable_until`, `confirmed` |
| `PendingWrite` | `command_id`, `path`, `payload`, `sent_at`, `completed_at`, `status` |

`ChangeKind` contains `UPDATE`, `ADD`, and `DELETE`. `ResyncReason` contains
`RECONNECT`, `LAGGED`, and `INCONSISTENT`. `Change.at` chooses the best
available feature timestamp in the order `observed_at`, `event_at`, then
`received_at`.

## `huepy.color`

Pure conversion helpers: no I/O, no async, no dependency on the rest of huepy,
so a colour can be prepared long before a bridge is reached.

```python
from huepy import color

xy = color.rgb_to_xy(color.hex_to_rgb("#ff8800"))
xy = color.clamp_to_gamut(xy, color.GAMUT_C)
warm = color.kelvin_to_mirek(2700)
```

| Function | Returns | Notes |
| --- | --- | --- |
| `hex_to_rgb(value)` | `tuple[int, int, int]` | Accepts `"#rrggbb"`, `"rrggbb"` and `"#rgb"`. `ValueError` otherwise. |
| `rgb_to_hex(rgb)` | `str` | Always lowercase `#rrggbb`. |
| `rgb_to_xy(rgb)` | `tuple[float, float]` | Chromaticity only; luminance is discarded. |
| `xy_to_rgb(xy, brightness=100.0)` | `tuple[int, int, int]` | The inverse, with the luminance supplied. Overflow scales all three channels, preserving hue. |
| `kelvin_to_mirek(kelvin)` | `int` | Clamped to 153–500. `ValueError` if not positive. |
| `mirek_to_kelvin(mirek)` | `int` | Between 2000 and 6536; the exact inverse. |
| `clamp_to_gamut(xy, gamut)` | `tuple[float, float]` | Nearest point on the triangle, or `xy` unchanged if already inside. |
| `gamut_for(gamut_type)` | `Gamut \| None` | Looks up `"A"`, `"B"` or `"C"`; `None` for `"other"` or unknown, meaning "do not clamp". |

| Constant | Value |
| --- | --- |
| `Gamut` | `NamedTuple` of three `(x, y)` primaries: `red`, `green`, `blue`. |
| `GAMUT_A` | Legacy LivingColors, first-generation Iris and Bloom. |
| `GAMUT_B` | Older colour bulbs; weak in the greens. |
| `GAMUT_C` | Current colour bulbs and strips; the widest. |
| `MIREK_MIN` | `153` — the coolest the bridge accepts, about 6536 K. |
| `MIREK_MAX` | `500` — the warmest, 2000 K. |

## Resource handlers

The lower-level surface: every method takes an id, and results are still bound
models. Reach for it when you already hold an id, when a resource type has no
model commands, or for the handler-only helpers below.

| Method | Returns |
| --- | --- |
| `async get_all()` | `list[Model]` |
| `async all()` | `list[Model]` |
| `async get(resource_id)` | `Model` |
| `async update(resource_id, data)` | `list[ResourceIdentifier]` |
| `async delete(resource_id)` | `list[ResourceIdentifier]` |

Handlers in the [name-lookup table](#lookup-by-name) add `by_name(name)`,
`names()` and the `[...]` subscript.

| Attribute | Class | Model | Extra methods |
| --- | --- | --- | --- |
| `hue.light` | `Light` | `models.Light` | `turn_on`, `turn_off`, `set_brightness`, `set_color`, `set_color_temperature`, `get_lights_on`, `get_service_ids_on`, `get_device_ids_on` |
| `hue.light_group` | `GroupedLight` | `models.GroupedLight` | `turn_on`, `turn_off`, `set_brightness`, `set_color`, `set_color_temperature` |
| `hue.room` | `Room` | `models.Room` | `create`, `grouped_light_id`, `get_from_light_service_id`, and the five light commands |
| `hue.zone` | `Zone` | `models.Zone` | `create`, `grouped_light_id`, and the five light commands |
| `hue.scene` | `Scene` | `models.Scene` | `create`, `activate` |
| `hue.service_group` | `ServiceGroup` | `models.ServiceGroup` | `create` |
| `hue.motion` | `Motion` | `models.Motion` | `turn_on`, `turn_off`, `set_sensitivity`, `get_motion_state`, `get_last_motion` |
| `hue.motion_group` | `GroupedMotion` | `models.GroupedMotion` | `turn_on`, `turn_off` |
| `hue.temperature` | `Temperature` | `models.Temperature` | `turn_on`, `turn_off` |
| `hue.contact` | `Contact` | `models.Contact` | `turn_on`, `turn_off` |
| `hue.button` | `Button` | `models.Button` | — |
| `hue.relative_rotary` | `RelativeRotary` | `models.RelativeRotary` | — |
| `hue.zigbee_connectivity` | `ZigbeeConnectivity` | `models.ZigbeeConnectivity` | — |
| `hue.device` | `Device` | `models.Device` | — |
| `hue.device_power` | `DevicePower` | `models.DevicePower` | — |
| `hue.light_level` | `LightLevel` | `models.LightLevel` | — |
| `hue.light_level_group` | `GroupedLightLevel` | `models.GroupedLightLevel` | — |
| `hue.bridge` | `Bridge` | `models.Bridge` | — |
| `hue.bridge_home` | `BridgeHome` | `models.BridgeHome` | — |

### Light commands

```
async turn_on(resource_id) -> list[ResourceIdentifier]
async turn_off(resource_id) -> list[ResourceIdentifier]
async set_brightness(resource_id, brightness) -> list[ResourceIdentifier]
async set_color(resource_id, x, y) -> list[ResourceIdentifier]
async set_color_temperature(resource_id, mirek) -> list[ResourceIdentifier]
```

```python
lights = await hue.light.get_all()
await hue.light.set_brightness(lights[0].id, 40.0)
```

These take neither `transition` nor the human-unit colour arguments, and each
one is its own request — that is what the model commands add. On a room or
zone they resolve the group's `grouped_light` service first, so every call
costs an extra GET:

```python
kitchen = await hue.rooms["Kitchen"]
await hue.room.set_brightness(kitchen.id, 40.0)
service_id = await hue.room.grouped_light_id(kitchen.id)
```

`grouped_light_id` raises `ValueError` if the group has no such service.

### `Light`

```
async get_lights_on() -> list[models.Light]      # lights currently on
async get_service_ids_on() -> list[str]          # their service ids
async get_device_ids_on() -> list[str]           # their owning device ids
```

### `Room`, `Zone`, `Scene`, `ServiceGroup`

```
async Room.create(name, devices: list[str]) -> list[ResourceIdentifier]
async Room.get_from_light_service_id(light_id) -> str | None
async Zone.create(name, services: list[dict[str, str]]) -> list[ResourceIdentifier]
async Scene.create(name, room_id) -> list[ResourceIdentifier]
async Scene.activate(scene_id) -> list[ResourceIdentifier]
async ServiceGroup.create(name, services, archetype="sensor_group") -> list[ResourceIdentifier]
```

`Zone.create` and `ServiceGroup.create` take service references, each a dict of
`rid` and `rtype`; `Room.create` takes plain device ids.

### `Motion`

```
async set_sensitivity(resource_id, sensitivity: int) -> list[ResourceIdentifier]
async get_motion_state(resource_id) -> bool
async get_last_motion(resource_id) -> str   # ISO timestamp, or ""
```

`set_sensitivity` raises `TypeError` for a non-integer and `ValueError` if the
value is negative or above the sensor's reported maximum.

## Configuration

```
@dataclass
class HueConfig:
    bridge_ip: str = ""
    app_key: str | None = None
    config_path: Path = default_config_path()
    verify_ssl: bool = False
```

`bridge_ip` and `app_key` resolve as argument, then environment
(`HUE_BRIDGE_IP`, `HUE_APP_KEY`), then the config file. A missing address is a
`ValueError`; there is no built-in default.

`config_path` resolves as argument, then `HUE_CONFIG_PATH`, then
`$XDG_CONFIG_HOME/huepy/config.json` (`~/.config/huepy/config.json` by
default). The default is deliberately absolute: a relative one would tie the
stored credential to whichever directory a program was started from.

`save()` persists both to the config file with mode `0600`. Passing
`save(app_key)` also replaces the stored key; calling `save()` bare records the
address on its own and keeps any existing key. Because the address is stored,
a configured machine needs neither arguments nor environment — and no address
has to be committed to source.

If the filesystem ignores `chmod` and the mode does not take, `save()` issues
an `InsecureConfigWarning` (exported from `huepy`) instead of silently leaving
the credential world-readable.

`HueHttpClient` is the concrete transport, also exported from `huepy`; the
client builds one for you at `start()` and exposes it as `hue.http`. Its pool
allows three connections per bridge. A GET that receives 429 or 503 is retried
at most three times with bounded exponential backoff; PUT, POST and DELETE are
never replayed automatically.

## Exceptions

All derive from `HueError`.

| Exception | Attributes | Raised when |
| --- | --- | --- |
| `AuthenticationError` | — | No application key, or the bridge refused one |
| `BridgeConnectionError` | — | The bridge is unreachable |
| `HueAPIError` | `status_code`, `message` | Non-2xx HTTP status |
| `HueResponseError` | `errors: list[str]` | A 2xx body containing a blocking error, or only advisory errors with no successful data |
| `DetachedResourceError` | — | A command was issued on a model with no client |
| `ResourceNotFoundError` | `name`, `known` | A name lookup matched nothing |

`HueResponseError` matters: the v2 API reports many failures this way, so a
write can "succeed" at the HTTP level and still have been rejected.

### Partial failures

The bridge overloads `errors[]` for two different things, and only the
`error_code` on each entry separates them:

| `error_code` | Meaning | huepy |
| --- | --- | --- |
| `communication_error` | The command was accepted, but a device's radio is flaky, so it "may not have effect" | Logged as a warning; the call returns normally |
| anything else | The request itself was wrong, e.g. setting colour temperature on a light that has none | Raises `HueResponseError` |

On the measured bridge these partial outcomes arrived as HTTP **207
Multi-Status**, often with a resource still listed in `data`; neither the HTTP
status nor `data` alone distinguishes them. Raising on both would mean one
unreliable bulb breaks every call that touches it; raising on neither would
silently drop an unsupported attribute. An advisory error that changed nothing
at all still raises, since there is no success to preserve.

Transport-level rejections do not reach this path. For example, a nonexistent
resource is `404` and a negative transition is `400`; both surface as
`HueAPIError` before envelope parsing.

## Complete example

```python
import asyncio

from huepy import Hue, HueError, ResourceNotFoundError


async def main() -> None:
    try:
        async with Hue(bridge_ip="192.168.1.100") as hue:
            for light in await hue.lights.all():
                if light.is_on:
                    print(f"{light.name}: {light.brightness}% {light.mirek} mirek")

            kitchen = await hue.rooms["Kitchen"]
            await kitchen.set(brightness=40, kelvin=2200, transition=2.0)

            desk = await hue.lights["Desk lamp"]
            await desk.set_rgb((255, 136, 0), transition=1.0)

            async for event in hue.get_event_stream():
                print(event.event_type, event.resource_ids)
                break
    except ResourceNotFoundError as exc:
        print(f"No such resource: {exc.name}. Known: {exc.known}")
    except HueError as exc:
        print(f"{type(exc).__name__}: {exc}")


asyncio.run(main())
```
