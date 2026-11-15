from typing import Annotated, Literal
from pydantic import BaseModel, Field, StringConstraints
from app.models.entities import ReferralStatus

# Mandatory free text: whitespace-only is rejected.
RequiredText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]

class ReferralCreate(BaseModel):
    caseId: str  # the case's id, or its case number
    receivingFacilityId: str
    receivingClinicianId: str | None = None
    reason: RequiredText | None = None

class ReferralAssign(BaseModel):
    receivingClinicianId: str

class ReferralAccept(BaseModel):
    note: RequiredText | None = None

class ReferralReject(BaseModel):
    reason: RequiredText

class ReferralEscalate(BaseModel):
    reason: RequiredText

class ReferralReassign(BaseModel):
    receivingFacilityId: str
    reason: RequiredText

class ConsultationOutcome(BaseModel):
    outcome: RequiredText
