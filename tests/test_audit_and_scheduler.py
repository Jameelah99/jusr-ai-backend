from datetime import datetime, timedelta, timezone
import pytest
from sqlalchemy import text
from app.core.audit_guard import AuditImmutableError
from app.models.entities import AuditEvent, Case, Message, User
from app.scheduler import run_once
from app.services.audit import record_event

# ---------- R-21: audit events are append-only ----------

def test_audit_events_cannot_be_updated_or_deleted(client, org):
    client.post(f"/api/cases/{org['ids']['ca']}/delay-reason", headers=org["auth"]("coord"), json={"code": "AWAITING_RESULTS"})
    db = client.session_factory()
    event = db.query(AuditEvent).first(); eid = event.id
    event.description = "tampered"
    with pytest.raises(AuditImmutableError): db.commit()
    db.rollback()
    db.delete(db.get(AuditEvent, eid))
    with pytest.raises(AuditImmutableError): db.commit()
    db.rollback()
    # bypassing the ORM is stopped by the database trigger
    for sql in ("UPDATE audit_events SET description = 'tampered'", "DELETE FROM audit_events", "UPDATE audit_events SET actor_role = 'X' WHERE id = :i"):
        with pytest.raises(Exception, match="append-only"):
            db.execute(text(sql), {"i": eid} if ":i" in sql else {}); db.commit()
        db.rollback()
    assert db.get(AuditEvent, eid).description != "tampered" and db.query(AuditEvent).count() >= 1
    # appending still works
    user = db.get(User, org["ids"]["coord"])
    record_event(db, user, case_id=org["ids"]["ca"], event_type="X", description="appended"); db.commit()
    db.close()

def test_audit_trail_survives_workflow_actions(client, org):
    ids = org["ids"]
    client.post(f"/api/cases/{ids['ca']}/delay-reason", headers=org["auth"]("coord"), json={"code": "AWAITING_RESULTS"})
    client.post(f"/api/cases/{ids['ca']}/transition", headers=org["auth"]("coord"), json={"status": "IN_PROGRESS"})
    client.post(f"/api/cases/{ids['ca']}/transition", headers=org["auth"]("coord"), json={"status": "CLOSED"})   # invalid: logged, not changed
    kinds = [e["eventType"] for e in client.get(f"/api/cases/{ids['ca']}/events", headers=org["auth"]("coord")).json()["data"]]
    assert kinds == ["DELAY_REASON_RECORDED", "CASE_STATUS_CHANGED", "INVALID_ACTION_REJECTED"]

# ---------- scheduler ----------

def test_scheduler_pass_retries_messages_and_advances_routing_as_system(client, org):
    ids = org["ids"]; past = datetime.now(timezone.utc) - timedelta(seconds=1)
    db = client.session_factory()
    for key, spec in (("recv", "COLPOSCOPY"), ("recvB", "COLPOSCOPY")): db.get(User, ids[key]).specialty = spec
    db.commit(); db.close()
    client.post(f"/api/cases/{ids['ca']}/urgent-routing", headers=org["auth"]("coord"), json={"specialty": "COLPOSCOPY", "firstLimitMinutes": 1})
    m = client.post("/api/messages", headers=org["auth"]("coord"), json={"caseId": "CA", "channel": "SMS", "body": "Reminder"}).json()["data"]
    client.post(f"/api/messages/{m['id']}/approve", headers=org["auth"]("coord"), json={})
    db = client.session_factory()
    msg = db.get(Message, m["id"]); msg.status = "RETRYING"; msg.attempts = 1; msg.next_retry_at = past
    db.get(Case, ids["ca"]).routing_due_at = past
    db.commit(); db.close()

    result = run_once(client.session_factory)
    assert result["messages"] == {"processed": 1, "sent": 1, "retrying": 0, "failed": 0}
    assert result["urgentRouting"] == {"processed": 1, "escalated": 1, "reassigned": 0, "exceptions": 0}
    assert client.get(f"/api/messages/{m['id']}", headers=org["auth"]("coord")).json()["data"]["status"] == "SENT"
    system = [e for e in client.get(f"/api/cases/{ids['ca']}/events", headers=org["auth"]("coord")).json()["data"] if e["actorRole"] == "SYSTEM"]
    assert {e["eventType"] for e in system} == {"MESSAGE_SENT", "URGENT_ROUTING_ESCALATED"} and all(e["actorId"] is None for e in system)
    assert run_once(client.session_factory) == {"messages": {"processed": 0, "sent": 0, "retrying": 0, "failed": 0},
                                                 "urgentRouting": {"processed": 0, "escalated": 0, "reassigned": 0, "exceptions": 0}}

def test_scheduler_pass_never_raises(client):
    def broken():
        raise RuntimeError("database down")
    assert run_once(broken) == {"messages": {"error": True}, "urgentRouting": {"error": True}}

def test_openapi_lists_the_new_routes(client):
    paths = client.get("/api/v1/openapi.json").json()["paths"]
    for p in ("/api/v1/urgent-cases", "/api/v1/urgent-cases/{case_id}/accept", "/api/v1/urgent-cases/process-due",
              "/api/v1/cases/{case_id}/urgent-routing", "/api/v1/cases/{case_id}/urgent-routing/reassign"):
        assert p in paths, p
