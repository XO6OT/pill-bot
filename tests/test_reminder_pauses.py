import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import main


class ReminderPauseTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.original_pool = main.pool
        main.pool = AsyncMock()

    def tearDown(self):
        main.pool = self.original_pool

    async def test_pause_is_saved_for_seven_days(self):
        paused_until = datetime(2026, 8, 31, 10, 0, tzinfo=timezone.utc)
        main.pool.fetchrow.return_value = {"paused_until": paused_until}

        result = await main.pause_reminders(123)

        self.assertEqual(result, paused_until)
        pause_query = main.pool.fetchrow.await_args.args[0]
        self.assertIn("INTERVAL '7 days'", pause_query)
        self.assertEqual(main.pool.fetchrow.await_args.args[1], 123)

    async def test_resume_reports_whether_pause_was_removed(self):
        main.pool.execute.return_value = "DELETE 1"
        self.assertTrue(await main.resume_reminders(123))

        main.pool.execute.return_value = "DELETE 0"
        self.assertFalse(await main.resume_reminders(123))

    async def test_paused_users_are_filtered_from_reminders(self):
        main.pool.fetch.return_value = [{"user_id": 1}, {"user_id": 2}]

        self.assertEqual(await main.get_users_for_reminders(), [1, 2])
        reminder_query = main.pool.fetch.await_args.args[0]
        self.assertIn("paused_until > NOW()", reminder_query)

    def test_pause_time_is_shown_in_local_timezone(self):
        paused_until = datetime(2026, 8, 31, 10, 0, tzinfo=timezone.utc)

        self.assertEqual(
            main.format_pause_until(paused_until),
            "31.08.2026 в 12:00"
        )

    def test_reminder_has_pause_and_resume_actions(self):
        pause_button = main.reminder_keyboard().inline_keyboard[0][0]
        resume_button = main.resume_keyboard().inline_keyboard[0][0]

        self.assertEqual(pause_button.callback_data, "pause_request_7")
        self.assertEqual(resume_button.callback_data, "resume_reminders")

    def test_version_message_contains_release_and_current_date(self):
        now = datetime(2026, 8, 24, 15, 30, tzinfo=timezone.utc)

        message = main.version_message(now)

        self.assertIn(main.APP_VERSION, message)
        self.assertIn("24.08.2026 в 15:30", message)
        self.assertIn(main.UPDATE_DESCRIPTION, message)


if __name__ == "__main__":
    unittest.main()
