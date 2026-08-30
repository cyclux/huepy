"""Record every bridge change to a queryable SQLite file, then ask it a question.

    python examples/record_history.py            # record until Ctrl-C
    python examples/record_history.py "Desk lamp"   # query what was recorded

Recording is the `record=` argument; everything else here is the payoff.
"""

import asyncio
import sqlite3
import sys
from pathlib import Path

from huepy import Hue, SQLiteSink

DATABASE = Path("hue-history.sqlite3")

LAST_ON = """
    SELECT at FROM change
    WHERE name = ? AND on_state = 1
    ORDER BY at DESC LIMIT 1
"""


async def record() -> None:
    """Persist changes and uncertainty markers until interrupted."""
    # `record=` implies `state=True`, and the sink does its writing on its own
    # thread, so a slow disk never stalls the event stream.
    async with Hue(record=SQLiteSink(DATABASE)):
        print(f"Recording to {DATABASE}. Ctrl-C to stop.")
        await asyncio.Event().wait()


def last_on(name: str) -> None:
    """Answer a question the recorded schema was designed for."""
    if not DATABASE.exists():
        print(f"No recording yet. Run this without arguments to create {DATABASE}.")
        raise SystemExit(1)
    # WAL mode, so this works while a recorder is still writing the file.
    connection = sqlite3.connect(f"file:{DATABASE}?mode=ro", uri=True)
    try:
        row = connection.execute(LAST_ON, (name,)).fetchone()
    finally:
        connection.close()
    print(f"{name} was last on at {row[0]}" if row else f"No record of {name} on")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        last_on(sys.argv[1])
    else:
        asyncio.run(record())
