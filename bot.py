import logging
import time
import requests
import tempfile
import os
from telegram.ext import Updater, CommandHandler, MessageHandler, CallbackQueryHandler, Filters
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ParseMode, ChatAction
from io import BytesIO
import urllib3
import signal
import sys
import base64
import json
import ssl
import uuid
from datetime import datetime
import threading
import re

# Отключение SSL проверок для тестов
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
ssl._create_default_https_context = ssl._create_unverified_context

# ============== НАСТРОЙКИ ==============
TELEGRAM_BOT_TOKEN = "8308392046:AAEv55wxnCdx4HD2Iep_XzdyFoF0OPiq2t0"
EVOLINK_API_KEY = "sk-14XAeyFRrRi3T2SlrOS2SzRqbCUW6EheU5DsmRW6XYD1Sil4"

# Настройки ЮКассы - УКАЖИТЕ ВАШИ РЕАЛЬНЫЕ ДАННЫЕ!
YOOKASSA_SHOP_ID = "1245333"  # Идентификатор магазина из ЮКассы
YOOKASSA_SECRET_KEY = "live_V4IUU6ybHenE4aL8DvlQJCKyu2Pxn9VBZ5L-3YoocJc"  # Секретный ключ из ЮКассы
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

BOT_USERNAME = "AI_Photograph_Bot"

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

# Хранилища данных
user_states = {}
user_stats = {}
user_balances = {}
pending_payments = {}
user_images = {}

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

# ============== ФУНКЦИИ API ==============
def debug_log(message):
    logger.debug(message)
    print(f"[DEBUG] {time.strftime('%H:%M:%S')} - {message}")

def get_user_stats(user_id):
    if user_id not in user_stats:
        user_stats[user_id] = {model_key: 0 for model_key in AVAILABLE_MODELS}
        user_stats[user_id]['total_spent'] = 0
        user_stats[user_id]['created_at'] = time.time()
    return user_stats[user_id]

def get_user_balance(user_id):
    if user_id not in user_balances:
        user_balances[user_id] = 0
    return user_balances[user_id]

def can_use_for_free(user_id, model_key):
    stats = get_user_stats(user_id)
    free_limit = AVAILABLE_MODELS[model_key]['free_limit']
    return stats[model_key] < free_limit

def increment_usage(user_id, model_key):
    stats = get_user_stats(user_id)
    stats[model_key] += 1

def image_to_base64(image_data):
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

def create_generation_task(model_info, prompt, image_base64=None):
    """Создает задачу генерации через Evolink API"""
    try:
        api_model = model_info.get("api_model")
        endpoint = model_info.get("endpoint")
        
        debug_log(f"Создаю задачу для модели {api_model}")
        
        url = f"https://api.evolink.ai/v1/{endpoint}"
        headers = {
            "Authorization": f"Bearer {EVOLINK_API_KEY}",
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        
        # Формируем payload в зависимости от модели
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
        
        debug_log(f"URL: {url}")
        
        # Добавляем retry логику
        max_retries = 3
        for attempt in range(max_retries):
            try:
                response = requests.post(
                    url, 
                    headers=headers, 
                    json=payload, 
                    timeout=60, 
                    verify=False
                )
                
                debug_log(f"Ответ API: {response.status_code}")
                
                if response.status_code == 200:
                    data = response.json()
                    
                    if "id" in data:
                        task_id = data["id"]
                        estimated_time = data.get('task_info', {}).get('estimated_time', 45)
                        
                        debug_log(f"Задача создана: {task_id}")
                        
                        return {
                            "type": model_info["type"],
                            "task_id": task_id,
                            "result": None,
                            "estimated_time": estimated_time
                        }
                    elif "data" in data and isinstance(data["data"], list) and len(data["data"]) > 0:
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
                        
                else:
                    debug_log(f"Ошибка API {response.status_code}: {response.text}")
                    
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
                debug_log(f"Попытка {attempt + 1} не удалась: {e}")
                if attempt < max_retries - 1:
                    time.sleep(2)
                    continue
                else:
                    raise
                    
        return None
        
    except Exception as e:
        debug_log(f"Ошибка создания задачи: {str(e)}")
        import traceback
        traceback.print_exc()
        return None

# Остальные функции остаются такими же...

# ============== ЗАПУСК ==============
def main():
    """Основная функция"""
    print("="*60)
    print("🤖 AI Photograph Bot - Professional Edition")
    print("✨ Создание и редактирование изображений")
    print("💰 Интеграция с ЮКассой")
    print("🎨 Красивые диалоговые окна")
    print("="*60)
    
    def signal_handler(sig, frame):
        print("\n\n🔴 Бот останавливается...")
        sys.exit(0)
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    try:
        print("Запуск бота...")
        print(f"API ключ: {EVOLINK_API_KEY[:15]}...")
        
        # Проверка API
        print("Проверка подключения к API...")
        try:
            test_response = requests.get(
                "https://api.evolink.ai/v1/models",
                headers={
                    "Authorization": f"Bearer {EVOLINK_API_KEY}",
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                },
                timeout=10,
                verify=False
            )
            if test_response.status_code == 200:
                print("✅ API подключение успешно")
            else:
                print(f"⚠️ API код: {test_response.status_code}")
        except Exception as e:
            print(f"⚠️ Ошибка API: {e}")
        
        # Настройки для обхода блокировок
        request_kwargs = {
            'read_timeout': 120,
            'connect_timeout': 60,
            'pool_timeout': 60,
            'proxy_url': None,  # Если нужен прокси
        }
        
        # Запуск бота
        updater = Updater(
            token=TELEGRAM_BOT_TOKEN,
            use_context=True,
            request_kwargs=request_kwargs
        )
        
        dp = updater.dispatcher
        
        # Регистрация обработчиков
        dp.add_handler(CommandHandler("start", start))
        dp.add_handler(CommandHandler("balance", balance_command))
        dp.add_handler(CommandHandler("help", help_command))
        
        dp.add_handler(CallbackQueryHandler(handle_menu_selection))
        dp.add_handler(MessageHandler(Filters.photo, handle_photo))
        dp.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_text))
        
        # Улучшенный обработчик ошибок
        def error_handler(update, context):
            try:
                error_msg = str(context.error)
                debug_log(f"Ошибка бота: {error_msg}")
                
                # Отправляем сообщение пользователю только если это не ошибка соединения
                if "Connection" not in error_msg and "RemoteDisconnected" not in error_msg:
                    if update and update.effective_chat:
                        context.bot.send_message(
                            chat_id=update.effective_chat.id,
                            text="❌ Произошла ошибка. Пожалуйста, попробуйте еще раз."
                        )
            except:
                pass
        
        dp.add_error_handler(error_handler)
        
        print("\n✅ Бот запущен!")
        print("📱 Отправьте /start в Telegram")
        print("✨ Редактирование фото через Qwen AI")
        print("💰 Цена редактирования: 75 руб")
        print("🎨 2 бесплатных попытки")
        print("💳 Тестовая карта: 5555 5555 5555 4444")
        print("="*60)
        print("Логи сохраняются в файл bot.log")
        
        # Запуск с улучшенными настройками
        updater.start_polling(
            poll_interval=3.0,
            timeout=120,
            drop_pending_updates=True,
            allowed_updates=['message', 'callback_query'],
            bootstrap_retries=5,
            read_latency=5.0
        )
        
        # Бесконечный цикл
        updater.idle()
        
    except Exception as e:
        print(f"❌ Критическая ошибка: {e}")
        import traceback
        traceback.print_exc()
        input("Нажмите Enter для выхода...")

if __name__ == '__main__':
    main()
