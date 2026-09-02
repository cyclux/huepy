# huepy examples

Runnable scripts, each with its exact invocation in the module docstring. They
are part of the type-check gate, so they stay current with the API.

Run **`authenticate.py`** once first; after that every other script reads the
bridge address and application key from the config file it wrote, so they take
no arguments beyond the resource name they act on.

## Setup & discovery

| Script | What it shows |
| --- | --- |
| `authenticate.py <ip>` | One-time pairing: press the link button, store the address and app key. |
| `discover_bridge.py` | Find bridges on the network (mDNS, then the cloud endpoint). |
| `from_discovery.py` | Discover and connect in one call with `Hue.from_discovery()`. |

## Everyday control

| Script | What it shows |
| --- | --- |
| `basic.py` | List every light with its on/off, brightness and colour temperature. |
| `color_light.py <light> [hex]` | Set one light a colour, gamut-clamped, then restore it. |
| `control_room.py <room>` | Dim a room to a warm glow by name, hold, restore per light. |
| `control_zone.py <zone>` | The same, for a zone (lights grouped across rooms). |
| `scenes.py <scene> [smart]` | Recall a scene by name; optionally run a smart scene's schedule. |

## Light features

| Script | What it shows |
| --- | --- |
| `effects.py <light>` | The candle effect, a timed sunrise, and a gradient. |
| `attention.py <light>` | `identify`, `signal`, `alert`, and relative brightness/colour nudges. |
| `powerup.py <light>` | What a light does when its mains power returns. |

## Devices

| Script | What it shows |
| --- | --- |
| `pair_device.py` | Pair a new bulb via the `zigbee_device_discovery` search. |

## Events, state & history

| Script | What it shows |
| --- | --- |
| `listen_events.py` | Print the parsed event stream until Ctrl-C. |
| `track_state.py [room]` | The maintained local graph with `Hue(state=True)`; react to changes. |
| `record_history.py` | Record every bridge change to SQLite, then query it. |

## Under the hood

The `two_ways_*` scripts do one task twice -- once against the id-addressed
`hue.api` with hand-built payloads, once with the high-level API -- to show what
the convenience layer does for you.

| Script | What it shows |
| --- | --- |
| `low_level.py` | A read-only tour of the id-addressed typed API and the raw transport. |
| `two_ways_color.py <light> [hex]` | Setting a colour by hand vs. one `light.set(hex_color=...)`. |
| `two_ways_room.py <room>` | Dimming a room by hand vs. one `room.set(kelvin=...)`. |
| `two_ways_events.py` | Reading an event by hand vs. `EventResource.summary`. |

## Declarative plans

| Script | What it shows |
| --- | --- |
| `run_plan.py` | Loading a TOML plan and running it: day curves, sun anchors, a motion rule, a signalled mode. |
| `plans/flat.toml` | The plan format itself, commented. |
