#!/usr/bin/env python3
"""
Telegram бот для mangabuff.ru: авторизация, список желаемых карт, мониторинг новых владельцев
С поддержкой автоматического переподключения и обработки ошибок.
"""

import os
import sys
import json
import html
import threading
import time
import random
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    print("❌ Установите python-dotenv: pip install python-dotenv")
    sys.exit(1)

try:
    import telebot
    from telebot import types
except ImportError:
    print("❌ Установите pyTelegramBotAPI: pip install pyTelegramBotAPI")
    sys.exit(1)

from mangabuff_auth import MangaBuffAuth

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
if not BOT_TOKEN:
    print("❌ BOT_TOKEN не найден в .env файле. Создайте .env с BOT_TOKEN=ваш_токен")
    sys.exit(1)

# Конфигурация мониторинга
CHECK_INTERVAL = 30  # секунд между проверками (можно изменить)
MAX_RETRIES = 3       # количество повторных попыток при ошибке

SESSIONS_FILE = Path(__file__).parent / "tg_sessions.json"
WANTED_CARDS_CACHE = Path(__file__).parent / "wanted_cards_cache.json"
OWNERS_STATE_FILE = Path(__file__).parent / "owners_state.json"

sessions = {}
owners_state = {}
monitoring_active = False
monitoring_thread = None

def load_sessions():
    global sessions
    if SESSIONS_FILE.exists():
        try:
            sessions = json.loads(SESSIONS_FILE.read_text(encoding="utf-8"))
        except:
            sessions = {}

def save_sessions():
    SESSIONS_FILE.write_text(json.dumps(sessions, ensure_ascii=False, indent=2), encoding="utf-8")

def load_owners_state():
    global owners_state
    if OWNERS_STATE_FILE.exists():
        try:
            owners_state = json.loads(OWNERS_STATE_FILE.read_text(encoding="utf-8"))
        except:
            owners_state = {}

def save_owners_state():
    OWNERS_STATE_FILE.write_text(json.dumps(owners_state, ensure_ascii=False, indent=2), encoding="utf-8")

load_sessions()
load_owners_state()

bot = telebot.TeleBot(BOT_TOKEN)

def get_auth_for_user(chat_id: int) -> MangaBuffAuth:
    """Возвращает свежий экземпляр MangaBuffAuth с загруженными cookies"""
    auth = MangaBuffAuth()
    if str(chat_id) in sessions:
        cookies = sessions[str(chat_id)].get('cookies', [])
        if cookies:
            auth.load_cookies(cookies)
    return auth

def save_user_session(chat_id: int, user_id: str, cookies: list):
    sessions[str(chat_id)] = {'user_id': user_id, 'cookies': cookies}
    save_sessions()

def clear_user_session(chat_id: int):
    if str(chat_id) in sessions:
        del sessions[str(chat_id)]
        save_sessions()

def get_keyboard():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add(
        types.KeyboardButton("📋 Мои желаемые карты"),
        types.KeyboardButton("🔔 Мониторинг карт"),
    )
    markup.add(
        types.KeyboardButton("📊 Статус"),
        types.KeyboardButton("👥 Аккаунты"),
    )
    return markup

