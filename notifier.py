import requests
from config import Config


class Notifier:
    def __init__(self):
        self.enabled = bool(Config.TELEGRAM_BOT_TOKEN and Config.TELEGRAM_CHAT_ID)
        self.token = Config.TELEGRAM_BOT_TOKEN
        self.chat_id = Config.TELEGRAM_CHAT_ID

    def send(self, message: str):
        if not self.enabled:
            return
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        try:
            requests.post(url, json={"chat_id": self.chat_id, "text": message,
                          "parse_mode": "Markdown"}, timeout=10)
        except Exception:
            pass

    def notify_trade(self, side: str, symbol: str, volume: float, price: float,
                     reason: str, strategy: str, balance: float):
        emoji = "BUY" if side == "buy" else "SELL"
        self.send(f"{'BUY' if side == 'buy' else 'SELL'} *{symbol}*\n"
                  f"Vol: `{volume:.6f}` @ `{price:.2f}EUR`\n"
                  f"Strategy: _{strategy}_\n"
                  f"Reason: _{reason}_\n"
                  f"Balance: `{balance:.2f}EUR`")

    def notify_exit(self, symbol: str, exit_type: str, pnl: float, strategy: str):
        self.send(f"{'TP' if exit_type == 'take_profit' else 'SL'} *{symbol}*\n"
                  f"P&L: `{pnl:+.2f}EUR` [{strategy}]")
