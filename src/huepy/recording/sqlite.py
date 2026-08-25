"""Append-only bridge history in one queryable SQLite file.

Three tables, because their shapes genuinely differ: `change` is the history,
`resync` records where that history is knowingly incomplete, and `current`
holds the latest row per resource so "what is the Desk lamp now?" is one
indexed read that survives a restart.

Every extracted column is an index over `payload`, never a replacement for it:
:class:`Change` round trips through ``model_dump_json()``, so a column that
turns out wrong can be recomputed from rows already written.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, final, override

from huepy.models import GroupedLight, Light
from huepy.recording._thread import SinkThread
from huepy.recording.records import ChangeEntry

if TYPE_CHECKING:
    from collections.abc import Sequence

    from huepy.recording.records import HistoryEntry

SCHEMA_VERSION: Final = 1

SCHEMA: Final = """
CREATE TABLE IF NOT EXISTS change (
    id            INTEGER PRIMARY KEY,
    at            TEXT    NOT NULL,
    received_at   TEXT    NOT NULL,
    kind          TEXT    NOT NULL,
    resource_id   TEXT    NOT NULL,
    resource_type TEXT    NOT NULL,
    name          TEXT    NOT NULL,
    room          TEXT,
    origin        TEXT    NOT NULL,
    observation   TEXT    NOT NULL,
    resynced      INTEGER NOT NULL,
    command_id    TEXT,
    on_state      INTEGER,
    brightness    REAL,
    payload       TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS change_at          ON change (at);
CREATE INDEX IF NOT EXISTS change_resource_at ON change (resource_id, at);
CREATE INDEX IF NOT EXISTS change_name_at     ON change (name, at);

CREATE TABLE IF NOT EXISTS resync (
    id          INTEGER PRIMARY KEY,
    reason      TEXT    NOT NULL,
    gap_started TEXT    NOT NULL,
    gap_ended   TEXT    NOT NULL,
    dropped     INTEGER NOT NULL,
    payload     TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS resync_gap ON resync (gap_started);

CREATE TABLE IF NOT EXISTS current (
    resource_id   TEXT PRIMARY KEY,
    at            TEXT    NOT NULL,
    resource_type TEXT    NOT NULL,
    name          TEXT    NOT NULL,
    room          TEXT,
    on_state      INTEGER,
    brightness    REAL,
    payload       TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS current_name ON current (name);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

INSERT_CHANGE: Final = """
INSERT INTO change (
    at, received_at, kind, resource_id, resource_type, name, room,
    origin, observation, resynced, command_id, on_state, brightness, payload
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

INSERT_RESYNC: Final = """
INSERT INTO resync (reason, gap_started, gap_ended, dropped, payload)
VALUES (?, ?, ?, ?, ?)
"""

# The `WHERE` guard is insurance, not a fix for a known bug: batches arrive in
# order today. But `current` is documented as the authoritative "what is it
# now?", and one out-of-order row would silently rewind it.
UPSERT_CURRENT: Final = """
INSERT INTO current (
    resource_id, at, resource_type, name, room, on_state, brightness, payload
) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(resource_id) DO UPDATE SET
    at = excluded.at,
    resource_type = excluded.resource_type,
    name = excluded.name,
    room = excluded.room,
    on_state = excluded.on_state,
    brightness = excluded.brightness,
    payload = excluded.payload
WHERE excluded.at >= current.at
"""

DELETE_CURRENT: Final = "DELETE FROM current WHERE resource_id = ?"


def _stamp(value: datetime) -> str:
    """Render a timestamp so lexicographic order equals chronological order.

    Normalising to UTC is what makes string comparison sound; the explicit
    microseconds stop a whole-second timestamp rendering without a fractional
    part and sorting wrong against its neighbours. Nothing is lost -- the
    original offset survives verbatim in `payload`.
    """
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _power(entry: ChangeEntry) -> tuple[bool | None, float | None]:
    """Extract on/brightness from the resource *after* the transition.

    From `after`, not `delta`: a brightness-only delta would otherwise leave
    `on_state` NULL and silently break "when was it last on?". Read straight
    off the models rather than through ``Light.is_on``, which collapses unknown
    into False -- a lie a history table must not record.
    """
    resource = entry.change.after
    if not isinstance(resource, Light | GroupedLight):
        return None, None
    on = resource.on.on if resource.on is not None else None
    dimming = resource.dimming
    return on, dimming.brightness if dimming is not None else None


@final
class SQLiteSink:
    """Bridge history in a single SQLite file."""

    def __init__(self, path: str | Path) -> None:
        """Record where to write. No file is touched until :meth:`start`."""
        self.path = Path(path)
        self._connection: sqlite3.Connection | None = None
        self._thread = SinkThread("huepy-sqlite")

    async def start(self) -> None:
        """Open the database, apply the schema, and check its version."""
        self._connection = await self._thread.run(self._open)

    def _open(self) -> sqlite3.Connection:
        """Connect on the worker thread that will own the connection.

        Raises:
            RuntimeError: If the file was written by a newer schema.

        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        # WAL so the file stays queryable from the `sqlite3` CLI while huepy
        # writes to it -- the expected workflow. NORMAL survives an application
        # crash and drops one fsync per commit; only a power cut can lose the
        # last transactions, which is the right trade for light history.
        _ = connection.execute("PRAGMA journal_mode=WAL")
        _ = connection.execute("PRAGMA synchronous=NORMAL")
        _ = connection.execute("PRAGMA busy_timeout=5000")
        _ = connection.executescript(SCHEMA)
        row = connection.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
        if row is None:
            _ = connection.execute(
                "INSERT INTO meta (key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            connection.commit()
        elif int(row[0]) > SCHEMA_VERSION:
            connection.close()
            msg = (
                f"{self.path} was written by schema version {row[0]}, "
                f"but this huepy understands {SCHEMA_VERSION}"
            )
            raise RuntimeError(msg)
        return connection

    async def write(self, entries: Sequence[HistoryEntry]) -> None:
        """Persist one batch in a single transaction.

        Raises:
            RuntimeError: If the sink was not started.

        """
        connection = self._connection
        if connection is None:
            msg = f"SQLiteSink({self.path}) is not started"
            raise RuntimeError(msg)
        batch = tuple(entries)

        def commit() -> None:
            changes: list[tuple[Any, ...]] = []
            resyncs: list[tuple[Any, ...]] = []
            for entry in batch:
                if isinstance(entry, ChangeEntry):
                    changes.append(_change_row(entry))
                else:
                    resyncs.append(
                        (
                            entry.resync.reason.value,
                            _stamp(entry.resync.gap_started),
                            _stamp(entry.resync.gap_ended),
                            entry.resync.dropped,
                            entry.resync.model_dump_json(),
                        )
                    )
            with connection:
                if changes:
                    _ = connection.executemany(INSERT_CHANGE, changes)
                if resyncs:
                    _ = connection.executemany(INSERT_RESYNC, resyncs)
                for entry in batch:
                    if isinstance(entry, ChangeEntry):
                        _apply_current(connection, entry)

        await self._thread.run(commit)

    async def close(self) -> None:
        """Close the connection, then release the worker thread."""
        connection, self._connection = self._connection, None
        if connection is not None:
            await self._thread.run(connection.close)
        await self._thread.close()

    @override
    def __repr__(self) -> str:
        """Name the sink by its destination."""
        return f"SQLiteSink({str(self.path)!r})"


def _change_row(entry: ChangeEntry) -> tuple[Any, ...]:
    """Shape one change for the history table."""
    change = entry.change
    on, brightness = _power(entry)
    return (
        _stamp(change.at),
        _stamp(change.received_at),
        change.kind.value,
        change.resource_id,
        change.resource_type,
        entry.name,
        entry.room,
        change.origin,
        change.observation,
        int(change.resynced),
        # sqlite3 cannot bind a UUID.
        str(change.command_id) if change.command_id is not None else None,
        on,
        brightness,
        change.model_dump_json(),
    )


def _apply_current(connection: sqlite3.Connection, entry: ChangeEntry) -> None:
    """Keep the latest-state table in step with one change."""
    change = entry.change
    if change.after is None:
        _ = connection.execute(DELETE_CURRENT, (change.resource_id,))
        return
    on, brightness = _power(entry)
    _ = connection.execute(
        UPSERT_CURRENT,
        (
            change.resource_id,
            _stamp(change.at),
            change.resource_type,
            entry.name,
            entry.room,
            on,
            brightness,
            change.after.model_dump_json(),
        ),
    )
