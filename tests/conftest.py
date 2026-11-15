import os
os.environ.setdefault("SCHEDULER_ENABLED", "false")  # tests drive the scheduler explicitly
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from app.core import clock
from app.core.database import Base, get_db
from app.main import app
from app import demo_data
from app.core.security import pwd_context

pwd_context.update(bcrypt__rounds=4)  # fast hashing in tests only

@pytest.fixture(autouse=True)
def _clock():
    clock.reset(); yield; clock.reset()

@pytest.fixture()
def client():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    Base.metadata.create_all(bind=engine)
    def override():
        db = Session()
        try: yield db
        finally: db.close()
    app.dependency_overrides[get_db] = override
    with TestClient(app) as c:
        c.session_factory = Session
        yield c
    app.dependency_overrides.clear()

@pytest.fixture()
def org(client):
    db = client.session_factory()
    ids = demo_data.build_org(db)
    ids["patients"] = demo_data.add_patients(db, ids["hc"], 75)
    ids["patientsDc"] = demo_data.add_patients(db, ids["dc"], 3, start=101)
    db.commit(); db.close()
    return ids

@pytest.fixture()
def d(client, org):
    return demo_data.Driver(client, org)
