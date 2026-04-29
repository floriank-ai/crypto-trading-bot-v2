#!/usr/bin/env python3
"""
Crypto Trading Bot v2 - Multi-Strategy, Multi-Coin, AI-Powered
Usage:
    python main.py --mode paper    # Paper trading (default)
    python main.py --mode live     # Live trading
"""

import argparse
import time
import sys
from datetime import datetime, timezone

from config import Config
from auto_optimizer import should_run_today, run as run_optimizer, load_state, save_state
from exchange import Exchange
from scanner import CoinScanner
from gainer_scanner import GainerScanner
from strategies import MomentumStrategy, GridStrategy, DCAStrategy, GainerStrategy, Signal
from sentiment import NewsSentimentAnalyzer
from risk_manager import RiskManager
from trade_logger import TradeLogger
from notifier import Notifier
from daily_summary import DailySummary



def fmt_price(p: float) -> str:
    """Format price with enough decimal places for cheap coins."""
    if p >= 100:  return f"{p:.2f}"
    if p >= 1:    return f"{p:.3f}"
    if p >= 0.01: return f"{p:.4f}"
    return f"{p:.6f}"


def _is_blacklisted(symbol: str) -> bool:
    """True wenn Symbol oder Base in Config.SYMBOL_BLACKLIST steht.
    Stablecoins (USDT/EUR, USDC/EUR, ...) liefern strukturell ~0% PnL und blockieren
    nur Slots. Match auf vollen Symbol-String UND Base-String, case-insensitive.
    """
    if not symbol:
        return False
    s = symbol.upper()
    base = s.split("/")[0]
    bl = [b.strip() for b in Config.SYMBOL_BLACKLIST if b and b.strip()]
    return s in bl or base in bl


def _restore_positions(risk_mgr, exchange):
    """Restore open positions from positions.json (written after every trade)."""
    import json, os
    pos_path = os.path.join("logs", "positions.json")
    if not os.path.exists(pos_path):
        print("  [Restore] No positions.json — Restart without open positions")
        return
    try:
        with open(pos_path) as f:
            positions = json.load(f)
        if not positions:
            print("  [Restore] positions.json empty — no open positions")
            return
        # Only restore symbols that are available on Kraken (EUR pairs)
        valid_symbols = set(exchange.get_all_eur_pairs())
        skipped = []
        for sym, pos in positions.items():
            if sym not in valid_symbols:
                skipped.append(sym)
                continue
            # Backwards-compat: add Partial-TP fields if old positions.json
            pos.setdefault("initial_volume", pos["volume"])
            pos.setdefault("partial_tps_taken", [])
            risk_mgr.open_positions[sym] = pos
            direction = pos.get("direction", "long")
            if direction == "long":
                exchange.paper_positions[sym] = pos["volume"]
            else:
                if not hasattr(exchange, "paper_short_positions"):
                    exchange.paper_short_positions = {}
                exchange.paper_short_positions[sym] = {
                    "volume": pos["volume"],
                    "entry_price": pos["entry_price"],
                    "margin": pos.get("margin", pos["entry_price"] * pos["volume"] * 0.20),
                }
            print(f"    {sym} [{direction.upper()}] vol={pos['volume']:.6f} @ {pos['entry_price']:.4f} SL={pos['stop_loss']:.4f}")
        if skipped:
            print(f"  [Restore] Skipped (not on Kraken): {', '.join(skipped)}")
            risk_mgr._save_positions()  # overwrite positions.json without invalid symbols
        print(f"  [Restore] {len(risk_mgr.open_positions)} positions restored")
    except Exception as e:
        print(f"  [Restore] Error: {e}")
        import traceback
        traceback.print_exc()


def execute_trade(exchange, risk_manager, logger, notifier, symbol, side, analysis, balance, portfolio_val=None, drawdown_pct=0.0):
    """Execute a trade and log it."""
    strategy = analysis.get("strategy", "unknown")
    price = analysis.get("price", 0)

    direction = analysis.get("direction", "long")

    if side == "buy" or (side == "sell" and direction == "short"):
        strategy = analysis.get("strategy", "momentum")
        if not risk_manager.can_open_position(symbol, strategy, direction):
            if symbol in risk_manager.open_positions:
                print(f"    Skip: already holding {symbol}")
            elif len(risk_manager.open_positions) >= risk_manager.max_positions:
                print(f"    Skip: max positions ({risk_manager.max_positions}) reached")
            elif direction == "long" and sum(1 for p in risk_manager.open_positions.values()
                                             if p.get("direction", "long") == "long") >= risk_manager.MAX_LONG_POSITIONS:
                print(f"    Skip: LONG cap reached (max {risk_manager.MAX_LONG_POSITIONS} Longs — correlation guard)")
            elif direction == "short" and sum(1 for p in risk_manager.open_positions.values()
                                              if p.get("direction") == "short") >= risk_manager.MAX_SHORT_POSITIONS:
                print(f"    Skip: SHORT cap reached (max {risk_manager.MAX_SHORT_POSITIONS} Shorts — correlation guard)")
            elif strategy == "dca":
                print(f"    Skip: DCA cap reached (max {risk_manager.MAX_DCA_POSITIONS} DCA positions)")
            elif strategy == "gainer":
                print(f"    Skip: Gainer slot occupied")
            else:
                print(f"    Skip: Position not possible ({symbol})")
            return False

        dca_mult = analysis.get("dca_multiplier", 1.0)
        leverage = analysis.get("leverage", 1)
        # signal_strength wird vom Caller (cycle-loop) gesetzt: "strong" wenn
        # lev>=3 oder Sentiment+Momentum confirm beide gleich-Richtung. Sonst "normal".
        signal_strength = analysis.get("signal_strength", "normal")
        volume = risk_manager.calculate_position_size(
            balance, price, strategy, dca_mult, leverage, drawdown_pct,
            signal_strength=signal_strength,
        )
        min_order = exchange.get_min_order(symbol)

        if volume < min_order:
            print(f"    Skip: volume {volume} < min {min_order}")
            return False

        result = exchange.place_order(symbol, side, volume, direction=direction)
        if result["status"] == "ok":
            exec_price = result.get("price", price)
            cost = result.get("cost", volume * exec_price)
            fee = result.get("fee", cost * 0.0026)

            risk_manager.open_position(
                symbol, exec_price, volume, strategy, direction,
                sizing_tier=signal_strength,
            )
            print(f"    Sizing: {signal_strength.upper()} ~{volume * exec_price:.0f}EUR "
                  f"(cash {balance:.0f}EUR)")
            log_side = "short" if direction == "short" else "buy"
            logger.log_trade(pair=symbol, side=log_side, volume=volume,
                             price=exec_price, cost=cost, fee=fee,
                             mode=Config.TRADING_MODE, strategy=strategy,
                             signal_reason=analysis["reason"],
                             balance_after=exchange.get_balance())
            port = portfolio_val if portfolio_val is not None else risk_manager.get_portfolio_value(exchange)
            daily_pnl = risk_manager.get_daily_pnl_pct(exchange)
            notifier.notify_trade(log_side, symbol, volume, exec_price,
                                  analysis["reason"], strategy, exchange.get_balance(),
                                  port, daily_pnl)
            return True
        else:
            print(f"    Order failed: {result.get('error', '?')}")
    return False


def execute_gainer_trade(exchange, risk_manager, logger, notifier, symbol, analysis,
                         portfolio_val=None, drawdown_pct=0.0, daily_pnl_pct=0.0):
    """Execute a gainer trade on Binance (paper mode). Fixed EUR position size."""
    price = analysis.get("price", 0)
    if price <= 0:
        print(f"    [Gainer] No price for {symbol}")
        return False

    if not risk_manager.can_open_position(symbol, "gainer", "long"):
        print(f"    [Gainer] Slot already occupied")
        return False

    # Kraken-EUR-Liquidity-Gate (ersetzt alten Night-Mode-Zeitfilter).
    # Pruefung direkt vor dem Order-Placement: 24h-Volume + Spread auf Kraken.
    # KuCoin USDT kann fett sein, Kraken EUR trotzdem duenn — das ist der Kill-Switch.
    try:
        kraken_ticker = exchange.get_ticker(symbol)
        vol_eur = float(kraken_ticker.get("volume", 0) or 0)
        ask = float(kraken_ticker.get("ask", 0) or 0)
        bid = float(kraken_ticker.get("bid", 0) or 0)
        if vol_eur > 0 and vol_eur < Config.GAINER_MIN_KRAKEN_VOL_EUR:
            print(f"    [Gainer] SKIP {symbol}: Kraken-24h-Vol {vol_eur:,.0f}EUR "
                  f"< min {Config.GAINER_MIN_KRAKEN_VOL_EUR:,.0f}EUR (Orderbook zu duenn)")
            return False
        if ask > 0 and bid > 0:
            mid = (ask + bid) / 2
            spread_pct = (ask - bid) / mid * 100
            if spread_pct > Config.GAINER_MAX_SPREAD_PCT:
                print(f"    [Gainer] SKIP {symbol}: Spread {spread_pct:.2f}% "
                      f"> max {Config.GAINER_MAX_SPREAD_PCT:.2f}% (Slippage frisst Edge)")
                return False
            print(f"    [Gainer] Liquidity OK: Vol {vol_eur:,.0f}EUR, Spread {spread_pct:.2f}%")
    except Exception as e:
        print(f"    [Gainer] Liquidity-Check Fehler {symbol}: {e} — skip zur Sicherheit")
        return False

    # Anti-Martingale: Gainer slot also shrinks in drawdown
    if drawdown_pct >= 3.0:
        dd_scale = 0.4
    elif drawdown_pct >= 2.0:
        dd_scale = 0.5
    elif drawdown_pct >= 1.0:
        dd_scale = 0.7
    else:
        dd_scale = 1.0

    # Tages-Verlust-Schutz (Lehre Log 22.04): SPK wurde bei Tages-P&L -0.30% eröffnet
    # und verlor -9EUR. Bei jedem Tages-Minus halbe Größe — egal wie klein. Risiko
    # darf im Minus NICHT aufgestockt werden.
    if daily_pnl_pct < 0:
        dd_scale = min(dd_scale, 0.5)

    # Dynamic: always 10% of current portfolio value (* dd_scale bei Drawdown)
    port = portfolio_val if portfolio_val else risk_manager.get_portfolio_value(exchange)
    amount_eur = port * Config.GAINER_SLOT_PCT * dd_scale
    volume = round(amount_eur / price, 8)
    dd_note = ""
    if dd_scale < 1.0:
        dd_note = f" [DD-Scale {dd_scale:.1f}x, drawdown {drawdown_pct:.1f}%, day P&L {daily_pnl_pct:+.2f}%]"
    print(f"    [Gainer] Size: {amount_eur:.2f}EUR ({Config.GAINER_SLOT_PCT*100:.0f}% of {port:.2f}EUR portfolio){dd_note}")
    result = exchange.place_order(symbol, "buy", volume)

    if result["status"] == "ok":
        exec_price = result["price"]
        cost = result["cost"]
        fee = result["fee"]
        risk_manager.open_position(symbol, exec_price, volume, "gainer", "long")
        logger.log_trade(pair=symbol, side="buy", volume=volume,
                         price=exec_price, cost=cost, fee=fee,
                         mode=Config.TRADING_MODE, strategy="gainer",
                         signal_reason=analysis["reason"],
                         balance_after=exchange.get_balance())
        port = portfolio_val if portfolio_val is not None else risk_manager.get_portfolio_value(exchange)
        daily_pnl = risk_manager.get_daily_pnl_pct(exchange)
        notifier.notify_trade("buy", symbol, volume, exec_price,
                              analysis["reason"], "gainer",
                              exchange.get_balance(), port, daily_pnl)
        return True
    else:
        print(f"    [Gainer] Order failed: {result.get('error', '?')}")
        return False


