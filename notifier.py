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
