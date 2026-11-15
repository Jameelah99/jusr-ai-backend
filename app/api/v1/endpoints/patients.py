from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import or_, select
from sqlalchemy.orm import Session
from app.core.database import get_db
from app.core.permissions import GLOBAL_ROLES, CREATE_ROLES, visible_cases
from app.api.v1.deps import clinical_user
from app.models.entities import Case, Patient, Role
from app.schemas.patients import PatientCreate

router = APIRouter()

def visible_patients(q, user):
    """Patients from the user's own facility, or on a case the user can see."""
    if user.role in GLOBAL_ROLES:
        return q
    on_visible_case = visible_cases(select(Case.patient_id), user)
    if user.role == Role.RECEIVING_CLINICIAN:
        return q.filter(Patient.id.in_(on_visible_case))
    return q.filter(or_(Patient.facility_id == user.facility_id, Patient.id.in_(on_visible_case)))

def _out(p):
    return {"id": p.id, "displayLabel": p.display_label, "ageYears": p.age_years, "sex": p.sex, "isSynthetic": p.is_synthetic}

@router.get("")
def list_patients(skip: int = Query(0, ge=0), limit: int = Query(50, ge=1), db: Session = Depends(get_db), user=Depends(clinical_user)):
    q = visible_patients(db.query(Patient), user).order_by(Patient.display_label)
    return {"success": True, "data": [_out(p) for p in q.offset(skip).limit(min(limit, 100)).all()], "meta": {}}

@router.post("")
def create_patient(payload: PatientCreate, db: Session = Depends(get_db), user=Depends(clinical_user)):
    if user.role not in CREATE_ROLES:
        raise HTTPException(403, "Insufficient permissions")
    patient = Patient(display_label=payload.displayLabel, age_years=payload.ageYears, birth_year=payload.birthYear, sex=payload.sex, is_synthetic=True, data_origin="SYNTHETIC", facility_id=user.facility_id)
    db.add(patient); db.commit(); db.refresh(patient)
    return {"success": True, "data": {"id": patient.id, "displayLabel": patient.display_label, "isSynthetic": True}, "meta": {}}

@router.get("/{patient_id}")
def get_patient(patient_id: str, db: Session = Depends(get_db), user=Depends(clinical_user)):
    # 404 (not 403) for patients outside the user's scope, so existence is not disclosed.
    p = visible_patients(db.query(Patient), user).filter(Patient.id == patient_id).first()
    if not p: raise HTTPException(404, "Patient not found")
    return {"success": True, "data": _out(p), "meta": {}}
