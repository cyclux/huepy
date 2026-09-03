# Real-bridge regression fixtures

The scrubbed representative snapshot and SSE frames are generated only from an
explicitly selected test bridge. The capture module retains its original
`phase0` filename for reproducibility:

```console
HUEPY_INTEGRATION=1 uv run python -m tests.integration.capture_phase0
```

The capture tool never writes bridge credentials. It keeps one representative resource per
observed type, reduces unknown resources to `id` and `type`, truncates relationship and scene-action
lists, replaces identifiers consistently, generalises product data and timezone, and rebases all
absolute timestamps and SSE cursors. This preserves parser coverage without recording household
schedules, inventory, topology, or activity dates. It performs one 60-second fade on a dimmable
light whose name does not contain “Bad”, then creates, recalls, and deletes a uniquely named
temporary scene in a room that contains no such lights. Every cleanup action is attempted even if
an earlier one fails. The generated JSON files must still be reviewed before commit.

Durability evidence is captured separately because it deliberately creates a
connection gap and includes a 90-second quiet listen:

```console
HUEPY_INTEGRATION=1 uv run python -m tests.integration.probe_phase0
```

This writes `durability_probe.json`, covering the 6,000,000 ms transition
boundary, `Last-Event-ID` after 80 paced writes exceed the measured replay
buffer, and raw SSE comments during the quiet interval. Connection cleanup and
light restoration are both attempted even if either fails. Individual probes
can be skipped with the command's `--skip-*` flags.

The plan runner's evidence is captured by a third module, against the room the
plan runner's live tests use (`PLAN_ROOM` in `tests/integration/conftest.py`):

```console
HUEPY_INTEGRATION=1 uv run python -m tests.integration.probe_plans
```

This writes `plan_probe.json`. It fades one tunable-white light in that room
from 20 to 100 over sixty seconds, switches it off at ten seconds and back on
at twenty with no other field, samples the light at 11, 21, 30 and 61 seconds
and records every event frame about it, then restores it. The recorded outcome
is what the runner's override logic relies on. A second section fades the whole
room from 30 to 90 over forty seconds and records every progress report from
the member lights and the room's `grouped_light` against the linear ramp. A
third section switches one
tunable-white light off, writes it a brightness and a colour temperature with
no `on`, and switches it back on with no other field, the way a dimmer does;
whether the bulb comes on at the written values is what lets a day curve's
morning step undo the night light in a room nobody has switched on. A fourth,
passive section keeps
one minimised sensor resource of each kind from a snapshot and listens for the
first event frame of each for up to `--listen-minutes` (default 5); it wants a
sensor to see a change, so walk past one or press a dimmer while it runs.
Any section can be skipped with `--skip-resume` / `--skip-progress` /
`--skip-off-write` / `--skip-passive`. No display name is written into the file.
