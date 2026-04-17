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
from datetime import datetime

from config import Config
from auto_optimizer import should_run_today, run as run_optimizer, load_state
from exchange import Exchange
from scanner import CoinScanner
from strategies import MomentumStrategy, GridStrategy, DCAStrategy, GainerStrategy, Signal
from sentiment import NewsSentimentAnalyzer
from risk_manager import RiskManager
from trade_logger import TradeLogger
from notifier import Notifier



def fmt_price(p: float) -> str:
    """Format price with enough decimal places for cheap coins."""
    if p >= 100:  return f"{p:.2f}"
    if p >= 1:    return f"{p:.3f}"
    if p >= 0.01: return f"{p:.4f}"
    return f"{p:.6f}"


def _restore_positions(risk_mgr, exchange):
    """Restore open positions from positions.json (written after every trade)."""
    import json, os
    pos_path = os.path.join("logs", "positions.json")
    if not os.path.exists(pos_path):
        print("  [Restore] Keine positions.json — Neustart ohne offene Positionen")
        return
    try:
        with open(pos_path) as f:
            positions = json.load(f)
        if not positions:
            print("  [Restore] positions.json leer — keine offenen Positionen")
            return
        # Nur Symbole wiederherstellen die auf Kraken (EUR-Pairs) verfügbar sind
        valid_symbols = set(exchange.get_all_eur_pairs())
        skipped = []
        for sym, pos in positions.items():
            if sym not in valid_symbols:
                skipped.append(sym)
                continue
            # Backwards-compat: Felder für Partial-TP nachziehen falls alte positions.json
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
            print(f"  [Restore] Übersprungen (nicht auf Kraken): {', '.join(skipped)}")
            risk_mgr._save_positions()  # positions.json ohne ungültige Symbole überschreiben
        print(f"  [Restore] {len(risk_mgr.open_positions)} Positionen wiederhergestellt")
    except Exception as e:
        print(f"  [Restore] Fehler: {e}")
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
                print(f"    Skip: LONG-Cap erreicht (max {risk_manager.MAX_LONG_POSITIONS} Longs — Korrelations-Schutz)")
            elif direction == "short" and sum(1 for p in risk_manager.open_positions.values()
                                              if p.get("direction") == "short") >= risk_manager.MAX_SHORT_POSITIONS:
                print(f"    Skip: SHORT-Cap erreicht (max {risk_manager.MAX_SHORT_POSITIONS} Shorts — Korrelations-Schutz)")
            elif strategy == "dca":
                print(f"    Skip: DCA-Cap erreicht (max {risk_manager.MAX_DCA_POSITIONS} DCA-Positionen)")
            elif strategy == "gainer":
                print(f"    Skip: Gainer-Slot belegt")
            else:
                print(f"    Skip: Position nicht möglich ({symbol})")
            return False

        dca_mult = analysis.get("dca_multiplier", 1.0)
        leverage = analysis.get("leverage", 1)
        volume = risk_manager.calculate_position_size(balance, price, strategy, dca_mult, leverage, drawdown_pct)
        min_order = exchange.get_min_order(symbol)

        if volume < min_order:
            print(f"    Skip: volume {volume} < min {min_order}")
            return False

        result = exchange.place_order(symbol, side, volume, direction=direction)
        if result["status"] == "ok":
            exec_price = result.get("price", price)
            cost = result.get("cost", volume * exec_price)
            fee = result.get("fee", cost * 0.0026)

            risk_manager.open_position(symbol, exec_price, volume, strategy, direction)
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


