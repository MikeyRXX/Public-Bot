#!/usr/bin/env python
"""
Телеграм-бот администратор для группы.
Объединенная версия всех модулей в одном файле для большей стабильности.
"""

import os
import time
import signal
import logging
import threading
import datetime
import sys
import re
from typing import Dict, List, Tuple, Optional, Any, Union
from collections import defaultdict
import json

# Настраиваем логирование
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.FileHandler("bot_output.log"),
        logging.StreamHandler()
    ]
)

logger = logging.getLogger(__name__)

# ---------------------- КОНФИГУРАЦИЯ БОТА ---------------------- #

# Основные настройки
TOKEN = os.getenv("7235545682:AAGys_-0I_A9L7yflXd9Ft6S0pjCH1cllfI")  # Токен берется из переменной окружения
OWNER_IDS = [7336080619, 1163080940]     # ID владельцев бота
GROUP_ID = int(os.getenv("GROUP_ID", "-1002539213476"))  # ID группы

# Константы для мониторинга состояния бота
HEALTH_CHECK_FILE = "bot_health.txt"
HEALTH_CHECK_INTERVAL = 30  # Интервал обновления файла состояния в секундах

# Настройки модерации
MAX_WARNINGS = 3        # Максимальное количество предупреждений до бана
MUTE_TIME = 60 * 60 * 24  # Время мута по умолчанию (24 часа)
FLOOD_THRESHOLD = 5     # Количество сообщений для обнаружения флуда
FLOOD_TIME = 5          # Временное окно для обнаружения флуда (секунды)
FLOOD_MUTE_TIME = 60 * 15  # Время мута за флуд (15 минут)

# ---------------------- МОДЕЛИ ДАННЫХ ---------------------- #

# База данных
import sqlite3
from contextlib import contextmanager

# Создаем соединение с базой данных
DB_PATH = "bot.db"

@contextmanager
def get_db_connection():
    """Контекстный менеджер для соединения с базой данных"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()

def init_db():
    """Инициализация базы данных"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        
        # Создаем таблицу настроек группы
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS group_settings (
            id INTEGER PRIMARY KEY,
            group_id TEXT UNIQUE,
            welcome_message TEXT DEFAULT 'Добро пожаловать, {name}!',
            rules TEXT DEFAULT 'Правила не установлены!',
            anti_flood BOOLEAN DEFAULT 1,
            smart_warnings BOOLEAN DEFAULT 0,
            smart_warnings_auto BOOLEAN DEFAULT 0,
            smart_warnings_min_confidence REAL DEFAULT 0.8,
            smart_warnings_enabled_types TEXT DEFAULT 'spam,obscenity,rudeness,flood',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        ''')
        
        # Создаем таблицу предупреждений пользователей
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_warnings (
            id INTEGER PRIMARY KEY,
            group_id TEXT,
            user_id TEXT,
            warnings INTEGER DEFAULT 0,
            reason TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        ''')
        
        # Создаем таблицу информации о пользователях
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_info (
            id INTEGER PRIMARY KEY,
            user_id TEXT UNIQUE,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            join_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            is_admin BOOLEAN DEFAULT 0
        )
        ''')
        
        # Создаем таблицу анализа сообщений
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS message_analysis (
            id INTEGER PRIMARY KEY,
            group_id TEXT,
            user_id TEXT,
            message_id TEXT,
            message_text TEXT,
            has_violation BOOLEAN DEFAULT 0,
            violation_types TEXT,
            confidence REAL,
            suggested_warning TEXT,
            is_warned BOOLEAN DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        ''')
        
        conn.commit()

# ---------------------- УТИЛИТЫ ---------------------- #

# Трекер сообщений для обнаружения флуда и анализа
class MessageTracker:
    def __init__(self, message_history_size=100, flood_window_seconds=60):
        """Инициализация трекера сообщений"""
        self.message_history = defaultdict(list)  # user_id -> [сообщения]
        self.max_history_size = message_history_size
        self.flood_window = flood_window_seconds
        logger.info(f"MessageTracker инициализирован (история: {message_history_size}, окно флуда: {flood_window_seconds}s)")
    
    def add_message(self, user_id, chat_id, message_text):
        """Добавление сообщения и проверка на флуд"""
        # Создаем запись о сообщении
        message = {
            'text': message_text,
            'timestamp': time.time(),
            'chat_id': chat_id
        }
        
        # Добавляем в историю
        if user_id not in self.message_history:
            self.message_history[user_id] = []
        
        self.message_history[user_id].append(message)
        
        # Ограничиваем размер истории
        if len(self.message_history[user_id]) > self.max_history_size:
            self.message_history[user_id] = self.message_history[user_id][-self.max_history_size:]
        
        # Считаем количество сообщений в окне флуда
        flood_count = 0
        current_time = time.time()
        flood_window_start = current_time - self.flood_window
        
        for msg in reversed(self.message_history[user_id]):
            if msg['timestamp'] >= flood_window_start and msg['chat_id'] == chat_id:
                flood_count += 1
        
        return flood_count
    
    def get_user_messages(self, user_id, limit=None, chat_id=None, seconds=None):
        """Получение истории сообщений пользователя с фильтрацией"""
        if user_id not in self.message_history:
            return []
        
        messages = self.message_history[user_id]
        current_time = time.time()
        
        # Применяем фильтры
        if chat_id:
            messages = [msg for msg in messages if msg['chat_id'] == chat_id]
        
        if seconds:
            cutoff_time = current_time - seconds
            messages = [msg for msg in messages if msg['timestamp'] >= cutoff_time]
        
        # Ограничиваем количество сообщений
        if limit and limit > 0:
            messages = messages[-limit:]
        
        return messages
    
    def get_message_frequency(self, user_id, chat_id, seconds=None):
        """Вычисление частоты сообщений (сообщений в минуту)"""
        if seconds is None:
            seconds = self.flood_window
        
        messages = self.get_user_messages(user_id, chat_id=chat_id, seconds=seconds)
        
        if not messages:
            return 0.0
        
        # Вычисляем частоту
        oldest_time = min(msg['timestamp'] for msg in messages)
        newest_time = max(msg['timestamp'] for msg in messages)
        time_span = max(1, newest_time - oldest_time)  # Минимум 1 секунда для избежания деления на 0
        
        # Возвращаем сообщений в минуту
        return (len(messages) / time_span) * 60

# Анализатор сообщений для умных предупреждений
class WarningAnalyzer:
    def __init__(self):
        """Инициализация анализатора предупреждений"""
        # Заготовки шаблонов нарушений для демонстрации
        self.violation_patterns = {
            'spam': [
                r'купи[те]?|продам|реклама|акция|скидк[аи]|sale|https?://|www\.',
                r'зарабо[тт]ок|earn|money|quick cash|быстр[ыо].{1,5}деньги',
                r'(join|вступ[аи][йт][те]).{1,10}(channel|канал)',
                r'под[пз]ис[шщ][ие][тс][еь]с[ья]'
            ],
            'obscenity': [
                r'\b[хx][уy][йиеёeblit][л(нн)яebl]|\b[бb][лl][яeaиiu][тdt][ьb]|\b[еe][бb][лl][яеeаa]|\b[пn][иie][зz3][дd]',
                r'\b[сc][уy][кk][аa]|\b[мm][уy][дd][аa][кk]|\b[дd][уy][рp][аa][кk]|\bлох\b'
            ],
            'rudeness': [
                r'\b[гg][аоoa][вb][нnh][оoаa]|\b[дd][еe][рp][ьb][мm][оo]|\b[уy][рp][оo][дd]',
                r'заткнись|fool|stupid|идиот|дебил|тупо[йрг]|кретин'
            ],
            'flood': [
                r'(.)\\1{8,}',  # Повторение одного символа 8+ раз
                r'(.{1,5})\\1{5,}'  # Повторение группы символов 5+ раз
            ]
        }
        logger.info(f"WarningAnalyzer инициализирован")
    
    def analyze_message(self, message_text, context=None):
        """Анализ сообщения на наличие нарушений"""
        if not message_text:
            return {
                'has_violation': False,
                'violations': [],
                'confidence': 0.0,
                'suggested_warning': None
            }
        
        # Нормализация текста
        text = message_text.lower()
        violations = []
        
        # Проверяем каждый тип нарушения
        for violation_type, patterns in self.violation_patterns.items():
            for pattern in patterns:
                if re.search(pattern, text, re.IGNORECASE):
                    violations.append(violation_type)
                    break  # Если нашли хотя бы один паттерн, переходим к следующему типу
        
        # Учитываем контекст для обнаружения флуда
        if context and 'frequency' in context:
            frequency = context['frequency']
            if frequency > 10:  # Больше 10 сообщений в минуту
                violations.append('flood')
        
        if context and 'similar_messages' in context:
            similar_count = context['similar_messages']
            if similar_count >= 3:  # 3+ похожих сообщения подряд
                violations.append('flood')
        
        # Формируем результат
        has_violation = len(violations) > 0
        
        # Вычисляем уверенность (простой алгоритм)
        confidence = min(0.95, 0.5 + (len(violations) * 0.15))
        
        # Формируем предупреждение
        suggested_warning = None
        if has_violation:
            if 'spam' in violations:
                suggested_warning = "Спам запрещен в группе!"
            elif 'obscenity' in violations:
                suggested_warning = "Использование нецензурной лексики запрещено!"
            elif 'rudeness' in violations:
                suggested_warning = "Пожалуйста, общайтесь вежливо и уважительно!"
            elif 'flood' in violations:
                suggested_warning = "Пожалуйста, не флудите в группе!"
            else:
                suggested_warning = "Нарушение правил группы!"
        
        return {
            'has_violation': has_violation,
            'violations': violations,
            'confidence': confidence,
            'suggested_warning': suggested_warning
        }

