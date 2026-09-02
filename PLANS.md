# Declarative plans: architecture and bridge evidence

Companion to `STATE_LAYER.md`, for `huepy/plans/`. User-facing behaviour lives
in `README.md` and `API_REFERENCE.md`; this file records *why* the layer is
shaped the way it is, and which of its constraints are measured rather than
assumed.

## Scope and status

Shipped: the TOML format, solar computation, day-curve scheduling, name
resolution, the write executor, priority arbitration, manual-override yielding,
reconnect recovery, sensor rules (`motion:`, `button:`, `contact:`,
`light_level:` with a `below` / `above` threshold in lux), application
signals, an HTTP signal server so those reach a daemon from outside the
process, and a CLI.

## What the bridge can and cannot do

Four findings shape every decision below. Each was checked against the official
portal mirror in `docs/hue-dev-docs/` or this repo's own bridge fixtures.

| Finding | Source | Consequence |
| --- | --- | --- |
| `smart_scene` start times are `time` or `sunset` only — no sunrise, no offsets | `api-reference.md:41819`, and again at `:42209` and `:42807` | A plan cannot be compiled to a bridge-side smart scene |
| `geolocation` returns `sunset_time` but no `sunrise_time`; its latitude and longitude are **write-only** | `api-reference.md:35283`, `:35571` | Sun times must be computed in-process, and cannot even be seeded from the bridge |
| `behavior_script` is GET-only; third parties cannot author automations | `migration-guide-to-the-new-hue-api.md:354` | No bridge-side automation is generated. aiohue, Home Assistant and openHAB all decline to model these too |
| `dynamics.duration` accepts 6,000,000 ms and rejects 6,000,001 | `tests/fixtures/durability_probe.json:1457`, asserted at `tests/test_real_fixtures.py:109` | A 100-minute fade is one PUT. Longer ramps chain |
| `grouped_light` accepts 6,000,000 ms too | `tests/integration/test_live_plans.py` (opt-in, live) | A room's chained fade is the same shape as a light's |
| A light's `dimming.brightness` is a transition's *target* from the moment the write is accepted, and a bare switch-off leaves it there; switched back on with no other field, it reports the target again | `tests/fixtures/plan_probe.json`, asserted at `test_plan_probe_measures_a_transition_across_switch_off` | The fade after a switch-off starts from the interrupted fade's target. Only a cold start is blind to brightness |
| For that bulb the stream carried the target once and no progress report in sixty seconds | same fixture | A missing progress report is normal; the override arithmetic must not expect one |
| During a 40 s room fade each bulb's own progress reports track the linear ramp within 2 points, one report every 15-20 s; the `grouped_light` reports the average of its members' *last* reports, up to 27 points off the ramp | `tests/fixtures/plan_probe.json`, asserted at `test_plan_probe_measures_progress_reports_during_a_room_fade` | Only `light` reports are judged; a group's report is not a measurement, and its members are all indexed |
| A `light_level` event's delta carries `light.light_level_report.light_level` and the deprecated `light.light_level`, equal, with `light_level_valid` | same fixture, `test_plan_probe_records_sensor_representatives_and_frames` | `runner._reported_level()` reads the report and falls back to the field; both are real |

The duration ceiling is **measured, not documented** — the API reference gives
no bound at all. It was probed on a BSB002 at CLIP 1.78.0, and independently
corroborated by hue-scheduler's documented 100-minute maximum. Treat it as an
observation about one firmware, not a protocol guarantee.

The rate budget is documented: roughly 10 writes/s to `/light` and 1/s to
`/grouped_light` (`core-concepts.md:247`), because ZigBee caps broadcasts at
about one per second system-wide (`hue-system-performance.md:125`). Parameter
count is message count — `bri` is one ZigBee message, `bri + xy + on` is three
(`hue-system-performance.md:47`).

## Why TOML, and only TOML

