"""AI-assisted workflows. The AI service may only submit suggestions; every effect needs a staff member's approval (R-02, R-08)."""
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import ValidationError
from sqlalchemy import or_, select
from sqlalchemy.orm import Session
from app.core.database import get_db
from app.core.permissions import CREATE_ROLES, GLOBAL_ROLES, can_modify_case, can_view_case, visible_cases
from app.api.v1.deps import clinical_user, require_roles
from app.api.v1.endpoints.patients import visible_patients
from app.models.entities import (AiKind, AiSuggestion, AuditEvent, Case, CaseStatus, Consultation, Facility, Message, MessageStatus,
                                 Patient, PatientResponse, Referral, Role, SuggestionStatus, User)
from app.schemas.ai import PAYLOAD_MODELS, SuggestionApprove, SuggestionCreate, SuggestionReject
from app.services.audit import record_event, reject_and_log
from app.services.workflow import create_case_record, create_referral_record

router = APIRouter()

def _now():
    return datetime.now(timezone.utc)

def out(s: AiSuggestion) -> dict:
    return {"id": s.id, "kind": s.kind, "status": s.status, "caseId": s.case_id, "facilityId": s.facility_id, "payload": s.payload, "model": s.model,
            "confidence": s.confidence, "createdAt": s.created_at, "decidedById": s.decided_by_id, "decidedAt": s.decided_at,
            "decisionNote": s.decision_note, "applied": s.applied, "requiresStaffApproval": s.status == SuggestionStatus.PENDING.value}

def _visible(q, user):
    if user.role in GLOBAL_ROLES: return q
    return q.filter(or_(AiSuggestion.facility_id == user.facility_id, AiSuggestion.case_id.in_(visible_cases(select(Case.id), user))))

def _load(db, user, suggestion_id) -> AiSuggestion:
    s = _visible(db.query(AiSuggestion), user).filter(AiSuggestion.id == suggestion_id).first()
    if not s: raise HTTPException(404, "Suggestion not found")
    return s

def _verify_citations(db, case: Case, citations) -> None:
    """Every citation must point at a real record belonging to this case."""
    checks = {
        "CASE": lambda i: i == case.id,
        "SCREENING": lambda i: case.screening_id is not None and i == case.screening_id,
        "AUDIT_EVENT": lambda i: db.query(AuditEvent.id).filter(AuditEvent.id == i, AuditEvent.case_id == case.id).first() is not None,
        "REFERRAL": lambda i: db.query(Referral.id).filter(Referral.id == i, Referral.case_id == case.id).first() is not None,
        "CONSULTATION": lambda i: db.query(Consultation.id).filter(Consultation.id == i, Consultation.case_id == case.id).first() is not None,
        "PATIENT_RESPONSE": lambda i: db.query(PatientResponse.id).filter(PatientResponse.id == i, PatientResponse.case_id == case.id).first() is not None,
    }
    for c in citations:
        if not checks[c.sourceType](c.sourceId):
            raise HTTPException(422, detail={"code": "INVALID_CITATION", "message": f"Citation {c.sourceType} {c.sourceId} does not exist for this case",
                                             "sourceType": c.sourceType, "sourceId": c.sourceId})

# ---------- submission (AI service only) ----------

