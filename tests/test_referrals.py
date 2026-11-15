import pytest

def make(client, org, who="coord", **extra):
    """Refer case CA (facility A) to facility B. CA is overdue, so its R-18 reason is recorded first."""
    client.post(f"/api/cases/{org['ids']['ca']}/delay-reason", headers=org["auth"]("coord"), json={"code": "AWAITING_RESULTS"})
    body = {"caseId": org["ids"]["ca"], "receivingFacilityId": org["ids"]["fb"], "reason": "Needs colposcopy", **extra}
    return client.post("/api/referrals", headers=org["auth"](who), json=body)

def act(client, org, who, rid, op, body=None):
    return client.post(f"/api/referrals/{rid}/{op}", headers=org["auth"](who), json=body or {})

def err(r, status, code=None):
    assert r.status_code == status, r.text
    assert r.json()["success"] is False
    if code: assert r.json()["error"]["code"] == code

def events(client, org, who="national", case="ca"):
    rows = client.get(f"/api/cases/{org['ids'][case]}/events", headers=org["auth"](who)).json()["data"]
    return [e for e in rows if e["eventType"] != "DELAY_REASON_RECORDED"]

def test_create_moves_case_to_referred_and_audits(client, org):
    r = make(client, org); assert r.status_code == 200, r.text
    d = r.json()["data"]
    assert d["status"] == "PENDING" and d["case"]["status"] == "REFERRED" and d["receivingFacility"]["name"] == "B"
    ev = events(client, org)
    assert [e["eventType"] for e in ev] == ["REFERRAL_CREATED", "CASE_STATUS_CHANGED"]
    assert all(e["actorRole"] == "COORDINATOR" and e["actorId"] == org["ids"]["coord"] for e in ev)

def test_create_rules(client, org):
    ids = org["ids"]
    err(make(client, org, "fadmin"), 403)                       # no clinical access
    err(make(client, org, "recv"), 404)                         # receiving clinicians neither refer nor see ordinary facility cases
    err(make(client, org, "ai"), 403)                           # AI may suggest, not decide
    err(make(client, org, "coordB"), 404)                       # other facility's case is invisible
    client.post(f"/api/cases/{ids['ca']}/delay-reason", headers=org["auth"]("coord"), json={"code": "AWAITING_RESULTS"})
    err(client.post("/api/referrals", headers=org["auth"]("coord"), json={"caseId": ids["ca"], "receivingFacilityId": ids["fa"]}), 422, "INVALID_RECEIVING_FACILITY")
    err(client.post("/api/referrals", headers=org["auth"]("coord"), json={"caseId": ids["ca"], "receivingFacilityId": "nope"}), 404)
    err(make(client, org, receivingClinicianId=ids["recvC"]), 422, "INVALID_ASSIGNEE")  # clinician belongs to another facility
    err(client.post("/api/referrals", headers=org["auth"]("coord"), json={"caseId": ids["ca"]}), 422, "VALIDATION_ERROR")
    assert make(client, org, receivingClinicianId=ids["recvB"]).status_code == 200
    err(make(client, org), 409, "REFERRAL_ALREADY_ACTIVE")

def test_create_accepts_case_number(client, org):
    client.post(f"/api/cases/{org['ids']['ca']}/delay-reason", headers=org["auth"]("coord"), json={"code": "AWAITING_RESULTS"})
    r = client.post("/api/referrals", headers=org["auth"]("refer"), json={"caseId": "CA", "receivingFacilityId": org["ids"]["fb"]})
    assert r.status_code == 200

def test_create_requires_assigned_case_and_logs_refusal(client, org):
    db = client.session_factory()
    from app.models.entities import Case, CaseStatus
    db.get(Case, org["ids"]["ca"]).status = CaseStatus.OPEN; db.commit(); db.close()
    err(make(client, org), 409, "INVALID_STATUS_TRANSITION")
    assert [e["eventType"] for e in events(client, org)] == ["INVALID_ACTION_REJECTED"]
    assert client.get(f"/api/cases", headers=org["auth"]("coord")).json()["data"][0]["status"] in ("OPEN", "ASSIGNED")  # state unchanged