YAML 1.1 — which is what PyYAML implements — resolves the bare words `on`,
`off`, `yes` and `no` to booleans. This format's most important key is `on`, so
`set: { on: false }` parses as `{True: False}`. A format whose signature footgun
lands exactly on the domain's core vocabulary is disqualified.

TOML additionally rejects duplicate keys where YAML silently keeps the last, so
a copy-pasted `at` is a parse error rather than a step that never fires. It is
stdlib, so it costs no dependency, and it is the language the audience already
reads daily. JSON was excluded for having no comments in a file humans edit.

The choice is reversible: parsing is a handful of lines ahead of
`Plan.model_validate()`, so YAML could be added later as an optional extra
without a breaking change. The reverse would not be true.

## Module boundaries

Pure — no clock, no client, no I/O, `now` always a parameter:

| Module | Answers |
| --- | --- |
| `fields.py` | What does `"1h15m"` / `"sunset+30m"` / `"room:Living Room"` mean? |
| `schema.py` | Is this file a valid plan? |
| `sun.py` | When is sunrise here, on this date? |
| `timeline.py` | Which step is in force, and where should the light be now? |
| `arbiter.py` | Who owns this scope, and was that change ours? |

Impure:

| Module | Does |
| --- | --- |
| `loader.py` | Reads and merges `.toml` files |
| `resolve.py` | Binds names to resource ids against one snapshot |
| `executor.py` | Composes and sends the writes |
| `runner.py` | Owns the loop |

`plans/` depends on the `PlanClient` Protocol in `plans/protocol.py`, never on
`client/base.py`. That is what keeps the import graph acyclic, and it is why
every test in the package runs without a bridge.

### Signals from outside the process

`signals.py` serves `PlanRunner.fire()` over HTTP: `POST /signals/NAME`,
`GET /signals`. It is the one `aiohttp` import outside the client, and it is a
*server* that never talks to a bridge, so the `Transport` seam is untouched.
HTTP rather than a Unix socket because the users of this hook are shell
scripts, `curl` and a home-automation box, and because it works on Windows;
`aiohttp.web` rather than a hand-rolled parser on `asyncio.start_server`
because real clients send keep-alive, `Expect` and chunked bodies, and parsing
those by hand is where the bugs would live. Loopback by default; binding
anywhere else refuses to start without a bearer token, so a plan is reachable
from another machine on purpose or not at all. The server needs only a
callable and a set of names, which is what lets `cli.py` stay the one place
that binds it to a runner.

### `days` gates the whole day, not just the step times

The waypoint search spans yesterday, today and tomorrow, so last night's final
step still governs this morning. Applied naively that also lets a weekend-only
scenario keep asserting on Monday — and at a higher priority it masks the base
curve until its own steps fall out of the three-day window, which took the
shipped example to roughly fifty hours stuck at 100%. `waypoints_around()`
therefore returns nothing at all for a scenario the local day does not match.

The corollary for plan authors: a higher-priority scenario owns its scope for
as long as it makes *any* claim, so an override curve needs to be a complete
day, not just a different morning. `examples/plans/flat.toml` shows the shape.

### There is no such thing as "the local timezone" as a value

`datetime.now().astimezone().tzinfo` returns a **fixed offset** frozen at
whatever DST is in force when it runs. A runner holds its zone for the life of
the process, so a daemon started in summer would fire every clock step an hour
early all winter — and `[location]` is optional, so this is the default path.

`zone_of()` therefore returns `None` for "the host's own zone", and
`combine()` / `in_zone()` resolve it per instant against the system rules. Any
new code that reaches for a `tzinfo` must go through those two helpers.

## Seven decisions worth remembering

### Every trigger is one path

A motion sensor, a button, a door contact and an application signal all reach
the arbiter as the selector string they were written as, through
`Arbiter.fire(key, now)`. That one method matches `activate_on`, `release_on`
and every rule's `when`. The consequence is that nothing is a special case:
`activate_on = "contact:Front door"` wakes a mode, `when = "signal:doorbell"`
fires a rule, and `PlanRunner.fire()` is three lines.

