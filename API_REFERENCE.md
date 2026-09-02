# huepy API Reference

Async wrapper for the Philips Hue v2 CLIP API. Bridge I/O is asynchronous and
resource responses are validated pydantic models.

Anything fetched through a high-level collection is **bound**: it carries the
client that fetched it and can act on itself, so most code never handles a
bridge id.

```python
await hue.rooms.set("Kitchen", brightness=30, kelvin=2200, transition=2.0)
```

That resolves the unique room name and sends one PUT. The id-based API is under
`hue.api` and is documented in [Resource handlers](#resource-handlers); use it
when you already hold an id or need an unnamed resource type.

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
- [Smart scenes](#smart-scenes)
- [Models](#models)
- [Events](#events)
- [Last-reported state](#last-reported-state)
  - [Migrating from 0.4](#migrating-from-04)
- [Recording history](#recording-history)
- [huepy.color](#huepycolor)
- [Resource handlers](#resource-handlers)
- [Configuration](#configuration)
- [Exceptions](#exceptions)
  - [Partial failures](#partial-failures)
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
        tls: TlsMode | None = None,
        bridge_id: str | None = None,
        rate_limit: bool | None = None,
        state: bool = False,
        record: HistorySink | Sequence[HistorySink] | None = None,
    )
```

Settings are resolved eagerly, at construction, from three sources in this
order:

| Setting | Argument | Environment | Config file | If nothing is found |
| --- | --- | --- | --- | --- |
| Bridge address | `bridge_ip=` | `HUE_BRIDGE_IP` | `bridge_ip` | `ValueError` from the constructor |
| Application key | `app_key=` | `HUE_APP_KEY` | `app_key` | Stays `None`; `ensure_authenticated()` then raises |
| Config file path | `config_path=` | `HUE_CONFIG_PATH` | — | `$XDG_CONFIG_HOME/huepy/config.json` |
| TLS verification | `tls=` | `HUE_TLS` | — | `TlsMode.VERIFIED` — verify against Signify's bundled roots |
| Bridge id (cert pinning) | `bridge_id=` | `HUE_BRIDGE_ID` | `bridge_id` | `None` — verified but the identity is not pinned |
| Write pacing | `rate_limit=` | `HUE_RATE_LIMIT` | — | `True` — space writes to the bridge's budget |
| State tracking | `state=` | — | — | `False` — a plain client opens no event stream |
| History recording | `record=` | — | — | `None` — implies `state=True` when given |

TLS is verified by default. A genuine bridge presents a certificate signed by
one of Signify's two private root CAs (bundled in `huepy.client.tls`), with the
bridge id as its common name. `TlsMode.VERIFIED` checks the chain against those
roots; when a `bridge_id` is known it also pins the common name to it. Without a
`bridge_id` verification degrades to certificate-only and emits an
`UnverifiedBridgeIdentityWarning` — the peer is still proven to be a real Hue
bridge, but *which* bridge is not asserted. `TlsMode.INSECURE` skips verification
entirely and emits an `InsecureTlsWarning`; use it only for development against a
proxy or emulator, never production.

Writes are paced to the bridge's documented throughput budget: roughly ten per
second to a light and one per second to the shared broadcast budget that
`grouped_light` writes and scene recalls draw from. This spaces the start of
each write so a burst — a `room.restore()` fanning out over many lights — cannot
flood the bridge. Pass `rate_limit=False` to manage pacing yourself.

Use the client as an async context manager. Normal startup opens the HTTP
session and does no resource GETs:

```python
async with Hue(bridge_ip="192.168.1.100") as hue:
    for light in await hue.lights.list():
        print(light.name, light.is_on)
```

#### Methods

| Method | Description |
| --- | --- |
| `async start() -> None` | Open the session without eagerly fetching resources. Called by `__aenter__`. |
| `async close() -> None` | Close every event stream and the session. Safe to call when not started. |
| `async refresh_names() -> dict[str, str]` | Reload the id-to-name lookup from one aggregate snapshot request. |
| `get_name(resource_id: str) -> str` | Display name for an id, or `"Unknown"`. Local; no request. |
| `ensure_authenticated() -> None` | Raise `AuthenticationError` if no key is available. Never prompts. |
| `async authenticate(app_name="huepy", timeout=60) -> str` | Obtain a key. The bridge link button must be pressed while this runs. |
| `async get_event_stream() -> AsyncGenerator[models.HueEvent]` | Yield typed events pushed by the bridge. See [Events](#events). |
| `async snapshot() -> list[models.AnyResource]` | Fetch all aggregate-visible resources in one request. Known types use their concrete model; future types use `models.HueResource`. |

#### Attributes

| Attribute | Type | Description |
| --- | --- | --- |
| `config` | `HueConfig` | The resolved settings. |
| `http` | `Transport` | The open transport. Raises `RuntimeError` before `start()`. |
| `names` | `dict[str, str]` | The local id-to-name map. It is populated by `refresh_names()` or by state tracking. |
| `state` | `huepy.state.HueState` | The local resource graph. Present from construction; observing when `Hue(state=True)`. See [Last-reported state](#last-reported-state). |
| `recorder` | `huepy.recording.Recorder \| None` | The running history recorder, when `record=` was given. See [Recording history](#recording-history). |
| `api` | `HueAPI` | Typed, strictly id-addressed CLIP v2 handlers. |
| `lights`, `rooms`, `zones`, `scenes`, `smart_scenes`, `devices`, `service_groups` | named collections | Human-facing collections addressed by display name. |

The package itself exposes `huepy.__version__`, read from the installed
distribution metadata, or `"unknown"` when running from a source tree.

### Discovering a bridge

Find a bridge before any address, key or bridge id is configured. Manual
configuration (`bridge_ip=` to `Hue`) remains the fallback, and the
deprecated UPnP/SSDP method is deliberately not implemented.

```python
from huepy import Hue, discover

bridges = await discover()
if bridges:
    hue = Hue(bridge_ip=bridges[0].ip, bridge_id=bridges[0].bridge_id)
```

The classmethod `Hue.from_discovery(*, app_key=None, config_path=None,
method="auto", index=None, state=False)` wraps that into one awaitable call,
carrying the discovered bridge id through so TLS pinning works without a second
lookup, and returns an unstarted client. It raises `BridgeConnectionError` when
no bridge is found, or when several are found and `index=` was not given.

| Function | Returns | Notes |
| --- | --- | --- |
| `await discover(*, method="auto", validate=True, timeout=5.0, session=None)` | `list[DiscoveredBridge]` | `method` is `"mdns"`, `"cloud"`, or `"auto"` -- mDNS first, falling back to the rate-limited cloud endpoint when it finds nothing. `validate` confirms each candidate against its unauthenticated `/api/0/config`, filling in the bridge id and versions; candidates that do not answer are dropped. |
| `await discover_bridge_id(ip, *, timeout=5.0, session=None)` | `str` | Reads one bridge's id from its unauthenticated config -- the value that pins its TLS certificate. Raises `BridgeConnectionError` if the bridge does not answer. |

`DiscoveredBridge` is a frozen dataclass: `bridge_id` and `ip`, plus
`model_id`, `sw_version` and `api_version` once validated. mDNS needs
multicast reachability but is not rate limited; the cloud endpoint answers
from anywhere but allows one request per 15 minutes per client, so discover
once and store the result with `HueConfig.save()`.

## Fetching resources

The top-level plural collections are the canonical human-facing API. They use
one vocabulary: `list()` returns every current resource, `get(name)` returns
one uniquely named bound resource, `names()` returns display names,
`rename(name, new_name)` renames one, and `delete(name)` removes one.

| Call | Returns | Stateless cost | Tracked cost |
| --- | --- | --- | --- |
| `await hue.lights.list()` | `list[models.Light]` | one GET | local |
| `await hue.lights.get("Desk lamp")` | `models.Light` | one collection GET | local |
| `await hue.rooms.names()` | `list[str]` | one GET | local |
| `await hue.rooms.rename("Kitchen", "Galley")` | `CommandResult` | collection GET + PUT | PUT |
| `await hue.rooms.delete("Kitchen")` | `CommandResult` | collection GET + DELETE | DELETE |

```python
lights = await hue.lights.list()
on_now = [light.name for light in lights if light.is_on]
```

## Lookup by name

The human-facing collections are `hue.lights`, `hue.rooms`, `hue.zones`,
`hue.scenes`, `hue.smart_scenes`, `hue.devices`, and `hue.service_groups`.
Their names are matched case-insensitively after surrounding whitespace is
removed.

```python
kitchen = await hue.rooms.get("Kitchen")
desk = await hue.lights.get("desk lamp")
available = await hue.rooms.names()
```

A miss raises `ResourceNotFoundError`, which carries the requested `name` and
the available `known` names. A duplicate raises `AmbiguousResourceError`, with
the requested `name` and every matching `resource_ids`; commands are never
sent for an ambiguous name.

```python
from huepy import AmbiguousResourceError, ResourceNotFoundError

try:
    kitchen = await hue.rooms.get("Kitchn")
except ResourceNotFoundError as exc:
    print(exc.name, exc.known)
except AmbiguousResourceError as exc:
    print(exc.name, exc.resource_ids)
```

Stateless collections fetch and match a collection for each lookup because CLIP
has no server-side name filter. Use `list()` for many local matches. With
`Hue(state=True)`, startup requires an application key, takes one aggregate
snapshot, and keeps a local graph current from the event stream. Subsequent
collection reads use that graph. During a disconnect or reconciliation they
raise `BridgeConnectionError` rather than resolving a command against stale
names: a name resolved here is the target of a rename, a delete, or a command,
so a graph known to be stale must not answer. Reading `hue.state` directly is
last-reported state and stays readable while disconnected.

Resource creation stays under `hue.api`: room, zone, scene, smart-scene, and
service-group creation inherently requires ids or typed CLIP reference shapes,
so exposing it on the name-oriented layer would mix abstraction levels.

## Bound and detached models

A model parsed by a handler is bound to the client that fetched it, and can
issue its own requests. A model you construct yourself is detached: it is
plain data, with no bridge to talk to.

```python
light = await hue.lights.get("Desk lamp")
print(light.is_bound)  # True
await light.turn_on()

detached = models.Light(id="abc")
await detached.turn_on()  # raises DetachedResourceError
```

`refresh()` returns a new bound model. Bound and high-level write commands
return `CommandResult`: `sent` is false only for a no-op `set()`, and
`resources` contains bridge `ResourceIdentifier` references. `bind(hue,
rtype="")` exists for the rare case of attaching a client to a model you built
or cached yourself; handlers call it for you.

## Commands on every resource

Every addressable resource — light, room, scene, sensor, device — carries
these three.

| Command | Sends | Notes |
| --- | --- | --- |
| `await resource.update(data)` | `PUT` to the resource's own path | `data` is in the bridge's payload shape |
| `await resource.delete()` | `DELETE` | Returns a `CommandResult`; an empty body produces no affected references |
| `await resource.refresh()` | `GET` | Returns a **new** bound instance; the one you called it on is untouched |

```python
kitchen = await hue.rooms.get("Kitchen")
await kitchen.update({"metadata": {"name": "Kitchen (north)"}})
kitchen = await kitchen.refresh()
```

Writes return `CommandResult`; `refresh()` is how you read the new state.

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
    speed: float | None = None,
) -> CommandResult
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
| `speed` | `float` | Speed of the active dynamic palette, 0.0–1.0. Only takes effect while a dynamic scene is running. |

Everything supplied goes into **one** PUT:

```python
light = await hue.lights.get("Desk lamp")
await light.set(on=True, brightness=40, kelvin=2700, transition=1.5)
```

Rules `set()` enforces before it sends anything:

- `xy`, `rgb` and `hex_color` are three spellings of one colour, and `mirek`
  and `kelvin` two spellings of one colour temperature. Passing two of either
  raises `ValueError` rather than silently preferring one.
- A colour and a colour temperature cannot be combined — a light does one or
  the other. That, too, is a `ValueError`.
- A call that supplies nothing sends no request and returns
  `CommandResult(sent=False)`.

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

The named collections expose the same commands for one-shot work. They resolve
the unique name and delegate to the corresponding bound-resource command:

```python
await hue.lights.turn_on("Desk lamp", transition=1.0)
await hue.lights.set("Desk lamp", brightness=40, kelvin=2700)
await hue.rooms.set("Kitchen", on=True, brightness=40)
await hue.zones.turn_off("Downstairs", transition=3.0)
```

`lights` also provides `set_effect(name, effect)` and `alert(name)`. `scenes`
provides `activate(name)`. Every direct command returns `CommandResult`.

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
own default fade. The [effect, timed effect, gradient, powerup, signal,
identify and relative-adjustment](#lights-only) commands take no `transition`
argument -- `set_timed_effect()` and `signal()` take their own `duration` in
seconds instead -- and neither do the id-based handler commands.

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
kitchen = await hue.rooms.get("Kitchen")
await kitchen.set(rgb=(255, 136, 0), gamut=color.GAMUT_C)
```

The conversions themselves are public and pure — see [huepy.color](#huepycolor).

## Rooms and zones

A room does not accept light commands itself; it owns a `grouped_light`
service that does. A bound room already carries that reference in its
`services`, so it resolves the service from memory and sends **one** request:

```python
kitchen = await hue.rooms.get("Kitchen")
await kitchen.set(brightness=40, kelvin=2200, transition=2.0)
```

| Task | Bound room | Id-based handler |
| --- | --- | --- |
| Dim a room | 1 PUT | 1 PUT when the service id is known |
| Dim and warm a room | 1 PUT | 2 PUTs when the service id is known |
| Look the room up first | 1 GET, then as above | — |

The id-based layer does not hide a room GET inside a command. Resolve the
service explicitly and address `hue.api.grouped_lights`, or use a bound room
when the human-facing group is the natural selector.

Zones behave identically, and carry the same commands:

```python
downstairs = await hue.zones.get("Downstairs")
await downstairs.turn_off(transition=3)
```

Rooms and zones also carry:

| Member | Returns | Description |
| --- | --- | --- |
| `room.service_id(rtype)` | `str \| None` | The rid of a service of that type, e.g. `models.ResourceType.GROUPED_LIGHT`. |
| `room.contains_device(device_id)` | `bool` | Whether the device is a direct child. |
| `room.contains_light(light)` | `bool` | Whether this group's children resolve to that light. |
| `await room.lights()` | `list[models.Light]` | The group's own lights, bound. One GET. |
| `await room.capture()` | `models.GroupState` | Snapshot every light in the group. |
| `await room.restore(state, transition=...)` | `list[CommandResult]` | Put a snapshot back, one request per light. |
| `room.children` | `list[ResourceIdentifier]` | Devices in the room; light services in a zone. |
| `room.services` | `list[ResourceIdentifier]` | Services the group exposes. |

`service_id`, `contains_device` and `contains_light` read what the model
already carries; they issue no request. A light command on a group with no
`grouped_light` service raises `ValueError`.

A group's `children` are references rather than lights — devices for a room,
light services for a zone — so `lights()` lists lights once and joins them with
`contains_light`, which is the one rule both this and `hue.state.lights_in()`
use.

```python
kitchen = await hue.rooms.get("Kitchen")
before = await kitchen.capture()
await kitchen.set(brightness=30, kelvin=2200, transition=2.0)
await kitchen.restore(before, transition=2.0)
```

`capture()` returns a `models.GroupState` holding `group_id` and a tuple of
`models.LightState`. `restore()` refuses a snapshot from a different group,
raising `ValueError`, and skips a light that has since left the group rather
than resurrecting it.

Restoring deliberately sends one request per light instead of one to the
group: a `grouped_light` reports no aggregate colour temperature, so a group
restore silently drops it and leaves the room the wrong colour. The requests
are issued concurrently.

`update()` and `delete()` still address the group itself, not its light
service: `await kitchen.update({"metadata": {"name": "Kitchen"}})` renames the
room, while `await kitchen.set(on=True)` switches its lights on.

## Lights only

These live on `models.Light` and nowhere else. The bridge's `grouped_light`
service accepts none of them, so there is no room-wide or zone-wide form: to
run an effect across a room, iterate its lights.

| Command | Sends | Notes |
| --- | --- | --- |
| `await light.set_effect(effect, *, xy=None, rgb=None, hex_color=None, mirek=None, kelvin=None, speed=None)` | `{"effects_v2": {"action": {"effect": ..., "parameters": {...}}}}` | An `models.Effect` member or a raw name. `Effect.NO_EFFECT` stops the running one and takes no parameters. The tint and speed follow the same colour and 0.0–1.0 rules as `set()`. Sends the current `effects_v2` shape, not the deprecated `effects` key. |
| `await light.set_timed_effect(effect, *, duration=None)` | `{"timed_effects": {"effect": ..., "duration": ...}}` | A `models.TimedEffect` member or a raw name, e.g. a sunrise or sunset fade. `duration` is seconds, sent in milliseconds; required for a real effect, at most six hours. |
| `await light.set_gradient(colors, *, mode=None)` | `{"gradient": {...}}` | `colors` is a list of CIE `(x, y)` stops, at most `gradient.points_capable` of them. `mode` is a `models.GradientMode` member or a raw name -- `interpolated_palette`, `interpolated_palette_mirrored`, or `random_pixelated` -- left to the light when omitted. |
| `await light.set_powerup(preset="custom", *, on=None, on_mode=None, brightness=None, xy=None, rgb=None, hex_color=None, mirek=None, kelvin=None)` | `{"powerup": {"preset": ...}}` | `preset` is a `models.PowerupPreset` member or a raw name: `safety`, `powerfail`, `last_on_state`, `custom`. Passing any `on`/brightness/colour field configures a custom powerup and forces `preset="custom"`. `on_mode` is a `models.PowerupOnMode` member or a raw name -- `on`, `toggle`, or `previous`. |
| `await light.signal(signal, *, duration=None, colors=None)` | `{"signaling": {"signal": ..., "duration": ..., "colors": [{"xy": {...}}]}}` | A `models.Signal` member or a raw name. `duration` is seconds, sent in milliseconds. `colors` takes at most two CIE `(x, y)` points, clamped to the light's gamut, and only for `ON_OFF_COLOR` and `ALTERNATING`. |
| `await light.identify()` | `{"identify": {"action": "identify"}}` | A short breathe cycle to identify the light, distinct from `alert()`'s wire shape below. |
| `await light.adjust_brightness(delta)` | `{"dimming_delta": {"action": "up"\|"down"\|"stop", "brightness_delta": ...}}` | Nudges brightness by a relative percentage-point `delta` without reading the current value; `0` sends `"stop"`, halting an in-progress change. |
| `await light.adjust_color_temperature(mirek_delta)` | `{"color_temperature_delta": {"action": "up"\|"down"\|"stop", "mirek_delta": ...}}` | Nudges colour temperature by a relative `mirek_delta`; `0` sends `"stop"`. |
| `await light.alert()` | `{"alert": {"action": "breathe"}}` | One pulse to identify a light; it restores itself. |

```python
strip = await hue.lights.get("Hallway strip")
await strip.set_effect(models.Effect.CANDLE, mirek=400, speed=0.6)
await strip.set_timed_effect(models.TimedEffect.SUNSET, duration=1800)
await strip.set_gradient([(0.6, 0.35), (0.2, 0.15)], mode="interpolated_palette")
await strip.set_powerup("last_on_state")
await strip.signal(models.Signal.ON_OFF_COLOR, duration=10, colors=[(0.6, 0.35)])
await strip.identify()
await strip.adjust_brightness(-10)
await strip.adjust_color_temperature(50)
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
scene = await hue.scenes.get("Movie night")
await scene.activate()
await scene.activate(action=models.RecallAction.DYNAMIC_PALETTE, duration=4.0)
```

`activate(*, action=RecallAction.ACTIVE, duration=None, brightness=None)`
sends one PUT to the scene's `recall`, applying it to the room or zone in its
`group` field. The default call sends `{"recall": {"action": "active"}}`;
`duration` (seconds, sent in milliseconds) and `brightness` (0–100,
overriding the scene's own) are added only when given. `action` is a
`models.RecallAction`: `ACTIVE` applies the stored actions once,
`DYNAMIC_PALETTE` starts the scene cycling its palette, and `STATIC` applies
the palette's first frame without animating. Scene names repeat across rooms
far more often than room names do, so when several match, the first in bridge
order wins.

The named collection wraps the same call:

```python
await hue.scenes.activate("Movie night", duration=2.0)
```

`hue.api.scenes.create(name, room_id, *, actions=None, speed=None,
auto_dynamic=None)` stores a scene: `actions` are the per-target scene
actions in the bridge's shape, `speed` paces its dynamic palette from 0.0 to
1.0, and `auto_dynamic` starts it dynamically on recall.

The model also exposes stored `actions` (`models.SceneAction`) and optional
bridge `status` (`models.SceneStatus`), including `active` and the aware
`last_recall` timestamp when reported.

## Smart scenes

A smart scene recalls other scenes on a weekly schedule instead of once: it
is started and then follows its `week_timeslots` through the day until
stopped, which is why its verbs are `activate`/`deactivate` rather than a
recall action.

```python
await hue.smart_scenes.activate("Daily rhythm")
await hue.smart_scenes.deactivate("Daily rhythm")
```

`hue.smart_scenes` is the named counterpart to `hue.scenes`: `get`, `list`,
`names`, `rename`, `delete`, plus `activate(name)` and `deactivate(name)`.
Both resolve the unique name and delegate to the bound model, whose own
commands take no arguments:

```
async SmartScene.activate() -> CommandResult    # {"recall": {"action": "activate"}}
async SmartScene.deactivate() -> CommandResult  # {"recall": {"action": "deactivate"}}
```

Creation stays on the id-based handler:

```python
await hue.api.smart_scenes.create(
    "Daily rhythm",
    room_id,
    week_timeslots,
    transition_duration=30.0,
)
```

`create(name, group_id, week_timeslots, *, group_rtype=ResourceType.ROOM,
transition_duration=None)` takes the schedule in the bridge's own shape:
`week_timeslots` is a list of `models.SmartSceneWeekTimeslot`, each holding a
day's `timeslots` (`models.SmartSceneTimeslot`, pairing a `start_time` --
`models.SmartSceneStartTime`, a fixed clock `models.SmartSceneTime` or local
sunset -- with a scene `target`) and the `recurrence` weekdays it repeats on
(`models.WeekDay`). `transition_duration` fades between timeslots, in
seconds. `hue.api.smart_scenes.activate(scene_id)` and
`.deactivate(scene_id)` are the id-based equivalents of the bound commands
above.

The model reports its schedule back in `week_timeslots`, `transition_duration`,
which timeslot is currently running in `active_timeslot`
(`models.SmartSceneActiveTimeslot`), and `state`.

## Models

All models allow unknown fields, so a firmware update that adds a key cannot
break parsing. Anything the bridge sent but the model does not declare stays
available in `model_extra`.

### Resources

| Model | Fields beyond `id`, `type`, `id_v1`, `owner` | Commands beyond `update` / `delete` / `refresh` |
| --- | --- | --- |
| `models.Light` | `metadata`, `on`, `dimming`, `color`, `color_temperature`, `mode`, `effects`, `timed_effects`, `gradient`, `powerup`, `alert_actions`, `signaling` | light commands, `set_effect`, `set_timed_effect`, `set_gradient`, `set_powerup`, `signal`, `identify`, `adjust_brightness`, `adjust_color_temperature`, `alert` |
| `models.GroupedLight` | `on`, `dimming`, `color` (`GroupedColor`, whose `xy` may be absent), `color_temperature` | light commands |
| `models.Room` | `metadata`, `children`, `services` | light commands, `service_id`, `contains_device` |
| `models.Zone` | `metadata`, `children`, `services` | light commands, `service_id`, `contains_device` |
| `models.Scene` | `metadata`, `group`, `speed`, `auto_dynamic`, `actions`, `status` | `activate` |
| `models.SmartScene` | `metadata`, `group`, `week_timeslots`, `transition_duration`, `active_timeslot`, `state` | `activate`, `deactivate` |
| `models.Entertainment` | `renderer`, `renderer_reference`, `proxy`, `equalizer`, `max_streams` | — |
| `models.EntertainmentConfiguration` | `metadata`, `configuration_type`, `status`, `active_streamer`, `stream_proxy`, `channels`, `light_services` | `start`, `stop` |
| `models.Device` | `metadata`, `product_data`, `services` | `service_id`, `identify`, `usertest` |
| `models.Bridge` | `bridge_id`, `time_zone` | `set_timezone` |
| `models.BridgeHome` | `children`, `services` | — |
| `models.ServiceGroup` | `metadata`, `children`, `services` | — |
| `models.DevicePower` | `power_state` | — |
| `models.Motion` | `enabled`, `motion`, `sensitivity` | `enable`, `disable` |
| `models.GroupedMotion` | as `Motion` | `enable`, `disable` |
| `models.CameraMotion` | as `Motion` | `enable`, `disable` |
| `models.Temperature` | `enabled`, `temperature` | `enable`, `disable` |
| `models.LightLevel` | `enabled`, `light` | — |
| `models.GroupedLightLevel` | `enabled`, `light` | — |
| `models.Button` | `metadata`, `button` | — |
| `models.Contact` | `enabled`, `contact_report` | `enable`, `disable` |
| `models.RelativeRotary` | `relative_rotary` | — |
| `models.ZigbeeConnectivity` | `status`, `mac_address`, `channel`, `extended_pan_id` | — |
| `models.ZgpConnectivity` | `status`, `source_id` | — |
| `models.WifiConnectivity` | `status` | — |
| `models.ZigbeeDeviceDiscovery` | `status`, `action_values` | `search`, `search_with_default_link_key` |
| `models.DeviceSoftwareUpdate` | `state`, `auto_install`, `problems` | `install`, `set_auto_install` |
| `models.Geolocation` | `is_configured`, `sun_today` | `set_location` |
| `models.GeofenceClient` | `name`, `is_at_home` | — |
| `models.BehaviorScript` | `metadata`, `description`, `configuration_schema`, `trigger_schema`, `state_schema`, `version`, `supported_features`, `max_number_instances` | — |
| `models.BehaviorInstance` | `metadata`, `script_id`, `enabled`, `state`, `configuration`, `dependees`, `status`, `last_error` | `enable`, `disable`, `configure` |
| `models.Homekit` | `status`, `status_values` | `reset` |
| `models.Matter` | `max_fabrics`, `has_qr_code` | `reset` |
| `models.MatterFabric` | `status`, `creation_time`, `fabric_data` | — |
| `models.Tamper` | `tamper_reports` | — |

"Light commands" is `set`, `turn_on`, `turn_off`, `set_brightness`,
`set_color`, `set_rgb`, `set_color_temperature`, `set_kelvin`.

Every command in the table above is a bound-model method: `area.start()`,
`discovery.search()`, `software_update.install()`, `instance.enable()`,
`geolocation.set_location(...)` and `homekit.reset()` each resolve to one PUT
on that resource, exactly like `scene.activate()`. `create(...)` is the one
exception -- it never appears on a bound model, only on the id-based handler
or a named collection, because a resource cannot create itself.

`Device.identify(*, duration=None)` and `Device.usertest(*, enabled)`, and
`Bridge.set_timezone(time_zone)`, are bound-model commands only; there is no
handler-level equivalent. Fetch the model first --
`await hue.api.devices.get(device_id)` or `await hue.api.bridges.get(bridge_id)`
-- then call the command on it.

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
| `Light` | `hex_color` | `str \| None`; the same colour as `#rrggbb`, mirroring the `hex_color=` argument to `set()` |
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
| `ZigbeeConnectivity`, `ZgpConnectivity`, `WifiConnectivity` | `is_connected` | `bool` |
| `EntertainmentConfiguration` | `is_streaming` | `bool` |
| `ZigbeeDeviceDiscovery` | `is_searching` | `bool` |
| `Tamper` | `is_tampered` | `bool` |

### Payload models

Exported from `huepy.models` alongside the resources, for typing and for
building payloads by hand.

| Area | Names |
| --- | --- |
| Shared | `HueModel`, `HueResource`, `NamedResource`, `Metadata`, `ResourceIdentifier`, `ResourceType` |
| Light state | `On`, `Dimming`, `Color`, `GroupedColor`, `ColorXY`, `ColorGamut`, `ColorTemperature`, `MirekSchema` |
| Light services | `Effect`, `Effects`, `TimedEffect`, `TimedEffects`, `Signal`, `Gradient`, `GradientPoint`, `GradientMode`, `Powerup`, `PowerupPreset`, `PowerupOnMode`, `Alert`, `Signaling`, `LightCommands` |
| Sensors and input | `MotionReading`, `MotionReport`, `Sensitivity`, `TemperatureReading`, `TemperatureReport`, `ButtonReading`, `ButtonReport`, `ContactReport`, `LightLevelReading`, `LightLevelReport`, `RelativeRotaryReading`, `RelativeRotaryReport`, `RelativeRotaryEvent`, `RelativeRotaryRotation` |
| Devices | `ProductData`, `PowerState`, `TimeZone` |
| Connectivity | `ZigbeeConnectivity`, `ZigbeeChannel`, `ZgpConnectivity`, `WifiConnectivity` |
| Groups | `ResourceGroup`, `SceneAction`, `SceneStatus`, `RecallAction`, `WeekDay`, `SmartScene`, `SmartSceneWeekTimeslot`, `SmartSceneTimeslot`, `SmartSceneStartTime`, `SmartSceneTime`, `SmartSceneActiveTimeslot` |
| Entertainment | `Entertainment`, `EntertainmentConfiguration`, `EntertainmentChannel`, `StreamProxy` |
| Automation and presence | `BehaviorScript`, `BehaviorInstance`, `Geolocation`, `SunToday`, `GeofenceClient` |
| Device management | `ZigbeeDeviceDiscovery`, `DeviceSoftwareUpdate`, `AutoInstall` |
| Smart-home integrations and Hue Secure | `Homekit`, `Matter`, `MatterFabric`, `FabricData`, `Tamper`, `TamperReport`, `CameraMotion` |
| Events | `HueEvent`, `EventResource`, `EventType`, `parse_events` |
| Envelope | `HueResponse`, `HueErrorDetail`, `unwrap`, `unwrap_one` |
| Payload builders | `build_light_payload`, `build_effect_payload`, `build_powerup_payload`, `build_scene_recall` |
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
`build_effect_payload(effect, **tint)`, `build_powerup_payload(preset,
**config)` and `build_scene_recall(action, **recall)` do the same for
`set_effect()`, `set_powerup()` and scene `activate()`.

## Events

`hue.get_event_stream()` yields `models.HueEvent`, not dicts.

```python
async for event in hue.get_event_stream():
    for resource in event.data:
        print(hue.get_name(resource.id), resource.summary)
```

`summary` is the readable form of an event: `"on, 62%, 2700 K"`, `"motion"`,
`"22.4 °C"`. Without it, describing an event means checking every optional
section by hand, since each is optional and each nests differently.

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
| `resource.summary` | `str` | Human-readable description of whichever sections that `EventResource` carries. |

An `EventResource` carries `id`, `type`, `id_v1`, `owner`, and whichever typed
sections changed: light state (`on`, `dimming`, `color`, `color_temperature`),
sensor state (`motion`, `temperature`, `light`, `contact_report`,
`power_state`), or input state (`button`, `relative_rotary`). Future sections
remain on `model_extra`, and `summary` renders those too. Grouped and ungrouped sensor reports share the same
reading models. Events are payloads, not resources: they are *not* bound and
cannot issue commands. Fetch the resource by id to act on it.

A payload that will not parse is logged at warning level and skipped — a
stream meant to run for weeks must not die on one malformed event. The stream
reconnects with exponential backoff, and is closed for you by `hue.close()`.

For the raw decoded payloads instead, use the transport directly:

```python
async for payload in hue.api.raw.subscribe_events():
    print(payload)
```

`models.parse_events(payload)` turns such a payload into `list[HueEvent]`.

### Summaries

`resource.summary` and `change.summary` are both thin wrappers over one public
function:

```
def summarize(state: Mapping[str, Any]) -> str
```

`huepy.summarize` takes a resource's state in the shape the bridge nests it and
returns a comma-separated description — `"on, 62%, 2700 K"` — skipping every
key it does not recognise and returning `""` when none of them are. It renders
power, brightness, colour temperature in Kelvin, colour as hex, effects, motion,
temperature, ambient light level, button events, contact state, battery level,
rotary movement, scene status and renames.

Reach for it directly when you hold a payload rather than a model: a raw event
read through `hue.api.raw.subscribe_events()`, or a section of `model_extra`
this library has no model for yet.

`hue.api.raw.subscribe_event_frames()` yields complete `SSEFrame` objects. Each
has `event_id`, an aware `received_at`, and decoded `events`; multi-line SSE
data stays one frame. `hue.api.raw.event_connections()` additionally yields an
`EventConnection` with `opened_at`, `resumed_from`, and its `frames` iterator.
Both resume reconnects with the last event id. The compatibility
`subscribe_events()` iterator continues to yield individual decoded event dictionaries.

## Last-reported state

`hue.state` is a `huepy.state.HueState`. It exists from construction and does
nothing until started, so handlers and sinks can be registered before the
stream opens. Starting it establishes the stream, buffers it during a
one-request aggregate snapshot, folds the buffered frames without publishing
startup history, and then serves a last-reported view. The bridge gives the
snapshot no cursor, so an event already represented by the snapshot can briefly
regress one field; the next event or reconnect reconciliation repairs it.

Start it with `Hue(state=True)`, which tracks for the client's whole lifetime:

```python
from huepy import models

async with Hue(state=True) as hue:
    desk = hue.state.lights.get("Desk lamp")
    print(hue.state.connected, desk.kelvin)
    all_lights = hue.state.list(models.Light)
```

Or scope it explicitly, which is the same object entered by hand:

```python
async with hue.state as state:
    print(state.connected, len(state.resources))
```

Reads before tracking starts raise `StateNotStartedError` rather than
answering "no lights" for "not tracking yet". `hue.state.tracking` reports
whether observation has started. A *closed* state keeps what it last observed
and stays readable. For a second, independently scoped graph, construct
`huepy.state.HueState(hue)` directly.

`state.lights`, `rooms`, `zones`, `scenes` and `devices` are synchronous local
views with `get(name)`, `by_id(id)`, `list()` and `names()`. `hue.state.resources`,
`by_id(id)`, `list(Model)`, `lights_in(group)`, `room_of(id)`, `zones_of(id)`,
`device_of(id)` and `name_of(id)` provide generic and topology lookup. Every
read returns a fresh bound model; changing it cannot alter the stored graph.

`hue.state.changes(maxsize=4096)` is an async iterator of frozen
`huepy.state.Change` and `Resync` records. A `Change` includes full before and
after resources, raw `delta`, source timestamps, event id, and write-correlation
fields (`origin`, `command_id`, `command_confirmed`, `observation`, and
`transition_ends_at`). A `Resync` has `RECONNECT`, `LAGGED`, or `INCONSISTENT`
reason and marks history whose continuity cannot be guaranteed. Subscribers are
independent and bounded; overflow is coalesced into `Resync(LAGGED)`.

State is never optimistic. Commands observed through its transport may be
attributed to this client only after their transport outcome is known.
`hue.state.fading` is a read-only mapping of current locally issued
`huepy.state.ActiveFade` records, keyed by resource id; each record includes the
command id, normalized target, end, report-reliability end, and confirmation.
See [`STATE_LAYER.md`](STATE_LAYER.md) for the reconnect, reconciliation,
folding, and write-correlation rationale and the bridge observations behind it.

### Reacting to changes

Register a handler instead of owning the loop. Handlers may be plain functions
or coroutine functions, and may be registered before tracking starts.

```python
from huepy import models

subscription = hue.state.on_change(
    lambda change: print(change.at, change.delta),
    name="Desk lamp",
)
hue.state.on_resync(lambda marker: print("gap", marker.reason))
subscription.cancel()
```

| Member | Type | Description |
| --- | --- | --- |
| `hue.state.on_change(handler, *, name, model, resource_id, kind, room)` | `Subscription` | Call `handler` for each matching `Change`. Markers never arrive here. |
| `hue.state.on_resync(handler)` | `Subscription` | Call `handler` for each `Resync` continuity marker. |
| `hue.state.watch(*, name, model, resource_id, kind, room, maxsize=4096)` | `AsyncGenerator[Change]` | Matching changes only, for callers who want the loop. |
| `await hue.state.wait_for(*, name, model, resource_id, kind, room, predicate, timeout)` | `Change` | The first matching change, then stop. Raises `TimeoutError`. |
| `hue.state.describe(change)` | `ChangeContext` | Resolve `name` and `room` for one change. |

Every supplied filter must match. `name=` matches case-insensitively and
ignores surrounding whitespace, so no resource id need appear in caller code.
`model=` is matched against the resource after the change, falling back to the
one before it so a delete still matches. `room=` matches the containing room's
display name, resolved through the resource's owning device — and, for a delete,
through what the record carried, since the resource has already left the graph.
It is the costliest filter: resolving topology revalidates the room set per
change, where `name=` reads a cached map.
`Subscription` has `cancel()` and `active`, and cancels on exit when used as a
`with` block.

`wait_for()` is the one-shot counterpart to `watch()`, for "do this, then wait
until it lands". It registers its subscriber before returning control to the
event loop, so a change caused by a write issued after the call is still seen,
and it closes its iterator on every exit including a timeout.

```python
await hue.lights.turn_on("Desk lamp")
change = await hue.state.wait_for(name="Desk lamp", timeout=5.0)
print(change.summary)
```

`predicate=` takes a `Callable[[Change], bool]` for conditions the filters
cannot express, e.g. `lambda change: "on" in change.delta`.

All handlers share one reader, so a slow handler delays the other handlers —
never the fold loop, which writes into bounded newest-wins buffers that never
block. Use `watch()` in your own task when you need isolation. A handler that
raises is logged and skipped: one bad handler must not stop a process meant to
run for weeks.

`watch()` discards continuity markers, logging each one at `WARNING` first;
`changes()` is the stream that reports gaps. Both register their subscriber
when *called* rather than when first advanced, so nothing published between
building the iterator and consuming it is lost.

`ChangeContext` is a frozen view holding `change`, `name`, and `room`. Name and
room are resolved on request rather than stored on `Change`: they are derived,
mutable, and sometimes unresolvable, and freezing a possibly-stale `"Unknown"`
into a record of an observed fact would be a quiet lie.

### State lifecycle and views

| Member | Type | Description |
| --- | --- | --- |
| `hue.state.tracking` | `bool` | Whether observation has been started on this graph. |
| `hue.state.connected` | `bool` | Whether the event stream is active and startup or reconnect reconciliation is complete. |
| `hue.state.resources` | `list[AnyResource]` | Fresh bound copies of every aggregate-visible resource. |
| `hue.state.fading` | `Mapping[str, ActiveFade]` | Fresh, read-only records for locally issued fades still inside their reporting window. |
| `hue.state.by_id(resource_id)` | `AnyResource \| None` | Local id lookup. |
| `hue.state.list(Model)` | `list[Model]` | Local lookup by concrete model class. |
| `hue.state.name_of(resource_id)` | `str` | Resource or owner name, or `"Unknown"`. |
| `hue.state.device_of(resource_id)` | `Device \| None` | Owning physical device. |
| `hue.state.room_of(resource_id)` | `Room \| None` | Containing room. |
| `hue.state.zones_of(resource_id)` | `list[Zone]` | Every containing zone. |
| `hue.state.lights_in(group)` | `list[Light]` | Resolvable lights in a room or zone. |
| `hue.state.changes(maxsize=4096)` | `AsyncGenerator[Change \| Resync]` | Independent bounded history stream. Requires tracking to be started. |
| `hue.state.close()` | `Coroutine[Any, Any, None]` | Stop observation and close every subscriber when awaited. Called by context exit. |

Each `StateView` (`lights`, `rooms`, `zones`, `scenes`, and `devices`) has
`get(name)`, `by_id(id)`, `list()`, and `names()`. These operations are
synchronous and issue no bridge request. Missing and duplicate names raise the
same errors as the asynchronous high-level collections.

### State records

The state package exports `HueState`, `StateView`, `Change`, `ChangeContext`,
`ChangeKind`, `ChangeFilter`, `ChangeHandler`, `Resync`, `ResyncHandler`,
`ResyncReason`, `ActiveFade`, `Subscription`, and the transport-level
`PendingWrite`.

| Record | Important fields |
| --- | --- |
| `Change` | `kind`, `at`, `summary`, `observed_at`, `event_at`, `received_at`, `event_id`, `resource_id`, `resource_type`, `before`, `after`, `delta`, `resynced`, `origin`, `command_id`, `command_confirmed`, `observation`, `transition_ends_at` |
| `Resync` | `reason`, `gap_started`, `gap_ended`, `dropped`, `detail` |
| `ActiveFade` | `command_id`, `resource_id`, `target`, `sent_at`, `ends_at`, `unreliable_until`, `confirmed` |
| `ChangeContext` | `change`, `name`, `room` |
| `PendingWrite` | `command_id`, `path`, `payload`, `sent_at`, `completed_at`, `status` |

`ChangeKind` contains `UPDATE`, `ADD`, and `DELETE`. `ResyncReason` contains
`RECONNECT`, `LAGGED`, and `INCONSISTENT`. `Change.at` chooses the best
available feature timestamp in the order `observed_at`, `event_at`, then
`received_at`. `Change.summary` renders `delta` — what moved, not the whole
resource — through [`summarize`](#summaries).

`ChangeFilter` carries `name`, `model`, `resource_id`, `kind` and `room`, and
its `matches(change, name_for, room_for=None)` applies them all. The two
resolvers are consulted only when the filter that needs them is set, so an
id-only filter costs no topology lookup.

### Migrating from 0.4

State tracking had two overlapping entry points; 0.5 keeps one.

| 0.4 | 0.5 |
| --- | --- |
| `Hue(live=True)` | `Hue(state=True)` |
| `live_state` on the client (`HueState \| None`) | `hue.state` (always a `HueState`) |
| `async with hue.state() as state:` | `async with hue.state as state:` |
| a second, independent graph | `huepy.state.HueState(hue)` |

The removed `live_state` attribute and `state()` factory are asserted absent
by the test suite, so they cannot quietly return. `hue.state` now exists from
construction, so handlers and sinks can be
registered before the stream opens and nothing has to be threaded through a
call stack. Reads before tracking starts raise `StateNotStartedError` instead
of returning an empty graph.

## Recording history

Persisting the change stream is one constructor argument. `record=` accepts one
sink or several, and implies `state=True`.

```python
from huepy import Hue, SQLiteSink

async with Hue(record=SQLiteSink("hue-history.sqlite3")) as hue:
    print(hue.recorder is not None)
```

| Sink | Writes | Notes |
| --- | --- | --- |
| `SQLiteSink(path)` | One queryable file | WAL mode, so the file stays readable from the `sqlite3` CLI while huepy writes it. |
| `JSONLSink(path)` | One JSON object per line | Lossless and greppable; the escape hatch for questions the schema does not anticipate. |
| `LoggingSink(logger=None, level=INFO)` | Records on a `logging.Logger` | Installs no handler; the host application decides the format. |

Sinks receive frozen `ChangeEntry(record, change, name, room)` and
`ResyncEntry(record, resync)` values, never the state graph. The recorder
resolves topology once, so every sink sees identical information and none
reaches back into the engine. Write your own by satisfying `HistorySink`:

```
class HistorySink(Protocol):
    async def start(self) -> None
    async def write(self, entries: Sequence[HistoryEntry]) -> None
    async def close(self) -> None
```

Both shipped file sinks do their blocking work — including serialisation — on a
dedicated thread each, so a slow disk never stalls the fold loop.

### Failure and loss

The recorder holds an ordinary bounded subscriber. If a sink cannot keep up,
that subscriber overflows and the resulting `Resync(LAGGED)` is written as a
row: the archive states where and how much of itself is missing.

A sink that raises mid-stream never stops state tracking. The batch is dropped,
never retried, and the next batch that sink accepts is prefixed with
`Resync(INCONSISTENT)` carrying `detail["source"] == "sink"` and the count lost.
Repeated failures coalesce into one widened marker, so an outage leaves one
honest row rather than one per flush. `hue.recorder.stats` returns a frozen
`RecorderStats` with `written`, `batches`, `dropped`, `failures`, `last_error`
and `last_error_at`.

A sink that cannot *open* fails `Hue.start()` and closes what already opened:
an unwritable path is a configuration bug, and a recorder that silently records
nothing is worse than a refused start.

### The SQLite schema

Three tables. `change` is the history, `resync` records where that history is
knowingly incomplete, and `current` holds the latest row per resource.

| Table | Key columns |
| --- | --- |
| `change` | `at`, `received_at`, `kind`, `resource_id`, `resource_type`, `name`, `room`, `origin`, `observation`, `resynced`, `command_id`, `on_state`, `brightness`, `payload` |
| `resync` | `reason`, `gap_started`, `gap_ended`, `dropped`, `payload` |
| `current` | `resource_id` (primary key), `at`, `resource_type`, `name`, `room`, `on_state`, `brightness`, `payload` |
| `meta` | `key`, `value` — holds `schema_version` |

Timestamps are normalised to UTC ISO-8601 with explicit microseconds, so
lexicographic order equals chronological order; the original offset survives in
`payload`. `payload` is the full `model_dump_json()` and is the source of
truth — the extracted columns are an index over it, so a column that turns out
wrong can be recomputed. `on_state` and `brightness` come from the resource
*after* the transition rather than from `delta`, so a brightness-only change
does not leave `on_state` NULL.

```sql
-- When was the Desk lamp last on?
SELECT at FROM change WHERE name = 'Desk lamp' AND on_state = 1
ORDER BY at DESC LIMIT 1;

-- What is it right now, without replaying anything?
SELECT on_state, brightness, at FROM current WHERE name = 'Desk lamp';

-- Where is this history incomplete?
SELECT reason, gap_started, gap_ended, dropped FROM resync ORDER BY gap_started DESC;
```

`name` and `room` are the values *at the time of recording*: renaming a room
later does not rewrite history. `payload` always carries `resource_id` for
re-joining against current topology.

**Growth.** A `Change` carries full before and after resources, so a row is
roughly 1–3 KB and a busy household produces on the order of 30 MB a day. huepy
does no rotation or pruning: every retention knob is a policy guess, reclaiming
space needs a long exclusive-lock `VACUUM`, and a library that silently deletes
your data is user-hostile. Prune when you want to:

```sql
-- strftime, not datetime(): `at` is 'T'-separated with microseconds, and
-- datetime() renders a space separator, so the comparison would be off by a day.
DELETE FROM change WHERE at < strftime('%Y-%m-%dT%H:%M:%f', 'now', '-90 days');
VACUUM;
```

## Declarative plans

A plan is a TOML file describing what a scope — a light, a room, a zone, a whole
flat — should look like over the day, and how it reacts to what happens.
`load_plans()` reads one file or a directory of them, and `PlanRunner` executes
the result.

```python
from huepy import Hue, PlanRunner, load_plans

plan = load_plans("./plans")
async with Hue(state=True) as hue:
    async with PlanRunner(hue, plan, changes=hue.state) as runner:
        await runner.run()
```

`changes=` is what lets the plan notice a light someone adjusted by hand. Pass
`hue.state` from a client started with `state=True`; without it the plan never
yields.

TOML, and only TOML. The format's central key is `on`, and YAML 1.1 — what
PyYAML implements — reads the bare word `on` as a boolean, so `set = { on =
false }` would silently parse as `{True: False}`. TOML also rejects a duplicate
key outright where YAML keeps the last one, so a copy-pasted `at` cannot quietly
drop a step. `tomllib` is in the standard library, so this costs no dependency.

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

| Key | Where | Means |
| --- | --- | --- |
| `at` | `[[scenario.step]]` | `"07:30"`, `"sunrise"`, `"sunset+30m"`, `"sunrise-1h15m"`, `"dawn"`, `"dusk"`. |
| `ramp` | step, rule, scenario | How long the fade takes: `"90s"`, `"45m"`, `"2h"`, `"1h15m"`. |
| `set` | step, rule, scenario | Target state. The keys are exactly `build_light_payload()`'s: `on`, `brightness`, `xy`, `mirek`, `rgb`, `hex_color`, `kelvin`. |
| `scope` | `[[scenario]]` | What it drives: `light:Name`, `room:Name`, `zone:Name`. |
| `priority` | `[[scenario]]` | Higher wins when several scenarios cover one scope. |
| `days` | `[[scenario]]` | Restrict a day curve to certain weekdays. |
| `activate_on` / `release_on` | `[[scenario]]` | Make it a mode, dormant until a trigger fires. |
| `when` / `between` / `hold` | `[[scenario.rule]]` | A trigger, an optional window, and how long to stay. |
| `below` / `above` | `[[scenario.rule]]` | For a `light_level:` trigger, the illuminance in lux that fires it. Exactly one of the two. |

Triggers and scopes share one `kind:name` grammar. Scopes take `light:`, `room:`
and `zone:`; triggers take `motion:`, `button:`, `contact:`, `light_level:` and
`signal:`. A sensor carries no name of its own, so `motion:Hall sensor` means
the motion service belonging to the *device* called `Hall sensor`.

| Trigger | Fires when |
| --- | --- |
| `motion:Name` | The device's motion sensor reports motion starting. A `hold` on a motion rule starts counting when the sensor reports the room still again. |
| `button:Name` | Any button on the device goes down (`initial_press`). |
| `contact:Name` | The device's contact sensor opens (`no_contact`). |
| `light_level:Name` | The device's light sensor crosses the rule's `below` or `above` lux. It ends — and a `hold` starts counting — once the reading is back past the threshold by about a factor of five, the band the Hue app itself uses, so a sensor that sees the light it switched on does not blink. A reading that stays on the firing side never re-fires. |
| `signal:name` | The application calls `runner.fire("name")`. |

Every trigger goes through one path, so any of them can sit in `activate_on`,
`release_on` or a rule's `when`: a mode can be woken by a door contact and a
rule can be fired by a signal. The one exception is `light_level:`, which
needs a threshold and so only makes sense on a rule. Every rule naming one
sensor must agree on its threshold.

### A fade is one request, not a tick loop

The bridge runs a transition of up to 6,000 seconds from a single PUT, so a
ninety-minute sunset fade is one request followed by silence. A ramp longer than
that is chained into segments with interpolated waypoints — a three-hour fade is
two requests — rather than stepped. A room is written through its
`grouped_light`, one broadcast instead of one write per bulb, and `on` is never
re-sent to a scope already on, because each payload attribute is a separate
ZigBee message.

### Restarts

The runner keeps no durable state. On start, and after every reconnect, it asks
where each scope *should* be at this instant — interpolating a part-finished
fade — moves there over `defaults.catchup_ramp`, and then hands the rest of the
step's ramp to the bridge. A process killed half an hour into a sunset comes
back, lands in the right place, and carries on fading.

### Manual changes

By default a scope someone changes by hand is yielded: the runner stops
asserting it and takes it back at the first step, trigger or mode that begins
after the change. A switch-off counts, and the next step's `on` is no longer
dropped as redundant — a step that sets no `on` still sends none, so give the
step that should wake a room `on = true`. Because this layer issues fades
lasting up to a hundred minutes, a running fade is checked against its own
arithmetic rather than a time window — movement consistent with the ramp is
ours; a jump, or a power state the fade did not ask for, is a human. Only a
light's own reports are judged: a room's `grouped_light` reports the average
of its members' last readings, which mid-fade is not a measurement. A light
switched on by a fade is judged from dark, a fade to off is allowed to report
the light on and dimming until its ramp ends, and after a hand change to one
light the others' remaining progress is not a second hand change. The fade
that follows a hand change starts from where the human left the light. One
window is blind to brightness, though never to a power change: the
`catchup_ramp` seconds after a cold start. A switch-off leaves the bridge's
brightness at the target of the transition it was running — measured, not
assumed — so the fade that follows one starts from a known level and is judged
like any other. With
`on_manual_change = "reassert"` the scope is never yielded, but the runner still
notices the change and puts the light back.

### Rules

```toml
[[scenario]]
name = "hall-night-light"
scope = ["room:Hallway"]
priority = 10

[[scenario.rule]]
when = "motion:Hall sensor"
between = ["sunset", "sunrise"]
ramp = "2s"
hold = "90s"
set = { on = true, brightness = 15, kelvin = 2000 }
```

A rule that fires *holds* each of its scenario's scopes with its `set`, at the
scenario's priority, for `hold`. Firing again while held extends the hold
without another request. For `motion:` the clock starts when the sensor
reports the room still, so the light stays as long as someone is there and
`hold` is how long it lingers after they leave. Without `hold` the scope is
held until its next scheduled step — not forever, so a button press cannot
switch a day curve off for good; when nothing scheduled covers the scope, the
hold lasts until a hand change, a higher-priority claim, or the owning mode
releasing. `between` is checked when the trigger fires, and wraps midnight.
A `light_level:` rule fires on the first reading after the process starts if
that reading is already on the firing side — so a plan started after dark
lights the room, and so does a restart at three in the morning. Give a rule a
`between` when that would be unwelcome; an overcast-afternoon rule wants
`["sunrise", "sunset"]`. A `between` is checked when the crossing happens, so
a crossing before the window opens is missed, not deferred.

When the hold lapses the scope goes back to whatever is underneath. A scope
nobody claims is left alone, so a motion light that should switch itself off
needs a resting state to hand back to — a flat `set = { on = false }`
scenario at a lower priority, as in `examples/plans/flat.toml`.

Handing a scope back to a day curve never snaps: the return fade is the
curve's remaining ramp or `defaults.catchup_ramp`, whichever is longer. A mode
or a flat state keeps the ramp its author wrote.

A scope someone changed by hand rejoins at the next trigger as well as at the
next step, and a hand change during a hold drops the hold, so the plan rejoins
with its schedule rather than a stale rule.

### Signals

`runner.fire("movie_started")` fires the trigger `signal:movie_started`:
every mode whose `activate_on` names it wakes, every `release_on` gives its
scope back, and every rule whose `when` names it fires. It is not a coroutine,
so a callback can call it directly, and it returns what it did.

From outside the process, `huepy plan run` serves the same hook over HTTP on
the loopback interface, and `huepy plan signal movie_started` from another
shell posts to it:

```console
curl -X POST http://127.0.0.1:8757/signals/movie_started
curl http://127.0.0.1:8757/signals
```

`POST /signals/NAME` answers `{"signal": ..., "outcomes": [...]}`, or 404 with
the names the plan does listen for. `GET /signals` lists them. `--listen
HOST:PORT` moves the server; listening on anything but loopback requires
`--token` (or `HUEPY_PLAN_TOKEN`), which every request must then carry as
`Authorization: Bearer`. A Home Assistant `rest_command` with that URL and
header is all it takes to start the movie mode from an automation. A second
plan started on the same port fails with a `PlanError` naming it. Embedding
users get the same server as `SignalServer(runner.fire, runner.signals)`.

### Command line

| Command | Touches the bridge | Does |
| --- | --- | --- |
| `huepy plan check PATH` | no | Parses the files and reports what is malformed. |
| `huepy plan explain PATH [--at ISO]` | no | Prints the day, every solar anchor resolved, with the request count per step. |
| `huepy plan validate PATH` | reads | Also resolves every name, reporting all unknown ones at once, and prints what each bound to: the `grouped_light` and member lights behind a room, the services behind a sensor, a warning for a sensor disabled on the bridge, a warning for a dimmer or sensor the Hue app's own automation also listens to, whose action arrives as a hand change and cancels the rule, and a warning for a light driven both as itself and through its room, where each write is invisible to the other scope and the last one wins. `run` prints the same report before it starts. |
| `huepy plan run PATH [--listen HOST:PORT] [--token T]` | writes | Executes the plan until Ctrl-C or SIGTERM, which stop it after the write it is on, serving its signals at `http://127.0.0.1:8757/signals` meanwhile. |
| `huepy plan signal NAME [--url U] [--token T]` | no | Fires `signal:NAME` into the running plan and prints what it did. |
| `huepy plan schema` | no | Emits the format as JSON Schema, for editor completion. |

A name that does not resolve raises `PlanError`, which carries the file it came
from. Nothing is written before resolution succeeds, so a misspelled room cannot
half-run a plan.

`huepy -v plan run PATH` logs every write — scope, target, ramp, request count
and when the fade ends — plus hand changes, triggers and reconnects. `-vv`
adds what was skipped as already in force, how long the loop sleeps, each
progress report the override arithmetic explained, and the wire payload of
every PUT. `-q` keeps only errors.

### What `huepy.plans` exports

| Name | Is |
| --- | --- |
| `load_plans(path)`, `load_plan(path)` | Read a `.toml` file, or a directory of them, into a `Plan`. `PlanError` on anything wrong, naming the file and key. |
| `Plan`, `Scenario`, `Step`, `Rule`, `Action`, `Defaults`, `Location` | The format, as frozen pydantic models. `Plan.model_json_schema()` is what `huepy plan schema` prints. |
| `PlanRunner` | Runs a plan. `changes=` takes anything with `on_change` and `on_resync` — `hue.state` — and `clock=` / `sleep=` are injectable for tests. `fire(name)` returns what the signal did, one phrase per scenario it reached, and `signals` is the set of names the plan listens for. `stop()` asks `run()` to return after the write it is on, so a signal handler can end a daemon without cancelling it. |
| `PlanClient`, `ChangeSource` | The two Protocols the runner depends on. `Hue` and `HueState` satisfy them. |
| `SignalServer(fire, known, *, host, port, token)`, `DEFAULT_SIGNAL_PORT` | Serves `signal:` triggers over HTTP; `async with` it around `runner.run()`. Loopback unless a token is given. |
| `resolve(client, plan)`, `ResolvedPlan`, `Binding`, `TriggerBinding` | Bind every name to a resource id in one snapshot, reporting every unknown name together. `ResolvedPlan.warnings` lists what bound but will not behave — a sensor disabled on the bridge never fires — and the runner logs each at WARNING. |
| `waypoints_for_day(plan, scenario, day, zone)`, `Waypoint` | A scenario's day curve pinned to instants. Pure. |
| `current_step(plan, scenario, now, zone)`, `target_at(...)`, `next_transition(...)` | Which step is in force, where the light should already be, and when the next fade starts. Pure; `now` is a parameter. |
| `zone_of(location)` | The zone a plan's clock times are written in — `None` for the host's own, which is resolved per instant rather than frozen at one DST offset. |
| `solar_event(event, date, latitude, longitude)`, `SunEvent` | Sunrise, sunset, dawn or dusk as an aware UTC datetime; `None` in polar day or night. |

The scheduling arithmetic, executor and arbiter stay reachable as
`huepy.plans.timeline`, `huepy.plans.executor` and `huepy.plans.arbiter`, but
are not part of the promise this table makes.

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

`hue.api` is the lower-level, typed CLIP v2 namespace. Its plural attributes
are exclusively id-addressed; it never guesses whether a string is a name or
an id. Every handler provides `list()`, `get(resource_id)`,
`update(resource_id, data)`, and `delete(resource_id)`. These write methods
return bridge `list[models.ResourceIdentifier]`; model and collection commands
instead return `CommandResult`.

| Attribute | Class | Model | Extra methods |
| --- | --- | --- | --- |
| `hue.api.lights` | `Light` | `models.Light` | `turn_on`, `turn_off`, `set_brightness`, `set_color`, `set_color_temperature`, `get_lights_on`, `get_service_ids_on`, `get_device_ids_on` |
| `hue.api.grouped_lights` | `GroupedLight` | `models.GroupedLight` | `turn_on`, `turn_off`, `set_brightness`, `set_color`, `set_color_temperature` |
| `hue.api.rooms` | `Room` | `models.Room` | `create`, `grouped_light_id`, `get_from_light_service_id` |
| `hue.api.zones` | `Zone` | `models.Zone` | `create`, `grouped_light_id` |
| `hue.api.scenes` | `Scene` | `models.Scene` | `create`, `activate` |
| `hue.api.smart_scenes` | `SmartScene` | `models.SmartScene` | `create`, `activate`, `deactivate` |
| `hue.api.service_groups` | `ServiceGroup` | `models.ServiceGroup` | `create` |
| `hue.api.entertainments` | `Entertainment` | `models.Entertainment` | — |
| `hue.api.entertainment_configurations` | `EntertainmentConfiguration` | `models.EntertainmentConfiguration` | `create`, `start`, `stop` |
| `hue.api.motions` | `Motion` | `models.Motion` | `enable`, `disable`, `set_sensitivity`, `get_motion_state`, `get_last_motion` |
| `hue.api.grouped_motions` | `GroupedMotion` | `models.GroupedMotion` | `enable`, `disable` |
| `hue.api.temperatures` | `Temperature` | `models.Temperature` | `enable`, `disable` |
| `hue.api.contacts` | `Contact` | `models.Contact` | `enable`, `disable` |
| `hue.api.camera_motions` | `CameraMotion` | `models.CameraMotion` | `enable`, `disable` |
| `hue.api.tampers` | `Tamper` | `models.Tamper` | — |
| `hue.api.buttons` | `Button` | `models.Button` | — |
| `hue.api.relative_rotaries` | `RelativeRotary` | `models.RelativeRotary` | — |
| `hue.api.zigbee_connectivities` | `ZigbeeConnectivity` | `models.ZigbeeConnectivity` | — |
| `hue.api.zgp_connectivities` | `ZgpConnectivity` | `models.ZgpConnectivity` | — |
| `hue.api.wifi_connectivities` | `WifiConnectivity` | `models.WifiConnectivity` | — |
| `hue.api.devices` | `Device` | `models.Device` | — |
| `hue.api.device_powers` | `DevicePower` | `models.DevicePower` | — |
| `hue.api.zigbee_device_discoveries` | `ZigbeeDeviceDiscovery` | `models.ZigbeeDeviceDiscovery` | `search`, `search_with_default_link_key` |
| `hue.api.device_software_updates` | `DeviceSoftwareUpdate` | `models.DeviceSoftwareUpdate` | `install`, `set_auto_install` |
| `hue.api.light_levels` | `LightLevel` | `models.LightLevel` | — |
| `hue.api.grouped_light_levels` | `GroupedLightLevel` | `models.GroupedLightLevel` | — |
| `hue.api.bridges` | `Bridge` | `models.Bridge` | — |
| `hue.api.bridge_homes` | `BridgeHome` | `models.BridgeHome` | — |
| `hue.api.geolocations` | `Geolocation` | `models.Geolocation` | `set_location` |
| `hue.api.geofence_clients` | `GeofenceClient` | `models.GeofenceClient` | `create` |
| `hue.api.behavior_scripts` | `BehaviorScript` | `models.BehaviorScript` | — |
| `hue.api.behavior_instances` | `BehaviorInstance` | `models.BehaviorInstance` | `create`, `enable`, `disable`, `configure` |
| `hue.api.homekits` | `Homekit` | `models.Homekit` | `reset` |
| `hue.api.matters` | `Matter` | `models.Matter` | `reset` |
| `hue.api.matter_fabrics` | `MatterFabric` | `models.MatterFabric` | — |

`hue.api.raw` exposes the open decoded-JSON `Transport` for an advanced CLIP
operation without a typed handler.

Entertainment areas stream colour to lights at high frame rate over a
separate UDP/DTLS channel this library does not implement; the REST side --
listing, configuring, and starting or stopping a session -- is what
`hue.api.entertainments` and `hue.api.entertainment_configurations` expose.
Entertainment configurations carry `metadata.name`, so they read like a named
collection, but no high-level named collection exists for them yet; use the
id-based handler above.

`hue.api.zigbee_device_discoveries` is how new lights and other Zigbee
devices are paired -- there is no other route to it in the v2 API.
`hue.api.device_software_updates` reads and controls a device's firmware.
`hue.api.geolocations` and `hue.api.geofence_clients` give automations sun
and presence data to react to; `hue.api.behavior_scripts` and
`hue.api.behavior_instances` are the v2 successor to the v1 rule engine, a
script being a bridge-shipped template and an instance being one configured,
running copy of it.

`hue.api.homekits` and `hue.api.matters` expose the bridge to Apple Home and
the Matter fabric, and each can be reset. `hue.api.matter_fabrics` lists
commissioned fabrics and deletes one to decommission it -- there is no
update. `hue.api.tampers` and `hue.api.camera_motions` are Hue Secure sensor
services; the latter is enabled and disabled exactly like
`hue.api.motions`.

### Light commands

```
async turn_on(resource_id) -> list[ResourceIdentifier]
async turn_off(resource_id) -> list[ResourceIdentifier]
async set_brightness(resource_id, brightness) -> list[ResourceIdentifier]
async set_color(resource_id, x, y) -> list[ResourceIdentifier]
async set_color_temperature(resource_id, mirek) -> list[ResourceIdentifier]
```

```python
lights = await hue.api.lights.list()
await hue.api.lights.set_brightness(lights[0].id, 40.0)
```

These take neither `transition` nor the human-unit colour arguments, and each
one is its own request — that is what the model commands add. Rooms and zones
are grouping resources, so the id-level path to their lights is explicit:

```python
kitchen = await hue.api.rooms.get(room_id)
service_id = kitchen.service_id(models.ResourceType.GROUPED_LIGHT)
if service_id is not None:
    await hue.api.grouped_lights.set_brightness(service_id, 40.0)
```

`grouped_light_id` raises `ValueError` if the group has no such service.

### `Light`

```
async get_lights_on() -> list[models.Light]      # lights currently on
async get_service_ids_on() -> list[str]          # their service ids
async get_device_ids_on() -> list[str]           # their owning device ids
```

### `Room`, `Zone`, `Scene`, `SmartScene`, `ServiceGroup`

```
async Room.create(name, devices: list[str]) -> list[ResourceIdentifier]
async Room.get_from_light_service_id(light_id) -> str | None
async Zone.create(name, services: list[dict[str, str]]) -> list[ResourceIdentifier]
async Scene.create(name, room_id, *, actions=None, speed=None, auto_dynamic=None) -> list[ResourceIdentifier]
async Scene.activate(scene_id, *, action=RecallAction.ACTIVE, duration=None, brightness=None) -> list[ResourceIdentifier]
async SmartScene.create(name, group_id, week_timeslots, *, group_rtype=ResourceType.ROOM, transition_duration=None) -> list[ResourceIdentifier]
async SmartScene.activate(scene_id) -> list[ResourceIdentifier]
async SmartScene.deactivate(scene_id) -> list[ResourceIdentifier]
async ServiceGroup.create(name, services, archetype="sensor_group") -> list[ResourceIdentifier]
```

`Zone.create` and `ServiceGroup.create` take service references, each a dict of
`rid` and `rtype`; `Room.create` takes plain device ids. `SmartScene.create`
takes `week_timeslots` in the bridge's weekly-schedule shape.

### `Motion`

```
async set_sensitivity(resource_id, sensitivity: int) -> list[ResourceIdentifier]
async get_motion_state(resource_id) -> bool
async get_last_motion(resource_id) -> str   # ISO timestamp, or ""
```

`set_sensitivity` raises `TypeError` for a non-integer and `ValueError` if the
value is negative or above the sensor's reported maximum.

### `EntertainmentConfiguration`

```
async create(config: dict[str, Any]) -> list[ResourceIdentifier]
async start(resource_id) -> list[ResourceIdentifier]
async stop(resource_id) -> list[ResourceIdentifier]
```

```python
areas = await hue.api.entertainment_configurations.list()
await hue.api.entertainment_configurations.start(areas[0].id)
await hue.api.entertainment_configurations.stop(areas[0].id)
```

`create` takes the configuration body in the bridge's own shape -- its
channels and members are not modelled as constructor arguments.

### `BehaviorInstance`, `Geolocation`, `GeofenceClient`

```
async BehaviorInstance.create(script_id, configuration: dict, *, enabled=True, name=None) -> list[ResourceIdentifier]
async BehaviorInstance.enable(resource_id) -> list[ResourceIdentifier]
async BehaviorInstance.disable(resource_id) -> list[ResourceIdentifier]
async BehaviorInstance.configure(resource_id, configuration: dict) -> list[ResourceIdentifier]
async Geolocation.set_location(resource_id, latitude: float, longitude: float) -> list[ResourceIdentifier]
async GeofenceClient.create(name, *, is_at_home=False) -> list[ResourceIdentifier]
```

```python
scripts = await hue.api.behavior_scripts.list()
await hue.api.behavior_instances.create(
    scripts[0].id, {"when": {}}, name="Sunset dimming"
)
await hue.api.geolocations.set_location(geolocation_id, 52.5, 13.4)
await hue.api.geofence_clients.create("Guest phone", is_at_home=True)
```

`configuration` is validated against the script's own `configuration_schema`,
so its shape is carried as arbitrary JSON rather than modelled. `set_location`
raises `ValueError` for a latitude outside -90..90 or a longitude outside
-180..180.

### `ZigbeeDeviceDiscovery`, `DeviceSoftwareUpdate`

```
async ZigbeeDeviceDiscovery.search(resource_id, *, install_codes=None, channels=None) -> list[ResourceIdentifier]
async ZigbeeDeviceDiscovery.search_with_default_link_key(resource_id, *, install_codes=None, channels=None) -> list[ResourceIdentifier]
async DeviceSoftwareUpdate.install(resource_id) -> list[ResourceIdentifier]
async DeviceSoftwareUpdate.set_auto_install(resource_id, *, on: bool, update_time=None) -> list[ResourceIdentifier]
```

```python
await hue.api.zigbee_device_discoveries.search(discovery_id, channels=[11, 15])
await hue.api.device_software_updates.set_auto_install(
    update_id, on=True, update_time="03:00:00"
)
```

`search_with_default_link_key` additionally allows the well-known default
link key, for older or third-party devices that join no other way -- at a
small security cost.

### `Homekit`, `Matter`

```
async Homekit.reset(resource_id) -> list[ResourceIdentifier]
async Matter.reset(resource_id) -> list[ResourceIdentifier]
```

```python
await hue.api.homekits.reset(homekit_id)
fabrics = await hue.api.matter_fabrics.list()
await hue.api.matter_fabrics.delete(fabrics[0].id)
```

## Configuration

```
@dataclass
class HueConfig:
    bridge_ip: str = ""
    app_key: str | None = None
    config_path: Path = default_config_path()
    tls: TlsMode | None = None
    bridge_id: str | None = None
    rate_limit: bool | None = None
```

`bridge_ip`, `app_key` and `bridge_id` resolve as argument, then environment
(`HUE_BRIDGE_IP`, `HUE_APP_KEY`, `HUE_BRIDGE_ID`), then the config file. A
missing address is a `ValueError`; there is no built-in default.

`tls` and `rate_limit` resolve as argument, then environment (`HUE_TLS`,
`HUE_RATE_LIMIT`), then the secure default (`TlsMode.VERIFIED`, pacing on). An
explicit argument always wins — a `None` field is what marks "unset", so a
stale `HUE_TLS=insecure` can never downgrade a caller who passed
`tls=TlsMode.VERIFIED`. An unrecognised `HUE_TLS` value is a `ValueError`.
`TlsMode` is a `StrEnum` with members `VERIFIED` and `INSECURE`, exported from
`huepy`.

`config_path` resolves as argument, then `HUE_CONFIG_PATH`, then
`$XDG_CONFIG_HOME/huepy/config.json` (`~/.config/huepy/config.json` by
default). The default is deliberately absolute: a relative one would tie the
stored credential to whichever directory a program was started from.

`save()` persists the address, key and bridge id to the config file with mode
`0600`. Passing `save(app_key)` also replaces the stored key; calling `save()`
bare records the address (and any known bridge id) and keeps any existing key. Because the address is stored,
a configured machine needs neither arguments nor environment — and no address
has to be committed to source.

If the filesystem ignores `chmod` and the mode does not take, `save()` issues
an `InsecureConfigWarning` (exported from `huepy`) instead of silently leaving
the credential world-readable.

`HueHttpClient` is the concrete transport, also exported from `huepy`; the
client builds one for you at `start()` and exposes it through `hue.api.raw`.
Its pool allows three connections per bridge. A GET that receives 429 or 503
is retried at most three times with bounded exponential backoff; PUT, POST and
DELETE are never replayed automatically.

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
| `AmbiguousResourceError` | `name`, `resource_ids` | A high-level name matched more than one resource |
| `StateNotStartedError` | — | `hue.state` was read before tracking started |

`HueResponseError` matters: the v2 API reports many failures this way, so a
write can "succeed" at the HTTP level and still have been rejected.

### Partial failures

The bridge overloads `errors[]` for two different things, and only the
`error_code` on each entry separates them:

| `error_code` | Meaning | huepy |
| --- | --- | --- |
| `communication_error` | The command was accepted, but a device's radio is flaky, so it "may not have effect" | Logged as a warning; the call returns normally |
| `attribute_may_have_no_effect` | The command was accepted, but the light is "soft off", so one attribute may not land | Logged as a warning; the call returns normally |
| anything else | The request itself was wrong, e.g. setting colour temperature on a light that has none | Raises `HueResponseError` |

The first two are advisory; every other code is blocking. Classification is per
error, so a blocking code alongside an advisory one still raises, and an
advisory error on a response that changed nothing raises too.

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

The high-level API keeps names, human units, service routing, and composed
commands in one uniform expression:

```python
import asyncio

from huepy import AmbiguousResourceError, Hue, HueError, ResourceNotFoundError


async def main() -> None:
    try:
        async with Hue(bridge_ip="192.168.1.100") as hue:
            for light in await hue.lights.list():
                if light.is_on:
                    print(f"{light.name}: {light.brightness}% {light.mirek} mirek")

            await hue.rooms.set("Kitchen", brightness=40, kelvin=2200, transition=2.0)

            desk = await hue.lights.get("Desk lamp")
            await desk.set_rgb((255, 136, 0), transition=1.0)

            async for event in hue.get_event_stream():
                print(event.event_type, event.resource_ids)
                break
    except ResourceNotFoundError as exc:
        print(f"No such resource: {exc.name}. Known: {exc.known}")
    except AmbiguousResourceError as exc:
        print(f"Name is ambiguous: {exc.name}. Ids: {exc.resource_ids}")
    except HueError as exc:
        print(f"{type(exc).__name__}: {exc}")


asyncio.run(main())
```

The lower-level typed API expresses the bridge mechanics explicitly. This is
the right level when an application already stores ids or needs to construct a
CLIP payload itself; it is not necessary boilerplate for ordinary control:

```python
from huepy import models

room = await hue.api.rooms.get(room_id)
grouped_light_id = room.service_id(models.ResourceType.GROUPED_LIGHT)
if grouped_light_id is not None:
    await hue.api.grouped_lights.update(
        grouped_light_id,
        {
            "dimming": {"brightness": 40},
            "color_temperature": {"mirek": 455},
            "dynamics": {"duration": 2000},
        },
    )

# For an operation without a typed handler, opt into decoded JSON explicitly.
payload = await hue.api.raw.get(f"/clip/v2/resource/room/{room_id}")
```
