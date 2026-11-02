import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from aiogram import types

import main


class ReminderPauseTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.original_pool = main.pool
        main.pool = AsyncMock()
        send_patch = patch.object(main.bot, "send_message", new_callable=AsyncMock)
        self.send_message = send_patch.start()
        self.addCleanup(send_patch.stop)

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

    async def test_study_reminder_goes_only_to_selected_users(self):
        main.pool.fetch.return_value = [{"user_id": 7}]
        await main.study_reminder()

        study_query = main.pool.fetch.await_args.args[0]
        self.assertIn("WHERE study_reminder", study_query)
        self.send_message.assert_awaited_once_with(7, "📚 Пора изучить тему!")

    async def test_pill_reminder_has_no_good_morning(self):
        main.pool.fetch.return_value = [{"user_id": 7}]
        await main.morning_reminder()

        text = self.send_message.await_args.args[1]
        self.assertNotIn("Доброе утро", text)
        self.assertIn("Пришли фото упаковки", text)

    async def test_delete_user_removes_history_and_user(self):
        main.pool.execute.side_effect = ["DELETE 3", "DELETE 1"]

        self.assertTrue(await main.delete_user(42))

        queries = [call.args[0] for call in main.pool.execute.await_args_list]
        self.assertIn("DELETE FROM history", queries[0])
        self.assertIn("DELETE FROM users", queries[1])

    async def test_save_user_keeps_name_up_to_date(self):
        user = types.User(id=5, is_bot=False, first_name="Соня", last_name="К", username="sonya")

        await main.save_user(user)

        args = main.pool.execute.await_args.args
        self.assertIn("ON CONFLICT (user_id) DO UPDATE", args[0])
        self.assertEqual(args[1:], (5, "Соня К", "sonya"))

    def test_user_card_has_study_toggle_and_delete(self):
        user = {"user_id": 5, "full_name": "Соня", "username": None, "study_reminder": True}

        buttons = [row[0] for row in main.user_card_keyboard(user).inline_keyboard]

        self.assertEqual(
            [button.callback_data for button in buttons],
            ["adm_study_5", "adm_del_5", "adm_users"]
        )
        self.assertIn("вкл", buttons[0].text)

    def test_users_list_shows_names_and_study_mark(self):
        users = [
            {"user_id": 5, "full_name": "Соня", "username": "sonya", "study_reminder": True},
            {"user_id": 6, "full_name": None, "username": None, "study_reminder": False},
        ]

        buttons = [row[0] for row in main.users_list_keyboard(users).inline_keyboard]

        self.assertEqual(buttons[0].text, "📚 Соня (@sonya)")
        self.assertEqual(buttons[1].text, "ID 6")
        self.assertEqual(buttons[1].callback_data, "adm_user_6")


if __name__ == "__main__":
    unittest.main()
