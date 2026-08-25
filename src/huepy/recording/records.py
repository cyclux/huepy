"""The enriched records a history sink receives.

Sinks never see the state graph. The recorder resolves topology once and hands
every sink the same self-contained record, so a JSONL line and a SQLite row
carry identical information and no sink has to reach back into the engine.
"""

from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict

from huepy.state.records import Change, Resync


class ChangeEntry(BaseModel):
    """One resource transition, with the names it carried when it happened."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    # Not `kind`: `Change.kind` already means update/add/delete, and a line
    # reading {"kind": "change", "change": {"kind": "update"}} invites misreads.
    record: Literal["change"] = "change"
    change: Change
    name: str
    room: str | None = None


class ResyncEntry(BaseModel):
    """One window where the persisted history is known to be incomplete."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    record: Literal["resync"] = "resync"
    resync: Resync


# Two classes rather than one padded class: the union is real, so the SQLite
# sink maps it onto two tables instead of NULL-padding a single row shape.
type HistoryEntry = ChangeEntry | ResyncEntry