# Глобальные экземпляры классов
message_tracker = MessageTracker()
warning_analyzer = WarningAnalyzer()

# ---------------------- УТИЛИТАРНЫЕ ФУНКЦИИ ---------------------- #

def is_owner(user_id):
    """Проверка, является ли пользователь владельцем бота"""
    return user_id in OWNER_IDS

def is_admin(user_id, chat_id=None):
    """Проверка, является ли пользователь администратором"""
    # Сначала проверяем, является ли пользователь владельцем
    if is_owner(user_id):
        return True
    
    # Проверяем запись в базе данных
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT is_admin FROM user_info WHERE user_id = ?", (str(user_id),))
        row = cursor.fetchone()
        
        if row and row['is_admin']:
            return True
    
    return False

def get_group_settings(group_id):
    """Получение настроек группы"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM group_settings WHERE group_id = ?",
            (str(group_id),)
        )
        row = cursor.fetchone()
        
        if not row:
            # Создаем настройки по умолчанию
            cursor.execute(
                "INSERT INTO group_settings (group_id) VALUES (?)",
                (str(group_id),)
            )
            conn.commit()
            
            cursor.execute(
                "SELECT * FROM group_settings WHERE group_id = ?",
                (str(group_id),)
            )
            row = cursor.fetchone()
        
        return dict(row) if row else None

def get_user_warnings(group_id, user_id):
    """Получение предупреждений пользователя"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM user_warnings WHERE group_id = ? AND user_id = ?",
            (str(group_id), str(user_id))
        )
        row = cursor.fetchone()
        
        if not row:
            # Создаем запись с нулевыми предупреждениями
            cursor.execute(
                "INSERT INTO user_warnings (group_id, user_id, warnings) VALUES (?, ?, 0)",
                (str(group_id), str(user_id))
            )
            conn.commit()
            
            cursor.execute(
                "SELECT * FROM user_warnings WHERE group_id = ? AND user_id = ?",
                (str(group_id), str(user_id))
            )
            row = cursor.fetchone()
        
        return dict(row) if row else None

def update_user_info(user_id, username=None, first_name=None, last_name=None, is_admin=False):
    """Обновление информации о пользователе"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        
        # Проверяем, существует ли пользователь
        cursor.execute("SELECT * FROM user_info WHERE user_id = ?", (str(user_id),))
        user = cursor.fetchone()
        
        if user:
            # Обновляем существующую запись
            query = "UPDATE user_info SET "
            params = []
            
            if username is not None:
                query += "username = ?, "
                params.append(username)
            if first_name is not None:
                query += "first_name = ?, "
                params.append(first_name)
            if last_name is not None:
                query += "last_name = ?, "
                params.append(last_name)
            
            # Обновляем статус администратора только для неосновных владельцев
            if not is_owner(int(user_id)):
                query += "is_admin = ?, "
                params.append(1 if is_admin else 0)
            
            # Удаляем последнюю запятую и пробел
            query = query.rstrip(", ")
            
            query += " WHERE user_id = ?"
            params.append(str(user_id))
            
            if len(params) > 1:  # Есть что обновлять
                cursor.execute(query, params)
        else:
            # Создаем нового пользователя
            is_owner_flag = 1 if is_owner(int(user_id)) else 0
            is_admin_flag = 1 if is_admin or is_owner_flag else 0
            
            cursor.execute(
                "INSERT INTO user_info (user_id, username, first_name, last_name, is_admin) VALUES (?, ?, ?, ?, ?)",
                (str(user_id), username, first_name, last_name, is_admin_flag)
            )
        
        conn.commit()

def check_flood(user_id, chat_id, message_text):
    """Проверка на флуд"""
    # Получаем настройки группы
    settings = get_group_settings(chat_id)
    
    if not settings or not settings['anti_flood']:
        return False  # Антифлуд отключен
    
    # Добавляем сообщение в трекер и получаем количество сообщений в окне флуда
    flood_count = message_tracker.add_message(user_id, chat_id, message_text)
    
    # Проверка на флуд
    if flood_count > FLOOD_THRESHOLD:
        return True
    
    return False

def is_smart_warnings_enabled(group_id):
    """Проверка, включены ли умные предупреждения"""
    settings = get_group_settings(group_id)
    return settings and settings['smart_warnings']

def is_auto_warnings_enabled(group_id):
    """Проверка, включены ли автоматические предупреждения"""
    settings = get_group_settings(group_id)
    return settings and settings['smart_warnings'] and settings['smart_warnings_auto']

def get_enabled_violation_types(group_id):
    """Получение списка включенных типов нарушений"""
    settings = get_group_settings(group_id)
    if not settings or not settings['smart_warnings_enabled_types']:
        return []
    
    return settings['smart_warnings_enabled_types'].split(',')

def get_min_confidence(group_id):
    """Получение минимальной уверенности для автопредупреждений"""
    settings = get_group_settings(group_id)
    if not settings:
        return 0.8
    
    return float(settings['smart_warnings_min_confidence'])

def toggle_smart_warnings(group_id, enabled=False):
    """Включение/выключение умных предупреждений"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE group_settings SET smart_warnings = ? WHERE group_id = ?",
            (1 if enabled else 0, str(group_id))
        )
        conn.commit()
        return cursor.rowcount > 0

def toggle_auto_warnings(group_id, enabled=False):
    """Включение/выключение автоматических предупреждений"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE group_settings SET smart_warnings_auto = ? WHERE group_id = ?",
            (1 if enabled else 0, str(group_id))
        )
        conn.commit()
        return cursor.rowcount > 0

def set_enabled_violation_types(group_id, types):
    """Установка включенных типов нарушений"""
    types_str = ','.join(types)
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE group_settings SET smart_warnings_enabled_types = ? WHERE group_id = ?",
            (types_str, str(group_id))
        )
        conn.commit()
        return cursor.rowcount > 0

def set_min_confidence(group_id, confidence):
    """Установка минимальной уверенности для автопредупреждений"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE group_settings SET smart_warnings_min_confidence = ? WHERE group_id = ?",
            (float(confidence), str(group_id))
        )
        conn.commit()
        return cursor.rowcount > 0

def record_analysis(group_id, user_id, message_id, message_text, analysis_result, is_warned=False):
    """Запись результата анализа сообщения"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        
        violation_types = ','.join(analysis_result['violations']) if analysis_result['violations'] else ''
        
        cursor.execute(
            """
            INSERT INTO message_analysis 
            (group_id, user_id, message_id, message_text, has_violation, 
             violation_types, confidence, suggested_warning, is_warned)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(group_id),
                str(user_id),
                str(message_id),
                message_text,
                1 if analysis_result['has_violation'] else 0,
                violation_types,
                analysis_result['confidence'],
                analysis_result['suggested_warning'],
                1 if is_warned else 0
            )
        )
        
        conn.commit()
        return cursor.lastrowid

def get_last_analysis(group_id, limit=10):
    """Получение последних результатов анализа"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM message_analysis WHERE group_id = ? ORDER BY created_at DESC LIMIT ?",
            (str(group_id), limit)
        )
        rows = cursor.fetchall()
        return [dict(row) for row in rows]

def update_health_check():
    """Обновление файла проверки состояния"""
    while True:
        try:
            with open(HEALTH_CHECK_FILE, 'w') as f:
                f.write(f"Bot is running: {datetime.datetime.now()}")
            logger.debug("Обновлен файл состояния бота")
        except Exception as e:
            logger.error(f"Ошибка при обновлении файла состояния: {e}")
        
        time.sleep(HEALTH_CHECK_INTERVAL)

# ---------------------- TELEGRAM BOT ---------------------- #

from telegram import Update, ChatPermissions, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes
)
from telegram.constants import ParseMode

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик ошибок"""
    logger.error(f"Update {update} caused error {context.error}")

