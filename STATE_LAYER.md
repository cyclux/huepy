# HueState architecture and bridge evidence

This document explains the architecture and measured bridge behaviour behind
the current state layer. The user-facing contract lives in
[`README.md`](README.md) and [`API_REFERENCE.md`](API_REFERENCE.md); the code
and tests are authoritative when this rationale and the implementation differ.

## Scope and status

Version 0.3.0 added an opt-in, continuously maintained, last-reported view of
the aggregate-visible Hue resource graph. Version 0.5.0 collapsed its two entry
points into one permanent `hue.state`, added handler registration, and added a
configurable recording layer:

```python
async with Hue(state=True) as hue:
    desk = hue.state.lights.get("Desk lamp")
    print(desk.brightness)

    async for item in hue.state.changes():
        print(item)
```

Ordinary handler reads remain uncached and continue to issue bridge requests.
The state layer is explicit: `hue.state` is a stopped `HueState` from
construction, and `Hue(state=True)` -- or entering it directly -- starts
observation. Reads before that raise `StateNotStartedError`, because a graph
that has never taken a snapshot would otherwise report an empty bridge.

The current implementation includes:

- a typed aggregate snapshot with a generic fallback for future resource types;
- complete SSE frame parsing, connection metadata, reconnect resume cursors,
  and multi-stream cleanup;
- reconnect reconciliation and explicit uncertainty markers;
- independent bounded change subscribers;
- write outcome observation, self-write correlation, and fade metadata;
- read-side helpers for capture/restore, groups, scenes, sensors, and
  connectivity;
- typed motion, temperature, ambient-light, button, contact, battery, rotary,
  and grouped-light event deltas;
- a three-connection per-bridge pool and bounded retries for GET requests that
  receive 429 or 503;
- privacy-minimised real-bridge fixtures and opt-in live regression probes.

## Current architecture

### Snapshot and model registry

`Hue.snapshot()` fetches `/clip/v2/resource` once and returns bound
`models.AnyResource` instances. Known `type` values select a concrete model
through `models.RESOURCE_MODELS`; an unknown value becomes a tolerant generic
`HueResource`, preserving fields in `model_extra`.

The registry is the shared source for aggregate parsing and `HueState`. Adding
a concrete aggregate model requires updating the registry, the `AnyResource`
union, exports, the matching handler where useful, API documentation, and
tests.

### Event transport

The transport exposes three levels:

- `subscribe_events()` yields individual decoded event dictionaries for
  compatibility;
- `subscribe_event_frames()` yields `SSEFrame(event_id, received_at, events)`;
- `event_connections()` yields `EventConnection(opened_at, resumed_from,
  frames)` so recovery boundaries remain visible.

The frame parser handles SSE field ordering and multi-line `data:` payloads.
Reconnects use exponential backoff and send the most recently observed SSE id
as `Last-Event-ID`. TCP keepalive is enabled and tuned where the platform
supports it. `Hue.close()` closes every stream it created, not only the latest
subscriber.

### Startup and reconnect reconciliation

Entering `HueState` opens the event stream before requesting the aggregate
snapshot. Frames received while the snapshot is in flight are buffered and
folded before the context returns, without publishing artificial startup
history.

Every reconnect is conservative:

1. the transport requests retained events with `Last-Event-ID`;
2. `HueState` continues draining frames while it requests a fresh snapshot;
3. it publishes `Resync(RECONNECT)` because replay completeness cannot be
   proved;
4. it emits `Change(resynced=True)` for differences between replayed history
   and the fresh snapshot;
5. the snapshot becomes canonical, with buffered frames folded over it.

This intentionally over-reports possible gaps. The bridge silently truncates
its replay buffer, so absence of an error is not proof of continuity.

### State reads

`HueState` stores raw detached payloads internally. Every public read reparses
a deep copy and binds it to the owning client, preventing callers from mutating
canonical state while keeping bound-model commands available.

The named `lights`, `rooms`, `zones`, `scenes`, and `devices` views provide
synchronous local `get(name)`, `by_id(id)`, `list()`, and `names()` lookup.
Generic and topology helpers are `resources`, `by_id`, `list(Model)`, `name_of`,
`device_of`, `room_of`, `zones_of`, and `lights_in`.

`lights_in` delegates group membership to `ResourceGroup.contains_light`, the
same rule the stateless `await room.lights()` applies. One rule in one place:
a room's children are devices and a zone's are light services, and the two id
spaces do not overlap, so testing a light's own id and its owner's covers both
without a branch.

### Folding and history

