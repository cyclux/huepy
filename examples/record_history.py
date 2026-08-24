"""Persist state changes and explicit uncertainty windows to SQLite."""

import asyncio
import sqlite3
from pathlib import Path

from huepy import Hue
from huepy.state import Change, Resync

DATABASE = Path("hue-history.sqlite3")


def open_database() -> sqlite3.Connection:
    connection = sqlite3.connect(DATABASE)
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS history (
            received_at TEXT NOT NULL,
            record_type TEXT NOT NULL,
            resource_id TEXT,
            resource_type TEXT,
            name TEXT,
            room TEXT,
            payload TEXT NOT NULL
        )
        """
    )
    return connection


async def main() -> None:
    database = open_database()
    try:
        async with Hue() as hue, hue.state() as state:
            async for item in state.changes():
                if isinstance(item, Change):
                    room = state.room_of(item.resource_id)
                    values = (
                        item.received_at.isoformat(),
                        "change",
                        item.resource_id,
                        item.resource_type,
                        state.name_of(item.resource_id),
                        room.name if room is not None else None,
                        item.model_dump_json(),
                    )
                elif isinstance(item, Resync):
                    values = (
                        item.gap_ended.isoformat(),
                        "resync",
                        None,
                        None,
                        None,
                        None,
                        item.model_dump_json(),
                    )
                else:  # pragma: no cover - the public union is exhaustive
                    continue
                database.execute(
                    "INSERT INTO history VALUES (?, ?, ?, ?, ?, ?, ?)", values
                )
                database.commit()
    finally:
        database.close()


if __name__ == "__main__":
    asyncio.run(main())
