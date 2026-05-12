import logging
from flask import Flask, request, jsonify
import requests
import json

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

# ============ НАЛАШТУВАННЯ - ЗМІНІТЬ ЦЕ! ============
BOT_TOKEN = "8599704700:AAGQuJTOTB-36FGtk66PAVce0x-CSy9PE0c"  # Вставте ваш токен
YOUR_USERNAME = "furious1008"  # Вставте ваш username на PythonAnywhere
# ===================================================

WEBAPP_URL = f"https://{YOUR_USERNAME}.pythonanywhere.com"
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

def send_message(chat_id, text, reply_markup=None):
    """Відправити повідомлення"""
    url = f"{TELEGRAM_API}/sendMessage"
    data = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML"
    }
    if reply_markup:
        data["reply_markup"] = json.dumps(reply_markup)
    
    try:
        response = requests.post(url, json=data)
        return response.json()
    except Exception as e:
        logging.error(f"Send error: {e}")
        return None

@app.route('/webhook', methods=['POST'])
def webhook():
    """Головний webhook для Telegram"""
    try:
        update = request.json
        logging.info(f"Received update: {update}")
        
        # Обробка повідомлень
        if 'message' in update:
            message = update['message']
            chat_id = message['chat']['id']
            text = message.get('text', '')
            
            if text == '/start':
                keyboard = {
                    "inline_keyboard": [[{
                        "text": "🛍️ ВІДКРИТИ KISIK MARKET",
                        "web_app": {"url": f"{WEBAPP_URL}?tg_id={chat_id}"}
                    }]]
                }
                
                welcome_text = f"""<b>👋 Вітаю!</b>

<b>🛍️ KISIK MARKET</b> - ваш помічник для пошуку та продажу

<b>👇 Натисніть кнопку, щоб відкрити маркет</b>"""
                
                send_message(chat_id, welcome_text, keyboard)
                
            elif text == '/help':
                help_text = """<b>📚 Довідка</b>

Ви отримуватимете сповіщення про:
• Нові відгуки на ваші товари
• Підтвердження угод
• Статус модерації
• Завершення угод

<b>📞 Підтримка:</b>
@kisik_support"""
                send_message(chat_id, help_text)
                
            elif text == '/id':
                send_message(chat_id, f"Ваш Telegram ID: <code>{chat_id}</code>")
                
            elif text == '/test':
                send_message(chat_id, "✅ Бот працює!")
        
        return jsonify({"status": "ok"}), 200
        
    except Exception as e:
        logging.error(f"Webhook error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/notify', methods=['POST'])
def notify():
    """Endpoint для сповіщень від вашого сайту"""
    try:
        data = request.json
        event = data.get('event')
        event_data = data.get('data', {})
        
        logging.info(f"Notification: {event} - {event_data}")
        
        if event == 'new_response':
            send_message(
                event_data.get('order_owner_tg_id'),
                f"""<b>🔔 Новий відгук! 🎯</b>

На ваше замовлення <b>№{event_data.get('order_number')}</b> відгукнувся користувач <b>{event_data.get('responder_name')}</b>

📱 Увійдіть в маркет, щоб підтвердити угоду!"""
            )
            
        elif event == 'deal_confirmed':
            send_message(
                event_data.get('buyer_tg_id'),
                f"""<b>🔔 Угоду підтверджено! 🎉</b>

Вашу угоду щодо товару <b>{event_data.get('product_name')}</b> підтверджено!

Тепер ви можете зв'язатися з продавцем для обговорення деталей."""
            )
            
        elif event == 'order_approved':
            send_message(
                event_data.get('user_tg_id'),
                f"""<b>🔔 Замовлення схвалено ✅</b>

Ваше замовлення <b>№{event_data.get('order_number')}</b> пройшло модерацію і тепер доступне в стрічці!

Очікуйте на відгуки від покупців."""
            )
            
        elif event == 'order_rejected':
            reason = event_data.get('reason', 'не вказана')
            send_message(
                event_data.get('user_tg_id'),
                f"""<b>🔔 Замовлення відхилено ❌</b>

Ваше замовлення <b>№{event_data.get('order_number')}</b> не пройшло модерацію.

<b>Причина:</b> {reason}

Ви можете створити нове замовлення, врахувавши зауваження."""
            )
            
        elif event == 'deal_completed':
            send_message(
                event_data.get('owner_tg_id'),
                f"""<b>🔔 Угоду завершено! 🎉</b>

Вашу угоду щодо товару <b>{event_data.get('product_name')}</b> завершено.

Дякуємо, що користуєтеся KISIK MARKET!"""
            )
            
        elif event == 'test':
            send_message(
                event_data.get('tg_id'),
                """<b>🔔 Тестове сповіщення 🧪</b>

Це тестове сповіщення! Якщо ви це бачите - бот працює правильно!"""
            )
            
        return jsonify({"status": "ok"}), 200
        
    except Exception as e:
        logging.error(f"Notify error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/health', methods=['GET'])
def health():
    """Перевірка стану"""
    return jsonify({"status": "ok", "service": "kisik-bot"})

@app.route('/set_webhook', methods=['GET'])
def set_webhook():
    """Встановити webhook (запустіть один раз)"""
    webhook_url = f"https://{YOUR_USERNAME}.pythonanywhere.com/webhook"
    
    response = requests.post(
        f"{TELEGRAM_API}/setWebhook",
        json={"url": webhook_url}
    )
    
    return jsonify(response.json())

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)