"""Workflow actions shared by direct staff calls and AI-suggestion approvals, so both pass the same checks."""
from datetime import datetime, timezone
from fastapi import HTTPException
from app.models.entities import Case, CaseStatus, Facility, Referral, ReferralStatus, Role, User
from app.services.audit import record_event, reject_and_log
from app.services.routing import normalize_specialty
from app.services.delay import delay_reason_missing, DELAY_REQUIRED

ACTIVE_REFERRAL = (ReferralStatus.PENDING.value, ReferralStatus.ACCEPTED.value)

def check_receiving_clinician(db, clinician_id, facility_id) -> User:
    c = db.get(User, clinician_id)
    if not c:
        raise HTTPException(404, "Receiving clinician not found")
    if c.role != Role.RECEIVING_CLINICIAN or not c.is_active or c.facility_id != facility_id:
        raise HTTPException(422, detail={"code": "INVALID_ASSIGNEE", "message": "Must be an active receiving clinician at the receiving facility"})
    return c

def create_case_record(db, user, *, patient, case_number, facility_id, priority, next_action_code=None, next_action_label=None, required_specialty=None, extra_event=None) -> Case:
    if db.query(Case.id).filter(Case.case_number == case_number).first():
        raise HTTPException(409, detail={"code": "DUPLICATE_CASE_ID", "message": f"Case {case_number} already exists"})
    case = Case(case_number=case_number, patient_id=patient.id, facility_id=facility_id, priority=priority, status=CaseStatus.OPEN,
                next_action_code=next_action_code, next_action_label=next_action_label,
                required_specialty=normalize_specialty(required_specialty))
    db.add(case); db.flush()
    record_event(db, user, case_id=case.id, event_type="CASE_CREATED", description="Case created",
                 new_values={"status": "OPEN", **(extra_event or {})})
    return case

def create_referral_record(db, user, case, *, facility_id, clinician_id=None, reason=None, extra_event=None) -> Referral:
    facility = db.get(Facility, facility_id)
    if not facility:
        raise HTTPException(404, "Receiving facility not found")
    if facility.id == case.facility_id:
        raise HTTPException(422, detail={"code": "INVALID_RECEIVING_FACILITY", "message": "Receiving facility must differ from the case's facility"})
    if clinician_id:
        check_receiving_clinician(db, clinician_id, facility.id)
    if db.query(Referral.id).filter(Referral.case_id == case.id, Referral.status.in_(ACTIVE_REFERRAL)).first():
        reject_and_log(db, user, case_id=case.id, attempted="referral:create", status_code=409, code="REFERRAL_ALREADY_ACTIVE", message="This case already has an active referral")
    if case.status != CaseStatus.ASSIGNED:  # ASSIGNED -> REFERRED is the only transition into REFERRED
        reject_and_log(db, user, case_id=case.id, attempted=f"{case.status.value}->REFERRED", status_code=409, code="INVALID_STATUS_TRANSITION", message=f"Cannot move {case.status.value} to REFERRED")
    if delay_reason_missing(case):
        reject_and_log(db, user, case_id=case.id, attempted="referral:create", **DELAY_REQUIRED)
    r = Referral(case_id=case.id, status=ReferralStatus.PENDING.value, receiving_facility_id=facility.id,
                 receiving_clinician_id=clinician_id, referred_by_id=user.id, reason=reason)
    db.add(r); db.flush()
    old_status = case.status.value
    case.status = CaseStatus.REFERRED; case.updated_at = datetime.now(timezone.utc)
    record_event(db, user, case_id=r.case_id, event_type="REFERRAL_CREATED", description="Referral created",
                 old_values={"referralId": r.id},
                 new_values={"referralId": r.id, "status": r.status, "receivingFacilityId": facility.id, "receivingClinicianId": clinician_id, "reason": reason, **(extra_event or {})})
    record_event(db, user, case_id=case.id, event_type="CASE_STATUS_CHANGED", description="Case status changed by referral",
                 old_values={"status": old_status}, new_values={"status": "REFERRED", "referralId": r.id})
    return r
