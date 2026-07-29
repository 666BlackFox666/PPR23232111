import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

from app.api import auth
from app.config import Settings


class AuthAndCorsSecurityTestCase(unittest.TestCase):
    @staticmethod
    def settings(app_env: str, dev_commands_enabled: bool, cors_allowed_origins: str = "") -> Settings:
        return Settings(
            _env_file=None,
            app_env=app_env,
            dev_commands_enabled=dev_commands_enabled,
            cors_allowed_origins=cors_allowed_origins,
        )

    def get_user_with_dev_headers(self, settings: Settings):
        expected_user = SimpleNamespace(telegram_id="100", is_active=True)
        with patch.object(auth, "get_settings", return_value=settings), patch.object(
            auth, "get_or_sync_user", return_value=expected_user
        ) as sync_user:
            user = auth.get_current_user(
                x_telegram_init_data="",
                x_dev_telegram_id="100",
                x_dev_username="admin",
                x_dev_full_name="Admin User",
                db=object(),
            )
        return user, expected_user, sync_user

    def test_development_with_enabled_dev_commands_accepts_dev_headers(self):
        user, expected_user, sync_user = self.get_user_with_dev_headers(self.settings("development", True))

        self.assertIs(user, expected_user)
        sync_user.assert_called_once_with(unittest.mock.ANY, "100", "admin", "Admin User")

    def test_development_with_disabled_dev_commands_rejects_dev_headers(self):
        with self.assertRaises(HTTPException) as raised:
            self.get_user_with_dev_headers(self.settings("development", False))

        self.assertEqual(raised.exception.status_code, 401)

    def test_production_with_enabled_dev_commands_rejects_dev_headers(self):
        with self.assertRaises(HTTPException) as raised:
            self.get_user_with_dev_headers(self.settings("production", True))

        self.assertEqual(raised.exception.status_code, 401)

    def test_production_with_disabled_dev_commands_rejects_dev_headers(self):
        with self.assertRaises(HTTPException) as raised:
            self.get_user_with_dev_headers(self.settings("production", False))

        self.assertEqual(raised.exception.status_code, 401)

    def test_telegram_init_data_authentication_is_unchanged(self):
        settings = self.settings("production", False)
        expected_user = SimpleNamespace(telegram_id="200", is_active=True)
        init_data = {"user": {"id": 200, "username": "telegram", "first_name": "Telegram"}}
        with patch.object(auth, "get_settings", return_value=settings), patch.object(
            auth, "validate_telegram_init_data", return_value=init_data
        ), patch.object(auth, "get_or_sync_user", return_value=expected_user) as sync_user:
            user = auth.get_current_user(x_telegram_init_data="signed-init-data", db=object())

        self.assertIs(user, expected_user)
        sync_user.assert_called_once_with(unittest.mock.ANY, "200", "telegram", "Telegram")

    def test_cors_never_uses_wildcard(self):
        settings = self.settings("production", False, "*,https://miniapp.example.com")

        self.assertEqual(settings.allowed_cors_origins, ["https://miniapp.example.com"])

    def test_empty_production_cors_does_not_open_access(self):
        settings = self.settings("production", False)

        self.assertEqual(settings.allowed_cors_origins, [])

    def test_development_localhost_cors_requires_explicit_configuration(self):
        disabled = self.settings("development", False)
        enabled = self.settings("development", False, "http://127.0.0.1:5173,http://localhost:5173")

        self.assertEqual(disabled.allowed_cors_origins, [])
        self.assertEqual(enabled.allowed_cors_origins, ["http://127.0.0.1:5173", "http://localhost:5173"])


if __name__ == "__main__":
    unittest.main()
