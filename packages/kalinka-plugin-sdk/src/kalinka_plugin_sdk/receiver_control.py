"""Optional receiver management, independent of browsing and media transport.

Commands finish when their bounded transition is complete. Opening pairing
returns an open window; it never waits for a phone or a renderer to connect.
"""

from typing import Annotated, Literal, Protocol, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, StrictBool

CommandName: TypeAlias = Literal[
    "open_pairing",
    "close_pairing",
    "answer_pairing",
    "grant_audio",
    "revoke_audio",
    "disconnect_audio",
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


class PairingRequest(BaseModel):
    """One real agent callback. Codes are strings, including leading zeroes."""

    request_id: str
    device_name: str
    method: Literal[
        "confirmation",
        "authorization",
        "pin",
        "passkey",
        "display_pin",
        "display_passkey",
    ]
    code: str | None = Field(default=None, repr=False)
    entered: int | None = Field(default=None, ge=0, le=6)
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
    pairing_request: PairingRequest | None = None
    password_required: bool = False
    password_reveal_available: bool = False
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


class AnswerPairing(_Command):
    command: Literal["answer_pairing"]
    request_id: str = Field(min_length=1, max_length=128)
    accept: StrictBool
    code: str | None = Field(default=None, min_length=1, max_length=16, repr=False)


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
    OpenPairing
    | ClosePairing
    | AnswerPairing
    | GrantAudio
    | RevokeAudio
    | DisconnectAudio,
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


class ReceiverPasswordControl(Protocol):
    """Optional explicit reveal of a receiver's shareable connection password.

    Uses Core's management API access, never public config or receiver events.
    Advertise password_reveal_available only when the applied secret is usable.
    Other plugin credentials must never be exposed through this interface.
    """

    async def reveal_password(self) -> str: ...