def test_assign_accept_consultation_flow(client, org):
    ids = org["ids"]; rid = make(client, org).json()["data"]["id"]
    err(act(client, org, "coord", rid, "assign", {"receivingClinicianId": ids["recvB"]}), 403)   # referrer's coordinator is not the receiving facility's
    err(act(client, org, "recvB", rid, "assign", {"receivingClinicianId": ids["recvB"]}), 403)  # clinicians do not assign
    err(act(client, org, "coordB", rid, "assign", {"receivingClinicianId": ids["refer"]}), 422, "INVALID_ASSIGNEE")
    assert act(client, org, "coordB", rid, "assign", {"receivingClinicianId": ids["recvB"]}).json()["data"]["receivingClinician"]["id"] == ids["recvB"]
    err(act(client, org, "recvB2", rid, "accept"), 403)         # assigned to someone else
    err(act(client, org, "coordB", rid, "accept"), 403)         # coordinators cannot decide
    err(act(client, org, "recvB", rid, "consultation", {"outcome": "x"}), 409, "INVALID_REFERRAL_STATE")  # not accepted yet
    assert act(client, org, "recvB", rid, "accept", {"note": "ok"}).json()["data"]["status"] == "ACCEPTED"
    err(act(client, org, "recvB", rid, "accept"), 409, "INVALID_REFERRAL_STATE")
    err(act(client, org, "recvB", rid, "consultation", {}), 422, "VALIDATION_ERROR")
    err(act(client, org, "recvB", rid, "consultation", {"outcome": "   "}), 422, "VALIDATION_ERROR")
    err(act(client, org, "recvB2", rid, "consultation", {"outcome": "x"}), 403)
    d = act(client, org, "recvB", rid, "consultation", {"outcome": "Treated; discharge"}).json()["data"]
    assert d["consultation"]["outcome"] == "Treated; discharge" and d["consultation"]["recordedById"] == ids["recvB"]
    err(act(client, org, "recvB", rid, "consultation", {"outcome": "again"}), 409, "CONSULTATION_ALREADY_RECORDED")
    types = [e["eventType"] for e in events(client, org)]
    assert types.count("REFERRAL_CLINICIAN_ASSIGNED") == 1 and "REFERRAL_ACCEPTED" in types and "CONSULTATION_RECORDED" in types
    assert types.count("INVALID_ACTION_REJECTED") >= 3
    assert all(e["actorRole"] for e in events(client, org))

def test_unassigned_referral_can_be_accepted_by_any_receiving_clinician(client, org):
    rid = make(client, org).json()["data"]["id"]
    d = act(client, org, "recvB2", rid, "accept").json()["data"]
    assert d["status"] == "ACCEPTED" and d["receivingClinician"]["id"] == org["ids"]["recvB2"]

def test_reject_needs_reason(client, org):
    rid = make(client, org).json()["data"]["id"]
    err(act(client, org, "recvB", rid, "reject"), 422, "VALIDATION_ERROR")
    err(act(client, org, "recvB", rid, "reject", {"reason": "  "}), 422, "VALIDATION_ERROR")
    err(act(client, org, "recv", rid, "reject", {"reason": "x"}), 403)  # sees the referral as the referring facility, but cannot decide it
    d = act(client, org, "recvB", rid, "reject", {"reason": "No capacity"}).json()["data"]
    assert d["status"] == "REJECTED" and d["rejectionReason"] == "No capacity" and d["decidedAt"]
    err(act(client, org, "recvB", rid, "accept"), 409, "INVALID_REFERRAL_STATE")
    rej = [e for e in events(client, org) if e["eventType"] == "REFERRAL_REJECTED"]
    assert len(rej) == 1 and rej[0]["actorRole"] == "RECEIVING_CLINICIAN"

def test_escalate_and_reassign(client, org):
    ids = org["ids"]; rid = make(client, org).json()["data"]["id"]
    err(act(client, org, "recvB", rid, "escalate", {"reason": "slow"}), 403)
    err(act(client, org, "coord", rid, "escalate"), 422, "VALIDATION_ERROR")
    d = act(client, org, "coord", rid, "escalate", {"reason": "Urgent, no response"}).json()["data"]
    assert d["escalated"] is True and d["escalationReason"] == "Urgent, no response" and d["status"] == "PENDING"
    err(act(client, org, "coord", rid, "escalate", {"reason": "again"}), 409, "INVALID_REFERRAL_STATE")
    err(act(client, org, "coord", rid, "reassign", {"receivingFacilityId": ids["fb"], "reason": "r"}), 422, "INVALID_RECEIVING_FACILITY")  # same facility
    err(act(client, org, "coord", rid, "reassign", {"receivingFacilityId": ids["fc"]}), 422, "VALIDATION_ERROR")
    err(act(client, org, "recvB", rid, "reassign", {"receivingFacilityId": ids["fc"], "reason": "r"}), 403)
    d = act(client, org, "coord", rid, "reassign", {"receivingFacilityId": ids["fc"], "reason": "B is full"}).json()["data"]
    assert d["receivingFacility"]["name"] == "C" and d["status"] == "PENDING" and d["escalated"] is False
    # old receiving facility loses sight of it; new one gains it
    assert client.get(f"/api/referrals/{rid}", headers=org["auth"]("recvB")).status_code == 404
    assert client.get(f"/api/referrals/{rid}", headers=org["auth"]("recvC")).status_code == 200

