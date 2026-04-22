import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    # Kraken API
    KRAKEN_API_KEY = os.getenv("KRAKEN_API_KEY", "")
    KRAKEN_API_SECRET = os.getenv("KRAKEN_API_SECRET", "")

    # Claude API
    ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

    # Trading
    TRADING_MODE = os.getenv("TRADING_MODE", "paper")
    INITIAL_CAPITAL = float(os.getenv("INITIAL_CAPITAL", 1000))

    # Risk (aggressive)
    MAX_RISK_PER_TRADE = float(os.getenv("MAX_RISK_PER_TRADE", 0.25))
    STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", 0.04))
    # TP bewusst nah dran — Partial-TP (2.5%/5%) + Trailing sollen vorher greifen.
    # Hart-Cap als Sicherheitsnetz, falls Preis durchschießt.
    TAKE_PROFIT_PCT = float(os.getenv("TAKE_PROFIT_PCT", 0.08))
    # Globaler Cap eher großzügig — echte Risikosteuerung macht der Korrelations-Cap
    # (max 3 LONG + 3 SHORT in risk_manager.py). Zusätzlich Gainer-Slot + DCA/Grid.
    MAX_OPEN_POSITIONS = int(os.getenv("MAX_OPEN_POSITIONS", 12))
    ROTATION_MIN_LEVERAGE = int(os.getenv("ROTATION_MIN_LEVERAGE", 2))
    DAILY_TARGET_PCT = float(os.getenv("DAILY_TARGET_PCT", 5.0))  # Tages-Ziel in %

    # Strategies
    ACTIVE_STRATEGIES = os.getenv("ACTIVE_STRATEGIES", "momentum,sentiment").split(",")

    # Momentum-Prioritätsliste: Coins die historisch gut mit Momentum funktionieren
    # Wird wöchentlich vom Auto-Optimizer befüllt — kein hardcoded Ban mehr
    # Alle anderen Coins sind weiterhin handelbar, kommen nur weiter hinten im Scan
    MOMENTUM_PRIORITY: list = []  # wird bei Startup aus optimizer_state.json geladen

    # Scanner
    SCAN_TOP_N = int(os.getenv("SCAN_TOP_N", 50))
    AUTO_PICK_COUNT = int(os.getenv("AUTO_PICK_COUNT", 10))

    # Gainer Slot (Binance top gainers)
    GAINER_SLOT_PCT = float(os.getenv("GAINER_SLOT_PCT", 0.10))         # 10% of portfolio per trade
    GAINER_MIN_GAIN_24H = float(os.getenv("GAINER_MIN_GAIN_24H", 15.0)) # min 24h gain %
    GAINER_SL_PCT = float(os.getenv("GAINER_SL_PCT", 0.06))             # stop loss 6%
    GAINER_TP_PCT = float(os.getenv("GAINER_TP_PCT", 0.12))             # take profit 12% (partial-TP + trail vorher)
    GAINER_SCAN_INTERVAL_MINUTES = int(os.getenv("GAINER_SCAN_INTERVAL_MINUTES", 15))
    # Telegram-Alarm ab diesem 24h-Gewinn (unabhaengig von Slot-Status — damit du
    # manuell entscheiden kannst, auch wenn Slots voll sind). Debounce 4h pro Symbol.
    GAINER_ALERT_THRESHOLD = float(os.getenv("GAINER_ALERT_THRESHOLD", 50.0))
    GAINER_ALERT_DEBOUNCE_HOURS = float(os.getenv("GAINER_ALERT_DEBOUNCE_HOURS", 4.0))

    # Mega-Gainer-Alarm: scannt ALLE KuCoin-USDT-Paare (nicht nur Kraken-EUR),
    # damit auch Coins wie CHIP/PEPE-Klone ueber Telegram gepingt werden, selbst
    # wenn Kraken sie nicht listet. Inklusive Kraken-Tradeability-Check.
    MEGA_GAINER_THRESHOLD = float(os.getenv("MEGA_GAINER_THRESHOLD", 100.0))
    MEGA_GAINER_MIN_VOL_USDT = float(os.getenv("MEGA_GAINER_MIN_VOL_USDT", 500_000))
    MEGA_GAINER_DEBOUNCE_HOURS = float(os.getenv("MEGA_GAINER_DEBOUNCE_HOURS", 6.0))

    # Grid
    GRID_LEVELS = int(os.getenv("GRID_LEVELS", 10))
    GRID_SPREAD_PCT = float(os.getenv("GRID_SPREAD_PCT", 0.04))

    # DCA
    DCA_INTERVAL_MINUTES = int(os.getenv("DCA_INTERVAL_MINUTES", 60))
    DCA_AMOUNT_EUR = float(os.getenv("DCA_AMOUNT_EUR", 5))

    # Momentum
    RSI_PERIOD = int(os.getenv("RSI_PERIOD", 14))
    RSI_OVERSOLD = int(os.getenv("RSI_OVERSOLD", 35))
    RSI_OVERBOUGHT = int(os.getenv("RSI_OVERBOUGHT", 65))
    EMA_FAST = int(os.getenv("EMA_FAST", 9))
    EMA_SLOW = int(os.getenv("EMA_SLOW", 21))

    # Intervals
    CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", 60))
    NEWS_CHECK_INTERVAL = int(os.getenv("NEWS_CHECK_INTERVAL", 300))

    # Telegram
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

    @classmethod
    def is_paper_mode(cls):
        return cls.TRADING_MODE.lower() == "paper"

    @classmethod
    def validate(cls):
        if not cls.is_paper_mode():
            if not cls.KRAKEN_API_KEY or not cls.KRAKEN_API_SECRET:
                raise ValueError("Kraken API keys required for live trading!")
        print(f"{'='*50}")
        print(f"  Mode: {'PAPER' if cls.is_paper_mode() else '!! LIVE !!'}")
        print(f"  Capital: {cls.INITIAL_CAPITAL}EUR")
        print(f"  Risk/trade: {cls.MAX_RISK_PER_TRADE*100:.0f}%")
        print(f"  Strategies: {', '.join(cls.ACTIVE_STRATEGIES)}")
        print(f"  Max positions: {cls.MAX_OPEN_POSITIONS}")
        print(f"  Scanner: top {cls.SCAN_TOP_N} -> pick {cls.AUTO_PICK_COUNT}")
        print(f"  Interval: {cls.CHECK_INTERVAL}s")
        print(f"{'='*50}")
