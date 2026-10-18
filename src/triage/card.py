"""The pitch card: what a deck says, field by field, each with the quote and page it came from."""

from enum import Enum
from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

T = TypeVar("T")


class Evidence(BaseModel):
    """Where a fact comes from: the exact words and the page they are on."""

    model_config = ConfigDict(extra="forbid")

    quote: str = Field(min_length=1)
    page: int = Field(ge=1)


class Fact(BaseModel, Generic[T]):
    """One fact from the deck: a value with its evidence, or both empty (not in the deck)."""

    model_config = ConfigDict(extra="forbid")

    value: T | None = None
    evidence: Evidence | None = None

    @model_validator(mode="after")
    def value_and_evidence_go_together(self):
        if (self.value is None) != (self.evidence is None):
            raise ValueError("a value needs a quote, and a quote needs a value")
        return self

    @property
    def is_filled(self) -> bool:
        return self.value is not None


class Stage(str, Enum):
    PRE_SEED = "pre_seed"
    SEED = "seed"
    SERIES_A = "series_a"
    SERIES_B_OR_LATER = "series_b_or_later"


class RoundStatus(str, Enum):
    SOUGHT = "sought"
    RAISED = "raised"


class RoundSize(BaseModel):
    model_config = ConfigDict(extra="forbid")

    amount: float = Field(gt=0)
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")  # None: only "$" or no currency written (decision 67)
    status: RoundStatus


class Person(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    role: str | None = None
    evidence: Evidence


class ClaimKind(str, Enum):
    REVENUE = "revenue"
    CUSTOMERS = "customers"
    CONTRACTS = "contracts"
    PIPELINE = "pipeline"
    PARTNERSHIP = "partnership"
    POTENTIAL_PARTNERSHIP = "potential_partnership"
    OTHER = "other"


class Claim(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: ClaimKind
    text: str = Field(min_length=1)
    evidence: Evidence


class PitchCard(BaseModel):
    """What one deck says. Empty means the deck does not say it."""

    model_config = ConfigDict(extra="forbid")

    company_name: Fact[str] = Field(default_factory=Fact[str])
    website: Fact[str] = Field(default_factory=Fact[str])
    one_liner: Fact[str] = Field(default_factory=Fact[str])
    sector: Fact[str] = Field(default_factory=Fact[str])
    stage: Fact[Stage] = Field(default_factory=Fact[Stage])
    round_size: Fact[RoundSize] = Field(default_factory=Fact[RoundSize])
    headquarters: Fact[str] = Field(default_factory=Fact[str])
    product: Fact[str] = Field(default_factory=Fact[str])
    ai_role: Fact[str] = Field(default_factory=Fact[str])
    team: list[Person] = Field(default_factory=list)
    traction: list[Claim] = Field(default_factory=list)

    def completeness(self) -> float:
        """Share of the 11 fields that the deck filled, from 0.0 to 1.0."""
        facts = [self.company_name, self.website, self.one_liner, self.sector, self.stage,
                 self.round_size, self.headquarters, self.product, self.ai_role]
        filled = sum(f.is_filled for f in facts) + bool(self.team) + bool(self.traction)
        return round(filled / (len(facts) + 2), 2)