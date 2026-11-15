import pytest

def err(r, status, code=None):
    assert r.status_code == status, r.text
    assert r.json()["success"] is False
    if code: assert r.json()["error"]["code"] == code

def delay(client, org, who="coord", **body):
    return client.post(f"/api/cases/{org['ids']['ca']}/delay-reason", headers=org["auth"](who), json=body or {"code": "AWAITING_RESULTS"})

def transition(client, org, status, who="coord", **extra):
    return client.post(f"/api/cases/{org['ids']['ca']}/transition", headers=org["auth"](who), json={"status": status, **extra})

def events(client, org):
    return client.get(f"/api/cases/{org['ids']['ca']}/events", headers=org["auth"]("national")).json()["data"]

def test_overdue_case_cannot_move_without_a_delay_reason(client, org):
    err(transition(client, org, "IN_PROGRESS"), 422, "DELAY_REASON_REQUIRED")
    assert events(client, org)[-1]["eventType"] == "INVALID_ACTION_REJECTED"      # logged (R-12), nothing changed
    assert client.get("/api/cases?status=ASSIGNED", headers=org["auth"]("coord")).json()["meta"]["total"] == 1
    assert delay(client, org).status_code == 200
    assert transition(client, org, "IN_PROGRESS").status_code == 200

def test_invalid_transition_is_reported_before_missing_delay_reason(client, org):
    err(transition(client, org, "CLOSED"), 409, "INVALID_STATUS_TRANSITION")

def test_delay_reason_validation_and_permissions(client, org):
    err(delay(client, org, code="BOGUS"), 422, "VALIDATION_ERROR")
    err(delay(client, org, code="OTHER"), 422, "VALIDATION_ERROR")                  # OTHER needs a note
    err(delay(client, org, code="OTHER", note="   "), 422, "VALIDATION_ERROR")
    assert delay(client, org, code="OTHER", note="Clinic flooded").status_code == 200
    for who, status in (("fadmin", 403), ("recv", 404), ("ai", 403), ("coordB", 404)):
        err(delay(client, org, who), status)

def test_delay_reason_only_on_overdue_cases(client, org):
    r = client.post(f"/api/cases/{org['ids']['cb']}/delay-reason", headers=org["auth"]("coordB"), json={"code": "STAFF_CAPACITY"})
    err(r, 409, "CASE_NOT_OVERDUE")

def test_reason_can_be_updated_and_is_audited_with_role(client, org):
    delay(client, org, code="AWAITING_RESULTS")
    r = delay(client, org, who="refer", code="PATIENT_UNREACHABLE", note="Phone off")
    assert r.json()["data"]["delayReason"]["code"] == "PATIENT_UNREACHABLE"
    ev = [e for e in events(client, org) if e["eventType"] == "DELAY_REASON_RECORDED"]
    assert [e["actorRole"] for e in ev] == ["COORDINATOR", "REFERRING_CLINICIAN"]

def test_worklist_and_dashboard_expose_it(client, org):
    row = lambda: next(c for c in client.get("/api/worklist", headers=org["auth"]("coord")).json()["data"]["cases"] if c["caseId"] == "CA")
    assert row()["delayReasonRequired"] is True and row()["delayReason"] is None
    assert client.get("/api/dashboard/summary", headers=org["auth"]("coord")).json()["data"]["stats"]["missingDelayReason"] == 1
    delay(client, org)
    assert row()["delayReasonRequired"] is False and row()["delayReason"]["code"] == "AWAITING_RESULTS"
    assert client.get("/api/dashboard/summary", headers=org["auth"]("coord")).json()["data"]["stats"]["missingDelayReason"] == 0

def test_referral_needs_delay_reason_too(client, org):
    body = {"caseId": org["ids"]["ca"], "receivingFacilityId": org["ids"]["fb"]}
    err(client.post("/api/referrals", headers=org["auth"]("coord"), json=body), 422, "DELAY_REASON_REQUIRED")
    delay(client, org)
    assert client.post("/api/referrals", headers=org["auth"]("coord"), json=body).status_code == 200

def test_code_list(client, org):
    codes = client.get("/api/cases/delay-reasons", headers=org["auth"]("coord")).json()["data"]
    assert "OTHER" in codes and "AWAITING_RESULTS" in codes
