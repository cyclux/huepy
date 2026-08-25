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
