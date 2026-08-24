"""Serializable records emitted by the bridge state layer."""

from datetime import datetime
from enum import StrEnum
from typing import Any, ClassVar, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, computed_field

from huepy.models import AnyResource


class ChangeKind(StrEnum):
    """Ways one bridge resource can change."""

    UPDATE = "update"
    ADD = "add"
    DELETE = "delete"


class Change(BaseModel):
    """One complete resource transition, ready to persist."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    kind: ChangeKind
    observed_at: AwareDatetime | None = None
    event_at: AwareDatetime | None = None
    received_at: AwareDatetime
    event_id: str | None = None
    resource_id: str
    resource_type: str
    before: AnyResource | None
    after: AnyResource | None
    delta: dict[str, Any]
    resynced: bool = False
    origin: Literal["self", "unattributed"] = "unattributed"
    command_id: UUID | None = None
    command_confirmed: bool | None = None
    observation: Literal["reported", "command_echo"] = "reported"
    transition_ends_at: AwareDatetime | None = None

    @computed_field
    @property
    def at(self) -> datetime:
        """Best feature timestamp while retaining every source timestamp."""
        return self.observed_at or self.event_at or self.received_at


class ResyncReason(StrEnum):
    """Why continuity could not be proved."""

    RECONNECT = "reconnect"
    LAGGED = "lagged"
    INCONSISTENT = "inconsistent"


class Resync(BaseModel):
    """A window where state-history continuity is uncertain."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    reason: ResyncReason
    gap_started: AwareDatetime
    gap_ended: AwareDatetime
    dropped: int = 0
    detail: dict[str, Any] | None = None


class ActiveFade(BaseModel):
    """A transition issued through this state's transport."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    command_id: UUID
    resource_id: str
    target: dict[str, Any]
    sent_at: AwareDatetime
    ends_at: AwareDatetime
    unreliable_until: AwareDatetime
    confirmed: bool | None = None
