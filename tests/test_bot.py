import unittest
from datetime import date, datetime, time
from io import BytesIO
from unittest.mock import AsyncMock, MagicMock, patch

import main

TZ = main.LOCAL_TIMEZONE


def local(hour, minute=0, day=12):
    return TZ.localize(datetime(2026, 11, day, hour, minute))


class BotTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.original_pool = main.pool
        main.pool = AsyncMock()
        send_patch = patch.object(main.bot, "send_message", new_callable=AsyncMock)
        self.send_message = send_patch.start()
        self.addCleanup(send_patch.stop)

    def tearDown(self):
        main.pool = self.original_pool

    def test_parse_time_and_text(self):
        self.assertEqual(main.parse_time_and_text("11:30 📚 Пора изучить тему!"), (time(11, 30), "📚 Пора изучить тему!"))
        self.assertEqual(main.parse_time_and_text("в 9.05 Зарядка"), (time(9, 5), "Зарядка"))
        self.assertEqual(main.parse_time_and_text("12:00"), (time(12, 0), ""))
        self.assertEqual(main.parse_time_and_text("8:00 Утро\nвторая строка"), (time(8, 0), "Утро\nвторая строка"))
        self.assertIsNone(main.parse_time_and_text("Пора изучить тему"))
        self.assertIsNone(main.parse_time_and_text("25:00 текст"))

    def test_notification_is_due_once_a_day_within_an_hour(self):
        notification = {"notify_time": time(11, 30), "last_sent_date": None}
        self.assertFalse(main.notification_is_due(notification, local(11, 29)))
        self.assertTrue(main.notification_is_due(notification, local(11, 30)))
        self.assertTrue(main.notification_is_due(notification, local(12, 15)))  # бот перезапускался
        self.assertFalse(main.notification_is_due(notification, local(12, 31)))  # слишком поздно

        sent_today = {"notify_time": time(11, 30), "last_sent_date": date(2026, 11, 12)}
        self.assertFalse(main.notification_is_due(sent_today, local(11, 30)))
        sent_yesterday = {"notify_time": time(11, 30), "last_sent_date": date(2026, 11, 11)}
        self.assertTrue(main.notification_is_due(sent_yesterday, local(11, 30)))

    def test_new_notification_does_not_fire_for_past_time_today(self):
        self.assertEqual(main.initial_last_sent(time(11, 30), local(15, 0)), date(2026, 11, 12))
        self.assertIsNone(main.initial_last_sent(time(18, 0), local(15, 0)))

    async def test_due_notification_goes_only_to_recipients_and_is_marked(self):
        main.pool.fetch.side_effect = [
            [{"id": 1, "text": "📚 Пора изучить тему!", "notify_time": time(11, 30), "last_sent_date": None},
             {"id": 2, "text": "Вечернее", "notify_time": time(20, 0), "last_sent_date": None}],
            [{"user_id": 7}],
        ]

        with patch.object(main, "datetime") as fake_datetime:
            fake_datetime.now.return_value = local(11, 30)
            await main.send_admin_notifications()

        self.send_message.assert_awaited_once_with(7, "📚 Пора изучить тему!")
        mark_query, notification_id, day = main.pool.execute.await_args.args
        self.assertIn("last_sent_date = $2", mark_query)
        self.assertEqual((notification_id, day), (1, date(2026, 11, 12)))

    async def test_toggle_recipient(self):
        main.pool.execute.return_value = "DELETE 1"
        self.assertFalse(await main.toggle_recipient(1, 7))

        main.pool.execute.side_effect = ["DELETE 0", "INSERT 0 1"]
        self.assertTrue(await main.toggle_recipient(1, 7))

    def test_user_card_buttons(self):
        user = {"user_id": 5, "full_name": "Соня", "username": None}
        notifications = [{"id": 1, "text": "📚 Пора изучить тему!", "notify_time": time(11, 30)},
                         {"id": 2, "text": "Вода", "notify_time": time(15, 0)}]

        rows = main.user_card_keyboard(user, notifications, {1}).inline_keyboard
        buttons = [row[0] for row in rows]

        self.assertEqual([b.callback_data for b in buttons],
                         ["adm_photo_5", "usr_n_1_5", "usr_n_2_5", "adm_del_5", "adm_users"])
        self.assertTrue(buttons[1].text.startswith("✅ 11:30"))
        self.assertTrue(buttons[2].text.startswith("▫️ 15:00"))

    def test_notification_card(self):
        notification = {"id": 3, "text": "Вода", "notify_time": time(15, 0)}
        users = [{"user_id": 5, "full_name": "Соня", "username": None},
                 {"user_id": 6, "full_name": None, "username": None}]

        keyboard = main.notification_card_keyboard(notification, users, {5}).inline_keyboard
        text = main.notification_card_text(notification, users, {5})

        self.assertEqual(keyboard[0][0].text, "✅ Соня")
        self.assertEqual(keyboard[0][0].callback_data, "ntf_u_3_5")
        self.assertEqual(keyboard[1][0].text, "▫️ ID 6")
        self.assertEqual([b.callback_data for b in keyboard[2]], ["ntf_time_3", "ntf_text_3"])
        self.assertEqual(keyboard[3][0].callback_data, "ntf_del_3")
        self.assertIn("Каждый день в 15:00", text)
        self.assertIn("Кому: Соня", text)
        self.assertIn("пока никому", main.notification_card_text(notification, users, set()))

    async def test_own_reminders_are_sent_and_cleaned_up(self):
        main.pool.fetch.return_value = [
            {"id": 1, "user_id": 7, "text": "Позвонить маме", "repeat_days": 0},
            {"id": 2, "user_id": 7, "text": "про витамины", "repeat_days": 7},
        ]

        await main.send_own_reminders()

        sent = [call.args for call in self.send_message.await_args_list]
        self.assertEqual(sent, [(7, "⏰ Напоминаю: Позвонить маме"), (7, "⏰ Напоминаю про витамины")])
        queries = [call.args for call in main.pool.execute.await_args_list]
        self.assertIn("DELETE FROM user_reminders", queries[0][0])
        self.assertIn("INTERVAL '1 day'", queries[1][0])

    async def test_own_reminder_limit(self):
        main.pool.fetchval.return_value = main.MAX_OWN_REMINDERS
        self.assertIsNone(await main.add_own_reminder(7, object()))

    async def test_delete_user_removes_history_and_user(self):
        main.pool.execute.side_effect = ["DELETE 3", "DELETE 1"]
        self.assertTrue(await main.delete_user(42))

    def test_version_message(self):
        message = main.version_message(local(15, 30))
        self.assertIn(main.APP_VERSION, message)
        self.assertIn("12.11.2026 в 15:30", message)


    def voice_message(self, duration=5):
        message = MagicMock()
        message.voice.duration = duration
        message.audio = None
        message.from_user.id = 7
        message.chat.id = 7
        message.answer = AsyncMock()
        return message

    async def run_voice(self, message, key="test-key", heard=None):
        with patch.object(main, "GROQ_API_KEY", key), \
                patch.object(main.bot, "send_chat_action", new_callable=AsyncMock), \
                patch.object(main.bot, "download", new=AsyncMock(return_value=BytesIO(b"ogg"))), \
                patch.object(main, "transcribe", new=AsyncMock(return_value=heard)):
            await main.handle_voice(message)
        return message.answer.await_args.args[0]

    async def test_voice_without_key_says_not_set(self):
        reply = await self.run_voice(self.voice_message(), key=None)
        self.assertIn("напоминание не поставлено", reply)
        self.assertIn("текстом", reply)

    async def test_voice_recognized_sets_reminder(self):
        main.pool.fetchval.side_effect = [0, 42]  # напоминаний у неё ещё нет, новое получило id 42
        message = self.voice_message()

        reply = await self.run_voice(message, heard="Напомни мне через 5 минут достать бельё")

        self.assertIn("🎤 Распознано: «Напомни мне через 5 минут достать бельё»", reply)
        self.assertIn("✅ Напоминание поставлено", reply)
        self.assertIn("Достать бельё", reply)
        button = message.answer.await_args.kwargs["reply_markup"].inline_keyboard[0][0]
        self.assertEqual(button.callback_data, "rem_del_42")

    async def test_voice_recognized_but_no_time(self):
        reply = await self.run_voice(self.voice_message(), heard="Напомни достать бельё")
        self.assertIn("🎤 Распознано", reply)
        self.assertIn("❌ Напоминание не поставлено", reply)

    async def test_voice_without_request_to_remind(self):
        reply = await self.run_voice(self.voice_message(), heard="Привет, как дела")
        self.assertIn("🎤 Распознано: «Привет, как дела»", reply)
        self.assertIn("не нашёл просьбы напомнить", reply)

    async def test_voice_not_recognized(self):
        reply = await self.run_voice(self.voice_message(), heard=None)
        self.assertIn("Не получилось разобрать голосовое", reply)

    async def test_voice_too_long(self):
        reply = await self.run_voice(self.voice_message(duration=300), heard="что угодно")
        self.assertIn("слишком длинное", reply)


if __name__ == "__main__":
    unittest.main()
