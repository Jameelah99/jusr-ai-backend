
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.api.v1.deps import clinical_user, worklist_user
from app.core.permissions import (
    GLOBAL_ROLES,
    ASSIGN_ROLES,
    CREATE_ROLES,
    visible_cases,
    can_view_case,
    can_modify_case,
)
from app.models.entities import (
    Role,
    DelayReason,
    Case,
    Patient,
    AuditEvent,
    CaseStatus,
    Priority,
    Screening,
    Facility,
    User,
)
from app.api.v1.endpoints.patients import visible_patients
from app.services.audit import record_event, reject_and_log
from app.services.delay import delay_reason_missing, is_overdue, DELAY_REQUIRED
from app.services.workflow import create_case_record
from app.schemas.cases import (
    CaseCreate,
    CaseAssign,
    CaseTransition,
    DelayReasonRecord,
)

router = APIRouter()


@router.get("")
def list_cases(
    page: int = Query(1, ge=1),
    pageSize: int = Query(20, ge=1),
    priority: Priority | None = None,
    status: CaseStatus | None = None,
    db: Session = Depends(get_db),
    user=Depends(worklist_user),
):
    q = db.query(Case)
    q = visible_cases(q, user)

    if priority:
        q = q.filter(Case.priority == priority)
    if status:
        q = q.filter(Case.status == status)

    total = q.count()
    rows = (
        q.order_by(Case.updated_at.desc())
        .offset((page - 1) * pageSize)
        .limit(min(pageSize, 100))
        .all()
    )

    return {
        "success": True,
        "data": [
            {
                "id": c.id,
                "caseId": c.case_number,
                "patientId": c.patient_id,
                "facilityId": c.facility_id,
                "priority": c.priority.value,
                "status": c.status.value,
                "dueAt": c.due_at,
                "updatedAt": c.updated_at,
            }
            for c in rows
        ],
        "meta": {
            "total": total,
            "page": page,
            "pageSize": min(pageSize, 100),
        },
    }


@router.post("")
def create_case(
    payload: CaseCreate,
    db: Session = Depends(get_db),
    user=Depends(clinical_user),
):
    if user.role not in CREATE_ROLES:
        raise HTTPException(403, "Insufficient permissions")

    facility_id = payload.facilityId or user.facility_id

    if user.role != Role.NATIONAL_ADMIN and facility_id != user.facility_id:
        raise HTTPException(
            403,
            "Cannot create a case outside your facility",
        )

    patient = (
        visible_patients(db.query(Patient), user)
        .filter(Patient.id == payload.patientId)
        .first()
    )

    if not patient:
        raise HTTPException(404, "Patient not found")

    case = create_case_record(
        db,
        user,
        patient=patient,
        case_number=payload.caseId,
        facility_id=facility_id,
        priority=payload.priority,
        next_action_code=payload.nextActionCode,
        next_action_label=payload.nextActionLabel,
        required_specialty=payload.requiredSpecialty,
    )

    db.commit()
    db.refresh(case)

    return {
        "success": True,
        "data": {
            "id": case.id,
            "caseId": case.case_number,
            "status": case.status.value,
        },
        "meta": {},
    }


@router.post("/{case_id}/assign")
def assign_case(
    case_id: str,
    payload: CaseAssign,
    db: Session = Depends(get_db),
    user=Depends(clinical_user),
):
    case = db.get(Case, case_id)

    if not case or not can_view_case(user, case, db):
        raise HTTPException(404, "Case not found")

    if user.role not in ASSIGN_ROLES or not can_modify_case(user, case):
        raise HTTPException(403, "Insufficient permissions")

    coordinator = db.get(User, payload.coordinatorId)

    if not coordinator:
        raise HTTPException(404, "Coordinator not found")

    if coordinator.role != Role.COORDINATOR or not coordinator.is_active:
        raise HTTPException(
            422,
            detail={
                "code": "INVALID_ASSIGNEE",
                "message": "Cases can only be assigned to an active coordinator",
            },
        )

    if coordinator.facility_id != case.facility_id:
        raise HTTPException(
            422,
            detail={
                "code": "INVALID_ASSIGNEE_FACILITY",
                "message": "The coordinator must belong to the case facility",
            },
        )

    old = case.coordinator_id
    case.coordinator_id = coordinator.id

    if case.status == CaseStatus.OPEN:
        case.status = CaseStatus.ASSIGNED

    case.updated_at = datetime.now(timezone.utc)

    record_event(
        db,
        user,
        case_id=case.id,
        event_type="CASE_ASSIGNED",
        description="Case assigned",
        old_values={"coordinatorId": old},
        new_values={"coordinatorId": coordinator.id},
    )

    db.commit()

    return {
        "success": True,
        "data": {
            "caseId": case.case_number,
            "coordinatorId": coordinator.id,
            "status": case.status.value,
        },
        "meta": {},
    }


