import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from app.api import routes
from app.bot import runner as bot_runner
from app.services.outlook_graph import outlook_integration_configured


class FakeMessage:
    def __init__(self):
        self.answers = []

    async def answer(self, text, **kwargs):
        self.answers.append((text, kwargs))


class OutlookDisabledTestCase(unittest.TestCase):
    @staticmethod
    def incomplete_settings():
        return SimpleNamespace(
            outlook_enabled=True,
            outlook_tenant_id="",
            outlook_client_id="configured-client",
            outlook_client_secret="configured-secret",
            outlook_user_id="calendar-owner@example.com",
        )

    def test_capabilities_exposes_only_safe_outlook_flag(self):
        with patch.object(routes, "outlook_integration_configured", return_value=False):
            payload = routes.api_capabilities(SimpleNamespace())

        self.assertEqual(payload, {"features": {"outlook": False}})

    def test_api_sync_stops_before_notification_lookup_or_graph(self):
        with patch.object(routes, "outlook_integration_configured", return_value=False), patch.object(
            routes, "get_notification", side_effect=AssertionError("database lookup must not happen")
        ), patch.object(routes, "sync_notification_outlook_link", new=AsyncMock()) as sync_link:
            with self.assertRaises(HTTPException) as raised:
                asyncio.run(routes.api_outlook_sync(10, None, None))

        self.assertEqual(raised.exception.status_code, 400)
        self.assertEqual(raised.exception.detail, "Outlook-интеграция отключена или не настроена")
        sync_link.assert_not_awaited()

    def test_help_hides_both_outlook_commands_when_unavailable(self):
        message = FakeMessage()
        with patch.object(bot_runner, "outlook_integration_configured", return_value=False):
            asyncio.run(bot_runner.on_help(message))

        text = message.answers[0][0]
        self.assertNotIn("/outlooktest", text)
        self.assertNotIn("/setoutlook", text)

    def test_direct_outlook_commands_are_unavailable_without_database_or_graph(self):
        unavailable_message = "Outlook-интеграция отключена или не настроена"
        with patch.object(bot_runner, "outlook_integration_configured", return_value=False), patch.object(
            bot_runner, "SessionLocal", side_effect=AssertionError("database must not be opened")
        ), patch.object(bot_runner, "sync_notification_outlook_link", new=AsyncMock()) as sync_link:
            outlooktest_message = FakeMessage()
            setoutlook_message = FakeMessage()
            asyncio.run(bot_runner.on_outlooktest(outlooktest_message))
            asyncio.run(bot_runner.on_setoutlook(setoutlook_message))

        self.assertEqual(outlooktest_message.answers[0][0], unavailable_message)
        self.assertEqual(setoutlook_message.answers[0][0], unavailable_message)
        sync_link.assert_not_awaited()

    def test_incomplete_configuration_is_unavailable_without_database_or_graph(self):
        unavailable_message = "Outlook-интеграция отключена или не настроена"
        with patch("app.services.outlook_graph.get_settings", return_value=self.incomplete_settings()):
            self.assertFalse(outlook_integration_configured())

            with patch.object(routes, "get_notification", side_effect=AssertionError("database lookup must not happen")), patch.object(
                routes, "sync_notification_outlook_link", new=AsyncMock()
            ) as sync_link:
                with self.assertRaises(HTTPException) as raised:
                    asyncio.run(routes.api_outlook_sync(10, None, None))

            self.assertEqual(raised.exception.status_code, 400)
            self.assertEqual(raised.exception.detail, unavailable_message)
            sync_link.assert_not_awaited()

            with patch.object(bot_runner, "SessionLocal", side_effect=AssertionError("database must not be opened")), patch.object(
                bot_runner, "sync_notification_outlook_link", new=AsyncMock()
            ) as sync_link:
                help_message = FakeMessage()
                outlooktest_message = FakeMessage()
                setoutlook_message = FakeMessage()
                asyncio.run(bot_runner.on_help(help_message))
                asyncio.run(bot_runner.on_outlooktest(outlooktest_message))
                asyncio.run(bot_runner.on_setoutlook(setoutlook_message))

            self.assertNotIn("/outlooktest", help_message.answers[0][0])
            self.assertNotIn("/setoutlook", help_message.answers[0][0])
            self.assertEqual(outlooktest_message.answers[0][0], unavailable_message)
            self.assertEqual(setoutlook_message.answers[0][0], unavailable_message)
            sync_link.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
