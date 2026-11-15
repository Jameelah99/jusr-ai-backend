import os
os.environ.setdefault("SCHEDULER_ENABLED", "false")  # tests drive the scheduler explicitly
from datetime import datetime, timedelta, timezone
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from app.core.database import Base, get_db
from app.main import app

@pytest.fixture()
def client():
    engine=create_engine("sqlite://",connect_args={"check_same_thread":False},poolclass=StaticPool)
    TestingSession=sessionmaker(bind=engine,autoflush=False,autocommit=False)
    Base.metadata.create_all(bind=engine)
    def override():
        db=TestingSession()
        try: yield db
        finally: db.close()
    app.dependency_overrides[get_db]=override
    with TestClient(app) as c:
        c.session_factory=TestingSession
        yield c
    app.dependency_overrides.clear()

PW = "pw12345!"

@pytest.fixture()
def org(client):
    """Facilities A, B, C; one user per role in A; coordinator, receiving clinician and admin in B;
    receiving clinician in C; a case in A (urgent, overdue, ASSIGNED, unowned) and one in B referred to A."""
    from app.core.security import hash_password
    from app.models.entities import Facility, User, Patient, Case, Referral, Role, Priority, CaseStatus
    db = client.session_factory()
    a = Facility(name="A", code="A"); b = Facility(name="B", code="B"); f_c = Facility(name="C", code="C")
    db.add_all([a, b, f_c]); db.flush()
    def user(email, role, fac):
        u = User(email=email, display_name=email, password_hash=hash_password(PW), role=role, facility_id=fac.id if fac else None)
        db.add(u); db.flush(); return u
    users = {
        "coord": user("coord@x", Role.COORDINATOR, a), "refer": user("refer@x", Role.REFERRING_CLINICIAN, a),
        "recv": user("recv@x", Role.RECEIVING_CLINICIAN, a), "fadmin": user("fadmin@x", Role.FACILITY_ADMIN, a),
        "national": user("national@x", Role.NATIONAL_ADMIN, None), "ai": user("ai@x", Role.AI_SERVICE, None),
        "patient": user("patient@x", Role.PATIENT, a), "coordB": user("coordB@x", Role.COORDINATOR, b),
        "fadminB": user("fadminB@x", Role.FACILITY_ADMIN, b), "recvB": user("recvB@x", Role.RECEIVING_CLINICIAN, b),
        "recvB2": user("recvB2@x", Role.RECEIVING_CLINICIAN, b), "recvC": user("recvC@x", Role.RECEIVING_CLINICIAN, f_c),
    }
    pa = Patient(display_label="PA", facility_id=a.id); pb = Patient(display_label="PB", facility_id=b.id); db.add_all([pa, pb]); db.flush()
    ca = Case(case_number="CA", patient_id=pa.id, facility_id=a.id, status=CaseStatus.ASSIGNED, priority=Priority.URGENT, due_at=datetime.now(timezone.utc) - timedelta(days=1))
    cb = Case(case_number="CB", patient_id=pb.id, facility_id=b.id, status=CaseStatus.ASSIGNED)
    db.add_all([ca, cb]); db.flush()
    db.add(Referral(case_id=cb.id, receiving_facility_id=a.id, status="PENDING"))
    db.commit()
    ids = {"ca": ca.id, "cb": cb.id, "pa": pa.id, "pb": pb.id, "fa": a.id, "fb": b.id, "fc": f_c.id, **{k: v.id for k, v in users.items()}}
    db.close()
    def auth(name):
        r = client.post("/api/v1/auth/login", data={"username": f"{name}@x", "password": PW}); assert r.status_code == 200, r.text
        return {"Authorization": "Bearer " + r.json()["data"]["accessToken"]}
    return {"auth": auth, "ids": ids}