What each kind *means* is fixed in `runner._edge()`: motion starting, a
button going down (`initial_press` — the first event a press produces, so the
light reacts before the finger lifts), a contact opening (`no_contact`). Only
the delta is read, never the folded state, so enabling a sensor whose last
state happened to be "motion" does not fire a rule.

Motion and a light level are the triggers with a duration, and their holds
are timed from the *end*: the sensor reports `false` once the room has been
still for its own timeout, or the level goes back past its threshold, and
that is when a `hold` starts counting (`Arbiter.ended()`). Timed from the
start, someone standing in the hall for three minutes lost the light after
ninety seconds — the Hue sensor reports `true` once and then says nothing
while movement continues.

A light level is the one kind with a threshold, and `runner._level_edge()` is
where it fires: on the *crossing* of the rule's `below` or `above` lux, never
on a periodic repeat, and released only once the reading is past the
threshold by `LIGHT_LEVEL_DEADBAND` — 7000 raw units, a factor of about five
in lux. The band is the `offset` the Hue app writes into a motion sensor's
`daylight_sensitivity` beside its `dark_threshold`, read off this bridge's own
behaviour instances: measured, not documented. Without it a hallway sensor
that sees the night light it switches on releases the rule as the light comes
up and fires again as it goes out. The crossing needs the previous reading,
so `PlanRunner._levels` keeps one per sensor — the runner's only per-sensor
memory, kept across a resync because a stale previous still gives the right
direction. A first reading is judged as if the one before it had been on the
far side, so a daemon started after dark fires on the sensor's next report,
and a still-dark report three minutes after someone dimmed the hall by hand
does not refresh the hold and take the room back. A trigger reaches the
arbiter as the selector string it was written as, and one crossing fires
every rule naming it, so the schema makes every rule naming one sensor agree
on its threshold; two thresholds on one sensor would need a `Rule.key` in
place of the selector, an upgrade nothing else would notice.

The state layer folds every resource type, and each sensor event carries a
fresh `changed` / `updated` timestamp, so a repeated `initial_press` is a
distinct `Change` even though the folded `event` field did not move.

### Handing a scope back never snaps

A rule hold lapses, or a mode releases, and the day curve underneath takes the
scope again. The "right" ramp for a normal tick is the current step's
*remaining* ramp — and that is zero once the step has finished, which would
drop a hallway from the rule's 15% to the curve's 80% in one frame as someone
walked away. `Arbiter._claim_for()` floors the return ramp at
`defaults.catchup_ramp`, but only for a day curve: a mode or flat state keeps
the ramp its author wrote, including zero.

Telling "the same claim, still in force" from "a hand-over" is done by
comparing `Claim.source` — the scenario name, or `scenario/trigger` for a
hold — against `ScopeState.owner`. Comparing scenario names alone missed the
case of a hold lapsing back into its own scenario's curve.

A yield ends the same way, by comparing instants rather than by precomputing a
resume time. `ScopeState.yielded_at` is when the human acted; `Claim.since` is
when the authority behind the winning claim began — the step's start, the
hold's placement, the mode's activation, whichever is latest. The scope comes
back at the first winning claim whose `since` is not before `yielded_at`. A
precomputed "next step" had no answer on a day when nothing covering the scope
ran, and left the scope yielded for good; and it let a *losing* rule's hold
un-yield a room the human had dimmed during a film. Releasing a mode is itself
the trigger that ends a yield made while it held the scope.

Two corollaries for plan authors. A rule without `hold` lasts until the scope's
next scheduled step, not forever, or a button press would switch a day curve
off for good. And a scope claimed by nobody is left alone, so a motion light
that should switch itself off needs a resting state underneath it — a flat
`set = { on = false }` scenario at a lower priority.

### A fade is handed to the bridge whole

Prior art splits two ways. Adaptive Lighting re-sends every 90 seconds because
Home Assistant must drive every vendor's bulbs. hue-scheduler leans on long
native transitions because it targets this bridge specifically. huepy is in the
second camp: a two-hour sunset is two PUTs and then silence, not eighty.

