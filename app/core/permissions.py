"""Central role and facility rules. The backend, not the frontend, is the security boundary."""

from sqlalchemy import or_, select
from app.models.entities import Case, Referral, Role

# Read across every facility.
GLOBAL_ROLES = {Role.NATIONAL_ADMIN, Role.AI_SERVICE}
# Roles that may see patient and clinical case content. FACILITY_ADMIN and PATIENT are excluded (R-09).
CLINICAL_ROLES = {
    Role.COORDINATOR,
    Role.REFERRING_CLINICIAN,
    Role.RECEIVING_CLINICIAN,
    Role.NATIONAL_ADMIN,
    Role.AI_SERVICE,
}
# Roles that may change workflow state. AI_SERVICE may suggest but never decide (R-02, R-08).
WORKFLOW_ROLES = {
    Role.COORDINATOR,
    Role.REFERRING_CLINICIAN,
    Role.RECEIVING_CLINICIAN,
    Role.NATIONAL_ADMIN,
}
ASSIGN_ROLES = {Role.COORDINATOR, Role.NATIONAL_ADMIN}
CREATE_ROLES = {Role.COORDINATOR, Role.REFERRING_CLINICIAN, Role.NATIONAL_ADMIN}


def role_values(roles) -> tuple[str, ...]:
    return tuple(r.value for r in roles)


def visible_cases(q, user):
    """Restrict cases to the records the user is authorised to see."""
    if user.role in GLOBAL_ROLES:
        return q

    if user.role == Role.RECEIVING_CLINICIAN:
        clauses = [Case.routed_clinician_id == user.id]

        # Referrals assigned directly to this clinician.
        clauses.append(
            Case.id.in_(
                select(Referral.case_id).where(
                    Referral.receiving_clinician_id == user.id
                )
            )
        )

        # Pending, unassigned referrals matching this clinician's specialty.
        if user.facility_id and user.specialty:
            clauses.append(
                Case.id.in_(
                    select(Referral.case_id)
                    .where(
                        Referral.case_id == Case.id,
                        Referral.receiving_facility_id == user.facility_id,
                        Referral.receiving_clinician_id.is_(None),
                        Referral.status == "PENDING",
                        Case.required_specialty == user.specialty,
                    )
                    .correlate(Case)
                )
            )

        return q.filter(or_(*clauses))

    # Other clinical staff: own facility or cases they own.
    return q.filter(
        or_(Case.facility_id == user.facility_id, Case.coordinator_id == user.id)
    )


def can_view_case(user, case, db=None) -> bool:
    if user.role not in CLINICAL_ROLES:
        return False
    if user.role in GLOBAL_ROLES:
        return True
    if user.role == Role.RECEIVING_CLINICIAN:
        if case.routed_clinician_id == user.id:
            return True
        return bool(
            db is not None
            and user.facility_id
            and db.query(Referral.id)
            .filter(
                Referral.case_id == case.id,
                Referral.receiving_facility_id == user.facility_id,
            )
            .first()
        )
    return case.coordinator_id == user.id or case.facility_id == user.facility_id


def visible_referrals(q, user):
    """Restrict referrals to those the user is authorised to see."""
    if user.role in GLOBAL_ROLES:
        return q

    if user.role == Role.RECEIVING_CLINICIAN:
        clauses = [Referral.receiving_clinician_id == user.id]

        # Allow eligible clinicians to see pending referrals
        # that have not yet been assigned to an individual.
        if user.facility_id and user.specialty:
            clauses.append(
                (Referral.receiving_facility_id == user.facility_id)
                & (Referral.receiving_clinician_id.is_(None))
                & (Referral.status == "PENDING")
                & (Case.required_specialty == user.specialty)
            )

        return q.filter(or_(*clauses))

    # Other clinical staff: outgoing referrals from their facility,
    # cases they own, or incoming referrals to their facility.
    return q.filter(
        or_(
            Case.facility_id == user.facility_id,
            Case.coordinator_id == user.id,
            Referral.receiving_facility_id == user.facility_id,
        )
    )


def can_modify_case(user, case) -> bool:
    """Workflow changes need a workflow role plus own facility or named ownership (not just referral visibility)."""
    if user.role not in WORKFLOW_ROLES:
        return False
    return (
        user.role == Role.NATIONAL_ADMIN
        or case.facility_id == user.facility_id
        or case.coordinator_id == user.id
    )
