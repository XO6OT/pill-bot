import unittest
from datetime import datetime, timedelta

import pytz

from reminder_parser import (
    ReminderError, describe_when, looks_like_reminder, parse_reminder, with_reminder_text,
)

TZ = pytz.timezone("Europe/Kaliningrad")
NOW = TZ.localize(datetime(2026, 11, 5, 10, 0, 15))  # четверг, 10:00
START = NOW.replace(second=0)


def at(day, hour, minute=0, month=11, year=2026):
    return TZ.localize(datetime(year, month, day, hour, minute))


class ReminderParserTests(unittest.TestCase):
    def check(self, phrase, when, text, repeat=0):
        parsed = parse_reminder(phrase, NOW)
        self.assertEqual((parsed.remind_at, parsed.text, parsed.repeat_days), (when, text, repeat), phrase)

    def test_word_order(self):
        self.check("в 12:30 напомни мне о звонке маме", at(5, 12, 30), "о звонке маме")
        self.check("Напомни мне в 12:30 о звонке маме", at(5, 12, 30), "о звонке маме")
        self.check("напомни мне о звонке маме в 12:30", at(5, 12, 30), "о звонке маме")
        self.check("о звонке маме в 12:30 напомни", at(5, 12, 30), "о звонке маме")
        self.check("Напомни мне в 18:00, что надо забрать посылку", at(5, 18), "Забрать посылку")
        self.check("напомните пожалуйста в 14:05 про врача", at(5, 14, 5), "про врача")
        self.check("напомни мне в 15:00, чтобы я полила цветы", at(5, 15), "Полила цветы")
        self.check("напомни чтобы не забыть в 15:00 полить цветы", at(5, 15), "Полить цветы")

    def test_address_and_caps(self):
        self.check("Котик, напомни мне в 13:00 поесть", at(5, 13), "Поесть")
        self.check("НАПОМНИ В 6 ВЕЧЕРА ПРО СПОРТЗАЛ", at(5, 18), "про спортзал")
        self.check("напомни в 14:00 что у меня созвон", at(5, 14), "У меня созвон")

    def test_other_ways_to_ask(self):
        self.check("не дай мне забыть в 18:00 выключить духовку", at(5, 18), "Выключить духовку")
        self.check("поставь напоминание на 12:30 позвонить в банк", at(5, 12, 30), "Позвонить в банк")
        self.check("поставь мне будильник на 7 утра проснуться", at(6, 7), "Проснуться")
        self.check("напоминалка в 21:00 маска для лица", at(5, 21), "Маска для лица")

    def test_time_formats(self):
        self.check("напомни в 12.30 купить хлеб", at(5, 12, 30), "Купить хлеб")
        self.check("напомни в 12 30 купить хлеб", at(5, 12, 30), "Купить хлеб")
        self.check("напомни к 16:45 забрать заказ", at(5, 16, 45), "Забрать заказ")
        self.check("напомни около 17 позвонить", at(5, 17), "Позвонить")
        self.check("напомни в 15 часов позвонить", at(5, 15), "Позвонить")
        self.check("напомни в 9 вечера выпить чай", at(5, 21), "Выпить чай")
        self.check("в 7 утра напомни про зарядку", at(6, 7), "про зарядку")
        self.check("напомни в 8:30 вечера погулять", at(5, 20, 30), "Погулять")
        self.check("напомни в 2 дня про обед", at(5, 14), "про обед")
        self.check("напомни в 11 ночи лечь спать", at(5, 23), "Лечь спать")

    def test_time_in_words(self):
        self.check("напомни в девять вечера выпить чай", at(5, 21), "Выпить чай")
        self.check("напомни в девять тридцать позвонить", at(6, 9, 30), "Позвонить")
        self.check("напомни в двадцать один сорок пять про сериал", at(5, 21, 45), "про сериал")
        self.check("напомни в час дня пообедать", at(5, 13), "Пообедать")
        self.check("напомни в полдень про обед", at(5, 12), "про обед")
        self.check("напомни в полночь поздравить Машу", at(6, 0), "Поздравить Машу")
        self.check("напомни в половине девятого вечера про ужин", at(5, 20, 30), "про ужин")
        self.check("напомни в полседьмого выйти из дома", at(6, 6, 30), "Выйти из дома")
        self.check("напомни без пятнадцати девять вечера про звонок", at(5, 20, 45), "про звонок")
        self.check("напомни без четверти семь позвонить", at(6, 6, 45), "Позвонить")

    def test_part_of_day_only(self):
        self.check("вечером напомни позвонить бабушке", at(5, 19), "Позвонить бабушке")
        self.check("завтра утром напомни про врача", at(6, 9), "про врача")
        self.check("сегодня вечером в 8 напомни про кино", at(5, 20), "про кино")
        self.check("напомни днём купить подарок", at(5, 13), "Купить подарок")

    def test_days_and_dates(self):
        self.check("завтра в 9 напомни про врача", at(6, 9), "про врача")
        self.check("напомни послезавтра в 10:00 оплатить интернет", at(7, 10), "Оплатить интернет")
        self.check("завтра напомни купить молоко", at(6, 9), "Купить молоко")
        self.check("в пятницу в 10 напомни оплатить интернет", at(6, 10), "Оплатить интернет")
        self.check("напомни в четверг в 9:00 про отчёт", at(12, 9), "про отчёт")  # сегодня 9:00 уже было
        self.check("во вторник напомни про маникюр", at(10, 9), "про маникюр")
        self.check("напомни 20 ноября в 12:00 про день рождения", at(20, 12), "про день рождения")
        self.check("напомни 3 января про визу", at(3, 9, month=1, year=2027), "про визу")
        self.check("напомни 12.11 в 18:30 забрать платье", at(12, 18, 30), "Забрать платье")
        self.check("напомни 01.12.2026 оплатить квартиру", at(1, 9, month=12), "Оплатить квартиру")
        with self.assertRaises(ReminderError):
            parse_reminder("сегодня в 9:00 напомни про врача", NOW)
        with self.assertRaises(ReminderError):
            parse_reminder("напомни 31 февраля про праздник", NOW)

    def test_relative(self):
        self.check("через 20 минут напомни выпить воды", START + timedelta(minutes=20), "Выпить воды")
        self.check("через двадцать минут напомни выпить воды", START + timedelta(minutes=20), "Выпить воды")
        self.check("напомни через 5 мин проверить духовку", START + timedelta(minutes=5), "Проверить духовку")
        self.check("напомни через 2 часа снять пирог", START + timedelta(hours=2), "Снять пирог")
        self.check("напомни через час позвонить", START + timedelta(hours=1), "Позвонить")
        self.check("напомни через минуту встать", START + timedelta(minutes=1), "Встать")
        self.check("через пару минут напомни про чайник", START + timedelta(minutes=2), "про чайник")
        self.check("через полчаса напомни выключить духовку", START + timedelta(minutes=30), "Выключить духовку")
        self.check("через полтора часа напомни забрать стирку", START + timedelta(minutes=90), "Забрать стирку")
        self.check("через 1 час 15 минут напомни выйти", START + timedelta(minutes=75), "Выйти")
        self.check("через час двадцать минут напомни выйти", START + timedelta(minutes=80), "Выйти")
        self.check("через 3 дня напомни про анализы", START + timedelta(days=3), "про анализы")
        self.check("через 2 дня в 10:00 напомни про анализы", at(7, 10), "про анализы")
        self.check("через неделю напомни продлить подписку", START + timedelta(days=7), "Продлить подписку")

    def test_repeats(self):
        self.check("каждый день в 8:00 напомни про витамины", at(6, 8), "про витамины", repeat=1)
        self.check("напоминай ежедневно в 21:30 почистить зубы", at(5, 21, 30), "Почистить зубы", repeat=1)
        self.check("каждое утро напоминай пить воду", at(6, 9), "Пить воду", repeat=1)
        self.check("каждый вечер в 22:30 напоминай снять линзы", at(5, 22, 30), "Снять линзы", repeat=1)
        self.check("каждый понедельник в 10 напомни про отчёт", at(9, 10), "про отчёт", repeat=7)
        self.check("каждую пятницу в 18:00 напоминай про уборку", at(6, 18), "про уборку", repeat=7)
        self.check("по средам в 19:00 напоминай про йогу", at(11, 19), "про йогу", repeat=7)

    def test_errors_are_explained(self):
        for phrase in [
            "напомни купить хлеб", "напомни мне в 12:30", "каждый день через 5 минут напомни пить",
            "напомни через 0 минут встать",
        ]:
            with self.assertRaises(ReminderError, msg=phrase):
                parse_reminder(phrase, NOW)

    def test_trigger_detection(self):
        self.assertTrue(looks_like_reminder("В 12:30 НАПОМНИ мне"))
        self.assertTrue(looks_like_reminder("не дай мне забыть про врача"))
        self.assertTrue(looks_like_reminder("поставь напоминание на 9"))
        self.assertFalse(looks_like_reminder("люблю тебя"))
        self.assertFalse(looks_like_reminder("в 9 буду дома"))
        self.assertFalse(looks_like_reminder(""))

    def test_wording(self):
        self.assertEqual(with_reminder_text("⏰ Напоминаю", "о звонке маме"), "⏰ Напоминаю о звонке маме")
        self.assertEqual(with_reminder_text("⏰ Напоминаю", "Купить хлеб"), "⏰ Напоминаю: Купить хлеб")
        self.assertEqual(describe_when(at(5, 12, 30), 0, NOW), "сегодня в 12:30")
        self.assertEqual(describe_when(at(6, 9), 0, NOW), "завтра в 09:00")
        self.assertEqual(describe_when(at(7, 10), 0, NOW), "07.11 (сб) в 10:00")
        self.assertEqual(describe_when(at(6, 8), 1, NOW), "каждый день в 08:00")
        self.assertEqual(describe_when(at(11, 19), 7, NOW), "каждую среду в 19:00")


if __name__ == "__main__":
    unittest.main()
