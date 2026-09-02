"""Replace the example fields with the smallest flat analytical record for the project."""

from datetime import date
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ContractModel(BaseModel):
    """Reject silent model-output drift."""

    model_config = ConfigDict(extra="forbid")


class DocumentStatus(StrEnum):
    TARGET = "target"
    NO_RELEVANT_MATERIAL = "no_relevant_material"
    UNCERTAIN = "uncertain"


class ScanQuality(StrEnum):
    CLEAR = "clear"
    DIFFICULT = "difficult"
    UNREADABLE = "unreadable"


class ValueStatus(StrEnum):
    """Visible source state; never collapse these distinctions during extraction."""

    OBSERVED = "observed"
    BLANK = "blank"
    DASH = "dash"
    TEXTUAL_NONE = "textual_none"
    UNREADABLE = "unreadable"
    NOT_APPLICABLE = "not_applicable"


class Record(ContractModel):
    """One example entity-period-value observation; customize before calibration."""

    entity_raw: str = Field(description="Exact visible entity label, including punctuation and readable damage.")
    entity: str | None = Field(description="Conservative cleaned label, or null when it cannot be resolved from the page.")
    period_raw: str = Field(description="Exact visible date/period text or readable fragment.")
    period: date | None = Field(description="Normalized date only at the precision visibly supported by the source.")
    value_raw: str = Field(description="Exact visible value token; empty only for a truly blank field.")
    value: Decimal | None = Field(description="Normalized numeric value, or null for blank, dash, damaged, or unsupported values.")
    value_status: ValueStatus = Field(description="Explicit visible state of the value cell; zero is observed, never blank.")
    correction_raw: str | None = Field(description="Exact original and replacement wording for a visible correction, otherwise null.")
    note: str | None = Field(description="Exact substantive note or short uncertainty explanation, otherwise null.")
    supplemental_facts: list[str] = Field(default_factory=list, description="Substantive visible facts that do not fit a standard field.")
    uncertain_fields: list[str] = Field(default_factory=list, description="Names of entered fields that cannot be read confidently.")

    @model_validator(mode="after")
    def normalized_values_require_source_text(self) -> "Record":
        if self.entity is not None and not self.entity_raw:
            raise ValueError("entity requires entity_raw")
        if self.period is not None and not self.period_raw:
            raise ValueError("period requires period_raw")
        if self.value is not None and not self.value_raw:
            raise ValueError("value requires value_raw")
        if self.value_status == ValueStatus.OBSERVED and self.value is None:
            raise ValueError("observed value_status requires a normalized value")
        if self.value_status != ValueStatus.OBSERVED and self.value is not None:
            raise ValueError("non-observed value_status requires value=null")
        if self.value_status == ValueStatus.BLANK and self.value_raw:
            raise ValueError("blank value_status requires value_raw to be empty")
        return self


class PageExtraction(ContractModel):
    """Complete response for one supplied page image."""

    document_status: DocumentStatus
    scan_quality: ScanQuality
    records: list[Record]
    page_note: str | None = Field(description="Page-wide uncertainty or relevance explanation, otherwise null.")
    unmapped_text: list[str] = Field(default_factory=list, description="Substantive entered text not represented elsewhere.")

    @model_validator(mode="after")
    def status_matches_records(self) -> "PageExtraction":
        if self.document_status == DocumentStatus.TARGET and not self.records:
            raise ValueError("a target page must contain at least one record")
        if self.document_status != DocumentStatus.TARGET and self.records:
            raise ValueError("a non-target or uncertain page must not contain records")
        return self
