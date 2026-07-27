from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "dev"
    env: str = ""
    dev_commands_enabled: bool = False
    deployment_mode: Literal["bot_only", "full"] = "bot_only"
    database_url: str = "postgresql+psycopg://ppr_user:ppr_password@localhost:5432/ppr_db"
    schedule_xlsx_path: str = "./data/schedule.xlsx"
    schedule_auto_import_enabled: bool = False
    default_timezone: str = "Europe/Moscow"

    telegram_enabled: bool = False
    notifications_auto_send_enabled: bool = False
    pilot_auto_send_allowed: bool = False
    auto_send_poll_interval_seconds: int = 30
    auto_send_mass_limit: int = 10
    auto_send_allow_mass: bool = False
    auto_send_max_attempts: int = 3
    auto_send_retry_delay_seconds: int = 60
    auto_send_max_late_minutes: int = 60
    processing_stale_after_seconds: int = 300
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_bot_username: str = ""
    telegram_miniapp_short_name: str = ""
    webapp_url: str = ""
    admin_telegram_ids: str = ""
    telegram_webapp_auth_max_age_seconds: int = 86400

    outlook_enabled: bool = False
    outlook_tenant_id: str = ""
    outlook_client_id: str = ""
    outlook_client_secret: str = ""
    outlook_user_id: str = ""
    outlook_search_days_window: int = 1

    @property
    def miniapp_enabled(self) -> bool:
        return self.deployment_mode == "full"

    @property
    def frontend_required(self) -> bool:
        return self.miniapp_enabled

    @property
    def public_webapp_enabled(self) -> bool:
        value = self.webapp_url.strip().lower()
        if not self.miniapp_enabled or not value.startswith("https://"):
            return False
        return not value.startswith(("https://127.0.0.1", "https://localhost"))


@lru_cache
def get_settings() -> Settings:
    return Settings()