`executor.plan_segments()` chains anything over the ceiling with interpolated
waypoints, writes a room through its `grouped_light` rather than per bulb, and
drops `on` when the scope is already on. It also drops a payload that would
carry nothing but `dynamics`, which is what a redundant `on` leaves behind.

### Interpolation is for catching up, not for scheduling

These are different questions and conflating them is a real bug, not a style
choice. `current_step()` answers "which step is in force, and how much ramp is
left" — that is what a normal tick sends. `target_at()` answers "where should
this light already be", interpolating a part-finished fade — that is what a
restart uses, over the short `catchup_ramp`.

Using the interpolated value for scheduling means that at the instant a step
begins, its target equals the previous step's value, so the runner computes a
write that changes nothing and the fade never starts.

### Override detection cannot lean on the state layer's window

`HueState` marks a fade as its own until `ends_at + FADE_REPORT_ALLOWANCE`
(`state/core.py:1482`). That is right for a two-second transition. It is wrong
here, because this layer issues fades lasting up to a hundred minutes: someone
hitting the wall switch thirty minutes into a sunset would be masked for the
next hour.

`arbiter.Fade.explains()` therefore checks a report against the fade's own
interpolated expectation at that instant. Progress consistent with the ramp is
ours; a jump beyond `BRIGHTNESS_TOLERANCE` is a human, and so is a reported
power state the fade did not ask for — a fade to a brightness is a fade on a
light that is on. The tolerance is deliberately generous — the bridge reports
progress on the device's cadence, and a false "that was a human" costs one
skipped step, while a false "that was us" ignores someone reaching for the
switch. Measured on a room of four, each bulb's own reports sit within two
points of the ramp, one every fifteen to twenty seconds, and a colour bulb may
send none at all. The room's `grouped_light` reports something else entirely:
the average of its members' last reports, a stale mix of the target and each
bulb's progress that sat twenty-seven points off the ramp and yielded a room
nobody had touched. `runner._observe()` therefore judges `light` reports only.
Every member of a room or zone scope is indexed, so nothing is lost.

A five-minute daemon soak on that room -- three timed steps, one light
dimmed by hand mid-fade, the take-back at the next step -- found three more
things the fakes had never modelled. A light a fade switches on from off
ramps up from *dark*, whatever brightness the bridge held for it while off
(17, 26, 37 on the way to 40), so `timeline.fade_origin()` starts the
arithmetic at zero for a fade-in. A fade to off with a ramp reports each bulb
still on and dimming until the ramp ends, then `on=False brightness=0` some
twenty seconds later; `Fade.explains()` accepts both. And after the hand
change, the bridge kept fading the two lights the human had not touched, and
every one of their progress reports read as another hand change: the yield's
instant crept forward for the rest of the ramp and `reported` ended at the
other bulbs' level rather than the human's. `ScopeState.lapsed` keeps the
interrupted fade so a bare dimming report it explains is left alone; a report
that names `on` is always a switch, because forgetting one is how a later
step comes to drop `on`.

The corollary that was missed once: `Change.origin == "self"` *is* that time
window, so the runner must not use it as proof either. Driven through a real
`HueState`, a brightness of 95 reported thirty minutes into a fade expecting 73
arrived as `origin="self"` and was waved through. The one report that is ours
by construction is `observation == "command_echo"` — the bridge repeating the
transition's *target* back the moment it accepts the write — and that is the
only thing `runner._observe()` skips. Everything else is judged.

A hand change also resets what the runner believes about the light. The
executor drops `on` when the previous fade already turned the light on, so a
switch-off that went unnoticed made the noon step go out without `on` and the
room stayed dark. Forgetting the fade is what makes the next write carry it
again — under `reassert` as much as under `yield`, which is why a reassert plan
still subscribes to changes.

