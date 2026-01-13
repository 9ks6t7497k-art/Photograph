import logging
import time
import requests
import tempfile
import os
from telegram.ext import Updater, CommandHandler, MessageHandler, CallbackQueryHandler, Filters
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ParseMode, ChatAction
from io import BytesIO
import base64
import json
import uuid
from datetime import datetime
import threading
import re
from dotenv import load_dotenv

# Загружаем переменные окружения из .env файла
load_dotenv()

# ============== БЕЗОПАСНЫЕ НАСТРОЙКИ ==============
# Токены загружаются из переменных окружения
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
EVOLINK_API_KEY = os.getenv("EVOLINK_API_KEY")

# Проверяем наличие обязательных токенов
if not TELEGRAM_BOT_TOKEN:
    print("❌ ОШИБКА: TELEGRAM_BOT_TOKEN не найден в переменных окружения!")
    print("💡 Создайте файл .env с TELEGRAM_BOT_TOKEN=ваш_токен")
    exit(1)

if not EVOLINK_API_KEY:
    print("❌ ОШИБКА: EVOLINK_API_KEY не найден в переменных окружения!")
    print("💡 Создайте файл .env с EVOLINK_API_KEY=ваш_ключ")
    exit(1)

# Настройки ЮКассы (можно оставить тестовые или указать в .env)
YOOKASSA_SHOP_ID = os.getenv("YOOKASSA_SHOP_ID", "test_shop_id")
YOOKASSA_SECRET_KEY = os.getenv("YOOKASSA_SECRET_KEY", "test_secret_key")
YOOKASSA_PAYMENT_URL = "https://api.yookassa.ru/v3/payments"

# Цены в рублях
PRICES = {
    'text-to-image': 50,      # 50 руб за изображение
    'text-to-video': 150,     # 150 руб за видео из текста
    'image-to-video': 100,    # 100 руб за видео из изображения
    'image-to-image': 75,     # 75 руб за редактирование изображения
}

# Лимиты для демо-режима
FREE_LIMITS = {
    'text-to-image': 3,
    'text-to-video': 1,
    'image-to-video': 1,
    'image-to-image': 2,
}

# ID администратора (из .env или по умолчанию)
ADMIN_ID = int(os.getenv("ADMIN_ID", "123456789"))

# Хранилища данных (в памяти)
user_states = {}
user_stats = {}
user_balances = {}
pending_payments = {}

# Настройка логгирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.FileHandler('bot.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ============== МОДЕЛИ ==============
AVAILABLE_MODELS = {
    'text-to-image': {
        "name": "🖼️ Текст → Изображение",
        "description": "Создает картинку по описанию",
        "api_model": "gpt-4o-image",
        "endpoint": "images/generations",
        "type": "image",
        "requires": "text",
        "size": "1024x1024",
        "price": PRICES['text-to-image'],
        "free_limit": FREE_LIMITS['text-to-image']
    },
    'text-to-video': {
        "name": "🎬 Текст → Видео",
        "description": "Создает видео по описанию",
        "api_model": "wan2.5-text-to-video",
        "endpoint": "videos/generations",
        "type": "video",
        "requires": "text",
        "size": "1024x576",
        "duration": 5,
        "price": PRICES['text-to-video'],
        "free_limit": FREE_LIMITS['text-to-video']
    },
    'image-to-video': {
        "name": "🎬 Изображение → Видео",
        "description": "Создает видео из картинки",
        "api_model": "wan2.5-image-to-video",
        "endpoint": "videos/generations",
        "type": "video",
        "requires": "image",
        "size": "1024x576",
        "duration": 5,
        "price": PRICES['image-to-video'],
        "free_limit": FREE_LIMITS['image-to-video']
    },
    'image-to-image': {
        "name": "✨ Изображение → Изображение (AI-редактирование)",
        "description": "Редактирует и улучшает изображение с помощью Qwen AI",
        "api_model": "qwen-image-edit-plus",
        "endpoint": "services/aigc/image2image/editing",
        "type": "image",
        "requires": "both",
        "size": "1024x1024",
        "price": PRICES['image-to-image'],
        "free_limit": FREE_LIMITS['image-to-image'],
        "special_model": True
    }
}

# ============== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==============
def debug_log(message):
    """Логирование отладочной информации"""
    logger.debug(message)
    print(f"[DEBUG] {time.strftime('%H:%M:%S')} - {message}")

def get_user_stats(user_id):
    """Получение статистики пользователя"""
    if user_id not in user_stats:
        user_stats[user_id] = {model_key: 0 for model_key in AVAILABLE_MODELS}
        user_stats[user_id]['total_spent'] = 0
        user_stats[user_id]['created_at'] = time.time()
    return user_stats[user_id]

def get_user_balance(user_id):
    """Получение баланса пользователя"""
    if user_id not in user_balances:
        user_balances[user_id] = 0
    return user_balances[user_id]

def can_use_for_free(user_id, model_key):
    """Проверка возможности бесплатного использования"""
    stats = get_user_stats(user_id)
    free_limit = AVAILABLE_MODELS[model_key]['free_limit']
    return stats[model_key] < free_limit

def increment_usage(user_id, model_key):
    """Увеличение счетчика использования"""
    stats = get_user_stats(user_id)
    stats[model_key] += 1

def image_to_base64(image_data):
    """Конвертация изображения в base64"""
    try:
        if hasattr(image_data, 'read'):
            image_data.seek(0)
            image_bytes = image_data.read()
        else:
            image_bytes = image_data
        
        return base64.b64encode(image_bytes).decode('utf-8')
    except Exception as e:
        debug_log(f"Ошибка конвертации в base64: {e}")
        return None

