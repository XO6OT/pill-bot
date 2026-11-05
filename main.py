import asyncio
import logging
import os
import asyncpg
import pytz
from datetime import datetime
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from reminder_parser import ReminderError, looks_like_reminder, parse_reminder, with_reminder_text

# --- КОНФИГУРАЦИЯ ---
# Railway сам подставит сюда пароль от базы
DATABASE_URL = os.getenv("DATABASE_URL") 
BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = 565936264
APP_VERSION = "2026.11.05-own-reminders"
UPDATE_DESCRIPTION = "свои напоминания: «напомни мне в 12:30 о …», список /reminders"

# Список пользователей, которые ДОЛЖНЫ быть в базе всегда
PRELOAD_USERS = [565936264, 8530310460, 907912564]

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler()
pool = None  # Тут будет соединение с базой
LOCAL_TIMEZONE = pytz.timezone("Europe/Kaliningrad")
PILL_REMINDER_TEXT = "Люблю тебя котик, пришли фото ❤️❤️❤️"
NAGGING_TEXT = "⏰ Выпей таблетку и пришли фото!"

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
        
        # 4. Свои напоминания пользователей («напомни мне в 12:30 о …»)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_reminders (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                text TEXT NOT NULL,
                remind_at TIMESTAMPTZ NOT NULL,
                daily BOOLEAN NOT NULL DEFAULT FALSE
            );
        """)

        # 5. Имена для админки и флаг напоминания об учёбе
        await conn.execute("""
            ALTER TABLE users
                ADD COLUMN IF NOT EXISTS full_name TEXT,
                ADD COLUMN IF NOT EXISTS username TEXT,
                ADD COLUMN IF NOT EXISTS study_reminder BOOLEAN NOT NULL DEFAULT FALSE,
                ADD COLUMN IF NOT EXISTS custom_text TEXT,
                ADD COLUMN IF NOT EXISTS custom_text_date DATE;
        """)

        # 6. Добавляем твоих друзей только в пустую базу,
        # иначе удалённые через /users вернулись бы после перезапуска
        if await conn.fetchval("SELECT COUNT(*) FROM users") == 0:
            for uid in PRELOAD_USERS:
                await conn.execute("""
                    INSERT INTO users (user_id) VALUES ($1)
                    ON CONFLICT (user_id) DO NOTHING
                """, uid)

        logging.info("✅ База данных подключена и обновлена!")

async def add_user(user_id):
    await pool.execute("INSERT INTO users (user_id) VALUES ($1) ON CONFLICT DO NOTHING", user_id)

async def save_user(user: types.User):
    """Добавляет пользователя и запоминает его имя для админки."""
    await pool.execute("""
        INSERT INTO users (user_id, full_name, username) VALUES ($1, $2, $3)
        ON CONFLICT (user_id) DO UPDATE
        SET full_name = EXCLUDED.full_name, username = EXCLUDED.username
    """, user.id, user.full_name, user.username)

async def get_all_users():
    """Все пользователи для админки, по имени."""
    return await pool.fetch("""
        SELECT user_id, full_name, username, study_reminder, joined_at
        FROM users
        ORDER BY full_name NULLS LAST, user_id
    """)

async def get_user(user_id):
    return await pool.fetchrow("""
        SELECT user_id, full_name, username, study_reminder, joined_at
        FROM users WHERE user_id = $1
    """, user_id)

async def toggle_study_reminder(user_id):
    """Включает или выключает напоминание об учёбе. Возвращает новое значение."""
    return await pool.fetchval("""
        UPDATE users SET study_reminder = NOT study_reminder
        WHERE user_id = $1
        RETURNING study_reminder
    """, user_id)

async def delete_user(user_id):
    """Удаляет пользователя вместе с историей отчётов и отсрочкой."""
    await pool.execute("DELETE FROM history WHERE user_id = $1", user_id)
    result = await pool.execute("DELETE FROM users WHERE user_id = $1", user_id)
    return result == "DELETE 1"

def today_local():
    return datetime.now(LOCAL_TIMEZONE).date()

async def set_today_text(user_id, text):
    """Текст таблеточных напоминаний только на сегодняшний день по Калининграду."""
    await pool.execute(
        "UPDATE users SET custom_text = $2, custom_text_date = $3 WHERE user_id = $1",
        user_id, text, today_local()
    )

async def clear_today_text(user_id):
    await pool.execute(
        "UPDATE users SET custom_text = NULL, custom_text_date = NULL WHERE user_id = $1",
        user_id
    )

async def get_today_text(user_id):
    """Текст админа на сегодня или None, если он не задан или устарел."""
    return await pool.fetchval(
        "SELECT custom_text FROM users WHERE user_id = $1 AND custom_text_date = $2",
        user_id, today_local()
    )

MAX_OWN_REMINDERS = 30

async def add_own_reminder(user_id, parsed):
    """Сохраняет напоминание. Возвращает id или None, если их уже слишком много."""
    count = await pool.fetchval("SELECT COUNT(*) FROM user_reminders WHERE user_id = $1", user_id)
    if count >= MAX_OWN_REMINDERS:
        return None
    return await pool.fetchval("""
        INSERT INTO user_reminders (user_id, text, remind_at, daily)
        VALUES ($1, $2, $3, $4) RETURNING id
    """, user_id, parsed.text, parsed.remind_at, parsed.daily)

async def get_own_reminders(user_id):
    return await pool.fetch("""
        SELECT id, text, remind_at, daily FROM user_reminders
        WHERE user_id = $1 ORDER BY remind_at
    """, user_id)

async def delete_own_reminder(reminder_id, user_id):
    result = await pool.execute(
        "DELETE FROM user_reminders WHERE id = $1 AND user_id = $2", reminder_id, user_id
    )
    return result == "DELETE 1"

async def get_study_users():
    rows = await pool.fetch("SELECT user_id FROM users WHERE study_reminder")
    return [row['user_id'] for row in rows]

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

def format_remind_at(remind_at, daily=False):
    local = remind_at.astimezone(LOCAL_TIMEZONE)
    if daily:
        return f"каждый день в {local.strftime('%H:%M')}"
    return local.strftime("%d.%m в %H:%M")

def own_reminders_keyboard(reminders):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"🗑 {format_remind_at(r['remind_at'], r['daily'])} — {r['text'][:30]}",
            callback_data=f"rem_del_{r['id']}"
        )]
        for r in reminders
    ])

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

# --- АДМИНКА ---

def user_title(user):
    """Имя пользователя для кнопок; без имени — ID."""
    name = user['full_name'] or f"ID {user['user_id']}"
    if user['username']:
        name += f" (@{user['username']})"
    return name

def users_list_keyboard(users):
    rows = [
        [InlineKeyboardButton(
            text=("📚 " if user['study_reminder'] else "") + user_title(user),
            callback_data=f"adm_user_{user['user_id']}"
        )]
        for user in users
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)

class AdminText(StatesGroup):
    waiting = State()

def user_card_keyboard(user, has_today_text=False):
    user_id = user['user_id']
    study_text = "📚 Учёба: вкл ✅" if user['study_reminder'] else "📚 Учёба: выкл"
    text_row = [InlineKeyboardButton(text="✏️ Текст на сегодня", callback_data=f"adm_text_{user_id}")]
    if has_today_text:
        text_row.append(
            InlineKeyboardButton(text="↩️ Обычный текст", callback_data=f"adm_textreset_{user_id}")
        )
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=study_text, callback_data=f"adm_study_{user_id}")],
        text_row,
        [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"adm_del_{user_id}")],
        [InlineKeyboardButton(text="⬅️ Все пользователи", callback_data="adm_users")]
    ])

def delete_confirmation_keyboard(user_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Да, удалить", callback_data=f"adm_delok_{user_id}"),
            InlineKeyboardButton(text="Нет", callback_data=f"adm_user_{user_id}")
        ]
    ])

def users_list_text(users):
    if not users:
        return "👥 Пользователей нет."
    return (
        f"👥 Пользователи: {len(users)}\n"
        "📚 — получает напоминание об учёбе в 11:30.\n"
        "Нажми на имя, чтобы управлять."
    )

async def user_card_text(user, today_text=None):
    paused_until = await get_pause_until(user['user_id'])
    pause = f"отложены до {format_pause_until(paused_until)}" if paused_until else "включены"
    took = "да ✅" if await check_completed(user['user_id']) else "нет"
    return (
        f"👤 {user_title(user)}\n"
        f"ID: {user['user_id']}\n"
        f"Таблетки: {pause}\n"
        f"Отчёт сегодня: {took}\n"
        f"Учёба в 11:30: {'да' if user['study_reminder'] else 'нет'}\n"
        f"Текст напоминаний сегодня: {today_text or 'обычный'}"
    )

async def edit_user_card(message: types.Message, user):
    today_text = await get_today_text(user['user_id'])
    await message.edit_text(
        await user_card_text(user, today_text),
        reply_markup=user_card_keyboard(user, has_today_text=bool(today_text))
    )

async def fill_missing_names(users):
    """Подтягивает имена из Telegram для тех, кто ещё не писал боту после обновления."""
    for user in users:
        if user['full_name']:
            continue
        try:
            chat = await bot.get_chat(user['user_id'])
        except Exception:
            continue
        await pool.execute(
            "UPDATE users SET full_name = $2, username = $3 WHERE user_id = $1",
            user['user_id'], chat.full_name, chat.username
        )

async def show_users(message: types.Message, edit=False):
    users = await get_all_users()
    if any(not user['full_name'] for user in users):
        await fill_missing_names(users)
        users = await get_all_users()

    text, keyboard = users_list_text(users), users_list_keyboard(users)
    if edit:
        await message.edit_text(text, reply_markup=keyboard)
    else:
        await message.answer(text, reply_markup=keyboard)

@dp.message(Command("users"), F.from_user.id == ADMIN_ID)
async def cmd_users(message: types.Message, state: FSMContext):
    await state.clear()
    await show_users(message)

@dp.callback_query(F.data == "adm_users", F.from_user.id == ADMIN_ID)
async def admin_users(callback: types.CallbackQuery):
    await show_users(callback.message, edit=True)
    await callback.answer()

@dp.callback_query(F.data.startswith("adm_user_"), F.from_user.id == ADMIN_ID)
async def admin_user_card(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    user = await get_user(int(callback.data.split("_")[2]))
    if not user:
        await callback.answer("Пользователь уже удалён")
        await show_users(callback.message, edit=True)
        return

    await edit_user_card(callback.message, user)
    await callback.answer()

@dp.callback_query(F.data.startswith("adm_study_"), F.from_user.id == ADMIN_ID)
async def admin_toggle_study(callback: types.CallbackQuery):
    user_id = int(callback.data.split("_")[2])
    enabled = await toggle_study_reminder(user_id)
    user = await get_user(user_id)
    if not user:
        await callback.answer("Пользователь уже удалён")
        return

    await edit_user_card(callback.message, user)
    await callback.answer("Учёба включена" if enabled else "Учёба выключена")

@dp.callback_query(F.data.startswith("adm_text_"), F.from_user.id == ADMIN_ID)
async def admin_text_request(callback: types.CallbackQuery, state: FSMContext):
    user = await get_user(int(callback.data.split("_")[2]))
    if not user:
        await callback.answer("Пользователь уже удалён")
        return

    await state.set_state(AdminText.waiting)
    await state.update_data(user_id=user['user_id'])
    await callback.message.edit_text(
        f"✏️ Пришли текст для {user_title(user)}.\n"
        f"Сегодня ({today_local().strftime('%d.%m')}) таблеточные напоминания придут "
        f"с ним вместо обычного «{PILL_REMINDER_TEXT}». Завтра текст снова станет обычным.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Отмена", callback_data=f"adm_user_{user['user_id']}")]
        ])
    )
    await callback.answer()

@dp.message(AdminText.waiting, F.from_user.id == ADMIN_ID, F.text, ~F.text.startswith("/"))
async def admin_text_save(message: types.Message, state: FSMContext):
    user_id = (await state.get_data())['user_id']
    await state.clear()
    user = await get_user(user_id)
    if not user:
        await message.answer("Пользователь уже удалён.")
        return

    await set_today_text(user_id, message.text)
    await message.answer(
        f"✅ Сегодня {user_title(user)} получит:\n{message.text}",
        reply_markup=user_card_keyboard(user, has_today_text=True)
    )

@dp.callback_query(F.data.startswith("adm_textreset_"), F.from_user.id == ADMIN_ID)
async def admin_text_reset(callback: types.CallbackQuery):
    user = await get_user(int(callback.data.split("_")[2]))
    if not user:
        await callback.answer("Пользователь уже удалён")
        return

    await clear_today_text(user['user_id'])
    await edit_user_card(callback.message, user)
    await callback.answer("Текст снова обычный")

@dp.callback_query(F.data.startswith("adm_del_"), F.from_user.id == ADMIN_ID)
async def admin_delete_request(callback: types.CallbackQuery):
    user = await get_user(int(callback.data.split("_")[2]))
    if not user:
        await callback.answer("Пользователь уже удалён")
        return

    await callback.message.edit_text(
        f"🗑 Удалить {user_title(user)}?\n"
        "Бот перестанет ему писать, история отчётов сотрётся. "
        "Если он снова напишет боту, то вернётся в список.",
        reply_markup=delete_confirmation_keyboard(user['user_id'])
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("adm_delok_"), F.from_user.id == ADMIN_ID)
async def admin_delete_confirm(callback: types.CallbackQuery):
    deleted = await delete_user(int(callback.data.split("_")[2]))
    await callback.answer("Удалён" if deleted else "Пользователь уже удалён")
    await show_users(callback.message, edit=True)

# --- ХЕНДЛЕРЫ ---

@dp.message.outer_middleware()
async def remember_user_name(handler, message: types.Message, data):
    """Обновляет имя пользователя при каждом сообщении боту."""
    if message.from_user:
        await save_user(message.from_user)
    return await handler(message, data)

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    user_id = message.from_user.id

    paused_until = await get_pause_until(user_id)
    if paused_until:
        await message.answer(
            f"⏸ Напоминания отложены до {format_pause_until(paused_until)}.\n"
            "Если это было случайно, нажми кнопку ниже.",
            reply_markup=resume_keyboard()
        )
        return

    if user_id == ADMIN_ID:
        await message.answer("✅ Ты админ. Пользователи и учёба: /users")
        return

    await message.answer("привет сонечка любимая ✅\n\n" + OWN_REMINDERS_HELP)

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

@dp.callback_query(F.data.startswith("approve_"), F.from_user.id == ADMIN_ID)
async def admin_approve(callback: types.CallbackQuery):
    user_id = int(callback.data.split("_")[1])
    
    await mark_completed(user_id)
    
    try:
        await bot.send_message(user_id, "✅ Админ подтвердил! Напоминания отключены.")
    except: pass

    await callback.message.edit_caption(caption=f"{callback.message.caption}\n\n✅ ПОДТВЕРЖДЕНО")
    await callback.answer()

@dp.callback_query(F.data.startswith("reject_"), F.from_user.id == ADMIN_ID)
async def admin_reject(callback: types.CallbackQuery):
    user_id = int(callback.data.split("_")[1])
    try:
        await bot.send_message(user_id, "❌ Отчет отклонен. Пришли нормальное фото!")
    except: pass
    await callback.message.edit_caption(caption=f"{callback.message.caption}\n\n❌ ОТКЛОНЕНО")
    await callback.answer()

# --- СВОИ НАПОМИНАНИЯ ---

OWN_REMINDERS_HELP = (
    "Можно ставить себе напоминания обычным сообщением, например:\n"
    "• напомни мне в 12:30 о звонке\n"
    "• в 18:00 напомни купить хлеб\n"
    "• завтра в 9 напомни про врача\n"
    "• через 20 минут напомни выпить воды\n"
    "• каждый день в 8:00 напомни про витамины\n"
    "Список и удаление: /reminders"
)

@dp.message(F.text, ~F.text.startswith("/"), F.text.func(looks_like_reminder))
async def own_reminder_create(message: types.Message):
    try:
        parsed = parse_reminder(message.text, datetime.now(LOCAL_TIMEZONE))
    except ReminderError as error:
        await message.answer(str(error))
        return

    reminder_id = await add_own_reminder(message.from_user.id, parsed)
    if reminder_id is None:
        await message.answer(
            f"У тебя уже {MAX_OWN_REMINDERS} напоминаний. Удали ненужные в /reminders."
        )
        return

    await message.answer(
        with_reminder_text(f"✅ Напомню {format_remind_at(parsed.remind_at, parsed.daily)}", parsed.text),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Отменить", callback_data=f"rem_del_{reminder_id}")]
        ])
    )

@dp.message(Command("reminders"))
async def cmd_reminders(message: types.Message):
    reminders = await get_own_reminders(message.from_user.id)
    if not reminders:
        await message.answer("У тебя пока нет своих напоминаний.\n\n" + OWN_REMINDERS_HELP)
        return

    await message.answer(
        "⏰ Твои напоминания. Нажми на ненужное, чтобы удалить:",
        reply_markup=own_reminders_keyboard(reminders)
    )

@dp.callback_query(F.data.startswith("rem_del_"))
async def own_reminder_delete(callback: types.CallbackQuery):
    deleted = await delete_own_reminder(int(callback.data.split("_")[2]), callback.from_user.id)
    reminders = await get_own_reminders(callback.from_user.id)
    if callback.message:
        if reminders:
            await callback.message.edit_text(
                "⏰ Твои напоминания. Нажми на ненужное, чтобы удалить:",
                reply_markup=own_reminders_keyboard(reminders)
            )
        else:
            await callback.message.edit_text("🗑 Напоминание удалено. Других нет.")
    await callback.answer("Удалено" if deleted else "Уже удалено")

async def send_own_reminders():
    """Раз в минуту отправляет наступившие напоминания пользователей."""
    due = await pool.fetch("""
        SELECT id, user_id, text, daily FROM user_reminders
        WHERE remind_at <= NOW() ORDER BY remind_at
    """)
    for reminder in due:
        try:
            await bot.send_message(reminder['user_id'], with_reminder_text("⏰ Напоминаю", reminder['text']))
        except Exception:
            logging.exception("Не удалось отправить своё напоминание")

        if reminder['daily']:
            # следующий день, даже если бот лежал несколько суток
            await pool.execute("""
                UPDATE user_reminders
                SET remind_at = remind_at
                    + (FLOOR(EXTRACT(EPOCH FROM NOW() - remind_at) / 86400) + 1) * INTERVAL '1 day'
                WHERE id = $1
            """, reminder['id'])
        else:
            await pool.execute("DELETE FROM user_reminders WHERE id = $1", reminder['id'])

# --- ПЛАНИРОВЩИК ---

async def study_reminder():
    """Ежедневное напоминание без отчёта и кнопок — только тем, кому включил админ."""
    for user_id in await get_study_users():
        try:
            await bot.send_message(user_id, "📚 Пора изучить тему!")
        except Exception:
            logging.exception("Не удалось отправить напоминание об учёбе")

async def morning_reminder():
    users = await get_users_for_reminders()
    for user_id in users:
        try:
            await bot.send_message(
                user_id,
                await get_today_text(user_id) or PILL_REMINDER_TEXT,
                reply_markup=reminder_keyboard()
            )
        except: pass

async def nagging_check():
    now = datetime.now(LOCAL_TIMEZONE)
    # Напоминаем с 22:00 до 01:00 по Калининграду.
    if not (now.hour >= 22 or now.hour < 1): return

    users = await get_users_for_reminders()
    for user_id in users:
        if not await check_completed(user_id):
            try:
                await bot.send_message(
                    user_id,
                    await get_today_text(user_id) or NAGGING_TEXT,
                    reply_markup=reminder_keyboard()
                )
            except: pass

# --- ЗАПУСК ---
async def main():
    await init_db() # Подключаемся к БД

    scheduler.add_job(
        study_reminder, "cron", hour=11, minute=30,
        timezone=LOCAL_TIMEZONE, id="daily_study_reminder"
    )
    
    scheduler.add_job(
        morning_reminder, "cron", hour=22, minute=0,
        timezone=LOCAL_TIMEZONE, id="daily_pill_reminder"
    )
    scheduler.add_job(nagging_check, "interval", minutes=10)
    scheduler.add_job(send_own_reminders, "cron", second=0, id="own_reminders")
    
    scheduler.start()
    
    await bot.delete_webhook(drop_pending_updates=True)
    await bot.set_my_commands([
        types.BotCommand(command="reminders", description="Мои напоминания"),
        types.BotCommand(command="resume", description="Отменить отсрочку таблеток"),
    ])

    try:
        await bot.send_message(ADMIN_ID, version_message())
    except Exception as error:
        logging.error("Не удалось отправить сообщение об обновлении: %s", error)

    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
