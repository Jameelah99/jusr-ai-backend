from datetime import datetime, timezone
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from app.core.database import get_db
from app.core.permissions import CREATE_ROLES, can_modify_case, can_view_case, visible_referrals
from app.api.v1.deps import clinical_user
from app.models.entities import Case, CaseStatus, Consultation, Facility, Patient, Referral, ReferralStatus, Role, Screening, User
from app.schemas.referrals import (ConsultationOutcome, ReferralAccept, ReferralAssign, ReferralCreate,
                                   ReferralEscalate, ReferralReassign, ReferralReject)
from app.services.audit import record_event, reject_and_log
from app.services.workflow import ACTIVE_REFERRAL, check_receiving_clinician, create_referral_record

router = APIRouter()

# ---------- helpers ----------

def _now():
    return datetime.now(timezone.utc)

def _brief(obj, *fields):
    return {f: getattr(obj, f) for f in fields} if obj else None

def serialize(db: Session, r: Referral) -> dict:
    case = db.get(Case, r.case_id)
    patient = db.get(Patient, case.patient_id)
    screening = db.get(Screening, case.screening_id) if case.screening_id else None
    clinician = db.get(User, r.receiving_clinician_id) if r.receiving_clinician_id else None
    referrer = db.get(User, r.referred_by_id) if r.referred_by_id else None
    consultation = db.query(Consultation).filter(Consultation.referral_id == r.id).first()
    return {
        "id": r.id, "status": r.status,
        "case": {"id": case.id, "caseId": case.case_number, "priority": case.priority.value, "status": case.status.value,
                 "patient": {"id": patient.id, "displayLabel": patient.display_label},
                 "screening": ({"type": screening.screening_type, "outcome": screening.outcome, "displayLabel": screening.display_label} if screening else None)},
        "referringFacility": _brief(db.get(Facility, case.facility_id), "id", "name"),
        "receivingFacility": _brief(db.get(Facility, r.receiving_facility_id), "id", "name") if r.receiving_facility_id else None,
        "receivingClinician": {"id": clinician.id, "displayName": clinician.display_name} if clinician else None,
        "referredById": r.referred_by_id,
        "referredBy": {"id": referrer.id, "displayName": referrer.display_name, "role": referrer.role.value} if referrer else None, "reason": r.reason, "rejectionReason": r.rejection_reason,
        "escalated": r.escalated, "escalationReason": r.escalation_reason, "escalatedAt": r.escalated_at,
        "decidedAt": r.decided_at, "createdAt": r.created_at, "updatedAt": r.updated_at,
        "consultation": ({"id": consultation.id, "outcome": consultation.notes, "recordedById": consultation.clinician_id, "recordedAt": consultation.created_at} if consultation else None),
    }

def load_referral(db: Session, user, referral_id: str) -> tuple[Referral, Case]:
    row = visible_referrals(db.query(Referral, Case).join(Case, Referral.case_id == Case.id), user).filter(Referral.id == referral_id).first()
    if not row:
        raise HTTPException(404, "Referral not found")
    return row

def _referrer_side(user, case) -> bool:
    return user.role in CREATE_ROLES and can_modify_case(user, case)

def _receiving_coordinator(user, r) -> bool:
    return user.role == Role.NATIONAL_ADMIN or (user.role == Role.COORDINATOR and user.facility_id is not None and user.facility_id == r.receiving_facility_id)

def _can_decide(user, r) -> bool:
    """Accepting or rejecting is a clinical decision by a receiving clinician at the receiving facility."""
    return (user.role == Role.RECEIVING_CLINICIAN and user.facility_id is not None and user.facility_id == r.receiving_facility_id
            and r.receiving_clinician_id in (None, user.id))

def _forbid():
    raise HTTPException(403, "Insufficient permissions")

def _invalid_state(db, user, r, attempted, message):
    reject_and_log(db, user, case_id=r.case_id, attempted=f"referral:{attempted}", status_code=409, code="INVALID_REFERRAL_STATE", message=message)