def save_to_temp_file(data, extension='.jpg'):
    """Сохранение данных во временный файл"""
    try:
        temp_file = tempfile.NamedTemporaryFile(suffix=extension, delete=False)
        if hasattr(data, 'seek'):
            data.seek(0)
        if hasattr(data, 'read'):
            temp_file.write(data.read())
        else:
            temp_file.write(data)
        temp_file.close()
        return temp_file.name
    except Exception as e:
        debug_log(f"Ошибка сохранения файла: {e}")
        return None

# ============== API ФУНКЦИИ ==============
def create_generation_task(model_info, prompt, image_base64=None):
    """Создает задачу генерации через Evolink API"""
    try:
        api_model = model_info.get("api_model")
        endpoint = model_info.get("endpoint")
        
        debug_log(f"Создаю задачу для модели {api_model}")
        
        url = f"https://api.evolink.ai/v1/{endpoint}"
        headers = {
            "Authorization": f"Bearer {EVOLINK_API_KEY}",
            "Content-Type": "application/json"
        }
        
        # Формируем payload в зависимости от модели
        payload = {}
        
        if api_model == "qwen-image-edit-plus":
            if not image_base64:
                debug_log("Для Qwen Image Edit требуется изображение")
                return None
            
            payload = {
                "model": api_model,
                "prompt": prompt,
                "image_urls": [f"data:image/jpeg;base64,{image_base64}"],
                "n": 1,
                "size": model_info.get("size", "1024x1024"),
                "prompt_extend": True,
                "watermark": False,
                "negative_prompt": "blurry, low quality, distorted"
            }
            
        elif endpoint == "images/generations":
            payload = {
                "model": api_model,
                "prompt": prompt,
                "size": model_info.get("size", "1024x1024"),
                "n": 1
            }
            
        elif endpoint == "videos/generations":
            payload = {
                "model": api_model,
                "prompt": prompt,
                "size": model_info.get("size", "1024x576"),
                "duration": model_info.get("duration", 5)
            }
            
            if image_base64:
                payload["image"] = f"data:image/jpeg;base64,{image_base64}"
        
        debug_log(f"Отправка запроса к API: {url}")
        
        # Отправляем запрос с таймаутом
        response = requests.post(url, headers=headers, json=payload, timeout=60)
        response.raise_for_status()  # Проверка на ошибки HTTP
        
        data = response.json()
        debug_log(f"Ответ API получен")
        
        # Обрабатываем разные форматы ответа
        if "id" in data:
            # Асинхронная задача
            task_id = data["id"]
            estimated_time = data.get('task_info', {}).get('estimated_time', 45)
            
            debug_log(f"Задача создана: {task_id}, время: {estimated_time}с")
            
            return {
                "type": model_info["type"],
                "task_id": task_id,
                "result": None,
                "estimated_time": estimated_time
            }
            
        elif "data" in data and isinstance(data["data"], list) and len(data["data"]) > 0:
            # Прямой результат
            result_url = data["data"][0].get("url")
            if result_url:
                return {
                    "type": model_info["type"],
                    "result": result_url,
                    "task_id": None
                }
                
        elif "url" in data:
            return {
                "type": model_info["type"],
                "result": data["url"],
                "task_id": None
            }
            
        debug_log(f"Неожиданный формат ответа")
        return None
            
    except requests.exceptions.Timeout:
        debug_log("Таймаут при подключении к API")
        return None
    except requests.exceptions.RequestException as e:
        debug_log(f"Ошибка сети: {e}")
        return None
    except Exception as e:
        debug_log(f"Ошибка создания задачи: {str(e)}")
        return None

def wait_for_task_completion(task_id, task_type, max_wait=300, poll_interval=5):
    """Ожидает завершения задачи"""
    debug_log(f"Ожидаю завершения задачи {task_id}...")
    
    start_time = time.time()
    
    while time.time() - start_time < max_wait:
        try:
            url = f"https://api.evolink.ai/v1/tasks/{task_id}"
            headers = {
                "Authorization": f"Bearer {EVOLINK_API_KEY}",
                "Content-Type": "application/json"
            }
            
            response = requests.get(url, headers=headers, timeout=30)
            response.raise_for_status()
            
            task_data = response.json()
            status = task_data.get("status", "unknown")
            
            if status == "completed":
                debug_log(f"Задача завершена за {time.time() - start_time:.1f} секунд")
                
                # Ищем URL результата
                result_url = None
                
                if "output" in task_data and isinstance(task_data["output"], dict):
                    output = task_data["output"]
                    if task_type == "image" and "image_urls" in output and output["image_urls"]:
                        result_url = output["image_urls"][0]
                    elif task_type == "video" and "video_urls" in output and output["video_urls"]:
                        result_url = output["video_urls"][0]
                
                if not result_url and "url" in task_data:
                    result_url = task_data["url"]
                    
                if result_url:
                    debug_log(f"Результат получен")
                    return result_url
                else:
                    debug_log("Не удалось найти URL результата")
                    return None
                
            elif status == "failed":
                error_msg = task_data.get('error', {}).get('message', 'No error details')
                debug_log(f"Задача провалена: {error_msg}")
                return None
                
            elif status in ["processing", "pending"]:
                progress = task_data.get("progress", 0)
                debug_log(f"Прогресс: {progress}%")
            
            time.sleep(poll_interval)
            
        except requests.exceptions.RequestException as e:
            debug_log(f"Ошибка проверки задачи: {e}")
            time.sleep(poll_interval)
        except Exception as e:
            debug_log(f"Ошибка: {e}")
            time.sleep(poll_interval)
    
    debug_log(f"Превышено время ожидания")
    return None

