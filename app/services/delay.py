"""R-18: an overdue case needs a reason-for-delay code before it can move on."""
from datetime import datetime, timezone
from app.models.entities import Case, CaseStatus

def is_overdue(case: Case, now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    return case.status != CaseStatus.CLOSED and case.due_at is not None and case.due_at < now

def delay_reason_missing(case: Case, now: datetime | None = None) -> bool:
    return is_overdue(case, now) and not case.delay_reason_code

DELAY_REQUIRED = dict(status_code=422, code="DELAY_REASON_REQUIRED",
                      message="This case is overdue; record a reason-for-delay code first (rule R-18)")