Forgetting the fade must not mean forgetting where the light *is*. The first
version of that fix cleared the fade and nothing else, so the next fade ran
from `start=None`, `Fade.expected_at()` answered "the target" for the whole
ramp, and the bulb's own progress reports — which the state layer used to hide
— were judged as jumps: one false yield per step, for ever, or under `reassert`
one PUT per progress report. `ScopeState.reported` now keeps the state the
human left, the next fade starts from it, and a fade whose starting brightness
is genuinely unknown judges only the power state until it lands. That leaves
one deliberate blind window for brightness, never for `on`: the cold-start
catch-up (`catchup_ramp` seconds, `start=None`).

The fade after a bare switch-off used to be the second. Closing it needed a
measurement, and `tests/fixtures/plan_probe.json` is it: the bridge holds a
transition's *target* as the light's brightness from the moment it accepts the
write, a switch-off leaves it there, and a switch-on with no other field
reports it again. `arbiter._remember()` therefore keeps what the bridge holds
as the brightness of a switch-off: the target of the *segment* the
interrupted fade was running (`Fade.held_at()` -- a chained fade only ever
gave the bridge its current waypoint), or, for a fade that set no brightness,
the level it started from. Whichever of `on` and brightness a report did not
name is filled the same way -- the bridge keeps each attribute on its own, so
the runner's belief does too -- and the last *hand* report is consulted only
when no fade has run since, because it goes stale while the plan drives the
light. In the same spirit, a brightness reported during a fade that never
asked for one is a human at the dial, not the fade's work. The same probe
showed that bulb pushing no progress report at all in sixty seconds; the
arithmetic must not depend on seeing one.