def check_exits(exchange, risk_manager, logger, notifier, last_sl_time=None, last_win_time=None):
    """Check all open positions for partial-TP, stop-loss, take-profit.

    last_sl_time: dict symbol->timestamp; bei STOP_LOSS gesetzt. Main-Loop nutzt
    das fuer Post-SL-Cooldown (6h) damit wir nicht wie mit SPK 4x in Folge kaufen.
    """
    exits = []
    for symbol in list(risk_manager.open_positions.keys()):
        pos = risk_manager.open_positions[symbol]
        ticker = exchange.get_ticker(symbol)

        if not ticker or not ticker.get("last"):
            continue

        current_price = ticker["last"]

        # Partial-TP zuerst: Teilverkauf auf dem Weg nach oben
        partial = risk_manager.check_partial_tp(symbol, current_price)
        if partial:
            vol_close, stage_idx = partial
            direction = pos.get("direction", "long")
            close_side = "buy" if direction == "short" else "sell"
            trigger_pct = risk_manager.PARTIAL_TP_STAGES[stage_idx][0] * 100
            print(f"  >> PARTIAL-TP stage {stage_idx+1} ({trigger_pct:.1f}%) {symbol} [{direction}]: sell {vol_close:.8f}")
            res = exchange.place_order(symbol, close_side, vol_close, direction=direction)
            if res["status"] == "ok":
                cp = res.get("price", current_price)
                cost = res.get("cost", vol_close * cp)
                fee = res.get("fee", cost * 0.0026)
                if direction == "short":
                    pnl = (pos["entry_price"] - cp) * vol_close - 2 * fee
                    log_side = "cover"
                else:
                    pnl = (cp - pos["entry_price"]) * vol_close - 2 * fee
                    log_side = "sell"
                logger.log_trade(pair=symbol, side=log_side, volume=vol_close,
                                 price=cp, cost=cost, fee=fee,
                                 mode=Config.TRADING_MODE, strategy=pos["strategy"],
                                 signal_reason=f"partial_tp_{stage_idx+1}",
                                 balance_after=exchange.get_balance(), realized_pnl=pnl)
                risk_manager.record_partial_tp(symbol, stage_idx, vol_close)
                port_val = risk_manager.get_portfolio_value(exchange)
                d_pnl = risk_manager.get_daily_pnl_pct(exchange)
                notifier.notify_exit(symbol, f"partial_tp_{stage_idx+1}", pnl, pos["strategy"], port_val, d_pnl)
                print(f"    Partial: {symbol} P&L {pnl:+.2f}EUR | Restvolumen: {pos['volume']:.8f}")
            # Weiter mit SL/TP-Check: falls Rest-Volumen eh null, ist Position zu.
            if pos["volume"] <= 0:
                risk_manager.close_position(symbol)
                continue

        exit_type = risk_manager.check_exit(symbol, current_price)

        if exit_type:
            direction = pos.get("direction", "long")
            print(f"  >> {exit_type.upper()} triggered: {symbol} [{direction}]")

            close_side = "buy" if direction == "short" else "sell"
            result = exchange.place_order(symbol, close_side, pos["volume"], direction=direction)

            if result["status"] == "ok":
                close_price = result.get("price", current_price)
                cost = result.get("cost", pos["volume"] * close_price)
                fee = result.get("fee", cost * 0.0026)

                if direction == "short":
                    pnl = (pos["entry_price"] - close_price) * pos["volume"] - 2 * fee
                    log_side = "cover"
                else:
                    pnl = (close_price - pos["entry_price"]) * pos["volume"] - 2 * fee
                    log_side = "sell"

                logger.log_trade(pair=symbol, side=log_side, volume=pos["volume"],
                                 price=close_price, cost=cost, fee=fee,
                                 mode=Config.TRADING_MODE, strategy=pos["strategy"],
                                 signal_reason=exit_type,
                                 balance_after=exchange.get_balance(),
                                 realized_pnl=pnl)
                risk_manager.close_position(symbol)  # erst schließen, dann Portfolio berechnen
                port_val = risk_manager.get_portfolio_value(exchange)
                d_pnl = risk_manager.get_daily_pnl_pct(exchange)
                notifier.notify_exit(symbol, exit_type, pnl, pos["strategy"], port_val, d_pnl)
                exits.append((symbol, exit_type, pnl))
                # Post-SL-Cooldown: Symbol wird fuer POST_SL_COOLDOWN_HOURS gesperrt.
                # Verhindert SPK-Szenario (4x gekauft, 3x stop-geloss't, -21EUR).
                if exit_type == "stop_loss" and last_sl_time is not None:
                    last_sl_time[symbol] = time.time()
                # Win-Cooldown: nach profitablem Exit WIN_COOLDOWN_HOURS Pause.
                # ORCA 27.04.: Win +7.02 → 2.5h spaeter Re-Entry → SL -4.98.
                # Setup ist durch, nicht direkt wieder rein.
                if pnl > 0 and last_win_time is not None:
                    last_win_time[symbol] = time.time()

    return exits


