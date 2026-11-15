"""Outbound messaging. Real delivery is not assumed to exist: the default provider is simulated.

R-28: nothing is sent unless approved. R-19: a failed send is retried, staff are alerted and the message is set to MESSAGE_FAILED.
"""
from datetime import datetime, timedelta, timezone
from app.core.config import settings
from app.models.entities import Case, Message, MessageStatus
from app.services.audit import record_event
from app.services.tasks import create_task

class DeliveryError(Exception):
    pass

class SimulatedProvider:
    """Demo provider: reports success without contacting anyone."""
    def send(self, message: Message) -> None:
        return None

def get_message_provider():
    return SimulatedProvider()

def _retry_delay(attempts: int) -> timedelta:
    minutes = settings.message_retry_minutes or [5]
    return timedelta(minutes=minutes[min(attempts - 1, len(minutes) - 1)])

def attempt_delivery(db, user, message: Message, provider) -> Message:
    """One delivery attempt for an APPROVED or due RETRYING message. Caller commits."""
    assert message.status in (MessageStatus.APPROVED.value, MessageStatus.RETRYING.value) and message.approved_by_id
    now = datetime.now(timezone.utc)
    message.attempts += 1
    try:
        provider.send(message)
    except Exception as exc:  # any provider failure counts as a failed attempt
        message.last_error = str(exc)[:500] or exc.__class__.__name__
        if message.attempts < settings.message_max_attempts:
            message.status = MessageStatus.RETRYING.value
            message.next_retry_at = now + _retry_delay(message.attempts)
            record_event(db, user, case_id=message.case_id, event_type="MESSAGE_SEND_FAILED", description="Message delivery failed; will retry",
                         new_values={"messageId": message.id, "attempt": message.attempts, "nextRetryAt": message.next_retry_at.isoformat(), "error": message.last_error})
        else:
            message.status = MessageStatus.MESSAGE_FAILED.value; message.next_retry_at = None
            case = db.get(Case, message.case_id)
            create_task(db, case, "MESSAGE_FAILED", "Message could not be delivered",
                        f"{message.attempts} attempts failed. Last error: {message.last_error}. Contact the patient another way.", source_ref=message.id)
            record_event(db, user, case_id=message.case_id, event_type="MESSAGE_FAILED", description="Message delivery failed after all retries; staff alerted",
                         new_values={"messageId": message.id, "attempts": message.attempts, "error": message.last_error})
    else:
        message.status = MessageStatus.SENT.value; message.sent_at = now; message.next_retry_at = None; message.last_error = None
        record_event(db, user, case_id=message.case_id, event_type="MESSAGE_SENT", description="Message sent",
                     new_values={"messageId": message.id, "attempt": message.attempts})
    message.updated_at = now
    return message

def process_due_messages(db, actor, provider, message_query=None, now=None) -> dict:
    """Retry RETRYING messages whose retry time has arrived (R-19). Caller commits."""
    now = now or datetime.now(timezone.utc)
    q = message_query if message_query is not None else db.query(Message)
    due = q.filter(Message.status == MessageStatus.RETRYING.value, Message.next_retry_at <= now).with_for_update(skip_locked=True, of=Message).all()
    for m in due:
        attempt_delivery(db, actor, m, provider)
    return {"processed": len(due), "sent": sum(m.status == "SENT" for m in due), "retrying": sum(m.status == "RETRYING" for m in due),
            "failed": sum(m.status == "MESSAGE_FAILED" for m in due)}