Update deltas are deep-merged into the prior raw resource and revalidated.
Multiple entries for the same resource in one event are merged in wire order.
Add and delete events update the graph directly. An update for an id absent
from the aggregate snapshot triggers a typed point fetch; an unresolvable or
invalid shape produces `Resync(INCONSISTENT)` rather than silently claiming
complete history.

`state.changes(maxsize=4096)` gives each subscriber an independent bounded
queue. Overflow is represented by `Resync(LAGGED)` and a dropped-count value.
Slow subscribers cannot block state folding or other subscribers.

`ChangeFilter` narrows on `name`, `model`, `resource_id`, `kind`, and `room`,
all ANDed. Its `matches(change, name_for, room_for=None)` takes the two
resolvers rather than the graph, and consults each only when the filter that
needs it is set, so an id-only filter costs no topology lookup. Both resolvers
fall back to what the record carried, which is what keeps a delete matching a
`name=` or `room=` filter after the resource has been folded out of the graph.

`state.wait_for(...)` is the one-shot form of `watch()`: the same filters plus
an optional `predicate` and `timeout`. It registers its subscriber before
yielding to the event loop, so a change caused by a write issued after the call
is still observed, and it closes its iterator on every exit path.

`Change` records retain:

- add/update/delete kind and complete before/after resources;
- the raw delta;
- sensor observation, bridge event, and local receive timestamps;
- SSE event id;
- reconnect-diff provenance;
- write origin, command id/outcome, observation class, and transition end.

The computed `Change.at` selects `observed_at`, then `event_at`, then
`received_at`. `Change.summary` renders the delta through `huepy.summarize`,
the same dict-in formatter `EventResource.summary` uses -- one implementation,
because the event stream and the fold produce the same nested section shape.

### Write correlation and fades

`HueHttpClient` publishes immutable `PendingWrite` lifecycle records around
PUT requests. A write starts as `pending` and finishes as `accepted`,
`rejected`, or `unknown`. Observers cannot fail the request.

`HueState` registers one observer for its lifetime. Compatible deltas within
the correlation window can be labelled `origin="self"`. Rejected commands are
not attributed. Grouped-light writes are correlated with the group service and
the member lights resolvable from the current topology.

State is never updated optimistically. A locally issued transition appears in
`state.fading` until the measured reporting allowance ends. The immediate
target echo is marked `observation="command_echo"`; later bridge reports are
marked `"reported"`.

## Measured bridge behaviour

These observations came from one BSB002 bridge running CLIP API 1.78.0. They
are evidence for the current defensive design, not universal firmware
guarantees.

- `Last-Event-ID` was honoured and retained frames replayed immediately.
- The observed replay buffer held roughly 15 frames and silently returned only
  the surviving tail when an older cursor was requested.
- No periodic application keepalive was observed: the bridge sent one `: hi`
  comment at connection and could then remain quiet for more than 90 seconds.
- SSE frame ids behaved as ordered cursors but no documented successor rule or
  replay/live boundary was observed.
- A transition PUT produced an immediate event containing the commanded target,
  while physical progress reports arrived on a much slower device cadence.
- That echo carries the bridge's own quantisation, not the commanded number: a
  PUT of `20.0` came back as `20.16`. Brightness is stored as 254 levels, so
  the grid is `100/253` ~ 0.395 apart and the worst-case echo error under
  round-to-nearest is a *half* step -- 0.198, at `50.0` -> `49.80`. The old
  0.1 tolerance sat below that half step, which is why `command_echo` was
  unreachable for every brightness. The allowance is 0.5: a whole step, since
  the rounding rule itself was not established. Only brightness is widened --
  `mirek` is an int end to end and echoes exactly, and `xy` spans 0..1, where
  the brightness allowance would match almost anything.
