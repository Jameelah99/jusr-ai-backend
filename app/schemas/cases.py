
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.models.entities import CaseStatus, Priority, DelayReason


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


class CaseCreate(BaseModel):
    patientId: str
    caseId: str = Field(min_length=1, max_length=40)
    facilityId: str | None = None
    priority: Priority = Priority.NORMAL
    nextActionCode: str | None = Field(default=None, max_length=80)
    nextActionLabel: str | None = Field(default=None, max_length=200)
    requiredSpecialty: str | None = Field(default=None, max_length=60)


class CaseAssign(BaseModel):
    coordinatorId: str


ClosureType = Literal[
    "care_confirmed",
    "not_actionable",
    "no_further_care_needed",
    "patient_declined",
    "unreachable",
    "moved_or_transferred",
    "other",
]


class ClosureEvidenceItem(BaseModel):
    id: str
    kind: str
    label: str
    present: bool
    sourceRecordId: str | None = None
    sourceDescription: str | None = None


class CaseTransition(BaseModel):
    status: CaseStatus

    # Rule A-01: resolving or closing a case needs evidence or a recorded decision.
    evidence: str | None = Field(default=None, max_length=2000)
    decision: str | None = Field(default=None, max_length=2000)

    # Case closure details.
    closureType: ClosureType | None = None
    reason: str | None = Field(default=None, max_length=2000)
    closureEvidence: list[ClosureEvidenceItem] = Field(default_factory=list)
    secondApproverStaffId: str | None = None

    @field_validator("evidence", "decision", "reason")
    @classmethod
    def blank_to_none(cls, value):
        return _clean(value)

    @field_validator("secondApproverStaffId")
    @classmethod
    def clean_approver_id(cls, value):
        return _clean(value)

    @model_validator(mode="after")
    def validate_closure_details(self):
        if self.status == CaseStatus.CLOSED:
            if not self.closureType:
                raise ValueError("A closure type is required")
            if not self.reason:
                raise ValueError("A closure reason is required")
            if self.closureType == "other" and not self.secondApproverStaffId:
                raise ValueError(
                    "A second authorised approver is required for this closure type"
                )
        return self


class DelayReasonRecord(BaseModel):
    code: DelayReason
    note: str | None = Field(default=None, max_length=1000)

    @field_validator("note")
    @classmethod
    def blank_note(cls, value):
        return _clean(value)

    @model_validator(mode="after")
    def other_needs_note(self):
        if self.code == DelayReason.OTHER and not self.note:
            raise ValueError("A note is required when the code is OTHER")
        return self
