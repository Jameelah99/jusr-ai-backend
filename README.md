# Jusr AI FastAPI Backend

A starter backend implementation for the Jusr AI hackathon architecture. It uses FastAPI, SQLAlchemy, Alembic and PostgreSQL. The demo seed contains synthetic patient records only.

## Included
- FastAPI application with versioned `/api/v1` routes
- SQLAlchemy domain models for users, facilities, patients, screenings, cases, referrals, consultations and audit events
- Alembic initial migration
- JWT login and current-user endpoint
- Facility-scoped case and worklist queries
- Worklist filtering, sorting and page-based pagination
- Dashboard summary endpoint
- Case assignment and validated status transitions with audit events
- Synthetic demo seed records
- Docker Compose PostgreSQL and API setup
- Basic automated health and OpenAPI tests

## Run with Docker
```bash
cp .env.example .env
docker compose up --build
```
API: http://localhost:8000  
Swagger: http://localhost:8000/docs  
OpenAPI JSON: http://localhost:8000/api/v1/openapi.json

Demo accounts:
- `admin@jusr.local` / `Admin123!`
- `coordinator@jusr.local` / `Coordinator123!`
- `referring@jusr.local` / `Referring123!`
- `receiving@jusr.local` / `Receiving123!` (colposcopy)
- `receiving2@jusr.local` / `Receiving123!` (colposcopy, backup clinician)

`admin@jusr.local` is a Facility Administrator and, by design, sees staff and facility information only, not patients or cases.

Login uses OAuth2 form fields: `username` is the email and `password` is the password.