@router.post("")
def submit_suggestion(payload: SuggestionCreate, db: Session = Depends(get_db), user=Depends(require_roles(Role.AI_SERVICE.value))):
    try:
        data = PAYLOAD_MODELS[payload.kind].model_validate(payload.payload)
    except ValidationError as exc:
        raise HTTPException(422, detail={"code": "INVALID_SUGGESTION_PAYLOAD", "message": f"Payload does not match a {payload.kind.value} suggestion",
                                         "errors": exc.errors(include_url=False, include_context=False, include_input=False)})
    case = None
    if payload.kind == AiKind.INTAKE:
        if payload.caseId:
            raise HTTPException(422, detail={"code": "CASE_NOT_ALLOWED", "message": "An INTAKE suggestion proposes a new case; omit caseId"})
        if not data.facilityId or not db.get(Facility, data.facilityId):
            raise HTTPException(422, detail={"code": "INVALID_FACILITY", "message": "INTAKE needs a valid facilityId in the payload"})
        if not db.get(Patient, data.patientId):
            raise HTTPException(404, "Patient not found")
        facility_id = data.facilityId
    else:
        if not payload.caseId:
            raise HTTPException(422, detail={"code": "CASE_REQUIRED", "message": f"{payload.kind.value} needs a caseId"})
        case = db.query(Case).filter((Case.id == payload.caseId) | (Case.case_number == payload.caseId)).first()
        if not case: raise HTTPException(404, "Case not found")
        facility_id = case.facility_id
        if payload.kind == AiKind.CASE_SUMMARY: _verify_citations(db, case, data.citations)
        if payload.kind == AiKind.REFERRAL_TARGET:
            for fid in [data.receivingFacilityId] + [a.receivingFacilityId for a in data.alternatives]:
                if not db.get(Facility, fid): raise HTTPException(404, "Receiving facility not found")
    s = AiSuggestion(kind=payload.kind.value, case_id=case.id if case else None, facility_id=facility_id, payload=data.model_dump(mode="json"),
                     model=payload.model, confidence=payload.confidence, created_by_id=user.id)
    db.add(s); db.flush()
    record_event(db, user, case_id=s.case_id, event_type="AI_SUGGESTION_SUBMITTED", description=f"AI suggestion submitted: {s.kind}",
                 new_values={"suggestionId": s.id, "kind": s.kind, "model": payload.model})
    db.commit()
    return {"success": True, "data": out(s), "meta": {}}

# ---------- reads ----------

@router.get("")
def list_suggestions(page: int = Query(1, ge=1), pageSize: int = Query(20, ge=1, le=100), kind: AiKind | None = None,
                     status: SuggestionStatus | None = None, caseId: str | None = None,
                     db: Session = Depends(get_db), user=Depends(clinical_user)):
    q = _visible(db.query(AiSuggestion), user)
    if kind: q = q.filter(AiSuggestion.kind == kind.value)
    if status: q = q.filter(AiSuggestion.status == status.value)
    if caseId: q = q.filter(AiSuggestion.case_id.in_(select(Case.id).where((Case.id == caseId) | (Case.case_number == caseId))))
    total = q.count()
    rows = q.order_by(AiSuggestion.created_at.desc(), AiSuggestion.id).offset((page - 1) * pageSize).limit(pageSize).all()
    return {"success": True, "data": [out(s) for s in rows], "meta": {"total": total, "page": page, "pageSize": pageSize}}

@router.get("/cases/{case_id}/summary")
def approved_summary(case_id: str, db: Session = Depends(get_db), user=Depends(clinical_user)):
    """The latest staff-approved AI case summary, with its source citations."""
    case = db.query(Case).filter((Case.id == case_id) | (Case.case_number == case_id)).first()
    if not case or not can_view_case(user, case, db): raise HTTPException(404, "Case not found")
    s = (db.query(AiSuggestion).filter(AiSuggestion.case_id == case.id, AiSuggestion.kind == AiKind.CASE_SUMMARY.value, AiSuggestion.status == SuggestionStatus.APPROVED.value)
         .order_by(AiSuggestion.decided_at.desc()).first())
    if not s: raise HTTPException(404, "No approved summary for this case")
    return {"success": True, "data": {**out(s), "approvedBy": s.decided_by_id}, "meta": {}}

@router.get("/{suggestion_id}")
def get_suggestion(suggestion_id: str, db: Session = Depends(get_db), user=Depends(clinical_user)):
    return {"success": True, "data": out(_load(db, user, suggestion_id)), "meta": {}}

# ---------- staff decision ----------