def download_file(url, max_retries=3):
    """Скачивает файл по URL"""
    for retry in range(max_retries):
        try:
            debug_log(f"Скачиваю файл (попытка {retry+1})")
            
            response = requests.get(url, timeout=60, stream=True)
            response.raise_for_status()
            
            content = BytesIO()
            for chunk in response.iter_content(chunk_size=8192):
                content.write(chunk)
            content.seek(0)
            
            file_size = len(content.getvalue())
            debug_log(f"Файл скачан, размер: {file_size} байт")
            
            if file_size > 1024:  # Минимальный размер
                return content
                
        except Exception as e:
            debug_log(f"Ошибка скачивания: {e}")
            if retry < max_retries - 1:
                time.sleep(2)
    
    debug_log(f"Не удалось скачать файл")
    return None

# ============== МЕНЮ И ИНТЕРФЕЙС ==============
def show_main_menu(update, context):
    """Главное меню"""
    keyboard = [
        [InlineKeyboardButton("🖼️ Создать изображение", callback_data='menu_generate')],
        [InlineKeyboardButton("🎬 Создать видео", callback_data='menu_video')],
        [InlineKeyboardButton("✨ Редактировать фото", callback_data='model_image-to-image')],
        [InlineKeyboardButton("💰 Мой баланс", callback_data='menu_balance')],
        [InlineKeyboardButton("💳 Пополнить баланс", callback_data='menu_topup')],
        [InlineKeyboardButton("📊 Статистика", callback_data='menu_stats')],
        [InlineKeyboardButton("❓ Помощь", callback_data='menu_help')]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    if update.message:
        update.message.reply_text(
            "🎨 *AI Photograph Bot*\n\n"
            "Добро пожаловать! Я помогу вам:\n"
            "• Создать изображения и видео из текста\n"
            "• Редактировать фотографии с помощью AI\n"
            "• Улучшать качество изображений\n\n"
            "Выберите действие:",
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        try:
            query = update.callback_query
            query.edit_message_text(
                "🎨 *AI Photograph Bot*\n\n"
                "Добро пожаловать! Выберите действие:",
                reply_markup=reply_markup,
                parse_mode=ParseMode.MARKDOWN
            )
            query.answer()
        except Exception as e:
            debug_log(f"Ошибка: {e}")

def show_generation_menu(update, context):
    """Меню генерации изображений"""
    keyboard = [
        [InlineKeyboardButton("🖼️ Из текста (50 руб)", callback_data='model_text-to-image')],
        [InlineKeyboardButton("✨ Изображение → Изображение (75 руб)", callback_data='model_image-to-image')],
        [InlineKeyboardButton("🔙 Назад", callback_data='menu_back')]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    try:
        query = update.callback_query
        query.edit_message_text(
            "🖼️ *Создание изображений*\n\n"
            "Выберите тип генерации:\n\n"
            "• *Текст → Изображение* (50 руб)\n"
            "  Создание картинки по вашему описанию\n\n"
            "• *Изображение → Изображение* (75 руб)\n"
            "  Редактирование и улучшение фотографий\n"
            "  Примеры: изменение стиля, улучшение качества,\n"
            "  добавление эффектов, удаление фона\n",
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN
        )
        query.answer()
    except Exception as e:
        debug_log(f"Ошибка: {e}")

def show_video_menu(update, context):
    """Меню генерации видео"""
    keyboard = [
        [InlineKeyboardButton("🎬 Из текста (150 руб)", callback_data='model_text-to-video')],
        [InlineKeyboardButton("🎬 Из изображения (100 руб)", callback_data='model_image-to-video')],
        [InlineKeyboardButton("🔙 Назад", callback_data='menu_back')]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    try:
        query = update.callback_query
        query.edit_message_text(
            "🎬 *Создание видео*\n\n"
            "Выберите тип генерации:\n\n"
            "• *Текст → Видео* (150 руб)\n"
            "  Создание анимации по описанию\n\n"
            "• *Изображение → Видео* (100 руб)\n"
            "  Оживление фотографий, создание анимации\n",
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN
        )
        query.answer()
    except Exception as e:
        debug_log(f"Ошибка: {e}")

def handle_model_selection(update, context, user_id, model_key):
    """Обработчик выбора модели"""
    try:
        if model_key not in AVAILABLE_MODELS:
            update.callback_query.edit_message_text("❌ Модель не найдена")
            return
        
        model_info = AVAILABLE_MODELS[model_key]
        price = model_info['price']
        free_available = can_use_for_free(user_id, model_key)
        balance = get_user_balance(user_id)
        
        # Для редактирования фото показываем специальное меню
        if model_key == 'image-to-image':
            show_edit_photo_menu(update, context, user_id)
            return
        
        # Сохраняем состояние
        user_states[user_id] = {
            'model': model_key,
            'step': 'waiting_input',
            'free_generation': free_available
        }
        
        # Формируем сообщение
        if free_available:
            message = f"🎨 *{model_info['name']}*\n\nБесплатная попытка! (обычно {price} руб)\n\n"
        elif balance >= price:
            message = f"🎨 *{model_info['name']}*\n\nСтоимость: {price} руб\nВаш баланс: {balance} руб\n\n"
        else:
            message = f"❌ *Недостаточно средств!*\n\nНужно: {price} руб\nВаш баланс: {balance} руб\n\n"
        
        if model_info['requires'] == "image":
            message += "Отправьте изображение (можно с подписью):"
        else:
            message += "Опишите что создать:"
        
        query = update.callback_query
        query.edit_message_text(message, parse_mode=ParseMode.MARKDOWN)
        query.answer()
        
    except Exception as e:
        debug_log(f"Ошибка: {e}")

def show_edit_photo_menu(update, context, user_id):
    """Специальное меню для редактирования фото"""
    stats = get_user_stats(user_id)
    model_key = 'image-to-image'
    used = stats.get(model_key, 0)
    free_limit = AVAILABLE_MODELS[model_key]['free_limit']
    remaining = max(0, free_limit - used)
    
    keyboard = [
        [InlineKeyboardButton("📸 Загрузить фото", callback_data='upload_photo')],
        [InlineKeyboardButton("🔙 Назад", callback_data='menu_generate')]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    try:
        query = update.callback_query
        query.edit_message_text(
            f"✨ *Редактирование фотографий*\n\n"
            f"Стоимость: *{PRICES['image-to-image']} руб*\n"
            f"Бесплатных попыток осталось: *{remaining}/{free_limit}*\n\n"
            "*Что можно сделать:*\n"
            "• Изменить стиль (аниме, пиксель-арт, масляная живопись)\n"
            "• Улучшить качество и резкость\n"
            "• Удалить или заменить фон\n"
            "• Добавить/убрать объекты\n"
            "• Изменить время суток\n"
            "• Создать портрет в стиле известных художников\n\n"
            "*Как использовать:*\n"
            "1. Нажмите '📸 Загрузить фото'\n"
            "2. Отправьте фотографию\n"
            "3. Опишите что изменить\n"
            "4. Получите результат через 30-60 секунд",
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN
        )
        query.answer()
    except Exception as e:
        debug_log(f"Ошибка: {e}")

# ============== ОБРАБОТЧИКИ СООБЩЕНИЙ ==============
def handle_photo(update, context):
    """Обработчик фотографий"""
    try:
        user_id = update.message.from_user.id
        
        # Если пользователь просто отправил фото без выбора модели
        if user_id not in user_states:
            keyboard = [[InlineKeyboardButton("✨ Редактировать это фото", callback_data='model_image-to-image')]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            update.message.reply_text(
                "📸 *Фото получено!*\n\n"
                "Что вы хотите сделать с этим изображением?",
                reply_markup=reply_markup,
                parse_mode=ParseMode.MARKDOWN
            )
            return
        
        state = user_states[user_id]
        
        # Получаем фото
        photo_file = update.message.photo[-1].get_file()
        image_data = BytesIO()
        photo_file.download(out=image_data)
        image_data.seek(0)
        
        # Сохраняем в состояние
        state['image_data'] = image_data
        state['step'] = 'waiting_prompt'
        
        # Отправляем сообщение о следующем шаге
        model_info = AVAILABLE_MODELS[state['model']]
        price = model_info['price']
        
        if state.get('free_generation'):
            price_text = "(бесплатная попытка)"
        else:
            price_text = f"({price} руб)"
        
        update.message.reply_text(
            f"✅ *Фото получено!* {price_text}\n\n"
            "Теперь опишите что сделать с изображением:\n\n"
            "*Примеры запросов:*\n"
            "• Сделай в стиле аниме\n"
            "• Улучши качество, добавь детали\n"
            "• Убери фон, оставь только человека\n"
            "• Добавь солнечный свет и тени\n"
            "• Сделай пиксель-арт\n"
            "• Преврати в картину маслом",
            parse_mode=ParseMode.MARKDOWN
        )
        
    except Exception as e:
        debug_log(f"Ошибка обработки фото: {e}")
        update.message.reply_text("❌ Ошибка загрузки изображения")

def handle_text(update, context):
    """Обработчик текстовых сообщений"""
    try:
        user_id = update.message.from_user.id
        text = update.message.text.strip()
        
        if text.startswith('/'):
            return
        
        if user_id not in user_states:
            update.message.reply_text("🤔 Сначала выберите действие через меню /start")
            return
        
        state = user_states[user_id]
        
        if state.get('step') == 'waiting_prompt':
            # Проверяем баланс для платных запросов
            model_info = AVAILABLE_MODELS[state['model']]
            
            if not state.get('free_generation'):
                price = model_info['price']
                balance = get_user_balance(user_id)
                
                if balance < price:
                    update.message.reply_text(
                        f"❌ *Недостаточно средств!*\n\n"
                        f"Нужно: {price} руб\n"
                        f"Ваш баланс: {balance} руб\n\n"
                        f"Используйте /topup для пополнения",
                        parse_mode=ParseMode.MARKDOWN
                    )
                    user_states[user_id] = {}
                    return
                
                # Списываем средства
                user_balances[user_id] -= price
                get_user_stats(user_id)['total_spent'] += price
            
            # Увеличиваем счетчик использования
            increment_usage(user_id, state['model'])
            
            # Начинаем обработку
            state['prompt'] = text
            state['step'] = 'processing'
            
            # Отправляем сообщение о начале обработки
            processing_msg = update.message.reply_text(
                "🔄 *Обрабатываю запрос...*\n\n"
                "⏳ Ожидание: 30-60 секунд\n"
                "✍️ Запрос: " + (text[:50] + "..." if len(text) > 50 else text)
            )
            state['processing_msg_id'] = processing_msg.message_id
            
            # Запускаем обработку в отдельном потоке
            threading.Thread(
                target=process_generation,
                args=(update, user_id, context),
                daemon=True
            ).start()
            
        elif state.get('step') == 'waiting_input':
            # Для текстовых моделей (без изображения)
            model_info = AVAILABLE_MODELS[state['model']]
            
            if model_info['requires'] == "image":
                update.message.reply_text("📸 Для этой модели требуется изображение")
                return
            
            # Проверяем баланс
            if not state.get('free_generation'):
                price = model_info['price']
                balance = get_user_balance(user_id)
                
                if balance < price:
                    update.message.reply_text(
                        f"❌ *Недостаточно средств!*\n\n"
                        f"Нужно: {price} руб\n"
                        f"Ваш баланс: {balance} руб\n\n"
                        f"Используйте /topup для пополнения",
                        parse_mode=ParseMode.MARKDOWN
                    )
                    user_states[user_id] = {}
                    return
                
                user_balances[user_id] -= price
                get_user_stats(user_id)['total_spent'] += price
            
            increment_usage(user_id, state['model'])
            
            # Начинаем обработку
            state['prompt'] = text
            state['step'] = 'processing'
            
            processing_msg = update.message.reply_text(
                f"🔄 *Создаю {model_info['type']}...*\n\n"
                f"⏳ Ожидание: 1-2 минуты\n"
                f"✍️ Запрос: " + (text[:50] + "..." if len(text) > 50 else text)
            )
            state['processing_msg_id'] = processing_msg.message_id
            
            threading.Thread(
                target=process_generation,
                args=(update, user_id, context),
                daemon=True
            ).start()
            
        else:
            update.message.reply_text("🤔 Сначала выберите действие через меню")
            user_states[user_id] = {}
            
    except Exception as e:
        debug_log(f"Ошибка обработки текста: {e}")
        update.message.reply_text("❌ Ошибка обработки запроса")

def process_generation(update, user_id, context):
    """Обрабатывает генерацию"""
    try:
        state = user_states.get(user_id, {})
        if not state:
            return
        
        model_key = state.get('model')
        prompt = state.get('prompt', '')
        image_data = state.get('image_data')
        
        # Удаляем сообщение "Обрабатываю..."
        if 'processing_msg_id' in state:
            try:
                context.bot.delete_message(
                    chat_id=update.effective_chat.id,
                    message_id=state['processing_msg_id']
                )
            except:
                pass
        
        # Отправляем сообщение о начале генерации
        status_msg = context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="⚙️ *Отправляю запрос в AI-систему...*\n\nПожалуйста, подождите ⏳",
            parse_mode=ParseMode.MARKDOWN
        )
        
        # Конвертируем изображение если нужно
        image_base64 = None
        if image_data:
            image_base64 = image_to_base64(image_data)
        
        # Создаем задачу
        model_info = AVAILABLE_MODELS[model_key]
        task_result = create_generation_task(model_info, prompt, image_base64)
        
        if not task_result:
            context.bot.delete_message(
                chat_id=update.effective_chat.id,
                message_id=status_msg.message_id
            )
            context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="❌ *Не удалось создать задачу*\n\nПопробуйте позже или другой запрос",
                parse_mode=ParseMode.MARKDOWN
            )
            user_states[user_id] = {}
            return
        
        if task_result.get("task_id"):
            # Асинхронная задача
            task_id = task_result["task_id"]
            estimated_time = task_result.get("estimated_time", 45)
            
            # Обновляем статус
            context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=status_msg.message_id,
                text=f"⏳ *Задача создана!*\n\nID: `{task_id}`\nОжидание: {estimated_time} секунд",
                parse_mode=ParseMode.MARKDOWN
            )
            
            # Ждем завершения
            result_url = wait_for_task_completion(task_id, model_info["type"])
            
            if not result_url:
                context.bot.delete_message(
                    chat_id=update.effective_chat.id,
                    message_id=status_msg.message_id
                )
                context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text="❌ *Не удалось получить результат*\n\nПопробуйте другой запрос",
                    parse_mode=ParseMode.MARKDOWN
                )
                user_states[user_id] = {}
                return
            
            # Скачиваем результат
            file_data = download_file(result_url)
            
            if not file_data:
                context.bot.delete_message(
                    chat_id=update.effective_chat.id,
                    message_id=status_msg.message_id
                )
                context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text="❌ *Ошибка загрузки результата*\n\nПопробуйте позже",
                    parse_mode=ParseMode.MARKDOWN
                )
                user_states[user_id] = {}
                return
            
        else:
            # Прямой результат
            result_url = task_result["result"]
            file_data = download_file(result_url)
            
            if not file_data:
                context.bot.delete_message(
                    chat_id=update.effective_chat.id,
                    message_id=status_msg.message_id
                )
                context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text="❌ *Ошибка загрузки результата*",
                    parse_mode=ParseMode.MARKDOWN
                )
                user_states[user_id] = {}
                return
        
        # Удаляем сообщение о статусе
        context.bot.delete_message(
            chat_id=update.effective_chat.id,
            message_id=status_msg.message_id
        )
        
        # Отправляем результат
        send_result(update, file_data, model_info, prompt, context, state.get('free_generation', False))
        
        # Очищаем состояние
        user_states[user_id] = {}
        
    except Exception as e:
        debug_log(f"Ошибка process_generation: {e}")
        
        try:
            context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="❌ *Произошла ошибка при генерации*\n\nПопробуйте еще раз",
                parse_mode=ParseMode.MARKDOWN
            )
        except:
            pass
        
        user_states[user_id] = {}