## Run locally
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
alembic upgrade head
python -m app.seed
uvicorn app.main:app --reload
```
On Windows PowerShell, activate with `.venv\Scripts\Activate.ps1`.

## Worklist
`GET /api/v1/worklist?page=1&pageSize=20&priority=URGENT&status=OPEN&overdue=true&sortBy=dueAt&sortOrder=asc`

Supported filters: `search`, `priority`, `status`, `facilityId`, `coordinatorId`, `screeningType`, `overdue`. Sort fields: `dueAt`, `updatedAt`, `createdAt`, `priority`.

Response fields: `summary`, `cases`, `total`, `page`, `pageSize`, `totalPages`.

## Paths, roles and access
Routes are served under `/api/v1` and, for the frontend register, also under `/api` (for example `/api/worklist`); only `/api/v1` appears in OpenAPI. Run `alembic upgrade head` on existing databases: revision 0002 adds the new roles and `patients.facility_id`; 0003 adds the referral columns and `audit_events.actor_role`; 0004 adds patient links, responses, tasks, AI suggestions, messages, the R-18 case columns and `patients.birth_year`; 0005 adds urgent routing columns, `users.specialty` and the audit triggers. Existing patients have no birth year, so link verification only applies once it is set.

Roles: `COORDINATOR`, `REFERRING_CLINICIAN`, `RECEIVING_CLINICIAN`, `FACILITY_ADMIN`, `NATIONAL_ADMIN`, `AI_SERVICE`, `PATIENT`. Role and facility always come from the session token's user record, never from request parameters. `FACILITY_ADMIN` and `PATIENT` receive 403 on all patient and case endpoints, and the admin dashboard contains staff and facility data only (R-09). Users see their own facility's cases and patients, plus cases they own in other facilities; receiving clinicians also see cases referred to their facility. Only coordinators and national admins assign; coordinators, clinicians and national admins change workflow state within their own facility or owned cases; `AI_SERVICE` is read-only. Rules live in `app/core/permissions.py`.

## Referrals
`GET /referrals` (filters: `status`, `direction=incoming|outgoing`, `escalated`, `caseId`, `page`, `pageSize`), `GET /referrals/{id}`, and `POST` on `/referrals` (create), `/{id}/assign`, `/{id}/accept`, `/{id}/reject`, `/{id}/escalate`, `/{id}/reassign` and `/{id}/consultation`. `caseId` on create accepts the case id or case number.

- **Create** (coordinator, referring clinician, national admin; own facility or owned case): the case must be `ASSIGNED` and becomes `REFERRED`; one active referral per case; the receiving facility must differ from the case's facility.
- **Assign receiving clinician:** coordinator at the receiving facility or national admin; the assignee must be an active receiving clinician at that facility.
- **Accept / reject:** a receiving clinician at the receiving facility (the assigned one, or any if unassigned, who then becomes assigned). Rejection requires a reason.
- **Escalate:** referring side, on a `PENDING` referral, with a reason; it stays `PENDING` and is flagged `escalated`.
- **Reassign:** referring side, on a `PENDING` or `REJECTED` referral, to a different facility with a reason; it returns to `PENDING` with no clinician.
- **Consultation outcome:** the assigned receiving clinician, on an `ACCEPTED` referral, once.

Every operation writes an audit event with the actor's id and role. Refused actions (invalid case transitions, invalid referral states, missing A-01 evidence) change nothing and are logged as `INVALID_ACTION_REJECTED` (R-12). Receiving clinicians can read a case referred to their facility but cannot change its workflow.

## Urgent specialty routing
An URGENT case is routed to a receiving clinician whose `specialty` matches the case's required specialty. Receiving clinicians work from their own list and have no access to the general worklist or case list (`403`); they reach only cases referred to their facility or routed to them.

Staff (coordinator, referring clinician, national admin):
- `POST /cases/{id}/urgent-routing` `{specialty?, receivingClinicianId?, firstLimitMinutes?, secondLimitMinutes?}` routes the case. Without `receivingClinicianId` a qualified clinician is chosen (same facility first); with no qualified clinician the case goes straight to `EXCEPTION` and a coordinator task is opened. A clinician with the wrong specialty is refused (422 `SPECIALTY_MISMATCH`). The specialty can also be set on the case (`requiredSpecialty` on create).
- `GET /cases/{id}/urgent-routing` shows the routing state; `POST /cases/{id}/urgent-routing/reassign` `{receivingClinicianId, reason}` hands an awaiting or exception case to another qualified clinician (reason required).

Receiving clinician:
- `GET /urgent-cases` lists urgent cases routed to the caller (`?status=AWAITING_ACCEPTANCE|ACCEPTED`), with patient label, age, sex, screening, facility, next action, specialty, `routingStatus` and `routingDueAt`.
- `POST /urgent-cases/{caseId}/accept` accepts one. The clinician's specialty is validated again at acceptance (403 `SPECIALTY_MISMATCH`, logged).

Case routing statuses: `AWAITING_ACCEPTANCE`, `ACCEPTED`, `EXCEPTION`. Time limits are configurable (`URGENT_ACCEPTANCE_MINUTES`, default 30, and `URGENT_ESCALATION_MINUTES`, default 30) and can be overridden per routing request. When the first limit expires the coordinator is alerted by a task (`URGENT_ROUTING_ESCALATED`, stage 1) and the clinician may still accept; when the second limit expires the case is routed to another qualified, active clinician who has not yet been offered it (stage 2, new first-limit window); if none exists, or the backup also lets the limit pass, the case becomes `EXCEPTION` and a coordinator task is opened. Audit events: `URGENT_CASE_ROUTED`, `URGENT_CASE_ACCEPTED`, `URGENT_ROUTING_ESCALATED`, `URGENT_ROUTING_REASSIGNED`, `URGENT_ROUTING_EXCEPTION`. This is separate from referral escalation. Specialty is a free-text code normalised to upper case (`colposcopy` becomes `COLPOSCOPY`); there is no endpoint yet to assign a specialty to a user, so set `users.specialty` directly or in the seed.

## Scheduler
Message retries and urgent-routing deadlines are processed automatically every `SCHEDULER_INTERVAL_SECONDS` (default 60). By default the API process runs the loop (`SCHEDULER_ENABLED=true`). With several API workers, set it to `false` on the API and run `python -m app.worker` as one separate process; `docker-compose.yml` does this. `POST /messages/process-due` and `POST /urgent-cases/process-due` run the same processing on demand. Work done by the scheduler is audited with actor role `SYSTEM`.

## Audit integrity (R-21)
`audit_events` is append-only: the ORM refuses updates and deletes, and database triggers (PostgreSQL and SQLite) reject any `UPDATE` or `DELETE`, including raw SQL. The triggers are created by migration 0005. A table owner can still drop a trigger, so also revoke the permissions from the role the API connects as: `REVOKE UPDATE, DELETE, TRUNCATE ON audit_events FROM <api_role>;`.

## Overdue reason-for-delay codes (R-18)
`POST /cases/{id}/delay-reason` with `{"code": "...", "note": "..."}` records why an overdue case is late (only on an overdue, open case). Codes (`GET /cases/delay-reasons`): `PATIENT_UNREACHABLE`, `PATIENT_DECLINED`, `AWAITING_RESULTS`, `AWAITING_REFERRAL_RESPONSE`, `TRANSPORT_OR_ACCESS`, `STAFF_CAPACITY`, `FACILITY_CAPACITY`, `OTHER` (needs a note). The code set is a proposal; confirm it against the Developer Guide. While an overdue case has no code, case transitions and referral creation are refused with 422 `DELAY_REASON_REQUIRED`. The worklist returns `delayReason` and `delayReasonRequired` per case, and the coordinator and referring-clinician dashboards return `missingDelayReason`.

## Patient secure links and assisted responses
Staff: `POST /cases/{id}/links` generates a link (or resends, which cancels the earlier one; expiry `PATIENT_LINK_EXPIRY_DAYS`, default 7); `GET /cases/{id}/links` lists link status without tokens. The token is 256-bit random, shown once, stored only as a SHA-256 hash, and the URL holds no personal data.

Patient (no staff login), served at `/p/{token}` and not under `/api`: `GET /p/{token}` (only what the patient needs), `POST /p/{token}/verify` `{birthYear}`, `/ack`, `/understanding` `{understood}`, `/confidence` `{rating 1-5}`, `/not-explained`, `/callback` `{preferredTime?, note?}`. Birth-year verification applies when the patient has a `birth_year` and `PATIENT_LINK_VERIFY_BIRTH_YEAR` is true; a successful verify returns a 30-minute `sessionToken` to send as `X-Patient-Session`. Three wrong attempts (`PATIENT_LINK_MAX_ATTEMPTS`) lock the link (423 `LINK_LOCKED`) and open a coordinator task; resending replaces it. Unknown links return 404, replaced or expired links 410.

`POST /cases/{id}/assisted-responses` records a response obtained by staff (all fields required: `acknowledged`, `understood`, `confidence`, `explanationStatus`, `callbackRequested`, optional `note`). It is stored with `source: STAFF_ASSISTED` and the recording staff member's id, role and time, and is never presented as the patient's own. `GET /cases/{id}/patient-responses` shows both kinds with their source.

`GET /tasks` and `POST /tasks/{id}/complete` expose coordinator tasks created by locked links, call-back requests, not-explained responses and failed messages.

## AI-assisted workflows (human approval)
The `AI_SERVICE` role can only `POST /ai/suggestions`; it cannot change anything else. Kinds and their payloads: `INTAKE` (proposed new case), `URGENCY` (priority and actionability with rationale), `MESSAGE_DRAFT`, `CASE_SUMMARY` (every claim set needs `citations` that point at real records of that case) and `REFERRAL_TARGET`. A suggestion stays `PENDING` and changes nothing until a coordinator, referring clinician or national admin calls `POST /ai/suggestions/{id}/approve` (which then applies it as that staff member, under the same rules as a direct action) or `/reject` (reason required). `GET /ai/suggestions`, `GET /ai/suggestions/{id}` and `GET /ai/suggestions/cases/{caseId}/summary` (latest approved summary) read them.

Messages (`/messages`): staff or an approved AI draft creates a message; it must be `APPROVED` before `POST /messages/{id}/send` will send it (R-28). A failed send is retried (`MESSAGE_MAX_ATTEMPTS` total attempts, waiting `MESSAGE_RETRY_MINUTES` between them), each failure is audited, and after the last one the message is set to `MESSAGE_FAILED` and a coordinator task alerts staff (R-19). Retries run when `POST /messages/process-due` is called, so schedule that call (for example every minute); no background worker runs inside the API. Delivery uses a simulated provider that always succeeds, because no real messaging service is assumed; replace `get_message_provider` in `app/services/messaging.py` with a real one.

## Errors
All errors use one envelope, including validation failures (422) and unhandled exceptions (500): `{"success": false, "error": {"code", "message", "details"}, "meta": {"requestId"}}`. Every response carries an `X-Request-ID` header.

## Case transitions and rule A-01
`POST /api/v1/cases/{id}/transition` takes `{"status": "...", "evidence": "...", "decision": "..."}`. Moving a case to `RESOLVED` requires `evidence` or `decision` (422 `EVIDENCE_REQUIRED` otherwise). Moving it to `CLOSED` requires one too, unless the resolution already recorded one. The text is stored on the audit event.

## Important implementation notes
This package is an initial runnable implementation baseline, not a production-hardened clinical system. Before production use, complete the following:
- Add refresh-token rotation, logout/revocation and password reset flows.
- Apply field-level authorization to patient age and sex and all patient endpoints.
- Add comprehensive authorization checks to every resource and query.
- Add schema validation models for all request and response payloads, for the remaining payloads.
- Add pagination caps, database indexes, query optimization and cursor pagination if needed.
- Add workflow-specific referral acceptance/rejection/cancellation and consultation endpoints with transactional audit records.
- Make audit events append-only at the database permission level.
- Add migration revisions generated from metadata rather than relying on `create_all` in migration.
- Add rate limiting, secure secret management, HTTPS, logging, monitoring, backups and security review.
- Confirm status-transition rules, dashboard denominator rules and active-case definition with product owners.
- Add integration and authorization tests for all roles, facilities and workflow branches.
- Do not connect real EMR systems in demo mode. All seeded patients are synthetic.
