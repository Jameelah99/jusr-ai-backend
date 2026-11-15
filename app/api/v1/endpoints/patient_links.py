"""Staff side of patient access: generate/resend links, record assisted responses, read responses."""
import secrets
from datetime import timedelta
from app.core import clock
from uuid import uuid4
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from app.core.config import settings
from app.services import settings_store
from app.core.database import get_db
from app.core.permissions import CREATE_ROLES, can_modify_case, can_view_case
from app.api.v1.deps import clinical_user
from app.api.v1.endpoints.patient_portal import hash_token, needs_verification
from app.models.entities import Case, CaseStatus, LinkStatus, Patient, PatientLink, PatientResponse, User
from app.schemas.patient_access import AssistedResponseCreate
from app.services.audit import record_event, reject_and_log
from app.services.tasks import create_task
from app.services import state_machine as sm
from app.services.workflow import after_patient_response

router = APIRouter()

def _case_for(db, user, case_id, *, modify):
    from app.api.v1.endpoints.cases import find_case
    case = find_case(db, case_id)
    if not case or not can_view_case(user, case, db): raise HTTPException(404, "Case not found")
    if modify and (user.role not in CREATE_ROLES or not can_modify_case(user, case)): raise HTTPException(403, "Insufficient permissions")
    return case

def _link_status(link: PatientLink) -> str:
    if link.status == LinkStatus.ACTIVE.value and link.expires_at <= clock.now():
        return "EXPIRED"
    return link.status

@router.post("/{case_id}/links")
def generate_link(case_id: str, db: Session = Depends(get_db), user=Depends(clinical_user)):
    """Generate a secure link, or resend: any earlier live link for the case is cancelled."""
    case = _case_for(db, user, case_id, modify=True)
    if case.status == CaseStatus.CLOSED:
        reject_and_log(db, user, case_id=case.id, attempted="link:generate", status_code=409, code="CASE_CLOSED", message="Cannot generate a link for a closed case")
    patient = db.get(Patient, case.patient_id)
    now = clock.now()
    previous = db.query(PatientLink).filter(PatientLink.case_id == case.id, PatientLink.status.in_([LinkStatus.ACTIVE.value, LinkStatus.LOCKED.value])).all()
    for old in previous:
        old.status = LinkStatus.SUPERSEDED.value; old.superseded_at = now
    token = secrets.token_urlsafe(32)  # 256 bits of randomness; nothing about the patient is in the URL
    link = PatientLink(case_id=case.id, patient_id=patient.id, token_hash=hash_token(token), created_by_id=user.id,
                       expires_at=now + timedelta(days=settings.patient_link_expiry_days))
    db.add(link); db.flush()
    record_event(db, user, case_id=case.id, event_type="LINK_RESENT" if previous else "LINK_GENERATED",
                 description="Patient link resent" if previous else "Patient link generated",
                 new_values={"linkId": link.id, "expiresAt": link.expires_at.isoformat(), "supersededLinkIds": [o.id for o in previous]})
    db.commit()
    return {"success": True, "data": {"linkId": link.id, "token": token, "url": f"{settings.patient_link_base_url.rstrip('/')}/p/{token}",
                                      "expiresAt": link.expires_at, "verificationRequired": needs_verification(patient), "resent": bool(previous),
                                      "note": "The token is shown once and cannot be retrieved again."}, "meta": {}}

@router.get("/{case_id}/links")
def list_links(case_id: str, db: Session = Depends(get_db), user=Depends(clinical_user)):
    case = _case_for(db, user, case_id, modify=False)
    rows = db.query(PatientLink).filter(PatientLink.case_id == case.id).order_by(PatientLink.created_at.desc()).all()
    return {"success": True, "data": [{"id": l.id, "status": _link_status(l), "createdAt": l.created_at, "expiresAt": l.expires_at, "failedAttempts": l.failed_attempts,
                                       "lockedAt": l.locked_at, "verifiedAt": l.verified_at, "lastAccessedAt": l.last_accessed_at, "createdById": l.created_by_id} for l in rows], "meta": {}}

def _response_out(db, r: PatientResponse):
    staff = db.get(User, r.recorded_by_id) if r.recorded_by_id else None
    return {"id": r.id, "kind": r.kind, "value": r.value, "source": r.source, "submissionId": r.submission_id, "note": r.note, "recordedAt": r.created_at,
            "recordedBy": {"id": staff.id, "displayName": staff.display_name, "role": r.recorded_by_role} if staff else None}

@router.post("/{case_id}/assisted-responses")
def record_assisted_response(case_id: str, payload: AssistedResponseCreate, db: Session = Depends(get_db), user=Depends(clinical_user)):
    """A response obtained by staff when the patient cannot use the link. Attributed to the staff member; never shown as the patient's own."""
    case = _case_for(db, user, case_id, modify=True)
    sub = str(uuid4())
    values = [("ACKNOWLEDGEMENT", {"acknowledged": payload.acknowledged}), ("UNDERSTANDING", {"level": payload.understood_level, "understood": payload.understood_level == "YES"}),
              ("CONFIDENCE", {"rating": payload.confidence}), ("EXPLANATION_STATUS", {"status": payload.explanationStatus}),
              ("CALLBACK_REQUEST", {"requested": payload.callbackRequested})]
    rows = [PatientResponse(case_id=case.id, kind=k, value=v, source="STAFF_ASSISTED", submission_id=sub, note=payload.note,
                            recorded_by_id=user.id, recorded_by_role=user.role.value) for k, v in values]
    db.add_all(rows)
    if payload.explanationStatus == "NOT_EXPLAINED":
        case.flag_not_explained = True
        create_task(db, case, "NOT_EXPLAINED", "Patient says the information was not understood or explained", "Recorded by staff on the patient's behalf.", source_ref=sub)
    if payload.callbackRequested:
        create_task(db, case, "CALLBACK_REQUESTED", "Patient requested a call-back", payload.note, source_ref=sub)
    record_event(db, user, case_id=case.id, event_type="ASSISTED_RESPONSE_RECORDED", description="Staff-assisted patient response recorded",
                 new_values={"submissionId": sub, "source": "STAFF_ASSISTED", "acknowledged": payload.acknowledged, "understood": payload.understood_level == "YES", "understanding": payload.understood_level,
                             "confidence": payload.confidence, "explanationStatus": payload.explanationStatus, "callbackRequested": payload.callbackRequested})
    db.flush()
    after_patient_response(db, case, actor=sm.SYSTEM, source="STAFF_ASSISTED", staff_id=user.id)
    db.commit()
    return {"success": True, "data": {"submissionId": sub, "source": "STAFF_ASSISTED", "submittedByPatient": False,
                                      "recordedBy": {"id": user.id, "displayName": user.display_name, "role": user.role.value},
                                      "recordedAt": rows[0].created_at, "responses": [_response_out(db, r) for r in rows]}, "meta": {}}

@router.get("/{case_id}/patient-responses")
def list_patient_responses(case_id: str, db: Session = Depends(get_db), user=Depends(clinical_user)):
    case = _case_for(db, user, case_id, modify=False)
    rows = db.query(PatientResponse).filter(PatientResponse.case_id == case.id).order_by(PatientResponse.created_at).all()
    return {"success": True, "data": [_response_out(db, r) for r in rows], "meta": {}}