def _has_recorded_basis(db: Session, case: Case) -> bool:
    """Check whether the case has a previously recorded resolution basis."""
    events = (
        db.query(AuditEvent)
        .filter(
            AuditEvent.case_id == case.id,
            AuditEvent.event_type == "CASE_STATUS_CHANGED",
        )
        .order_by(AuditEvent.occurred_at.desc())
        .all()
    )

    for event in events:
        new_values = event.new_values or {}

        if new_values.get("status") == CaseStatus.RESOLVED.value:
            return bool(
                new_values.get("evidence")
                or new_values.get("decision")
            )

    return False


def _validate_second_approver(
    db: Session,
    case: Case,
    user: User,
    approver_id: str,
):
    """Validate that the second approver is a different active staff member."""
    approver = db.get(User, approver_id)

    if not approver or not approver.is_active:
        raise HTTPException(
            422,
            detail={
                "code": "INVALID_SECOND_APPROVER",
                "message": "The second approver must be an active staff member",
            },
        )

    if approver.id == user.id:
        raise HTTPException(
            422,
            detail={
                "code": "SECOND_APPROVER_MUST_DIFFER",
                "message": "The second approver must be a different person",
            },
        )

    if approver.facility_id != case.facility_id:
        raise HTTPException(
            422,
            detail={
                "code": "INVALID_SECOND_APPROVER_FACILITY",
                "message": "The second approver must belong to the case facility",
            },
        )

    allowed_roles = {
        Role.COORDINATOR,
        Role.REFERRING_CLINICIAN,
    }

    if approver.role not in allowed_roles:
        raise HTTPException(
            422,
            detail={
                "code": "INVALID_SECOND_APPROVER_ROLE",
                "message": "The selected staff member cannot act as a second approver",
            },
        )

    return approver


@router.post("/{case_id}/transition")
def transition_case(
    case_id: str,
    payload: CaseTransition,
    db: Session = Depends(get_db),
    user=Depends(clinical_user),
):
    case = db.get(Case, case_id)

    if not case or not can_view_case(user, case, db):
        raise HTTPException(404, "Case not found")

    if not can_modify_case(user, case):
        raise HTTPException(403, "Insufficient permissions")

    target = payload.status

    allowed = {
        CaseStatus.OPEN: {CaseStatus.ASSIGNED},
        CaseStatus.ASSIGNED: {
            CaseStatus.IN_PROGRESS,
            CaseStatus.REFERRED,
        },
        CaseStatus.REFERRED: {CaseStatus.IN_PROGRESS},
        CaseStatus.IN_PROGRESS: {CaseStatus.RESOLVED},
        CaseStatus.RESOLVED: {CaseStatus.CLOSED},
        CaseStatus.CLOSED: set(),
    }

    if target not in allowed[case.status]:
        reject_and_log(
            db,
            user,
            case_id=case.id,
            attempted=f"{case.status.value}->{target.value}",
            status_code=409,
            code="INVALID_STATUS_TRANSITION",
            message=(
                f"Cannot move {case.status.value} to {target.value}"
            ),
        )

    # An overdue case needs a recorded reason before it can move on.
    if delay_reason_missing(case):
        reject_and_log(
            db,
            user,
            case_id=case.id,
            attempted=f"{case.status.value}->{target.value}",
            **DELAY_REQUIRED,
        )

    # A case cannot be resolved without evidence or a recorded decision.
    if target == CaseStatus.RESOLVED and not (
        payload.evidence or payload.decision
    ):
        reject_and_log(
            db,
            user,
            case_id=case.id,
            attempted=f"{case.status.value}->{target.value}",
            status_code=422,
            code="EVIDENCE_REQUIRED",
            message=(
                "Resolving a case requires evidence or a recorded decision"
            ),
        )

    # Closing requires a previously recorded resolution basis.
    if target == CaseStatus.CLOSED:
        if not _has_recorded_basis(db, case):
            reject_and_log(
                db,
                user,
                case_id=case.id,
                attempted=f"{case.status.value}->{target.value}",
                status_code=422,
                code="RESOLUTION_BASIS_REQUIRED",
                message=(
                    "A case must have a recorded resolution basis "
                    "before it can be closed"
                ),
            )

        if not payload.closureType or not payload.reason:
            raise HTTPException(
                422,
                detail={
                    "code": "CLOSURE_DETAILS_REQUIRED",
                    "message": "A closure type and reason are required",
                },
            )

        if payload.closureType == "other":
            if not payload.secondApproverStaffId:
                raise HTTPException(
                    422,
                    detail={
                        "code": "SECOND_APPROVER_REQUIRED",
                        "message": (
                            "A second approver is required for this closure type"
                        ),
                    },
                )

            _validate_second_approver(
                db,
                case,
                user,
                payload.secondApproverStaffId,
            )

    old_status = case.status.value
    case.status = target
    case.updated_at = datetime.now(timezone.utc)

    new_values = {
        "status": target.value,
    }

    if payload.evidence:
        new_values["evidence"] = payload.evidence

    if payload.decision:
        new_values["decision"] = payload.decision

    if target == CaseStatus.CLOSED:
        new_values["closure"] = {
            "closureType": payload.closureType,
            "reason": payload.reason,
            "closureEvidence": [
                item.model_dump(mode="json")
                for item in payload.closureEvidence
            ],
            "secondApproverStaffId": payload.secondApproverStaffId,
        }

    record_event(
        db,
        user,
        case_id=case.id,
        event_type="CASE_STATUS_CHANGED",
        description=(
            "Case closed"
            if target == CaseStatus.CLOSED
            else "Case status changed"
        ),
        old_values={"status": old_status},
        new_values=new_values,
    )

    db.commit()

    return {
        "success": True,
        "data": {
            "caseId": case.case_number,
            "status": case.status.value,
        },
        "meta": {},
    }