@router.post("/{suggestion_id}/approve")
def approve_suggestion(suggestion_id: str, payload: SuggestionApprove | None = None, db: Session = Depends(get_db), user=Depends(clinical_user)):
    s = _load(db, user, suggestion_id)
    if user.role not in CREATE_ROLES: raise HTTPException(403, "Insufficient permissions")
    case = db.get(Case, s.case_id) if s.case_id else None
    if case and not can_modify_case(user, case): raise HTTPException(403, "Insufficient permissions")
    if not case and user.role != Role.NATIONAL_ADMIN and s.facility_id != user.facility_id: raise HTTPException(403, "Insufficient permissions")
    kind = AiKind(s.kind)
    if payload and payload.editedBody and kind != AiKind.MESSAGE_DRAFT:
        raise HTTPException(422, detail={"code": "EDIT_NOT_ALLOWED", "message": "Only a MESSAGE_DRAFT can be edited before approval"})
    if s.status != SuggestionStatus.PENDING.value:
        reject_and_log(db, user, case_id=s.case_id, attempted="suggestion:approve", status_code=409, code="SUGGESTION_ALREADY_DECIDED", message=f"Suggestion is already {s.status}")
    data = PAYLOAD_MODELS[kind].model_validate(s.payload)
    ref = {"suggestionId": s.id}
    applied: dict = {}
    if kind == AiKind.INTAKE:
        patient = visible_patients(db.query(Patient), user).filter(Patient.id == data.patientId).first()
        if not patient: raise HTTPException(404, "Patient not found")
        new_case = create_case_record(db, user, patient=patient, case_number=data.caseId, facility_id=s.facility_id, priority=data.priority,
                                      next_action_code=data.nextActionCode, next_action_label=data.nextActionLabel, extra_event=ref)
        s.case_id = new_case.id; applied = {"caseId": new_case.id, "caseNumber": new_case.case_number}
    elif kind == AiKind.URGENCY:
        if case.status == CaseStatus.CLOSED:
            reject_and_log(db, user, case_id=case.id, attempted="suggestion:urgency", status_code=409, code="CASE_CLOSED", message="Cannot change the priority of a closed case")
        old = case.priority.value; case.priority = data.priority; case.updated_at = _now()
        record_event(db, user, case_id=case.id, event_type="CASE_PRIORITY_CHANGED", description="Case priority changed on approving an AI suggestion",
                     old_values={"priority": old}, new_values={"priority": data.priority.value, **ref})
        applied = {"priority": data.priority.value, "actionability": data.actionability}
    elif kind == AiKind.MESSAGE_DRAFT:
        body = (payload.editedBody if payload and payload.editedBody else data.body)
        m = Message(case_id=case.id, facility_id=case.facility_id, channel=data.channel, body=body, status=MessageStatus.APPROVED.value, source="AI_SUGGESTION",
                    suggestion_id=s.id, created_by_id=s.created_by_id, approved_by_id=user.id, approved_at=_now())
        db.add(m); db.flush()
        record_event(db, user, case_id=case.id, event_type="MESSAGE_APPROVED", description="Message approved for sending (from an AI draft)",
                     new_values={"messageId": m.id, "edited": body != data.body, **ref})
        applied = {"messageId": m.id, "edited": body != data.body}
    elif kind == AiKind.REFERRAL_TARGET:
        r = create_referral_record(db, user, case, facility_id=data.receivingFacilityId, clinician_id=data.receivingClinicianId, reason=data.rationale, extra_event=ref)
        applied = {"referralId": r.id}
    s.status = SuggestionStatus.APPROVED.value; s.decided_by_id = user.id; s.decided_at = _now()
    s.decision_note = payload.note if payload else None; s.applied = applied
    record_event(db, user, case_id=s.case_id, event_type="AI_SUGGESTION_APPROVED", description=f"AI suggestion approved: {s.kind}",
                 new_values={**ref, "kind": s.kind, "applied": applied})
    db.commit()
    return {"success": True, "data": out(s), "meta": {}}

@router.post("/{suggestion_id}/reject")
def reject_suggestion(suggestion_id: str, payload: SuggestionReject, db: Session = Depends(get_db), user=Depends(clinical_user)):
    s = _load(db, user, suggestion_id)
    if user.role not in CREATE_ROLES: raise HTTPException(403, "Insufficient permissions")
    case = db.get(Case, s.case_id) if s.case_id else None
    if case and not can_modify_case(user, case): raise HTTPException(403, "Insufficient permissions")
    if not case and user.role != Role.NATIONAL_ADMIN and s.facility_id != user.facility_id: raise HTTPException(403, "Insufficient permissions")
    if s.status != SuggestionStatus.PENDING.value:
        reject_and_log(db, user, case_id=s.case_id, attempted="suggestion:reject", status_code=409, code="SUGGESTION_ALREADY_DECIDED", message=f"Suggestion is already {s.status}")
    s.status = SuggestionStatus.REJECTED.value; s.decided_by_id = user.id; s.decided_at = _now(); s.decision_note = payload.reason
    record_event(db, user, case_id=s.case_id, event_type="AI_SUGGESTION_REJECTED", description=f"AI suggestion rejected: {s.kind}", new_values={"suggestionId": s.id, "kind": s.kind, "reason": payload.reason})
    db.commit()
    return {"success": True, "data": out(s), "meta": {}}