def execute_gainer_trade(exchange, risk_manager, logger, notifier, symbol, analysis, portfolio_val=None, drawdown_pct=0.0):
    """Execute a gainer trade on Binance (paper mode). Fixed EUR position size."""
    price = analysis.get("price", 0)
    if price <= 0:
        print(f"    [Gainer] No price for {symbol}")
        return False

    if not risk_manager.can_open_position(symbol, "gainer", "long"):
        print(f"    [Gainer] Slot already occupied")
        return False

    # Anti-Martingale: auch Gainer-Slot schrumpft in Drawdown
    if drawdown_pct >= 3.0:
        dd_scale = 0.4
    elif drawdown_pct >= 2.0:
        dd_scale = 0.5
    elif drawdown_pct >= 1.0:
        dd_scale = 0.7
    else:
        dd_scale = 1.0

    # Dynamic: always 10% of current portfolio value (* dd_scale bei Drawdown)
    port = portfolio_val if portfolio_val else risk_manager.get_portfolio_value(exchange)
    amount_eur = port * Config.GAINER_SLOT_PCT * dd_scale
    volume = round(amount_eur / price, 8)
    dd_note = f" [DD-Scale {dd_scale:.1f}x, drawdown {drawdown_pct:.1f}%]" if dd_scale < 1.0 else ""
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


