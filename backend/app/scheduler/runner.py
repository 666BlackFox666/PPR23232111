import logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.config import get_settings
from app.db.session import SessionLocal
from app.excel.importer import import_excel

settings = get_settings()
logger = logging.getLogger(__name__)


async def import_schedule_job():
    with SessionLocal() as db:
        await import_excel(db, settings.schedule_xlsx_path)


def setup_scheduler() -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=settings.default_timezone)
    if settings.schedule_auto_import_enabled:
        scheduler.add_job(import_schedule_job, "interval", minutes=10, id="import_schedule", replace_existing=True)
    else:
        logger.info("Automatic schedule import disabled.")
    if settings.notifications_auto_send_enabled:
        logger.warning("FastAPI scheduler does not send Telegram notifications. Auto-send is handled by python -m app.bot.runner.")
    return scheduler
