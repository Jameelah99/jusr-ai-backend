from datetime import datetime, timezone
from math import ceil
from typing import Literal
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from sqlalchemy import or_
from app.core.database import get_db
from app.api.v1.deps import worklist_user
from app.core.permissions import visible_cases
from app.services.delay import delay_reason_missing
from app.services import routing
from app.models.entities import Case, Patient, Screening, Facility, User, Priority, CaseStatus, RoutingStatus

router = APIRouter()

@router.get("")
def get_worklist(page: int = Query(1, ge=1), pageSize: int = Query(20, ge=1, le=100), search: str | None = None,
                 priority: Priority | None = None, status: CaseStatus | None = None,
                 facilityId: str | None = None, coordinatorId: str | None = None,
                 screeningType: str | None = None, overdue: bool | None = None, routingStatus: RoutingStatus | None = None,
                 sortBy: Literal["dueAt", "updatedAt", "createdAt", "priority"] = "dueAt", sortOrder: Literal["asc", "desc"] = "asc",
                 db: Session = Depends(get_db), user=Depends(worklist_user)):
    q = db.query(Case).join(Patient, Case.patient_id == Patient.id).outerjoin(Screening, Case.screening_id == Screening.id)
    q = visible_cases(q, user)
    if facilityId: q = q.filter(Case.facility_id == facilityId)
    if coordinatorId: q = q.filter(Case.coordinator_id == coordinatorId)
    if priority: q = q.filter(Case.priority == priority)
    if status: q = q.filter(Case.status == status)
    if screeningType: q = q.filter(Screening.screening_type == screeningType)
    if routingStatus: q = q.filter(Case.routing_status == routingStatus.value)
    if overdue is True: q = q.filter(Case.due_at < datetime.now(timezone.utc), Case.status != CaseStatus.CLOSED)
    if overdue is False: q = q.filter(or_(Case.due_at >= datetime.now(timezone.utc), Case.due_at.is_(None)))
    if search:
        term = f"%{search}%"
        q = q.filter(or_(Case.case_number.ilike(term), Patient.display_label.ilike(term)))
    total = q.count()
    now = datetime.now(timezone.utc)
    # Summary is based on the filtered set, before pagination.
    all_rows = q.all()
    summary = {
        "totalCases": len(all_rows),
        "urgentCases": sum(c.priority == Priority.URGENT for c in all_rows),
        "overdueCases": sum(c.due_at is not None and c.due_at < now and c.status != CaseStatus.CLOSED for c in all_rows),
        "unassignedCases": sum(c.coordinator_id is None for c in all_rows),
    }
    sort_columns = {"dueAt": Case.due_at, "updatedAt": Case.updated_at, "createdAt": Case.created_at, "priority": Case.priority}
    column = sort_columns.get(sortBy, Case.due_at)
    q = q.order_by(column.desc() if sortOrder == "desc" else column.asc())
    rows = q.offset((page-1)*pageSize).limit(pageSize).all()
    results = []
    for c in rows:
        patient = db.get(Patient, c.patient_id)
        screening = db.get(Screening, c.screening_id) if c.screening_id else None
        coordinator = db.get(User, c.coordinator_id) if c.coordinator_id else None
        facility = db.get(Facility, c.facility_id)
        results.append({
            "id": c.id, "caseId": c.case_number,
            "patient": {"id": patient.id, "displayLabel": patient.display_label, "ageYears": patient.age_years, "sex": patient.sex},
            "screening": ({"id":screening.id,"type":screening.screening_type,"outcome":screening.outcome,"displayLabel":screening.display_label} if screening else None),
            "priority": c.priority.value, "status": c.status.value,
            "assignedCoordinator": ({"id":coordinator.id,"displayName":coordinator.display_name,"role":coordinator.role.value} if coordinator else None),
            "facility": {"id":facility.id,"name":facility.name},
            "nextAction": ({"code":c.next_action_code,"label":c.next_action_label} if c.next_action_code or c.next_action_label else None),
            "dueAt": c.due_at, "updatedAt": c.updated_at,
            "delayReason": ({"code": c.delay_reason_code, "note": c.delay_reason_note, "recordedAt": c.delay_reason_recorded_at} if c.delay_reason_code else None),
            "delayReasonRequired": delay_reason_missing(c, now),
            # Urgent routing (canonical block) and flat aliases for the frontend's current field names
            "specialty": c.required_specialty, "routingStatus": c.routing_status, "routing": routing.summary(db, c),
            "patientLabel": patient.display_label, "age": patient.age_years, "sex": patient.sex,
            "screeningType": screening.screening_type if screening else None,
            "assignedTo": coordinator.display_name if coordinator else None
        })
    return {"success":True,"data":{"summary":summary,"cases":results,"total":total,"page":page,"pageSize":pageSize,"totalPages":ceil(total/pageSize) if total else 0},"meta":{}}