# ------ Основные команды ------ #

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start"""
    user = update.effective_user
    chat_id = update.effective_chat.id
    
    # Обновляем информацию о пользователе
    update_user_info(
        user.id, 
        username=user.username,
        first_name=user.first_name,
        last_name=user.last_name
    )
    
    # Разные приветствия для личных сообщений и группы
    if chat_id == GROUP_ID:
        await update.message.reply_text(
            f"Привет, {user.first_name}! Я бот-администратор этой группы. "
            "Используйте /help для просмотра доступных команд."
        )
    else:
        await update.message.reply_text(
            f"Привет, {user.first_name}! Я бот-администратор. "
            "Добавьте меня в группу и сделайте администратором, чтобы я мог помогать с управлением.\n\n"
            "Используйте /help для просмотра доступных команд."
        )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /help"""
    user = update.effective_user
    
    # Полная справка
    help_text = f"""
*Команды администрирования:*
/ban - Забанить пользователя (ответьте на сообщение)
/unban - Разбанить пользователя (ответьте или укажите ID)
/kick - Выгнать пользователя из группы
/mute - Заглушить пользователя (можно указать время)
/unmute - Снять ограничения на отправку сообщений

*Система предупреждений:*
/warn - Выдать предупреждение пользователю
/unwarn - Снять предупреждение
/warnings - Показать все предупреждения пользователя
/clearwarnings - Очистить все предупреждения

*Настройки группы:*
/setrules - Установить правила группы
/rules - Показать правила группы
/setwelcome - Установить приветственное сообщение
/toggleflood - Включить/выключить антифлуд

*Умные предупреждения:*
/smartwarnings - Управление системой умных предупреждений
/analyze - Анализировать сообщение на нарушения
/analyses - Показать последние результаты анализа

*Профиль и информация:*
/id - Показать ID пользователя или группы
/info - Информация о пользователе
/profile - Быстрый профиль пользователя

Подробную информацию о каждой команде можно получить, используя: 
/help [команда], например /help warn
    """
    
    # Если указан аргумент, показываем детальную справку по команде
    if context.args and len(context.args) > 0:
        command = context.args[0].lower().strip('/')
        
        if command == 'ban':
            help_text = """*Команда /ban*
Забанить пользователя в группе.
*Использование:* ответьте на сообщение пользователя командой /ban [причина]
*Пример:* /ban нарушение правил
"""
        elif command == 'warn':
            help_text = """*Команда /warn*
Выдать предупреждение пользователю.
*Использование:* ответьте на сообщение пользователя командой /warn [причина]
*Пример:* /warn спам
При достижении 3 предупреждений пользователь будет забанен автоматически.
"""
        elif command == 'smartwarnings':
            help_text = """*Система умных предупреждений*
Автоматически анализирует сообщения и предлагает модераторам выдавать предупреждения.
*Команды:*
/smartwarnings on - Включить систему
/smartwarnings off - Выключить систему
/smartwarnings auto on - Включить автоматические предупреждения
/smartwarnings auto off - Выключить автоматические предупреждения
/smartwarnings types - Посмотреть типы нарушений
/smartwarnings set_types spam,obscenity,rudeness,flood - Установить типы нарушений
/smartwarnings confidence 0.8 - Установить минимальную уверенность (0-1)
"""
    
    await update.message.reply_text(help_text, parse_mode=ParseMode.MARKDOWN)

async def id_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать ID пользователя или чата"""
    # ID пользователя
    user_id = update.effective_user.id
    
    if update.message.reply_to_message:
        # ID пользователя, на сообщение которого ответили
        replied_user = update.message.reply_to_message.from_user
        await update.message.reply_text(
            f"ID пользователя {replied_user.first_name}: `{replied_user.id}`\n"
            f"ID чата: `{update.effective_chat.id}`",
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        # ID пользователя и чата
        await update.message.reply_text(
            f"Ваш ID: `{user_id}`\n"
            f"ID чата: `{update.effective_chat.id}`",
            parse_mode=ParseMode.MARKDOWN
        )

async def info_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получить информацию о пользователе"""
    if update.message.reply_to_message:
        user = update.message.reply_to_message.from_user
    else:
        user = update.effective_user
    
    # Получаем информацию из базы данных
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM user_info WHERE user_id = ?",
            (str(user.id),)
        )
        db_user = cursor.fetchone()
        
        # Если пользователя нет в базе, добавляем его
        if not db_user:
            update_user_info(
                user.id,
                username=user.username,
                first_name=user.first_name,
                last_name=user.last_name
            )
            cursor.execute(
                "SELECT * FROM user_info WHERE user_id = ?",
                (str(user.id),)
            )
            db_user = cursor.fetchone()
        
        # Получаем предупреждения пользователя
        cursor.execute(
            "SELECT warnings FROM user_warnings WHERE user_id = ? AND group_id = ?",
            (str(user.id), str(update.effective_chat.id))
        )
        warning_row = cursor.fetchone()
        warnings = warning_row['warnings'] if warning_row else 0
    
    # Формируем информацию
    join_date = db_user['join_date'] if db_user else "Неизвестно"
    is_admin = db_user['is_admin'] if db_user else False
    is_owner_user = is_owner(user.id)
    
    # Статус модератора
    status = "👑 Владелец бота" if is_owner_user else "⭐️ Администратор" if is_admin else "👤 Пользователь"
    
    # Собираем информацию о пользователе
    info_text = f"""
*Информация о пользователе:*
ID: `{user.id}`
Имя: {user.first_name}
"""
    
    if user.last_name:
        info_text += f"Фамилия: {user.last_name}\n"
    
    if user.username:
        info_text += f"Имя пользователя: @{user.username}\n"
    
    info_text += f"""
Статус: {status}
Предупреждения: {warnings}/{MAX_WARNINGS}
Первое появление: {join_date}
"""
    
    await update.message.reply_text(info_text, parse_mode=ParseMode.MARKDOWN)

async def profile_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать мини-профиль пользователя"""
    if update.message.reply_to_message:
        user = update.message.reply_to_message.from_user
    else:
        user = update.effective_user
    
    # Получаем данные о пользователе
    with get_db_connection() as conn:
        cursor = conn.cursor()
        
        # Предупреждения
        cursor.execute(
            "SELECT warnings FROM user_warnings WHERE user_id = ? AND group_id = ?",
            (str(user.id), str(update.effective_chat.id))
        )
        warning_row = cursor.fetchone()
        warnings = warning_row['warnings'] if warning_row else 0
        
        # Статус
        cursor.execute(
            "SELECT is_admin FROM user_info WHERE user_id = ?",
            (str(user.id),)
        )
        admin_row = cursor.fetchone()
        is_admin_user = admin_row and admin_row['is_admin']
    
    # Определяем статус
    if is_owner(user.id):
        status = "👑 Владелец"
    elif is_admin_user:
        status = "⭐️ Администратор"
    else:
        status = "👤 Пользователь"
    
    # Создаем профиль с кнопками действий
    name = f"{user.first_name} {user.last_name}" if user.last_name else user.first_name
    username = f" (@{user.username})" if user.username else ""
    
    profile_text = f"""
*Профиль пользователя*
{name}{username}
ID: `{user.id}`
Статус: {status}
Предупреждения: {warnings}/{MAX_WARNINGS}
"""
    
    # Создаем кнопки для быстрых действий (только для администраторов)
    if (is_owner(update.effective_user.id) or is_admin(update.effective_user.id)) and not is_owner(user.id):
        # Создаем клавиатуру с действиями
        keyboard = [
            [
                InlineKeyboardButton("⚠️ Предупредить", callback_data=f"profile_warn_{user.id}"),
                InlineKeyboardButton("🔇 Мут", callback_data=f"profile_mute_{user.id}")
            ],
            [
                InlineKeyboardButton("👢 Кик", callback_data=f"profile_kick_{user.id}"),
                InlineKeyboardButton("🚫 Бан", callback_data=f"profile_ban_{user.id}")
            ]
        ]
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(profile_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text(profile_text, parse_mode=ParseMode.MARKDOWN)

# ------ Команды модерации ------ #

async def ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Забанить пользователя"""
    user = update.effective_user
    chat_id = update.effective_chat.id
    
    # Проверяем, является ли пользователь администратором
    if not is_admin(user.id):
        await update.message.reply_text("Эта команда доступна только администраторам.")
        return
    
    # Проверяем, есть ли ответ на сообщение
    if not update.message.reply_to_message:
        await update.message.reply_text("Пожалуйста, ответьте на сообщение пользователя, которого хотите забанить.")
        return
    
    target_user = update.message.reply_to_message.from_user
    
    # Нельзя забанить владельца или администратора
    if is_owner(target_user.id) or is_admin(target_user.id):
        await update.message.reply_text("Невозможно забанить администратора или владельца бота.")
        return
    
    # Получаем причину бана
    reason = " ".join(context.args) if context.args else "Нарушение правил"
    
    try:
        # Баним пользователя
        await context.bot.ban_chat_member(chat_id, target_user.id)
        
        # Отправляем сообщение об успешном бане
        await update.message.reply_text(
            f"Пользователь {target_user.first_name} (ID: {target_user.id}) забанен.\n"
            f"Причина: {reason}"
        )
        
        logger.info(f"Пользователь {target_user.id} забанен пользователем {user.id}. Причина: {reason}")
    except Exception as e:
        await update.message.reply_text(f"Ошибка при бане пользователя: {e}")
        logger.error(f"Ошибка при бане пользователя {target_user.id}: {e}")

