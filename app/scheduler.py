"""Background processing: message retries (R-19) and urgent-routing deadlines.

Runs inside the API process when SCHEDULER_ENABLED is true, or as its own process via `python -m app.worker`.
With several API workers, enable it on one instance only (or use the worker): row locks make an overlapping run skip
rows another run is handling, but a single scheduler is simpler to reason about.
"""
import asyncio
import logging
from app.core.config import settings
from app.core.database import SessionLocal
from app.services.messaging import get_message_provider, process_due_messages
from app.services.routing import SYSTEM_ACTOR, process_due_routing

logger = logging.getLogger("app.scheduler")

def run_once(session_factory=SessionLocal) -> dict:
    """One pass over everything that may be due. Never raises; failures are logged and rolled back."""
    result = {}
    for name, job in (("messages", lambda db: process_due_messages(db, SYSTEM_ACTOR, get_message_provider())),
                      ("urgentRouting", lambda db: process_due_routing(db, SYSTEM_ACTOR))):
        db = None
        try:
            db = session_factory()
            result[name] = job(db)
            db.commit()
        except Exception:
            logger.exception("Scheduled job %s failed", name)
            result[name] = {"error": True}
            try:
                if db is not None: db.rollback()
            except Exception:
                logger.exception("Rollback after failed job %s also failed", name)
        finally:
            try:
                if db is not None: db.close()
            except Exception:
                logger.exception("Closing the session for job %s failed", name)
    return result

async def run_forever(interval_seconds: int | None = None) -> None:
    interval = interval_seconds or settings.scheduler_interval_seconds
    while True:
        await asyncio.sleep(interval)
        await asyncio.to_thread(run_once)
