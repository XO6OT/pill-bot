import asyncio
import logging
import os
import re
import asyncpg
import pytz
from datetime import datetime, time, timedelta
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import ErrorEvent, InlineKeyboardMarkup, InlineKeyboardButton
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from reminder_parser import (
    HELP_EXAMPLES, ReminderError, describe_when, looks_like_reminder, parse_reminder, with_reminder_text,
)

# --- КОНФИГУРАЦИЯ ---
# Railway сам подставит сюда пароль от базы
DATABASE_URL = os.getenv("DATABASE_URL")
BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = 565936264
APP_VERSION = "2026.11.12-admin-notifications"
UPDATE_DESCRIPTION = "таблетки убраны; свои уведомления в /notify; фото можно прислать и запросить"

# Список пользователей, которые ДОЛЖНЫ быть в базе всегда
PRELOAD_USERS = [565936264, 8530310460, 907912564]

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler()
pool = None  # Тут будет соединение с базой
LOCAL_TIMEZONE = pytz.timezone("Europe/Kaliningrad")
PHOTO_REQUEST_TEXT = "📸 Пришли, пожалуйста, фото ❤️"
# опоздавшее уведомление (бот перезапускался) ещё отправляем, позже — уже нет
NOTIFICATION_GRACE = timedelta(hours=1)

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
        # 2. История отчётов о таблетках (больше не пополняется, хранится как было)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS history (
                id SERIAL PRIMARY KEY,
                user_id BIGINT,
                date DATE DEFAULT CURRENT_DATE,
                UNIQUE(user_id, date)
            );
        """)

        # 3. Свои напоминания пользователей («напомни мне в 12:30 о …»)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_reminders (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                text TEXT NOT NULL,
                remind_at TIMESTAMPTZ NOT NULL,
                daily BOOLEAN NOT NULL DEFAULT FALSE
            );
        """)
        # повтор в днях: 0 — один раз, 1 — каждый день, 7 — каждую неделю
        await conn.execute("""
            ALTER TABLE user_reminders ADD COLUMN IF NOT EXISTS repeat_days INT NOT NULL DEFAULT 0;
            UPDATE user_reminders SET repeat_days = 1 WHERE daily AND repeat_days = 0;
        """)

        # 4. Имена для админки (study_reminder нужен только для переноса в уведомления)
        await conn.execute("""
            ALTER TABLE users
                ADD COLUMN IF NOT EXISTS full_name TEXT,
                ADD COLUMN IF NOT EXISTS username TEXT,
                ADD COLUMN IF NOT EXISTS study_reminder BOOLEAN NOT NULL DEFAULT FALSE;
        """)

        # 5. Уведомления админа «каждый день в такое-то время» и кому они приходят
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS admin_notifications (
                id SERIAL PRIMARY KEY,
                text TEXT NOT NULL,
                notify_time TIME NOT NULL,
                last_sent_date DATE
            );
            CREATE TABLE IF NOT EXISTS admin_notification_users (
                notification_id INT NOT NULL REFERENCES admin_notifications(id) ON DELETE CASCADE,
                user_id BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                PRIMARY KEY (notification_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS bot_settings (
                key TEXT PRIMARY KEY,
                value TEXT
            );
        """)

        # 6. Добавляем твоих друзей только в пустую базу,
        # иначе удалённые через /users вернулись бы после перезапуска
        if await conn.fetchval("SELECT COUNT(*) FROM users") == 0:
            for uid in PRELOAD_USERS:
                await conn.execute("""
                    INSERT INTO users (user_id) VALUES ($1)
                    ON CONFLICT (user_id) DO NOTHING
                """, uid)

        # 7. Один раз переносим «📚 Пора изучить тему!» в 11:30 в уведомления
        migrated = await conn.fetchval("SELECT value FROM bot_settings WHERE key = 'study_migrated'")
        if not migrated:
            async with conn.transaction():
                notification_id = await conn.fetchval("""
                    INSERT INTO admin_notifications (text, notify_time, last_sent_date)
                    VALUES ('📚 Пора изучить тему!', '11:30', $1) RETURNING id
                """, initial_last_sent(time(11, 30)))
                await conn.execute("""
                    INSERT INTO admin_notification_users (notification_id, user_id)
                    SELECT $1, user_id FROM users WHERE study_reminder
                """, notification_id)
                await conn.execute(
                    "INSERT INTO bot_settings (key, value) VALUES ('study_migrated', '1')"
                )

        logging.info("✅ База данных подключена и обновлена!")

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
        SELECT user_id, full_name, username, joined_at
        FROM users
        ORDER BY full_name NULLS LAST, user_id
    """)

async def get_user(user_id):
    return await pool.fetchrow("""
        SELECT user_id, full_name, username, joined_at
        FROM users WHERE user_id = $1
    """, user_id)

async def delete_user(user_id):
    """Удаляет пользователя вместе с историей, его напоминаниями и подписками на уведомления."""
    await pool.execute("DELETE FROM history WHERE user_id = $1", user_id)
    result = await pool.execute("DELETE FROM users WHERE user_id = $1", user_id)
    return result == "DELETE 1"

MAX_OWN_REMINDERS = 30

async def add_own_reminder(user_id, parsed):
    """Сохраняет напоминание. Возвращает id или None, если их уже слишком много."""
    count = await pool.fetchval("SELECT COUNT(*) FROM user_reminders WHERE user_id = $1", user_id)
    if count >= MAX_OWN_REMINDERS:
        return None
    return await pool.fetchval("""
        INSERT INTO user_reminders (user_id, text, remind_at, daily, repeat_days)
        VALUES ($1, $2, $3, $4, $5) RETURNING id
    """, user_id, parsed.text, parsed.remind_at, parsed.repeat_days == 1, parsed.repeat_days)

async def get_own_reminders(user_id):
    return await pool.fetch("""
        SELECT id, text, remind_at, repeat_days FROM user_reminders
        WHERE user_id = $1 ORDER BY remind_at
    """, user_id)

async def delete_own_reminder(reminder_id, user_id):
    result = await pool.execute(
        "DELETE FROM user_reminders WHERE id = $1 AND user_id = $2", reminder_id, user_id
    )
    return result == "DELETE 1"

# --- УВЕДОМЛЕНИЯ АДМИНА (БАЗА) ---

def initial_last_sent(notify_time, now=None):
    """Если сегодняшнее время уже прошло, отмечаем день как отправленный —
    иначе новое уведомление на 11:30, созданное в 15:00, пришло бы сразу."""
    now = now or datetime.now(LOCAL_TIMEZONE)
    return now.date() if notify_time <= now.time() else None

async def create_notification(text, notify_time):
    return await pool.fetchval("""
        INSERT INTO admin_notifications (text, notify_time, last_sent_date)
        VALUES ($1, $2, $3) RETURNING id
    """, text, notify_time, initial_last_sent(notify_time))

async def get_notifications():
    return await pool.fetch("""
        SELECT n.id, n.text, n.notify_time, n.last_sent_date, COUNT(u.user_id) AS recipients
        FROM admin_notifications n
        LEFT JOIN admin_notification_users u ON u.notification_id = n.id
        GROUP BY n.id
        ORDER BY n.notify_time, n.id
    """)

async def get_notification(notification_id):
    return await pool.fetchrow(
        "SELECT id, text, notify_time, last_sent_date FROM admin_notifications WHERE id = $1",
        notification_id
    )

async def set_notification_time(notification_id, notify_time):
    await pool.execute(
        "UPDATE admin_notifications SET notify_time = $2, last_sent_date = $3 WHERE id = $1",
        notification_id, notify_time, initial_last_sent(notify_time)
    )

async def set_notification_text(notification_id, text):
    await pool.execute("UPDATE admin_notifications SET text = $2 WHERE id = $1", notification_id, text)

async def delete_notification(notification_id):
    result = await pool.execute("DELETE FROM admin_notifications WHERE id = $1", notification_id)
    return result == "DELETE 1"

async def get_recipients(notification_id):
    rows = await pool.fetch(
        "SELECT user_id FROM admin_notification_users WHERE notification_id = $1", notification_id
    )
    return {row['user_id'] for row in rows}

async def get_user_notification_ids(user_id):
    rows = await pool.fetch(
        "SELECT notification_id FROM admin_notification_users WHERE user_id = $1", user_id
    )
    return {row['notification_id'] for row in rows}

async def toggle_recipient(notification_id, user_id):
    """Включает или выключает уведомление человеку. Возвращает True, если теперь включено."""
    removed = await pool.execute(
        "DELETE FROM admin_notification_users WHERE notification_id = $1 AND user_id = $2",
        notification_id, user_id
    )
    if removed == "DELETE 1":
        return False
    await pool.execute("""
        INSERT INTO admin_notification_users (notification_id, user_id) VALUES ($1, $2)
        ON CONFLICT DO NOTHING
    """, notification_id, user_id)
    return True

def notification_is_due(notification, now):
    """Пора отправить: время наступило, сегодня ещё не отправляли и опоздание не больше часа."""
    if notification['last_sent_date'] == now.date():
        return False
    scheduled = now.replace(
        hour=notification['notify_time'].hour, minute=notification['notify_time'].minute,
        second=0, microsecond=0
    )
    return scheduled <= now < scheduled + NOTIFICATION_GRACE

TIME_INPUT = re.compile(r"^\s*(?:в\s+)?([01]?\d|2[0-3])\s*[:.]\s*([0-5]\d)\s*(.*)$", re.IGNORECASE | re.DOTALL)

def parse_time_and_text(raw):
    """«11:30 Пора изучить тему» → (time(11, 30), "Пора изучить тему"). Не вышло — None."""
    match = TIME_INPUT.match(raw or "")
    if not match:
        return None
    return time(int(match.group(1)), int(match.group(2))), match.group(3).strip()

# --- ФОРМАТИРОВАНИЕ ---

def format_remind_at(remind_at, repeat_days=0):
    return describe_when(remind_at, repeat_days, datetime.now(LOCAL_TIMEZONE))

def own_reminders_keyboard(reminders):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"🗑 {format_remind_at(r['remind_at'], r['repeat_days'])} — {r['text'][:30]}",
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

def user_title(user):
    """Имя пользователя для кнопок; без имени — ID."""
    name = user['full_name'] or f"ID {user['user_id']}"
    if user['username']:
        name += f" (@{user['username']})"
    return name

def short_name(user):
    return user['full_name'] or f"ID {user['user_id']}"

def notification_label(notification):
    return f"{notification['notify_time'].strftime('%H:%M')} · {notification['text'][:30]}"

# --- АДМИНКА: ПОЛЬЗОВАТЕЛИ ---

class AdminInput(StatesGroup):
    new_notification = State()
    notification_time = State()
    notification_text = State()

def users_list_keyboard(users):
    rows = [
        [InlineKeyboardButton(text=user_title(user), callback_data=f"adm_user_{user['user_id']}")]
        for user in users
    ]
    rows.append([InlineKeyboardButton(text="🔔 Уведомления", callback_data="adm_notifs")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def users_list_text(users):
    if not users:
        return "👥 Пользователей нет."
    return f"👥 Пользователи: {len(users)}\nНажми на имя, чтобы управлять."

def user_card_keyboard(user, notifications, enabled_ids):
    user_id = user['user_id']
    rows = [[InlineKeyboardButton(text="📸 Запросить фото", callback_data=f"adm_photo_{user_id}")]]
    for notification in notifications:
        mark = "✅" if notification['id'] in enabled_ids else "▫️"
        rows.append([InlineKeyboardButton(
            text=f"{mark} {notification_label(notification)}",
            callback_data=f"usr_n_{notification['id']}_{user_id}"
        )])
    rows.append([InlineKeyboardButton(text="🗑 Удалить", callback_data=f"adm_del_{user_id}")])
    rows.append([InlineKeyboardButton(text="⬅️ Все пользователи", callback_data="adm_users")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def user_card_text(user, notifications, enabled_ids):
    text = f"👤 {user_title(user)}\nID: {user['user_id']}\n\n"
    if not notifications:
        return text + "Уведомлений пока нет. Создать: /notify"
    return text + "🔔 Уведомления: ✅ — приходит, ▫️ — нет. Нажми, чтобы переключить."

async def edit_user_card(message: types.Message, user):
    notifications = await get_notifications()
    enabled_ids = await get_user_notification_ids(user['user_id'])
    await message.edit_text(
        user_card_text(user, notifications, enabled_ids),
        reply_markup=user_card_keyboard(user, notifications, enabled_ids)
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

async def get_users_with_names():
    users = await get_all_users()
    if any(not user['full_name'] for user in users):
        await fill_missing_names(users)
        users = await get_all_users()
    return users

async def show_users(message: types.Message, edit=False):
    users = await get_users_with_names()
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
async def admin_users(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await show_users(callback.message, edit=True)
    await callback.answer()

@dp.callback_query(F.data.startswith("adm_user_"), F.from_user.id == ADMIN_ID)
async def admin_user_card(callback: types.CallbackQuery):
    user = await get_user(int(callback.data.split("_")[2]))
    if not user:
        await callback.answer("Пользователь уже удалён")
        await show_users(callback.message, edit=True)
        return

    await edit_user_card(callback.message, user)
    await callback.answer()

@dp.callback_query(F.data.startswith("adm_photo_"), F.from_user.id == ADMIN_ID)
async def admin_request_photo(callback: types.CallbackQuery):
    user = await get_user(int(callback.data.split("_")[2]))
    if not user:
        await callback.answer("Пользователь уже удалён")
        return

    try:
        await bot.send_message(user['user_id'], PHOTO_REQUEST_TEXT)
    except Exception:
        logging.exception("Не удалось запросить фото")
        await callback.answer("Не получилось: человек, похоже, остановил бота", show_alert=True)
        return
    await callback.answer(f"📸 Запрос отправлен: {short_name(user)}")

@dp.callback_query(F.data.startswith("usr_n_"), F.from_user.id == ADMIN_ID)
async def admin_toggle_from_user_card(callback: types.CallbackQuery):
    _, _, notification_id, user_id = callback.data.split("_")
    user = await get_user(int(user_id))
    if not user or not await get_notification(int(notification_id)):
        await callback.answer("Уже удалено")
        return

    enabled = await toggle_recipient(int(notification_id), int(user_id))
    await edit_user_card(callback.message, user)
    await callback.answer("Включено" if enabled else "Выключено")

@dp.callback_query(F.data.startswith("adm_del_"), F.from_user.id == ADMIN_ID)
async def admin_delete_request(callback: types.CallbackQuery):
    user = await get_user(int(callback.data.split("_")[2]))
    if not user:
        await callback.answer("Пользователь уже удалён")
        return

    await callback.message.edit_text(
        f"🗑 Удалить {user_title(user)}?\n"
        "Бот перестанет ему писать, его напоминания сотрутся. "
        "Если он снова напишет боту, то вернётся в список.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="Да, удалить", callback_data=f"adm_delok_{user['user_id']}"),
                InlineKeyboardButton(text="Нет", callback_data=f"adm_user_{user['user_id']}")
            ]
        ])
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("adm_delok_"), F.from_user.id == ADMIN_ID)
async def admin_delete_confirm(callback: types.CallbackQuery):
    deleted = await delete_user(int(callback.data.split("_")[2]))
    await callback.answer("Удалён" if deleted else "Пользователь уже удалён")
    await show_users(callback.message, edit=True)

# --- АДМИНКА: УВЕДОМЛЕНИЯ ---

NEW_NOTIFICATION_HELP = (
    "Пришли время и текст одним сообщением, например:\n"
    "11:30 📚 Пора изучить тему!\n\n"
    "Уведомление будет приходить каждый день в это время (по Калининграду)."
)

def cancel_keyboard(callback_data):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Отмена", callback_data=callback_data)]
    ])

def notifications_list_keyboard(notifications):
    rows = [
        [InlineKeyboardButton(
            text=f"{notification_label(n)} ({n['recipients']} чел.)",
            callback_data=f"ntf_open_{n['id']}"
        )]
        for n in notifications
    ]
    rows.append([InlineKeyboardButton(text="➕ Добавить", callback_data="ntf_new")])
    rows.append([InlineKeyboardButton(text="⬅️ Пользователи", callback_data="adm_users")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def notifications_list_text(notifications):
    if not notifications:
        return "🔔 Уведомлений пока нет. Нажми «➕ Добавить»."
    return "🔔 Уведомления каждый день. Нажми, чтобы выбрать людей, время или текст."

def notification_card_keyboard(notification, users, recipients):
    notification_id = notification['id']
    rows = [
        [InlineKeyboardButton(
            text=f"{'✅' if user['user_id'] in recipients else '▫️'} {short_name(user)}",
            callback_data=f"ntf_u_{notification_id}_{user['user_id']}"
        )]
        for user in users
    ]
    rows.append([
        InlineKeyboardButton(text="🕐 Время", callback_data=f"ntf_time_{notification_id}"),
        InlineKeyboardButton(text="✏️ Текст", callback_data=f"ntf_text_{notification_id}"),
    ])
    rows.append([InlineKeyboardButton(text="🗑 Удалить", callback_data=f"ntf_del_{notification_id}")])
    rows.append([InlineKeyboardButton(text="⬅️ Все уведомления", callback_data="adm_notifs")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def notification_card_text(notification, users, recipients):
    names = [short_name(user) for user in users if user['user_id'] in recipients]
    return (
        f"🔔 Каждый день в {notification['notify_time'].strftime('%H:%M')}\n"
        f"{notification['text']}\n\n"
        f"Кому: {', '.join(names) if names else 'пока никому'}\n"
        "Нажми на имя, чтобы включить ✅ или выключить ▫️."
    )

async def show_notifications(message: types.Message, edit=False):
    notifications = await get_notifications()
    text, keyboard = notifications_list_text(notifications), notifications_list_keyboard(notifications)
    if edit:
        await message.edit_text(text, reply_markup=keyboard)
    else:
        await message.answer(text, reply_markup=keyboard)

async def show_notification(message: types.Message, notification_id, edit=True):
    notification = await get_notification(notification_id)
    if not notification:
        await show_notifications(message, edit=edit)
        return
    users = await get_users_with_names()
    recipients = await get_recipients(notification_id)
    text = notification_card_text(notification, users, recipients)
    keyboard = notification_card_keyboard(notification, users, recipients)
    if edit:
        await message.edit_text(text, reply_markup=keyboard)
    else:
        await message.answer(text, reply_markup=keyboard)

@dp.message(Command("notify"), F.from_user.id == ADMIN_ID)
async def cmd_notify(message: types.Message, state: FSMContext):
    await state.clear()
    await show_notifications(message)

@dp.callback_query(F.data == "adm_notifs", F.from_user.id == ADMIN_ID)
async def admin_notifications(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await show_notifications(callback.message, edit=True)
    await callback.answer()

@dp.callback_query(F.data.startswith("ntf_open_"), F.from_user.id == ADMIN_ID)
async def admin_notification_card(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await show_notification(callback.message, int(callback.data.split("_")[2]))
    await callback.answer()

@dp.callback_query(F.data.startswith("ntf_u_"), F.from_user.id == ADMIN_ID)
async def admin_toggle_recipient(callback: types.CallbackQuery):
    _, _, notification_id, user_id = callback.data.split("_")
    if not await get_notification(int(notification_id)) or not await get_user(int(user_id)):
        await callback.answer("Уже удалено")
        await show_notifications(callback.message, edit=True)
        return

    enabled = await toggle_recipient(int(notification_id), int(user_id))
    await show_notification(callback.message, int(notification_id))
    await callback.answer("Включено" if enabled else "Выключено")

@dp.callback_query(F.data == "ntf_new", F.from_user.id == ADMIN_ID)
async def admin_new_notification(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(AdminInput.new_notification)
    await callback.message.edit_text(NEW_NOTIFICATION_HELP, reply_markup=cancel_keyboard("adm_notifs"))
    await callback.answer()

@dp.message(AdminInput.new_notification, F.from_user.id == ADMIN_ID, F.text, ~F.text.startswith("/"))
async def admin_new_notification_save(message: types.Message, state: FSMContext):
    parsed = parse_time_and_text(message.text)
    if not parsed or not parsed[1]:
        await message.answer(
            "Не получилось 🙈 Нужно время, а потом текст. Например:\n11:30 📚 Пора изучить тему!",
            reply_markup=cancel_keyboard("adm_notifs")
        )
        return

    await state.clear()
    notification_id = await create_notification(parsed[1], parsed[0])
    await message.answer("✅ Уведомление создано. Теперь выбери, кому оно приходит:")
    await show_notification(message, notification_id, edit=False)

@dp.callback_query(F.data.startswith("ntf_time_"), F.from_user.id == ADMIN_ID)
async def admin_notification_time(callback: types.CallbackQuery, state: FSMContext):
    notification_id = int(callback.data.split("_")[2])
    await state.set_state(AdminInput.notification_time)
    await state.update_data(notification_id=notification_id)
    await callback.message.edit_text(
        "🕐 Пришли новое время, например 12:00", reply_markup=cancel_keyboard(f"ntf_open_{notification_id}")
    )
    await callback.answer()

@dp.message(AdminInput.notification_time, F.from_user.id == ADMIN_ID, F.text, ~F.text.startswith("/"))
async def admin_notification_time_save(message: types.Message, state: FSMContext):
    notification_id = (await state.get_data())['notification_id']
    parsed = parse_time_and_text(message.text)
    if not parsed:
        await message.answer(
            "Не получилось 🙈 Пришли время так: 12:00", reply_markup=cancel_keyboard(f"ntf_open_{notification_id}")
        )
        return

    await state.clear()
    await set_notification_time(notification_id, parsed[0])
    await show_notification(message, notification_id, edit=False)

@dp.callback_query(F.data.startswith("ntf_text_"), F.from_user.id == ADMIN_ID)
async def admin_notification_text(callback: types.CallbackQuery, state: FSMContext):
    notification_id = int(callback.data.split("_")[2])
    await state.set_state(AdminInput.notification_text)
    await state.update_data(notification_id=notification_id)
    await callback.message.edit_text(
        "✏️ Пришли новый текст уведомления", reply_markup=cancel_keyboard(f"ntf_open_{notification_id}")
    )
    await callback.answer()

@dp.message(AdminInput.notification_text, F.from_user.id == ADMIN_ID, F.text, ~F.text.startswith("/"))
async def admin_notification_text_save(message: types.Message, state: FSMContext):
    notification_id = (await state.get_data())['notification_id']
    await state.clear()
    await set_notification_text(notification_id, message.text)
    await show_notification(message, notification_id, edit=False)

@dp.callback_query(F.data.startswith("ntf_del_"), F.from_user.id == ADMIN_ID)
async def admin_notification_delete(callback: types.CallbackQuery):
    notification = await get_notification(int(callback.data.split("_")[2]))
    if not notification:
        await callback.answer("Уже удалено")
        await show_notifications(callback.message, edit=True)
        return

    await callback.message.edit_text(
        f"🗑 Удалить уведомление «{notification_label(notification)}»?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="Да, удалить", callback_data=f"ntf_delok_{notification['id']}"),
                InlineKeyboardButton(text="Нет", callback_data=f"ntf_open_{notification['id']}")
            ]
        ])
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("ntf_delok_"), F.from_user.id == ADMIN_ID)
async def admin_notification_delete_confirm(callback: types.CallbackQuery):
    deleted = await delete_notification(int(callback.data.split("_")[2]))
    await callback.answer("Удалено" if deleted else "Уже удалено")
    await show_notifications(callback.message, edit=True)

# --- ХЕНДЛЕРЫ ---

@dp.message.outer_middleware()
async def remember_user_name(handler, message: types.Message, data):
    """Обновляет имя пользователя при каждом сообщении боту."""
    if message.from_user:
        await save_user(message.from_user)
    return await handler(message, data)

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    if message.from_user.id == ADMIN_ID:
        await message.answer(
            "✅ Ты админ.\n"
            "/users — пользователи, запрос фото\n"
            "/notify — уведомления каждый день"
        )
        return

    await message.answer(
        "привет сонечка любимая ✅\n\n" + OWN_REMINDERS_HELP + "\n\nФото можно прислать в любой момент 📸"
    )

@dp.message(Command("version"))
async def cmd_version(message: types.Message):
    await message.answer(version_message())

@dp.message(F.photo | (F.document & F.document.mime_type.startswith("image/")))
async def handle_photo(message: types.Message):
    """Любое фото от пользователя сразу пересылается админу."""
    if message.from_user.id == ADMIN_ID:
        return

    caption = f"📸 Фото от {message.from_user.full_name}"
    if message.caption:
        caption += f"\n{message.caption}"
    try:
        await bot.copy_message(
            chat_id=ADMIN_ID, from_chat_id=message.chat.id, message_id=message.message_id,
            caption=caption[:1024]
        )
    except Exception:
        logging.exception("Не удалось переслать фото админу")
        await message.answer("Не получилось отправить фото 🙈 Попробуй ещё раз.")
        return
    await message.answer("📸 Фото отправлено ❤️")

# --- СВОИ НАПОМИНАНИЯ ---

OWN_REMINDERS_HELP = (
    "Можно ставить себе напоминания обычным сообщением, например:\n"
    + HELP_EXAMPLES
    + "\nСписок и удаление: /reminders"
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
        with_reminder_text(f"✅ Напомню {format_remind_at(parsed.remind_at, parsed.repeat_days)}", parsed.text),
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

@dp.callback_query()
async def stale_button(callback: types.CallbackQuery):
    """Кнопки, которых бот не знает (например, таблеточные от старой версии)."""
    await callback.answer("Эта кнопка устарела")

@dp.errors()
async def on_error(event: ErrorEvent):
    """Ошибка в обработчике не должна оставлять кнопку «висеть»."""
    logging.exception("Ошибка при обработке обновления", exc_info=event.exception)
    if event.update.callback_query:
        try:
            await event.update.callback_query.answer()
        except Exception:
            pass
    return True

# --- ПЛАНИРОВЩИК ---

async def send_own_reminders():
    """Раз в минуту отправляет наступившие напоминания пользователей."""
    due = await pool.fetch("""
        SELECT id, user_id, text, repeat_days FROM user_reminders
        WHERE remind_at <= NOW() ORDER BY remind_at
    """)
    for reminder in due:
        try:
            await bot.send_message(reminder['user_id'], with_reminder_text("⏰ Напоминаю", reminder['text']))
        except Exception:
            logging.exception("Не удалось отправить своё напоминание")

        if reminder['repeat_days']:
            # следующий повтор, даже если бот лежал несколько суток
            await pool.execute("""
                UPDATE user_reminders
                SET remind_at = remind_at + (
                    FLOOR(EXTRACT(EPOCH FROM NOW() - remind_at) / (86400 * repeat_days)) + 1
                ) * repeat_days * INTERVAL '1 day'
                WHERE id = $1
            """, reminder['id'])
        else:
            await pool.execute("DELETE FROM user_reminders WHERE id = $1", reminder['id'])

async def send_admin_notifications():
    """Раз в минуту отправляет уведомления админа, время которых наступило."""
    now = datetime.now(LOCAL_TIMEZONE)
    for notification in await get_notifications():
        if not notification_is_due(notification, now):
            continue
        # сначала отмечаем, чтобы при ошибке посередине не разослать дважды
        await pool.execute(
            "UPDATE admin_notifications SET last_sent_date = $2 WHERE id = $1",
            notification['id'], now.date()
        )
        for user_id in await get_recipients(notification['id']):
            try:
                await bot.send_message(user_id, notification['text'])
            except Exception:
                logging.exception("Не удалось отправить уведомление")

async def every_minute():
    await send_own_reminders()
    await send_admin_notifications()

# --- ЗАПУСК ---
async def main():
    await init_db() # Подключаемся к БД

    scheduler.add_job(every_minute, "cron", second=0, id="every_minute")
    scheduler.start()

    # сообщения, пришедшие во время перезапуска, не теряем
    await bot.delete_webhook(drop_pending_updates=False)
    await bot.set_my_commands([
        types.BotCommand(command="reminders", description="Мои напоминания"),
    ])

    try:
        await bot.send_message(ADMIN_ID, version_message())
    except Exception as error:
        logging.error("Не удалось отправить сообщение об обновлении: %s", error)

    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
