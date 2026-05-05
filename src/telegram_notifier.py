import requests
import logging

logger = logging.getLogger(__name__)

def send_telegram(bot_token: str, chat_id: str, message: str) -> bool:
    """Send a message to a Telegram chat via bot."""
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "Markdown"
    }
    try:
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code != 200:
            logger.error(f"Telegram error: {resp.text}")
            return False
        logger.info("Telegram message sent successfully.")
        return True
    except Exception as e:
        logger.error(f"Telegram request failed: {e}")
        return False
