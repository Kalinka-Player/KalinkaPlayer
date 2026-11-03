"""Optional receiver management, independent of browsing and media transport.

Commands finish when their bounded transition is complete. Opening pairing
returns an open window; it never waits for a phone or a renderer to connect.
"""

from typing import Annotated, Literal, Protocol, TypeAlias

from pydantic import BaseModel, ConfigDict, Field

CommandName: TypeAlias = Literal[
    "open_pairing", "close_pairing", "grant_audio", "revoke_audio", "disconnect_audio"
]


class ReceiverDescriptor(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    version: Literal[1] = 1
    title: str
    kind: str
    instructions: str = ""
    commands: tuple[CommandName, ...] = ()


class ReceiverAction(BaseModel):
    command: CommandName
    available: bool = True
    reason: str | None = None


class PairingState(BaseModel):
    window_id: str
    remaining_seconds: float = Field(ge=0, le=120)


class ReceiverPeer(BaseModel):
    peer_id: str
    name: str
    paired: bool = True
    audio_allowed: bool = False
    connected: bool = False


class ReceiverSession(BaseModel):
    session_id: str
    name: str
    peer_id: str | None = None
    playing: bool = False


class ReceiverSnapshot(BaseModel):
    version: Literal[1] = 1
    receiver_id: str = ""  # Core supplies identity from the owning plugin.
    title: str = ""
    kind: str = ""
    instructions: str = ""
    enabled: bool
    state: Literal["disabled", "starting", "ready", "stopping", "error"]
    message: str | None = None
    effective_name: str | None = None
    name_inherited: bool = True
    visibility: Literal["hidden", "advertising", "unknown"] = "unknown"
    pairing: PairingState | None = None
    recent_peer_id: str | None = None
    peers: list[ReceiverPeer] = Field(default_factory=list)
    sessions: list[ReceiverSession] = Field(default_factory=list)
    actions: list[ReceiverAction] = Field(default_factory=list)


class ReceiversSnapshot(BaseModel):
    version: Literal[1] = 1
    receivers: list[ReceiverSnapshot] = Field(default_factory=list)


class _Command(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OpenPairing(_Command):
    command: Literal["open_pairing"]


class ClosePairing(_Command):
    command: Literal["close_pairing"]
    window_id: str = Field(min_length=1, max_length=128)


class GrantAudio(_Command):
    command: Literal["grant_audio"]
    peer_id: str = Field(min_length=1, max_length=128)


class RevokeAudio(_Command):
    command: Literal["revoke_audio"]
    peer_id: str = Field(min_length=1, max_length=128)


class DisconnectAudio(_Command):
    command: Literal["disconnect_audio"]
    session_id: str = Field(min_length=1, max_length=128)


ReceiverCommand: TypeAlias = Annotated[
    OpenPairing | ClosePairing | GrantAudio | RevokeAudio | DisconnectAudio,
    Field(discriminator="command"),
]


class ReceiverError(Exception):
    """A readable, expected refusal; Core maps the code to an HTTP status."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class ReceiverControl(Protocol):
    async def snapshot(self) -> ReceiverSnapshot: ...
    async def execute(self, command: ReceiverCommand) -> ReceiverSnapshot: ...