# ---------- Мониторинг ----------
def monitoring_loop(chat_id):
    global monitoring_active
    print(f"[MONITOR] Запуск мониторинга для чата {chat_id}")
    
    # Получаем свой user_id
    auth = get_auth_for_user(chat_id)
    if not auth.is_authenticated():
        bot.send_message(chat_id, "❌ Вы не авторизованы. Используйте /login")
        monitoring_active = False
        return
    
    my_user_id = auth.get_user_id()
    if not my_user_id:
        bot.send_message(chat_id, "❌ Не удалось определить ваш user_id")
        monitoring_active = False
        return

    # Загружаем список карт из кэша
    if not WANTED_CARDS_CACHE.exists():
        bot.send_message(chat_id, "❌ Нет кэша с картами. Сначала нажмите «Мои желаемые карты».")
        monitoring_active = False
        return

    try:
        cache_data = json.loads(WANTED_CARDS_CACHE.read_text(encoding="utf-8"))
        cards = cache_data.get('cards', [])
    except Exception as e:
        bot.send_message(chat_id, f"❌ Ошибка чтения кэша карт: {e}")
        monitoring_active = False
        return

    if not cards:
        bot.send_message(chat_id, "📭 Нет карт для мониторинга.")
        monitoring_active = False
        return

    # Инициализируем состояние owners_state для отсутствующих карт
    for card in cards:
        card_id = card['card_id']
        if card_id not in owners_state:
            owners_state[card_id] = None
    save_owners_state()

    bot.send_message(chat_id, f"🔔 Мониторинг запущен для {len(cards)} карт. Проверка каждые {CHECK_INTERVAL} сек.")

    while monitoring_active:
        # Для каждой карты делаем запрос с повторными попытками
        for card in cards:
            if not monitoring_active:
                break
            card_id = card['card_id']
            card_name = card['name']
            card_url = f"{auth.BASE_URL}/cards/{card_id}/users"
            card_image_url = None

            # Пытаемся получить первого владельца с повторными попытками
            owner = None
            for attempt in range(MAX_RETRIES):
                try:
                    # Создаём свежую сессию для каждого запроса (чтобы избежать проблем с закрытым соединением)
                    fresh_auth = get_auth_for_user(chat_id)
                    owner = fresh_auth.get_first_owner(card_id)
                    if owner is not None:
                        # Если получили владельца, пробуем получить картинку карты (только если нет в кэше)
                        if not card_image_url:
                            try:
                                card_page = fresh_auth.session.get(f"{fresh_auth.BASE_URL}/cards/{card_id}")
                                if card_page.status_code == 200:
                                    img_match = re.search(r'<meta[^>]*property="og:image"[^>]*content="([^"]+)"', card_page.text)
                                    if img_match:
                                        card_image_url = img_match.group(1)
                                    else:
                                        img_match2 = re.search(r'<img[^>]*class="[^"]*card-show__image[^"]*"[^>]*src="([^"]+)"', card_page.text)
                                        if img_match2:
                                            card_image_url = img_match2.group(1)
                            except:
                                pass
                        break
                except Exception as e:
                    print(f"[MONITOR] Ошибка при получении владельца карты {card_id}, попытка {attempt+1}: {e}")
                    time.sleep(2 ** attempt)  # экспоненциальная задержка
                    continue
            if owner is None:
                continue

            # Игнорируем, если владелец – мы сами
            if owner.get('user_id') == my_user_id:
                continue

            current_owner_key = owner.get('card_user_id') or owner.get('user_id')
            previous = owners_state.get(card_id)

            if previous is None:
                # Первый запуск
                owners_state[card_id] = {
                    'owner_key': current_owner_key,
                    'username': owner['username'],
                    'user_id': owner['user_id'],
                    'card_user_id': owner['card_user_id']
                }
                save_owners_state()
            elif previous.get('owner_key') != current_owner_key:
                # Новый владелец!
                message = f"🆕 *Новый владелец карты!*\n\n"
                message += f"🎴 *Карта:* {html.escape(card_name)}\n"
                message += f"🔗 [Ссылка на карту]({card_url})\n\n"
                message += f"👤 *Новый владелец:* {html.escape(owner['username'])}\n"
                message += f"🔗 [Профиль владельца]({owner['profile_url']})\n"
                if owner['is_online']:
                    message += "🟢 Онлайн\n"
                if owner['handshake']:
                    message += "🤝 Готов к обмену\n"
                if owner['trade_lock']:
                    message += "🔒 Обмен заблокирован\n"

                try:
                    if card_image_url:
                        bot.send_photo(chat_id, card_image_url, caption=message, parse_mode='HTML')
                    else:
                        bot.send_message(chat_id, message, parse_mode='HTML', disable_web_page_preview=True)
                except Exception as e:
                    print(f"[MONITOR] Ошибка отправки уведомления: {e}")

                # Обновляем состояние
                owners_state[card_id] = {
                    'owner_key': current_owner_key,
                    'username': owner['username'],
                    'user_id': owner['user_id'],
                    'card_user_id': owner['card_user_id']
                }
                save_owners_state()

        # Пауза с возможностью досрочного выхода
        for _ in range(CHECK_INTERVAL):
            if not monitoring_active:
                break
            time.sleep(1)

    print("[MONITOR] Мониторинг остановлен")
    try:
        bot.send_message(chat_id, "🔕 Мониторинг остановлен.")
    except:
        pass

# ---------- Команды ----------
@bot.message_handler(commands=['start'])
def cmd_start(message):
    bot.send_message(
        message.chat.id,
        "🤖 Бот для mangabuff.ru\n\n"
        "Доступные команды:\n"
        "/login email password – войти в аккаунт\n"
        "/register username email password – зарегистрироваться\n"
        "/status – проверить статус сессии\n"
        "/logout – выйти (очистить сессию)\n"
        "/monitor_start – запустить мониторинг новых владельцев\n"
        "/monitor_stop – остановить мониторинг\n"
        "/monitor_status – статус мониторинга\n\n"
        "Используйте кнопки для управления.",
        reply_markup=get_keyboard()
    )

@bot.message_handler(commands=['login'])
def cmd_login(message):
    chat_id = message.chat.id
    args = message.text.split()
    if len(args) < 3:
        bot.send_message(chat_id, "❌ Использование: /login email password")
        return
    email = args[1]
    password = args[2]

    bot.send_message(chat_id, "⏳ Выполняю вход...")
    auth = MangaBuffAuth()
    success, result = auth.login(email, password)

    if success:
        user_id = result['user_id']
        save_user_session(chat_id, user_id, result['cookies'])
        bot.send_message(chat_id, f"✅ Успешный вход!\nВаш user_id: {user_id}\nСессия сохранена.")
    else:
        bot.send_message(chat_id, f"❌ Ошибка входа: {result}")

