import time
import requests
import feedparser
from config import Config
from strategies import Signal


class NewsSentimentAnalyzer:
    """Analyzes crypto news using Claude API and RSS feeds to generate trading signals."""

    RSS_FEEDS = [
        "https://cointelegraph.com/rss",
        "https://coindesk.com/arc/outboundfeeds/rss/",
        "https://decrypt.co/feed",
        "https://cryptonews.com/news/feed/",
    ]

    # Map coin names/tickers to trading symbols
    COIN_ALIASES = {
        "bitcoin": "BTC/EUR", "btc": "BTC/EUR",
        "ethereum": "ETH/EUR", "eth": "ETH/EUR", "ether": "ETH/EUR",
        "solana": "SOL/EUR", "sol": "SOL/EUR",
        "cardano": "ADA/EUR", "ada": "ADA/EUR",
        "polkadot": "DOT/EUR", "dot": "DOT/EUR",
        "avalanche": "AVAX/EUR", "avax": "AVAX/EUR",
        "chainlink": "LINK/EUR", "link": "LINK/EUR",
        "polygon": "MATIC/EUR", "matic": "MATIC/EUR", "pol": "MATIC/EUR",
        "dogecoin": "DOGE/EUR", "doge": "DOGE/EUR",
        "ripple": "XRP/EUR", "xrp": "XRP/EUR",
        "litecoin": "LTC/EUR", "ltc": "LTC/EUR",
        "uniswap": "UNI/EUR", "uni": "UNI/EUR",
        "aave": "AAVE/EUR",
        "near": "NEAR/EUR", "near protocol": "NEAR/EUR",
        "cosmos": "ATOM/EUR", "atom": "ATOM/EUR",
        "algorand": "ALGO/EUR", "algo": "ALGO/EUR",
        "stellar": "XLM/EUR", "xlm": "XLM/EUR",
        "tron": "TRX/EUR", "trx": "TRX/EUR",
        "pepe": "PEPE/EUR",
        "shiba": "SHIB/EUR", "shib": "SHIB/EUR",
    }

    def __init__(self):
        self.last_check = 0
        self.last_headlines = []
        self.signals = {}  # symbol -> signal
        self.has_api_key = bool(Config.ANTHROPIC_API_KEY)

        if not self.has_api_key:
            print("  News sentiment: disabled (no ANTHROPIC_API_KEY)")
        else:
            print("  News sentiment: enabled")

    def check_news(self) -> dict[str, dict]:
        """Fetch news and analyze sentiment. Returns signals per symbol."""
        if not self.has_api_key:
            return {}

        now = time.time()
        if now - self.last_check < Config.NEWS_CHECK_INTERVAL:
            return self.signals

        self.last_check = now
        headlines = self._fetch_headlines()

        if not headlines or headlines == self.last_headlines:
            return self.signals

        self.last_headlines = headlines
        print(f"\n  Analyzing {len(headlines)} news headlines with Claude...")

        self.signals = self._analyze_with_claude(headlines)
        return self.signals

    def _fetch_headlines(self) -> list[str]:
        """Fetch latest headlines from RSS feeds."""
        headlines = []
        for feed_url in self.RSS_FEEDS:
            try:
                feed = feedparser.parse(feed_url)
                for entry in feed.entries[:5]:
                    title = entry.get("title", "")
                    summary = entry.get("summary", "")[:200]
                    if title:
                        headlines.append(f"{title}. {summary}")
            except Exception:
                continue
        return headlines[:20]

    def _analyze_with_claude(self, headlines: list[str]) -> dict[str, dict]:
        """Use Claude API to analyze news sentiment for specific coins."""
        try:
            news_text = "\n".join(f"- {h}" for h in headlines)

            prompt = f"""Analyze these crypto news headlines for trading signals.
For each cryptocurrency mentioned, give a sentiment score from -10 (very bearish) to +10 (very bullish).
Only include coins where the news strongly suggests a price movement.

Headlines:
{news_text}

Respond ONLY in this exact format, one per line:
COIN_TICKER|SCORE|ONE_LINE_REASON

Example:
SOL|8|Major partnership announced with Visa
BTC|-3|Regulatory concerns in EU

Only include scores >= 5 or <= -5 (strong signals only). If no strong signals, respond with: NONE"""

            response = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": Config.ANTHROPIC_API_KEY,
                    "content-type": "application/json",
                    "anthropic-version": "2023-06-01",
                },
                json={
                    "model": "claude-sonnet-4-20250514",
                    "max_tokens": 500,
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=30,
            )

            if response.status_code != 200:
                print(f"  Claude API error: {response.status_code}")
                return {}

            data = response.json()
            text = data["content"][0]["text"].strip()

            if text == "NONE":
                print("  No strong news signals")
                return {}

            return self._parse_signals(text)

        except Exception as e:
            print(f"  Claude analysis error: {e}")
            return {}

    def _parse_signals(self, text: str) -> dict[str, dict]:
        """Parse Claude's response into trading signals."""
        signals = {}

        for line in text.strip().split("\n"):
            parts = line.strip().split("|")
            if len(parts) != 3:
                continue

            ticker = parts[0].strip().lower()
            try:
                score = int(parts[1].strip())
            except ValueError:
                continue
            reason = parts[2].strip()

            # Map to trading symbol
            symbol = self.COIN_ALIASES.get(ticker)
            if not symbol:
                symbol = f"{ticker.upper()}/EUR"

            if score >= 5:
                signals[symbol] = {
                    "signal": Signal.BUY,
                    "reason": f"News: {reason} (score: +{score})",
                    "score": score,
                    "strategy": "sentiment",
                }
                print(f"  NEWS BUY signal: {symbol} | {reason} | score: +{score}")
            elif score <= -5:
                signals[symbol] = {
                    "signal": Signal.SELL,
                    "reason": f"News: {reason} (score: {score})",
                    "score": score,
                    "strategy": "sentiment",
                }
                print(f"  NEWS SELL signal: {symbol} | {reason} | score: {score}")

        return signals
