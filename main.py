import asyncio
import logging
import os
import asyncpg
import pytz
from datetime import datetime
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# --- КОНФИГУРАЦИЯ ---
# Railway сам подставит сюда пароль от базы
DATABASE_URL = os.getenv("DATABASE_URL") 
BOT_TOKEN = "8514223980:AAE1FBB766X8H3MiG-YYFWjqgy3-k9f2xv0"
ADMIN_ID = 565936264

# Список пользователей, которые ДОЛЖНЫ быть в базе всегда
PRELOAD_USERS = [565936264, 8530310460, 907912564]

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler()
pool = None  # Тут будет соединение с базой

# --- РАБОТА С БАЗОЙ ДАННЫХ (PostgreSQL) ---

async def init_db():
    """Создает таблицы и добавляет важных юзеров"""
    global pool
    # Подключаемся к базе
    pool = await asyncpg.create_pool(DATABASE_URL)
    
    async with pool.acquire() as conn:
        # 1. Таблица пользователей
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                joined_at TIMESTAMP DEFAULT NOW()
            );
        """)
        # 2. Таблица отчетов (история)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS history (
                id SERIAL PRIMARY KEY,
                user_id BIGINT,
                date DATE DEFAULT CURRENT_DATE,
                UNIQUE(user_id, date)
            );
        """)
        
        # 3. Автоматически добавляем твоих друзей, если их нет
        for uid in PRELOAD_USERS:
            await conn.execute("""
                INSERT INTO users (user_id) VALUES ($1) 
                ON CONFLICT (user_id) DO NOTHING
            """, uid)
        
        logging.info("✅ База данных подключена и обновлена!")

async def add_user(user_id):
    await pool.execute("INSERT INTO users (user_id) VALUES ($1) ON CONFLICT DO NOTHING", user_id)

async def get_all_users():
    rows = await pool.fetch("SELECT user_id FROM users")
    return [row['user_id'] for row in rows]

async def mark_completed(user_id):
    """Записываем, что юзер выпил таблетку сегодня"""
    await pool.execute("""
        INSERT INTO history (user_id, date) VALUES ($1, CURRENT_DATE)
        ON CONFLICT (user_id, date) DO NOTHING
    """, user_id)

async def check_completed(user_id):
    """Проверяем, пил ли сегодня"""
    row = await pool.fetchrow("""
        SELECT 1 FROM history WHERE user_id = $1 AND date = CURRENT_DATE
    """, user_id)
    return row is not None

# --- ХЕНДЛЕРЫ ---

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    # Добавляем пользователя в базу
    await add_user(message.from_user.id)
    
    # Отправляем сообщение с сердечками
    await message.answer("Ты в системе! ❤️❤️❤️")

@dp.message(F.photo)
async def handle_photo(message: types.Message):
    user_id = message.from_user.id
    day = datetime.now().day
    
    if not (1 <= day <= 20):
        await message.answer("Сегодня не отчетный день. Отдыхай!")
        return

    if await check_completed(user_id):
        await message.answer("Ты уже отчитался сегодня! 💊")
        return

    await message.answer("📸 Фото принято! Ждем админа...")
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"approve_{user_id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_{user_id}")
        ]
    ])
    await bot.send_photo(
        chat_id=ADMIN_ID,
        photo=message.photo[-1].file_id,
        caption=f"Пользователь: {message.from_user.full_name}\nID: {user_id}\n\nПодтверди прием:",
        reply_markup=keyboard
    )

@dp.callback_query(F.data.startswith("approve_"))
async def admin_approve(callback: types.CallbackQuery):
    user_id = int(callback.data.split("_")[1])
    
    await mark_completed(user_id)
    
    try:
        await bot.send_message(user_id, "✅ Админ подтвердил! Напоминания отключены.")
    except: pass

    await callback.message.edit_caption(caption=f"{callback.message.caption}\n\n✅ ПОДТВЕРЖДЕНО")
    await callback.answer()

@dp.callback_query(F.data.startswith("reject_"))
async def admin_reject(callback: types.CallbackQuery):
    user_id = int(callback.data.split("_")[1])
    try:
        await bot.send_message(user_id, "❌ Отчет отклонен. Пришли нормальное фото!")
    except: pass
    await callback.message.edit_caption(caption=f"{callback.message.caption}\n\n❌ ОТКЛОНЕНО")
    await callback.answer()

# --- ПЛАНИРОВЩИК ---

async def morning_reminder():
    now = datetime.now()
    if not (1 <= now.day <= 20): return

    users = await get_all_users()
    for user_id in users:
        try:
            await bot.send_message(user_id, "💊 Доброе утро! Пришли фото упаковки.")
        except: pass

async def nagging_check():
    now = datetime.now()
    if not (1 <= now.day <= 20): return
    
    # Внимание! Серверное время.
    # Если ты хочешь напоминать с 22:00 до 01:00 по ТВОЕМУ времени
    # (а сервер отстает на 2 часа), то пишем с 20:00 до 23:00
    if not (20 <= now.hour < 23): return

    users = await get_all_users()
    for user_id in users:
        if not await check_completed(user_id):
            try:
                await bot.send_message(user_id, "⏰ Выпей таблетку и пришли фото!")
            except: pass

# --- ЗАПУСК ---
async def main():
    await init_db() # Подключаемся к БД
    
    # Часовой пояс не нужен, если мы просто сдвигаем часы вручную (hour=20 -> 22:00)
    scheduler.add_job(morning_reminder, "cron", hour=20, minute=0)
    scheduler.add_job(nagging_check, "interval", minutes=10)
    
    scheduler.start()
    
    await bot.delete_webhook(drop_pending_updates=True)
    
    # --- ДОБАВЛЕННАЯ СТРОЧКА ---
    await bot.send_message(ADMIN_ID, "👨‍💻 Бот успешно обновлен и запущен! База данных в норме.")
    # ---------------------------

    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