def send_result(update, file_data, model_info, prompt, context, free_generation=False):
    """Отправляет результат пользователю"""
    try:
        chat_id = update.effective_chat.id
        
        # Формируем подпись
        caption = f"✅ *{model_info['name']}*\n\n"
        
        if free_generation:
            caption += "🎁 *Бесплатная генерация!*\n\n"
        else:
            caption += f"💰 Стоимость: {model_info['price']} руб\n\n"
        
        if prompt and len(prompt) < 100:
            caption += f"✍️ *Запрос:* {prompt}\n\n"
        
        caption += "✨ Готово! Что дальше?\n"
        caption += "• Попробовать другой запрос\n"
        caption += "• Исправить результат\n"
        caption += "• Создать видео из изображения\n\n"
        caption += "Используйте /start для нового запроса"
        
        if model_info["type"] == "image":
            # Сохраняем временный файл
            temp_file = save_to_temp_file(file_data, '.jpg')
            if temp_file:
                try:
                    with open(temp_file, 'rb') as f:
                        context.bot.send_photo(
                            chat_id=chat_id,
                            photo=f,
                            caption=caption,
                            parse_mode=ParseMode.MARKDOWN
                        )
                finally:
                    if os.path.exists(temp_file):
                        os.unlink(temp_file)
            else:
                context.bot.send_message(
                    chat_id=chat_id,
                    text="✅ *Изображение создано!*\n\n" + caption,
                    parse_mode=ParseMode.MARKDOWN
                )
                
        elif model_info["type"] == "video":
            temp_file = save_to_temp_file(file_data, '.mp4')
            if temp_file:
                try:
                    with open(temp_file, 'rb') as f:
                        context.bot.send_video(
                            chat_id=chat_id,
                            video=f,
                            caption=caption,
                            parse_mode=ParseMode.MARKDOWN,
                            supports_streaming=True
                        )
                finally:
                    if os.path.exists(temp_file):
                        os.unlink(temp_file)
            else:
                context.bot.send_message(
                    chat_id=chat_id,
                    text="✅ *Видео создано!*\n\n" + caption,
                    parse_mode=ParseMode.MARKDOWN
                )
                
    except Exception as e:
        debug_log(f"Ошибка отправки результата: {e}")
        context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="✅ *Результат готов!*\n\nК сожалению, не удалось отправить файл. Попробуйте еще раз.",
            parse_mode=ParseMode.MARKDOWN
        )