async def unban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Разбанить пользователя"""
    user = update.effective_user
    chat_id = update.effective_chat.id
    
    # Проверяем, является ли пользователь администратором
    if not is_admin(user.id):
        await update.message.reply_text("Эта команда доступна только администраторам.")
        return
    
    # Определяем ID пользователя для разбана
    target_user_id = None
    
    if update.message.reply_to_message:
        # Из ответа на сообщение
        target_user_id = update.message.reply_to_message.from_user.id
    elif context.args:
        # Из аргументов команды
        try:
            target_user_id = int(context.args[0])
        except ValueError:
            await update.message.reply_text("Пожалуйста, укажите правильный ID пользователя.")
            return
    else:
        await update.message.reply_text(
            "Пожалуйста, ответьте на сообщение пользователя или укажите ID пользователя, которого хотите разбанить."
        )
        return
    
    try:
        # Разбаниваем пользователя
        await context.bot.unban_chat_member(chat_id, target_user_id)
        
        # Отправляем сообщение об успешном разбане
        await update.message.reply_text(
            f"Пользователь (ID: {target_user_id}) разбанен и может снова присоединиться к группе."
        )
        
        logger.info(f"Пользователь {target_user_id} разбанен пользователем {user.id}")
    except Exception as e:
        await update.message.reply_text(f"Ошибка при разбане пользователя: {e}")
        logger.error(f"Ошибка при разбане пользователя {target_user_id}: {e}")

async def kick_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выгнать пользователя из группы"""
    user = update.effective_user
    chat_id = update.effective_chat.id
    
    # Проверяем, является ли пользователь администратором
    if not is_admin(user.id):
        await update.message.reply_text("Эта команда доступна только администраторам.")
        return
    
    # Проверяем, есть ли ответ на сообщение
    if not update.message.reply_to_message:
        await update.message.reply_text("Пожалуйста, ответьте на сообщение пользователя, которого хотите выгнать.")
        return
    
    target_user = update.message.reply_to_message.from_user
    
    # Нельзя выгнать владельца или администратора
    if is_owner(target_user.id) or is_admin(target_user.id):
        await update.message.reply_text("Невозможно выгнать администратора или владельца бота.")
        return
    
    # Получаем причину кика
    reason = " ".join(context.args) if context.args else "Нарушение правил"
    
    try:
        # Выгоняем пользователя (бан с последующим разбаном)
        await context.bot.ban_chat_member(chat_id, target_user.id)
        await context.bot.unban_chat_member(chat_id, target_user.id)
        
        # Отправляем сообщение об успешном кике
        await update.message.reply_text(
            f"Пользователь {target_user.first_name} (ID: {target_user.id}) выгнан из группы.\n"
            f"Причина: {reason}"
        )
        
        logger.info(f"Пользователь {target_user.id} выгнан пользователем {user.id}. Причина: {reason}")
    except Exception as e:
        await update.message.reply_text(f"Ошибка при выгоне пользователя: {e}")
        logger.error(f"Ошибка при выгоне пользователя {target_user.id}: {e}")

async def mute_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Заглушить пользователя"""
    user = update.effective_user
    chat_id = update.effective_chat.id
    
    # Проверяем, является ли пользователь администратором
    if not is_admin(user.id):
        await update.message.reply_text("Эта команда доступна только администраторам.")
        return
    
    # Проверяем, есть ли ответ на сообщение
    if not update.message.reply_to_message:
        await update.message.reply_text("Пожалуйста, ответьте на сообщение пользователя, которого хотите заглушить.")
        return
    
    target_user = update.message.reply_to_message.from_user
    
    # Нельзя заглушить владельца или администратора
    if is_owner(target_user.id) or is_admin(target_user.id):
        await update.message.reply_text("Невозможно заглушить администратора или владельца бота.")
        return
    
    # Определяем время мута
    mute_time = MUTE_TIME  # По умолчанию 24 часа
    time_desc = "24 часа"
    
    if context.args:
        try:
            # Проверяем первый аргумент на время
            time_arg = context.args[0].lower()
            
            if time_arg.endswith('m'):
                # Минуты
                minutes = int(time_arg[:-1])
                mute_time = minutes * 60
                time_desc = f"{minutes} минут"
            elif time_arg.endswith('h'):
                # Часы
                hours = int(time_arg[:-1])
                mute_time = hours * 3600
                time_desc = f"{hours} часов"
            elif time_arg.endswith('d'):
                # Дни
                days = int(time_arg[:-1])
                mute_time = days * 86400
                time_desc = f"{days} дней"
            else:
                # Секунды
                mute_time = int(time_arg)
                time_desc = f"{mute_time} секунд"
                
            # Удаляем первый аргумент, чтобы он не попал в причину
            context.args.pop(0)
            
        except (ValueError, IndexError):
            # Если ошибка парсинга, используем значение по умолчанию
            pass
    
    # Получаем причину мута
    reason = " ".join(context.args) if context.args else "Нарушение правил"
    
    try:
        # Заглушаем пользователя
        permissions = ChatPermissions(
            can_send_messages=False,
            can_send_media_messages=False,
            can_send_polls=False,
            can_send_other_messages=False,
            can_add_web_page_previews=False,
            can_change_info=False,
            can_invite_users=False,
            can_pin_messages=False
        )
        
        # Вычисляем время окончания мута
        until_date = int(time.time() + mute_time)
        
        await context.bot.restrict_chat_member(
            chat_id,
            target_user.id,
            permissions,
            until_date=until_date
        )
        
        # Отправляем сообщение об успешном муте
        await update.message.reply_text(
            f"Пользователь {target_user.first_name} (ID: {target_user.id}) заглушен на {time_desc}.\n"
            f"Причина: {reason}"
        )
        
        logger.info(f"Пользователь {target_user.id} заглушен пользователем {user.id} на {time_desc}. Причина: {reason}")
    except Exception as e:
        await update.message.reply_text(f"Ошибка при заглушении пользователя: {e}")
        logger.error(f"Ошибка при заглушении пользователя {target_user.id}: {e}")

async def unmute_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Снять ограничения с пользователя"""
    user = update.effective_user
    chat_id = update.effective_chat.id
    
    # Проверяем, является ли пользователь администратором
    if not is_admin(user.id):
        await update.message.reply_text("Эта команда доступна только администраторам.")
        return
    
    # Проверяем, есть ли ответ на сообщение
    if not update.message.reply_to_message:
        await update.message.reply_text("Пожалуйста, ответьте на сообщение пользователя, с которого хотите снять ограничения.")
        return
    
    target_user = update.message.reply_to_message.from_user
    
    try:
        # Возвращаем все права
        permissions = ChatPermissions(
            can_send_messages=True,
            can_send_media_messages=True,
            can_send_polls=True,
            can_send_other_messages=True,
            can_add_web_page_previews=True,
            can_change_info=False,
            can_invite_users=True,
            can_pin_messages=False
        )
        
        await context.bot.restrict_chat_member(
            chat_id,
            target_user.id,
            permissions
        )
        
        # Отправляем сообщение об успешном снятии ограничений
        await update.message.reply_text(
            f"С пользователя {target_user.first_name} (ID: {target_user.id}) сняты все ограничения."
        )
        
        logger.info(f"С пользователя {target_user.id} сняты ограничения пользователем {user.id}")
    except Exception as e:
        await update.message.reply_text(f"Ошибка при снятии ограничений: {e}")
        logger.error(f"Ошибка при снятии ограничений с пользователя {target_user.id}: {e}")

# ------ Система предупреждений ------ #

