from app.core.database import Base, engine, SessionLocal
from app.core.security import hash_password
from app.models.entities import User, Facility, Patient, Screening, Case, Role, Priority, CaseStatus
from datetime import datetime, timedelta, timezone

def seed():
    Base.metadata.create_all(bind=engine)
    db=SessionLocal()
    if db.query(User).first():
        db.close(); return
    facility=Facility(name="Jusr Demo Health Centre",code="JUSR-DEMO")
    db.add(facility); db.flush()
    admin=User(email="admin@jusr.local",display_name="Demo Administrator",password_hash=hash_password("Admin123!"),role=Role.FACILITY_ADMIN,facility_id=facility.id)
    staff=User(email="coordinator@jusr.local",display_name="Demo Coordinator",password_hash=hash_password("Coordinator123!"),role=Role.COORDINATOR,facility_id=facility.id)
    referring=User(email="referring@jusr.local",display_name="Demo Referring Clinician",password_hash=hash_password("Referring123!"),role=Role.REFERRING_CLINICIAN,facility_id=facility.id)
    receiving=User(email="receiving@jusr.local",display_name="Demo Receiving Clinician",password_hash=hash_password("Receiving123!"),role=Role.RECEIVING_CLINICIAN,facility_id=facility.id,specialty="COLPOSCOPY")
    receiving2=User(email="receiving2@jusr.local",display_name="Demo Backup Receiving Clinician",password_hash=hash_password("Receiving123!"),role=Role.RECEIVING_CLINICIAN,facility_id=facility.id,specialty="COLPOSCOPY")
    db.add_all([admin,staff,referring,receiving,receiving2]); db.flush()
    for i in range(1,11):
        p=Patient(display_label=f"Patient {i:03d}",age_years=25+i,birth_year=datetime.now(timezone.utc).year-(25+i),sex="F" if i%2 else "M",is_synthetic=True,data_origin="SYNTHETIC",facility_id=facility.id)
        db.add(p); db.flush()
        s=Screening(patient_id=p.id,screening_type="Cervical screening",outcome="ABNORMAL" if i%3==0 else "NORMAL",display_label="Abnormal screening result" if i%3==0 else "Normal screening result")
        db.add(s); db.flush()
        c=Case(case_number=f"CASE-{i:04d}",patient_id=p.id,screening_id=s.id,facility_id=facility.id,coordinator_id=None if i%4==0 else staff.id,priority=Priority.URGENT if i%5==0 else (Priority.HIGH if i%3==0 else Priority.NORMAL),status=CaseStatus.OPEN if i%4==0 else CaseStatus.ASSIGNED,required_specialty="COLPOSCOPY" if i%5==0 else None,next_action_code="CONFIRMATORY_TEST" if i%3==0 else "PATIENT_NOTIFICATION",next_action_label="Confirmatory testing" if i%3==0 else "Patient notification",due_at=datetime.now(timezone.utc)+timedelta(days=i-5))
        db.add(c)
    db.commit(); db.close()

if __name__ == "__main__": seed()