@bot.message_handler(commands=['register'])
def cmd_register(message):
    chat_id = message.chat.id
    args = message.text.split()
    if len(args) < 4:
        bot.send_message(chat_id, "❌ Использование: /register username email password")
        return
    username = args[1]
    email = args[2]
    password = args[3]

    bot.send_message(chat_id, "⏳ Регистрирую...")
    auth = MangaBuffAuth()
    success, msg = auth.register(username, email, password)

    if success:
        bot.send_message(chat_id, f"✅ {msg}\nТеперь войдите через /login {email} {password}")
    else:
        bot.send_message(chat_id, f"❌ Ошибка регистрации: {msg}")

@bot.message_handler(commands=['status'])
def cmd_status(message):
    chat_id = message.chat.id
    auth = get_auth_for_user(chat_id)
    if auth.is_authenticated():
        user_id = auth.get_user_id()
        bot.send_message(chat_id, f"🟢 Вы авторизованы\nUser ID: {user_id}")
    else:
        bot.send_message(chat_id, "🔴 Вы не авторизованы. Используйте /login")

@bot.message_handler(commands=['logout'])
def cmd_logout(message):
    chat_id = message.chat.id
    clear_user_session(chat_id)
    bot.send_message(chat_id, "👋 Вы вышли. Сессия очищена.")

@bot.message_handler(commands=['monitor_start'])
def cmd_monitor_start(message):
    global monitoring_active, monitoring_thread
    chat_id = message.chat.id
    if monitoring_active:
        bot.send_message(chat_id, "⚠️ Мониторинг уже запущен.")
        return
    auth = get_auth_for_user(chat_id)
    if not auth.is_authenticated():
        bot.send_message(chat_id, "❌ Вы не авторизованы. Используйте /login")
        return
    monitoring_active = True
    monitoring_thread = threading.Thread(target=monitoring_loop, args=(chat_id,), daemon=True)
    monitoring_thread.start()
    bot.send_message(chat_id, "✅ Мониторинг запущен. Уведомления будут приходить сюда.")

@bot.message_handler(commands=['monitor_stop'])
def cmd_monitor_stop(message):
    global monitoring_active
    if not monitoring_active:
        bot.send_message(message.chat.id, "ℹ️ Мониторинг не запущен.")
        return
    monitoring_active = False
    bot.send_message(message.chat.id, "⏹ Мониторинг остановлен.")

@bot.message_handler(commands=['monitor_status'])
def cmd_monitor_status(message):
    chat_id = message.chat.id
    if monitoring_active:
        bot.send_message(chat_id, "🟢 Мониторинг активен.")
    else:
        bot.send_message(chat_id, "🔴 Мониторинг не активен.")

# ---------- Обработка кнопок ----------
@bot.message_handler(func=lambda m: m.text in ["📋 Мои желаемые карты", "🔔 Мониторинг карт", "📊 Статус", "👥 Аккаунты"])
def handle_buttons(message):
    text = message.text
    chat_id = message.chat.id

    if text == "📋 Мои желаемые карты":
        auth = get_auth_for_user(chat_id)
        if not auth.is_authenticated():
            bot.send_message(chat_id, "❌ Вы не авторизованы. Используйте /login")
            return
        bot.send_message(chat_id, "⏳ Загружаю список...")
        cards = auth.get_my_wanted_cards()
        if not cards:
            bot.send_message(chat_id, "📭 У вас нет карт в разделе «Хочу».")
            return
        from datetime import datetime
        user_id = auth.get_user_id()
        cache_data = {
            "user_id": user_id,
            "timestamp": datetime.now().isoformat(),
            "count": len(cards),
            "cards": cards
        }
        WANTED_CARDS_CACHE.write_text(json.dumps(cache_data, ensure_ascii=False, indent=2), encoding="utf-8")
        bot.send_message(chat_id, "<b>📋 Ваши желаемые карты:</b>", parse_mode='HTML')
        for i, card in enumerate(cards, 1):
            card_text = (
                f"{i}. <b>{html.escape(card['name'])}</b>\n"
                f"   📖 Манга: {html.escape(card['manga'])}\n"
                f"   🔗 <a href='{card['url']}'>Ссылка на карту</a>"
            )
            bot.send_message(chat_id, card_text, parse_mode='HTML', disable_web_page_preview=True)

    elif text == "🔔 Мониторинг карт":
        if monitoring_active:
            bot.send_message(chat_id, "⚠️ Мониторинг уже запущен. Используйте /monitor_stop для остановки.")
        else:
            cmd_monitor_start(message)

    elif text == "📊 Статус":
        cmd_status(message)

    elif text == "👥 Аккаунты":
        auth = get_auth_for_user(chat_id)
        if auth.is_authenticated():
            bot.send_message(chat_id, "👥 Вы используете один аккаунт. Сессия активна.")
        else:
            bot.send_message(chat_id, "👥 Аккаунт не авторизован. Используйте /login")

# ---------- Запуск бота с авто-переподключением ----------
def run_bot():
    while True:
        try:
            print("✅ Бот запущен. Нажмите Ctrl+C для остановки.")
            bot.infinity_polling(timeout=60, long_polling_timeout=60)
        except Exception as e:
            print(f"❌ Ошибка соединения: {e}. Переподключение через 10 секунд...")
            time.sleep(10)

if __name__ == '__main__':
    run_bot()