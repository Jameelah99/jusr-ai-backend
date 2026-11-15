import enum
import uuid
from datetime import datetime, timezone
from sqlalchemy import String, Boolean, DateTime, ForeignKey, Integer, Float, Text, Enum, JSON, false, event
from sqlalchemy.types import TypeDecorator
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.core.database import Base
from app.core import audit_guard

def uid():
    return str(uuid.uuid4())

class UTCDateTime(TypeDecorator):
    """Timezone-aware UTC datetime on every backend.

    PostgreSQL returns aware values for timestamptz, but SQLite drops the offset
    and returns naive values. This normalises both directions so comparisons with
    datetime.now(timezone.utc) never mix naive and aware datetimes.
    """
    impl = DateTime
    cache_ok = True

    def __init__(self):
        super().__init__(timezone=True)

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

class Role(str, enum.Enum):
    PATIENT = "PATIENT"
    COORDINATOR = "COORDINATOR"
    REFERRING_CLINICIAN = "REFERRING_CLINICIAN"
    RECEIVING_CLINICIAN = "RECEIVING_CLINICIAN"
    FACILITY_ADMIN = "FACILITY_ADMIN"
    NATIONAL_ADMIN = "NATIONAL_ADMIN"
    AI_SERVICE = "AI_SERVICE"

class CaseStatus(str, enum.Enum):
    OPEN = "OPEN"
    ASSIGNED = "ASSIGNED"
    REFERRED = "REFERRED"
    IN_PROGRESS = "IN_PROGRESS"
    RESOLVED = "RESOLVED"
    CLOSED = "CLOSED"

class ReferralStatus(str, enum.Enum):
    PENDING = "PENDING"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"

class DelayReason(str, enum.Enum):
    """R-18 reason-for-delay codes (proposed set; confirm with the Developer Guide)."""
    PATIENT_UNREACHABLE = "PATIENT_UNREACHABLE"
    PATIENT_DECLINED = "PATIENT_DECLINED"
    AWAITING_RESULTS = "AWAITING_RESULTS"
    AWAITING_REFERRAL_RESPONSE = "AWAITING_REFERRAL_RESPONSE"
    TRANSPORT_OR_ACCESS = "TRANSPORT_OR_ACCESS"
    STAFF_CAPACITY = "STAFF_CAPACITY"
    FACILITY_CAPACITY = "FACILITY_CAPACITY"
    OTHER = "OTHER"

class LinkStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    SUPERSEDED = "SUPERSEDED"
    LOCKED = "LOCKED"

class AiKind(str, enum.Enum):
    INTAKE = "INTAKE"
    URGENCY = "URGENCY"
    MESSAGE_DRAFT = "MESSAGE_DRAFT"
    CASE_SUMMARY = "CASE_SUMMARY"
    REFERRAL_TARGET = "REFERRAL_TARGET"

class RoutingStatus(str, enum.Enum):
    """Case-level status of urgent specialty routing."""
    AWAITING_ACCEPTANCE = "AWAITING_ACCEPTANCE"
    ACCEPTED = "ACCEPTED"
    EXCEPTION = "EXCEPTION"

class SuggestionStatus(str, enum.Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"

class MessageStatus(str, enum.Enum):
    DRAFT = "DRAFT"
    APPROVED = "APPROVED"
    RETRYING = "RETRYING"
    SENT = "SENT"
    MESSAGE_FAILED = "MESSAGE_FAILED"
    REJECTED = "REJECTED"

class Priority(str, enum.Enum):
    URGENT = "URGENT"
    HIGH = "HIGH"
    NORMAL = "NORMAL"

class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(160))
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[Role] = mapped_column(Enum(Role), default=Role.COORDINATOR)
    facility_id: Mapped[str | None] = mapped_column(ForeignKey("facilities.id"), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    specialty: Mapped[str | None] = mapped_column(String(60), nullable=True)  # receiving clinicians; matched against a case's required specialty

class Facility(Base):
    __tablename__ = "facilities"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(200))
    code: Mapped[str] = mapped_column(String(40), unique=True)

class Patient(Base):
    __tablename__ = "patients"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    display_label: Mapped[str] = mapped_column(String(120))
    age_years: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sex: Mapped[str | None] = mapped_column(String(30), nullable=True)
    is_synthetic: Mapped[bool] = mapped_column(Boolean, default=True)
    data_origin: Mapped[str] = mapped_column(String(30), default="SYNTHETIC")
    facility_id: Mapped[str | None] = mapped_column(ForeignKey("facilities.id"), nullable=True)
    birth_year: Mapped[int | None] = mapped_column(Integer, nullable=True)  # used only to verify patient secure links; never returned by staff APIs

class Screening(Base):
    __tablename__ = "screenings"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    patient_id: Mapped[str] = mapped_column(ForeignKey("patients.id"))
    screening_type: Mapped[str] = mapped_column(String(100))
    outcome: Mapped[str] = mapped_column(String(100))
    display_label: Mapped[str] = mapped_column(String(200))