def _event(db, user, r, event_type, description, old=None, new=None):
    record_event(db, user, case_id=r.case_id, event_type=event_type, description=description,
                 old_values={"referralId": r.id, **(old or {})}, new_values={"referralId": r.id, **(new or {})})

def _done(db, r):
    r.updated_at = _now()
    db.commit(); db.refresh(r)
    return {"success": True, "data": serialize(db, r), "meta": {}}

# ---------- reads ----------

@router.get("")
def list_referrals(page: int = Query(1, ge=1), pageSize: int = Query(20, ge=1, le=100),
                   status: ReferralStatus | None = None, direction: Literal["incoming", "outgoing"] | None = None,
                   escalated: bool | None = None, caseId: str | None = None,
                   db: Session = Depends(get_db), user=Depends(clinical_user)):
    q = visible_referrals(db.query(Referral).join(Case, Referral.case_id == Case.id), user)
    if status: q = q.filter(Referral.status == status.value)
    if escalated is not None: q = q.filter(Referral.escalated == escalated)
    if caseId: q = q.filter((Case.id == caseId) | (Case.case_number == caseId))
    if direction == "incoming": q = q.filter(Referral.receiving_facility_id == user.facility_id)
    if direction == "outgoing": q = q.filter(Case.facility_id == user.facility_id)
    total = q.count()
    rows = q.order_by(Referral.created_at.desc(), Referral.id).offset((page - 1) * pageSize).limit(pageSize).all()
    return {"success": True, "data": [serialize(db, r) for r in rows], "meta": {"total": total, "page": page, "pageSize": pageSize}}

@router.get("/{referral_id}")
def get_referral(referral_id: str, db: Session = Depends(get_db), user=Depends(clinical_user)):
    r, _ = load_referral(db, user, referral_id)
    return {"success": True, "data": serialize(db, r), "meta": {}}

# ---------- operations ----------

@router.post("")
def create_referral(payload: ReferralCreate, db: Session = Depends(get_db), user=Depends(clinical_user)):
    case = db.query(Case).filter((Case.id == payload.caseId) | (Case.case_number == payload.caseId)).first()
    if not case or not can_view_case(user, case, db): raise HTTPException(404, "Case not found")
    if not _referrer_side(user, case): _forbid()
    r = create_referral_record(db, user, case, facility_id=payload.receivingFacilityId, clinician_id=payload.receivingClinicianId, reason=payload.reason)
    return _done(db, r)

@router.post("/{referral_id}/assign")
def assign_clinician(referral_id: str, payload: ReferralAssign, db: Session = Depends(get_db), user=Depends(clinical_user)):
    r, case = load_referral(db, user, referral_id)
    if not _receiving_coordinator(user, r): _forbid()
    if r.status not in ACTIVE_REFERRAL: _invalid_state(db, user, r, "assign", f"Cannot assign a clinician to a {r.status} referral")
    clinician = check_receiving_clinician(db, payload.receivingClinicianId, r.receiving_facility_id)
    old = r.receiving_clinician_id
    r.receiving_clinician_id = clinician.id
    _event(db, user, r, "REFERRAL_CLINICIAN_ASSIGNED", "Receiving clinician assigned", old={"receivingClinicianId": old}, new={"receivingClinicianId": clinician.id})
    return _done(db, r)

@router.post("/{referral_id}/accept")
def accept_referral(referral_id: str, payload: ReferralAccept | None = None, db: Session = Depends(get_db), user=Depends(clinical_user)):
    r, case = load_referral(db, user, referral_id)
    if not _can_decide(user, r): _forbid()
    if r.status != ReferralStatus.PENDING.value: _invalid_state(db, user, r, "accept", f"Cannot accept a {r.status} referral")
    r.status = ReferralStatus.ACCEPTED.value; r.receiving_clinician_id = user.id; r.decided_at = _now()
    _event(db, user, r, "REFERRAL_ACCEPTED", "Referral accepted", old={"status": "PENDING"}, new={"status": r.status, "note": payload.note if payload else None})
    return _done(db, r)