async def warn_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выдать предупреждение пользователю"""
    user = update.effective_user
    chat_id = update.effective_chat.id
    
    # Проверяем, является ли пользователь администратором
    if not is_admin(user.id):
        await update.message.reply_text("Эта команда доступна только администраторам.")
        return
    
    # Проверяем, есть ли ответ на сообщение
    if not update.message.reply_to_message:
        await update.message.reply_text("Пожалуйста, ответьте на сообщение пользователя, которому хотите выдать предупреждение.")
        return
    
    target_user = update.message.reply_to_message.from_user
    
    # Нельзя выдать предупреждение владельцу или администратору
    if is_owner(target_user.id) or is_admin(target_user.id):
        await update.message.reply_text("Невозможно выдать предупреждение администратору или владельцу бота.")
        return
    
    # Получаем причину предупреждения
    reason = " ".join(context.args) if context.args else "Нарушение правил"
    
    # Получаем текущие предупреждения пользователя
    warnings_data = get_user_warnings(chat_id, target_user.id)
    warnings = warnings_data['warnings']
    
    # Увеличиваем количество предупреждений
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE user_warnings SET warnings = warnings + 1, reason = ?, updated_at = CURRENT_TIMESTAMP "
            "WHERE group_id = ? AND user_id = ?",
            (reason, str(chat_id), str(target_user.id))
        )
        conn.commit()
    
    # Проверяем, достигнут ли лимит предупреждений
    warnings += 1
    
    if warnings >= MAX_WARNINGS:
        # Баним пользователя
        try:
            await context.bot.ban_chat_member(chat_id, target_user.id)
            
            # Сбрасываем счетчик предупреждений
            with get_db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "UPDATE user_warnings SET warnings = 0, updated_at = CURRENT_TIMESTAMP "
                    "WHERE group_id = ? AND user_id = ?",
                    (str(chat_id), str(target_user.id))
                )
                conn.commit()
            
            await update.message.reply_text(
                f"Пользователь {target_user.first_name} (ID: {target_user.id}) забанен после достижения {MAX_WARNINGS} предупреждений.\n"
                f"Последняя причина: {reason}"
            )
            
            logger.info(f"Пользователь {target_user.id} забанен после {MAX_WARNINGS} предупреждений")
        except Exception as e:
            await update.message.reply_text(f"Ошибка при бане пользователя: {e}")
            logger.error(f"Ошибка при бане пользователя {target_user.id} после предупреждений: {e}")
    else:
        # Отправляем сообщение о предупреждении
        await update.message.reply_text(
            f"Пользователь {target_user.first_name} (ID: {target_user.id}) получил предупреждение.\n"
            f"Всего предупреждений: {warnings}/{MAX_WARNINGS}\n"
            f"Причина: {reason}"
        )
        
        logger.info(f"Пользователь {target_user.id} получил предупреждение от {user.id}. Всего: {warnings}")

async def unwarn_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Снять предупреждение с пользователя"""
    user = update.effective_user
    chat_id = update.effective_chat.id
    
    # Проверяем, является ли пользователь администратором
    if not is_admin(user.id):
        await update.message.reply_text("Эта команда доступна только администраторам.")
        return
    
    # Проверяем, есть ли ответ на сообщение
    if not update.message.reply_to_message:
        await update.message.reply_text("Пожалуйста, ответьте на сообщение пользователя, с которого хотите снять предупреждение.")
        return
    
    target_user = update.message.reply_to_message.from_user
    
    # Получаем текущие предупреждения пользователя
    warnings_data = get_user_warnings(chat_id, target_user.id)
    warnings = warnings_data['warnings']
    
    if warnings <= 0:
        await update.message.reply_text(f"У пользователя {target_user.first_name} нет предупреждений.")
        return
    
    # Уменьшаем количество предупреждений
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE user_warnings SET warnings = warnings - 1, updated_at = CURRENT_TIMESTAMP "
            "WHERE group_id = ? AND user_id = ?",
            (str(chat_id), str(target_user.id))
        )
        conn.commit()
    
    # Отправляем сообщение
    await update.message.reply_text(
        f"С пользователя {target_user.first_name} (ID: {target_user.id}) снято одно предупреждение.\n"
        f"Всего предупреждений: {warnings - 1}/{MAX_WARNINGS}"
    )
    
    logger.info(f"С пользователя {target_user.id} снято предупреждение пользователем {user.id}")

async def warnings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать предупреждения пользователя"""
    chat_id = update.effective_chat.id
    
    # Определяем целевого пользователя
    if update.message.reply_to_message:
        target_user = update.message.reply_to_message.from_user
    else:
        target_user = update.effective_user
    
    # Получаем предупреждения пользователя
    warnings_data = get_user_warnings(chat_id, target_user.id)
    warnings = warnings_data['warnings']
    reason = warnings_data['reason'] if warnings_data['reason'] else "Причина не указана"
    
    # Отправляем сообщение с предупреждениями
    await update.message.reply_text(
        f"Предупреждения пользователя {target_user.first_name} (ID: {target_user.id}):\n"
        f"Количество: {warnings}/{MAX_WARNINGS}\n"
        f"Последняя причина: {reason}"
    )

async def clear_warnings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Очистить все предупреждения пользователя"""
    user = update.effective_user
    chat_id = update.effective_chat.id
    
    # Проверяем, является ли пользователь администратором
    if not is_admin(user.id):
        await update.message.reply_text("Эта команда доступна только администраторам.")
        return
    
    # Проверяем, есть ли ответ на сообщение
    if not update.message.reply_to_message:
        await update.message.reply_text("Пожалуйста, ответьте на сообщение пользователя, у которого хотите очистить предупреждения.")
        return
    
    target_user = update.message.reply_to_message.from_user
    
    # Сбрасываем предупреждения
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE user_warnings SET warnings = 0, updated_at = CURRENT_TIMESTAMP "
            "WHERE group_id = ? AND user_id = ?",
            (str(chat_id), str(target_user.id))
        )
        conn.commit()
    
    # Отправляем сообщение
    await update.message.reply_text(
        f"Все предупреждения пользователя {target_user.first_name} (ID: {target_user.id}) очищены."
    )
    
    logger.info(f"Предупреждения пользователя {target_user.id} очищены пользователем {user.id}")

# ------ Настройки группы ------ #

async def set_rules_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Установить правила группы"""
    user = update.effective_user
    chat_id = update.effective_chat.id
    
    # Проверяем, является ли пользователь администратором
    if not is_admin(user.id):
        await update.message.reply_text("Эта команда доступна только администраторам.")
        return
    
    # Получаем текст правил
    if not context.args:
        await update.message.reply_text(
            "Пожалуйста, укажите текст правил после команды.\n"
            "Пример: /setrules Правила нашей группы: 1. Будьте вежливы. 2. Не спамьте."
        )
        return
    
    rules_text = " ".join(context.args)
    
    # Обновляем правила в базе данных
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE group_settings SET rules = ?, updated_at = CURRENT_TIMESTAMP "
            "WHERE group_id = ?",
            (rules_text, str(chat_id))
        )
        conn.commit()
    
    # Отправляем сообщение об успешном обновлении
    await update.message.reply_text(
        "Правила группы обновлены успешно!\n"
        "Участники могут просмотреть их с помощью команды /rules"
    )
    
    logger.info(f"Правила группы {chat_id} обновлены пользователем {user.id}")

async def rules_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать правила группы"""
    chat_id = update.effective_chat.id
    
    # Получаем правила из базы данных
    settings = get_group_settings(chat_id)
    rules = settings['rules'] if settings else "Правила не установлены!"
    
    # Отправляем правила
    await update.message.reply_text(
        f"*Правила группы:*\n\n{rules}",
        parse_mode=ParseMode.MARKDOWN
    )

async def set_welcome_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Установить приветственное сообщение"""
    user = update.effective_user
    chat_id = update.effective_chat.id
    
    # Проверяем, является ли пользователь администратором
    if not is_admin(user.id):
        await update.message.reply_text("Эта команда доступна только администраторам.")
        return
    
    # Получаем текст приветствия
    if not context.args:
        await update.message.reply_text(
            "Пожалуйста, укажите текст приветствия после команды.\n"
            "Используйте {name} для имени пользователя.\n"
            "Пример: /setwelcome Добро пожаловать, {name}! Ознакомьтесь с правилами группы."
        )
        return
    
    welcome_text = " ".join(context.args)
    
    # Обновляем приветствие в базе данных
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE group_settings SET welcome_message = ?, updated_at = CURRENT_TIMESTAMP "
            "WHERE group_id = ?",
            (welcome_text, str(chat_id))
        )
        conn.commit()
    
    # Отправляем сообщение об успешном обновлении
    await update.message.reply_text("Приветственное сообщение обновлено успешно!")
    
    logger.info(f"Приветственное сообщение группы {chat_id} обновлено пользователем {user.id}")

async def toggle_flood_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Включить или выключить антифлуд"""
    user = update.effective_user
    chat_id = update.effective_chat.id
    
    # Проверяем, является ли пользователь администратором
    if not is_admin(user.id):
        await update.message.reply_text("Эта команда доступна только администраторам.")
        return
    
    # Получаем текущие настройки
    settings = get_group_settings(chat_id)
    current_state = settings['anti_flood'] if settings else True
    
    # Переключаем состояние
    new_state = not current_state
    
    # Обновляем настройки в базе данных
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE group_settings SET anti_flood = ?, updated_at = CURRENT_TIMESTAMP "
            "WHERE group_id = ?",
            (1 if new_state else 0, str(chat_id))
        )
        conn.commit()
    
    # Отправляем сообщение о новом состоянии
    state_text = "включена" if new_state else "выключена"
    await update.message.reply_text(f"Защита от флуда {state_text}!")
    
    logger.info(f"Защита от флуда в группе {chat_id} {state_text} пользователем {user.id}")

# ------ Умные предупреждения ------ #

async def smart_warnings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Управление умными предупреждениями"""
    user = update.effective_user
    chat_id = update.effective_chat.id
    
    # Проверяем, является ли пользователь администратором
    if not is_admin(user.id):
        await update.message.reply_text("Эта команда доступна только администраторам.")
        return
    
    # Обработка команды без аргументов - показываем текущие настройки
    if not context.args:
        settings = get_group_settings(chat_id)
        smart_enabled = settings['smart_warnings'] if settings else False
        auto_enabled = settings['smart_warnings_auto'] if settings else False
        confidence = settings['smart_warnings_min_confidence'] if settings else 0.8
        enabled_types = settings['smart_warnings_enabled_types'].split(',') if settings and settings['smart_warnings_enabled_types'] else []
        
        status_text = f"""