def test_reassign_after_rejection_restarts_the_referral(client, org):
    ids = org["ids"]; rid = make(client, org).json()["data"]["id"]
    act(client, org, "recvB", rid, "reject", {"reason": "No capacity"})
    d = act(client, org, "refer", rid, "reassign", {"receivingFacilityId": ids["fc"], "reason": "Try C"}).json()["data"]
    assert d["status"] == "PENDING" and d["rejectionReason"] is None
    assert act(client, org, "recvC", rid, "accept").json()["data"]["status"] == "ACCEPTED"
    err(act(client, org, "coord", rid, "reassign", {"receivingFacilityId": ids["fb"], "reason": "r"}), 409, "INVALID_REFERRAL_STATE")

def test_receiving_clinician_sees_case_referred_to_them(client, org):
    rid = make(client, org).json()["data"]["id"]
    assert client.get(f"/api/cases/{org['ids']['ca']}/events", headers=org["auth"]("recvB")).status_code == 200
    assert client.get(f"/api/cases/{org['ids']['ca']}/events", headers=org["auth"]("recvC")).status_code == 404
    r = client.post(f"/api/cases/{org['ids']['ca']}/transition", headers=org["auth"]("recvB"), json={"status": "IN_PROGRESS"})
    assert r.status_code == 403  # visible, but workflow stays with the referring facility

def test_list_filters_and_scope(client, org):
    ids = org["ids"]; make(client, org)
    lst = lambda who, q="": client.get(f"/api/referrals{q}", headers=org["auth"](who)).json()
    assert lst("coord")["meta"]["total"] == 2                                   # outgoing A->B plus seeded incoming B->A
    assert lst("coord", "?direction=incoming")["meta"]["total"] == 1            # CB -> A (seeded)
    assert lst("coord", "?direction=outgoing")["data"][0]["case"]["caseId"] == "CA"
    assert lst("recvC")["meta"]["total"] == 0 and lst("national")["meta"]["total"] == 2
    assert lst("national", "?status=ACCEPTED")["meta"]["total"] == 0
    assert lst("national", "?caseId=CA")["meta"]["total"] == 1
    assert lst("national", "?escalated=false")["meta"]["total"] == 2
    err(client.get("/api/referrals?status=BOGUS", headers=org["auth"]("coord")), 422, "VALIDATION_ERROR")
    err(client.get("/api/referrals?direction=sideways", headers=org["auth"]("coord")), 422, "VALIDATION_ERROR")
    for who in ("fadmin", "patient"):
        err(client.get("/api/referrals", headers=org["auth"](who)), 403)
    assert client.get("/api/referrals", headers=org["auth"]("ai")).status_code == 200

def test_dashboard_reflects_referrals(client, org):
    ids = org["ids"]; rid = make(client, org).json()["data"]["id"]
    act(client, org, "coordB", rid, "assign", {"receivingClinicianId": ids["recvB"]})
    d = client.get("/api/dashboard/summary", headers=org["auth"]("recvB")).json()["data"]["referrals"]
    assert d["incoming"] == 1 and d["assignedToMe"] == 1 and d["outstandingDecisions"] == 1 and d["urgent"] == 1
    d2 = client.get("/api/dashboard/summary", headers=org["auth"]("recvB2")).json()["data"]["referrals"]
    assert d2["assignedToMe"] == 0 and d2["outstandingDecisions"] == 0
    assert "awaitingReview" not in client.get("/api/dashboard/summary", headers=org["auth"]("refer")).json()["data"]["stats"]

def test_invalid_case_transition_is_logged(client, org):
    r = client.post(f"/api/cases/{org['ids']['ca']}/transition", headers=org["auth"]("coord"), json={"status": "CLOSED"})
    err(r, 409, "INVALID_STATUS_TRANSITION")
    ev = events(client, org)
    assert ev[-1]["eventType"] == "INVALID_ACTION_REJECTED" and ev[-1]["actorRole"] == "COORDINATOR"