- A write to a light that is switched off is *accepted* and answered with
  `attribute_may_have_no_effect` ('is "soft off", command (.dimming.brightness)
  may not have effect'), alongside `communication_error` for an unreachable
  radio. Both list the resource in `data`, so both are advisory; treating the
  second as blocking made capture/restore fail for any light that was off.
- The accepted transition ceiling was 6,000,000 milliseconds; 6,000,001 was
  rejected.
- The aggregate endpoint returned 186 resources across 27 types on the test
  bridge but omitted resources available from the `motion_area_candidate`
  endpoint. State topology therefore tolerates unresolved references.
- A 40-request read burst completed fastest with three concurrent connections
  on the measured bridge. No stable firmware-independent rejection threshold
  was established, so the pool limit is a throughput choice, not a claimed Hue
  protocol limit.

## Models and read symmetry

The 0.3.0 model work includes:

- aware event and sensor timestamps;
- grouped colour whose `xy` may legitimately be absent;
- scene actions and status, including last recall;
- service-group `services` references;
- `Light.capture()` and `restore()` with resource-id validation;
- computed `Light.kelvin`, `Light.rgb`, and `LightLevel.lux`;
- `Device.service_id()` and group `service_id()` helpers;
- `zigbee_connectivity` and tolerant `relative_rotary` support;
- local enforcement of the measured 6,000-second transition ceiling.

Stored configuration intentionally remains a tolerant standard-library
dataclass. Malformed, unreadable, unknown, or wrong-typed stored values fall
back to explicit arguments, environment variables, or defaults as already
covered by the configuration tests.

## Evidence and privacy

Real-bridge tests are opt-in and double-guarded:

```console
HUEPY_INTEGRATION=1 uv run pytest -m integration
```

They physically change lights and must never be run without the operator's
explicit request. Mutating tests capture state before writes and restore it in
cleanup, including failure paths.

Committed fixtures are deliberately not raw bridge dumps. The capture tools:

- retain one representative resource per observed type;
- reduce unknown resource bodies to `id` and `type`;
- truncate relationship and scene-action lists;
- replace identifiers consistently;
- generalise names, product data, network identifiers, and timezone;
- rebase absolute timestamps and SSE cursors;
- remove schedules, geolocation-derived fields, and automation configuration.

Regression tests enforce those privacy properties. The original aggregate
resource count above is retained only as a research observation.

## Recording

`huepy.recording` persists the change stream. It holds an ordinary bounded
`changes()` subscriber rather than a queue of its own, which is what makes the
loss contract carry through to disk: a sink that cannot keep up overflows that
subscriber, and the coalesced `Resync(LAGGED)` is written as a row. The archive
therefore states where and how much of itself is missing, in the same terms the
in-memory stream uses.

A sink that raises is isolated. Its batch is dropped, never retried, and the
next batch it accepts is prefixed with a coalesced `Resync(INCONSISTENT)`
carrying `detail["source"] == "sink"`. That reuses the existing reason rather
than adding a fourth member: "continuity could not be proved" is precisely
true, `detail` exists to distinguish origins, and `HueState` itself would never
emit the new member.

Sinks receive enriched, self-contained records and never the state graph, so
`huepy.recording` depends only on `huepy.state.records` and the models. Blocking
sinks own a single-threaded executor each rather than using `asyncio.to_thread`,
whose shared pool would move a `sqlite3` connection between threads and trip
`check_same_thread`.

## Known limits

- `HueState` is last-reported state, not guaranteed physical state. This is
  most visible during fades and when a device is unreachable.
- The aggregate endpoint can omit resource types exposed by their individual
  endpoints.
- Every reconnect is marked uncertain because replay truncation is silent.
- Writes issued through another client or process cannot be self-attributed.
- Event `error` payloads remain tolerated but lack an observed live fixture.
- `relative_rotary` is modelled from the known payload shape but was absent on
  the bridge used for fixture capture.
- Most resource types are now modelled concretely (35 in `RESOURCE_MODELS`); the
  tolerant generic `HueResource` fallback covers only a few niche types -- the
  motion-area services, `speaker`, `clip`, `bell_button`,
  `switch_input_configuration` -- and any genuinely new firmware type, until a
  consumer needs their fields.

## Optional future work

This document is not a roadmap. Future work should start from a concrete
consumer or new bridge observation. Reasonable candidates are:

- promote another generic resource type when its fields have a caller;
- add live evidence for `EventType.ERROR` or `relative_rotary` when suitable
  hardware is available;
- introduce an opt-in strict configuration mode without weakening the current
  tolerant default;
- suppress reconnect markers only if a firmware-independent continuity proof
  becomes available.

## Verification map

- `tests/test_http.py` and `tests/test_sse_frames.py`: transport, retry, SSE,
  cursor, connection, and cleanup semantics.
- `tests/test_state.py`: startup, fold, history, lag, reconnect, topology,
  isolation, write correlation, and fades.
- `tests/test_models.py` and `tests/test_events.py`: typed resource and delta
  shapes.
- `tests/test_real_fixtures.py`: parser coverage, measured durability evidence,
  and fixture privacy.
- `tests/test_recording.py`: sink conformance, batching, loss marking, and the
  documented SQLite queries.
- `tests/test_state_callbacks.py` and `tests/test_state_facade.py`: handler
  dispatch, filters, failure isolation, and the permanent `hue.state` attribute.
- `tests/integration/test_live_state.py`: opt-in end-to-end snapshot, fade
  attribution, replay overflow, reconnect marker, and reconciliation.
- `examples/track_state.py` and `examples/record_history.py`: maintained state
  and persistence usage.