*Настройки умных предупреждений:*
Статус: {'Включены ✅' if smart_enabled else 'Выключены ❌'}
Автопредупреждения: {'Включены ✅' if auto_enabled else 'Выключены ❌'}
Минимальная уверенность: {confidence * 100:.0f}%
Типы нарушений: {', '.join(enabled_types) if enabled_types else 'Не выбраны'}

*Управление:*
/smartwarnings on - Включить
/smartwarnings off - Выключить
/smartwarnings auto on - Включить автопредупреждения
/smartwarnings auto off - Выключить автопредупреждения
/smartwarnings types - Посмотреть типы нарушений
/smartwarnings set_types spam,obscenity,rudeness,flood - Установить типы нарушений
/smartwarnings confidence 0.8 - Установить минимальную уверенность (0-1)
"""
        await update.message.reply_text(status_text, parse_mode=ParseMode.MARKDOWN)
        return
    
    # Обработка команды с аргументами
    command = context.args[0].lower()
    
    if command == 'on':
        # Включаем умные предупреждения
        toggle_smart_warnings(chat_id, True)
        await update.message.reply_text("Умные предупреждения включены! ✅")
    
    elif command == 'off':
        # Выключаем умные предупреждения
        toggle_smart_warnings(chat_id, False)
        toggle_auto_warnings(chat_id, False)  # Также выключаем автопредупреждения
        await update.message.reply_text("Умные предупреждения выключены! ❌")
    
    elif command == 'auto':
        if len(context.args) < 2:
            await update.message.reply_text("Пожалуйста, укажите 'on' или 'off' после 'auto'.")
            return
        
        auto_command = context.args[1].lower()
        
        if auto_command == 'on':
            # Включаем автопредупреждения (и умные предупреждения тоже)
            toggle_smart_warnings(chat_id, True)
            toggle_auto_warnings(chat_id, True)
            await update.message.reply_text("Автоматические предупреждения включены! ✅")
        
        elif auto_command == 'off':
            # Выключаем только автопредупреждения
            toggle_auto_warnings(chat_id, False)
            await update.message.reply_text("Автоматические предупреждения выключены! ❌")
        
        else:
            await update.message.reply_text("Неизвестная команда. Используйте 'on' или 'off' после 'auto'.")
    
    elif command == 'types':
        # Показываем доступные типы нарушений
        types_text = """
*Доступные типы нарушений:*
- spam - Спам и реклама
- obscenity - Нецензурная лексика
- rudeness - Грубость и оскорбления
- flood - Флуд и повторяющиеся сообщения

*Текущие включенные типы:*
{}