class Case(Base):
    __tablename__ = "cases"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    case_number: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    patient_id: Mapped[str] = mapped_column(ForeignKey("patients.id"))
    screening_id: Mapped[str | None] = mapped_column(ForeignKey("screenings.id"), nullable=True)
    facility_id: Mapped[str] = mapped_column(ForeignKey("facilities.id"))
    coordinator_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    priority: Mapped[Priority] = mapped_column(Enum(Priority), default=Priority.NORMAL)
    status: Mapped[CaseStatus] = mapped_column(Enum(CaseStatus), default=CaseStatus.OPEN)
    next_action_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    next_action_label: Mapped[str | None] = mapped_column(String(200), nullable=True)
    due_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    delay_reason_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    delay_reason_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    delay_reason_recorded_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    delay_reason_recorded_by_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    # Urgent specialty routing
    required_specialty: Mapped[str | None] = mapped_column(String(60), nullable=True)
    routing_status: Mapped[str | None] = mapped_column(String(30), nullable=True, index=True)
    routed_clinician_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    routing_stage: Mapped[int] = mapped_column(Integer, default=0, server_default="0")  # 0 primary, 1 escalated to coordinator, 2 backup clinician
    routing_first_limit_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    routing_second_limit_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    routing_started_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    routing_due_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    routing_accepted_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    routing_exception_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    routing_tried: Mapped[list | None] = mapped_column(JSON, nullable=True)  # clinician ids already offered the case
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

class Referral(Base):
    __tablename__ = "referrals"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id"))
    status: Mapped[str] = mapped_column(String(30), default="PENDING")
    receiving_facility_id: Mapped[str | None] = mapped_column(ForeignKey("facilities.id"), nullable=True)
    receiving_clinician_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    referred_by_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    escalated: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    escalation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    escalated_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

class Consultation(Base):
    __tablename__ = "consultations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id"))
    clinician_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    referral_id: Mapped[str | None] = mapped_column(ForeignKey("referrals.id"), nullable=True)
    notes: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=lambda: datetime.now(timezone.utc))

class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    actor_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    actor_role: Mapped[str | None] = mapped_column(String(40), nullable=True)
    case_id: Mapped[str | None] = mapped_column(ForeignKey("cases.id"), nullable=True)
    event_type: Mapped[str] = mapped_column(String(100))
    description: Mapped[str] = mapped_column(String(500))
    old_values: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    new_values: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=lambda: datetime.now(timezone.utc))


def _now():
    return datetime.now(timezone.utc)

class PatientLink(Base):
    """Secure patient link. Only a SHA-256 hash of the random token is stored."""
    __tablename__ = "patient_links"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id"), index=True)
    patient_id: Mapped[str] = mapped_column(ForeignKey("patients.id"))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(20), default=LinkStatus.ACTIVE.value)
    failed_attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_by_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_now)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime())
    superseded_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    locked_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    last_accessed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)

class PatientResponse(Base):
    """A response about a case: submitted by the patient via link, or recorded by staff on their behalf."""
    __tablename__ = "patient_responses"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id"), index=True)
    kind: Mapped[str] = mapped_column(String(30))
    value: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    source: Mapped[str] = mapped_column(String(20))  # PATIENT_LINK or STAFF_ASSISTED
    link_id: Mapped[str | None] = mapped_column(ForeignKey("patient_links.id"), nullable=True)
    submission_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    recorded_by_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    recorded_by_role: Mapped[str | None] = mapped_column(String(40), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_now)

class Task(Base):
    __tablename__ = "tasks"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id"), index=True)
    facility_id: Mapped[str] = mapped_column(ForeignKey("facilities.id"))
    type: Mapped[str] = mapped_column(String(40))
    title: Mapped[str] = mapped_column(String(200))
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="OPEN")
    assigned_to_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    source_ref: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_now)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    completed_by_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)

class AiSuggestion(Base):
    """A read-only AI suggestion. Nothing changes until a staff member approves it."""
    __tablename__ = "ai_suggestions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    kind: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(20), default=SuggestionStatus.PENDING.value)
    case_id: Mapped[str | None] = mapped_column(ForeignKey("cases.id"), nullable=True, index=True)
    facility_id: Mapped[str | None] = mapped_column(ForeignKey("facilities.id"), nullable=True)
    payload: Mapped[dict] = mapped_column(JSON)
    model: Mapped[str | None] = mapped_column(String(80), nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_by_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_now)
    decided_by_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    decision_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    applied: Mapped[dict | None] = mapped_column(JSON, nullable=True)

class Message(Base):
    """Outbound patient message. It cannot be sent until a staff member approves it (R-28)."""
    __tablename__ = "messages"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id"), index=True)
    facility_id: Mapped[str] = mapped_column(ForeignKey("facilities.id"))
    channel: Mapped[str] = mapped_column(String(20))
    body: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default=MessageStatus.DRAFT.value)
    source: Mapped[str] = mapped_column(String(20), default="STAFF")
    suggestion_id: Mapped[str | None] = mapped_column(ForeignKey("ai_suggestions.id"), nullable=True)
    created_by_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    approved_by_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_retry_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_now, onupdate=_now)


# ---- R-21: audit events are append-only ----
@event.listens_for(AuditEvent, "before_update")
@event.listens_for(AuditEvent, "before_delete")
def _audit_is_append_only(mapper, connection, target):
    raise audit_guard.AuditImmutableError(audit_guard.MESSAGE)

@event.listens_for(AuditEvent.__table__, "after_create")
def _install_audit_triggers(target, connection, **kw):
    audit_guard.install(connection)
