import requests
import threading
from config import Config


class Notifier:
    def __init__(self):
        self.enabled = bool(Config.TELEGRAM_BOT_TOKEN and Config.TELEGRAM_CHAT_ID)
        self.token = Config.TELEGRAM_BOT_TOKEN
        self.chat_id = Config.TELEGRAM_CHAT_ID
        self._status_callback = None  # wird von main.py gesetzt
        self._last_update_id = 0
        if self.enabled:
            t = threading.Thread(target=self._poll_commands, daemon=True)
            t.start()

    def set_status_callback(self, callback):
        """Callback-Funktion die aufgerufen wird wenn /status kommt."""
        self._status_callback = callback

    def _poll_commands(self):
        """Pollt Telegram auf eingehende Nachrichten (Commands)."""
        import time
        while True:
            try:
                url = f"https://api.telegram.org/bot{self.token}/getUpdates"
                resp = requests.get(url, params={"offset": self._last_update_id + 1, "timeout": 30}, timeout=35)
                data = resp.json()
                for update in data.get("result", []):
                    self._last_update_id = update["update_id"]
                    msg = update.get("message", {})
                    text = msg.get("text", "").strip().lower()
                    if text in ("/status", "/portfolio", "/p"):
                        if self._status_callback:
                            self._status_callback()
            except Exception:
                pass
            time.sleep(1)

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
                     reason: str, strategy: str, balance: float, portfolio: float = 0,
                     daily_pnl: float = 0):
        action = "🟢 BUY" if side == "buy" else ("🔴 SHORT" if side == "short" else "⚪ SELL")
        self.send(f"{action} *{symbol}*\n"
                  f"Vol: `{volume:.6f}` @ `{price:.4f}EUR`\n"
                  f"Strategy: _{strategy}_\n"
                  f"Signal: _{reason}_\n"
                  f"Cash: `{balance:.2f}EUR`\n"
                  f"Portfolio: `{portfolio:.2f}EUR`\n"
                  f"Tages-P&L: `{daily_pnl:+.2f}%`")

    def notify_exit(self, symbol: str, exit_type: str, pnl: float, strategy: str,
                    portfolio: float = 0, daily_pnl: float = 0):
        icon = "✅" if exit_type == "take_profit" else "🛑"
        label = "TAKE PROFIT" if exit_type == "take_profit" else "STOP LOSS"
        self.send(f"{icon} {label} *{symbol}*\n"
                  f"P&L: `{pnl:+.2f}EUR` _{strategy}_\n"
                  f"Portfolio: `{portfolio:.2f}EUR`\n"
                  f"Tages-P&L: `{daily_pnl:+.2f}%`")
