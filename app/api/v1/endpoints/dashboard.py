from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session
from app.core import clock
from app.services import state_machine as sm
from app.core.database import get_db
from app.core.permissions import visible_cases
from app.api.v1.deps import current_user
from app.models.entities import Case, Priority, CaseStatus, Referral, Role, RoutingStatus, User, Facility

router = APIRouter()

REFERRAL_STATUSES = ("SENT", "ACCEPTED", "DECLINED", "NOT_ACCEPTED_IN_TIME")

def _referral_counts(referrals) -> dict:
    out = {"total": len(referrals)}
    for status in REFERRAL_STATUSES:
        out[status.lower()] = sum(r.status == status for r in referrals)
    return out

def _open(rows):
    return [c for c in rows if c.status != CaseStatus.CLOSED]

def _coordinator_view(db, user, now):
    rows = visible_cases(db.query(Case), user).all()
    sm.sync_overdue(db, [c for c in rows if c.status != CaseStatus.CLOSED], now); db.commit()
    active = _open(rows)
    by_status = {}
    for c in active: by_status[c.status.value] = by_status.get(c.status.value, 0) + 1
    referrals = visible_cases(db.query(Referral).join(Case, Referral.case_id == Case.id), user).all()
    return {
        "stats": {
            "activeCases": len(active),
            "urgentCases": sum(c.priority == Priority.URGENT for c in active),
            "overdueCases": sum(c.flag_overdue for c in active),
            "unassignedCases": sum(c.coordinator_id is None for c in active),
            "exceptionCases": sum(c.flag_exception for c in active),
            "messageFailedCases": sum(c.flag_message_failed for c in active),
            "notExplainedCases": sum(c.flag_not_explained for c in active),
            "dataMissingCases": sum(c.flag_data_missing for c in active),
            "missingDelayReason": sum(c.flag_overdue for c in active),
            "awaitingUrgentAcceptance": sum(c.routing_status == RoutingStatus.AWAITING_ACCEPTANCE.value for c in active),
            "urgentRoutingExceptions": sum(c.routing_status == RoutingStatus.EXCEPTION.value for c in active),
        },
        "byStatus": by_status,
        "referrals": _referral_counts(referrals),
    }

def _referring_view(db, user, now):
    # Cases the clinician is the named owner of. "Cases awaiting clinical review" is not defined yet, so it is not reported.
    owned = [c for c in visible_cases(db.query(Case), user).all() if c.coordinator_id == user.id]
    sm.sync_overdue(db, [c for c in owned if c.status != CaseStatus.CLOSED], now); db.commit()
    active = _open(owned)
    ids = [c.id for c in owned]
    referrals = db.query(Referral).filter(Referral.case_id.in_(ids)).all() if ids else []
    return {
        "stats": {
            "assignedCases": len(active),
            "urgentCases": sum(c.priority == Priority.URGENT for c in active),
            "overdueCases": sum(c.flag_overdue for c in active),
            "missingDelayReason": sum(c.flag_overdue for c in active),
        },
        "referrals": _referral_counts(referrals),
    }

def _receiving_view(db, user, now):
    routed = db.query(Case).filter(Case.routed_clinician_id == user.id).all()
    urgent_routing = {"awaitingAcceptance": sum(c.routing_status == RoutingStatus.AWAITING_ACCEPTANCE.value for c in routed),
                      "accepted": sum(c.routing_status == RoutingStatus.ACCEPTED.value for c in routed)}
    if not user.facility_id:
        return {"referrals": {"incoming": 0, "pending": 0, "accepted": 0, "urgent": 0, "assignedToMe": 0, "outstandingDecisions": 0}, "urgentRouting": urgent_routing}
    rows = db.query(Referral, Case).join(Case, Referral.case_id == Case.id).filter(Referral.receiving_facility_id == user.facility_id).all()
    return {"referrals": {
        "incoming": len(rows),
        "pending": sum(r.status == "SENT" for r, _ in rows),
        "accepted": sum(r.status == "ACCEPTED" for r, _ in rows),
        "urgent": sum(r.status in ("SENT", "ACCEPTED") and c.priority == Priority.URGENT for r, c in rows),
        "assignedToMe": sum(r.receiving_clinician_id == user.id and r.status in ("SENT", "ACCEPTED") for r, _ in rows),
        "outstandingDecisions": sum(r.status == "SENT" and r.receiving_clinician_id in (None, user.id) for r, _ in rows),
    }, "urgentRouting": urgent_routing}

def _facility_admin_view(db, user, now):
    """Staff access and facility connection only. No patient or clinical case data (R-09)."""
    facility = db.get(Facility, user.facility_id) if user.facility_id else None
    staff = db.query(User).filter(
        User.facility_id == user.facility_id,
        User.role.in_((Role.COORDINATOR, Role.REFERRING_CLINICIAN, Role.RECEIVING_CLINICIAN, Role.FACILITY_ADMIN)),
    ).all() if user.facility_id else []
    by_role = {}
    for u in staff:
        by_role[u.role.value] = by_role.get(u.role.value, 0) + 1
    return {
        "facility": {"id": facility.id, "name": facility.name, "code": facility.code} if facility else None,
        "staffAccess": {"totalStaff": len(staff), "activeStaff": sum(u.is_active for u in staff), "inactiveStaff": sum(not u.is_active for u in staff), "byRole": by_role},
        "connection": {"status": facility.connection_status if facility else "UNKNOWN", "connectorType": facility.connector_type if facility else None,
                       "lastEventAt": facility.last_event_at if facility else None},
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
def dashboard_summary(db: Session = Depends(get_db), user=Depends(current_user)):
    view = VIEWS.get(user.role)
    if view is None:
        raise HTTPException(403, "Insufficient permissions")
    now = clock.now()
    data = view(db, user, now)
    data.update({"role": user.role.value, "generatedAt": now})
    if user.role != Role.FACILITY_ADMIN:
        data["demoClock"] = {"active": clock.is_active(), "offsetMinutes": clock.offset_minutes()}
    return {"success": True, "data": data, "meta": {}}
