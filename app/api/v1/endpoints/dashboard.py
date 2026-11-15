
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.core.permissions import visible_cases, visible_referrals
from app.services.delay import delay_reason_missing
from app.api.v1.deps import current_user
from app.models.entities import (
    Case,
    Priority,
    CaseStatus,
    Referral,
    Role,
    RoutingStatus,
    User,
    Facility,
    Patient,
    Screening,
    AuditEvent,
)

router = APIRouter()

REFERRAL_STATUSES = ("PENDING", "ACCEPTED", "REJECTED", "CANCELLED")


def _referral_counts(referrals) -> dict:
    out = {"total": len(referrals)}
    for status in REFERRAL_STATUSES:
        out[status.lower()] = sum(r.status == status for r in referrals)
    return out


def _open(rows):
    return [c for c in rows if c.status != CaseStatus.CLOSED]


def _priority_queue(db, cases, limit=10):
    """Build a priority queue from real, permission-scoped case records."""
    if not cases:
        return []

    patient_ids = list({c.patient_id for c in cases})
    screening_ids = list({c.screening_id for c in cases if c.screening_id})
    coordinator_ids = list({c.coordinator_id for c in cases if c.coordinator_id})

    patients = {
        p.id: p
        for p in db.query(Patient).filter(Patient.id.in_(patient_ids)).all()
    }

    screenings = {
        s.id: s
        for s in db.query(Screening).filter(Screening.id.in_(screening_ids)).all()
    } if screening_ids else {}

    coordinators = {
        u.id: u
        for u in db.query(User).filter(User.id.in_(coordinator_ids)).all()
    } if coordinator_ids else {}

    priority_order = {
        Priority.URGENT: 0,
        Priority.HIGH: 1,
        Priority.NORMAL: 2,
    }

    ordered = sorted(
        cases,
        key=lambda c: (
            priority_order.get(c.priority, 3),
            c.due_at is None,
            c.due_at or datetime.max.replace(tzinfo=timezone.utc),
            c.created_at,
        ),
    )

    result = []

    for case in ordered[:limit]:
        patient = patients.get(case.patient_id)
        screening = screenings.get(case.screening_id)
        coordinator = coordinators.get(case.coordinator_id)

        result.append({
            "id": case.id,
            "caseId": case.case_number,
            "patientLabel": patient.display_label if patient else "Unknown patient",
            "age": patient.age_years if patient else None,
            "screeningType": screening.screening_type if screening else None,
            "screeningResult": screening.display_label if screening else None,
            "priority": case.priority.value,
            "status": case.status.value,
            "nextStep": case.next_action_label,
            "nextActionCode": case.next_action_code,
            "owner": coordinator.display_name if coordinator else "Unassigned",
            "dueAt": case.due_at,
            "overdue": case.due_at is not None and case.due_at < datetime.now(timezone.utc),
        })

    return result


def _recent_activity(db, cases, limit=10):
    """Return recent audit activity for cases visible to this user."""
    case_ids = [c.id for c in cases]

    if not case_ids:
        return []

    events = (
        db.query(AuditEvent)
        .filter(AuditEvent.case_id.in_(case_ids))
        .order_by(AuditEvent.occurred_at.desc())
        .limit(limit)
        .all()
    )

    actor_ids = list({e.actor_id for e in events if e.actor_id})

    actors = {
        u.id: u
        for u in db.query(User).filter(User.id.in_(actor_ids)).all()
    } if actor_ids else {}

    return [
        {
            "id": event.id,
            "description": event.description,
            "actor": (
                actors[event.actor_id].display_name
                if event.actor_id in actors
                else "Patient" if event.actor_role == "PATIENT"
                else "System"
            ),
            "actorRole": event.actor_role,
            "eventType": event.event_type,
            "timestamp": event.occurred_at,
        }
        for event in events
    ]


def _coordinator_view(db, user, now):
    rows = visible_cases(db.query(Case), user).all()
    active = _open(rows)

    referrals = visible_referrals(
        db.query(Referral).join(Case, Referral.case_id == Case.id),
        user,
    ).all()

    return {
        "stats": {
            "activeCases": len(active),
            "urgentCases": sum(c.priority == Priority.URGENT for c in active),
            "overdueCases": sum(c.due_at is not None and c.due_at < now for c in active),
            "unassignedCases": sum(c.coordinator_id is None for c in active),
            "missingDelayReason": sum(delay_reason_missing(c, now) for c in active),
            "awaitingUrgentAcceptance": sum(
                c.routing_status == RoutingStatus.AWAITING_ACCEPTANCE.value
                for c in active
            ),
            "urgentRoutingExceptions": sum(
                c.routing_status == RoutingStatus.EXCEPTION.value
                for c in active
            ),
        },
        "referrals": _referral_counts(referrals),
        "priorityCases": _priority_queue(db, active),
        "recentActivity": _recent_activity(db, rows),
    }