# ============== МЕНЮ БАЛАНСА И СТАТИСТИКИ ==============
def show_balance_menu(update, context):
    """Меню баланса"""
    try:
        user_id = update.callback_query.from_user.id
        balance = get_user_balance(user_id)
        stats = get_user_stats(user_id)
        
        text = f"💰 *Ваш баланс:* {balance} руб\n\n"
        text += "*Бесплатные попытки:*\n"
        
        for model_key, model_info in AVAILABLE_MODELS.items():
            used = stats.get(model_key, 0)
            free_limit = model_info['free_limit']
            remaining = max(0, free_limit - used)
            text += f"• {model_info['name']}: {remaining}/{free_limit}\n"
        
        text += f"\n*Всего потрачено:* {stats['total_spent']} руб"
        
        keyboard = [
            [InlineKeyboardButton("💳 Пополнить баланс", callback_data='menu_topup')],
            [InlineKeyboardButton("🔙 Назад", callback_data='menu_back')]
        ]
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        query = update.callback_query
        query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
        query.answer()
        
    except Exception as e:
        debug_log(f"Ошибка: {e}")

def show_topup_menu(update, context):
    """Меню пополнения"""
    keyboard = [
        [InlineKeyboardButton("100 руб", callback_data='topup_100'),
         InlineKeyboardButton("300 руб", callback_data='topup_300')],
        [InlineKeyboardButton("500 руб", callback_data='topup_500'),
         InlineKeyboardButton("1000 руб", callback_data='topup_1000')],
        [InlineKeyboardButton("🔙 Назад", callback_data='menu_back')]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    query = update.callback_query
    query.edit_message_text(
        "💳 *Пополнение баланса*\n\n"
        "Выберите сумму для пополнения:\n\n"
        "*После оплаты:*\n"
        "1. Нажмите '✅ Я оплатил'\n"
        "2. Средства поступят на баланс\n"
        "3. Можете использовать платные генерации\n\n"
        "*Тестовый режим:*\n"
        "Используйте тестовую карту: 5555 5555 5555 4444",
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN
    )
    query.answer()

def show_stats_menu(update, context):
    """Меню статистики"""
    try:
        user_id = update.callback_query.from_user.id
        stats = get_user_stats(user_id)
        
        text = "📊 *Ваша статистика*\n\n"
        text += "*Использовано генераций:*\n"
        
        total_used = 0
        for model_key, model_info in AVAILABLE_MODELS.items():
            used = stats.get(model_key, 0)
            total_used += used
            text += f"• {model_info['name']}: {used} раз\n"
        
        text += f"\n*Всего генераций:* {total_used}\n"
        text += f"*Всего потрачено:* {stats['total_spent']} руб\n"
        
        days_used = int((time.time() - stats['created_at']) / 86400)
        text += f"*Дней использования:* {days_used}"
        
        keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data='menu_back')]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        query = update.callback_query
        query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
        query.answer()
        
    except Exception as e:
        debug_log(f"Ошибка: {e}")

