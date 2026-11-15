from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.permissions import GLOBAL_ROLES, CREATE_ROLES, visible_cases
from app.api.v1.deps import clinical_user
from app.models.entities import (
    Case,
    Facility,
    Message,
    Patient,
    PatientLink,
    Referral,
    Screening,
    User,
)
from app.schemas.patients import PatientCreate

router = APIRouter()


def visible_patients(q, user):
    """Patients from the user's own facility or cases they can access."""
    if user.role in GLOBAL_ROLES:
        return q

    on_visible_case = visible_cases(select(Case.patient_id), user)

    if user.role.name == "RECEIVING_CLINICIAN":
        return q.filter(Patient.id.in_(on_visible_case))

    return q.filter(
        or_(
            Patient.facility_id == user.facility_id,
            Patient.id.in_(on_visible_case),
        )
    )


def _out(patient):
    return {
        "id": patient.id,
        "displayLabel": patient.display_label,
        "ageYears": patient.age_years,
        "sex": patient.sex,
        "isSynthetic": patient.is_synthetic,
    }


@router.get("")
def list_patients(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1),
    db: Session = Depends(get_db),
    user=Depends(clinical_user),
):
    q = visible_patients(db.query(Patient), user).order_by(Patient.display_label)

    records = q.offset(skip).limit(min(limit, 100)).all()

    return {
        "success": True,
        "data": [_out(patient) for patient in records],
        "meta": {},
    }


@router.post("")
def create_patient(
    payload: PatientCreate,
    db: Session = Depends(get_db),
    user=Depends(clinical_user),
):
    if user.role not in CREATE_ROLES:
        raise HTTPException(403, "Insufficient permissions")

    patient = Patient(
        display_label=payload.displayLabel,
        age_years=payload.ageYears,
        birth_year=payload.birthYear,
        sex=payload.sex,
        is_synthetic=True,
        data_origin="SYNTHETIC",
        facility_id=user.facility_id,
    )

    db.add(patient)
    db.commit()
    db.refresh(patient)

    return {
        "success": True,
        "data": {
            "id": patient.id,
            "displayLabel": patient.display_label,
            "isSynthetic": True,
        },
        "meta": {},
    }


@router.get("/{patient_id}")
def get_patient(
    patient_id: str,
    db: Session = Depends(get_db),
    user=Depends(clinical_user),
):
    # Do not reveal whether a patient exists outside the user's scope.
    patient = (
        visible_patients(db.query(Patient), user)
        .filter(Patient.id == patient_id)
        .first()
    )

    if not patient:
        raise HTTPException(404, "Patient not found")

    # Only include cases the signed-in user is allowed to see.
    cases = (
        visible_cases(db.query(Case), user)
        .filter(Case.patient_id == patient.id)
        .order_by(Case.created_at.desc())
        .all()
    )

    case_records = []
    screening_records = []
    communications = []
    patient_links = []

    for case in cases:
        facility = db.get(Facility, case.facility_id)
        coordinator = db.get(User, case.coordinator_id) if case.coordinator_id else None

        screening = db.get(Screening, case.screening_id) if case.screening_id else None

        case_records.append(
            {
                "id": case.id,
                "caseNumber": case.case_number,
                "screeningType": (
                    screening.screening_type if screening else "Not recorded"
                ),
                "screeningResult": screening.outcome if screening else "Not recorded",
                "priority": case.priority.value.lower(),
                "status": case.status.value,
                "assignedTo": coordinator.display_name if coordinator else None,
                "facility": facility.name if facility else "Not recorded",
                "createdAt": case.created_at.isoformat() if case.created_at else None,
                "lastUpdated": case.updated_at.isoformat() if case.updated_at else None,
                "nextAction": case.next_action_label,
            }
        )

        if screening and screening.id not in {item["id"] for item in screening_records}:
            screening_records.append(
                {
                    "id": screening.id,
                    "screeningType": screening.screening_type,
                    "result": screening.outcome,
                    "date": None,
                    "facility": facility.name if facility else "Not recorded",
                }
            )

        # Retrieve actual messages belonging to this visible case.
        messages = (
            db.query(Message)
            .filter(Message.case_id == case.id)
            .order_by(Message.created_at.desc())
            .all()
        )

        for message in messages:
            if message.status == "SENT":
                status = "Delivered"
            elif message.status in ("MESSAGE_FAILED", "REJECTED"):
                status = "Failed"
            else:
                status = "Pending"

            communications.append(
                {
                    "id": message.id,
                    "channel": message.channel,
                    "description": f"Patient message via {message.channel}",
                    "status": status,
                    "date": (message.sent_at or message.created_at).isoformat(),
                }
            )

        # Retrieve actual patient links. Never expose token hashes.
        links = (
            db.query(PatientLink)
            .filter(
                PatientLink.case_id == case.id,
                PatientLink.patient_id == patient.id,
            )
            .order_by(PatientLink.created_at.desc())
            .all()
        )

        for link in links:
            now = datetime.now(timezone.utc)
            is_active = link.status == "ACTIVE" and link.expires_at > now

            patient_links.append(
                {
                    "id": link.id,
                    "status": "Active" if is_active else "Expired",
                    "expiresAt": link.expires_at.isoformat(),
                    "createdAt": link.created_at.isoformat(),
                }
            )

            communications.append(
                {
                    "id": link.id,
                    "channel": "Patient Link",
                    "description": "Patient secure link",
                    "status": "Pending" if is_active else "Expired",
                    "date": link.created_at.isoformat(),
                }
            )

    # Most recently created link across the cases the user can access.
    patient_links.sort(
        key=lambda item: item["createdAt"],
        reverse=True,
    )

    latest_link = patient_links[0] if patient_links else None

    communications.sort(
        key=lambda item: item["date"] or "",
        reverse=True,
    )

    return {
        "success": True,
        "data": {
            "patient": _out(patient),
            "cases": case_records,
            "screenings": screening_records,
            "communications": communications,
            "patientLink": {
                "status": latest_link["status"] if latest_link else "Not generated",
                "expiresAt": latest_link["expiresAt"] if latest_link else None,
            },
        },
        "meta": {},
    }