def _referring_view(db, user, now):
    rows = visible_cases(db.query(Case), user).all()
    active = _open(rows)

    referrals = visible_referrals(
        db.query(Referral).join(Case, Referral.case_id == Case.id),
        user,
    ).filter(Referral.referred_by_id == user.id).all()

    return {
        "stats": {
            "assignedCases": len(active),
            "urgentCases": sum(c.priority == Priority.URGENT for c in active),
            "overdueCases": sum(c.due_at is not None and c.due_at < now for c in active),
            "missingDelayReason": sum(delay_reason_missing(c, now) for c in active),
        },
        "referrals": _referral_counts(referrals),
        "priorityCases": _priority_queue(db, active),
        "recentActivity": _recent_activity(db, rows),
    }


def _receiving_view(db, user, now):
    routed = db.query(Case).filter(Case.routed_clinician_id == user.id).all()

    urgent_routing = {
        "awaitingAcceptance": sum(
            c.routing_status == RoutingStatus.AWAITING_ACCEPTANCE.value
            for c in routed
        ),
        "accepted": sum(
            c.routing_status == RoutingStatus.ACCEPTED.value
            for c in routed
        ),
    }

    if not user.facility_id:
        return {
            "referrals": {
                "incoming": 0,
                "pending": 0,
                "accepted": 0,
                "urgent": 0,
                "assignedToMe": 0,
                "outstandingDecisions": 0,
            },
            "urgentRouting": urgent_routing,
            "priorityCases": _priority_queue(db, routed),
            "recentActivity": _recent_activity(db, routed),
        }

    rows = (
        db.query(Referral, Case)
        .join(Case, Referral.case_id == Case.id)
        .filter(
            Referral.receiving_facility_id == user.facility_id,
            (
                (Referral.receiving_clinician_id == user.id)
                | (Referral.receiving_clinician_id.is_(None))
            ),
        )
        .all()
    )

    receiving_cases = {case.id: case for _, case in rows}
    receiving_cases.update({c.id: c for c in routed})
    visible_rows = list(receiving_cases.values())

    return {
        "referrals": {
            "incoming": len(rows),
            "pending": sum(r.status == "PENDING" for r, _ in rows),
            "accepted": sum(r.status == "ACCEPTED" for r, _ in rows),
            "urgent": sum(
                r.status in ("PENDING", "ACCEPTED")
                and c.priority == Priority.URGENT
                for r, c in rows
            ),
            "assignedToMe": sum(
                r.receiving_clinician_id == user.id
                and r.status in ("PENDING", "ACCEPTED")
                for r, _ in rows
            ),
            "outstandingDecisions": sum(
                r.status == "PENDING"
                and r.receiving_clinician_id in (None, user.id)
                for r, _ in rows
            ),
        },
        "urgentRouting": urgent_routing,
        "priorityCases": _priority_queue(db, visible_rows),
        "recentActivity": _recent_activity(db, visible_rows),
    }


def _facility_admin_view(db, user, now):
    """Staff access and facility connection only. No patient or clinical data."""
    facility = db.get(Facility, user.facility_id) if user.facility_id else None

    staff = (
        db.query(User)
        .filter(
            User.facility_id == user.facility_id,
            User.role != Role.PATIENT,
        )
        .all()
        if user.facility_id else []
    )

    by_role = {}
    for staff_member in staff:
        by_role[staff_member.role.value] = (
            by_role.get(staff_member.role.value, 0) + 1
        )

    return {
        "facility": {
            "id": facility.id,
            "name": facility.name,
            "code": facility.code,
        } if facility else None,
        "staffAccess": {
            "totalStaff": len(staff),
            "activeStaff": sum(u.is_active for u in staff),
            "inactiveStaff": sum(not u.is_active for u in staff),
            "byRole": by_role,
        },
        "connection": {
            "status": "DEMO" if settings.demo_mode else "UNKNOWN",
        },
    }


VIEWS = {
    Role.COORDINATOR: _coordinator_view,
    Role.NATIONAL_ADMIN: _coordinator_view,
    Role.AI_SERVICE: _coordinator_view,
    Role.REFERRING_CLINICIAN: _referring_view,
    Role.RECEIVING_CLINICIAN: _receiving_view,
    Role.FACILITY_ADMIN: _facility_admin_view,
}


@router.get("/summary")
def dashboard_summary(
    db: Session = Depends(get_db),
    user=Depends(current_user),
):
    view = VIEWS.get(user.role)

    if view is None:
        raise HTTPException(403, "Insufficient permissions")

    now = datetime.now(timezone.utc)
    data = view(db, user, now)

    data.update({
        "role": user.role.value,
        "generatedAt": now,
    })

    return {
        "success": True,
        "data": data,
        "meta": {},
    }
