import asyncio
import logging
import os
import re
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import aiosqlite
import dateparser
import sqlite3

TOKEN = os.getenv("TOKEN", "9235096840:AAHcY07_0cObSeEkxTIZ7laU1xioe3OZzNA")
DB_FILE = "tasks.db"

logging.basicConfig(level=logging.INFO)

bot = Bot(token=TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler(timezone="Europe/Moscow")

# Временное хранилище для режима редактирования задачи (user_id -> task_id)
EDITING_USERS = {}

# --- Инициализация базы данных ---
async def init_db():
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                task_text TEXT,
                remind_at TIMESTAMP,
                priority TEXT DEFAULT 'low',
                status TEXT DEFAULT 'pending' 
            )
        """)
        # Безопасное добавление колонки priority для старых баз
        try:
            await db.execute("ALTER TABLE tasks ADD COLUMN priority TEXT DEFAULT 'low'")
            await db.commit()
        except sqlite3.OperationalError:
            pass
        await db.commit()

# --- Клавиатуры ---

def get_welcome_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Начать работу", callback_data="main_menu")]
    ])

def get_main_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Создать задачу (инструкция)", callback_data="menu_create_hint")],
        [InlineKeyboardButton(text="📅 Посмотреть задачи на день", callback_data="menu_view")],
        [InlineKeyboardButton(text="✏️ Редактировать задачу", callback_data="menu_edit_date")],
        [InlineKeyboardButton(text="🗑 Удалить задачу", callback_data="menu_del_date")],
        [InlineKeyboardButton(text="📊 Статистика за день", callback_data="menu_stat_date")]
    ])

def get_date_navigation_keyboard(mode: str, current_date_str: str):
    curr_date = datetime.strptime(current_date_str, "%Y-%m-%d")
    prev_date = (curr_date - timedelta(days=1)).strftime("%Y-%m-%d")
    next_date = (curr_date + timedelta(days=1)).strftime("%Y-%m-%d")
    
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="◀️ Вчера", callback_data=f"{mode}_{prev_date}"),
            InlineKeyboardButton(text="📅 Сегодня", callback_data=f"{mode}_{datetime.now().strftime('%Y-%m-%d')}"),
            InlineKeyboardButton(text="Завтра ▶️", callback_data=f"{mode}_{next_date}")
        ],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")]
    ])

def get_task_feedback_keyboard(task_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Успел", callback_data=f"done_{task_id}")],
        [InlineKeyboardButton(text="⏳ Начал, но не доделал", callback_data=f"prog_{task_id}")],
        [InlineKeyboardButton(text="❌ Не успел", callback_data=f"missed_{task_id}")]
    ])

def get_priority_icon(priority: str) -> str:
    if priority == 'high':
        return "🔴"
    elif priority == 'medium':
        return "🟡"
    return "🟢"

# --- Фоновая проверка наступления времени задач ---
async def check_reminders(bot: Bot):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
            "SELECT id, user_id, task_text, priority FROM tasks WHERE remind_at <= ? AND status = 'pending'",
            (now,)
        ) as cursor:
            rows = await cursor.fetchall()
            
            for row in rows:
                task_id, user_id, task_text, priority = row
                p_icon = get_priority_icon(priority)
                try:
                    await bot.send_message(
                        user_id,
                        f"⏰ **Время задачи!** {p_icon}\n\n«{task_text}»\n\nКак успехи?",
                        reply_markup=get_task_feedback_keyboard(task_id),
                        parse_mode="Markdown"
                    )
                    await db.execute("UPDATE tasks SET status = 'notified' WHERE id = ?", (task_id,))
                    await db.commit()
                except Exception as e:
                    logging.error(f"Не удалось отправить уведомление пользователю {user_id}: {e}")

# --- Фоновая задача: Итоги дня (каждый день в 21:00) ---
async def send_daily_summary(bot: Bot):
    today_date = datetime.now().strftime("%Y-%m-%d")
    
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
            "SELECT DISTINCT user_id FROM tasks WHERE DATE(remind_at) = ?",
            (today_date,)
        ) as cursor:
            users = await cursor.fetchall()
            
            for (user_id,) in users:
                async with db.execute(
                    "SELECT task_text, status, priority FROM tasks WHERE user_id = ? AND DATE(remind_at) = ?",
                    (user_id, today_date)
                ) as task_cursor:
                    tasks = list(await task_cursor.fetchall())
                    
                    if not tasks:
                        continue
                        
                    done_count = sum(1 for t, s, p in tasks if s == 'done')
                    prog_count = sum(1 for t, s, p in tasks if s == 'in_progress')
                    missed_count = sum(1 for t, s, p in tasks if s in ('missed', 'notified', 'pending'))
                    total = len(tasks)
                    
                    report = f"📊 **Авто-итоги дня ({today_date}):**\n\n"
                    report += f"Всего задач: {total}\n"
                    report += f"✅ Выполнено: {done_count}\n"
                    report += f"⏳ Начато, но не закончено: {prog_count}\n"
                    report += f"❌ Пропущено: {missed_count}\n\n"
                    
                    for t_text, status, priority in tasks:
                        icon = "✅" if status == 'done' else ("⏳" if status == 'in_progress' else "❌")
                        p_icon = get_priority_icon(priority)
                        report += f"{icon} {p_icon} {t_text}\n"
                        
                    try:
                        await bot.send_message(user_id, report, parse_mode="Markdown")
                    except Exception as e:
                        logging.error(f"Не удалось отправить итоги дня пользователю {user_id}: {e}")

# --- Обработка команд и меню ---

@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        "👋 Привет! Добро пожаловать в твой умный бот-планировщик.\n\n"
        "Здесь ты можешь легко управлять задачами, ставить приоритеты и отслеживать продуктивность.\n\n"
        "Нажми на кнопку ниже, чтобы открыть главное меню:",
        reply_markup=get_welcome_keyboard(),
        parse_mode="Markdown"
    )

@dp.callback_query(F.data == "main_menu")
async def callback_main_menu(callback: CallbackQuery):
    if callback.message and isinstance(callback.message, Message):
        await callback.message.answer(
            "📋 **Главное меню:**\nВыбери нужное действие:",
            reply_markup=get_main_menu(),
            parse_mode="Markdown"
        )
    await callback.answer()

@dp.callback_query(F.data == "menu_create_hint")
async def menu_create_hint(callback: CallbackQuery):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")]
    ])
    if callback.message and isinstance(callback.message, Message):
        await callback.message.answer(
            "➕ **Как создать задачу:**\n\n"
            "Просто отправь мне текст сообщения в свободной форме, указав время и (опционально) приоритет:\n\n"
            "📌 *Примеры:*\n"
            "• `Купить молоко завтра в 15:30 высокий`\n"
            "• `Прибраться в комнате сегодня в 17:00 средний`\n"
            "• `Сделать отчет через 2 часа` *(приоритет по умолчанию — низкий)*",
            reply_markup=keyboard,
            parse_mode="Markdown"
        )
    await callback.answer()

# 1. Меню: Посмотреть задачи -> Выбор даты (отправляем в чат)
@dp.callback_query(F.data == "menu_view")
async def menu_view(callback: CallbackQuery):
    today = datetime.now().strftime("%Y-%m-%d")
    if callback.message and isinstance(callback.message, Message):
        await callback.message.answer(
            f"📅 Выбери дату для просмотра задач:",
            reply_markup=get_date_navigation_keyboard("view", today)
        )
    await callback.answer()

@dp.callback_query(F.data.startswith("view_"))
async def show_tasks_for_date(callback: CallbackQuery):
    if not callback.data or not callback.message or not callback.from_user:
        return
    target_date = callback.data.split("_")[1]
    user_id = callback.from_user.id
    
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
            "SELECT task_text, remind_at, status, priority FROM tasks WHERE user_id = ? AND DATE(remind_at) = ?",
            (user_id, target_date)
        ) as cursor:
            tasks = list(await cursor.fetchall())
            
            text = f"📋 **Задачи на {target_date}:**\n\n"
            if not tasks:
                text += "На этот день задач нет. 🎉"
            else:
                for i, (t_text, r_time, status, priority) in enumerate(tasks, 1):
                    icon = "✅" if status == 'done' else ("⏳" if status == 'in_progress' else ("❌" if status == 'missed' else "📌"))
                    p_icon = get_priority_icon(priority)
                    time_only = r_time.split()[1][:5]
                    text += f"{i}. {icon} {p_icon} *{time_only}* — {t_text}\n"
                    
            if callback.message and isinstance(callback.message, Message):
                await callback.message.answer(
                    text,
                    reply_markup=get_date_navigation_keyboard("view", target_date),
                    parse_mode="Markdown"
                )
    await callback.answer()

# 2. Меню: Удалить задачу -> Выбор даты -> Список задач
@dp.callback_query(F.data == "menu_del_date")
async def menu_del_date(callback: CallbackQuery):
    today = datetime.now().strftime("%Y-%m-%d")
    if callback.message and isinstance(callback.message, Message):
        await callback.message.answer(
            "🗑 **Удаление задачи**\nВыбери дату, за которую хочешь посмотреть и удалить задачу:",
            reply_markup=get_date_navigation_keyboard("deldate", today),
            parse_mode="Markdown"
        )
    await callback.answer()

@dp.callback_query(F.data.startswith("deldate_"))
async def show_tasks_to_delete(callback: CallbackQuery):
    if not callback.data or not callback.message or not callback.from_user:
        return
    target_date = callback.data.split("_")[1]
    user_id = callback.from_user.id
    
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
            "SELECT id, task_text, remind_at, priority FROM tasks WHERE user_id = ? AND DATE(remind_at) = ? AND status IN ('pending', 'notified', 'in_progress')",
            (user_id, target_date)
        ) as cursor:
            tasks = list(await cursor.fetchall())
            
            if not tasks:
                keyboard = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="◀️ Назад к выбору дат", callback_data="menu_del_date")],
                    [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")]
                ])
                if callback.message and isinstance(callback.message, Message):
                    await callback.message.answer(f"На дату {target_date} нет активных задач для удаления.", reply_markup=keyboard)
                await callback.answer()
                return
                
            buttons = []
            for task_id, t_text, r_time, priority in tasks:
                p_icon = get_priority_icon(priority)
                time_only = r_time.split()[1][:5]
                buttons.append([InlineKeyboardButton(text=f"❌ {p_icon} {time_only} | {t_text[:20]}...", callback_data=f"deltask_{task_id}_{target_date}")])
            
            buttons.append([InlineKeyboardButton(text="◀️ К выбору другой даты", callback_data="menu_del_date")])
            
            if callback.message and isinstance(callback.message, Message):
                await callback.message.answer(
                    f"🗑 Выбери задачу для удаления на **{target_date}**:",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
                    parse_mode="Markdown"
                )
    await callback.answer()

@dp.callback_query(F.data.startswith("deltask_"))
async def execute_task_deletion(callback: CallbackQuery):
    if not callback.data or not callback.message:
        return
    parts = callback.data.split("_")
    task_id = int(parts[1])
    target_date = parts[2]
    
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        await db.commit()
        
    # Отправляем сообщение в чат, что задача удалена, и возвращаем главное меню
    await callback.message.answer("🗑 Задача успешно удалена!")
    await callback.message.answer(
        "📋 **Главное меню:**\nВыбери нужное действие:",
        reply_markup=get_main_menu(),
        parse_mode="Markdown"
    )
    await callback.answer()

# 3. Меню: Редактировать задачу -> Выбор даты -> Список задач -> Ввод нового текста
@dp.callback_query(F.data == "menu_edit_date")
async def menu_edit_date(callback: CallbackQuery):
    today = datetime.now().strftime("%Y-%m-%d")
    if callback.message and isinstance(callback.message, Message):
        await callback.message.answer(
            "✏️ **Редактирование задачи**\nВыбери дату, за которую хочешь изменить задачу:",
            reply_markup=get_date_navigation_keyboard("editdate", today),
            parse_mode="Markdown"
        )
    await callback.answer()

@dp.callback_query(F.data.startswith("editdate_"))
async def show_tasks_to_edit(callback: CallbackQuery):
    if not callback.data or not callback.message or not callback.from_user:
        return
    target_date = callback.data.split("_")[1]
    user_id = callback.from_user.id
    
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
            "SELECT id, task_text, remind_at, priority FROM tasks WHERE user_id = ? AND DATE(remind_at) = ? AND status IN ('pending', 'notified', 'in_progress')",
            (user_id, target_date)
        ) as cursor:
            tasks = list(await cursor.fetchall())
            
            if not tasks:
                keyboard = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="◀️ Назад к выбору дат", callback_data="menu_edit_date")],
                    [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")]
                ])
                if callback.message and isinstance(callback.message, Message):
                    await callback.message.answer(f"На дату {target_date} нет активных задач для редактирования.", reply_markup=keyboard)
                await callback.answer()
                return
                
            buttons = []
            for task_id, t_text, r_time, priority in tasks:
                p_icon = get_priority_icon(priority)
                time_only = r_time.split()[1][:5]
                buttons.append([InlineKeyboardButton(text=f"✏️ {p_icon} {time_only} | {t_text[:20]}...", callback_data=f"edittask_{task_id}")])
            
            buttons.append([InlineKeyboardButton(text="◀️ К выбору другой даты", callback_data="menu_edit_date")])
            
            if callback.message and isinstance(callback.message, Message):
                await callback.message.answer(
                    f"✏️ Выбери задачу для редактирования на **{target_date}**:",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
                    parse_mode="Markdown"
                )
    await callback.answer()

@dp.callback_query(F.data.startswith("edittask_"))
async def start_task_editing(callback: CallbackQuery):
    if not callback.data or not callback.message or not callback.from_user:
        return
    task_id = int(callback.data.split("_")[1])
    user_id = callback.from_user.id
    
    # Сохраняем в память, какую задачу пользователь сейчас редактирует
    EDITING_USERS[user_id] = task_id
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏠 Отмена / Главное меню", callback_data="main_menu")]
    ])
    
    await callback.message.answer(
        "✍️ **Введите новый текст и время задачи**\n"
        "*(например: Купить продукты завтра в 18:00 высокий)*",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )
    await callback.answer()

# 4. Меню: Статистика -> Выбор даты -> Отчет
@dp.callback_query(F.data == "menu_stat_date")
async def menu_stat_date(callback: CallbackQuery):
    today = datetime.now().strftime("%Y-%m-%d")
    if callback.message and isinstance(callback.message, Message):
        await callback.message.answer(
            "📊 **Статистика**\nВыбери дату для просмотра отчета:",
            reply_markup=get_date_navigation_keyboard("stat", today),
            parse_mode="Markdown"
        )
    await callback.answer()

@dp.callback_query(F.data.startswith("stat_"))
async def show_statistics_for_date(callback: CallbackQuery):
    if not callback.data or not callback.message or not callback.from_user:
        return
    target_date = callback.data.split("_")[1]
    user_id = callback.from_user.id
    
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
            "SELECT task_text, status, priority FROM tasks WHERE user_id = ? AND DATE(remind_at) = ?",
            (user_id, target_date)
        ) as cursor:
            tasks = list(await cursor.fetchall())
            
            report = f"📊 **Статистика за {target_date}:**\n\n"
            if not tasks:
                report += "Задач на эту дату не зафиксировано."
            else:
                done_count = sum(1 for t, s, p in tasks if s == 'done')
                prog_count = sum(1 for t, s, p in tasks if s == 'in_progress')
                missed_count = sum(1 for t, s, p in tasks if s in ('missed', 'notified', 'pending'))
                total = len(tasks)
                
                report += f"Всего задач: {total}\n"
                report += f"✅ Выполнено: {done_count}\n"
                report += f"⏳ Начато, но не закончено: {prog_count}\n"
                report += f"❌ Не выполнено / пропущено: {missed_count}\n\n"
                report += "Детализация:\n"
                
                for t_text, status, priority in tasks:
                    icon = "✅" if status == 'done' else ("⏳" if status == 'in_progress' else "❌")
                    p_icon = get_priority_icon(priority)
                    report += f"{icon} {p_icon} {t_text}\n"
                    
            if callback.message and isinstance(callback.message, Message):
                await callback.message.answer(
                    report,
                    reply_markup=get_date_navigation_keyboard("stat", target_date),
                    parse_mode="Markdown"
                )
    await callback.answer()

# 5. Обработка кнопок из пуш-уведомлений
@dp.callback_query(F.data.startswith("done_") | F.data.startswith("prog_") | F.data.startswith("missed_"))
async def process_task_feedback(callback: CallbackQuery):
    if not callback.data:
        await callback.answer()
        return
        
    parts = callback.data.split("_")
    action = parts[0]
    task_id = int(parts[1])
    
    if action == "done":
        new_status = "done"
        status_text = "✅ Отлично! Задача отмечена как выполненная."
    elif action == "prog":
        new_status = "in_progress"
        status_text = "⏳ Записал: начато, но не закончено."
    else:
        new_status = "missed"
        status_text = "❌ Понял. Задача отмечена как невыполненная."
    
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "UPDATE tasks SET status = ? WHERE id = ?", (new_status, task_id)
        )
        await db.commit()
        
    if callback.message and isinstance(callback.message, Message):
        await callback.message.answer(status_text)
    
    await callback.answer()

# --- Текстовый ввод: создание новой задачи ИЛИ сохранение отредактированной ---
@dp.message(F.text)
async def handle_text_input(message: Message):
    if not message.text or not message.from_user:
        return
        
    user_id = message.from_user.id
    raw_text = message.text.strip()
    
    # ПЕРЕХВАТЧИК РЕДАКТИРОВАНИЯ: если пользователь сейчас в режиме правки задачи
    if user_id in EDITING_USERS:
        task_id = EDITING_USERS.pop(user_id) # удаляем из словаря режима правки
        
        # Парсим новый текст/дату
        priority = "low"
        lower_text = raw_text.lower()
        if "высокий приоритет" in lower_text or "срочно" in lower_text or "высокий" in lower_text:
            priority = "high"
            for word in ["высокий приоритет", "срочно", "высокий"]:
                raw_text = raw_text.replace(word, "").strip()
        elif "средний приоритет" in lower_text or "средний" in lower_text:
            priority = "medium"
            for word in ["средний приоритет", "средний"]:
                raw_text = raw_text.replace(word, "").strip()
        elif "низкий приоритет" in lower_text or "низкий" in lower_text:
            priority = "low"
            for word in ["низкий приоритет", "низкий"]:
                raw_text = raw_text.replace(word, "").strip()

        raw_text = raw_text.rstrip(",.- ")
        
        now = datetime.now()
        target_date = None
        time_match = re.search(r'\b([0-1]?[0-9]|2[0-3])[:.]([0-5][0-9])\b', raw_text)
        
        if not time_match:
            await message.answer(
                "⚠️ Не смог найти время (например, `17:00`). Попробуйте еще раз или вернитесь в главное меню.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")]]),
                parse_mode="Markdown"
            )
            EDITING_USERS[user_id] = task_id # возвращаем обратно до успешного ввода
            return

        hour = int(time_match.group(1))
        minute = int(time_match.group(2))

        if "сегодня" in lower_text:
            target_date = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        elif "завтра" in lower_text:
            target_date = (now + timedelta(days=1)).replace(hour=hour, minute=minute, second=0, microsecond=0)
        else:
            parsed_date = dateparser.parse(raw_text, languages=['ru'], settings={'PREFER_DATES_FROM': 'future'})
            if parsed_date:
                target_date = parsed_date

        if not target_date:
            await message.answer(
                "⚠️ Не смог распознать дату (укажите 'сегодня' или 'завтра'). Попробуйте еще раз.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")]]),
                parse_mode="Markdown"
            )
            EDITING_USERS[user_id] = task_id
            return

        # Обновляем задачу в базе данных
        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute(
                "UPDATE tasks SET task_text = ?, remind_at = ?, priority = ?, status = 'pending' WHERE id = ?",
                (raw_text, target_date.strftime("%Y-%m-%d %H:%M:%S"), priority, task_id)
            )
            await db.commit()

        p_icon = get_priority_icon(priority)
        await message.answer(
            f"✏️ **Задача успешно обновлена!**\n"
            f"📝 Текст: {raw_text}\n"
            f"🏷 Приоритет: {p_icon} {priority.capitalize()}\n"
            f"📅 Новое время: {target_date.strftime('%Y-%m-%d %H:%M')}",
            reply_markup=get_main_menu(),
            parse_mode="Markdown"
        )
        return

    # Обычное создание новой задачи
    priority = "low"
    lower_text = raw_text.lower()
    
    if "высокий приоритет" in lower_text or "срочно" in lower_text or "высокий" in lower_text:
        priority = "high"
        for word in ["высокий приоритет", "срочно", "высокий"]:
            raw_text = raw_text.replace(word, "").strip()
    elif "средний приоритет" in lower_text or "средний" in lower_text:
        priority = "medium"
        for word in ["средний приоритет", "средний"]:
            raw_text = raw_text.replace(word, "").strip()
    elif "низкий приоритет" in lower_text or "низкий" in lower_text:
        priority = "low"
        for word in ["низкий приоритет", "низкий"]:
            raw_text = raw_text.replace(word, "").strip()

    raw_text = raw_text.rstrip(",.- ")

    now = datetime.now()
    target_date = None
    
    time_match = re.search(r'\b([0-1]?[0-9]|2[0-3])[:.]([0-5][0-9])\b', raw_text)
    
    if not time_match:
        await message.answer(
            "⚠️ Не смог найти время (например, `17:00`) в твоем сообщении.\n"
            "Попробуй написать: *Прибраться в комнате сегодня в 17:00*",
            reply_markup=get_main_menu(),
            parse_mode="Markdown"
        )
        return

    hour = int(time_match.group(1))
    minute = int(time_match.group(2))

    if "сегодня" in lower_text:
        target_date = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    elif "завтра" in lower_text:
        target_date = (now + timedelta(days=1)).replace(hour=hour, minute=minute, second=0, microsecond=0)
    else:
        parsed_date = dateparser.parse(raw_text, languages=['ru'], settings={'PREFER_DATES_FROM': 'future'})
        if parsed_date:
            target_date = parsed_date

    if not target_date:
        await message.answer(
            "⚠️ Не смог распознать дату (укажи 'сегодня' или 'завтра').\n"
            "Пример: *Прибраться в комнате сегодня в 17:00*",
            reply_markup=get_main_menu(),
            parse_mode="Markdown"
        )
        return
    
    task_text = raw_text
    
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "INSERT INTO tasks (user_id, task_text, remind_at, priority, status) VALUES (?, ?, ?, ?, 'pending')",
            (user_id, task_text, target_date.strftime("%Y-%m-%d %H:%M:%S"), priority)
        )
        await db.commit()
        
    p_icon = get_priority_icon(priority)
    await message.answer(
        f"✅ **Задача принята!**\n"
        f"📝 Текст: {task_text}\n"
        f"🏷 Приоритет: {p_icon} {priority.capitalize()}\n"
        f"📅 Напомню: {target_date.strftime('%Y-%m-%d %H:%M')}",
        reply_markup=get_main_menu(),
        parse_mode="Markdown"
    )

async def main():
    await init_db()
    scheduler.add_job(check_reminders, "interval", seconds=60, args=[bot])
    scheduler.add_job(send_daily_summary, "cron", hour=21, minute=0, args=[bot])
    scheduler.start()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())