# huepy: state layer design and roadmap

A design for the next stage of huepy, measured against the consumers being built on it: an
application that controls every Hue device, room and zone on a schedule, and one that records
every observed state change plus explicit uncertainty windows to a database so a model can later
predict what a user wants.

The second requirement is what this document turns on. Controlling lights is a request/response
problem, and huepy already solves it well. Recording behaviour is a *state* problem — a
continuously maintained picture of what the bridge last reported, with every observed change
timestamped and every interval of uncertain continuity marked — and that is work the library
currently leaves to each consumer.

> **Verification status.** Every bridge-behaviour claim in this document was re-tested against the
> live bridge on 2026-08-24 (BSB002, `apiversion` 1.78.0). The `HueState` direction survived, but
> **three bridge facts it rested on were wrong** — most importantly that the bridge ignores
> `Last-Event-ID`, which it does not — and Phase 0 turned up one behaviour nobody had asked about
> that limits what the history application can honestly claim. A subsequent repository/design
> audit also found that frame-count overflow detection was unsafe, `SerializeAsAny` did not restore
> resource subtypes, startup was not atomic, frozen records were only shallowly frozen, and write
> observation lacked a hook. Those corrections are now part of the design. The short version is in
> [Verified facts about the bridge](#verified-facts-about-the-bridge) and
> [What the bridge reports during a fade](#what-the-bridge-reports-during-a-fade).
>
> That fade finding was then **re-tested a second time over 22 runs and partly retracted** — its
> first version came from two runs and mistook the phase of a periodic report for the shape of the
> behaviour. The retraction is written up in place rather than quietly edited out, because the
> failure mode generalises: **anything measured here from fewer than ~10 runs is provisional.**
> The aggregate snapshot and one reversible 60-second fade capture now live in
> `tests/fixtures/`. The broader replay/overflow and scene probe suite remains the unfinished part
> of Phase 0.

## Goal

A library that is powerful and intuitive for building consumers that

1. control any Hue device, room or zone,
2. observe the bridge's aggregate-visible resource graph as one typed, continuously maintained
   **last-reported** state, with unresolved references exposed rather than hidden, and
3. record every observed change, with explicit gap markers wherever loss cannot be ruled out and
   enough context per change to train a model on later.

The measure of "clean" is the consumer code: a history recorder should be a `for` loop, a plan
engine should ask "is the kitchen at target?" without a request, and neither should contain a
reconnect supervisor.

## Where the library stands today

**The command surface is ready. The observe and write-observation paths are not.**

`await room.set(on=True, brightness=30, kelvin=2200, transition=2.0)` as a single PUT is exactly
the primitive a plan engine needs, and name lookup lets plan config say `"Küche"` rather than a
UUID. Its command API stays intact; it only needs the measured transition ceiling and a non-blocking
transport observer so the state layer can correlate writes.

The half the consumers lean on hardest — reading state back and capturing change over time — is
where the work still falls to the caller. Nothing found is architectural damage: binding, envelope
handling, `color.py` and the tolerant models are the right foundations, and every addition below
slots on top of them. The full gap register is at the end of this document, with the evidence for
each entry.

## Shape of the change

One new concept, `HueState`, and a handful of small supporting changes around it.

`HueState` is an opt-in, event-updated view of the aggregate-visible bridge graph: one baseline
snapshot, folded forward from the event stream, reconciled with a snapshot after every reconnect.
It is the right centre of gravity because
it resolves most of the gap register at once rather than patching each entry individually:

- it turns delta events into complete state rows, which is the history application's actual
  requirement;
- it makes "is this room already at target?" free, so the plan engine stops issuing redundant
  writes — **except during a fade this library issued**, where the bridge reports a stale value
  and the check must be suppressed (see [during a fade](#what-the-bridge-reports-during-a-fade));
- it keeps GET traffic off the bridge, which is worth having even though the measured concurrency
  ceiling turned out to be far softer than this document originally assumed;
- it forces reconnect recovery to be solved **once, inside the library** — request replay via
  `Last-Event-ID`, then snapshot-and-diff because this bridge gives no reliable proof that replay
  was complete — instead of in every consumer. That problem has to be solved for honest history
  either way.

The broad snapshot-plus-event-cache shape is also used by `aiohue` and the Home Assistant
integration. aiohue resumes from `Last-Event-ID` and refetches full state after longer disconnects.
That is useful corroboration for the transport mechanism, not evidence for huepy's stricter
history semantics or startup ordering: aiohue maintains current state, whereas huepy's history
consumer must also say when intermediate changes may have been lost.

## What stays exactly as it is

- The pure request/response client: `Hue`, the handlers, name lookup (`await hue.rooms["Kitchen"]`),
  bound models, `set()` as one PUT, `huepy.color`, the envelope handling, tolerant models.
- Nothing becomes cached behind the caller's back. `hue.lights.all()` always goes to the bridge.
  State is an explicit, separate object the consumer opts into. (aiohue's transparent caching makes
  "is this live?" unanswerable at the call site; that is the one thing not to copy.)

## Verified facts about the bridge

Measured against the live bridge on 2026-08-24 — BSB002, `swversion` 1978074000, `apiversion`
1.78.0, 186 resources across 27 types. **Three rows of the previous version of this table were
wrong**, and one of them was load-bearing for the whole reconnect design; they are marked
*(was wrong)* below.

| Fact | Consequence | How it was established |
| --- | --- | --- |
| The bridge **honours `Last-Event-ID`** and replays retained events verbatim *(was wrong: the table said it ignores the header)* | Replay is the first recovery input, but silent buffer truncation means every reconnect still gets a possible-gap marker and snapshot reconciliation | Disconnected, made three changes, reconnected with the header: all three replayed at t≈0. Control run without the header: nothing replayed |
| The bridge emits **`id:` lines** of the form `<unix_epoch>:<seq>` | A monotonic ordering key, strictly better than `creationtime`. huepy discards these lines today — `client/http.py:477` keeps only `data:` | Raw SSE capture |
| The replay buffer holds roughly **15 SSE frames**, evicted by count rather than age | Resume covers a brief reconnect, not a long outage. 40 writes overflowed it; the last 15 frames survived | Synthesised ids from `now-60s` through `epoch 0` all saturate at the same window |
| **Buffer overflow is silent**: an id older than the buffer returns whatever is left, with a 200 and no error | Loss cannot be detected reliably from the response. Neither the first returned id nor a frame count proves continuity without a documented id-successor rule and a stable, known buffer depth. Reconnects therefore remain possible gaps and require snapshot reconciliation | `Last-Event-ID: 0:0` returned 200 and the buffer contents. A malformed id and a future id both replay nothing |
| **No periodic application keepalive.** One `: hi` comment at connect, then nothing | A half-open connection is invisible to the SSE reader. A short `sock_read` timeout is not a health check — it also expires on a healthy quiet bridge. Use TCP keepalive probes and remove the one-hour total connection lifetime | 355 s captured with one comment line and 90.1 s of unbroken silence. aiohue works around this by generating a bridge event every 60 s; huepy should not mutate bridge configuration merely to test a socket |
| `creationtime` is RFC 3339 with `Z` but has **1-second resolution** | Aware, so `AwareDatetime` parses and round-trips it exactly. It cannot order events *within* a second — several distinct changes shared one value in every capture | Captured `2026-08-24T11:56:17Z` |
| One event may carry **several entries for the same resource id** | The fold must merge every entry for an id before emitting one change, or a single command becomes several rows each holding a partial state | One PUT of `{on, dimming}` arrived as two entries under one event id; a colour PUT arrived as three |
| A `grouped_light` aggregate arrives **~1 s after** its member lights, carrying the same `creationtime` | Room-level and light-level state are briefly inconsistent. A room aggregate is not authoritative within a second of a light change | Every write in the capture, without exception |
| **There is no 3-concurrent-request limit.** 429 tracks response *size*, not request count *(was wrong: the table said the bridge rejects more than 3 concurrent requests)* | 40 concurrent `GET /resource/light` all returned 200. Ten concurrent full snapshots produced one 429; twenty produced seven | Bursts at 3, 4, 6, 10, 20 and 40 |
| `limit_per_host=3` is **2.7× faster**, not merely safer | 40 requests took 4.94 s uncapped and 1.85 s capped: connection reuse beats a TLS handshake storm. This, not a mythical 429 threshold, is the argument for the cap | The same burst with `limit_per_host=3` |
| A 429 body is an **nginx HTML error page**, and no `Retry-After` is ever sent | Never parse it as an envelope, and drop the "honouring `Retry-After`" clause from E. huepy already does the right thing at `client/http.py:224` | Captured 429 response |
| `dynamics.duration` maxes at **exactly 6 000 000 ms (100 minutes)** | **A 30-minute sunrise is one PUT.** Stepped writes are unnecessary and E drops sharply in priority | Bisected: 6 000 000 accepted, 6 000 001 rejected as `207 invalid attribute value (.dynamics.duration)`. A negative value is a `400` json-schema error, a different path from `models/state.py:171` |
| Durations off the 100 ms grid are **not** rejected | 50 ms and 150 ms were both accepted; the step is internal rounding, not a constraint | Same test |
| Rapid commands are **coalesced, never rejected** *(was wrong in emphasis: the guidance is real, but nothing is dropped silently in a way that matters)* | ~13 req/s is the self-limiting ceiling, set by response latency. Intermediate values are discarded but the **settled state always matched the last command**. Pacing stays a consumer concern | 40 writes at 2, 5, 10 and 25 Hz and unthrottled: every response 200, no 429, settled value correct every time |
| `GET /clip/v2/resource` returns **almost** every resource — it omits `motion_area_candidate` (10 of them) *(was wrong: the first check was circular, enumerating types **from** the aggregate and then verifying each, so a type missing from the aggregate could not be detected)* | The snapshot is still one request (111 KB, 186 resources, 27 types), but it is **not a complete picture**. Topology must tolerate a `services` rid that resolves to nothing, and the fold's unknown-id branch can fire for a resource the baseline never held | Re-probed 36 known v2 type names independently: every other type matched the aggregate exactly; `motion_area_candidate` returned 10 from its own endpoint and 0 from the aggregate |
| Folding real deltas onto a real snapshot **reproduces a fresh GET exactly** | The `deep_merge`-and-revalidate design is correct as specified, `mirek_schema` included | 14 deltas folded over 186 resources: zero mismatched sections |
| **10 of 145 topology references dangle** — every device advertising a `motion_area_candidate` service points at a resource the snapshot omits | `state.lights_in()`, `room_of()` and `device_of()` must skip unresolvable rids rather than raise or assume. `owner` rids, by contrast, all resolved | Same snapshot; zero dangling owners |

### What the bridge reports during a fade

Established over **22 runs across 5 lights and 3 bulb models** (LTG002, LTO001, LTA005), fade
durations 20 / 60 / 120 / 300 s, plus no-duration and idle controls — 930 GET polls and 177 light
events. The first draft of this section was written from two runs and **got the shape wrong**; the
corrected finding is below, with the retraction recorded at the end because the mistake is
instructive.

**Native fades are not observable in real time.** A PUT carrying `dynamics.duration` produces
exactly one immediate event, at **+0.10 s** (19/19 runs, sd 0.01 s), reporting the *commanded
target* — not the current level. The bridge's cached value is wrong by the full remaining fade span
until the bulb's next attribute report.

The bulb reports its true level on a **free-running ~24.5 s clock**, measured at 24.27–25.01 s per
light. The PUT does **not** reset that clock: time from PUT to first true report was uniform on
roughly [0, 24.5] s (observed 3.44–33.54 s, n=17). So the number of truthful samples inside a fade
is `duration / 24.5`, rounded unpredictably:

| Fade | Runs | Truthful in-fade samples | Median inter-report gap |
| --- | --- | --- | --- |
| 20 s | 7 | 0–1 | 24.37 s |
| 60 s | 7 | 1–3 | 24.55 s |
| 120 s | 4 | 4–5 | 24.95 s |
| 300 s | 1 | 11 | 24.94 s |

The gap is **flat across a 15× range of fade duration**, which is what proves it is a periodic
report rather than fade progress. Occasional dropped slots give gaps of 41–51 s (≈2 periods), and
the clock keeps running across a light being switched off and on.

**When a sample does arrive, its value is accurate.** All 48 in-fade samples matched a linear ramp
to within 0.9 brightness points (mean residual −0.18, sd 0.23); colour temperature tracks equally
well. The correct final value arrives at the first tick at or after fade end: **1.95–24.10 s late,
median 15.12 s** — bounded above by one report period.

**A GET gives nothing the event stream does not.** 930 polls produced zero real disagreements; the
five that looked like disagreements were polls landing 0.13–0.9 s after the matching event. Both
read the same cache.

**Baselines the first draft lacked.** A brightness change *without* `dynamics.duration` produces
exactly one event and stays correct — no regression, no intermediate samples. A light left untouched
produces no events at all (120 s idle, 42 polls, zero events).

Three consequences:

- **`HueState` cannot be "continuously correct" during a fade, and the document must stop claiming
  it is.** At any moment inside a fade the cached level is stale by up to one report period, and the
  state layer's contract is *last reported state* — a weaker and truthful claim. Unreliability
  extends past the commanded end by up to ~25 s.
- **The corruption in a history row comes from the target echo, not from the samples.** This is the
  useful correction. The periodic reports are truthful and interpolatable; it is the immediate
  post-PUT echo — the one event that is *not* a reading — that inserts a value the light never held
  and makes the trace non-monotonic. A recorder that flags that echo as commanded-not-observed keeps
  an accurate, if sparse, trace. Doing so requires knowing the write happened, which is why **write
  correlation stays a prerequisite for correct history** and moves into Phase 2b. Fades started
  outside the library (the Hue app's sunrise, a scene with dynamics) cannot be distinguished this
  way, and those windows have to be marked unattributable.
- **"Is this room already at target?" is unreliable during a fade.** The cached level trails the
  real one, so a plan engine that trusts it may re-issue the write — the opposite of the benefit
  claimed for `HueState` above. The check must be suppressed for any fade this library issued, plus
  one report period.

**What the first draft got wrong, and why.** It reported "target, then a regression to a near-start
value at ~5 s, then nothing at all until the end". Two of those three are wrong. The second value
is not "near the start" — it is the true level at that instant, and it looked like a start value
only because both original runs happened to sample 3.8–4.7 s into a fade, where the true level *is*
still low. In one run the apparent regression was 100.0 → 99.21. And "nothing until the end" holds
only for short fades: a 300 s fade delivered 11 intermediate samples. Two observations of a process
whose phase is uniform over a 24.5 s window could not have distinguished any of this — across three
*identical* 60 s fades on one light, the first true report landed at 26.78 / 33.54 / 11.09 s and the
final-value lag was 22.18 / 15.12 / 1.95 s. **Any Phase 0 finding drawn from fewer than ~10 runs
should be treated as provisional.**

Two operational notes for the Phase 0 suite: **do not probe the Bad lights** — a bridge-side room
automation switched both off mid-run and corrupted a sample. And two attributes changed in one PUT
arrive in the same report tick, as two entries in one SSE `data` array, corroborating the
multiple-entries-per-resource behaviour in the facts table.

---

## The model layer: pydantic v2 throughout

Bridge payloads are already pydantic v2 models, and that part is sound: `HueModel` with
`extra="allow"` is what keeps a firmware update from breaking parsing. What this design adds is a
second job for those models — **being the record written to the database** — and that job is
served by v2 features the library does not yet use. Everything below is measured against
pydantic 2.13.4, the version locked in `uv.lock` (the package metadata permits later v2 releases).

### 1. Use the discriminated resource union in persisted records

This is the one that would silently corrupt the history database, so it comes first.

Pydantic v2 serializes a field **by its annotation, not its runtime type**. `Change.before` typed
as `HueResource` therefore dumps the base class's four fields and discards everything the
consumer actually wants to record. `SerializeAsAny` fixes that half of the problem, but not the
other half: validation of the stored row still reconstructs a plain `HueResource`, not the
original `Light`. Measured on a real `models.Light`:

```
before: HueResource            -> {'id', 'type', 'id_v1', 'owner',
                                   # plus whatever landed in model_extra, which under
                                   # extra="allow" is dumped even under the base annotation:
                                   'product_data', 'identify', 'service_id', 'dynamics',
                                   'dimming_delta', 'color_temperature_delta', 'effects_v2'}
before: SerializeAsAny[HueResource]
                               -> the same, plus every declared subclass field:
                                  'metadata', 'on', 'dimming', 'color', 'color_temperature',
                                  'mode', 'effects', 'timed_effects', 'gradient', 'powerup',
                                  'alert_actions', 'signaling'
round-trip through Change.model_validate
                               -> HueResource, with those sections demoted to model_extra
```

Re-measured on a real `models.Light` under pydantic 2.13.4, and the truth is nastier than the
first draft of this section suggested. The base annotation does **not** produce a visibly stunted
four-key row: `extra="allow"` means the *unmodelled* sections still come through, so the row looks
populated. What it silently drops is exactly the **modelled** state — `on`, `dimming`, `color`,
`color_temperature`, `metadata`. A row carrying `product_data` and `dynamics` but no brightness
reads like a valid record, which makes the bug far harder to notice than a missing row would be.

No error, no warning — `db.write(change.before.model_dump())` would just quietly store rows with
the brightness missing. The correct persisted annotation is therefore the discriminated
`AnyResource` union described below, not `SerializeAsAny[HueResource]`. It preserves the subclass
on both dump and validation. `hue.snapshot()` likewise returns `list[AnyResource]`; a plain Python
return annotation has no serialization behaviour of its own, and `RESOURCE_LIST` is the adapter
used when a whole snapshot is dumped or restored.

### 2. The registry as a discriminated union

`parse_resource` is a dispatch on `type` with a fallback, which is exactly what a **callable
discriminator** expresses. Rather than a hand-written `if/elif` or a dict lookup plus a manual
`model_validate`, the union is declared once and pydantic-core does the dispatch in Rust:

```python
UNKNOWN_TAG = "_unknown"


def _resource_tag(value: Any) -> str:
    """Tag a payload by its `type`, falling back for anything unmodelled."""
    raw = value.get("type") if isinstance(value, dict) else getattr(value, "type", "")
    return raw if raw in RESOURCE_MODELS else UNKNOWN_TAG


AnyResource = Annotated[
    Annotated[Light, Tag("light")]
    | Annotated[Room, Tag("room")]
    # ... one explicit arm per modelled resource type
    | Annotated[HueResource, Tag(UNKNOWN_TAG)],
    Discriminator(_resource_tag),
]

RESOURCE_LIST = TypeAdapter(list[AnyResource])  # module-level, reused
```

`AnyResource` is declared explicitly so basedpyright can expose a useful public union type.
`RESOURCE_MODELS` is an immutable mapping containing the same arms, and a test asserts that its
tags and models exactly match the union; Python's static type syntax cannot generate a union from
a runtime dictionary without losing that useful typing. The discriminator reads `type` off
**both dicts and model instances**, which pydantic requires because the same callable runs on
serialization. Verified: a payload of type
`entertainment_configuration` validates to a generic `HueResource` with its unknown fields intact
in `model_extra`, alongside a `Light` and a `Room` in the same list.

One `TypeAdapter` is built at import and reused for every snapshot and every full-resource
validation after a fold; constructing one per call is the documented performance mistake.

### 3. `Change` and `Resync` are pydantic models, not stdlib dataclasses

They are the database row. As pydantic models they serialize with no bespoke encoder:
`model_dump(mode="json")` renders the datetimes as ISO strings and the nested resources as plain
dicts, `model_dump_json()` goes straight into a text column, and `model_validate` reads a row back
for replay and for tests **with the resource subtype intact**, because `before` and `after` use
`AnyResource`. A stdlib dataclass needs a hand-written converter for each of those.

`model_config = ConfigDict(frozen=True)` prevents reassignment of record fields, but pydantic
freezing is shallow: nested resource models and `delta` remain mutable. State therefore constructs
detached record resources from raw data and gives each subscriber its own deep copy. A subscriber
can mutate its own row, but cannot mutate the state's canonical data or another subscriber's row.

This does not contradict the project convention of preferring dataclasses for **configuration** —
`HueConfig` has no serialization requirement and stays a dataclass (see G below). These records
are serialized on every single row, so a model earns its place.

### 4. `computed_field` for the read side

The read-side symmetry work (C) is a natural fit for `@computed_field`, which includes a property
in the serialized output:

```python
class Light(LightCommands, NamedResource):
    @computed_field
    @property
    def kelvin(self) -> int | None:
        """Colour temperature in kelvin, or None if unsupported or invalid."""
        temperature = self.color_temperature
        if temperature is None or not temperature.mirek_valid:
            return None
        return (
            mirek_to_kelvin(temperature.mirek)
            if temperature.mirek is not None
            else None
        )
```

`light.model_dump()` then carries `'kelvin': 3333` next to `'id'`, so a history row records human
units without the recorder converting anything, and a feature column stops re-implementing
`mirek_to_kelvin`. The same applies to `Light.rgb` and `LightLevel.lux`.

Two consequences to accept deliberately:

- It changes the shape of `model_dump()` for existing callers. `API_REFERENCE.md` documents the
  new keys and `tests/test_docs.py` enforces it.
- A computed field is serialization-only: it is not accepted back on validation, and under
  `extra="allow"` a round-tripped dump would stash `kelvin` in `model_extra`. The fold is
  unaffected because it merges **raw bridge payloads and never `model_dump()` output** — which is
  independently the right choice, and this is one more reason for it.

### 5. `AwareDatetime` for every timestamp, with provenance kept

`HueEvent.creationtime`, every sensor report's `changed`/`updated`, and the times on `Change` and
`Resync` are annotated `AwareDatetime`, so a naive datetime is rejected at the boundary rather
than raising a `TypeError` later. `Change.event_at` retains the event's second-resolution bridge
time; `Change.observed_at` retains a sensor report's millisecond-resolution time when one exists;
`Change.received_at` is the local clock. `Change.at` is the effective feature timestamp:
`observed_at or event_at or received_at`. Pydantic parses the bridge's ISO string itself and
`model_dump(mode="json")` gives it back.

### 6. Validation cost is not a concern — measured

The fold revalidates a whole model per event, which invites a premature optimisation. Measured on
a fully-populated `models.Light` (colour, gamut, mirek schema, effects), 20 000 iterations:

| Operation | Per op | Throughput |
| --- | --- | --- |
| `deep_merge` alone | 0.32 µs | 3.1 M/s |
| `model_validate` alone | 8.5 µs | 118 k/s |
| Full fold (merge + validate) | 9.4 µs | 106 k/s |
| Fold + `model_dump(mode="json")` | 19.8 µs | 51 k/s |

Re-measured 2026-08-24 on the most fully-populated `models.Light` this bridge actually reports
(17 sections), which is why these are a little slower than the first draft's figures. The
conclusion is unchanged.

A busy bridge produces events in the tens per second. The fold is roughly four orders of magnitude
clear of the load, so it stays a plain `model_validate`: no `model_construct`, no caching, no
incremental validation. If a future profile disagrees, the note in the code should point back at
these numbers rather than at intuition.

### What stays out of pydantic

`huepy.color` keeps its plain tuples and its `Gamut(NamedTuple)`. It is deliberately free of any
dependency on the model layer — that is what lets a caller convert colours without a bridge
anywhere in sight — and `models/light.py:_as_gamut` is the one seam where the two meet. Converting
it would buy validation nobody asked for and cost the module its independence.

---

## Design: `HueState`

### Surface

```python
from huepy import Hue, models
from huepy.state import Change, ChangeKind, Resync

async with Hue() as hue, hue.state() as state:
    # Reads are local and synchronous: the bridge as of the last event.
    kitchen = state.rooms["Kitchen"]  # models.Room, bound -> .set() still works
    desk = state.lights.get(light_id)  # by id, or None
    for light in state.lights_in(kitchen):  # topology as a view, no request
        print(light.name, light.is_on, light.kelvin)
    room = state.room_of(light.id)  # -> models.Room | None
    motions = state.all(models.Motion)  # every resource of one type, typed

    # Every change, as full before/after models, to as many consumers as you like.
    async for item in state.changes():
        match item:
            case Change(kind=ChangeKind.UPDATE, before=b, after=a):
                db.write(item.at, a.type, a.id, state.name_of(a.id), item.delta)
            case Resync(gap_started=t0, gap_ended=t1):
                db.mark_gap(t0, t1)  # the model must not learn from this window
```

- `hue.state()` is a factory returning a `HueState` — composition over the `HueClient` protocol,
  testable against a fake client and transport. `HueState(hue)` works too.
- Entering the context starts an event-reader task, waits for an explicit **connection-ready
  handshake**, and keeps draining complete SSE frames into a private buffer while taking the
  snapshot. It enqueues a local barrier when the response completes, installs the baseline, folds
  pre-barrier frames in SSE order without publishing startup history, then switches to live folding.
  Merely constructing an async generator would not connect because async generators are lazy.
  There is an unavoidable overlap: a buffered event may already be represented by the snapshot,
  whose response has no cursor or timestamp. Startup emits no history rows before `__aenter__`
  returns; it suppresses no-op folds and documents the remaining possibility of a briefly stale
  field rather than claiming an atomic baseline the bridge cannot provide. Exiting cancels and
  awaits the reader task.
- Views: `state.lights`, `state.rooms`, `state.zones`, `state.scenes`, `state.devices` carry the
  same vocabulary as the handlers — `[name]`, `by_name()`, `get(id)`, `all()`, `names()` — but
  **synchronous**, because they are local. The await/no-await asymmetry with `hue.rooms[...]` is
  deliberate: it tells the reader which one costs a round trip. For every other type:
  `state.all(models.Motion)`, `state.get(id)`, `state.resources` (everything the aggregate snapshot
  returned, for an almost-full-state row).
- Topology: `state.lights_in(room_or_zone)` (hides the rooms-are-devices / zones-are-services
  asymmetry — confirmed live: all 7 rooms have `device` children only, and the one zone has `light`
  children only), `state.room_of(id)`, `state.zones_of(id)`, `state.device_of(id)`,
  `state.name_of(id)`. **These must skip rids that resolve to nothing**: 10 of 145 references in
  the live snapshot dangle, because devices advertise a `motion_area_candidate` service the
  snapshot endpoint does not return.
  These supersede the hand-rolled `members()` in `examples/control_room.py:21`,
  `Room.get_from_light_service_id` (two requests for what is a dict lookup), and `refresh_names()`'s
  five GETs while a state is running. `hue.get_name()` is left alone; the two are documented as
  separate.
- Models handed out are freshly validated, bound copies of the state's private raw dictionaries,
  so `await state.rooms["Kitchen"].set(...)` writes to the bridge without allowing caller mutation
  to corrupt canonical state. Validation costs ~10 µs per fully populated light, so isolation is
  worth more than caching public model instances.
  The state is **not** updated optimistically: it changes when the bridge's event comes back. That
  is the honest answer, and it is what makes the state usable as ground truth for a history DB.
- The contract is **last reported state**, not live state. During a transition the bridge samples
  the light only on a free-running ~24.5 s clock, so the cached value is stale by up to a full
  period and no client-side design can do better. `state.fading` exposes the transitions this
  library started, with their commanded end time, so a consumer can tell "the light is at 12%" from
  "the light is on its way to 100% and the next true sample is up to 25 s away".

### Internals

**Placement.** New package `huepy/state/`: `HueState` and the lifecycle, the views, the fold, and
the `Change`/`Resync` records, re-exported from `huepy.state`. It imports `huepy.models`,
`huepy.utils.naming` and `huepy.client.protocol` only; `client/base.py` imports it for the
`Hue.state()` factory. That keeps the import graph acyclic in the same way `resources` already is.

**Type registry.** `models.RESOURCE_MODELS: Mapping[str, type[HueResource]]` maps every `type` the
library models to its class, and the `AnyResource` discriminated union built from it (see
[The model layer](#the-model-layer-pydantic-v2-throughout)) does the dispatch, falling back to a
generic `HueResource` for anything unmodelled — entertainment, behaviour scripts, future types.
`models.parse_resource(payload)` and `models.RESOURCE_LIST` are the two entry points. The registry
lives in `huepy.models`, not in the handlers, so it stays acyclic; handlers keep declaring
`model =` as today, so an invariant test must assert that every handler/type pair agrees with the
registry. The fact is necessarily represented twice to preserve the current import graph.

**Snapshot.** `await hue.snapshot() -> list[AnyResource]`: one `GET /clip/v2/resource`, with the
existing `{errors, data}` envelope checked before `data` is parsed through `RESOURCE_LIST`; every
item is bound. Public on its own — it is a periodic almost-full-state row for the DB and a
debugging tool — and it is `HueState`'s baseline and reconnect reconciliation input. Dump or
restore a complete snapshot through `models.RESOURCE_LIST` so runtime subtypes survive.

It is **not quite complete**: `motion_area_candidate` resources exist and are referenced from
device `services`, but the aggregate endpoint omits them (see the facts table). The baseline is
therefore allowed to have holes, which is why topology lookups skip unresolvable rids and why the
fold needs its unknown-id branch. If a consumer needs those resources, they are one extra
per-type GET; the design does not chase them by default.

**Fold.** The state keeps, per resource id, the **raw dict** as last merged and an internal,
detached **validated model** built from it. Public views revalidate a copy and bind that copy;
`Change` records receive detached copies. The raw dictionaries remain the only canonical state.

```
per event -> group its data[] entries by resource id, deep_merge them in
             arrival order into one delta per id, then fold that once
update  -> raw = deep_merge(raw[id], delta)        # dicts merge recursively; lists and
           after = parse_resource(raw)             # scalars are replaced wholesale
           emit Change(UPDATE, detached before/after, delta)
add     -> raw[id] = payload; emit Change(ADD, None, after, payload)
delete  -> pop;               emit Change(DELETE, before, None, {})
update for an id the state does not hold -> fetch that one resource, treat as ADD;
                                       if it vanished meanwhile, emit Resync(INCONSISTENT)
error or unknown event type -> preserve/log the raw event and emit Resync(INCONSISTENT);
                               do not guess a state mutation
```

The per-event grouping is not a refinement, it is required. Measured: one PUT of
`{"on": ..., "dimming": ...}` comes back as **two entries for the same id inside one event**, and
a colour PUT as three. Folding each entry separately would emit two `Change` rows for one command,
the first carrying `on=true` beside a stale brightness — a state the light never occupied. Group
first, emit once.

The whole mechanism was validated against the live bridge: 14 real deltas folded onto a real
186-resource snapshot reproduced a fresh `GET /clip/v2/resource` with **zero mismatched sections**,
and `mirek_schema` survived intact.

Deep-merging the raw dict and re-validating is the detail that makes this correct. A
`color_temperature` delta carries `mirek` and `mirek_valid` but not `mirek_schema`, and
`model_copy(update=...)` does not merge — it **assigns**, and it does not validate. Measured: after
one such update, `light.color_temperature` is no longer a `ColorTemperature` at all but the bare
dict `{'mirek': 250, 'mirek_valid': True}`, so the schema is gone and `.mirek` attribute access
raises. The merge-and-revalidate path keeps `mirek_schema` intact and the field a real model.
Lists are replaced rather than merged, because the bridge sends lists whole (`gradient.points`,
`children`, `services`).

Every fold and every public lookup produces a **new** model instance. That matches the library's
existing rule — a model is a snapshot, and `refresh()` returns a new one — so a consumer holding
an older reference keeps an older snapshot rather than having it mutate underneath them. It also
prevents a caller from mutating the model stored inside `HueState`.

**Change records.**

```python
class ChangeKind(StrEnum):
    UPDATE = "update"
    ADD = "add"
    DELETE = "delete"


class Change(BaseModel):
    """One resource's state transition. This is the database row."""

    model_config = ConfigDict(frozen=True)

    kind: ChangeKind
    # A sensor report's changed/updated timestamp, when present. Millisecond
    # resolution on the bridge clock.
    observed_at: AwareDatetime | None = None
    # The event's creationtime. One-second resolution on the bridge clock.
    event_at: AwareDatetime | None = None
    # Local wall clock when the event arrived.
    received_at: AwareDatetime
    # The SSE `id:` line, `<unix_epoch>:<seq>`. Monotonic, so it is the real
    # ordering key, and it is what a reconnect resumes from.
    event_id: str | None = None
    resource_id: str
    resource_type: str
    # The discriminated union preserves the concrete subtype on both dump and load.
    # These resources are detached and subscriber-local: records are data, not
    # command handles, and one subscriber cannot mutate another's row.
    before: AnyResource | None
    after: AnyResource | None
    # The raw event sections, exactly as sent -- the DB row payload.
    delta: dict[str, Any]
    # True when derived from a post-gap diff: `at` is then the reconnect time,
    # and the change itself happened somewhere inside the gap.
    resynced: bool = False

    @computed_field
    @property
    def at(self) -> datetime:
        """Best feature time: observed_at, then event_at, then received_at."""
        return self.observed_at or self.event_at or self.received_at


class ResyncReason(StrEnum):
    # Last-Event-ID replay was requested, but completeness cannot be proved.
    RECONNECT = "reconnect"
    LAGGED = "lagged"
    INCONSISTENT = "inconsistent"


class Resync(BaseModel):
    """A window in which continuity was lost or could not be proved."""

    model_config = ConfigDict(frozen=True)

    reason: ResyncReason
    gap_started: AwareDatetime
    gap_ended: AwareDatetime
    # LAGGED only: changes this subscriber did not receive.
    dropped: int = 0
    # INCONSISTENT only: the raw event/error that could not be folded.
    detail: dict[str, Any] | None = None
```

For `RECONNECT`, `gap_started` is conservative: the last frame-receipt time on the old connection,
or its open time if it carried no frame; `gap_ended` is completion of the reconciliation snapshot.
That may mask a long genuinely idle period, but a half-open socket provides no later trustworthy
boundary. For `LAGGED`, the window spans the first dropped item through insertion of the coalesced
marker. `INCONSISTENT` covers an event that cannot be reconciled to any fetchable resource and uses
the event receipt time for both bounds.

`changes()` yields `Change | Resync`. The SSE `event_id` is the ordering key; timestamps anchor the
row to wall time but cannot replace it. `at` deliberately selects the best measurement timestamp,
while `observed_at`, `event_at` and `received_at` retain provenance instead of pretending the
bridge and host clocks are one clock. The `delta` is kept raw so a row can be written without the
consumer re-deriving "what changed" from two full models. Both records being models means
`change.model_dump(mode="json")` is already a storable row and `Change.model_validate(row)` reads
it back with the concrete resource type intact. Tests assert both directions, not only the dump.

**Reconnect: request replay, but reconcile and mark every reconnect.** The bridge honours
`Last-Event-ID`, so replay is valuable: it usually restores the actual intermediate events instead
of reducing an outage to one net diff. It does **not** prove that nothing was evicted. The measured
buffer is undocumented, overflow is silent, ids have no verified successor rule, and the same
connection flows directly from replay into live events with no boundary marker. A hard-coded
15-frame test would be unsafe: if firmware shrank the buffer to 10, ten returned frames would be
mistaken for a complete replay.

The conservative algorithm is therefore:

1. The transport records the id from every complete SSE frame (`<unix_epoch>:<seq>`) and the local
   time the frame was received.
2. On reconnect it sends `Last-Event-ID: <last seen>` and immediately resumes draining frames.
3. `HueState` requests a fresh snapshot while the reader continues buffering and enqueues a local
   barrier immediately when that response completes. On a historical copy of the pre-gap raw
   state, it folds frames before the barrier and emits their replayed `Change` rows with original
   bridge timestamps.
4. It emits `Resync(RECONNECT, gap_started, gap_ended)` on **every** reconnect, because continuity
   cannot be proved, then diffs the snapshot against that historical folded copy and emits one
   `Change(resynced=True)` per remaining difference.
5. For canonical current state it installs the snapshot, then silently reapplies the same buffered
   deltas in SSE order before admitting post-barrier frames. Reapplication does not emit duplicate
   history rows; it favours an observed event that may have happened after the snapshot was
   materialised, at the accepted risk described below.

This deliberately over-marks some lossless reconnects. That is the safe asymmetry for training
data: a false gap costs samples, while an unmarked real gap teaches from events that were never
observed. A future firmware-independent continuity proof may suppress the marker, but measured
buffer depth alone may not.

**Transport event types.** `_read_event_stream()` yields `SSEFrame`, preserving wire-frame
boundaries instead of flattening immediately:

```python
@dataclass(frozen=True)
class SSEFrame:
    event_id: str | None
    received_at: datetime
    events: list[dict[str, Any]]


@dataclass(frozen=True)
class EventConnection:
    opened_at: datetime
    resumed_from: str | None
    frames: AsyncIterator[SSEFrame]
```

`event_connections()` yields one `EventConnection` after its HTTP response is open, with backoff
between connections. The inner iterator ending or raising is the explicit disconnect boundary.
`subscribe_event_frames()` flattens connections to frames for state-aware callers;
`subscribe_events()` remains the compatibility flattening to raw event dictionaries. Parsed
`HueEvent` gains an excluded `sse_id` field/property so `get_event_stream()` users can order rows
without confusing the SSE cursor with the JSON event's UUID `id`.

`HueState` uses `max_retries=None`; cancellation is its exit. Finite retry counts count failures to
**establish** a connection and raise `BridgeConnectionError` on give-up. A successful HTTP open
resets the counter. `state.connected` is true only after startup/reconciliation is complete and an
SSE connection is active; transport-open-but-reconciling remains false.

**Detecting a dead connection.** The bridge sends `: hi` once at connect and **no application
keepalive after that** — 90 s of unbroken silence was observed on an idle stream. A short
`sock_read` timeout would deliberately tear down a healthy quiet stream; combined with honest
reconnect markers, that would manufacture possible gaps on a timer. Instead the connector enables
OS TCP keepalive with conditional platform options (idle 60 s, 10 s interval, 3 failed probes where
supported), and the SSE request uses `ClientTimeout(total=None, sock_read=None)`. Unit tests assert
the socket options through the connector's socket factory. Platforms that cannot tune keepalive use
their OS defaults and log that limitation. This detects a half-open TCP connection without
requiring Hue events or mutating the bridge as a synthetic keepalive would.

Known, bounded inconsistency: during startup or reconnect reconciliation, an event buffered while
the snapshot is in flight may already be represented by that snapshot and briefly regress one
field when folded afterwards. Conversely, discarding it could lose an event that occurred after
the snapshot was materialised. The response carries no cursor or timestamp that can resolve the
ordering, so huepy chooses not to discard observed events, suppresses folds that are true no-ops,
marks reconnect windows, and documents the possible brief regression. A follow-up event or the
next reconciliation repairs it.

**Fan-out and backpressure.** Each `changes()` call gets its own bounded deque plus condition
(`maxsize`, default 4096) and subscriber-local record instances. On overflow, the oldest entries
are dropped and one coalesced `Resync(LAGGED, dropped=n)` is kept in that deque; further drops
replace that frozen marker with a new one carrying the accumulated count until the consumer catches
up. A small private buffer type makes this explicit instead of reaching into `asyncio.Queue`
internals. The newest state transitions win over old queued ones. A
slow DB writer cannot stall the state or the plan engine. This is also what fixes the single-slot
`_event_stream` (`client/base.py:107`, `:304`): the state owns one stream and fans out,
`get_event_stream()` keeps working for direct use, and `close()` finalises a snapshot of every
stream it opened rather than only the most recent.

**Names.** `state.name_of(id)` filters the mixed snapshot to `NamedResource` before passing it to
`build_name_map`, recomputing when a named resource is added, deleted or renamed. The current
function assumes every input item has `.metadata`, so passing the unfiltered snapshot would raise.
Filtering preserves one name-building algorithm.

**Errors.** A failed reconciliation snapshot retries inside the reconnect loop with the same
backoff. If the stream task dies for any other reason, a terminal carrying that exception is
inserted into every subscriber buffer (evicting one item if necessary), so every open `changes()`
iterator raises it; the views keep serving last-known state and `state.connected` is False. The raw
`subscribe_events()` with a
finite `max_retries` **raises** `BridgeConnectionError` on give-up instead of returning, so the
silent stop at `client/http.py:434` goes away for everyone.

### Write correlation (no longer optional)

The `Hue` instance that owns `HueState` can see writes sent through its transport; it cannot see
writes from another process, another `Hue` instance, the Hue app, or an automation. The honest
classification is therefore `origin="self" | "unattributed"`, never `external` merely because no
local match was found.

Correlation needs an explicit transport hook, not inference inside `HueState`. `HueHttpClient`
publishes a subscriber-local `PendingWrite` before each PUT is sent and completes it with the
response or error afterwards:

```python
class PendingWrite(BaseModel):
    command_id: UUID
    path: str
    payload: dict[str, Any]
    sent_at: AwareDatetime
    completed_at: AwareDatetime | None = None
    status: Literal["pending", "accepted", "rejected", "unknown"] = "pending"
```

`HueState` registers and unregisters one observer for its lifetime. This catches bound-model,
handler, and direct `hue.http.put()` writes through the same transport; writes through a different
client remain unattributed. The pre-send notification matters because the event can race the HTTP
response. Observer callbacks must not delay the request and receive their own payload copy. An
explicit bridge rejection marks the command
`rejected` and removes it; a transport failure after send is `unknown`, because the bridge may have
applied a request whose response was lost.

A successful light or grouped-light PUT becomes one or more expected deltas. Group membership is
captured **at command time**, and bridge rounding/clamping is applied using the same payload builder
before matching. A grouped write can fan out to the individual lights — confirmed live — while a
direct light write has one expected target. Overlapping candidates match newest compatible command
first and retain `command_id` so ambiguity is inspectable rather than hidden.

**Phase 0 promoted this from a nice-to-have to a correctness requirement**, though for a narrower
reason than the first draft of that section claimed. The bulb's periodic samples during a fade are
*truthful*; what is not truthful is the immediate post-PUT echo of the commanded target, which
inserts a value the light never held. Distinguishing "the bridge told me 100 because I asked for
100" from "the bridge told me 100 because the light is at 100" requires knowing the write happened
and what duration it carried. The library retains the transition it observed, its commanded target
and end time, stamps matching `Change` rows, and classifies the immediate target echo separately
from later reported samples. `Change` therefore gains `origin`, `command_id`,
`transition_ends_at`, `command_confirmed`, and
`observation="reported" | "command_echo"`. Canonical state updates immediately, but fan-out of a
matched row may wait briefly for the write response so `command_confirmed` is accurate; an event
matched to an `unknown` command is still local in origin but unconfirmed. This is the same bookkeeping
`state.fading` needs, so it is one mechanism, not two. A fading entry remains active through the
commanded end plus one measured report period (25 s by default, named as a constant).

It remains a heuristic at the edges: a fade started by the Hue app, an automation, a different
client, or a scene with dynamics is indistinguishable from an ordinary report unless its event
shape carries independently verified dynamics metadata. Those rows stay `unattributed`; they are
not labelled user input. Scene recall correlation remains out of scope until its real event shape
is captured. Button events are the unambiguous user-input cases.

### Testing

- Unit: a fake transport scripted as a list of connections, each a list of `SSEFrame` objects or an
  exception, plus canned snapshot payloads. Assert connection-ready startup, full SSE-frame
  parsing, folds (including `mirek_schema`), typed dump/validate round-trips, subscriber isolation,
  reconnect markers/diffs, lag coalescing, and that caller mutation never changes canonical state.
- Fixtures captured from the real bridge in Phase 0 — one full snapshot and one event sample per
  sensor type — so the unit suite validates against shapes firmware actually sends. Ids scrubbed.
- Integration: change a light, assert `state.lights[name]` updates and `changes()` yields the
  matching before/after; kill the connection, make more changes than the measured replay buffer,
  and assert replay plus `Resync(RECONNECT)` plus a correct snapshot diff. A separate quiet-stream
  test remains connected past the old total timeout in a time-controlled unit test, and
  socket-factory tests cover TCP keepalive settings.
- `API_REFERENCE.md` gains a State section; `tests/test_docs.py` fails the build until it does,
  which is the intended behaviour.

---

## Supporting changes

Each is small and independently shippable; dependencies are in the roadmap.

**A. Registry + snapshot** (`models/__init__.py`, `client/base.py`). `RESOURCE_MODELS`, the
`AnyResource` discriminated union and its module-level `RESOURCE_LIST = TypeAdapter(...)`,
`parse_resource()`, `hue.snapshot()`. `HueEvent.creationtime` becomes `AwareDatetime` (pydantic
parses the ISO string, and `model_dump(mode="json")` gives the raw form back, so nothing else
needs keeping); existing report-model `changed`/`updated` fields become `AwareDatetime | None`.
The union is the public type and persistence annotation; the immutable registry is
the runtime dispatch table, with an invariant test keeping the two and every handler declaration in
sync.

**B. Stream durability** (`client/http.py`, `client/base.py`). Five changes, two of them added
after live testing:

- Parse complete SSE frames, including **`id:`** and multi-line `data:` fields, into `SSEFrame`.
  `_read_event_stream` currently discards every line that is not `data:` (`client/http.py:477`),
  which throws away both the ordering key and the resume token.
- Send **`Last-Event-ID`** on reconnect, but make no completeness claim. `HueState` marks and
  reconciles every reconnect because buffer overflow is silent.
- Remove the SSE total lifetime timeout and enable **TCP keepalive probes** in the connector. A
  short `sock_read` timeout cannot distinguish a dead socket from a healthy quiet bridge.
- `event_connections()`, `subscribe_event_frames()`, and compatibility `subscribe_events()`;
  `subscribe_events(max_retries: int | None = RETRIES_MAX)` raises on establishment give-up.
- `Hue` tracks every typed stream it opened in a set and finalises a copy of that set in `close()`.

**C. Read-side symmetry and capture/restore** (`models/light.py`). `Light.kelvin`, `Light.rgb`
(needs brightness; `xy_to_rgb` is already in `color.py`) and `LightLevel.lux`
(`10 ** ((level - 1) / 10000)`), each as a `@computed_field @property` so it appears in
`model_dump()` and every history row carries human units for free. **`lux` must honour
`light_level_valid`** and return None when it is false — the same subtlety `mirek_valid` already
forces on colour temperature. Live check: 2 of this bridge's 3 `light_level` sensors report
`light_level_valid: false` (and `enabled: false`), so a naive computed field would publish a
number the bridge itself calls meaningless on two thirds of the fleet. `Light.kelvin` likewise
returns None unless `mirek_valid` is true; `Light.rgb` returns None without both valid xy and
brightness. The formula is taken from Signify's documentation and has **not** been checked against
a calibrated meter; the one valid sensor reads 3578 → 2.28 lux, which is plausible for an unlit
room but is not a verification. Existing plain properties (`is_on`, `brightness`, `mirek`,
`celsius`, `battery_level`, …) stay plain properties in this phase; only the three explicitly new
human-unit fields widen `model_dump()`.

The `LightState` logic moves out of `tests/integration/conftest.py:40`, but not verbatim: pytest
failure handling and the helper's `restore(hue)` dependency do not belong in the library.
`light.capture() -> LightState` records the light id and valid active colour mode;
`await light.restore(state, *, transition=None)` rejects an id mismatch and propagates normal
library exceptions. Per light,
not per room: a `grouped_light` reports no aggregate colour temperature, so capturing through the
group silently loses it (`examples/control_room.py:31` explains this already). `GroupedLight` also
gains a `color` field; the bridge sends one, and today it lands in `model_extra`.

**D. Typed deltas** (`models/event.py`). Optional `motion`, `temperature`, `light`, `button`,
`contact_report`, `power_state` and — once F lands — `relative_rotary` sections on `EventResource`,
reusing the existing reading models. Acyclic. Useful to consumers of the raw stream;
state-layer consumers already get full typed models without it. Phase 2 extracts
`Change.observed_at` from the raw grouped or ungrouped report shape with one tested helper rather
than depending on D.

**E. Throughput** (`client/http.py`). Still worth doing, but for a different reason than this
document originally gave, and at a lower priority. There is **no 3-request concurrency limit** —
40 concurrent small GETs all returned 200, and 429 tracks response *size* (ten concurrent 111 KB
snapshots produced one). The real argument for `TCPConnector(limit_per_host=3)` is **speed**:
40 requests took 4.94 s uncapped and 1.85 s capped, because an uncapped burst pays a TLS handshake
per request. Add bounded 429/503 retry automatically for GET only, including snapshots; POST,
DELETE, and arbitrary PUT payloads are not known to be idempotent and must not be replayed behind
the caller's back. A later opt-in may retry known-absolute light PUTs. **Drop the `Retry-After`
clause** — the bridge never sends that header, and a 429 body is an nginx HTML page rather than an
envelope. Phase 0 showed no dropped commands worth a `min_interval`: writes are coalesced, never
rejected, and the settled state matched the last command every time, so that option is dropped.

**F. Missing types** (`models/`, `resources/`). `relative_rotary` (Tap Dial rotation — the
strongest "brighter/dimmer right now" label available) and `zigbee_connectivity` (reachability —
which rows to discard). With the registry in place these are two models, two handlers and two
registry rows; the state layer already carries them as generic resources before that.

**G. Strict stored-config validation** (`config.py`). The current code does **not** pass
`bridge_ip: 42` into aiohttp: `_as_str` rejects it, after which `HueConfig` raises the ordinary
"no bridge address" error with the file path. Merely swapping in `pydantic.dataclasses.dataclass`
would not validate assignments made while reading the file in `__post_init__`, so the original
proposal solved no current bug. If strict file diagnostics are wanted, keep `HueConfig` as a stdlib
dataclass and validate decoded JSON through a private strict `StoredConfig` pydantic model. Decide
deliberately whether malformed JSON and wrong field types should raise rather than retain today's
tolerant "ignore invalid stored config" behaviour. This is independent cleanup, not part of the
state release.

**Decision:** retain the tolerant behavior. Existing configuration files have historically ignored
malformed JSON, unreadable paths, unknown keys, and wrong-typed stored values, while explicit
arguments and environment variables still fail normally. Turning a previously ignored local-file
problem into a startup exception is a compatibility change without a demonstrated bug to fix; the
existing resolution tests now serve as the recorded contract. A future opt-in strict mode can add a
private validation model without changing the default.

**H. Small model-completeness gaps** (`models/group.py`). Add optional, fixture-backed models for
`Scene.actions` and `Scene.status`, and add `services` to `ServiceGroup`. These were found during
the audit but were previously left without a roadmap owner. Unknown subfields remain preserved by
`extra="allow"`; only the shapes the scheduler and recorder actually consume need declarations.

---

## Phase 0: investigations answered and evidence landed

Run against the live bridge (BSB002, `apiversion` 1.78.0). The probe scripts should land in
`tests/integration/` as the Phase 0 suite; the captures belong in `tests/fixtures/`.

1. **Transition ceiling — answered.** `dynamics.duration` accepts up to **exactly 6 000 000 ms
   (6 000 s, 100 minutes)**; 6 000 001 is rejected as `207 invalid attribute value`, and a
   negative value is a `400` json-schema error. So `set(transition=...)` is valid up to
   `6000.0` seconds. **A 30-minute sunrise is one PUT**, which settles the scheduler design in
   favour of native fades and demotes E. `models/state.py:171` should reject anything above the
   ceiling locally, rather than letting the bridge answer with a 207 the caller has to unpack.
2. **What the stream says during a fade — answered over 22 runs, after a two-run first attempt
   got it wrong.** The PUT echoes the commanded target at +0.10 s; the bulb then reports its true
   level on a free-running ~24.5 s clock the PUT does not reset, so a fade carries
   `duration / 24.5` truthful samples arriving at an unpredictable phase, and the correct final
   value lands 2–24 s after fade end. Full detail, the corrected numbers and the retraction are in
   [What the bridge reports during a fade](#what-the-bridge-reports-during-a-fade). This is what
   promotes write correlation from Phase 7 to a prerequisite.
3. **Concurrency — answered, and the premise was wrong.** There is no 3-request limit; 40
   concurrent small GETs all succeeded. 429 depends on response size. `limit_per_host=3` is still
   worth having, for throughput rather than for avoiding rejection. See E.
4. **Fixtures — mostly landed.** `tests/fixtures/` now contains a privacy-minimised representative
   `GET /clip/v2/resource` sample (all 27 observed types; the raw 186-resource count is retained
   only in the research notes above) and frames from one reversible 60-second
   fade plus a temporary-scene lifecycle. They cover light and grouped-light events, a
   multi-resource scene recall update, full scene `add`, minimal scene `delete`, and exposed the
   real bridge's empty `grouped_light.color` shape. `durability_probe.json` additionally records
   the exact transition ceiling, a requested replay after 80 paced writes overflowed the bridge's
   buffer, and a 90-second raw listen with only the initial `: hi` comment. The probe is repeatable
   in `tests/integration/probe_phase0.py`.

Two investigations this document did not think to ask for, both answered and both consequential:

5. **`Last-Event-ID` — the bridge honours it.** See the facts table. This is the single largest
   correction to the design.
6. **Application keepalive — there is none.** One `: hi` at connect and nothing after; 90 s of
   silence observed. A short `sock_read` timeout would also expire on healthy silence, so use TCP
   keepalive probes and no total SSE lifetime timeout instead.


---

## What is still unverified

Everything in the facts table was measured. These were **not**, and each is a place where the
design rests on reasoning or on Signify's documentation rather than on this bridge's behaviour.
They do not all block preparatory work, but the fixtures, reconnect behaviour, and scene/write
shapes must be settled before the phases that depend on them are called finished.

| Assumption | Why it is still open | Risk if wrong |
| --- | --- | --- |
| ~~**`add` and `delete` event shapes**~~ **— now captured** | A temporary scene produced a full resource on `add` and only `id`, `id_v1`, and `type` on `delete` | Closed. `EventType.ERROR` remains fixture-only until observed |
| ~~**Reconnect reconciliation works end to end**~~ **— now live-tested** | An opt-in transport shim pauses after one cursor-bearing frame, issues 80 paced writes, then lets `HueState` reconnect | Closed: the bridge resumed from `Last-Event-ID`, state emitted `Resync(RECONNECT)`, snapshot reconciliation completed, and the local view matched the final bridge brightness |
| **A replay/live boundary or id-successor rule exists** | Neither was observed. Frame count cannot identify replay completeness without one | Until proved, every reconnect stays marked and reconciled; there is no silent-loss dependency on this assumption |
| **The ~15-frame buffer depth is stable** | One firmware, one measurement session | Informational only. Correctness no longer depends on the constant; tests should still exceed the measured window |
| ~~Sensor event payloads~~ **— now captured** | A 14-minute passive listen caught real `motion`, `temperature`, `light_level`, `grouped_motion` and `grouped_light_level` deltas. See below | Closed, with one model bug and one timestamp finding falling out of it |
| **`contact` devices** | This bridge has none. The handler exists and parses nothing | D's `contact_report` section is unexercised |
| **`relative_rotary`** | Absent — no Tap Dial here | F's headline signal cannot be developed or tested on this bridge at all |
| **`LightLevel.lux`'s formula** | Taken from Signify's documentation; never checked against a calibrated meter. The one valid sensor reads 3578 → 2.28 lux, which is plausible, not verified | A systematically wrong feature column in the history DB |
| ~~**What a scene recall emits**~~ **— now captured** | The temporary scene became `active: "static"` with `last_recall`; the previously active scene became inactive, while member lights/grouped lights updated in nearby frames | Closed for static room scenes; dynamic and zone scenes remain unmeasured |
| ~~**A generic `HueResource` can be bound**~~ **— now tested** | Snapshot fallback resources bind using their raw `type`; refresh/update path construction is covered with a fake transport | Closed |
| ~~`build_name_map` accepts a snapshot list~~ **— false** | It accesses `.metadata` on every item and raises on generic resources | Closed in the design: filter the mixed snapshot to `NamedResource`, then reuse the function |

### Sensor deltas, captured passively

Fourteen minutes of listening with no writes caught 26 events, including every sensor type this
bridge has. Three things fall out, none of which item D anticipated:

- **Every sensor reading carries its own `*_report` sub-object with a `changed` timestamp at
  millisecond resolution** — `motion.motion_report.changed = 2026-08-24T14:16:45.498Z`, and the
  same for `temperature_report` and `light_level_report`. This is a **better timestamp than
  anything in the facts table**: `creationtime` is second-resolution and `received_at` is the host
  clock. For sensor rows, `*_report.changed` is the one to record, and `Change.observed_at` now
  carries it rather than forcing the recorder to dig it out of `delta`.
- **`LightLevelReading` is missing `light_level_report`** (`models/light.py:588`), so it lands in
  `model_extra` — while `MotionReading` and `TemperatureReading` both declare their equivalents
  (`models/sensor.py:18`, `:65`). A one-field inconsistency, and it is the field carrying the good
  timestamp. Fix it in C alongside `lux`.
- **Grouped and ungrouped sensors have different shapes.** `motion` and `light_level` send the flat
  legacy field *and* the `_report`; `grouped_motion` and `grouped_light_level` send **only** the
  `_report`. D's typed sections must accept both, or grouped sensor deltas parse to an empty
  reading.

The capture also incidentally recorded a real motion-triggered automation — motion at 14:16:45.498
followed by light `on` events — which is exactly the ground truth the history application exists to
learn from, and confirms these arrive on the same stream with no extra subscription.

Two smaller notes from the same pass: `GroupedLight.color` is present on only **2 of 9** grouped
lights (those whose members are colour-capable), so C should treat it as optional rather than as a
field the bridge always sends; and the `motion_area_candidate` omission above means **`hue.snapshot()`
is not a complete bridge state**, which the periodic "full-state row" idea in the DB schema should
say out loud.

---

## Gap register

Everything the library currently leaves to the consumer, with the evidence and where this design
resolves it.

| Gap | Where | Resolved by |
| --- | --- | --- |
| No way to list a room's lights | `examples/control_room.py:21` hand-rolls it; a room's children are *devices*, a zone's are *services* | `state.lights_in()` (Phase 2) |
| No throughput control | No rate-limit, 429 or semaphore handling anywhere in `src/`; `TCPConnector` is built with no `limit_per_host` (`client/http.py:171`) | E (Phase 5) — the cap is worth having for **speed** (2.7× on a 40-request burst), not to avoid a rejection threshold that does not exist |
| `transition` ceiling unenforced | `models/state.py:171` rejects only negatives; the real ceiling is 6 000 s, and exceeding it returns a 207 the caller must unpack | Phase 3 — reject the post-conversion duration above 6 000 000 ms locally; Phase 0 established the bound |
| Ambient light is raw | `models/light.py:602` returns `10000·log10(lux)+1` | `LightLevel.lux` (C, Phase 3) |
| Sensor half of the stream untyped | `models/event.py:44` types lights only; motion, temperature, light_level, button, contact and battery fall through to `model_extra` | Typed aggregate-visible state from the state layer (Phase 2); typed deltas for raw-stream users (D, Phase 4) |
| `creationtime` is a `str` | `models/event.py:75` — every row re-parses it | A (Phase 1) |
| Events are deltas with no baseline | No wrapper for `GET /clip/v2/resource`; `refresh_names()` spends 5 GETs and keeps only names | `hue.snapshot()` + `Change.before/after` (Phases 1–2) |
| Reconnects lose events silently | `client/http.py:477` parses only `data:` lines, so the `id:` resume token is thrown away and the whole backoff window is lost with no marker | Request replay via `Last-Event-ID`, mark every reconnect because completeness is unknowable, and snapshot-diff (B + Phase 2) |
| The collector can stop without telling you | `client/http.py:434` returns after `RETRIES_MAX`; the consumer's `async for` ends normally | `subscribe_events` raises on give-up; the state never gives up (B) |
| Two subscribers leak | `client/base.py:107`, `:304` — `_event_stream` is a single slot, and `close()` finalises only the last | `changes()` fan-out; `close()` finalises all (B + Phase 2) |
| No room enrichment | `client/base.py:239` gives a name; there is no `room_for(id)` | `state.room_of()`, `state.name_of()` (Phase 2) |
| Write in human units, read in bridge units | `models/light.py:439-453` gives `mirek` and `color.xy` only, though `set()` takes `kelvin=` and `rgb=` | `Light.kelvin`, `Light.rgb` (C) |
| `capture()` / `restore()` hand-rolled twice | `examples/control_room.py:31` and `tests/integration/conftest.py:40` — the same 15 lines, the same `mirek_valid` subtlety | C |
| Missing input signals | `models/common.py:171` — 17 `ResourceType` members, no `relative_rotary`, no `zigbee_connectivity`. Live: `zigbee_connectivity` is present (22 of them); **`relative_rotary` is absent — there is no Tap Dial on this bridge**, so the "strongest brighter/dimmer label" F promises does not exist here | F (Phase 6), rescoped |
| **11 live resource types are unmodelled** | The bridge reports 27 types; 16 of the 17 declared handler types are present (`contact` is absent). The 11 live gaps are `zigbee_connectivity` plus `behavior_instance`, `behavior_script`, `smart_scene`, `device_software_update`, `entertainment`, `geolocation`, `homekit`, `matter`, `clip`, and `zigbee_device_discovery`. `relative_rotary` is an additional known but absent type | The registry's generic `HueResource` fallback (Phase 1) carries all of them; promote one to a real model only when a consumer needs its fields |
| **`Scene.actions` and `Scene.status` are unmodelled** | `models/group.py:89` declares `group`, `speed` and `auto_dynamic` only. `actions` *is* the scene — the per-light target state — and `status` says which scene is currently active. Both fall into `model_extra` | H (Phase 3). A scheduler cannot ask "what would this scene do?", and a recorder cannot label rows with the active scene without them |
| **`ServiceGroup` drops `services`** | `models/group.py:83` declares `children`, but the bridge sends `children` *and* `services`; the latter lands in `model_extra` | H (Phase 3) |
| **The event `id:` line is discarded** | `client/http.py:477` keeps only `data:` lines, throwing away both the monotonic ordering key and the `Last-Event-ID` resume token | B (Phase 1) |
| **A half-open event-stream socket goes unnoticed for up to an hour** | The bridge sends no application keepalive, and the stream's only bound is `TIMEOUT_STREAM = 3600` | B (Phase 1) — TCP keepalive probes and no total SSE lifetime timeout; a short read timeout would break healthy idle streams |
| **A fade is sampled sparsely and its first event is a lie** | The PUT echoes the commanded target at +0.10 s; true levels then arrive only on the bulb's free-running ~24.5 s clock, and the final value 2–24 s after fade end | Write correlation, promoted out of Phase 7 into Phase 2b — flag the echo, interpolate between the truthful samples |

**Workarounds until each phase ships.** Keep `members()` from `control_room.py` for a room's
lights; a zone's `children` are already light services. Wrap writes in one shared
`asyncio.Semaphore(3)`. Read unmodelled sections off `resource.model_extra`, and parse timestamps
with `datetime.fromisoformat(event.creationtime)`. Take your own baseline with
`hue.http.get("/clip/v2/resource")`, and wrap the stream in a supervisor that treats normal
completion of the `async for` as a failure, re-subscribes, re-snapshots, and writes a gap row.
huepy's `subscribe_events()` cannot resume — it discards the `id:` lines — so until B lands, a
  consumer that needs honest reconnect handling has to read `/eventstream/clip/v2` itself, keep
  the last `id:`, send it back as `Last-Event-ID`, and still snapshot plus write a possible-gap row
  after every reconnect. Configure TCP keepalive rather than a short `sock_read` timeout, which
  cannot distinguish a dead socket from a quiet bridge. Fan out to several consumers from one
  subscription rather than calling `get_event_stream()` twice.
`mirek_to_kelvin(light.mirek)` and `xy_to_rgb((x, y), brightness)` are already exported from
`huepy.color`, and `lux = 10 ** ((level - 1) / 10000)`. Adapt the valid-colour-mode logic from
`tests/integration/conftest.py`; do not copy its pytest-specific restore behaviour. `relative_rotary` and `zigbee_connectivity` are readable
today via `hue.http.get("/clip/v2/resource/<type>")` and appear in the event stream regardless.

The gap row matters more than it looks: without it, a gap in the stream is indistinguishable from
"nothing happened", and the model learns from a period it has no evidence for.

---

## Roadmap

| Phase | Content | Depends on | Done when |
| --- | --- | --- | --- |
| 0 | **Complete.** Investigations and scrubbed evidence landed 2026-08-24 | — | Aggregate, fade, scene lifecycle, transition-boundary, replay-overflow, and raw quiet-stream captures are fixture-backed; opt-in repeatable probes live in `tests/integration/` |
| 1 | **Complete.** A + B: explicit `AnyResource` union, snapshot, aware timestamps, durable typed SSE connections, replay cursors, TCP keepalive, raise-on-give-up, multi-stream close | 0 fixtures | Registry, snapshot persistence, handshake, timeout, socket, replay, and cleanup invariants are tested |
| 2 | **Complete.** `HueState`, isolated synchronous views, typed `Change`, sensor timestamps, resync markers, unconditional reconnect snapshot-diff | 1 | Deterministic fold/lag/isolation coverage and the live overflow/replay/marker/reconciliation test pass; example and API reference landed |
| 2b | **Complete.** Write observation/correlation, command outcomes, echo/report classification, and `state.fading` | 2 + fade/group fixtures | Reversible live fade marks the target echo; deterministic tests cover outcomes, external writes, overlap, grouped targets, and publication ordering |
| 3 | **Complete.** C + H: read symmetry, capture/restore, transition ceiling, grouped colour, scene actions/status, service-group services | 0 fixtures; parallel with 1–2 | Computed fields, helpers, local bound, and fixture-backed scene/service parsing are tested and documented |
| 4 | **Complete.** D: typed light, sensor, button, contact, battery, and rotary deltas | 1, and 6 for rotary | Raw-stream consumers can read known reports without touching `model_extra`; grouped and ungrouped sensor shapes are tested |
| 5 | **Complete.** E: three-connection throughput cap and bounded GET-only 429/503 retries | 0 | Tests cover retry success/exhaustion and assert POST/DELETE/arbitrary PUT are never automatically replayed |
| 6 | **Complete for available evidence.** F: fixture-backed `zigbee_connectivity`; tolerant `relative_rotary` model and handler for bridges with a Tap Dial | 1 | `zigbee_connectivity` parses from the aggregate fixture and both types appear in `ResourceType`, the registry, and handler invariants |
| — | **Resolved without code.** G: retain tolerant stored-config loading for compatibility; strict validation may be a future opt-in | — | Existing resolution tests document malformed JSON, unreadable paths, unknown keys, and wrong-typed values as ignored fallbacks |

Phases 1, 2, 2b and 3 together make the coherent 0.3.0. Phase 2 alone is useful for control and
monitoring, but the release must not advertise correct fade history until 2b lands. Phases 4–6 are
0.3.x or 0.4.0. Each phase touches `API_REFERENCE.md` and `README.md` where its public surface
changes and lands with tests; `tests/test_docs.py` enforces the reference.

**Consumer examples** — they are the proof the design is usable:

- `examples/track_state.py`: `async with hue.state() as state`, print a room's lights from the
  state, then print changes as they arrive with names and rooms resolved.
- Phase 2b, `examples/record_history.py`: the history recorder in ~40 lines — one `changes()` loop
  writing `(at, observed_at, event_at, received_at, event_id, type, id, name, room, delta,
  origin, command_id, command_confirmed, observation, transition_ends_at, resynced)` rows and gap
  markers to sqlite.

## Consumer-side notes

- **Scheduler design is unblocked.** Long native fades work, up to 6 000 s. A sunrise is one PUT,
  and the throughput question largely disappears with it.
- **Attribution.** Issue writes through the same transport as `HueState`; it records the transition
  duration and correlates matching reports. Unmatched changes remain `unattributed`, not assumed
  user input. Retrofitting attribution onto history already collected is not possible, so the
  recorder carries `origin`, `command_id`, `command_confirmed`, `observation` and
  `transition_ends_at` columns from its first row.
- **Gap markers are not optional.** A `Resync` row separates "nothing happened" from "continuity
  could not be proved". It fires on every reconnect, even when replay was probably complete,
  because the bridge silently truncates its undocumented buffer. The history schema needs a place
  for it from day one.
- **Do not trust brightness during a fade.** At any instant the cached level is stale by up to one
  ~24.5 s report period, and stays unreliable until ~25 s past the commanded end. Rows inside a fade
  window need the commanded target and end time beside them.
- **Discard, or flag, the event that lands ~0.1 s after your own fade PUT.** It is the commanded
  target echoed back, not a reading. It is the only genuinely false value in the trace: the periodic
  samples that follow are accurate to within a brightness point and can be interpolated between.

## Decisions open to override

- `hue.state()` / `HueState(hue)` naming, and the sync view vocabulary mirroring the handlers.
- `Change | Resync` as a union, rather than one record type with a `kind` of `resync`.
- **Request replay, then mark and snapshot every reconnect.** This is intentionally conservative:
  `Last-Event-ID` recovers most real intermediate events, but neither frame count nor the first
  returned id proves completeness. Suppressing `Resync(RECONNECT)` requires a future verified
  successor/boundary rule, not a firmware-specific buffer constant.
- The measured ~15-frame depth remains a fixture/probe parameter, never a correctness constant.
- Bounded per-subscriber queues with lag signalled as a `Resync`, rather than unbounded queues.
- State is never updated optimistically on a write.
- `Change` and `Resync` as shallow-frozen pydantic models rather than stdlib dataclasses, with
  detached subscriber-local nested data because pydantic freezing is not recursive.
- Read-side conversions as `computed_field` rather than plain properties, which widens
  `model_dump()` for existing callers.
- Exposing `state.fading` at all, rather than letting the state lie quietly during a transition.
  The alternative — interpolating a synthetic brightness so `state.lights[x].brightness` reads
  plausibly mid-fade — was rejected: a history DB must not record numbers the bridge never
  reported. Marking the window and letting the consumer decide is the honest option. The 22-run
  re-test strengthens this: the bridge's own in-fade samples proved accurate to within 0.9
  brightness points of a linear ramp, so a consumer that *wants* to interpolate has a sound basis
  for it — which is an argument for exposing the window and the samples, not for smoothing them
  away behind the reader's back.