def show_help_menu(update, context):
    """Меню помощи"""
    help_text = """
🎨 *AI Photograph Bot - Помощь*

*Доступные функции:*
1. *🖼️ Создание изображений*
   • Из текста: 50 руб
   • Редактирование фото: 75 руб

2. *🎬 Создание видео*
   • Из текста: 150 руб
   • Из изображения: 100 руб

*Как редактировать фото:*
1. Выберите "✨ Редактировать фото"
2. Загрузите фотографию
3. Опишите что изменить
4. Получите результат через 30-60 сек

*Примеры запросов для редактирования:*
• "Сделай в стиле аниме"
• "Улучши качество фото"
• "Убери фон, оставь только человека"
• "Добавь солнечный свет"
• "Сделай пиксель-арт версию"
• "Преврати в картину маслом"

*Оплата и баланс:*
• У каждого типа генерации есть бесплатные попытки
• После их исчерпания нужна оплата
• Для теста используйте карту 5555 5555 5555 4444

*Команды:*
/start - Главное меню
/balance - Мой баланс
/topup - Пополнить баланс
/stats - Статистика
/help - Эта справка
"""
    
    keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data='menu_back')]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    query = update.callback_query
    query.edit_message_text(help_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
    query.answer()

# ============== ОПЛАТА ==============
def create_yookassa_payment(amount_rub, description, user_id):
    """Создает платеж в ЮКассе"""
    try:
        # Тестовый режим (если не настроены реальные ключи)
        if YOOKASSA_SHOP_ID == "test_shop_id" or YOOKASSA_SECRET_KEY == "test_secret_key":
            payment_id = f"demo_{int(time.time())}_{user_id}"
            confirmation_url = "https://yoomoney.ru/checkout/payments/v2/contract?orderId=DEMO"
            
            pending_payments[payment_id] = {
                "id": payment_id,
                "status": "pending",
                "amount": amount_rub,
                "user_id": user_id,
                "created_at": time.time(),
                "demo": True
            }
            
            return payment_id, confirmation_url
        
        # Реальный платеж
        idempotence_key = str(uuid.uuid4())
        
        payload = {
            "amount": {
                "value": f"{amount_rub:.2f}",
                "currency": "RUB"
            },
            "confirmation": {
                "type": "redirect",
                "return_url": f"https://t.me/{context.bot.username}"
            },
            "capture": True,
            "description": description[:128],
            "metadata": {
                "user_id": str(user_id)
            }
        }
        
        headers = {
            "Content-Type": "application/json",
            "Idempotence-Key": idempotence_key,
        }
        
        auth = (YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY)
        
        response = requests.post(
            YOOKASSA_PAYMENT_URL,
            headers=headers,
            json=payload,
            auth=auth,
            timeout=30
        )
        
        if response.status_code in [200, 201]:
            payment_data = response.json()
            payment_id = payment_data.get("id")
            confirmation_url = payment_data.get("confirmation", {}).get("confirmation_url")
            
            pending_payments[payment_id] = {
                "id": payment_id,
                "status": "pending",
                "amount": amount_rub,
                "user_id": user_id,
                "confirmation_url": confirmation_url,
                "created_at": time.time(),
                "demo": False
            }
            
            return payment_id, confirmation_url
        else:
            debug_log(f"Ошибка ЮКассы: {response.status_code}")
            return None, None
            
    except Exception as e:
        debug_log(f"Ошибка создания платежа: {e}")
        return None, None

def process_topup(update, context, user_id, amount):
    """Обрабатывает пополнение"""
    try:
        query = update.callback_query
        
        description = f"Пополнение баланса на {amount} руб"
        payment_id, payment_url = create_yookassa_payment(amount, description, user_id)
        
        if payment_id and payment_url:
            keyboard = [[InlineKeyboardButton("✅ Я оплатил", callback_data=f'check_payment_{payment_id}')]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            message_text = (
                f"💳 *Оплата {amount} руб*\n\n"
                f"Для оплаты перейдите по ссылке:\n{payment_url}\n\n"
                "*Инструкция:*\n"
                "1. Перейдите по ссылке выше\n"
                "2. Введите данные карты:\n"
                "   • Номер: `5555 5555 5555 4444` (для теста)\n"
                "   • Срок: любая будущая дата\n"
                "   • CVC: любые 3 цифры\n"
                "3. После оплаты нажмите '✅ Я оплатил'\n\n"
                "⚠️ *Тестовый платеж!* Деньги не списываются."
            )
            
            query.edit_message_text(
                message_text,
                reply_markup=reply_markup,
                parse_mode=ParseMode.MARKDOWN
            )
        else:
            query.edit_message_text(
                "❌ Ошибка создания платежа",
                parse_mode=ParseMode.MARKDOWN
            )
        query.answer()
        
    except Exception as e:
        debug_log(f"Ошибка: {e}")

def check_payment_status_handler(update, context, payment_id):
    """Проверяет статус платежа"""
    try:
        query = update.callback_query
        
        if payment_id not in pending_payments:
            query.edit_message_text("❌ Платеж не найден")
            query.answer()
            return
        
        payment_info = pending_payments[payment_id]
        
        # Демо-платеж
        payment_info["status"] = "succeeded"
        user_id = payment_info["user_id"]
        amount = payment_info["amount"]
        
        user_balances[user_id] = user_balances.get(user_id, 0) + amount
        
        query.edit_message_text(
            f"✅ *Оплата успешно проведена!*\n\n"
            f"Сумма: {amount} руб\n"
            f"Новый баланс: {user_balances[user_id]} руб",
            parse_mode=ParseMode.MARKDOWN
        )
        query.answer()
        
    except Exception as e:
        debug_log(f"Ошибка: {e}")

# ============== ОБРАБОТЧИК КОЛБЭКОВ ==============
def handle_menu_selection(update, context):
    """Обработчик меню"""
    try:
        query = update.callback_query
        user_id = query.from_user.id
        data = query.data
        
        query.answer()
        
        if data == 'menu_generate':
            show_generation_menu(update, context)
        elif data == 'menu_video':
            show_video_menu(update, context)
        elif data == 'menu_balance':
            show_balance_menu(update, context)
        elif data == 'menu_topup':
            show_topup_menu(update, context)
        elif data == 'menu_stats':
            show_stats_menu(update, context)
        elif data == 'menu_help':
            show_help_menu(update, context)
        elif data == 'menu_back':
            show_main_menu(update, context)
        elif data == 'upload_photo':
            # Пользователь хочет загрузить фото для редактирования
            user_states[user_id] = {
                'model': 'image-to-image',
                'step': 'waiting_image',
                'free_generation': can_use_for_free(user_id, 'image-to-image')
            }
            query.edit_message_text(
                "📸 *Загрузка фотографии*\n\n"
                "Отправьте фотографию, которую хотите отредактировать.\n\n"
                "*Рекомендации:*\n"
                "• Хорошее качество изображения\n"
                "• Четкий основной объект\n"
                "• Размер до 10MB\n"
                "• Форматы: JPG, PNG",
                parse_mode=ParseMode.MARKDOWN
            )
        elif data.startswith('model_'):
            model_key = data.replace('model_', '')
            handle_model_selection(update, context, user_id, model_key)
        elif data.startswith('topup_'):
            amount = int(data.replace('topup_', ''))
            process_topup(update, context, user_id, amount)
        elif data.startswith('check_payment_'):
            payment_id = data.replace('check_payment_', '')
            check_payment_status_handler(update, context, payment_id)
            
    except Exception as e:
        debug_log(f"Ошибка меню: {e}")

# ============== КОМАНДЫ ==============
def start(update, context):
    """Команда /start"""
    user_id = update.message.from_user.id
    user_states[user_id] = {}
    show_main_menu(update, context)

def balance_command(update, context):
    """Команда /balance"""
    try:
        user_id = update.effective_user.id
        balance = get_user_balance(user_id)
        
        keyboard = [[InlineKeyboardButton("💳 Пополнить баланс", callback_data='menu_topup')]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        update.message.reply_text(
            f"💰 *Ваш баланс:* {balance} руб\n\n"
            f"Используйте кнопку ниже для пополнения:",
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        debug_log(f"Ошибка: {e}")

def help_command(update, context):
    """Команда /help"""
    update.message.reply_text(
        "🎨 *AI Photograph Bot*\n\n"
        "Используйте /start для открытия меню\n"
        "/balance - проверить баланс\n"
        "/topup - пополнить баланс\n"
        "/stats - статистика использования\n"
        "/help - эта справка\n\n"
        "Для начала работы отправьте /start",
        parse_mode=ParseMode.MARKDOWN
    )

def stats_command(update, context):
    """Команда /stats"""
    user_id = update.effective_user.id
    show_stats_menu(update, context)

def error_handler(update, context):
    """Обработчик ошибок"""
    try:
        debug_log(f"Ошибка: {context.error}")
        
        # Отправляем сообщение об ошибке администратору
        error_text = f"❌ Ошибка в боте:\n\n{context.error}"
        
        try:
            context.bot.send_message(chat_id=ADMIN_ID, text=error_text)
        except:
            pass
            
    except Exception as e:
        print(f"Ошибка в обработчике ошибок: {e}")

def main():
    """Основная функция"""
    print("="*60)
    print("🤖 AI Photograph Bot - Professional Edition")
    print("✨ Создание и редактирование изображений")
    print("💰 Интеграция с ЮКассой")
    print("🎨 Красивые диалоговые окна")
    print("="*60)
    
    # Проверяем наличие токенов
    if not TELEGRAM_BOT_TOKEN or not EVOLINK_API_KEY:
        print("❌ ОШИБКА: Токены не загружены!")
        print("💡 Создайте файл .env с переменными:")
        print("TELEGRAM_BOT_TOKEN=ваш_токен")
        print("EVOLINK_API_KEY=ваш_ключ")
        return
    
    print("✅ Токены загружены успешно")
    print(f"🤖 Запуск бота...")
    
    try:
        updater = Updater(
            token=TELEGRAM_BOT_TOKEN,
            use_context=True,
            request_kwargs={
                'read_timeout': 120,
                'connect_timeout': 60,
            }
        )
        
        dp = updater.dispatcher
        
        # Регистрация обработчиков команд
        dp.add_handler(CommandHandler("start", start))
        dp.add_handler(CommandHandler("balance", balance_command))
        dp.add_handler(CommandHandler("help", help_command))
        dp.add_handler(CommandHandler("stats", stats_command))
        
        # Регистрация обработчиков сообщений
        dp.add_handler(CallbackQueryHandler(handle_menu_selection))
        dp.add_handler(MessageHandler(Filters.photo, handle_photo))
        dp.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_text))
        
        # Обработчик ошибок
        dp.add_error_handler(error_handler)
        
        print("✅ Бот запущен успешно!")
        print("📱 Отправьте /start в Telegram")
        print("✨ Редактирование фото через Qwen AI")
        print("💰 Цена редактирования: 75 руб")
        print("🎨 2 бесплатных попытки")
        print("💳 Тестовая карта: 5555 5555 5555 4444")
        print("="*60)
        print("🛑 Для остановки нажмите Ctrl+C")
        
        # Запуск бота
        updater.start_polling(
            poll_interval=2.0,
            timeout=60,
            drop_pending_updates=True,
            allowed_updates=['message', 'callback_query']
        )
        
        updater.idle()
        
    except Exception as e:
        print(f"❌ Критическая ошибка при запуске: {e}")
        import traceback
        traceback.print_exc()

if __name__ == '__main__':
    main()
