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
    STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", 0.06))
    TAKE_PROFIT_PCT = float(os.getenv("TAKE_PROFIT_PCT", 0.18))
    MAX_OPEN_POSITIONS = int(os.getenv("MAX_OPEN_POSITIONS", 12))
    ROTATION_MIN_LEVERAGE = int(os.getenv("ROTATION_MIN_LEVERAGE", 2))
    DAILY_TARGET_PCT = float(os.getenv("DAILY_TARGET_PCT", 5.0))  # Tages-Ziel in %

    # Strategies
    ACTIVE_STRATEGIES = os.getenv("ACTIVE_STRATEGIES", "momentum,sentiment").split(",")

    # Momentum: Diese Coins werden für Momentum-Trades gemieden (Backtest: schlechte Performance)
    # Automatisch generiert vom Auto-Optimizer (137 Coins, 3 Monate Backtest, TP=18% SL=6%)
    MOMENTUM_SKIP = os.getenv("MOMENTUM_SKIP", "0G/EUR,ACT/EUR,ACX/EUR,ADX/EUR,AEVO/EUR,AIXBT/EUR,ALCX/EUR,ALICE/EUR,ALT/EUR,ANKR/EUR,APE/EUR,API3/EUR,APT/EUR,AR/EUR,ARB/EUR,ARKM/EUR,ARPA/EUR,ASTR/EUR,ATOM/EUR,AUCTION/EUR,AUDIO/EUR,AVNT/EUR,BANANAS31/EUR,BAT/EUR,BIGTIME/EUR,BIO/EUR,BLUR/EUR,BMT/EUR,BNB/EUR,BNT/EUR,BREV/EUR,C98/EUR,CELO/EUR,CELR/EUR,CFG/EUR,CHR/EUR,CHZ/EUR,COTI/EUR,COW/EUR,CVC/EUR,CYBER/EUR,DASH/EUR,DENT/EUR,DOGS/EUR,DYDX/EUR,ENA/EUR,ENJ/EUR,ETHFI/EUR,EUL/EUR,FARM/EUR,FLOW/EUR,FLUX/EUR,FUN/EUR,GMT/EUR,GNO/EUR,HBAR/EUR,HFT/EUR,HMSTR/EUR,HOLO/EUR,ICP/EUR,ICX/EUR,IDEX/EUR,INIT/EUR,INJ/EUR,JST/EUR,JUP/EUR,KERNEL/EUR,KMNO/EUR,KNC/EUR,LAYER/EUR,LINK/EUR,LPT/EUR,LRC/EUR,LSK/EUR,LUNC/EUR,MANA/EUR,MEME/EUR,MET/EUR,METIS/EUR,MINA/EUR,MIRA/EUR,NEIRO/EUR,NIGHT/EUR,NIL/EUR,OGN/EUR,OPEN/EUR,OXT/EUR,PEPE/EUR,PHA/EUR,PLUME/EUR,PNUT/EUR,PUMP/EUR,QNT/EUR,QTUM/EUR,RAD/EUR,RARE/EUR,RAY/EUR,RENDER/EUR,REZ/EUR,RLC/EUR,RPL/EUR,S/EUR,SAHARA/EUR,SAND/EUR,SCRT/EUR,SENT/EUR,SKY/EUR,SNX/EUR,SOLV/EUR,SPELL/EUR,SPK/EUR,SSV/EUR,STORJ/EUR,SUI/EUR,SUPER/EUR,SUSHI/EUR,TLM/EUR,TNSR/EUR,TREE/EUR,TRU/EUR,TRUMP/EUR,TRX/EUR,UNI/EUR,USUAL/EUR,VANRY/EUR,VET/EUR,WAL/EUR,WBTC/EUR,WIF/EUR,WLD/EUR,WLFI/EUR,WOO/EUR,YB/EUR,YFI/EUR,YGG/EUR,ZAMA/EUR,ZRX/EUR").split(",")

    # Scanner
    SCAN_TOP_N = int(os.getenv("SCAN_TOP_N", 50))
    AUTO_PICK_COUNT = int(os.getenv("AUTO_PICK_COUNT", 10))

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
