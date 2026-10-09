import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    # Kraken API
    KRAKEN_API_KEY = os.getenv("KRAKEN_API_KEY", "")
    KRAKEN_API_SECRET = os.getenv("KRAKEN_API_SECRET", "")

    # Claude API (primary fuer Sentiment-Analyse)
    ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
    # Gemini API (fallback wenn Claude-Credit leer/401 — kostenloser Tier)
    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

    # Trading
    TRADING_MODE = os.getenv("TRADING_MODE", "paper")
    INITIAL_CAPITAL = float(os.getenv("INITIAL_CAPITAL", 1000))

    # 11.06.2026 — Kapital-Schutz statt Tages-P&L-Freeze (Option B, User-Entscheid).
    # Bot fror bei -6% vom hochgeratschten Tages-Anker ein, obwohl netto im Plus.
    # Jetzt: mitziehender Kapital-Boden capital_floor = max(INITIAL, peak*(1-BAND)),
    # sinkt nie, zieht mit echtem Kapitalwachstum hoch. Freeze NUR unter dem Boden.
    # BAND = wie viel % Buchgewinn vom Peak abgegeben werden darf, bevor der Boden
    # greift (über INITIAL). 0.08 = 8% — HWM trimmt eh schon ab 3% Drawdown.
    CAPITAL_TRAIL_BAND = float(os.getenv("CAPITAL_TRAIL_BAND", 0.08))
    # Schutz-Puffer ÜBER dem Boden: in diesem Band (in %) werden Longs blockiert
    # (nur Shorts), aber kein Hard-Freeze.
    # 29.06.2026 — Default 1.0 → 0.0. Bei 1.0 startete der Bot nach einem Reset
    # (Portfolio = Boden = 1000, cap_pnl = 0%) SOFORT in "nur Shorts" und durfte
    # erst Longs ab +1% (1010) handeln → im Bull-Markt gelähmt. 0 = binär:
    # am/über dem Boden voll traden (Longs+Shorts), erst UNTER dem Boden nur Shorts.
    # Das tote Band ist redundant — der Boden trailt eh 8% unter Peak, HWM ab 3%.
    # 08.10.2026 — Default 0.0 → -0.25. Live beim Verifizieren des Resets gesehen:
    # Portfolio 999.09 vs. Boden 1000.00 → cap_pnl -0.09% → sofort "nur Shorts",
    # und zwar AUSSCHLIESSLICH wegen der Einstiegsgebühr des ersten Trades (0.91 EUR).
    # Bei 0.0 kippt also jede normale Gebührenfriktion den Bot dauerhaft in
    # Shorts-only — genau die Schieflage, die das short-schwere Buch erzeugt hat,
    # das am 13.07.2026 deadlockte. -0.25% ist ein Toleranzband knapp unter dem
    # Boden: ein einzelner Round-Trip (~0.52% auf die Position, bei 200-400 EUR
    # Positionen ~0.1-0.2% des Portfolios) löst den Schutz nicht mehr aus, ein
    # echter Drawdown schon.
    CAPITAL_PROTECT_PCT = float(os.getenv("CAPITAL_PROTECT_PCT", -0.25))
    # 15.06.2026 — Zwei-Stufen-Schutz statt Hard-Freeze-Deadlock.
    # PROBLEM: Unter dem Boden machte der Bot sleep+continue → check_exits wurde
    # übersprungen → offene Positionen wurden NICHT mehr gemanagt (keine SL/TP)
    # und es lief gar nichts mehr → Deadlock bei 980 (unter Boden 1000).
    # NEU: Soft-Zone (unter Boden, aber über Hard-Stop) = nur Shorts + Exits laufen
    # weiter → Bot kann sich rausarbeiten. Hard-Freeze ERST bei echtem Absturz
    # CAPITAL_HARD_STOP_PCT % unter dem Boden (Katastrophenschutz).
    CAPITAL_HARD_STOP_PCT = float(os.getenv("CAPITAL_HARD_STOP_PCT", 5.0))
    # 08.10.2026: Der Hard-Stop ist jetzt ein Circuit-Breaker statt eines Vollstopps
    # (siehe main.py). Nach dem Flat-Gehen pausieren neue Entries so lange — danach
    # handelt der Bot normal weiter. Verhindert das 78-Tage-Dauergrab vom 13.07.2026.
    HARD_STOP_COOLDOWN_HOURS = float(os.getenv("HARD_STOP_COOLDOWN_HOURS", 2.0))
    # Beim Re-Basieren darf der Boden nie unter diesen Anteil des Startkapitals
    # fallen — sonst könnte sich der Boden bei einer Verlustserie beliebig tief
    # nach unten durchreichen. 0.5 = Boden mindestens 500 EUR bei 1000 Start.
    CAPITAL_FLOOR_REBASE_MIN_FRAC = float(os.getenv("CAPITAL_FLOOR_REBASE_MIN_FRAC", 0.5))

    # Risk (aggressive)
    MAX_RISK_PER_TRADE = float(os.getenv("MAX_RISK_PER_TRADE", 0.25))
    STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", 0.04))
    # TP bewusst nah dran — Partial-TP (2.5%/5%) + Trailing sollen vorher greifen.
    # Hart-Cap als Sicherheitsnetz, falls Preis durchschießt.
    TAKE_PROFIT_PCT = float(os.getenv("TAKE_PROFIT_PCT", 0.08))
    # Globaler Cap eher großzügig — echte Risikosteuerung macht der Korrelations-Cap
    # (max 3 LONG + 3 SHORT in risk_manager.py). Zusätzlich Gainer-Slot + DCA/Grid.
    MAX_OPEN_POSITIONS = int(os.getenv("MAX_OPEN_POSITIONS", 12))
    # Anti-Churn: max N Trades pro Symbol pro Tag. Verhindert Whipsaw wie bei
    # M/EUR (21.04.: 7 Einstiege/Ausstiege, ~14 EUR Fees, netto -X). Reset Mitternacht.
    # 24.06.2026: 3→2. Fee-Audit: 18 Trades = 10.44 EUR Fees auf 1000 EUR (>50% des
    # Realized-Loss). Jeder Re-Entry kostet ~0.52% Round-Trip. 2/Tag/Symbol = weniger
    # Whipsaw-Re-Entries, der Gewinner muss die Gebuehr wirklich ueberspringen.
    MAX_TRADES_PER_SYMBOL_PER_DAY = int(os.getenv("MAX_TRADES_PER_SYMBOL_PER_DAY", 2))
    # 08.10.2026: 2→3. Bei 2 war das Gate wirkungslos (Momentum liefert immer
    # lev>=2 → jedes Signal galt als "strong" und durfte rotieren). 3 = nur
    # echte Breakout-Konviktion darf eine bestehende Position verdrängen.
    ROTATION_MIN_LEVERAGE = int(os.getenv("ROTATION_MIN_LEVERAGE", 3))
    # Rotation nur, wenn die schwächste Position mindestens so tief im Minus ist.
    # Forensik: Rotation hatte EV -0.85/Trade — sie schnitt oft flache oder sogar
    # grüne Positionen weg und zahlte dafür den Round-Trip.
    ROTATION_MIN_LOSS_PCT = float(os.getenv("ROTATION_MIN_LOSS_PCT", -1.5))
    DAILY_TARGET_PCT = float(os.getenv("DAILY_TARGET_PCT", 5.0))  # Tages-Ziel in %

    # Strategies
    # 28.04.2026: grid dazu — Audit zeigte 75% NEUTRAL-Cycles, Grid ist regime-exempt
    # und confirmt zusätzlich Sentiment-LONG. Mehr Signal-Quellen = mehr 2-3%/Tag-Chancen.
    # 14.05.2026: Radikale Reduktion. Log 13.05. zeigte: 13 momentum-Sells profitabel,
    # 2 grid-Sells, Grid erzeugte 1411 SL-Cooldown-Pings für 6 Coins (Whipsaw-Hölle).
    # Sentiment ist confirm-only und triggert nie alleine → ohne Grid auch nichts zu
    # confirmen. DCA bleibt eh aus (nicht in Liste). Gainer läuft separat (eigener Slot).
    # Nur momentum behalten = die einzige Strategie mit dokumentierten Wins im Log.
    ACTIVE_STRATEGIES = os.getenv("ACTIVE_STRATEGIES", "momentum").split(",")

    # High-Conviction-Bypass: in NEUTRAL-Regime werden normalerweise alle Entries
    # geblockt (Whipsaw-Schutz). Wenn ein Signal ABER stark genug ist, darf es trotzdem
    # durch. Schwellen kalibriert auf Log-Daten 25.04.: typische Sentiment-Scores 5-8,
    # XRP-/SCAM-Crashes oft -6 bis -8.
    # 09.06.2026: 7→6. User-Beschwerde: XRP-News-Score +7 wurde geloggt, kein Trade.
    # Score 6 ist häufiger im 9h-Fenster (Audit-Daten 27.04.) — mehr NEUTRAL-Bypass-
    # Chancen für Sentiment-BUY auf BTC/ETH/XRP. Risiko: NEUTRAL-Longs hatten historisch
    # 0/9 Winrate, aber NUR ohne Bypass — hier nur stark-confirmte Setups durch.
    HIGH_CONVICTION_SENTIMENT_SCORE = int(os.getenv("HIGH_CONVICTION_SENTIMENT_SCORE", 6))
    # 28.04.2026: 3 von 4 Long-Verlierern waren Bypass-Trades mit lev=2 (Min-Schwelle).
    # Auf 3 angehoben — lev=3 ist seltener aber statistisch belastbar.
    # Fix 11.05.2026 (Bug E): Momentum-Strategie lieferte in strategies.py NUR lev=2,
    # nie lev=3. Schwelle auf 3 = Bypass nie aktiv. Damals auf 2 gesenkt — aber dann
    # bypassed JEDES Momentum-Signal (RSI-Mean-Reversion inkl.) das NEUTRAL-Gate.
    # Fix 10.06.2026: strategies.py gibt jetzt lev=3 NUR für Breakout/Breakdown+Volumen
    # (echtes Konvictions-Signal), lev=2 für RSI-Extrem. Schwelle zurück auf 3 →
    # nur der Breakout darf NEUTRAL bypassen, die Whipsaw-anfällige Mean-Reversion
    # nicht mehr. Genau die selektive Logik, die das Gate ursprünglich wollte.
    # Lehre 28.04. bleibt: Bypass NUR in NEUTRAL, nie gegen BULLISH/BEARISH-Trend.
    HIGH_CONVICTION_MOMENTUM_LEVERAGE = int(os.getenv("HIGH_CONVICTION_MOMENTUM_LEVERAGE", 3))
    # 08.10.2026: Der NEUTRAL-Bypass gilt nur noch für SHORTS. Begründung mit
    # Zahlen im Kommentar an der Bypass-Stelle in main.py: der Long-Breakout hat
    # über 310 Trades einen negativen BRUTTO-Edge (-0.427/Trade), der
    # Short-Breakdown über 663 Trades einen klar positiven (+0.753/Trade).
    # Longs nur noch in echtem BULLISH-Regime, nicht mehr im NEUTRAL-Chop.
    # Auf 1 setzen, falls Longs im Seitwärtsmarkt wieder erlaubt sein sollen.
    ALLOW_NEUTRAL_LONG_BYPASS = os.getenv("ALLOW_NEUTRAL_LONG_BYPASS", "0") == "1"

    # Sentiment-Whitelist: nur diese Symbole duerfen ueber Sentiment getradet werden.
    # Lehre 25.-27.04.2026: Sentiment hat in 76k Logzeilen NULL profitable Trades
    # erzeugt — Alts (XRP, etc.) reagieren kaum auf News, nur BTC reagiert konsistent.
    # Default BTC-only. ETH bewusst raus (zu viel L2/Narrative-Rauschen).
    # 06.05.2026: BTC-only zu eng. Sentiment ist confirm-only (main.py:1233) — d.h.
    # ein XRP-News-Score +7 dient als STRONG-Boost wenn XRP-Momentum/Grid in selber
    # Richtung feuert. BTC-Solo-Whitelist verhindert das. Erweitert um ETH+XRP, weil
    # das die zwei meistgemeldeten Alts in den 25k-Logzeilen sind.
    SENTIMENT_WHITELIST = os.getenv("SENTIMENT_WHITELIST", "BTC/EUR,ETH/EUR,XRP/EUR").upper().split(",")
    # Mindest-|score|: 27.04.2026 9h Logfenster zeigte: hoechster Score in 9h war 7
    # → Schwelle 8 = Strategie tot. Auf 6 reduziert: in derselben Periode haetten
    # ~192 Signale durchgekonnt, dann filtert TA-Confirm-Gate die unbestaetigten raus.
    SENTIMENT_MIN_SCORE = int(os.getenv("SENTIMENT_MIN_SCORE", 6))

    # Position-Sizing-Tiers: Fixed-EUR statt Risk-%-Logik (alte Logik schrumpfte mit
    # sinkendem Cash → 25 EUR Trades bei 705 EUR Cash). 27.04.2026 Audit: Fees
    # 6.39 EUR vs. Net 5.36 EUR = 54% Fee-Drag — viel zu viel. Bigger Trades = weniger
    # relativer Fee-Drag.
    # 06.05.2026: NORMAL 100→150, STRONG 200→300. User-Beschwerde: Bot dümpelt
    # mit +2EUR/Tag rum trotz volatilem Markt. Bei 100EUR Trades sind +5% TP nur
    # 5EUR brutto / ~3.7EUR netto nach Fees. Größere Sizes verbessern Net-Drag.
    # 08.05.2026: NORMAL 150→200, STRONG 300→400. Audit nach Reset: 20 Trades in
    # 8h erzeugten 6.96EUR Fees auf 1000EUR Kapital → 0.7%/Tag Fee-Drag, größer
    # als der durchschnittliche Profit. 200EUR Trades bringen Fee-Quote von 0.23%
    # auf 0.18% pro Trade. STRONG 400 nur bei 2-Strategien-Confluence (selten).
    # Exposure-check: 2 STRONG * 400 + 5 NORMAL * 200 = 1800 — Cash-Reserve 250
    # und MAX_OPEN_POSITIONS limitieren in der Praxis.
    POSITION_SIZE_MIN_EUR = float(os.getenv("POSITION_SIZE_MIN_EUR", 50))    # Cash-Reserve-Modus
    POSITION_SIZE_NORMAL_EUR = float(os.getenv("POSITION_SIZE_NORMAL_EUR", 200))  # Default
    POSITION_SIZE_STRONG_EUR = float(os.getenv("POSITION_SIZE_STRONG_EUR", 400))  # Sehr gutes Signal
    # Hard-Reserve fuer Gainer-Strategie (2 Slots * 100 EUR = 200) + 50 EUR Puffer.
    # Wenn Cash darunter → nur Min-Sizing (50 EUR), damit Gainer immer schlagen kann.
    MIN_CASH_RESERVE_EUR = float(os.getenv("MIN_CASH_RESERVE_EUR", 250))
    # Max parallele "strong"-Positions (200-EUR-Tier). Verhindert dass 7 Slots * 200
    # = 1400 EUR Exposure das Kapital sprengen.
    MAX_STRONG_POSITIONS = int(os.getenv("MAX_STRONG_POSITIONS", 2))

    # Time-Stop: Position die >X Stunden offen ist UND P&L flatlined zwischen
    # ±Y% → Soft-Close. 27.04.2026: 4 SHORTs hingen 8h+ ohne Bewegung, blockierten
    # Slots fuer 37 weitere Setups.
    # 08.10.2026: 4.0→12.0h. Forensik: time_stop_flatlined war brutto +25.96 EUR,
    # aber 112.05 EUR Fees → netto -86.09 (330 Trades, EV -0.26). Die Positionen
    # waren VOR Gebühren leicht profitabel — der 4h-Schnitt hat sie zu früh
    # kassiert, die Gebühr machte daraus ein Minus. Mehr Zeit + das engere
    # ±0.5%-Band (unten) = nur noch wirklich tote Positionen werden geflusht.
    POSITION_TIME_STOP_HOURS = float(os.getenv("POSITION_TIME_STOP_HOURS", 12.0))
    # 24.06.2026: 1.0→0.5. Fee-Drag-Fix: ein Time-Stop bei +0.7% Brutto ist nach
    # 0.52% Round-Trip-Fee netto NEGATIV. Engeres Band = nur wirklich tote Positionen
    # (±0.5%) werden geflusht; eine die noch +0.7% laeuft darf Richtung TP weiter,
    # statt mit Fee-Verlust geschlossen zu werden.
    POSITION_TIME_STOP_MAX_PNL_PCT = float(os.getenv("POSITION_TIME_STOP_MAX_PNL_PCT", 0.5))

    # Win-Cooldown: nach profitablem Exit X Stunden Pause fuer dasselbe Symbol.
    # ORCA 27.04.: Win +7.02, dann 2.5h spaeter Re-Entry → SL -4.98. Erstes Setup
    # war durch, wir sollten nicht direkt wieder rein.
    # 29.04.2026: 2h hat 393 Re-Entries blockiert. Auf 0.5h (30min) reduziert —
    # genug um Whipsaw zu verhindern, kurz genug um neue Setups nicht zu killen.
    # 24.06.2026: 0.5→1.5h. Fee-Drag-Fix: 30min war zu kurz, das Symbol re-triggerte
    # haeufig direkt im selben Move → zweiter Trade frass Fees ohne neuen Edge. 1.5h
    # laesst den Move erst auslaufen, bevor wir dasselbe Symbol nochmal anfassen.
    WIN_COOLDOWN_HOURS = float(os.getenv("WIN_COOLDOWN_HOURS", 1.5))

    # NEUTRAL-Short-Gate: in NEUTRAL-Regime werden Shorts nur erlaubt, wenn BTC 15m
    # mindestens X% nachgibt. 28.04.2026 Audit: btc_15m < 0 (alles unter 0%) hat 4
    # Shorts ausgeloest die ALLE -3.94 EUR verloren haben — Markt war 95% NEUTRAL,
    # 15m-Wackler waren keine Trends. Schwelle auf -0.5% angehoben (echtes
    # Intraday-Rutschen, nicht Noise).
    NEUTRAL_SHORT_BTC_15M_THRESHOLD = float(os.getenv("NEUTRAL_SHORT_BTC_15M_THRESHOLD", -0.005))

    # Marktkontext-Exit Gain-Locks (08.10.2026, aus der Forensik abgeleitet).
    # SHORT-Seite: war bei 1.5% der zweitschlimmste Posten (EV -0.81, -131.10 EUR)
    # weil er die einzig profitable Richtung deckelte → auf 4% angehoben, damit
    # Gewinner Richtung TP (EV +3.94) laufen dürfen.
    # LONG-Seite: bei 1.5% leicht positiv (EV +0.13) → bleibt.
    MARKET_EXIT_SHORT_GAIN_LOCK = float(os.getenv("MARKET_EXIT_SHORT_GAIN_LOCK", 0.04))
    MARKET_EXIT_LONG_GAIN_LOCK = float(os.getenv("MARKET_EXIT_LONG_GAIN_LOCK", 0.015))


    # Symbol-Blacklist: nie handeln. Stablecoins liefern strukturell ~0% PnL und
    # blockieren nur Slots (USDT/EUR-SHORT lag 3 Tage bei -0,09 EUR und hielt einen
    # SHORT-Cap-Slot fest, der fuer profitable Setups gefehlt hat). Match auf
    # vollen Symbol- oder Base-Strings (case-insensitive).
    SYMBOL_BLACKLIST = os.getenv(
        "SYMBOL_BLACKLIST",
        "USDT,USDC,DAI,EURT,EURC,PYUSD,TUSD,FDUSD,USDE,RLUSD"
    ).upper().split(",")

    # Momentum-Prioritätsliste: Coins die historisch gut mit Momentum funktionieren
    # Wird wöchentlich vom Auto-Optimizer befüllt — kein hardcoded Ban mehr
    # Alle anderen Coins sind weiterhin handelbar, kommen nur weiter hinten im Scan
    MOMENTUM_PRIORITY: list = []  # wird bei Startup aus optimizer_state.json geladen

    # Scanner
    SCAN_TOP_N = int(os.getenv("SCAN_TOP_N", 50))
    AUTO_PICK_COUNT = int(os.getenv("AUTO_PICK_COUNT", 10))

    # Grid-Strategie skipt Coins unter dieser Preisschwelle.
    # Audit 08.05.2026: AI 0.03EUR, BILL 0.06EUR, BIO 0.04EUR, DOGE 0.09EUR
    # generierten alle SLs nach Reset. Sub-0.10EUR-Coins haben weite Spreads
    # und Mikro-Tick-Bewegungen die Grid-TPs/SLs whipsawen. Gainer/Momentum
    # bleiben unbetroffen — die fangen echte Pumps auch in Penny-Coins.
    GRID_MIN_PRICE_EUR = float(os.getenv("GRID_MIN_PRICE_EUR", 0.10))

    # Gainer Slot (Binance top gainers)
    # WICHTIGE LEHRE 22.04.2026: SPK +50% RSI 79 → -9.02EUR in 20min. Entry-Filter
    # gescharft: RSI-Cap 72 (von 88), Max-24h-Gain 40% (neu), SL 4% (von 6%).
    GAINER_SLOT_PCT = float(os.getenv("GAINER_SLOT_PCT", 0.10))         # 10% of portfolio per trade (halbiert in Drawdown via main.py)
    GAINER_MIN_GAIN_24H = float(os.getenv("GAINER_MIN_GAIN_24H", 15.0)) # min 24h gain %
    GAINER_MAX_GAIN_24H = float(os.getenv("GAINER_MAX_GAIN_24H", 40.0)) # max 24h gain % — >40% = Pump gelaufen, nicht kaufen
    GAINER_RSI_MAX = float(os.getenv("GAINER_RSI_MAX", 72.0))           # RSI-Cap — >72 = overbought, top-buying-Risiko
    GAINER_SL_PCT = float(os.getenv("GAINER_SL_PCT", 0.04))             # stop loss 4% (was 6% — zu viel Slippage bei Micro-Caps)
    # 09.10.2026: TP 12% → 7%. User-Vorgabe "Gewinn machen und schnell wieder
    # aussteigen". 12% war für einen Pump-Trade zu weit — der Rest-Anteil nach den
    # Partial-TPs ritt viel zu lange. Forensik: gainer hatte brutto nur +21.07 EUR
    # auf 147 Trades, bei 33.83 EUR Fees → netto -12.77. Der Edge ist da, aber dünn;
    # er muss früher eingesammelt werden statt auf ein 12%-Ziel zu hoffen.
    GAINER_TP_PCT = float(os.getenv("GAINER_TP_PCT", 0.07))

    # --- 09.10.2026: Gainer-Entry auf FORTSETZUNG statt Dip-Kauf (User-Vorgabe:
    # "nur echte gainer die noch nach oben gehen mitgenommen werden").
    # Die alten Filter tolerierten bewusst Konsolidierung: rote Kerze bis -0.3% ok,
    # Volumen bis auf 70% abfallend ok, Preis MUSSTE 1.5% unter dem 4h-Hoch sein.
    # Das kaufte in die Abkühlung. Jetzt umgekehrt: grüne Kerze, Volumen weiter
    # erhöht, kurzfristige Steigung positiv. Die echten Top-Buying-Schutzmechanismen
    # (RSI-Cap 72, Max-Gain-Cap 40%) bleiben unverändert — die Lehre aus SPK +50%
    # bei RSI 79 (-9.02 EUR) gilt weiter.
    # Mindest-Grün der letzten 15m-Kerze (0.001 = +0.1%, filtert Dojis).
    GAINER_MIN_CANDLE_PCT = float(os.getenv("GAINER_MIN_CANDLE_PCT", 0.001))
    # Volumen der letzten Kerze muss mind. dieses Vielfache des 20er-Durchschnitts
    # sein. 1.0 = nicht abfallend (vorher 0.7 = bis 30% Abfall erlaubt).
    GAINER_MIN_VOL_RATIO = float(os.getenv("GAINER_MIN_VOL_RATIO", 1.0))
    # Kurzfristige Steigung: Close muss über dem Close von N Kerzen vorher liegen
    # (3 * 15m = 45min netto aufwärts). Das ist der eigentliche "geht noch nach
    # oben"-Nachweis, den vorher kein Filter geprüft hat.
    GAINER_SLOPE_LOOKBACK = int(os.getenv("GAINER_SLOPE_LOOKBACK", 3))
    # Peak-Schutz gelockert: 0.985 → 0.995. Ein Coin, der mit grüner Kerze und
    # steigendem Volumen läuft, steht naturgemäß NAHE seinem 4h-Hoch — die alte
    # 1.5%-Distanz hätte jede echte Fortsetzung abgewiesen. Er darf aber weiterhin
    # nicht AM oder ÜBER dem Hoch gekauft werden.
    GAINER_MAX_OF_4H_HIGH = float(os.getenv("GAINER_MAX_OF_4H_HIGH", 0.995))
    # Schnell wieder raus: Gainer waren bisher vom Time-Stop AUSGENOMMEN und konnten
    # unbegrenzt liegen. Wenn der Pump nach dieser Zeit nicht mal die erste
    # Partial-TP-Stufe erreicht hat, ist er vorbei → schließen.
    GAINER_MAX_HOLD_HOURS = float(os.getenv("GAINER_MAX_HOLD_HOURS", 6.0))
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
    # 09.10.2026 (User-Wunsch): Mega-Gainer-Alarm AUS. Er war reine Telegram-Info
    # und hat nie gehandelt (keine place_order im Pfad). Nebeneffekt des Abschaltens:
    # der teure KuCoin-fetch_tickers-Scan über ALLE USDT-Paare alle 5 Minuten
    # entfällt komplett — das war die Quelle der "[MegaGainer] Error: kucoin ..."
    # Meldungen. Auf 1 setzen, um die Pings wieder einzuschalten.
    MEGA_GAINER_ALERTS = os.getenv("MEGA_GAINER_ALERTS", "0") == "1"

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

    # Post-SL-Cooldown: nach Stop-Loss auf einem Symbol X Stunden Pause.
    # Lehre Log 22./23.04: SPK wurde 4x gekauft, 3 SLs in Folge = -21EUR.
    # 30min recently_traded Cooldown war zu kurz — erster Re-Entry kam 47min
    # nach SL. 6h = Pump ist sicher vorbei, Trend gebrochen.
    POST_SL_COOLDOWN_HOURS = float(os.getenv("POST_SL_COOLDOWN_HOURS", 6.0))

    # Gainer-Liquiditaets-Gate auf KRAKEN EUR (nicht nur KuCoin USDT!).
    # Ersetzt den alten Zeit-Filter (Night Mode) — der war zu stumpf, hat auch
    # legitime Asia-Pumps geblockt. Stattdessen pruefen wir direkt ob der
    # Kraken-EUR-Orderbook-Stand einen sauberen Entry erlaubt.
    # 24h-Quote-Volume auf Kraken EUR muss >= diesem Wert sein.
    GAINER_MIN_KRAKEN_VOL_EUR = float(os.getenv("GAINER_MIN_KRAKEN_VOL_EUR", 300_000))
    # Max Spread (ask-bid)/mid in % — > diesem Wert = Orderbook zu duenn,
    # Slippage frisst den Edge (typisch nachts auf Micro-Caps).
    GAINER_MAX_SPREAD_PCT = float(os.getenv("GAINER_MAX_SPREAD_PCT", 0.5))

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