@router.post("/{case_id}/delay-reason")
def record_delay_reason(
    case_id: str,
    payload: DelayReasonRecord,
    db: Session = Depends(get_db),
    user=Depends(clinical_user),
):
    case = db.get(Case, case_id)

    if not case or not can_view_case(user, case, db):
        raise HTTPException(404, "Case not found")

    if user.role not in CREATE_ROLES or not can_modify_case(user, case):
        raise HTTPException(403, "Insufficient permissions")

    if not is_overdue(case):
        reject_and_log(
            db,
            user,
            case_id=case.id,
            attempted="delay-reason",
            status_code=409,
            code="CASE_NOT_OVERDUE",
            message=(
                "A reason-for-delay code can only be recorded "
                "on an overdue case"
            ),
        )

    old = {
        "code": case.delay_reason_code,
        "note": case.delay_reason_note,
    }

    case.delay_reason_code = payload.code.value
    case.delay_reason_note = payload.note
    case.delay_reason_recorded_at = datetime.now(timezone.utc)
    case.delay_reason_recorded_by_id = user.id

    record_event(
        db,
        user,
        case_id=case.id,
        event_type="DELAY_REASON_RECORDED",
        description="Reason for delay recorded",
        old_values=old,
        new_values={
            "code": payload.code.value,
            "note": payload.note,
        },
    )

    db.commit()

    return {
        "success": True,
        "data": {
            "caseId": case.case_number,
            "delayReason": {
                "code": case.delay_reason_code,
                "note": case.delay_reason_note,
                "recordedAt": case.delay_reason_recorded_at,
                "recordedById": user.id,
            },
        },
        "meta": {},
    }


@router.get("/delay-reasons")
def delay_reason_codes(user=Depends(clinical_user)):
    return {
        "success": True,
        "data": [reason.value for reason in DelayReason],
        "meta": {},
    }


@router.get("/{case_id}/events")
def case_events(
    case_id: str,
    db: Session = Depends(get_db),
    user=Depends(clinical_user),
):
    case = db.get(Case, case_id)

    if not case or not can_view_case(user, case, db):
        raise HTTPException(404, "Case not found")

    events = (
        db.query(AuditEvent)
        .filter(AuditEvent.case_id == case.id)
        .order_by(AuditEvent.occurred_at.asc())
        .all()
    )

    return {
        "success": True,
        "data": [
            {
                "id": event.id,
                "eventType": event.event_type,
                "description": event.description,
                "occurredAt": event.occurred_at,
                "actorId": event.actor_id,
                "actorRole": event.actor_role,
            }
            for event in events
        ],
        "meta": {},
    }
