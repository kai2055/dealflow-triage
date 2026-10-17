"""The  pitch record: the fixed shape of every incoming pitch is  turned into."""

from datetime import datetime
from enum import Enum
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

SHA256_PATTERN = r"^[0-9a-f]{64}$"

class PitchState(str, Enum):
    RECEIVED = "received"
    NOT_A_PITCH = "not_a_pitch"
    EXTRACTED = "extracted"
    SCREENED = "screened"
    ENRICHED = "enriched"
    WRITTEN = "wirtten"
    FAILED = "failed"


class Source(str, Enum):
    FOLDER = "folder"
    GMAIL = "gmail"



class Attachment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sha256: str = Field(pattern=SHA256_PATTERN)
    filename: str
    detected_type: str
    size_bytes: int = Field(ge=0)
    supported: bool


class RawPitch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pitch_id: UUID = Field(default_factory=uuid4)
    source: Source
    raw_hash: str = Field(pattern=SHA256_PATTERN)
    message_id: str | None = None
    received_at: AwareDatetime
    sender: str | None = None
    subject: str | None = None
    body_text: str | None = None
    attachments: list[Attachment] = Field(default_factory=list)
    links: list[str] = Field(default_factory=list)
    state: PitchState = PitchState.RECEIVED
    
