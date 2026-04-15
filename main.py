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


def execute_trade(exchange, risk_manager, logger, notifier, symbol, side, analysis, balance, portfolio_val=None):
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
            elif strategy == "dca":
                print(f"    Skip: DCA-Cap erreicht (max {risk_manager.MAX_DCA_POSITIONS} DCA-Positionen)")
            elif strategy == "gainer":
                print(f"    Skip: Gainer-Slot belegt")
            else:
                print(f"    Skip: Position nicht möglich ({symbol})")
            return False

        dca_mult = analysis.get("dca_multiplier", 1.0)
        leverage = analysis.get("leverage", 1)
        volume = risk_manager.calculate_position_size(balance, price, strategy, dca_mult, leverage)
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


def execute_gainer_trade(exchange, risk_manager, logger, notifier, symbol, analysis, portfolio_val=None):
    """Execute a gainer trade on Binance (paper mode). Fixed EUR position size."""
    price = analysis.get("price", 0)
    if price <= 0:
        print(f"    [Gainer] No price for {symbol}")
        return False

    if not risk_manager.can_open_position(symbol, "gainer", "long"):
        print(f"    [Gainer] Slot already occupied")
        return False

    # Dynamic: always 10% of current portfolio value
    port = portfolio_val if portfolio_val else risk_manager.get_portfolio_value(exchange)
    amount_eur = port * Config.GAINER_SLOT_PCT
    volume = round(amount_eur / price, 8)
    print(f"    [Gainer] Size: {amount_eur:.2f}EUR ({Config.GAINER_SLOT_PCT*100:.0f}% of {port:.2f}EUR portfolio)")
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
    """Check all open positions for stop-loss / take-profit."""
    exits = []
    for symbol in list(risk_manager.open_positions.keys()):
        pos = risk_manager.open_positions[symbol]
        ticker = exchange.get_ticker(symbol)

        if not ticker or not ticker.get("last"):
            continue

        current_price = ticker["last"]
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
                lines.append(f"  {icon} *{sym}* [BINANCE]: `{unreal:+.2f}EUR`")
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

            # Marktkontext-Exit: verlierend Shorts schließen wenn BTC dreht bullisch
            # (verhindert dass z.B. BCH-Short weiter blutet wenn BTC plötzlich steigt)
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
                    if pnl_pct < -0.02:  # Short ist >2% im Minus
                        print(f"  [MarktExit] BTC bullisch + {sym} SHORT -{abs(pnl_pct)*100:.1f}% → schließen")
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

            # Marktkontext-Exit: verlierend Longs schließen wenn BTC dreht bearisch
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
                    if pnl_pct < -0.02:  # Long ist >2% im Minus
                        print(f"  [MarktExit] BTC bearisch + {sym} LONG -{abs(pnl_pct)*100:.1f}% → schließen")
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

            # Gainer-Slot Status
            gainer_slot_occupied = any(
                p.get("strategy") == "gainer" for p in risk_mgr.open_positions.values()
            )
            if gainer_slot_occupied:
                gainer_pos = next(
                    (f"{sym} [{p['entry_price']:.4f}]"
                     for sym, p in risk_mgr.open_positions.items()
                     if p.get("strategy") == "gainer"), ""
                )
                print(f"  [Gainer Slot] Aktiv: {gainer_pos}")
            else:
                print(f"  [Gainer Slot] Frei — suche Coin mit >{Config.GAINER_MIN_GAIN_24H:.0f}% 24h Gewinn")

            # Schutz-Phase: keine neuen Trades, bestehende Positionen laufen weiter
            if phase == "protect":
                print(f"  [PROTECT] Nahe am Ziel — keine neuen Trades, Trailing-SL aktiv ({daily_pnl:+.2f}%)")
                print(f"\n  Next scan in {Config.CHECK_INTERVAL}s...")
                time.sleep(Config.CHECK_INTERVAL)
                continue

            # 2. Scan for best coins
            scan_results = scanner.scan()
            scanner.print_results(scan_results)
            target_symbols = [r["symbol"] for r in scan_results]

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
                if "momentum" in active:
                    sig = momentum.analyze(df)
                    strong = sig.get("leverage", 1) >= 2
                    if sig["signal"] == Signal.BUY:
                        if loss_brake:
                            pass  # Verlustbremse: keine Longs
                        elif not market_bearish or strong:
                            signals.append(sig)
                            print(f"    [momentum] BUY: {sig['reason']}")
                    elif sig["signal"] == Signal.SELL:
                        if not market_bullish or strong:
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
                    gain_24h = coin_data.get("change_pct", 0) if coin_data else 0
                    if gain_24h >= Config.GAINER_MIN_GAIN_24H:
                        sig = gainer_strategy.analyze(df, gain_24h)
                        if sig["signal"] == Signal.BUY:
                            signals.append(sig)
                            print(f"    [gainer] BUY: {sig['reason']}")

                # Execute best signal (highest priority: gainer > sentiment > momentum > grid > dca)
                if signals:
                    priority = {"gainer": 5, "sentiment": 4, "momentum": 3, "grid": 2, "dca": 1}
                    best = max(signals, key=lambda s: priority.get(s.get("strategy", ""), 0))
                    direction = best.get("direction", "long")
                    side = "sell" if direction == "short" else "buy"

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
                                                      symbol, best, portfolio_val)
                    else:
                        traded = execute_trade(exchange, risk_mgr, logger, notifier,
                                               symbol, side, best, balance, portfolio_val)
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
                    print(f"    {s} [{direction.upper()}]: entry={p['entry_price']:.2f} "
                          f"now={current:.2f} "
                          f"P&L={unrealized:+.2f}EUR "
                          f"[{p['strategy']}] "
                          f"SL={p['stop_loss']:.2f} TP={p['take_profit']:.2f}")

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
