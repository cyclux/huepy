"""A single-threaded executor for sinks that do blocking I/O.

Not :func:`asyncio.to_thread`: that uses the loop's shared pool, so successive
calls land on different threads and ``sqlite3.Connection``, which defaults to
``check_same_thread=True``, raises ``ProgrammingError``. Passing
``check_same_thread=False`` would silence the tripwire for exactly this bug, so
each sink owns one thread instead. Serialising writes is a bonus, not a
workaround: correctness no longer depends on the caller never overlapping them.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, final, override

if TYPE_CHECKING:
    from collections.abc import Callable


@final
class SinkThread:
    """One worker thread, owned for the lifetime of one sink."""

    def __init__(self, name: str) -> None:
        """Name the worker without starting a thread."""
        self._name = name
        self._executor: ThreadPoolExecutor | None = None

    async def run[T](self, work: Callable[[], T]) -> T:
        """Run ``work`` on this sink's thread and await its result.

        Args:
            work: A zero-argument callable doing the blocking work. Give it
                everything, including serialisation: ``model_dump_json`` over a
                batch is milliseconds that belong off the event loop too.

        Returns:
            Whatever ``work`` returned.

        """
        loop = asyncio.get_running_loop()
        if self._executor is None:
            # Rebuilt on demand rather than in __init__, so a sink survives a
            # close/start cycle. `HueState` supports being re-entered, and a
            # client configured with `record=` must not be the one thing that
            # cannot be restarted.
            self._executor = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix=self._name
            )
        return await loop.run_in_executor(self._executor, work)

    async def close(self) -> None:
        """Wait for queued work to finish, then release the thread."""
        executor, self._executor = self._executor, None
        if executor is not None:
            await asyncio.to_thread(executor.shutdown, wait=True)

    @override
    def __repr__(self) -> str:
        """Describe the worker without exposing the executor's identity."""
        return f"{type(self).__name__}({self._name!r})"
