import requests
import threading
from config import Config


class Notifier:
    def __init__(self):
        self.enabled = bool(Config.TELEGRAM_BOT_TOKEN and Config.TELEGRAM_CHAT_ID)
        self.token = Config.TELEGRAM_BOT_TOKEN
        self.chat_id = Config.TELEGRAM_CHAT_ID
        self._status_callback = None    # wird von main.py gesetzt
        self._reset_callback = None     # wird von main.py gesetzt
        self._closeall_callback = None  # wird von main.py gesetzt
        self._reset_pending = False     # Zwei-Stufen-Reset: /reset → /reset confirm
        self._closeall_pending = False  # Zwei-Stufen-Closeall
        self._last_update_id = 0
        if self.enabled:
            t = threading.Thread(target=self._poll_commands, daemon=True)
            t.start()

    def set_status_callback(self, callback):
        """Callback-Funktion die aufgerufen wird wenn /status kommt."""
        self._status_callback = callback

    def set_reset_callback(self, callback):
        """Callback fuer /reset confirm — setzt Paper-Balance zurueck auf 1000EUR."""
        self._reset_callback = callback

    def set_closeall_callback(self, callback):
        """Callback fuer /closeall confirm — schliesst alle offenen Positionen.
        Gibt list[dict] zurueck: [{symbol, pnl, strategy}, ...] oder None bei Fehler."""
        self._closeall_callback = callback

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
                    elif text == "/reset":
                        # Zwei-Stufen-Reset: Warnung zeigen, auf /reset confirm warten.
                        self._reset_pending = True
                        self.send("⚠️ *RESET ANGEFORDERT*\n\n"
                                  "Das löscht ALLE Positionen und Trade-Historie und setzt das "
                                  "Paper-Portfolio zurück auf *1000.00 EUR*.\n\n"
                                  "Zum Bestätigen innerhalb 60s senden:\n"
                                  "`/reset confirm`\n\n"
                                  "Zum Abbrechen: `/cancel`")
                        # Auto-expire nach 60s
                        def expire():
                            time.sleep(60)
                            if self._reset_pending:
                                self._reset_pending = False
                        threading.Thread(target=expire, daemon=True).start()
                    elif text == "/reset confirm":
                        if not self._reset_pending:
                            self.send("❌ Kein aktiver Reset-Request. Sende erst `/reset`.")
                        elif self._reset_callback:
                            self._reset_pending = False
                            new_balance = self._reset_callback()
                            if new_balance is not None:
                                self.send(f"✅ *RESET FERTIG*\n"
                                          f"Paper-Balance: *{new_balance:.2f} EUR*\n"
                                          f"Positionen: 0 | Trades: 0\n\n"
                                          f"_Bot trading fortgesetzt. Kein weiterer Auto-Reset bei Deploys._")
                            else:
                                self.send("❌ Reset fehlgeschlagen — check logs.")
                    elif text == "/closeall":
                        # Zwei-Stufen-Closeall: alle offenen Positionen zum Markt schliessen
                        self._closeall_pending = True
                        self.send("⚠️ *ALLE POSITIONEN SCHLIESSEN*\n\n"
                                  "Das schliesst ALLE offenen Positionen zum aktuellen Markt-Preis "
                                  "(mit realisiertem P&L in trades.json). Balance bleibt erhalten.\n\n"
                                  "Zum Bestätigen innerhalb 60s senden:\n"
                                  "`/closeall confirm`\n\n"
                                  "Zum Abbrechen: `/cancel`\n\n"
                                  "_Tipp: Danach `/reset` → `/reset confirm` fuer sauberen Neustart._")
                        def expire_ca():
                            time.sleep(60)
                            if self._closeall_pending:
                                self._closeall_pending = False
                        threading.Thread(target=expire_ca, daemon=True).start()
                    elif text == "/closeall confirm":
                        if not self._closeall_pending:
                            self.send("❌ Kein aktiver Closeall-Request. Sende erst `/closeall`.")
                        elif self._closeall_callback:
                            self._closeall_pending = False
                            result = self._closeall_callback()
                            if result is None:
                                self.send("❌ Closeall fehlgeschlagen — check logs.")
                            elif not result:
                                self.send("ℹ️ Keine offenen Positionen.")
                            else:
                                lines = ["✅ *ALLE POSITIONEN GESCHLOSSEN*\n"]
                                total_pnl = 0.0
                                for item in result:
                                    icon = "🟢" if item["pnl"] >= 0 else "🔴"
                                    lines.append(f"{icon} {item['symbol']} [{item['strategy']}]: "
                                                 f"`{item['pnl']:+.2f}EUR`")
                                    total_pnl += item["pnl"]
                                total_icon = "🟢" if total_pnl >= 0 else "🔴"
                                lines.append(f"\n{total_icon} *Gesamt realisiert: {total_pnl:+.2f}EUR*")
                                self.send("\n".join(lines))
                    elif text == "/cancel":
                        if self._reset_pending:
                            self._reset_pending = False
                            self.send("✅ Reset abgebrochen.")
                        elif self._closeall_pending:
                            self._closeall_pending = False
                            self.send("✅ Closeall abgebrochen.")
                        else:
                            self.send("ℹ️ Nichts zum Abbrechen.")
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
