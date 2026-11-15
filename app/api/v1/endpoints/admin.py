"""Facility administration (section 5, A-11): facilities, staff profiles. The facility administrator sees staff and settings, never clinical content (R-09)."""
import re
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import or_
from sqlalchemy.orm import Session
from app.core.database import get_db
from app.core.security import hash_password
from app.api.v1.deps import current_user, require_roles
from app.models.entities import Facility, Role, User
from app.services.audit import record_event
from app.services.routing import normalize_specialty

router = APIRouter()
ADMIN = require_roles("FACILITY_ADMIN", "NATIONAL_ADMIN")
STAFF_ROLES = (Role.COORDINATOR, Role.REFERRING_CLINICIAN, Role.RECEIVING_CLINICIAN, Role.FACILITY_ADMIN)

def managed_facility_ids(db, user) -> set[str] | None:
    """None means every facility (national admin). A facility administrator manages their own facility and any they added."""
    if user.role == Role.NATIONAL_ADMIN: return None
    ids = {f.id for f in db.query(Facility).filter(Facility.created_by_id == user.id).all()}
    if user.facility_id: ids.add(user.facility_id)
    return ids

def facility_out(f: Facility):
    return {"id": f.id, "name": f.name, "code": f.code, "type": f.facility_type, "connectorType": f.connector_type, "connectionStatus": f.connection_status,
            "timeZone": f.time_zone, "workingDays": f.working_days, "lastEventAt": f.last_event_at}

def staff_out(u: User):
    return {"id": u.id, "email": u.email, "displayName": u.display_name, "role": u.role.value, "facilityId": u.facility_id, "specialty": u.specialty,
            "authorisedToClose": u.authorised_to_close, "isActive": u.is_active}

# ---------------------------------------------------------------- facilities

@router.get("/facilities")
def list_facilities(db: Session = Depends(get_db), user=Depends(current_user)):
    """Every signed-in staff member can list facilities for the filters and the routing table."""
    if user.role in (Role.PATIENT, Role.AI_SERVICE): raise HTTPException(403, "Insufficient permissions")
    return {"success": True, "data": [facility_out(f) for f in db.query(Facility).order_by(Facility.name).all()], "meta": {}}

class FacilityCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    type: str | None = Field(default=None, max_length=60)
    timeZone: str | None = Field(default=None, max_length=60)
    workingDays: list[Literal["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]] | None = None
    connectorType: Literal["FEED_SIMULATOR", "CSV"] = "FEED_SIMULATOR"

@router.post("/facilities")
def add_facility(body: FacilityCreate, db: Session = Depends(get_db), user=Depends(ADMIN)):
    """A new facility appears in filters and routing with no code change or restart (A-11). Its status turns CONNECTED after a first event."""
    base = re.sub(r"[^A-Z0-9]+", "-", body.name.upper()).strip("-")[:30] or "FACILITY"
    code, n = base, 1
    while db.query(Facility.id).filter(Facility.code == code).first():
        n += 1; code = f"{base}-{n}"
    f = Facility(name=body.name, code=code, facility_type=body.type, time_zone=body.timeZone, working_days=body.workingDays, connector_type=body.connectorType,
                 connection_status="NOT_CONNECTED", created_by_id=user.id)
    db.add(f); db.flush()
    record_event(db, user, case_id=None, event_type="FACILITY_ADDED", description=f"Facility added: {f.name}", new_values=facility_out(f) | {"lastEventAt": None})
    db.commit()
    return {"success": True, "data": facility_out(f), "meta": {}}

# ---------------------------------------------------------------- staff profiles

@router.get("/staff")
def list_staff(facilityId: str | None = None, role: Role | None = None, specialty: str | None = None, db: Session = Depends(get_db),
               user=Depends(require_roles("FACILITY_ADMIN", "NATIONAL_ADMIN", "COORDINATOR"))):
    q = db.query(User).filter(User.role.in_(STAFF_ROLES))
    ids = managed_facility_ids(db, user) if user.role != Role.COORDINATOR else {user.facility_id}
    if ids is not None: q = q.filter(User.facility_id.in_(ids))
    if facilityId: q = q.filter(User.facility_id == facilityId)
    if role: q = q.filter(User.role == role)
    if specialty: q = q.filter(User.specialty == normalize_specialty(specialty))
    return {"success": True, "data": [staff_out(u) for u in q.order_by(User.display_name).all()], "meta": {}}

class StaffCreate(BaseModel):
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=255)
    displayName: str = Field(min_length=1, max_length=160)
    password: str = Field(min_length=8, max_length=128)
    role: Literal["COORDINATOR", "REFERRING_CLINICIAN", "RECEIVING_CLINICIAN", "FACILITY_ADMIN"]
    facilityId: str | None = None
    specialty: str | None = Field(default=None, max_length=60)
    authorisedToClose: bool = False

class StaffPatch(BaseModel):
    displayName: str | None = Field(default=None, min_length=1, max_length=160)
    role: Literal["COORDINATOR", "REFERRING_CLINICIAN", "RECEIVING_CLINICIAN", "FACILITY_ADMIN"] | None = None
    specialty: str | None = Field(default=None, max_length=60)
    authorisedToClose: bool | None = None
    isActive: bool | None = None

def _managed_user(db, admin, user_id) -> User:
    u = db.get(User, user_id)
    ids = managed_facility_ids(db, admin)
    if not u or u.role not in STAFF_ROLES or (ids is not None and u.facility_id not in ids):
        raise HTTPException(404, "Staff member not found")
    return u

@router.post("/staff")
def create_staff(body: StaffCreate, db: Session = Depends(get_db), user=Depends(ADMIN)):
    fid = body.facilityId or user.facility_id
    ids = managed_facility_ids(db, user)
    if not fid or not db.get(Facility, fid) or (ids is not None and fid not in ids):
        raise HTTPException(404, "Facility not found")
    if db.query(User.id).filter(User.email == body.email).first():
        raise HTTPException(409, detail={"code": "EMAIL_IN_USE", "message": "A user with this email already exists"})
    u = User(email=body.email, display_name=body.displayName, password_hash=hash_password(body.password), role=Role(body.role), facility_id=fid,
             specialty=normalize_specialty(body.specialty), authorised_to_close=body.authorisedToClose)
    db.add(u); db.flush()
    record_event(db, user, case_id=None, event_type="STAFF_CREATED", description=f"Staff profile created: {u.display_name}", new_values=staff_out(u))
    db.commit()
    return {"success": True, "data": staff_out(u), "meta": {}}

@router.patch("/staff/{user_id}")
def patch_staff(user_id: str, body: StaffPatch, db: Session = Depends(get_db), user=Depends(ADMIN)):
    u = _managed_user(db, user, user_id)
    old = staff_out(u)
    if body.displayName is not None: u.display_name = body.displayName
    if body.role is not None: u.role = Role(body.role)
    if "specialty" in body.model_fields_set: u.specialty = normalize_specialty(body.specialty)
    if body.authorisedToClose is not None: u.authorised_to_close = body.authorisedToClose
    if body.isActive is not None: u.is_active = body.isActive
    record_event(db, user, case_id=None, event_type="STAFF_UPDATED", description=f"Staff profile updated: {u.display_name}", old_values=old, new_values=staff_out(u))
    db.commit()
    return {"success": True, "data": staff_out(u), "meta": {}}
