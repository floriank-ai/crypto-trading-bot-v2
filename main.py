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
from exchange import Exchange
from scanner import CoinScanner
from strategies import MomentumStrategy, GridStrategy, DCAStrategy, Signal
from sentiment import NewsSentimentAnalyzer
from risk_manager import RiskManager
from trade_logger import TradeLogger
from notifier import Notifier



def _restore_positions(risk_mgr, exchange):
    """Reconstruct open positions from trade log after restart."""
    import json, os
    json_path = os.path.join("logs", "trades.json")
    if not os.path.exists(json_path):
        return
    try:
        with open(json_path) as f:
            entries = json.load(f)
        # Only last session
        si = max((i for i,e in enumerate(entries) if e.get("session_start")), default=-1)
        trades = [e for e in entries[si+1:] if not e.get("session_start")]
        if not trades:
            return
        positions = {}
        for t in trades:
            sym, side, vol, price, cost = t["pair"], t["side"], t["volume"], t["price_eur"], t["total_eur"]
            if side in ("buy", "short"):
                if sym not in positions:
                    positions[sym] = {"volume": 0, "cost": 0, "entry": price,
                                      "direction": "short" if side == "short" else "long",
                                      "strategy": t.get("strategy", "momentum")}
                positions[sym]["volume"] += vol
                positions[sym]["cost"] += cost
            elif side == "sell" and sym in positions:
                positions[sym]["volume"] -= vol
                if positions[sym]["volume"] <= 0.0001:
                    del positions[sym]
        for sym, pos in positions.items():
            if pos["volume"] > 0.0001:
                risk_mgr.open_position(sym, pos["entry"], pos["volume"],
                                       pos["strategy"], pos["direction"])
                if pos["direction"] == "long":
                    exchange.paper_positions[sym] = pos["volume"]
        if positions:
            print(f"  [Restore] {len(positions)} Positionen wiederhergestellt")
    except Exception as e:
        print(f"  [Restore] Fehler: {e}")


def execute_trade(exchange, risk_manager, logger, notifier, symbol, side, analysis, balance):
    """Execute a trade and log it."""
    strategy = analysis.get("strategy", "unknown")
    price = analysis.get("price", 0)

    direction = analysis.get("direction", "long")

    if side == "buy" or (side == "sell" and direction == "short"):
        strategy = analysis.get("strategy", "momentum")
        if not risk_manager.can_open_position(symbol, strategy, direction):
            print(f"    Skip: max positions reached or already holding {symbol}")
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
            portfolio_val = risk_manager.get_portfolio_value(exchange)
            daily_pnl = risk_manager.get_daily_pnl_pct(exchange)
            notifier.notify_trade(log_side, symbol, volume, exec_price,
                                  analysis["reason"], strategy, exchange.get_balance(),
                                  portfolio_val, daily_pnl)
            return True
        else:
            print(f"    Order failed: {result.get('error', '?')}")
    return False


def check_exits(exchange, risk_manager, logger, notifier):
    """Check all open positions for stop-loss / take-profit."""
    exits = []
    for symbol in list(risk_manager.open_positions.keys()):
        ticker = exchange.get_ticker(symbol)
        if not ticker or not ticker.get("last"):
            continue

        current_price = ticker["last"]
        exit_type = risk_manager.check_exit(symbol, current_price)

        if exit_type:
            pos = risk_manager.open_positions[symbol]
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
                                 balance_after=exchange.get_balance())
                port_val = risk_manager.get_portfolio_value(exchange)
                d_pnl = risk_manager.get_daily_pnl_pct(exchange)
                notifier.notify_exit(symbol, exit_type, pnl, pos["strategy"], port_val, d_pnl)
                risk_manager.close_position(symbol)
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
    sentiment = NewsSentimentAnalyzer()
    risk_mgr = RiskManager()
    logger = TradeLogger()
    notifier = Notifier()

    # Restore open positions from trade log (survives Railway restarts)
    _restore_positions(risk_mgr, exchange)

    logger.log_session_start(Config.INITIAL_CAPITAL)
    recently_traded = {}  # symbol -> timestamp, Cooldown nach Rotation

    active = Config.ACTIVE_STRATEGIES
    print(f"\n  Active strategies: {', '.join(active)}")
    print(f"  Starting in 3 seconds...\n")
    time.sleep(3)

    cycle = 0
    peak_portfolio = Config.INITIAL_CAPITAL
    hwm_pause_until = 0
    today = datetime.now().date()

    while True:
        try:
            cycle += 1
            now = datetime.now().strftime("%H:%M:%S")
            print(f"\n{'='*60}")
            print(f"  Cycle {cycle} | {now}")
            print(f"{'='*60}")

            balance = exchange.get_balance()
            portfolio_val = risk_mgr.get_portfolio_value(exchange)

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

            # Fix 3: Verlustbremse — bei -3% Tages-P&L keine neuen Longs
            loss_brake = daily_pnl < -3.0
            if loss_brake:
                print(f"  ⚠️  VERLUSTBREMSE aktiv ({daily_pnl:.2f}%) — nur Shorts erlaubt")

            # 1. Check exits first
            exits = check_exits(exchange, risk_mgr, logger, notifier)
            for sym, etype, pnl in exits:
                print(f"    Exited {sym}: {etype} P&L {pnl:+.2f}EUR")

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

                # Execute best signal (highest priority: sentiment > momentum > grid > dca)
                if signals:
                    priority = {"sentiment": 4, "momentum": 3, "grid": 2, "dca": 1}
                    best = max(signals, key=lambda s: priority.get(s.get("strategy", ""), 0))
                    direction = best.get("direction", "long")
                    side = "sell" if direction == "short" else "buy"

                    # Rotation: if strong signal (leverage>=2) but slots full or barely any cash, close weakest
                    weakest = None
                    is_strong = best.get("leverage", 1) >= Config.ROTATION_MIN_LEVERAGE
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
                    traded = execute_trade(exchange, risk_mgr, logger, notifier,
                                          symbol, side, best, balance)
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
