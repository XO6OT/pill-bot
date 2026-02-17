import asyncio
import logging
import json
import os
from datetime import datetime
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# --- ТВОИ ДАННЫЕ ---
BOT_TOKEN = "8514223980:AAE1FBB766X8H3MiG-YYFWjqgy3-k9f2xv0"
ADMIN_ID = 565936264  # Твой ID
DB_FILE = "users_db.json"

# Настройка логирования
logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler()

# --- ФУНКЦИИ БАЗЫ ДАННЫХ ---
def load_db():
    if not os.path.exists(DB_FILE):
        return {"users": [], "history": {}}
    with open(DB_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_db(data):
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)

# --- ХЕНДЛЕРЫ (ОБРАБОТКА СООБЩЕНИЙ) ---

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    data = load_db()
    user_id = message.from_user.id
    
    if user_id not in data["users"]:
        data["users"].append(user_id)
        save_db(data)
        await message.answer("привет сонечка любимая ✅ Ты в системе!")
        # Уведомление админу о новом пользователе
        await bot.send_message(ADMIN_ID, f"Новый пользователь: {message.from_user.full_name} (ID: {user_id})")
    else:
        await message.answer("Ты уже добавлен в базу напоминаний.")

@dp.message(Command("time"))
async def cmd_time(message: types.Message):
    server_time = datetime.now().strftime("%H:%M:%S")
    await message.answer(f"🕒 Мое время на сервере: {server_time}")

@dp.message(F.photo)
async def handle_photo(message: types.Message):
    user_id = message.from_user.id
    day = datetime.now().day
    
    # Проверяем дату (1-20 число)
    if not (1 <= day <= 20):
        await message.answer("Сегодня не отчетный день (не 1-20 число). Отдыхай!")
        return

    # Проверяем, не сдал ли он уже сегодня отчет
    date_key = datetime.now().strftime("%Y-%m-%d")
    data = load_db()
    if user_id in data["history"].get(date_key, []):
        await message.answer("Ты уже отчитался сегодня! Молодец.")
        return

    await message.answer("📸 Фото принято! Отправляю админу на проверку...")
    
    # Клавиатура для админа
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"approve_{user_id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_{user_id}")
        ]
    ])
    
    # Пересылка фото админу
    await bot.send_photo(
        chat_id=ADMIN_ID,
        photo=message.photo[-1].file_id,
        caption=f"Пользователь: {message.from_user.full_name}\nID: {user_id}\n\nПодтверди прием таблетки:",
        reply_markup=keyboard
    )

# --- ОБРАБОТКА КНОПОК АДМИНА ---

@dp.callback_query(F.data.startswith("approve_"))
async def admin_approve(callback: types.CallbackQuery):
    user_id = int(callback.data.split("_")[1])
    date_key = datetime.now().strftime("%Y-%m-%d")
    
    data = load_db()
    if date_key not in data["history"]:
        data["history"][date_key] = []
    
    # Записываем, что юзер выпил таблетку
    if user_id not in data["history"][date_key]:
        data["history"][date_key].append(user_id)
        save_db(data)
    
    # Уведомляем юзера
    try:
        await bot.send_message(user_id, "✅ Админ подтвердил! Ты молодец, напоминания на сегодня отключены.")
    except:
        pass # Если юзер заблокировал бота

    # Меняем сообщение у админа
    await callback.message.edit_caption(caption=f"{callback.message.caption}\n\n✅ ВЫПИТО И ПОДТВЕРЖДЕНО")
    await callback.answer()

@dp.callback_query(F.data.startswith("reject_"))
async def admin_reject(callback: types.CallbackQuery):
    user_id = int(callback.data.split("_")[1])
    
    try:
        await bot.send_message(user_id, "❌ Админ отклонил отчет. Фото нечеткое или не то. Пришли новое!")
    except:
        pass

    await callback.message.edit_caption(caption=f"{callback.message.caption}\n\n❌ ОТКЛОНЕНО")
    await callback.answer()

# --- ПЛАНИРОВЩИК (НАПОМИНАНИЯ) ---

async def morning_reminder():
    """Отправляет первое напоминание утром"""
    now = datetime.now()
    if not (1 <= now.day <= 20): return # Только 1-20 число

    data = load_db()
    for user_id in data["users"]:
        try:
            await bot.send_message(user_id, "☀️ Доброе утро! Пора выпить таблетку. Пришли фото упаковки!")
        except Exception as e:
            logging.error(f"Ошибка отправки {user_id}: {e}")

async def nagging_check():
    """Проверяет каждые 10 минут, кто не выпил"""
    now = datetime.now()
    if not (1 <= now.day <= 20): return # Только 1-20 число
    
    # Не кошмарим людей ночью (работаем с 9:00 до 23:00 по твоему времени)
    # Сервер отстает на 2 часа, поэтому здесь 7-21
    if not (7 <= now.hour < 21): return

    date_key = now.strftime("%Y-%m-%d")
    data = load_db()
    completed_users = data["history"].get(date_key, [])

    for user_id in data["users"]:
        # Если юзера нет в списке выполнивших сегодня
        if user_id not in completed_users:
            try:
                await bot.send_message(user_id, "⏰ НАПОМИНАНИЕ: Таблетка сама себя не выпьет! Жду фото.")
            except Exception as e:
                logging.error(f"Не удалось напомнить {user_id}: {e}")

# --- ЗАПУСК ---
async def main():
    # Настраиваем расписание
    # 1. Основное напоминание в 22:00 по твоему времени
    # Сервер отстает на 2 часа, поэтому ставим 20:00
    scheduler.add_job(morning_reminder, "cron", hour=20, minute=0)
    
    # 2. Проверка должников каждые 10 минут
    scheduler.add_job(nagging_check, "interval", minutes=10)
    
    scheduler.start()
    
    # Пропускаем старые обновления и запускаем
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