*Как установить:*
/smartwarnings set_types spam,obscenity,rudeness,flood
"""
        enabled_types = get_enabled_violation_types(chat_id)
        types_text = types_text.format(', '.join(enabled_types) if enabled_types else 'Не выбраны')
        
        await update.message.reply_text(types_text, parse_mode=ParseMode.MARKDOWN)
    
    elif command == 'set_types':
        if len(context.args) < 2:
            await update.message.reply_text("Пожалуйста, укажите типы нарушений через запятую.")
            return
        
        types_arg = context.args[1]
        types = [t.strip() for t in types_arg.split(',')]
        
        # Проверяем корректность типов
        valid_types = ['spam', 'obscenity', 'rudeness', 'flood']
        invalid_types = [t for t in types if t not in valid_types]
        
        if invalid_types:
            await update.message.reply_text(
                f"Неизвестные типы нарушений: {', '.join(invalid_types)}\n"
                f"Доступные типы: {', '.join(valid_types)}"
            )
            return
        
        # Устанавливаем типы нарушений
        set_enabled_violation_types(chat_id, types)
        await update.message.reply_text(f"Установлены типы нарушений: {', '.join(types)}")
    
    elif command == 'confidence':
        if len(context.args) < 2:
            await update.message.reply_text("Пожалуйста, укажите значение уверенности (от 0 до 1).")
            return
        
        try:
            confidence = float(context.args[1])
            if confidence < 0 or confidence > 1:
                raise ValueError("Значение должно быть от 0 до 1")
            
            # Устанавливаем минимальную уверенность
            set_min_confidence(chat_id, confidence)
            await update.message.reply_text(f"Установлена минимальная уверенность: {confidence * 100:.0f}%")
        
        except ValueError as e:
            await update.message.reply_text(f"Ошибка: {e}. Укажите число от 0 до 1.")
    
    else:
        await update.message.reply_text("Неизвестная команда. Используйте /smartwarnings без аргументов для справки.")

async def analyze_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Анализировать сообщение на нарушения"""
    user = update.effective_user
    chat_id = update.effective_chat.id
    
    # Проверяем, является ли пользователь администратором
    if not is_admin(user.id):
        await update.message.reply_text("Эта команда доступна только администраторам.")
        return
    
    # Проверяем, есть ли ответ на сообщение
    if not update.message.reply_to_message:
        await update.message.reply_text("Пожалуйста, ответьте на сообщение, которое хотите проанализировать.")
        return
    
    # Получаем сообщение для анализа
    target_message = update.message.reply_to_message
    target_user = target_message.from_user
    message_text = target_message.text
    
    if not message_text:
        await update.message.reply_text("Это сообщение не содержит текста для анализа.")
        return
    
    # Получаем контекст сообщений пользователя
    messages = message_tracker.get_user_messages(
        str(target_user.id),
        chat_id=str(chat_id),
        seconds=60,
        limit=5
    )
    
    # Подсчитываем похожие сообщения
    similar_count = 0
    if messages:
        # Очень простая проверка на похожесть - совпадение первых 5 символов
        last_message_prefix = message_text[:5].lower() if len(message_text) >= 5 else message_text.lower()
        for msg in messages:
            msg_text = msg['text']
            if msg_text[:5].lower() == last_message_prefix:
                similar_count += 1
    
    # Создаем контекст для анализа
    context = {
        'recent_messages': [msg['text'] for msg in messages],
        'message_count': len(messages),
        'frequency': message_tracker.get_message_frequency(str(target_user.id), str(chat_id), 60),
        'similar_messages': similar_count
    }
    
    # Анализируем сообщение
    analysis_result = warning_analyzer.analyze_message(message_text, context)
    
    # Записываем результат анализа
    record_id = record_analysis(
        chat_id,
        target_user.id,
        target_message.message_id,
        message_text,
        analysis_result
    )
    
    # Формируем текст результата
    confidence_percent = int(analysis_result['confidence'] * 100)
    violations_text = ', '.join(analysis_result['violations']) if analysis_result['violations'] else "не обнаружены"
    
    result_text = f"""
*Результат анализа сообщения:*
Пользователь: {target_user.first_name} (ID: {target_user.id})
Нарушения: {violations_text}
Уверенность: {confidence_percent}%
"""
    
    if analysis_result['suggested_warning']:
        result_text += f"Предлагаемое предупреждение: {analysis_result['suggested_warning']}\n"
    
    result_text += f"""
*Контекст:*
Сообщений за минуту: {context['frequency']:.1f}
Похожих сообщений: {similar_count}
"""
    
    # Если есть нарушение, добавляем кнопку для выдачи предупреждения
    if analysis_result['has_violation']:
        keyboard = [
            [InlineKeyboardButton(
                "⚠️ Выдать предупреждение",
                callback_data=f"warn_{target_user.id}_{target_message.message_id}_{record_id}"
            )]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(result_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text(result_text, parse_mode=ParseMode.MARKDOWN)

async def show_analyses(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать последние результаты анализа"""
    user = update.effective_user
    chat_id = update.effective_chat.id
    
    # Проверяем, является ли пользователь администратором
    if not is_admin(user.id):
        await update.message.reply_text("Эта команда доступна только администраторам.")
        return
    
    # Получаем последние результаты анализа
    analyses = get_last_analysis(chat_id)
    
    if not analyses:
        await update.message.reply_text("Пока нет результатов анализа сообщений.")
        return
    
    # Формируем текст результатов
    results_text = "*Последние результаты анализа:*\n\n"
    
    for i, analysis in enumerate(analyses[:5], 1):
        violations = analysis['violation_types'].split(',') if analysis['violation_types'] else []
        violations_text = ', '.join(violations) if violations else "нет"
        
        confidence_percent = int(analysis['confidence'] * 100) if analysis['confidence'] else 0
        
        results_text += f"{i}. Пользователь ID: {analysis['user_id']}\n"
        results_text += f"   Нарушения: {violations_text}\n"
        results_text += f"   Уверенность: {confidence_percent}%\n"
        results_text += f"   Выдано предупреждение: {'Да' if analysis['is_warned'] else 'Нет'}\n\n"
    
    # Добавляем информацию о количестве записей
    total_count = len(analyses)
    if total_count > 5:
        results_text += f"... и еще {total_count - 5} записей."
    
    await update.message.reply_text(results_text, parse_mode=ParseMode.MARKDOWN)

# ------ Обработка колбэков ------ #

async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка колбэков от инлайн-кнопок"""
    query = update.callback_query
    user = query.from_user
    
    # Извлекаем данные из callback_data
    callback_data = query.data
    
    # Предотвращаем повторную обработку
    await query.answer()
    
    # Обработка разных типов колбэков
    if callback_data.startswith('warn_'):
        # Колбэк для выдачи предупреждения из результата анализа
        _, target_user_id, message_id, analysis_id = callback_data.split('_')
        
        # Проверяем, является ли пользователь администратором
        if not is_admin(user.id):
            await query.edit_message_text(
                "У вас нет прав для выполнения этого действия."
            )
            return
        
        try:
            # Получаем информацию о сообщении из базы данных
            with get_db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT * FROM message_analysis WHERE id = ?",
                    (analysis_id,)
                )
                analysis = cursor.fetchone()
                
                if not analysis:
                    await query.edit_message_text(
                        "Информация об анализе не найдена."
                    )
                    return
                
                # Проверяем, было ли уже выдано предупреждение
                if analysis['is_warned']:
                    await query.edit_message_text(
                        "Предупреждение уже было выдано за это сообщение."
                    )
                    return
                
                # Получаем предупреждение для пользователя
                suggested_warning = analysis['suggested_warning'] or "Нарушение правил"
                
                # Получаем текущие предупреждения пользователя
                warnings_data = get_user_warnings(analysis['group_id'], target_user_id)
                warnings = warnings_data['warnings']
                
                # Увеличиваем количество предупреждений
                cursor.execute(
                    "UPDATE user_warnings SET warnings = warnings + 1, reason = ?, updated_at = CURRENT_TIMESTAMP "
                    "WHERE group_id = ? AND user_id = ?",
                    (suggested_warning, analysis['group_id'], target_user_id)
                )
                
                # Помечаем анализ как предупрежденный
                cursor.execute(
                    "UPDATE message_analysis SET is_warned = 1 WHERE id = ?",
                    (analysis_id,)
                )
                conn.commit()
                
                # Проверяем, достигнут ли лимит предупреждений
                warnings += 1
                
                result_text = f"Пользователю (ID: {target_user_id}) выдано предупреждение.\n"
                result_text += f"Всего предупреждений: {warnings}/{MAX_WARNINGS}\n"
                result_text += f"Причина: {suggested_warning}"
                
                # Если достигнут лимит, баним пользователя
                if warnings >= MAX_WARNINGS:
                    try:
                        await context.bot.ban_chat_member(
                            int(analysis['group_id']), 
                            int(target_user_id)
                        )
                        
                        # Сбрасываем счетчик предупреждений
                        cursor.execute(
                            "UPDATE user_warnings SET warnings = 0, updated_at = CURRENT_TIMESTAMP "
                            "WHERE group_id = ? AND user_id = ?",
                            (analysis['group_id'], target_user_id)
                        )
                        conn.commit()
                        
                        result_text += f"\n\nПользователь забанен после достижения {MAX_WARNINGS} предупреждений!"
                    except Exception as e:
                        result_text += f"\n\nОшибка при бане пользователя: {e}"
                
                await query.edit_message_text(result_text)
                
                # Отправляем сообщение в группу
                try:
                    await context.bot.send_message(
                        int(analysis['group_id']),
                        f"Пользователь (ID: {target_user_id}) получил предупреждение.\n"
                        f"Всего предупреждений: {warnings}/{MAX_WARNINGS}\n"
                        f"Причина: {suggested_warning}"
                    )
                except Exception as e:
                    logger.error(f"Ошибка при отправке сообщения в группу: {e}")
        
        except Exception as e:
            await query.edit_message_text(f"Произошла ошибка: {e}")
            logger.error(f"Ошибка при обработке колбэка warn: {e}")
    
    elif callback_data.startswith('profile_'):
        # Колбэк для действий из профиля пользователя
        action, target_user_id = callback_data.split('_')[1:]
        
        # Проверяем, является ли пользователь администратором
        if not is_admin(user.id):
            await query.edit_message_text(
                "У вас нет прав для выполнения этого действия."
            )
            return
        
        # Получаем ID чата из предыдущего сообщения
        chat_id = query.message.chat_id
        
        try:
            if action == 'warn':
                # Выдаем предупреждение
                warnings_data = get_user_warnings(chat_id, target_user_id)
                warnings = warnings_data['warnings']
                
                # Увеличиваем количество предупреждений
                with get_db_connection() as conn:
                    cursor = conn.cursor()
                    cursor.execute(
                        "UPDATE user_warnings SET warnings = warnings + 1, reason = ?, updated_at = CURRENT_TIMESTAMP "
                        "WHERE group_id = ? AND user_id = ?",
                        ("Нарушение правил (из профиля)", str(chat_id), target_user_id)
                    )
                    conn.commit()
                
                # Проверяем, достигнут ли лимит предупреждений
                warnings += 1
                
                result_text = f"Пользователю (ID: {target_user_id}) выдано предупреждение.\n"
                result_text += f"Всего предупреждений: {warnings}/{MAX_WARNINGS}"
                
                # Если достигнут лимит, баним пользователя
                if warnings >= MAX_WARNINGS:
                    try:
                        await context.bot.ban_chat_member(chat_id, int(target_user_id))
                        
                        # Сбрасываем счетчик предупреждений
                        with get_db_connection() as conn:
                            cursor = conn.cursor()
                            cursor.execute(
                                "UPDATE user_warnings SET warnings = 0, updated_at = CURRENT_TIMESTAMP "
                                "WHERE group_id = ? AND user_id = ?",
                                (str(chat_id), target_user_id)
                            )
                            conn.commit()
                        
                        result_text += f"\n\nПользователь забанен после достижения {MAX_WARNINGS} предупреждений!"
                    except Exception as e:
                        result_text += f"\n\nОшибка при бане пользователя: {e}"
            
            elif action == 'mute':
                # Мутим пользователя на 24 часа
                permissions = ChatPermissions(
                    can_send_messages=False,
                    can_send_media_messages=False,
                    can_send_polls=False,
                    can_send_other_messages=False,
                    can_add_web_page_previews=False,
                    can_change_info=False,
                    can_invite_users=False,
                    can_pin_messages=False
                )
                
                until_date = int(time.time() + MUTE_TIME)
                
                await context.bot.restrict_chat_member(
                    chat_id,
                    int(target_user_id),
                    permissions,
                    until_date=until_date
                )
                
                result_text = f"Пользователь (ID: {target_user_id}) заглушен на 24 часа."
            
            elif action == 'kick':
                # Кикаем пользователя
                await context.bot.ban_chat_member(chat_id, int(target_user_id))
                await context.bot.unban_chat_member(chat_id, int(target_user_id))
                
                result_text = f"Пользователь (ID: {target_user_id}) выгнан из группы."
            
            elif action == 'ban':
                # Баним пользователя
                await context.bot.ban_chat_member(chat_id, int(target_user_id))
                
                result_text = f"Пользователь (ID: {target_user_id}) забанен."
            
            else:
                result_text = "Неизвестное действие."
            
            await query.edit_message_text(result_text)
        
        except Exception as e:
            await query.edit_message_text(f"Произошла ошибка: {e}")
            logger.error(f"Ошибка при обработке колбэка profile: {e}")

# ------ Обработчики событий ------ #

async def welcome_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Приветствие нового участника группы"""
    chat_id = update.effective_chat.id
    
    # Получаем настройки группы
    settings = get_group_settings(chat_id)
    welcome_message = settings['welcome_message'] if settings else "Добро пожаловать, {name}!"
    
    # Обрабатываем всех новых участников
    for new_member in update.message.new_chat_members:
        # Игнорируем бота
        if new_member.id == context.bot.id:
            continue
        
        # Обновляем информацию о пользователе в базе данных
        update_user_info(
            new_member.id,
            username=new_member.username,
            first_name=new_member.first_name,
            last_name=new_member.last_name
        )
        
        # Форматируем и отправляем приветствие
        formatted_welcome = welcome_message.format(
            name=new_member.first_name
        )
        
        await update.message.reply_text(formatted_welcome)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка каждого сообщения"""
    message = update.message
    user = message.from_user
    chat_id = message.chat_id
    
    # Игнорируем сообщения без текста или от ботов
    if not message.text or user.is_bot:
        return
    
    # Обновляем информацию о пользователе
    update_user_info(
        user.id,
        username=user.username,
        first_name=user.first_name,
        last_name=user.last_name
    )
    
    # Проверка на флуд
    if check_flood(str(user.id), str(chat_id), message.text):
        # Игнорируем флуд от владельцев и администраторов
        if is_owner(user.id) or is_admin(user.id):
            return
        
        settings = get_group_settings(chat_id)
        if settings and settings['anti_flood']:
            try:
                # Заглушаем пользователя
                permissions = ChatPermissions(
                    can_send_messages=False,
                    can_send_media_messages=False,
                    can_send_polls=False,
                    can_send_other_messages=False,
                    can_add_web_page_previews=False,
                    can_change_info=False,
                    can_invite_users=False,
                    can_pin_messages=False
                )
                
                until_date = int(time.time() + FLOOD_MUTE_TIME)
                
                await context.bot.restrict_chat_member(
                    chat_id,
                    user.id,
                    permissions,
                    until_date=until_date
                )
                
                # Отправляем предупреждение
                await message.reply_text(
                    f"Пользователь {user.first_name} заглушен на 15 минут за флуд."
                )
                
                logger.info(f"Пользователь {user.id} заглушен за флуд в группе {chat_id}")
            except Exception as e:
                logger.error(f"Ошибка при муте пользователя за флуд: {e}")
    
    # Умные предупреждения
    if is_smart_warnings_enabled(str(chat_id)):
        # Получаем контекст сообщений пользователя
        messages = message_tracker.get_user_messages(
            str(user.id),
            chat_id=str(chat_id),
            seconds=60,
            limit=5
        )
        
        # Подсчитываем похожие сообщения
        similar_count = 0
        if messages:
            # Простая проверка на похожесть
            last_message_prefix = message.text[:5].lower() if len(message.text) >= 5 else message.text.lower()
            for msg in messages:
                msg_text = msg['text']
                if msg_text[:5].lower() == last_message_prefix:
                    similar_count += 1
        
        # Создаем контекст для анализа
        context_data = {
            'recent_messages': [msg['text'] for msg in messages],
            'message_count': len(messages),
            'frequency': message_tracker.get_message_frequency(str(user.id), str(chat_id), 60),
            'similar_messages': similar_count
        }
        
        # Анализируем сообщение
        analysis_result = warning_analyzer.analyze_message(message.text, context_data)
        
        # Отфильтровываем по включенным типам нарушений
        enabled_types = get_enabled_violation_types(str(chat_id))
        if enabled_types:
            analysis_result['violations'] = [v for v in analysis_result['violations'] if v in enabled_types]
            analysis_result['has_violation'] = len(analysis_result['violations']) > 0
            
            # Пересчитываем уверенность
            if analysis_result['has_violation']:
                analysis_result['confidence'] = min(0.95, 0.5 + (len(analysis_result['violations']) * 0.15))
            else:
                analysis_result['confidence'] = 0.0
                analysis_result['suggested_warning'] = None
        
        # Если есть нарушение, записываем результат
        if analysis_result['has_violation']:
            record_id = record_analysis(
                chat_id,
                user.id,
                message.message_id,
                message.text,
                analysis_result
            )
            
            # Проверяем на автоматические предупреждения
            min_confidence = get_min_confidence(str(chat_id))
            
            if (is_auto_warnings_enabled(str(chat_id)) and
                analysis_result['confidence'] >= min_confidence and
                not is_owner(user.id) and
                not is_admin(user.id)):
                
                # Автоматически выдаем предупреждение
                warnings_data = get_user_warnings(chat_id, user.id)
                warnings = warnings_data['warnings']
                
                # Увеличиваем количество предупреждений
                with get_db_connection() as conn:
                    cursor = conn.cursor()
                    cursor.execute(
                        "UPDATE user_warnings SET warnings = warnings + 1, reason = ?, updated_at = CURRENT_TIMESTAMP "
                        "WHERE group_id = ? AND user_id = ?",
                        (analysis_result['suggested_warning'], str(chat_id), str(user.id))
                    )
                    
                    # Помечаем анализ как предупрежденный
                    cursor.execute(
                        "UPDATE message_analysis SET is_warned = 1 WHERE id = ?",
                        (record_id,)
                    )
                    conn.commit()
                
                # Проверяем, достигнут ли лимит предупреждений
                warnings += 1
                
                # Отправляем сообщение о предупреждении
                await message.reply_text(
                    f"Пользователь {user.first_name} (ID: {user.id}) получил автоматическое предупреждение.\n"
                    f"Всего предупреждений: {warnings}/{MAX_WARNINGS}\n"
                    f"Причина: {analysis_result['suggested_warning']}"
                )
                
                # Если достигнут лимит, баним пользователя
                if warnings >= MAX_WARNINGS:
                    try:
                        await context.bot.ban_chat_member(chat_id, user.id)
                        
                        # Сбрасываем счетчик предупреждений
                        with get_db_connection() as conn:
                            cursor = conn.cursor()
                            cursor.execute(
                                "UPDATE user_warnings SET warnings = 0, updated_at = CURRENT_TIMESTAMP "
                                "WHERE group_id = ? AND user_id = ?",
                                (str(chat_id), str(user.id))
                            )
                            conn.commit()
                        
                        await message.reply_text(
                            f"Пользователь {user.first_name} (ID: {user.id}) забанен после достижения {MAX_WARNINGS} предупреждений."
                        )
                    except Exception as e:
                        logger.error(f"Ошибка при бане пользователя после предупреждений: {e}")
            
            elif (analysis_result['confidence'] >= 0.7 and
                  not is_owner(user.id) and
                  not is_admin(user.id)):
                # Предлагаем администраторам выдать предупреждение
                # Отправляем кнопку предупреждения всем админам в личку
                pass

# ---------------------- ОСНОВНАЯ ФУНКЦИЯ ---------------------- #

def update_health_check_file():
    """Обновляет файл проверки состояния для мониторинга"""
    while True:
        try:
            with open(HEALTH_CHECK_FILE, 'w') as f:
                f.write(f"Bot is running: {datetime.datetime.now()}")
            logger.debug("Обновлен файл состояния бота")
        except Exception as e:
            logger.error(f"Ошибка при обновлении файла состояния: {e}")
        
        time.sleep(HEALTH_CHECK_INTERVAL)

def signal_handler(sig, frame):
    """Обработчик сигналов для корректного завершения"""
    logger.info(f"Получен сигнал {sig}, завершение работы...")
    try:
        with open(HEALTH_CHECK_FILE, 'w') as f:
            f.write(f"Bot shutdown: {datetime.datetime.now()}")
    except Exception:
        pass
    sys.exit(0)

def main():
    """Основная функция запуска бота"""
    # Инициализация базы данных
    init_db()
    
    # Установка обработчиков сигналов
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # Запись информации о запуске
    try:
        with open(HEALTH_CHECK_FILE, 'w') as f:
            f.write(f"Bot starting: {datetime.datetime.now()}")
    except Exception as e:
        logger.error(f"Не удалось создать файл состояния: {e}")
    
    # Запуск потока обновления файла состояния
    health_thread = threading.Thread(target=update_health_check_file, daemon=True)
    health_thread.start()
    logger.info("Запущен поток проверки состояния бота")
    
    # Создание и настройка приложения бота
    application = ApplicationBuilder().token(TOKEN).build()
    
    # Регистрация обработчиков команд
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("id", id_command))
    application.add_handler(CommandHandler("info", info_command))
    application.add_handler(CommandHandler("profile", profile_command))
    
    # Команды модерации
    application.add_handler(CommandHandler("ban", ban_command))
    application.add_handler(CommandHandler("unban", unban_command))
    application.add_handler(CommandHandler("kick", kick_command))
    application.add_handler(CommandHandler("mute", mute_command))
    application.add_handler(CommandHandler("unmute", unmute_command))
    
    # Система предупреждений
    application.add_handler(CommandHandler("warn", warn_command))
    application.add_handler(CommandHandler("unwarn", unwarn_command))
    application.add_handler(CommandHandler("warnings", warnings_command))
    application.add_handler(CommandHandler("clearwarnings", clear_warnings_command))
    
    # Настройки группы
    application.add_handler(CommandHandler("setrules", set_rules_command))
    application.add_handler(CommandHandler("rules", rules_command))
    application.add_handler(CommandHandler("setwelcome", set_welcome_command))
    application.add_handler(CommandHandler("toggleflood", toggle_flood_command))
    
    # Умные предупреждения
    application.add_handler(CommandHandler("smartwarnings", smart_warnings_command))
    application.add_handler(CommandHandler("analyze", analyze_command))
    application.add_handler(CommandHandler("analyses", show_analyses))
    
    # Обработчик кнопок
    application.add_handler(CallbackQueryHandler(handle_callback_query))
    
    # Обработчики событий
    application.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_new_member))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    # Обработчик ошибок
    application.add_error_handler(error_handler)
    
    # Запуск бота
    logger.info("Запуск бота...")
    try:
        # Обновляем файл состояния перед запуском
        with open(HEALTH_CHECK_FILE, 'w') as f:
            f.write(f"Bot polling started: {datetime.datetime.now()}")
        
        # Запускаем бота со стандартными параметрами
        application.run_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True
        )
    except Exception as e:
        logger.critical(f"Критическая ошибка при запуске бота: {e}")
        
        # Записываем информацию об ошибке
        try:
            with open(HEALTH_CHECK_FILE, 'w') as f:
                f.write(f"Bot crashed: {datetime.datetime.now()} - Error: {str(e)}")
        except Exception:
            pass
        
        # Повторно вызываем исключение
        raise
    finally:
        # Запись о завершении
        try:
            with open(HEALTH_CHECK_FILE, 'w') as f:
                f.write(f"Bot shutdown: {datetime.datetime.now()}")
        except Exception:
            pass

if __name__ == '__main__':
    main()