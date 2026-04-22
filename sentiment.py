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
        # Provider-Chain: Gemini bevorzugt (Free-Tier = kostenlos), Claude
        # nur als Fallback wenn Gemini gerade einen Fehler liefert
        # (Rate-Limit, 5xx, etc). So kostet Normalbetrieb 0€.
        self.has_claude = bool(Config.ANTHROPIC_API_KEY)
        self.has_gemini = bool(Config.GEMINI_API_KEY)
        self.active_provider = self._pick_provider()

        if self.active_provider == "none":
            print("  News sentiment: disabled (kein GEMINI_API_KEY und kein ANTHROPIC_API_KEY)")
        else:
            fallback_note = ""
            if self.active_provider == "gemini" and self.has_claude:
                fallback_note = " (Claude als Notfall-Fallback bereit)"
            print(f"  News sentiment: enabled via {self.active_provider.upper()}{fallback_note}")

    def _pick_provider(self) -> str:
        """Waehlt aktiven Provider. Gemini > Claude > none (kostenlos first)."""
        if self.has_gemini:
            return "gemini"
        if self.has_claude:
            return "claude"
        return "none"

    def check_news(self) -> dict[str, dict]:
        """Fetch news and analyze sentiment. Returns signals per symbol."""
        provider = self._pick_provider()
        if provider == "none":
            return {}

        now = time.time()
        if now - self.last_check < Config.NEWS_CHECK_INTERVAL:
            return self.signals

        self.last_check = now
        headlines = self._fetch_headlines()

        if not headlines or headlines == self.last_headlines:
            return self.signals

        self.last_headlines = headlines
        print(f"\n  Analyzing {len(headlines)} news headlines with {provider.upper()}...")

        # Gemini bevorzugt (kostenlos). Nur bei Fehler Claude als Einmal-Fallback
        # fuer diesen Call — nicht persistent, damit naechster Cycle wieder Gemini
        # probiert (Free-Tier-Limits setzen sich jede Minute zurueck).
        if provider == "gemini":
            result = self._analyze_with_gemini(headlines)
            if result is None and self.has_claude:
                print("  Gemini nicht verfuegbar -> Einmal-Fallback auf Claude")
                result = self._analyze_with_claude(headlines)
        else:
            result = self._analyze_with_claude(headlines)

        self.signals = result or {}
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

    def _build_prompt(self, headlines: list[str]) -> str:
        news_text = "\n".join(f"- {h}" for h in headlines)
        return f"""Analyze these crypto news headlines for trading signals.
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

    # Credit-/Auth-Fehler die zum Provider-Wechsel fuehren sollen
    _CREDIT_ERROR_CODES = {401, 402, 403, 429, 529}

    def _analyze_with_claude(self, headlines: list[str]) -> dict[str, dict] | None:
        """Use Claude API. Returns None bei Credit/Auth-Fehler (triggert Gemini-Fallback)."""
        try:
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
                    "messages": [{"role": "user", "content": self._build_prompt(headlines)}],
                },
                timeout=30,
            )

            if response.status_code in self._CREDIT_ERROR_CODES:
                print(f"  Claude API {response.status_code} (Credit/Auth) — Fallback erwuenscht")
                return None  # Signal zur Provider-Chain: nimm Gemini
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

    def _analyze_with_gemini(self, headlines: list[str]) -> dict[str, dict] | None:
        """Use Gemini 1.5 Flash (kostenloser Tier, 15 req/min).
        Returns None bei API-/Rate-Limit-Fehler (triggert Claude-Fallback einmalig)."""
        try:
            response = requests.post(
                "https://generativelanguage.googleapis.com/v1beta/models/"
                "gemini-1.5-flash:generateContent",
                params={"key": Config.GEMINI_API_KEY},
                headers={"content-type": "application/json"},
                json={
                    "contents": [{"parts": [{"text": self._build_prompt(headlines)}]}],
                    "generationConfig": {"maxOutputTokens": 500, "temperature": 0.2},
                },
                timeout=30,
            )
            if response.status_code != 200:
                print(f"  Gemini API {response.status_code} — {response.text[:200]}")
                return None  # Signal zur Provider-Chain: Claude-Fallback probieren

            data = response.json()
            candidates = data.get("candidates", [])
            if not candidates:
                print("  Gemini: keine Antwort (blocked/empty)")
                return {}
            parts = candidates[0].get("content", {}).get("parts", [])
            text = "".join(p.get("text", "") for p in parts).strip()

            if not text or text.upper() == "NONE":
                print("  No strong news signals (Gemini)")
                return {}

            return self._parse_signals(text)

        except Exception as e:
            print(f"  Gemini analysis error: {e}")
            return None

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
