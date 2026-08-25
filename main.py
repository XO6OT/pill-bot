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
APP_VERSION = "2026.08.25-snooze-undo"
UPDATE_DESCRIPTION = "добавлена отмена отсрочки напоминаний"

# Список пользователей, которые ДОЛЖНЫ быть в базе всегда
PRELOAD_USERS = [565936264, 8530310460, 907912564]

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler()
pool = None  # Тут будет соединение с базой
LOCAL_TIMEZONE = pytz.timezone("Europe/Kaliningrad")

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

        # 3. Таблица временных отсрочек напоминаний
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS reminder_pauses (
                user_id BIGINT PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
                paused_until TIMESTAMPTZ NOT NULL
            );
        """)
        
        # 4. Автоматически добавляем твоих друзей, если их нет
        for uid in PRELOAD_USERS:
            await conn.execute("""
                INSERT INTO users (user_id) VALUES ($1) 
                ON CONFLICT (user_id) DO NOTHING
            """, uid)
        
        logging.info("✅ База данных подключена и обновлена!")

async def add_user(user_id):
    await pool.execute("INSERT INTO users (user_id) VALUES ($1) ON CONFLICT DO NOTHING", user_id)

async def get_users_for_reminders():
    """Возвращает пользователей, у которых сейчас нет активной отсрочки."""
    rows = await pool.fetch("""
        SELECT users.user_id
        FROM users
        LEFT JOIN reminder_pauses
            ON reminder_pauses.user_id = users.user_id
            AND reminder_pauses.paused_until > NOW()
        WHERE reminder_pauses.user_id IS NULL
    """)
    return [row['user_id'] for row in rows]

async def pause_reminders(user_id):
    """Отключает напоминания на семь суток и возвращает дату их включения."""
    await add_user(user_id)
    row = await pool.fetchrow("""
        INSERT INTO reminder_pauses (user_id, paused_until)
        VALUES ($1, NOW() + INTERVAL '7 days')
        ON CONFLICT (user_id) DO UPDATE
        SET paused_until = EXCLUDED.paused_until
        RETURNING paused_until
    """, user_id)
    return row['paused_until']

async def get_pause_until(user_id):
    """Возвращает дату окончания активной отсрочки или None."""
    return await pool.fetchval("""
        SELECT paused_until
        FROM reminder_pauses
        WHERE user_id = $1 AND paused_until > NOW()
    """, user_id)

async def resume_reminders(user_id):
    """Снимает отсрочку. Возвращает True, если она была активна."""
    result = await pool.execute("""
        DELETE FROM reminder_pauses
        WHERE user_id = $1 AND paused_until > NOW()
    """, user_id)
    return result == "DELETE 1"

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

def format_pause_until(paused_until):
    """Показывает время возобновления в часовом поясе пользователя."""
    if paused_until.tzinfo is None:
        paused_until = pytz.utc.localize(paused_until)
    local_time = paused_until.astimezone(LOCAL_TIMEZONE)
    return local_time.strftime("%d.%m.%Y в %H:%M")

def version_message(now=None):
    """Сообщение, по которому видно, что новая версия действительно запущена."""
    now = now or datetime.now(LOCAL_TIMEZONE)
    return (
        "✅ Обновление бота запущено\n"
        f"Версия: {APP_VERSION}\n"
        f"Дата: {now.strftime('%d.%m.%Y в %H:%M')}\n"
        f"Изменение: {UPDATE_DESCRIPTION}"
    )

def reminder_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="⏸ Отложить на 7 дней",
            callback_data="pause_request_7"
        )]
    ])

def pause_confirmation_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="Да, отложить",
                callback_data="pause_confirm_7"
            ),
            InlineKeyboardButton(
                text="Нет, оставить",
                callback_data="pause_cancel"
            )
        ]
    ])

def resume_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="↩️ Отменить отсрочку",
            callback_data="resume_reminders"
        )]
    ])

# --- ХЕНДЛЕРЫ ---

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    user_id = message.from_user.id
    await add_user(user_id)

    paused_until = await get_pause_until(user_id)
    if paused_until:
        await message.answer(
            f"⏸ Напоминания отложены до {format_pause_until(paused_until)}.\n"
            "Если это было случайно, нажми кнопку ниже.",
            reply_markup=resume_keyboard()
        )
        return

    await message.answer("привет сонечка любимая ✅ ")

@dp.message(Command("resume"))
async def cmd_resume(message: types.Message):
    if await resume_reminders(message.from_user.id):
        await message.answer("✅ Отсрочка отменена. Напоминания снова включены.")
        return

    await message.answer("✅ Напоминания уже включены.")

@dp.message(Command("version"))
async def cmd_version(message: types.Message):
    await message.answer(version_message())

@dp.callback_query(F.data == "pause_request_7")
async def pause_request(callback: types.CallbackQuery):
    paused_until = await get_pause_until(callback.from_user.id)
    if paused_until:
        if callback.message:
            await callback.message.edit_text(
                f"⏸ Напоминания уже отложены до {format_pause_until(paused_until)}.",
                reply_markup=resume_keyboard()
            )
        await callback.answer("Отсрочка уже включена")
        return

    if callback.message:
        await callback.message.edit_reply_markup(
            reply_markup=pause_confirmation_keyboard()
        )
    await callback.answer("Точно отложить напоминания на 7 дней?")

@dp.callback_query(F.data == "pause_cancel")
async def pause_cancel(callback: types.CallbackQuery):
    if callback.message:
        await callback.message.edit_reply_markup(reply_markup=reminder_keyboard())
    await callback.answer("Напоминания остаются включенными")

@dp.callback_query(F.data == "pause_confirm_7")
async def pause_confirm(callback: types.CallbackQuery):
    paused_until = await pause_reminders(callback.from_user.id)
    if callback.message:
        await callback.message.edit_text(
            f"⏸ Напоминания отложены до {format_pause_until(paused_until)}.\n"
            "Передумала? Отсрочку можно отменить в любой момент.",
            reply_markup=resume_keyboard()
        )
    await callback.answer("Напоминания отложены на 7 дней")

@dp.callback_query(F.data == "resume_reminders")
async def resume_from_button(callback: types.CallbackQuery):
    resumed = await resume_reminders(callback.from_user.id)
    if callback.message:
        text = (
            "✅ Отсрочка отменена. Напоминания снова включены."
            if resumed
            else "✅ Напоминания уже включены."
        )
        await callback.message.edit_text(text, reply_markup=None)
    await callback.answer("Напоминания включены")

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

    users = await get_users_for_reminders()
    for user_id in users:
        try:
            await bot.send_message(
                user_id,
                "💊 Доброе утро! Пришли фото упаковки.",
                reply_markup=reminder_keyboard()
            )
        except: pass

async def nagging_check():
    now = datetime.now()
    if not (1 <= now.day <= 20): return
    
    # Внимание! Серверное время.
    # Если ты хочешь напоминать с 22:00 до 01:00 по ТВОЕМУ времени
    # (а сервер отстает на 2 часа), то пишем с 20:00 до 23:00
    if not (20 <= now.hour < 23): return

    users = await get_users_for_reminders()
    for user_id in users:
        if not await check_completed(user_id):
            try:
                await bot.send_message(
                    user_id,
                    "⏰ Выпей таблетку и пришли фото!",
                    reply_markup=reminder_keyboard()
                )
            except: pass

# --- ЗАПУСК ---
async def main():
    await init_db() # Подключаемся к БД
    
    # Часовой пояс не нужен, если мы просто сдвигаем часы вручную (hour=20 -> 22:00)
    scheduler.add_job(morning_reminder, "cron", hour=20, minute=0)
    scheduler.add_job(nagging_check, "interval", minutes=10)
    
    scheduler.start()
    
    await bot.delete_webhook(drop_pending_updates=True)

    try:
        await bot.send_message(ADMIN_ID, version_message())
    except Exception as error:
        logging.error("Не удалось отправить сообщение об обновлении: %s", error)

    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