def check_exits(exchange, risk_manager, logger, notifier):
    """Check all open positions for partial-TP, stop-loss, take-profit."""
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
    momentum = MomentumStrategy()
    grid = GridStrategy()
    dca = DCAStrategy()
    gainer_strategy = GainerStrategy()
    sentiment = NewsSentimentAnalyzer()
    risk_mgr = RiskManager()
    logger = TradeLogger()
    notifier = Notifier()

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

    recently_traded = {}  # symbol -> timestamp, Cooldown nach Rotation

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
    # Kill-Switch: {strategy_name: unix_ts_until} — blutende Strategien pausieren
    paused_strategies = {}
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
            if should_run_today():
                new_priority = run_optimizer(notifier)
                Config.MOMENTUM_PRIORITY = new_priority
                print(f"  [Optimizer] Neue Prioritätsliste: {new_priority[:5]}...")

            # Mitternacht-Reset
            if datetime.now().date() != today:
                today = datetime.now().date()
                risk_mgr.daily_start_value = portfolio_val
                peak_portfolio = portfolio_val
                print(f"  🌅 Neuer Tag — Tages-Zähler zurückgesetzt auf {portfolio_val:.2f}EUR")
                notifier.send(f"🌅 Neuer Tag\nNeuer Startpunkt: {portfolio_val:.2f}EUR")

            daily_pnl = risk_mgr.get_daily_pnl_pct(exchange)
            phase = risk_mgr.get_trading_phase(exchange)
            print(f"  Cash: {balance:.2f}EUR | Portfolio: {portfolio_val:.2f}EUR | Tages-P&L: {daily_pnl:+.2f}% [{phase.upper()}]")

            # Tageslimit: bei -5% keine neuen Trades bis Mitternacht
            if daily_pnl < -5.0:
                print(f"  🛑 TAGESLIMIT -5% ({daily_pnl:.2f}%) — pausiere bis Mitternacht")
                notifier.send(f"🛑 *TAGESLIMIT erreicht*\n{daily_pnl:.2f}% Tagesverlust\nKeine neuen Trades bis Mitternacht")
                time.sleep(Config.CHECK_INTERVAL)
                continue

            # High-Water-Mark: Peak tracken und Gewinn sichern
            if portfolio_val > peak_portfolio:
                peak_portfolio = portfolio_val
            drawdown_from_peak = (peak_portfolio - portfolio_val) / peak_portfolio * 100
            meaningful_gain = peak_portfolio > Config.INITIAL_CAPITAL * 1.03  # mind. 3% Gewinn gehabt
            if meaningful_gain and drawdown_from_peak >= 3.0 and time.time() > hwm_pause_until:
                print(f"\n  🔒 HIGH-WATER-MARK: -{drawdown_from_peak:.1f}% vom Peak ({peak_portfolio:.2f}→{portfolio_val:.2f}EUR) — schliesse alle Positionen!")
                notifier.send(f"🔒 *HIGH-WATER-MARK*\nPortfolio fiel {drawdown_from_peak:.1f}% vom Peak\n{peak_portfolio:.2f}EUR → {portfolio_val:.2f}EUR\nSchliesse alle Positionen & pause 30min")
                for sym in list(risk_mgr.open_positions.keys()):
                    pos = risk_mgr.open_positions[sym]
                    res = exchange.place_order(sym, "sell", pos["volume"])
                    if res["status"] == "ok":
                        p = res.get("price", pos["entry_price"])
                        c = res.get("cost", pos["volume"] * p)
                        f = res.get("fee", c * 0.0026)
                        pnl = (p - pos["entry_price"]) * pos["volume"]
                        logger.log_trade(pair=sym, side="sell", volume=pos["volume"],
                                         price=p, cost=c, fee=f,
                                         mode=Config.TRADING_MODE, strategy=pos["strategy"],
                                         signal_reason="hwm_protection",
                                         balance_after=exchange.get_balance())
                        print(f"    Closed {sym} P&L {pnl:+.2f}EUR")
                    risk_mgr.close_position(sym)
                hwm_pause_until = time.time() + 1800  # 30 Minuten Pause
                peak_portfolio = risk_mgr.get_portfolio_value(exchange)  # neuer Peak nach Close
                print(f"  Pause bis {datetime.fromtimestamp(hwm_pause_until).strftime('%H:%M:%S')}")
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
            exits = check_exits(exchange, risk_mgr, logger, notifier)
            for sym, etype, pnl in exits:
                print(f"    Exited {sym}: {etype} P&L {pnl:+.2f}EUR")

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

            # Gainer Discovery: Top-N Gainer aus ALLEN Kraken EUR-Paaren
            # (N = offene Gainer-Slots, idR 2)
            gainer_gain_lookup = {}  # symbol -> 24h change_pct (fuer per-symbol gainer check)
            if not gainer_slot_occupied:
                slots_free = risk_mgr.MAX_GAINER_POSITIONS - gainer_count
                all_eur_pairs = [p for p in exchange.get_all_eur_pairs()
                                 if p not in CoinScanner.SKIP_COINS and p not in risk_mgr.open_positions]
                all_tickers = exchange.get_tickers_bulk(all_eur_pairs)
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
            if "sentiment" in active:
                news_signals = sentiment.check_news()
                for sym, sig in news_signals.items():
                    if sym not in target_symbols:
                        target_symbols.append(sym)

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

                # Sentiment
                if "sentiment" in active and symbol in sentiment.signals:
                    sig = sentiment.signals[symbol]
                    if sig["signal"] == Signal.BUY:
                        signals.append(sig)
                        print(f"    [sentiment] BUY: {sig['reason']}")

                # Gainer: Kraken-Coin mit extremem 24h-Gewinn — höchste Priorität, 1 fixer Slot
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

                # Execute best signal (highest priority: gainer > sentiment > momentum > grid > dca)
                if signals:
                    priority = {"gainer": 5, "sentiment": 4, "momentum": 3, "grid": 2, "dca": 1}
                    best = max(signals, key=lambda s: priority.get(s.get("strategy", ""), 0))
                    direction = best.get("direction", "long")
                    side = "sell" if direction == "short" else "buy"

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

                    regime_exempt = strat in ("gainer", "dca", "grid")
                    if not regime_exempt:
                        if direction == "long" and not allow_long_entries:
                            print(f"    Skip {symbol}: LONG blockiert (Regime {regime_state})")
                            continue
                        if direction == "short" and not allow_short_entries:
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
                                                      drawdown_pct=drawdown_from_peak)
                    else:
                        traded = execute_trade(exchange, risk_mgr, logger, notifier,
                                               symbol, side, best, balance, portfolio_val,
                                               drawdown_pct=drawdown_from_peak)
                    if traded:
                        recently_traded[symbol] = time.time()
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