@router.post("/{referral_id}/reject")
def reject_referral(referral_id: str, payload: ReferralReject, db: Session = Depends(get_db), user=Depends(clinical_user)):
    r, case = load_referral(db, user, referral_id)
    if not _can_decide(user, r): _forbid()
    if r.status != ReferralStatus.PENDING.value: _invalid_state(db, user, r, "reject", f"Cannot reject a {r.status} referral")
    r.status = ReferralStatus.REJECTED.value; r.rejection_reason = payload.reason; r.receiving_clinician_id = user.id; r.decided_at = _now()
    _event(db, user, r, "REFERRAL_REJECTED", "Referral rejected", old={"status": "PENDING"}, new={"status": r.status, "reason": payload.reason})
    return _done(db, r)

@router.post("/{referral_id}/escalate")
def escalate_referral(referral_id: str, payload: ReferralEscalate, db: Session = Depends(get_db), user=Depends(clinical_user)):
    r, case = load_referral(db, user, referral_id)
    if not _referrer_side(user, case): _forbid()
    if r.status != ReferralStatus.PENDING.value: _invalid_state(db, user, r, "escalate", f"Cannot escalate a {r.status} referral")
    if r.escalated: _invalid_state(db, user, r, "escalate", "Referral is already escalated")
    r.escalated = True; r.escalation_reason = payload.reason; r.escalated_at = _now()
    _event(db, user, r, "REFERRAL_ESCALATED", "Referral escalated", new={"escalated": True, "reason": payload.reason})
    return _done(db, r)

@router.post("/{referral_id}/reassign")
def reassign_referral(referral_id: str, payload: ReferralReassign, db: Session = Depends(get_db), user=Depends(clinical_user)):
    r, case = load_referral(db, user, referral_id)
    if not _referrer_side(user, case): _forbid()
    if r.status not in (ReferralStatus.PENDING.value, ReferralStatus.REJECTED.value):
        _invalid_state(db, user, r, "reassign", f"Cannot reassign a {r.status} referral")
    facility = db.get(Facility, payload.receivingFacilityId)
    if not facility: raise HTTPException(404, "Receiving facility not found")
    if facility.id == case.facility_id or facility.id == r.receiving_facility_id:
        raise HTTPException(422, detail={"code": "INVALID_RECEIVING_FACILITY", "message": "Choose a facility other than the referring and current receiving facilities"})
    old = {"status": r.status, "receivingFacilityId": r.receiving_facility_id, "receivingClinicianId": r.receiving_clinician_id}
    r.receiving_facility_id = facility.id; r.receiving_clinician_id = None; r.status = ReferralStatus.PENDING.value
    r.rejection_reason = None; r.decided_at = None; r.escalated = False; r.escalation_reason = None; r.escalated_at = None
    _event(db, user, r, "REFERRAL_REASSIGNED", "Referral reassigned to another facility", old=old, new={"status": r.status, "receivingFacilityId": facility.id, "reason": payload.reason})
    return _done(db, r)

@router.post("/{referral_id}/consultation")
def record_consultation(referral_id: str, payload: ConsultationOutcome, db: Session = Depends(get_db), user=Depends(clinical_user)):
    r, case = load_referral(db, user, referral_id)
    if user.role != Role.RECEIVING_CLINICIAN or r.receiving_clinician_id != user.id: _forbid()
    if r.status != ReferralStatus.ACCEPTED.value: _invalid_state(db, user, r, "consultation", f"Consultation outcome can only be recorded on an ACCEPTED referral, not {r.status}")
    if db.query(Consultation.id).filter(Consultation.referral_id == r.id).first():
        reject_and_log(db, user, case_id=r.case_id, attempted="referral:consultation", status_code=409, code="CONSULTATION_ALREADY_RECORDED", message="A consultation outcome is already recorded for this referral")
    c = Consultation(case_id=r.case_id, clinician_id=user.id, referral_id=r.id, notes=payload.outcome)
    db.add(c); db.flush()
    _event(db, user, r, "CONSULTATION_RECORDED", "Consultation outcome recorded", new={"consultationId": c.id, "outcome": payload.outcome})
    return _done(db, r)