`reported` is written by foreign reports only -- the plan's own echoes are
skipped before the arbiter sees them -- so it goes stale while the plan drives
the light. That bit once: the plan switched the room off at 23:00, the next
morning's write was refused, and the retry started from the previous day's
hand-on report, dropped `on` as redundant, and left the room dark for the
step. A failed write therefore folds the fade it interrupted into `reported`
(`Fade.expected_at(now)`, which carries the plan's own `on` target) before
forgetting it; a failed chain tail does the same, so its retry chains again.
`TestWithRealState` drives the window, the echo and the progress path through
a real `HueState`, so a change to its attribution shows up in this suite; the
hand-change-then-next-fade path is pinned on the fake in
`TestProgressAfterHandChange`, and the failure paths in
`TestBeliefAfterFailure`.

## No durable state

The runner writes nothing to disk and remembers nothing across restarts. On
start, and after every reconnect, it asks the timeline where each scope should
be at this instant and fades there over `catchup_ramp`, waits for that fade to
land, and then ticks once so the rest of the step's ramp goes to the bridge.
Without that tick nothing scheduled would wake the loop — the step has already
started — and a restart at 09:30 froze the light at 60% for a quarter of an
hour. Without the wait, the second PUT overrides the first and the whole
remaining ramp runs from wherever the light happened to be. There is no journal
to replay and nothing that can get out of sync with reality.

This is the reason `timeline.py` must stay pure and clock-injected. It is also
why a whole simulated day runs in microseconds in `tests/test_plans_runner.py`.

Stopping is a request, not a cancellation. `PlanRunner.stop()` sets the
closing flag and wakes the loop; `run()` returns after the write it is on, and
the context managers around it close the session in order. The CLI installs
that as the SIGINT and SIGTERM handler, because `kill` and systemd send
SIGTERM, and Python's default answer to it is to end the process with the
bridge session and the event stream dropped mid-frame.

## Deliberately excluded

Compiling a plan to bridge-side `smart_scene` resources would let the schedule
survive the Python process dying. It is excluded because sunrise anchors and
offsets cannot be expressed at all (see the table above), and because no
third-party `smart_scene` POST is attested anywhere in the prior art — this
repo's own tests only ever activate one. Revisiting it starts with an
integration probe establishing whether a third-party app key can POST one.

## Verification map

| Claim | Checked by |
| --- | --- |
| Duration grammar, solar anchors, selectors | `tests/test_plans_fields.py` |
| Solar times against published almanac values, polar cases | `tests/test_plans_sun.py` |
| Format rejections: typos, colour conflicts, duplicate steps | `tests/test_plans_schema.py` |
| Midnight wrap, interpolation, `next_transition` | `tests/test_plans_timeline.py` |
| Name binding, segment chaining, exact wire shapes | `tests/test_plans_resolve.py` |
| Catch-up, idempotent ticks, yielding, priority | `tests/test_plans_runner.py` |
| The three offline CLI verbs never need a bridge | `tests/test_plans_cli.py` |
| A `days` scenario falls silent on days it does not run | `TestRecurrenceExpiry` |
| What fires each trigger kind, holds, windows, hand-back, the no-snap floor | `TestRules`, `TestModeHandback` |
| A level fires on the crossing, releases past the band, never on a repeat, and a still-dark report does not un-yield a scope; the lux scale round-trips through `models.LightLevel`; the schema ties `below`/`above` to `light_level:` and makes rules on one sensor agree | `TestLevelRules`, `TestLevelEdge`, `tests/test_plans_fields.py::TestLightLevelUnits`, `tests/test_plans_schema.py::TestLevelThreshold` |
| The signal server fires known names, refuses unknown ones with the list, guards a token, survives a failing callback, and will not bind beyond loopback unguarded; `huepy plan signal` reaches it | `tests/test_plans_signals.py`, `tests/test_plans_cli.py::TestSignal` |
| A trigger landing mid-write is not lost by the loop | `TestRules::test_a_trigger_during_a_write_is_not_lost` |
| `origin="self"` is not proof; `command_echo` is; a switch-off yields and resets `on` | `TestObservation` |
| A yield ends at the first later step, hold or mode; a losing hold does not end it | `TestYieldResume` |
| A restart mid-fade lands, waits, then continues the ramp | `TestRestart` |
| The fade after a hand change starts where the human left the light; reassert re-drives once | `TestProgressAfterHandChange` |
| A refused write retries from where the fade had the light, `on` included; a failed tail re-chains | `TestBeliefAfterFailure` |
| What the state layer's window actually delivers, and what the runner does with it | `TestWithRealState` |
| Directory merge: singletons, versions, unknown keys, names across files | `tests/test_plans_loader.py` |
| A host-local clock time survives a DST change | `TestZone` |
| Body-level write rejections raise rather than stranding a scope | `TestWriteErrors` |
| One failing scope neither stops the runner nor is forgotten | `TestFailureIsolation` |
| `stop()` ends `run()` without cancelling it; SIGTERM reaches it; what a write logs | `TestClose`, `TestLogging`, `tests/test_plans_cli.py::TestStopSignals` |
| What `validate` prints per binding; a disabled sensor is a warning, not an error | `tests/test_plans_cli.py::TestValidateReport`, `TestResolveTriggers` |
| Against a real bridge, in one vetted room: one-snapshot resolution, one `grouped_light` PUT per catch-up reaching every member, the echo is not a yield, a hand switch-off and a hand jump both yield through the state layer's window, a ceiling-length first segment is accepted on a group | `tests/integration/test_live_plans.py` (opt-in) |
| What a bare switch-off leaves as the light's brightness; a real `light_level` resource and event shape | `tests/test_real_fixtures.py::test_plan_probe_*` |
| A switch-off keeps the running segment's target, or the off step's starting level; a jump during the fade that follows is seen; a report naming only `on` keeps the brightness; a dimming report during an on-only fade is a human | `TestSwitchOffMemory` |
| A refused write leaves the previous fade in force, so a switch-off after it remembers what the bridge holds; a refused first segment and a failed tail both retry as a chain | `TestBeliefAfterFailure` |
| A `grouped_light` report is never judged; a member light's still is | `TestGroupReports` |
| A fade-out's own on-and-dimming and off-at-zero reports are the fade; a fade-in from off is judged from dark; untouched members' progress after a hand change is not a second hand change, a switch is | `TestFadeOut`, `TestProgressAfterHandChange`, `TestLapsedFade` |
