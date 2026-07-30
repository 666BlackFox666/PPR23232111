import logging
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.config import get_settings
from app.db.session import SessionLocal
from app.excel.importer import import_excel
from app.excel.import_service import ImportPreviewError

settings = get_settings()
logger = logging.getLogger(__name__)


async def import_schedule_job():
    schedule_path = Path(settings.schedule_xlsx_path)
    if not schedule_path.is_file():
        logger.warning(
            "Automatic schedule import skipped: Excel file does not exist: %s",
            schedule_path,
        )
        return

    try:
        with SessionLocal() as db:
            await import_excel(db, schedule_path)
    except FileNotFoundError:
        # The file can disappear between the existence check and opening it.
        logger.warning(
            "Automatic schedule import skipped: Excel file disappeared before it could be read: %s",
            schedule_path,
        )
    except OSError as exc:
        logger.warning(
            "Automatic schedule import skipped: Excel file cannot be read: %s (%s)",
            schedule_path,
            exc,
        )
    except ImportPreviewError as exc:
        logger.warning(
            "Automatic schedule import skipped: Excel validation or apply failed: %s",
            exc,
        )
    except Exception:
        logger.exception("Automatic schedule import iteration failed.")


def setup_scheduler() -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=settings.default_timezone)
    if settings.schedule_auto_import_enabled:
        scheduler.add_job(
            import_schedule_job,
            "interval",
            minutes=settings.schedule_auto_import_interval_minutes,
            id="import_schedule",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        logger.info(
            "Automatic schedule import enabled: path=%s interval=%s minute(s).",
            settings.schedule_xlsx_path,
            settings.schedule_auto_import_interval_minutes,
        )
    else:
        logger.info(
            "Automatic schedule import disabled by SCHEDULE_AUTO_IMPORT_ENABLED=false."
        )
    if settings.notifications_auto_send_enabled:
        logger.warning("FastAPI scheduler does not send Telegram notifications. Auto-send is handled by python -m app.bot.runner.")
    return scheduler
