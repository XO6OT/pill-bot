import unittest
from datetime import datetime, timedelta

import pytz

from reminder_parser import ReminderError, looks_like_reminder, parse_reminder, with_reminder_text

TZ = pytz.timezone("Europe/Kaliningrad")
NOW = TZ.localize(datetime(2026, 11, 5, 10, 0, 15))


def at(day, hour, minute=0):
    return TZ.localize(datetime(2026, 11, day, hour, minute))


class ReminderParserTests(unittest.TestCase):
    def check(self, phrase, when, text, daily=False):
        parsed = parse_reminder(phrase, NOW)
        self.assertEqual((parsed.remind_at, parsed.text, parsed.daily), (when, text, daily), phrase)

    def test_time_first_or_after_trigger(self):
        self.check("в 12:30 напомни мне о звонке маме", at(5, 12, 30), "о звонке маме")
        self.check("Напомни мне в 12:30 о звонке маме", at(5, 12, 30), "о звонке маме")
        self.check("напомни мне о звонке маме в 12:30", at(5, 12, 30), "о звонке маме")
        self.check("напомни в 12.30 купить хлеб", at(5, 12, 30), "Купить хлеб")
        self.check("Напомни мне в 18:00, что надо забрать посылку", at(5, 18), "Забрать посылку")
        self.check("напомните пожалуйста в 14:05 про врача", at(5, 14, 5), "про врача")

    def test_hour_only_and_part_of_day(self):
        self.check("напомни в 9 вечера выпить чай", at(5, 21), "Выпить чай")
        self.check("в 7 утра напомни про зарядку", at(6, 7), "про зарядку")
        self.check("напомни в 15 часов позвонить", at(5, 15), "Позвонить")
        self.check("напомни в 8:30 вечера погулять", at(5, 20, 30), "Погулять")

    def test_past_time_moves_to_tomorrow(self):
        self.check("напомни в 9:00 о витаминах", at(6, 9), "о витаминах")

    def test_day_words(self):
        self.check("завтра в 9 напомни про врача", at(6, 9), "про врача")
        self.check("напомни послезавтра в 10:00 оплатить интернет", at(7, 10), "Оплатить интернет")
        with self.assertRaises(ReminderError):
            parse_reminder("сегодня в 9:00 напомни про врача", NOW)

    def test_relative(self):
        start = NOW.replace(second=0)
        self.check("через 20 минут напомни выпить воды", start + timedelta(minutes=20), "Выпить воды")
        self.check("напомни через 2 часа снять пирог", start + timedelta(hours=2), "Снять пирог")
        self.check("напомни через час позвонить", start + timedelta(hours=1), "Позвонить")
        self.check("через полчаса напомни выключить духовку", start + timedelta(minutes=30), "Выключить духовку")
        self.check("через 1 час 15 минут напомни выйти", start + timedelta(minutes=75), "Выйти")

    def test_daily(self):
        self.check("каждый день в 8:00 напомни про витамины", at(6, 8), "про витамины", daily=True)
        self.check("напоминай ежедневно в 21:30 почистить зубы", at(5, 21, 30), "Почистить зубы", daily=True)

    def test_errors_are_explained(self):
        for phrase in ["напомни купить хлеб", "напомни мне в 12:30", "каждый день через 5 минут напомни пить"]:
            with self.assertRaises(ReminderError, msg=phrase):
                parse_reminder(phrase, NOW)

    def test_trigger_detection(self):
        self.assertTrue(looks_like_reminder("В 12:30 НАПОМНИ мне"))
        self.assertFalse(looks_like_reminder("люблю тебя"))
        self.assertFalse(looks_like_reminder(""))

    def test_reminder_wording(self):
        self.assertEqual(with_reminder_text("⏰ Напоминаю", "о звонке маме"), "⏰ Напоминаю о звонке маме")
        self.assertEqual(with_reminder_text("⏰ Напоминаю", "Купить хлеб"), "⏰ Напоминаю: Купить хлеб")


if __name__ == "__main__":
    unittest.main()