def run_bot():
    parser = argparse.ArgumentParser(description="Crypto Trading Bot v2")
    parser.add_argument("--mode", choices=["paper", "live"], default="paper")
    args = parser.parse_args()
    Config.TRADING_MODE = args.mode

    print("\n" + "=" * 60)
    print("  CRYPTO TRADING BOT v2")
    print("  Multi-Strategy | Multi-Coin | AI-Powered")
    print("=" * 60)
    Config.validate()

    # Initialize
    exchange = Exchange()
    scanner = CoinScanner(exchange)
    mega_scanner = GainerScanner()  # KuCoin-weiter Scan fuer 100%+ Pumps (inkl. Nicht-Kraken-Coins)
    momentum = MomentumStrategy()
    grid = GridStrategy()
    dca = DCAStrategy()
    gainer_strategy = GainerStrategy()
    sentiment = NewsSentimentAnalyzer()
    risk_mgr = RiskManager()
    logger = TradeLogger()
    notifier = Notifier()
    daily_summary = DailySummary()  # CSV-Tagesreport (logs/daily_summary.csv) — kein Telegram

    # Restore open positions from trade log (survives Railway restarts)
    _restore_positions(risk_mgr, exchange)

    # /status Telegram-Command
    def send_status():
        port = risk_mgr.get_portfolio_value(exchange)
        bal  = exchange.get_balance()
        pnl  = risk_mgr.get_daily_pnl_pct(exchange)
        pos  = risk_mgr.open_positions
        lines = [f"📊 *Portfolio Status*\n",
                 f"💰 Cash: `{bal:.2f}EUR`",
                 f"📈 Portfolio: `{port:.2f}EUR`",
                 f"🎯 Tages-P&L: `{pnl:+.2f}%`",
                 f"📂 Offene Positionen: {len(pos)}"]
        for sym, p in pos.items():
            ticker = exchange.get_ticker(sym)
            cur = ticker.get("last", 0) if ticker else 0
            d = p.get("direction", "long")
            unreal = (cur - p["entry_price"]) * p["volume"] if d == "long" else (p["entry_price"] - cur) * p["volume"]
            if p.get("strategy") == "gainer":
                icon = "🚀" if unreal >= 0 else "💥"
                lines.append(f"  {icon} *{sym}* [GAINER]: `{unreal:+.2f}EUR`")
            else:
                icon = "🟢" if unreal >= 0 else "🔴"
                lines.append(f"  {icon} {sym} [{d.upper()}]: `{unreal:+.2f}EUR`")
        notifier.send("\n".join(lines))
    notifier.set_status_callback(send_status)

    # Cooldown/Churn-State fruh initialisieren (damit do_reset() sie clearen kann)
    recently_traded = {}          # symbol -> ts, 30min Cooldown nach Trade
    last_sl_time = {}             # symbol -> ts, 6h Cooldown nach Stop-Loss
    last_win_time = {}            # symbol -> ts, WIN_COOLDOWN_HOURS Pause nach profit-exit
    trades_today_by_symbol = {}   # symbol -> count, Churn-Cap (3/Tag)
    ghost_symbols_logged = set()  # Sentiment-Ghost-Symbole die wir schon geloggt haben

    # /reset confirm Telegram-Command: Paper-Balance auf INITIAL_CAPITAL zuruecksetzen.
    # Einmaliger Reset — bei naechstem Deploy laeuft es mit dem neuen Stand weiter
    # (keine ENV-Variable RESET_PAPER_BALANCE noetig). Fuer klassischen Start-over.
    def do_reset():
        try:
            exchange._reset_paper_state()
            risk_mgr.open_positions.clear()
            risk_mgr.daily_start_value = Config.INITIAL_CAPITAL
            if hasattr(risk_mgr, "peak_portfolio_value"):
                risk_mgr.peak_portfolio_value = Config.INITIAL_CAPITAL
            # Trade-Counter zuruecksetzen (Churn-Cap, Post-SL-Cooldown)
            recently_traded.clear()
            last_sl_time.clear()
            trades_today_by_symbol.clear()
            print(f"\n  🔄 [Telegram-Reset] Paper-Portfolio zurueckgesetzt auf {Config.INITIAL_CAPITAL:.2f}EUR\n")
            return Config.INITIAL_CAPITAL
        except Exception as e:
            print(f"  [Telegram-Reset] Fehler: {e}")
            return None
    notifier.set_reset_callback(do_reset)

    # /closeall confirm Telegram-Command: alle offenen Positionen zum Markt schliessen.
    # Jede Position wird regulaer verkauft → realisierter P&L in trades.json,
    # Balance bleibt erhalten. Nach /closeall kann man optional /reset senden
    # fuer einen komplett sauberen Neustart.
    def do_closeall():
        try:
            results = []
            for symbol in list(risk_mgr.open_positions.keys()):
                pos = risk_mgr.open_positions[symbol]
                ticker = exchange.get_ticker(symbol)
                if not ticker or not ticker.get("last"):
                    print(f"  [Closeall] {symbol}: kein Preis — skip")
                    continue
                direction = pos.get("direction", "long")
                close_side = "buy" if direction == "short" else "sell"
                res = exchange.place_order(symbol, close_side, pos["volume"], direction=direction)
                if res["status"] != "ok":
                    print(f"  [Closeall] {symbol}: Order-Fehler — {res.get('error', '?')}")
                    continue
                cp = res.get("price", ticker["last"])
                cost = res.get("cost", pos["volume"] * cp)
                fee = res.get("fee", cost * 0.0026)
                if direction == "short":
                    pnl = (pos["entry_price"] - cp) * pos["volume"] - 2 * fee
                    log_side = "cover"
                else:
                    pnl = (cp - pos["entry_price"]) * pos["volume"] - 2 * fee
                    log_side = "sell"
                logger.log_trade(pair=symbol, side=log_side, volume=pos["volume"],
                                 price=cp, cost=cost, fee=fee,
                                 mode=Config.TRADING_MODE, strategy=pos["strategy"],
                                 signal_reason="telegram_closeall",
                                 balance_after=exchange.get_balance(), realized_pnl=pnl)
                risk_mgr.close_position(symbol)
                results.append({"symbol": symbol, "pnl": pnl,
                                "strategy": pos.get("strategy", "?")})
                print(f"  [Closeall] {symbol} geschlossen: P&L {pnl:+.2f}EUR")
            return results
        except Exception as e:
            print(f"  [Telegram-Closeall] Fehler: {e}")
            return None
    notifier.set_closeall_callback(do_closeall)

    # Echten Portfoliowert als Startpunkt setzen (nicht INITIAL_CAPITAL)
    # Verhindert Reset des Tages-P&L nach Neustart
    actual_portfolio = risk_mgr.get_portfolio_value(exchange)
    risk_mgr.daily_start_value = actual_portfolio
    print(f"  [Restore] Tages-Startpunkt: {actual_portfolio:.2f}EUR")

    logger.log_session_start(actual_portfolio)

    # Altlasten schließen: Positionen von Strategien die nicht mehr aktiv sind
    active = Config.ACTIVE_STRATEGIES
    always_keep = {"momentum", "sentiment", "gainer"}  # diese immer behalten
    stale = [
        sym for sym, pos in list(risk_mgr.open_positions.items())
        if pos.get("strategy") not in always_keep and pos.get("strategy") not in active
    ]
    if stale:
        print(f"\n  [Cleanup] Schliesse {len(stale)} Altlasten (inaktive Strategien): {stale}")
        for sym in stale:
            pos = risk_mgr.open_positions[sym]
            res = exchange.place_order(sym, "sell", pos["volume"])
            if res["status"] == "ok":
                cp = res.get("price", pos["entry_price"])
                cost = res.get("cost", pos["volume"] * cp)
                fee = res.get("fee", cost * 0.0026)
                pnl = (cp - pos["entry_price"]) * pos["volume"] - 2 * fee
                logger.log_trade(pair=sym, side="sell", volume=pos["volume"],
                                 price=cp, cost=cost, fee=fee,
                                 mode=Config.TRADING_MODE, strategy=pos["strategy"],
                                 signal_reason="strategy_disabled",
                                 balance_after=exchange.get_balance(), realized_pnl=pnl)
                risk_mgr.close_position(sym)
                print(f"    {sym} geschlossen: P&L {pnl:+.2f}EUR")
        print(f"  [Cleanup] Fertig — Cash: {exchange.get_balance():.2f}EUR")

    # (recently_traded, last_sl_time, trades_today_by_symbol wurden bereits oben
    # initialisiert — vor do_reset() — damit der Telegram-Reset sie clearen kann)

    active = Config.ACTIVE_STRATEGIES
    print(f"\n  Active strategies: {', '.join(active)}")
    print(f"  Starting in 3 seconds...\n")
    time.sleep(3)

    # Optimizer-State laden (Prioritätsliste aus letztem Backtest)
    optimizer_state = load_state()
    Config.MOMENTUM_PRIORITY = optimizer_state.get("priority_list", [])
    if Config.MOMENTUM_PRIORITY:
        print(f"  Momentum-Priorität: {len(Config.MOMENTUM_PRIORITY)} Coins — {Config.MOMENTUM_PRIORITY[:5]}...")
    else:
        print(f"  Momentum-Priorität: leer (Optimizer noch nicht gelaufen — alle Coins gleichwertig)")

    cycle = 0
    peak_portfolio = actual_portfolio
    hwm_pause_until = 0
    # Sauber-Start-Stempel: zeigt explizit, dass alle In-Memory-State-Variablen
    # auf den aktuellen Portfoliowert resettet wurden. Hilft bei Manual-Reset
    # via Portfolio-Edit ohne Restart (peak/HWM bleiben sonst aus altem Lauf).
    print(f"  [CleanStart] peak_portfolio={peak_portfolio:.2f}EUR | "
          f"daily_start={risk_mgr.daily_start_value:.2f}EUR | hwm_pause=off")
    tageslimit_alerted_today = False  # verhindert Telegram-Spam bei dauerhaftem -5%
    # Kill-Switch: {strategy_name: unix_ts_until} — blutende Strategien pausieren
    paused_strategies = {}
    # Gainer-Alert Debounce: {symbol: unix_ts_alerted} — damit nicht jeder Cycle spammt
    alerted_gainers = {}
    # Mega-Gainer (KuCoin-wide, >=100% 24h): eigener Debounce,
    # damit CHIP-artige Pumps gepingt werden auch wenn Kraken nichts listet
    alerted_mega_gainers = {}
    last_mega_scan_ts = 0.0  # Mega-Scan max. alle 5min (KuCoin fetch_tickers ist teuer)
    # Anti-Churn: {symbol: count_heute} — bereits oben als leer initialisiert,
    # damit do_reset() sie clearen kann. Hier nur als Marker-Kommentar.
    today = datetime.now().date()
    last_report_hour = -1  # Stunde des letzten 4h-Berichts

    while True:
        try:
            cycle += 1
            now = datetime.now().strftime("%H:%M:%S")
            print(f"\n{'='*60}")
            print(f"  Cycle {cycle} | {now}")
            print(f"{'='*60}")

            balance = exchange.get_balance()
            portfolio_val = risk_mgr.get_portfolio_value(exchange)

            # Tagesreport-CSV: prueft UTC-Tag-Wechsel, schreibt Zeile fuer Vortag.
            # Erster Aufruf nach Restart legt nur den Tagesanker an.
            daily_summary.tick(portfolio_val, len(risk_mgr.open_positions))

            # 4h-Bericht via Telegram
            current_hour = datetime.now().hour
            if current_hour % 4 == 0 and current_hour != last_report_hour:
                last_report_hour = current_hour
                pos = risk_mgr.open_positions
                daily_pnl_now = risk_mgr.get_daily_pnl_pct(exchange)

                def _pos_pnl(sym, p):
                    t = exchange.get_ticker(sym)
                    cur = (t or {}).get("last", p["entry_price"])
                    d = p.get("direction", "long")
                    return (cur - p["entry_price"]) * p["volume"] if d == "long" else (p["entry_price"] - cur) * p["volume"]

                best  = max(pos.items(), key=lambda x: _pos_pnl(x[0], x[1]), default=(None, None))
                worst = min(pos.items(), key=lambda x: _pos_pnl(x[0], x[1]), default=(None, None))
                best_pnl  = _pos_pnl(best[0],  best[1])  if best[0]  else 0
                worst_pnl = _pos_pnl(worst[0], worst[1]) if worst[0] else 0
                notifier.send(
                    f"📊 *{datetime.now().strftime('%H:%M')} Uhr Update*\n"
                    f"💰 Portfolio: `{portfolio_val:.2f}EUR` ({daily_pnl_now:+.2f}%)\n"
                    f"💵 Cash: `{balance:.2f}EUR`\n"
                    f"📂 Positionen: {len(pos)}\n"
                    + (f"🏆 Bester: {best[0]} `{best_pnl:+.2f}EUR`\n" if best[0] else "")
                    + (f"💀 Schlechtester: {worst[0]} `{worst_pnl:+.2f}EUR`" if worst[0] else "")
                )

            # Wöchentlicher Auto-Optimizer (jeden Sonntag)
            # Hart gewrappt — sonst crasht die ganze Cycle-Schleife bei Binance-Geo-Block
            # (Railway-IP ist aus Sicht Binance "restricted location", HTTP 451).
            if should_run_today():
                try:
                    new_priority = run_optimizer(notifier)
                    Config.MOMENTUM_PRIORITY = new_priority
                    print(f"  [Optimizer] Neue Prioritätsliste: {new_priority[:5]}...")
                except Exception as opt_err:
                    # State mit last_run=heute speichern damit nicht jeder Cycle retriggert
                    st = load_state()
                    st["last_run"] = datetime.now().isoformat()
                    st["last_error"] = str(opt_err)[:200]
                    save_state(st)
                    print(f"  [Optimizer] FEHLER (skip bis nächsten Sonntag): {opt_err}")
                    if notifier:
                        notifier.send(f"⚠️ *Auto-Optimizer übersprungen*\n`{str(opt_err)[:150]}`\nNächster Versuch in 7 Tagen.")

            # Mitternacht-Reset — mit HWM-Amnesie-Schutz:
            # Steigt Portfolio ueber gestrigen Start -> Gewinn sichern, neuer Start.
            # Faellt Portfolio darunter -> Start NICHT senken (Schaden bleibt sichtbar,
            # sonst wird -5% Kill-Switch nutzlos, weil er jeden Tag neu nach unten
            # gedehnt wuerde). Peak ebenfalls NICHT zuruecksetzen - all-time HWM
            # ist Referenz fuer Floor-Lock und 3% Drawdown-Schutz.
            if datetime.now().date() != today:
                today = datetime.now().date()
                prev_start = risk_mgr.daily_start_value
                if portfolio_val >= prev_start:
                    risk_mgr.daily_start_value = portfolio_val
                    start_msg = f"neuer Start: {portfolio_val:.2f}EUR (+{(portfolio_val/prev_start-1)*100:.2f}% gestern)"
                else:
                    start_msg = f"Start bleibt: {prev_start:.2f}EUR (Portfolio {portfolio_val:.2f} darunter)"
                trades_today_by_symbol = {}  # Per-Symbol-Churn-Counter reset
                tageslimit_alerted_today = False
                print(f"  🌅 Neuer Tag — {start_msg} | Peak: {peak_portfolio:.2f}EUR")
                notifier.send(f"🌅 *Neuer Tag*\n{start_msg}\nPeak: `{peak_portfolio:.2f}EUR`")

            # Peak-Tracking zuerst (vor TAGESLIMIT, damit Floor nachgezogen werden kann)
            if portfolio_val > peak_portfolio:
                peak_portfolio = portfolio_val
            drawdown_from_peak = (peak_portfolio - portfolio_val) / peak_portfolio * 100
            meaningful_gain = peak_portfolio > Config.INITIAL_CAPITAL * 1.03  # mind. 3% Gewinn gehabt

            # Proaktives Floor-Tracking (exponentielles Gewinn-Locking):
            # Tages-Start wird auf Peak*0.97 gezogen (= HWM-Trigger-Linie).
            # Greift automatisch ab Peak >+3.1% (dann ist Peak*0.97 > Start).
            # Konsequenz: TAGESLIMIT (-5%) triggert immer vom aktuellen
            # Gewinn-Niveau aus, nicht vom starren Mitternachts-Wert.
            #
            # Beispiel: Start 1000, Peak 1070 → Floor 1037.90
            # → TAGESLIMIT bei 986 statt 950 (32 EUR mehr Schutz).
            new_base = peak_portfolio * 0.97
            if new_base > risk_mgr.daily_start_value:
                print(f"  📈 Floor-Lock: Tages-Start {risk_mgr.daily_start_value:.2f} → {new_base:.2f}EUR (Peak {peak_portfolio:.2f})")
                risk_mgr.daily_start_value = new_base
                tageslimit_alerted_today = False  # neuer Trigger fuer neues Level moeglich

            daily_pnl = risk_mgr.get_daily_pnl_pct(exchange)
            phase = risk_mgr.get_trading_phase(exchange)
            print(f"  Cash: {balance:.2f}EUR | Portfolio: {portfolio_val:.2f}EUR | Tages-P&L: {daily_pnl:+.2f}% [{phase.upper()}]")

            # Tageslimit: bei -5% vom (evtl nachgezogenen) Tages-Start keine neuen Trades bis Mitternacht
            # Telegram nur EINMAL pro Tag (nicht bei jeder Cycle spammen)
            if daily_pnl < -5.0:
                print(f"  🛑 TAGESLIMIT -5% ({daily_pnl:.2f}%) — pausiere bis Mitternacht")
                if not tageslimit_alerted_today:
                    notifier.send(f"🛑 *TAGESLIMIT erreicht*\n{daily_pnl:.2f}% vom Tages-Boden ({risk_mgr.daily_start_value:.2f}EUR)\nKeine neuen Trades bis Mitternacht")
                    tageslimit_alerted_today = True
                time.sleep(Config.CHECK_INTERVAL)
                continue

            # High-Water-Mark: Peak tracken und Gewinn sichern
            # NEUE LOGIK (nach Blutbad 18.04.): close-all realisierte -8.5% Tages-P&L
            # durch Slippage + Winner-Kill. Jetzt:
            #   - Loser schliessen (P&L < 0)
            #   - Winner behalten, aber SL auf Entry+Puffer ziehen (Break-Even-Lock)
            # So schuetzen wir Peak OHNE laufende Gewinner abzuwuergen.
            if meaningful_gain and drawdown_from_peak >= 3.0 and time.time() > hwm_pause_until:
                print(f"\n  🔒 HIGH-WATER-MARK: -{drawdown_from_peak:.1f}% vom Peak ({peak_portfolio:.2f}→{portfolio_val:.2f}EUR)")
                closed = []
                locked = []
                for sym in list(risk_mgr.open_positions.keys()):
                    pos = risk_mgr.open_positions[sym]
                    t = exchange.get_ticker(sym)
                    cur = (t or {}).get("last", pos["entry_price"])
                    d = pos.get("direction", "long")
                    pos_pnl = (cur - pos["entry_price"]) * pos["volume"] if d == "long" else (pos["entry_price"] - cur) * pos["volume"]

                    if pos_pnl < 0:
                        # Verlierer: schliessen
                        res = exchange.place_order(sym, "sell", pos["volume"])
                        if res["status"] == "ok":
                            p = res.get("price", pos["entry_price"])
                            c = res.get("cost", pos["volume"] * p)
                            f = res.get("fee", c * 0.0026)
                            logger.log_trade(pair=sym, side="sell", volume=pos["volume"],
                                             price=p, cost=c, fee=f,
                                             mode=Config.TRADING_MODE, strategy=pos["strategy"],
                                             signal_reason="hwm_close_loser",
                                             balance_after=exchange.get_balance())
                            closed.append(f"{sym} ({pos_pnl:+.2f}EUR)")
                            print(f"    Closed Loser {sym} P&L {pos_pnl:+.2f}EUR")
                        risk_mgr.close_position(sym)
                    else:
                        # Winner: SL auf Entry +/- 0.3% Puffer ziehen (Break-Even-Lock)
                        buffer = 0.003
                        new_sl = pos["entry_price"] * (1 + buffer) if d == "long" else pos["entry_price"] * (1 - buffer)
                        # nur verschieben wenn neuer SL besser ist (fuer long: hoeher, fuer short: niedriger)
                        old_sl = pos.get("sl", pos["entry_price"])
                        if (d == "long" and new_sl > old_sl) or (d == "short" and new_sl < old_sl):
                            pos["sl"] = new_sl
                            locked.append(f"{sym} ({pos_pnl:+.2f}EUR)")
                            print(f"    Break-Even-Lock {sym} P&L {pos_pnl:+.2f}EUR, neuer SL={new_sl:.4f}")
                        else:
                            locked.append(f"{sym} ({pos_pnl:+.2f}EUR, SL schon enger)")

                msg = f"🔒 *HIGH-WATER-MARK* -{drawdown_from_peak:.1f}%\n{peak_portfolio:.2f}→{portfolio_val:.2f}EUR"
                if closed:
                    msg += f"\n❌ Geschlossen ({len(closed)}): {', '.join(closed)}"
                if locked:
                    msg += f"\n🔒 Break-Even-Lock ({len(locked)}): {', '.join(locked)}"
                msg += "\nKeine neuen Trades 30min."
                notifier.send(msg)
                hwm_pause_until = time.time() + 1800  # 30 Minuten Pause fuer neue Trades
                peak_portfolio = risk_mgr.get_portfolio_value(exchange)  # neuer Peak nach Close
                # Tages-Start auf neuen Boden ziehen: HWM hat Verluste abgeschnitten,
                # ab hier gilt der neue Wert als Basis fuer TAGESLIMIT. Ohne Reset
                # wuerde TAGESLIMIT sofort triggern weil der Close selbst -5% gekostet
                # haben kann (Slippage). Mit Reset: -5% vom neuen Boden = echter Stop.
                if peak_portfolio > risk_mgr.daily_start_value:
                    risk_mgr.daily_start_value = peak_portfolio
                tageslimit_alerted_today = False  # neuer Trigger moeglich
                print(f"  Pause fuer neue Trades bis {datetime.fromtimestamp(hwm_pause_until).strftime('%H:%M:%S')} | neuer Tages-Boden {risk_mgr.daily_start_value:.2f}EUR")
                time.sleep(Config.CHECK_INTERVAL)
                continue

            # HWM Pause aktiv
            if time.time() < hwm_pause_until:
                remaining = int((hwm_pause_until - time.time()) / 60)
                print(f"  🔒 HWM-Pause: noch {remaining}min — keine neuen Trades")
                time.sleep(Config.CHECK_INTERVAL)
                continue

            # Kill-Switch: Strategie-Performance der letzten 6h prüfen.
            # Wenn eine Strategie -2% (in EUR: 2% vom Portfolio) netto verloren hat,
            # pausiert sie für 6h. Verhindert Blutungsphasen einzelner Strategien
            # (z.B. gainer feuert 5× in bearishem Markt und haut Kapital raus).
            STRATEGY_PAUSE_THRESHOLD_PCT = 2.0
            STRATEGY_PAUSE_HOURS = 6.0
            # Expired pauses aufräumen
            now_ts = time.time()
            for strat in list(paused_strategies.keys()):
                if paused_strategies[strat] <= now_ts:
                    print(f"  [Kill-Switch] {strat} Pause abgelaufen — wieder aktiv")
                    del paused_strategies[strat]
            # Neue Pausen prüfen (nur alle 5 Cycles, um I/O zu sparen)
            if cycle % 5 == 0:
                perf = logger.get_strategy_performance(hours=STRATEGY_PAUSE_HOURS)
                threshold_eur = -(portfolio_val * STRATEGY_PAUSE_THRESHOLD_PCT / 100)
                for strat, stats in perf.items():
                    if strat in paused_strategies:
                        continue
                    if stats["pnl"] <= threshold_eur and stats["trades"] >= 3:
                        paused_strategies[strat] = now_ts + STRATEGY_PAUSE_HOURS * 3600
                        winrate = stats["wins"] / stats["trades"] * 100 if stats["trades"] else 0
                        msg = (f"[Kill-Switch] {strat} PAUSIERT für {STRATEGY_PAUSE_HOURS:.0f}h "
                               f"— 6h-P&L {stats['pnl']:+.2f}EUR ({stats['trades']} Trades, "
                               f"WR {winrate:.0f}%)")
                        print(f"\n  🚨 {msg}\n")
                        notifier.send(f"🚨 *{msg}*")
            if paused_strategies:
                paused_list = ", ".join(f"{s}({int((ts-now_ts)/60)}m)" for s, ts in paused_strategies.items())
                print(f"  [Kill-Switch] Pausiert: {paused_list}")

            # Markt-Trend-Filter via BTC
            btc_df = exchange.get_ohlcv("BTC/EUR", "1h", limit=3)  # 3h statt 6h → schnellere Reaktion
            market_bullish = False
            market_bearish = False
            if not btc_df.empty:
                btc_change = (btc_df["close"].iloc[-1] - btc_df["close"].iloc[0]) / btc_df["close"].iloc[0]
                market_bullish = btc_change > 0.004
                market_bearish = btc_change < -0.004
                trend_str = f"BTC {btc_change*100:+.2f}% → {'BULLISH' if market_bullish else 'BEARISH' if market_bearish else 'NEUTRAL'}"
                print(f"  Markt: {trend_str}")

            # Regime-Gate: Neue Entries NUR in passendem Trend-Regime.
            # NEUTRAL = nichts öffnen, nur manage. Grund: 12h-Log zeigte 0/9 Winrate,
            # ~52% aller Entries in NEUTRAL — Whipsaws + 0,26% Fees = negative Expectancy.
            allow_long_entries = market_bullish
            allow_short_entries = market_bearish
            regime_state = "BULLISH" if market_bullish else "BEARISH" if market_bearish else "NEUTRAL"

            # Log 22.04 Lehre: 35 valide SHORT-Breakdowns (AVAX, APE, CRV, ...) wurden
            # in NEUTRAL geblockt während Longs ausgebluted sind. Erweiterung: in NEUTRAL
            # zusätzlich Shorts erlauben wenn BTC 15m-Trend negativ ist (= Markt rutscht
            # intraday, selbst wenn 3h-Filter noch NEUTRAL zeigt). Longs bleiben strikt
            # BULLISH-only, da Log 17.04 zeigte: Longs in Micro-Downtrend = Gift.
            neutral_short_ok = False
            if not market_bullish and not market_bearish:  # NEUTRAL
                btc_15m = exchange.get_ohlcv("BTC/EUR", "15m", limit=4)
                if not btc_15m.empty and len(btc_15m) >= 2:
                    btc_15m_change = (btc_15m["close"].iloc[-1] - btc_15m["close"].iloc[0]) / btc_15m["close"].iloc[0]
                    # 29.04.2026: Schwelle von <0 auf <-0.5% verschaerft.
                    # Audit zeigte 4/4 NEUTRAL-Shorts verloren weil 15m-Noise (-0.01%
                    # bis -0.3%) als "Bear-Signal" gewertet wurde. Echtes
                    # Intraday-Rutschen braucht mindestens -0.5%.
                    if btc_15m_change < Config.NEUTRAL_SHORT_BTC_15M_THRESHOLD:
                        allow_short_entries = True
                        neutral_short_ok = True
                        print(f"  [Regime-Ext] NEUTRAL + BTC 15m {btc_15m_change*100:+.2f}% (<{Config.NEUTRAL_SHORT_BTC_15M_THRESHOLD*100:.1f}%) → Shorts erlaubt")
                    elif btc_15m_change < 0:
                        print(f"  [Regime-Ext] NEUTRAL + BTC 15m {btc_15m_change*100:+.2f}% → unter Schwelle ({Config.NEUTRAL_SHORT_BTC_15M_THRESHOLD*100:.1f}%), Shorts geblockt")

            # Marktkontext-Exit: Shorts schließen wenn BTC dreht bullisch
            # - Verlierer mit >0.5% Minus → cut (Verlust abfedern)
            # - Gewinner ab +1.5% (nach Fees ~+1%) → lock gain (Profit sichern bevor Trend dreht)
            # - Positionen im 0/0.5%-Dead-Zone laufen weiter
            if market_bullish:
                for sym in list(risk_mgr.open_positions.keys()):
                    pos = risk_mgr.open_positions[sym]
                    if pos.get("direction") != "short":
                        continue
                    ticker = exchange.get_ticker(sym)
                    if not ticker or not ticker.get("last"):
                        continue
                    cur = ticker["last"]
                    pnl_pct = (pos["entry_price"] - cur) / pos["entry_price"]
                    reason = None
                    if pnl_pct < -0.005:
                        reason = f"Verlust -{abs(pnl_pct)*100:.1f}% abfedern"
                    elif pnl_pct > 0.015:
                        reason = f"Gewinn +{pnl_pct*100:.1f}% sichern (Trend dreht)"
                    if reason:
                        print(f"  [MarktExit] BTC bullisch + {sym} SHORT → {reason}")
                        res = exchange.place_order(sym, "buy", pos["volume"], direction="short")
                        if res["status"] == "ok":
                            cp = res.get("price", cur)
                            cost = res.get("cost", pos["volume"] * cp)
                            fee = res.get("fee", cost * 0.0026)
                            pnl = (pos["entry_price"] - cp) * pos["volume"] - 2 * fee
                            logger.log_trade(pair=sym, side="cover", volume=pos["volume"],
                                             price=cp, cost=cost, fee=fee,
                                             mode=Config.TRADING_MODE, strategy=pos["strategy"],
                                             signal_reason="market_context_exit_bullish",
                                             balance_after=exchange.get_balance(), realized_pnl=pnl)
                            risk_mgr.close_position(sym)
                            port_val = risk_mgr.get_portfolio_value(exchange)
                            d_pnl = risk_mgr.get_daily_pnl_pct(exchange)
                            notifier.notify_exit(sym, "market_context_exit", pnl, pos["strategy"], port_val, d_pnl)
                            print(f"    Geschlossen: {sym} P&L {pnl:+.2f}EUR")

            # Marktkontext-Exit: Longs schließen wenn BTC dreht bearisch
            # - Verlierer mit >0.5% Minus → cut
            # - Gewinner ab +1.5% → lock gain
            if market_bearish:
                for sym in list(risk_mgr.open_positions.keys()):
                    pos = risk_mgr.open_positions[sym]
                    if pos.get("direction") != "long":
                        continue
                    if pos.get("strategy") in ("dca", "grid", "gainer"):
                        continue  # DCA/Grid/Gainer haben eigene Logik
                    ticker = exchange.get_ticker(sym)
                    if not ticker or not ticker.get("last"):
                        continue
                    cur = ticker["last"]
                    pnl_pct = (cur - pos["entry_price"]) / pos["entry_price"]
                    reason = None
                    if pnl_pct < -0.005:
                        reason = f"Verlust -{abs(pnl_pct)*100:.1f}% abfedern"
                    elif pnl_pct > 0.015:
                        reason = f"Gewinn +{pnl_pct*100:.1f}% sichern (Trend dreht)"
                    if reason:
                        print(f"  [MarktExit] BTC bearisch + {sym} LONG → {reason}")
                        res = exchange.place_order(sym, "sell", pos["volume"])
                        if res["status"] == "ok":
                            cp = res.get("price", cur)
                            cost = res.get("cost", pos["volume"] * cp)
                            fee = res.get("fee", cost * 0.0026)
                            pnl = (cp - pos["entry_price"]) * pos["volume"] - 2 * fee
                            logger.log_trade(pair=sym, side="sell", volume=pos["volume"],
                                             price=cp, cost=cost, fee=fee,
                                             mode=Config.TRADING_MODE, strategy=pos["strategy"],
                                             signal_reason="market_context_exit_bearish",
                                             balance_after=exchange.get_balance(), realized_pnl=pnl)
                            risk_mgr.close_position(sym)
                            port_val = risk_mgr.get_portfolio_value(exchange)
                            d_pnl = risk_mgr.get_daily_pnl_pct(exchange)
                            notifier.notify_exit(sym, "market_context_exit", pnl, pos["strategy"], port_val, d_pnl)
                            print(f"    Geschlossen: {sym} P&L {pnl:+.2f}EUR")

            # Fix 3: Verlustbremse — bei -3% Tages-P&L keine neuen Longs
            loss_brake = daily_pnl < -3.0
            if loss_brake:
                print(f"  ⚠️  VERLUSTBREMSE aktiv ({daily_pnl:.2f}%) — nur Shorts erlaubt")

            # 1. Check exits first
            exits = check_exits(exchange, risk_mgr, logger, notifier,
                                last_sl_time=last_sl_time, last_win_time=last_win_time)
            for sym, etype, pnl in exits:
                print(f"    Exited {sym}: {etype} P&L {pnl:+.2f}EUR")

            # Time-Stop: Positionen die >X Stunden offen sind UND P&L flatlined
            # (zwischen ±Y%) → Soft-Close. Verhindert Slot-Belegung durch hängende
            # Trades. Lehre 27.04.2026: 4 SHORTs hingen 8h+ flatlined und blockierten
            # 37 weitere Setup-Versuche. Gainer/DCA/Grid sind ausgenommen — die haben
            # eigene Akkumulationslogik.
            ts_secs = Config.POSITION_TIME_STOP_HOURS * 3600
            ts_max_pnl = Config.POSITION_TIME_STOP_MAX_PNL_PCT / 100.0
            for sym in list(risk_mgr.open_positions.keys()):
                pos = risk_mgr.open_positions[sym]
                if pos.get("strategy") in ("gainer", "dca", "grid"):
                    continue
                opened_at = pos.get("opened_at", 0)
                if not opened_at:
                    continue  # Alt-Positionen ohne Zeitstempel ignorieren (Restart-safe)
                age_h = (time.time() - opened_at) / 3600
                if age_h < Config.POSITION_TIME_STOP_HOURS:
                    continue
                ticker = exchange.get_ticker(sym)
                cur = (ticker or {}).get("last")
                if not cur:
                    continue
                d = pos.get("direction", "long")
                pnl_pct = (cur - pos["entry_price"]) / pos["entry_price"]
                if d == "short":
                    pnl_pct = -pnl_pct
                if abs(pnl_pct) > ts_max_pnl:
                    continue  # Position lebt noch — TP/SL kann greifen
                # Flatlined → close
                print(f"  [TimeStop] {sym} {d.upper()} {age_h:.1f}h offen, "
                      f"P&L {pnl_pct*100:+.2f}% (innerhalb ±{Config.POSITION_TIME_STOP_MAX_PNL_PCT}%) → Soft-Close")
                res = exchange.place_order(sym, "sell", pos["volume"])
                if res["status"] == "ok":
                    cp = res.get("price", cur)
                    cost = res.get("cost", pos["volume"] * cp)
                    fee = res.get("fee", cost * 0.0026)
                    if d == "long":
                        pnl_eur = (cp - pos["entry_price"]) * pos["volume"] - 2 * fee
                    else:
                        pnl_eur = (pos["entry_price"] - cp) * pos["volume"] - 2 * fee
                    log_side = "cover" if d == "short" else "sell"
                    logger.log_trade(pair=sym, side=log_side, volume=pos["volume"],
                                     price=cp, cost=cost, fee=fee,
                                     mode=Config.TRADING_MODE, strategy=pos["strategy"],
                                     signal_reason="time_stop_flatlined",
                                     balance_after=exchange.get_balance(),
                                     realized_pnl=pnl_eur)
                    risk_mgr.close_position(sym)
                    print(f"    Closed {sym}: P&L {pnl_eur:+.2f}EUR")

            # Gainer-Slot Status: max 2 parallel (MAX_GAINER_POSITIONS=2)
            gainer_count = sum(
                1 for p in risk_mgr.open_positions.values() if p.get("strategy") == "gainer"
            )
            gainer_slot_occupied = gainer_count >= risk_mgr.MAX_GAINER_POSITIONS
            if gainer_count > 0:
                gainer_syms = [f"{sym}[{p['entry_price']:.4f}]"
                               for sym, p in risk_mgr.open_positions.items()
                               if p.get("strategy") == "gainer"]
                print(f"  [Gainer Slots] {gainer_count}/{risk_mgr.MAX_GAINER_POSITIONS}: {', '.join(gainer_syms)}")
            if not gainer_slot_occupied:
                print(f"  [Gainer Slot] Frei ({risk_mgr.MAX_GAINER_POSITIONS-gainer_count} offen) — suche Coin mit >{Config.GAINER_MIN_GAIN_24H:.0f}% 24h Gewinn")

            # Schutz-Phase: keine neuen Trades, bestehende Positionen laufen weiter
            if phase == "protect":
                print(f"  [PROTECT] Nahe am Ziel — keine neuen Trades, Trailing-SL aktiv ({daily_pnl:+.2f}%)")
                print(f"\n  Next scan in {Config.CHECK_INTERVAL}s...")
                time.sleep(Config.CHECK_INTERVAL)
                continue

            # 2. Scan for best coins
            scan_results = scanner.scan()
            scanner.print_results(scan_results[:Config.AUTO_PICK_COUNT])
            target_symbols = [r["symbol"] for r in scan_results[:Config.AUTO_PICK_COUNT]]
            # Stablecoin-Blacklist (USDT/USDC/DAI/...): nie handeln. Strukturell ~0% PnL,
            # blockieren nur Slots. Vor allem USDT/EUR-SHORT lag 3 Tage bei -0,09 EUR
            # und hielt einen SHORT-Cap-Slot fest (24./25.04.2026).
            blacklisted_in_target = [s for s in target_symbols if _is_blacklisted(s)]
            if blacklisted_in_target:
                print(f"  [Blacklist] Stablecoins gefiltert: {', '.join(blacklisted_in_target)}")
                target_symbols = [s for s in target_symbols if not _is_blacklisted(s)]

            # Gainer Discovery + Alarm: IMMER scannen (auch bei vollen Slots),
            # damit der Alarm bei besonders starken Gainern feuern kann — du
            # kannst dann manuell entscheiden ob du 'ne schwache Position fuer
            # den Super-Gainer opferst.
            gainer_gain_lookup = {}  # symbol -> 24h change_pct (fuer per-symbol gainer check)
            all_eur_pairs = [p for p in exchange.get_all_eur_pairs()
                             if p not in CoinScanner.SKIP_COINS and p not in risk_mgr.open_positions]
            all_tickers = exchange.get_tickers_bulk(all_eur_pairs)

            # Alarm-Scan: alle Kandidaten ueber ALERT_THRESHOLD, debounced
            alert_cutoff = time.time() - Config.GAINER_ALERT_DEBOUNCE_HOURS * 3600
            for sym, alerted_ts in list(alerted_gainers.items()):
                if alerted_ts < alert_cutoff:
                    del alerted_gainers[sym]
            for sym, t in all_tickers.items():
                gain = t.get("change_pct") or 0
                if gain < Config.GAINER_ALERT_THRESHOLD:
                    continue
                if (t.get("volume") or 0) < 50000:
                    continue
                if sym in alerted_gainers:
                    continue
                price = t.get("last", 0)
                volume = t.get("volume", 0)
                bot_status = ("Bot oeffnet automatisch" if not gainer_slot_occupied
                              else f"Slots voll ({gainer_count}/{risk_mgr.MAX_GAINER_POSITIONS}) — manuell pruefen")
                msg = (f"🚀 GAINER-ALARM: {sym}\n"
                       f"24h: +{gain:.0f}%\n"
                       f"Preis: {price:.4f}EUR\n"
                       f"Volumen: {volume:,.0f}\n"
                       f"Status: {bot_status}")
                print(f"\n  🚀 [Gainer-Alarm] {sym} +{gain:.0f}% → Telegram\n")
                notifier.send(msg)
                alerted_gainers[sym] = time.time()

            # Mega-Gainer-Alarm: KuCoin-weiter Scan (alles, nicht nur Kraken-EUR),
            # damit Pumps wie CHIP (+600%) gepingt werden selbst wenn Kraken sie
            # nicht listet. Max alle 5 Minuten um KuCoin-API zu schonen.
            mega_now = time.time()
            if mega_now - last_mega_scan_ts >= 300:
                last_mega_scan_ts = mega_now
                mega_cutoff = mega_now - Config.MEGA_GAINER_DEBOUNCE_HOURS * 3600
                for k, ts in list(alerted_mega_gainers.items()):
                    if ts < mega_cutoff:
                        del alerted_mega_gainers[k]
                mega_hits = mega_scanner.get_mega_gainers(
                    min_gain_pct=Config.MEGA_GAINER_THRESHOLD,
                    min_volume_usdt=Config.MEGA_GAINER_MIN_VOL_USDT,
                )
                if mega_hits:
                    print(f"  🔥 [Mega-Gainer] {len(mega_hits)} Coin(s) >= +{Config.MEGA_GAINER_THRESHOLD:.0f}% 24h auf KuCoin")
                for hit in mega_hits:
                    key = hit["symbol"]
                    if key in alerted_mega_gainers:
                        continue
                    base = hit["base"]
                    gain = hit["gain_24h"]
                    price_usdt = hit["price"]
                    vol_usdt = hit["volume_usdt"]
                    tradeable = exchange.has_eur_pair(base)
                    if tradeable:
                        trade_line = f"✅ Kraken {base}/EUR verfuegbar — Bot kann einsteigen"
                    else:
                        trade_line = f"❌ Nicht auf Kraken — nur Info (manuell z.B. KuCoin/Binance)"
                    msg = (f"🔥 *MEGA-GAINER*: `{key}`\n"
                           f"24h: *+{gain:.0f}%*\n"
                           f"Preis: `{price_usdt:.6f} USDT`\n"
                           f"Volumen: `{vol_usdt:,} USDT`\n"
                           f"{trade_line}")
                    print(f"  🔥 [Mega-Gainer] {key} +{gain:.0f}% (Kraken: {'yes' if tradeable else 'no'}) → Telegram")
                    notifier.send(msg)
                    alerted_mega_gainers[key] = mega_now

            # Slot-Filling: Top-N Gainer fuer freie Slots
            if not gainer_slot_occupied:
                slots_free = risk_mgr.MAX_GAINER_POSITIONS - gainer_count
                candidates = sorted(
                    ((sym, t.get("change_pct", 0)) for sym, t in all_tickers.items()
                     if (t.get("change_pct") or 0) >= Config.GAINER_MIN_GAIN_24H
                     and (t.get("volume") or 0) >= 50000),
                    key=lambda x: x[1], reverse=True,
                )[:slots_free]
                if candidates:
                    for sym, gain in candidates:
                        gainer_gain_lookup[sym] = gain
                        if sym not in target_symbols:
                            target_symbols.insert(0, sym)
                    cand_str = ", ".join(f"{s} +{g:.0f}%" for s, g in candidates)
                    print(f"  [Gainer] Kandidaten ({len(candidates)}/{slots_free}): {cand_str}")
                else:
                    print(f"  [Gainer] Kein Coin mit >{Config.GAINER_MIN_GAIN_24H:.0f}% gefunden")

            # 3. Check news sentiment
            # Ghost-Symbol-Filter: Gemini erfindet manchmal Ticker aus News-Headlines
            # die es auf Kraken nicht gibt (DAT, THOR, POLYM, RUSSIA_CRYPTO,
            # SPACEX_TOKEN, BINANCE_AI_WALLET, OKX, PUSD, etc.). Ohne Filter →
            # 30+ OHLCV-Errors pro Coin und crasht den Bulk-Ticker-Call mit
            # "Unknown asset pair". has_eur_pair() prueft Kraken-Markets direkt.
            if "sentiment" in active:
                news_signals = sentiment.check_news()
                sentiment_wl = [s.strip() for s in Config.SENTIMENT_WHITELIST if s and s.strip()]
                for sym, sig in news_signals.items():
                    base = sym.split("/")[0]
                    if not exchange.has_eur_pair(base):
                        if sym not in ghost_symbols_logged:
                            print(f"  [Sentiment] Ghost-Symbol ignoriert: {sym} (nicht auf Kraken)")
                            ghost_symbols_logged.add(sym)
                        continue
                    if _is_blacklisted(sym):
                        # Stablecoins (USDT/USDC/...) gar nicht erst durch Sentiment einschleusen.
                        continue
                    # Whitelist: nur BTC/EUR (default). Alts reagieren nicht auf News.
                    if sym.upper() not in sentiment_wl:
                        continue
                    # Min-Score: BTC ist taeglich News, nur sehr starkes Sentiment zaehlt.
                    score_abs = abs(sig.get("score", 0) or 0)
                    if score_abs < Config.SENTIMENT_MIN_SCORE:
                        print(f"  [Sentiment] {sym} score={sig.get('score')} unter Schwelle "
                              f"{Config.SENTIMENT_MIN_SCORE} — ignoriert (BTC-News-Noise)")
                        continue
                    if sym not in target_symbols:
                        target_symbols.append(sym)

            # Kein Zeit-Filter mehr — Gainer duerfen 24/7 feuern.
            # Schutz statt Uhrzeit: Liquidity-Gate in execute_gainer_trade prueft
            # Kraken-EUR-Orderbook direkt (Volume + Spread). So bleiben legitime
            # Asia-Pumps handelbar, duenne Orderbooks werden trotzdem geblockt.

            # 4. Run strategies on each target
            for symbol in target_symbols:
                if symbol in risk_mgr.open_positions:
                    continue  # already holding
                # Fix 2: Cooldown — 30min nach letztem Trade in diesem Coin
                cooldown_secs = 30 * 60
                if symbol in recently_traded and (time.time() - recently_traded[symbol]) < cooldown_secs:
                    remaining = int((cooldown_secs - (time.time() - recently_traded[symbol])) / 60)
                    print(f"  {symbol} Cooldown noch {remaining}min")
                    continue

                # Post-SL-Cooldown: nach Stop-Loss 6h harte Pause fuer dieses Symbol.
                # Log 22./23.04 SPK-Desaster: 4x gekauft trotz 3 SLs. Mit 6h-Cooldown
                # waeren Kaeufe #2-#4 alle geblockt worden (+14EUR gespart).
                sl_cooldown_secs = Config.POST_SL_COOLDOWN_HOURS * 3600
                if symbol in last_sl_time and (time.time() - last_sl_time[symbol]) < sl_cooldown_secs:
                    remaining_h = (sl_cooldown_secs - (time.time() - last_sl_time[symbol])) / 3600
                    print(f"  {symbol} Post-SL-Cooldown noch {remaining_h:.1f}h (Stop-Loss vor {(time.time()-last_sl_time[symbol])/3600:.1f}h)")
                    continue

                # Win-Cooldown: nach profitablem Exit WIN_COOLDOWN_HOURS Pause.
                # Lehre 27.04. ORCA: Win +7.02EUR → 2.5h spaeter Re-Entry @ -4.98EUR.
                # Setup ist durch, Re-Entry kommt zu spaet im Run.
                win_cooldown_secs = Config.WIN_COOLDOWN_HOURS * 3600
                if symbol in last_win_time and (time.time() - last_win_time[symbol]) < win_cooldown_secs:
                    remaining_m = int((win_cooldown_secs - (time.time() - last_win_time[symbol])) / 60)
                    print(f"  {symbol} Win-Cooldown noch {remaining_m}min (profit-exit vor {(time.time()-last_win_time[symbol])/60:.0f}min)")
                    continue

                print(f"\n  Analyzing {symbol}...")
                df = exchange.get_ohlcv(symbol, "15m", limit=100)
                if df.empty:
                    continue

                balance = exchange.get_balance()
                if balance < 2:
                    print("  Low balance, skipping new trades")
                    break

                signals = []

                # Momentum (Long + Short) — BTC als Gewichtung + Verlustbremse
                # Regime-Gate HART: kein strong-Bypass mehr. Log 17.04 zeigte 4x SHORT
                # auf Junk-Alts (BASED/BIO) in BULLISH-Markt — alle gegrillt, weil
                # strong=leverage>=2 den Gate umgangen hat. Trend-Kampf kostet strukturell.
                if "momentum" in active:
                    sig = momentum.analyze(df)
                    if sig["signal"] == Signal.BUY:
                        if loss_brake:
                            pass  # Verlustbremse: keine Longs
                        elif not market_bearish:
                            signals.append(sig)
                            print(f"    [momentum] BUY: {sig['reason']}")
                    elif sig["signal"] == Signal.SELL:
                        if not market_bullish:
                            sig["direction"] = "short"
                            signals.append(sig)
                            print(f"    [momentum] SHORT: {sig['reason']}")

                # Grid
                if "grid" in active:
                    sig = grid.analyze(df, symbol)
                    if sig["signal"] == Signal.BUY:
                        signals.append(sig)
                        print(f"    [grid] BUY: {sig['reason']}")

                # DCA (only for top-scored coins)
                if "dca" in active:
                    coin_data = next((r for r in scan_results if r["symbol"] == symbol), None)
                    if coin_data and coin_data["score"] >= 5:
                        sig = dca.analyze(df, symbol)
                        if sig["signal"] == Signal.BUY:
                            signals.append(sig)
                            print(f"    [dca] BUY: {sig['reason']}")

                # Sentiment — nur BTC/EUR (Whitelist) + Min-Score (default 8).
                # Lehre 25.-27.04.2026: Sentiment hat 0 profitable Trades erzeugt,
                # XRP/Alts reagieren kaum auf News. BTC-only + hoher Score-Cut
                # filtert das tägliche BTC-News-Rauschen raus.
                _sent_wl = [s.strip() for s in Config.SENTIMENT_WHITELIST if s and s.strip()]
                if (
                    "sentiment" in active
                    and symbol in sentiment.signals
                    and symbol.upper() in _sent_wl
                ):
                    sig = dict(sentiment.signals[symbol])  # copy — don't mutate cache
                    # Min-Score-Gate (zweite Verteidigungslinie, auch wenn das Symbol
                    # schon ueber den ghost-block-Filter durchgekommen ist)
                    _score_abs = abs(sig.get("score", 0) or 0)
                    if _score_abs < Config.SENTIMENT_MIN_SCORE:
                        pass  # zu schwach, Sentiment-Signal verwerfen
                    else:
                        # Sentiment-Signal hat keinen Preis (kommt nur aus Headline-Score).
                        # Ohne Preis berechnet calculate_position_size() volume=0 → Skip.
                        # Deshalb aktuellen Close aus dem OHLCV-Frame injecten.
                        try:
                            sig["price"] = float(df["close"].iloc[-1])
                        except Exception:
                            sig["price"] = 0
                        if sig["signal"] == Signal.BUY:
                            if loss_brake:
                                pass  # Verlustbremse: keine Longs
                            elif not market_bearish:
                                signals.append(sig)
                                print(f"    [sentiment] BUY: {sig['reason']}")
                        elif sig["signal"] == Signal.SELL:
                            if not market_bullish:
                                sig["direction"] = "short"
                                signals.append(sig)
                                print(f"    [sentiment] SHORT: {sig['reason']}")

                # Gainer: Kraken-Coin mit extremem 24h-Gewinn — höchste Priorität, 1 fixer Slot
                # Kein Zeit-Filter mehr — Liquidity-Gate in execute_gainer_trade schuetzt stattdessen.
                if not gainer_slot_occupied:
                    coin_data = next((r for r in scan_results if r["symbol"] == symbol), None)
                    # Fallback: Discovery-Coin (z.B. MOVR) ist nicht in top-50 scan_results,
                    # aber in gainer_gain_lookup aus get_all_eur_pairs — sonst gain_24h=0
                    # und Gainer-Check schlaegt silent fehl.
                    gain_24h = (coin_data.get("change_pct", 0) if coin_data
                                else gainer_gain_lookup.get(symbol, 0))
                    if gain_24h >= Config.GAINER_MIN_GAIN_24H:
                        sig = gainer_strategy.analyze(df, gain_24h)
                        if sig["signal"] == Signal.BUY:
                            signals.append(sig)
                            print(f"    [gainer] BUY: {sig['reason']}")
                        else:
                            print(f"    [gainer] SKIP {symbol} +{gain_24h:.0f}%: {sig['reason']}")

                # Sentiment-Confirm-Gate: Sentiment darf NICHT mehr allein triggern.
                # Es muss von einer TA-Strategie (momentum/grid) in DERSELBEN Richtung
                # bestaetigt werden. News-Score allein hat in 76k Logzeilen 0 profitable
                # Trades erzeugt — TA-Confirmation ist der Filter gegen Hype-Echo.
                # Momentum confirmt LONG+SHORT, Grid confirmt nur LONG (Range-Buy).
                sent_in_signals = [s for s in signals if s.get("strategy") == "sentiment"]
                if sent_in_signals:
                    confirming = ("momentum", "grid")
                    for ss in list(sent_in_signals):
                        sent_dir = ss.get("direction", "long")
                        confirmed = any(
                            s.get("strategy") in confirming
                            and s.get("direction", "long") == sent_dir
                            for s in signals
                            if s is not ss
                        )
                        if not confirmed:
                            print(f"    [sentiment] {symbol} {sent_dir.upper()}: "
                                  f"keine TA-Bestaetigung (momentum/grid in selber Richtung) — verworfen")
                            signals.remove(ss)

                # Execute best signal (highest priority: gainer > sentiment > momentum > grid > dca)
                if signals:
                    priority = {"gainer": 5, "sentiment": 4, "momentum": 3, "grid": 2, "dca": 1}
                    best = max(signals, key=lambda s: priority.get(s.get("strategy", ""), 0))
                    direction = best.get("direction", "long")
                    side = "sell" if direction == "short" else "buy"

                    # Signal-Strength: bestimmt Position-Sizing (200 vs 100 EUR).
                    # "strong" wenn:
                    #   - Momentum mit lev>=3 (selten, echtes Trend-Setup)
                    #   - Sentiment+Momentum gleiche Richtung (TA-Confirm + News)
                    # Sonst "normal" (100 EUR Default).
                    _best_dir = best.get("direction", "long")
                    _best_strat = best.get("strategy", "")
                    _best_lev = best.get("leverage", 1) or 1
                    _has_sentiment_confirm = any(
                        s.get("strategy") == "sentiment"
                        and s.get("direction", "long") == _best_dir
                        for s in signals
                    )
                    _has_momentum_confirm = any(
                        s.get("strategy") == "momentum"
                        and s.get("direction", "long") == _best_dir
                        for s in signals
                    )
                    _has_grid_confirm = any(
                        s.get("strategy") == "grid"
                        and s.get("direction", "long") == _best_dir
                        for s in signals
                    )
                    # 29.04.2026: STRONG-Tier breiter. Bisher feuerte nie STRONG (11/11 NORMAL),
                    # weil Sentiment-Confirm Voraussetzung war und Sentiment selbst nie firet.
                    # Jetzt: jede 2-Strategien-Confluence (momentum+grid, sentiment+momentum,
                    # sentiment+grid) zaehlt als STRONG. Plus weiterhin lev>=3 momentum.
                    _confirms = sum([_has_sentiment_confirm, _has_momentum_confirm, _has_grid_confirm])
                    if _best_strat == "momentum" and _best_lev >= 3:
                        best["signal_strength"] = "strong"
                    elif _confirms >= 2:
                        best["signal_strength"] = "strong"
                    else:
                        best["signal_strength"] = "normal"

                    # Regime-Gate: LONGs nur in BULLISH, SHORTs nur in BEARISH.
                    # Gainer ist ausgenommen — hat eigenen 15%-24h-Filter, funktioniert
                    # regime-unabhängig. DCA/Grid sind Akkumulation bzw. Range — diese
                    # dürfen auch in NEUTRAL laufen.
                    strat = best.get("strategy", "")

                    # Kill-Switch: pausierte Strategien keine neuen Entries
                    if strat in paused_strategies:
                        mins_left = int((paused_strategies[strat] - time.time()) / 60)
                        print(f"    Skip {symbol}: Strategie {strat} pausiert (Kill-Switch, {mins_left}m)")
                        continue

                    # Anti-Churn: max N Trades pro Symbol pro Tag
                    sym_trades_today = trades_today_by_symbol.get(symbol, 0)
                    if sym_trades_today >= Config.MAX_TRADES_PER_SYMBOL_PER_DAY:
                        print(f"    Skip {symbol}: Churn-Cap erreicht ({sym_trades_today}/{Config.MAX_TRADES_PER_SYMBOL_PER_DAY} Trades heute)")
                        continue

                    regime_exempt = strat in ("gainer", "dca", "grid")
                    if not regime_exempt:
                        # High-Conviction-Bypass: in NEUTRAL werden normalerweise alle
                        # Entries geblockt (Whipsaw-Schutz). Wenn Sentiment-Score absolut
                        # >= HIGH_CONVICTION_SENTIMENT_SCORE (default 7) ODER Momentum
                        # mit leverage >= HIGH_CONVICTION_MOMENTUM_LEVERAGE (default 2)
                        # → Signal ist stark genug, durchlassen. Nur in NEUTRAL — gegen
                        # die echte Trend-Richtung (BULLISH/BEARISH) niemals bypassen.
                        # Lehre 17.04: strong-Bypass GEGEN Trend = 4x SHORT in BULLISH = -X EUR.
                        score_abs = abs(best.get("score", 0) or 0)
                        leverage = best.get("leverage", 1) or 1
                        high_conv = (
                            (strat == "sentiment" and score_abs >= Config.HIGH_CONVICTION_SENTIMENT_SCORE)
                            or (strat == "momentum" and leverage >= Config.HIGH_CONVICTION_MOMENTUM_LEVERAGE)
                        )
                        only_neutral = (regime_state == "NEUTRAL")

                        if direction == "long" and not allow_long_entries:
                            if high_conv and only_neutral:
                                print(f"    [HighConviction-Bypass] {symbol} LONG: "
                                      f"{strat} score={score_abs} lev={leverage} "
                                      f"durchgelassen trotz Regime {regime_state}")
                            else:
                                print(f"    Skip {symbol}: LONG blockiert (Regime {regime_state})")
                                continue
                        if direction == "short" and not allow_short_entries:
                            if high_conv and only_neutral:
                                print(f"    [HighConviction-Bypass] {symbol} SHORT: "
                                      f"{strat} score={score_abs} lev={leverage} "
                                      f"durchgelassen trotz Regime {regime_state}")
                            else:
                                print(f"    Skip {symbol}: SHORT blockiert (Regime {regime_state})")
                                continue

                    # Rotation: if strong signal (leverage>=2 OR scanner score>=10) but slots full or barely any cash
                    weakest = None
                    coin_score = next((r["score"] for r in scan_results if r["symbol"] == symbol), 0)
                    is_strong = best.get("leverage", 1) >= Config.ROTATION_MIN_LEVERAGE or coin_score >= 10
                    no_slots = len(risk_mgr.open_positions) >= risk_mgr.max_positions
                    low_cash = balance < 20  # only rotate if barely any cash left
                    if is_strong and (no_slots or low_cash):
                        weakest = risk_mgr.get_weakest_position(exchange)
                        if weakest and weakest != symbol:
                            print(f"    >> Rotation: closing {weakest} to free up capital for {symbol}")
                            weak_pos = risk_mgr.open_positions[weakest]
                            rot_result = exchange.place_order(weakest, "sell", weak_pos["volume"])
                            if rot_result["status"] == "ok":
                                rot_price = rot_result.get("price", 0)
                                rot_pnl = (rot_price - weak_pos["entry_price"]) * weak_pos["volume"]
                                rot_cost = rot_result.get("cost", weak_pos["volume"] * rot_price)
                                rot_fee = rot_result.get("fee", rot_cost * 0.0026)
                                logger.log_trade(pair=weakest, side="sell", volume=weak_pos["volume"],
                                                 price=rot_price, cost=rot_cost, fee=rot_fee,
                                                 mode=Config.TRADING_MODE, strategy=weak_pos["strategy"],
                                                 signal_reason="rotation",
                                                 balance_after=exchange.get_balance())
                                risk_mgr.close_position(weakest)
                                balance = exchange.get_balance()
                                print(f"    Closed {weakest} P&L {rot_pnl:+.2f}EUR, cash now {balance:.2f}EUR")

                    print(f"    >> Executing {best['strategy']} {direction.upper()} signal")
                    if best["strategy"] == "gainer":
                        traded = execute_gainer_trade(exchange, risk_mgr, logger, notifier,
                                                      symbol, best, portfolio_val,
                                                      drawdown_pct=drawdown_from_peak,
                                                      daily_pnl_pct=daily_pnl)
                    else:
                        traded = execute_trade(exchange, risk_mgr, logger, notifier,
                                               symbol, side, best, balance, portfolio_val,
                                               drawdown_pct=drawdown_from_peak)
                    if traded:
                        recently_traded[symbol] = time.time()
                        trades_today_by_symbol[symbol] = trades_today_by_symbol.get(symbol, 0) + 1
                        if trades_today_by_symbol[symbol] >= Config.MAX_TRADES_PER_SYMBOL_PER_DAY:
                            print(f"    [Churn] {symbol} hat Tages-Cap erreicht ({trades_today_by_symbol[symbol]}/{Config.MAX_TRADES_PER_SYMBOL_PER_DAY}) — blockiert bis Mitternacht")
                    if weakest:
                        recently_traded[weakest] = time.time()

            # 5. Print status
            summary = logger.get_summary()
            if summary["total_trades"] > 0:
                print(f"\n  Total trades: {summary['total_trades']} | "
                      f"Realisiert: {summary['realized_pnl']:+.2f}EUR | "
                      f"Fees: {summary['total_fees_eur']:.4f}EUR | "
                      f"Tages-P&L: {daily_pnl:+.2f}%")

            if risk_mgr.open_positions:
                print(f"\n  Open positions ({len(risk_mgr.open_positions)}):")
                for s, p in risk_mgr.open_positions.items():
                    ticker = exchange.get_ticker(s)
                    current = ticker.get("last", 0) if ticker else 0
                    direction = p.get("direction", "long")
                    if direction == "short":
                        unrealized = (p["entry_price"] - current) * p["volume"] if current else 0
                    else:
                        unrealized = (current - p["entry_price"]) * p["volume"] if current else 0
                    print(f"    {s} [{direction.upper()}]: entry={fmt_price(p['entry_price'])} "
                          f"now={fmt_price(current)} "
                          f"P&L={unrealized:+.2f}EUR "
                          f"[{p['strategy']}] "
                          f"SL={fmt_price(p['stop_loss'])} TP={fmt_price(p['take_profit'])}")

            print(f"\n  Next scan in {Config.CHECK_INTERVAL}s...")
            time.sleep(Config.CHECK_INTERVAL)

        except KeyboardInterrupt:
            print("\n\n  Bot stopped!")
            summary = logger.get_summary()
            portfolio = risk_mgr.get_portfolio_value(exchange)
            print(f"  Final portfolio: {portfolio:.2f}EUR")
            print(f"  Trades: {summary['total_trades']}")
            print(f"  Realized P&L: {summary['realized_pnl']:+.2f}EUR")
            if summary.get("strategies"):
                print(f"  Per strategy:")
                for s, d in summary["strategies"].items():
                    print(f"    {s}: {d['count']} trades")

            # Portfolio-Wert in .env speichern für nahtlosen Neustart
            import re
            env_path = os.path.join(os.path.dirname(__file__), ".env")
            with open(env_path, "r") as f:
                env = f.read()
            env = re.sub(r"INITIAL_CAPITAL=.*", f"INITIAL_CAPITAL={portfolio:.2f}", env)
            with open(env_path, "w") as f:
                f.write(env)
            print(f"  Portfolio gespeichert: {portfolio:.2f}EUR → .env")
            sys.exit(0)

        except Exception as e:
            print(f"\n  ERROR: {e}")
            import traceback
            traceback.print_exc()
            print(f"  Retrying in 30s...")
            time.sleep(30)


if __name__ == "__main__":
    run_bot()
