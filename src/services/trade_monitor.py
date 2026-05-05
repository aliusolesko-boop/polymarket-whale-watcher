"""
Trade monitoring service - per-market parallel architecture.

Each market runs its own independent async task that:
1. Polls the official Polymarket data-api for new trades
2. Detects whale trades
3. Fetches trader ranking + history in parallel
4. Fires the whale callback (LLM report generation) without blocking other markets

Modeled after paper_trading/paper_trading.py's _market_loop pattern.
"""
import asyncio
import json
import logging
import random
import time as _time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set, Callable, Awaitable

import httpx

from src.config import get_settings
from src.models.market import Market, TrendingMarket
from src.models.trade import (
    TradeActivity, WhaleTrade, TraderRanking, TraderHistory,
    EventPosition, MarketTopTrader,
)
from src.services.anomaly_detector import AnomalyDetector

logger = logging.getLogger(__name__)

# Gamma API for fetching latest market prices
GAMMA_API_URL = "https://gamma-api.polymarket.com/markets"

# Official Polymarket data-api for trade data
# URL and key loaded from settings (.env)

# File to persist processed transaction hashes
PROCESSED_TXNS_FILE = Path(__file__).parent.parent.parent / "data" / "processed_transactions.json"


class TradeMonitor:
    """
    Monitors Polymarket markets for large trades.

    Architecture: one asyncio.Task per market, fully parallel.
    """

    def __init__(
        self,
        on_whale_detected: Optional[Callable[[WhaleTrade], Awaitable[None]]] = None,
    ):
        self.settings = get_settings()

        # Official Polymarket data-api
        self.data_api_url = "https://data-api.polymarket.com"
        self.trades_endpoint = f"{self.data_api_url}/trades"
        self.leaderboard_endpoint = f"{self.data_api_url}/v1/leaderboard"
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, pool=120.0),
            limits=httpx.Limits(
                max_connections=50,
                max_keepalive_connections=20,
                keepalive_expiry=30,
            ),
        )

        # Per-market last-fetch timestamps for incremental polling
        self._market_last_ts: Dict[str, int] = {}

        # Rate limiter: Lock + Semaphore created lazily in run() to avoid "attached to different loop" error
        self._api_lock: Optional[asyncio.Lock] = None
        self._api_sem: Optional[asyncio.Semaphore] = None  # concurrency limiter
        self._api_last_request: float = 0.0
        self._api_global_interval: float = 0.2  # min 0.2s between requests = 5 QPS

        # Cache for trader rankings to avoid repeated API calls
        self._trader_ranking_cache: Dict[str, TraderRanking] = {}

        # Markets being monitored: market_id -> Market
        self._monitored_markets: Dict[str, Market] = {}

        # Track processed transactions to avoid duplicates
        self._processed_txns: Set[str] = set()
        self._load_processed_txns()

        # Anomaly detector for multi-dimensional scoring
        self._anomaly_detector = AnomalyDetector()

        # Callback for whale detection
        self._on_whale_detected = on_whale_detected

        # Control flag and per-market tasks
        self._running = False
        self._market_tasks: Dict[str, asyncio.Task] = {}

        # Flag to track if initial scan is complete (ignore historical trades)
        self._initial_scan_complete = False

    # ================================================================
    # Persistence
    # ================================================================

    def _load_processed_txns(self):
        """Load processed transaction hashes from JSON file."""
        try:
            if PROCESSED_TXNS_FILE.exists():
                with open(PROCESSED_TXNS_FILE, "r") as f:
                    data = json.load(f)
                    self._processed_txns = set(data.get("transactions", []))
                    logger.info(f"Loaded {len(self._processed_txns)} processed transactions from file")
        except json.JSONDecodeError as e:
            logger.warning(f"Corrupted JSON file, backing up and starting fresh: {e}")
            if PROCESSED_TXNS_FILE.exists():
                backup_file = PROCESSED_TXNS_FILE.with_suffix('.json.bak')
                PROCESSED_TXNS_FILE.rename(backup_file)
                logger.info(f"Backed up corrupted file to {backup_file}")
            self._processed_txns = set()
        except Exception as e:
            logger.warning(f"Failed to load processed transactions: {e}")
            self._processed_txns = set()

    def _save_processed_txns(self):
        """Save processed transaction hashes to JSON file."""
        try:
            PROCESSED_TXNS_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(PROCESSED_TXNS_FILE, "w") as f:
                json.dump({
                    "transactions": list(self._processed_txns),
                    "count": len(self._processed_txns),
                    "last_updated": datetime.now().isoformat()
                }, f, indent=2)
            logger.debug(f"Saved {len(self._processed_txns)} processed transactions to file")
        except Exception as e:
            logger.warning(f"Failed to save processed transactions: {e}")

    async def close(self):
        """Cleanup resources."""
        self._save_processed_txns()
        await self._client.aclose()

    # ================================================================
    # Market list management
    # ================================================================

    def set_monitored_markets(self, markets: List[TrendingMarket]):
        """Update the list of markets to monitor."""
        self._monitored_markets = {}
        for tm in markets:
            if tm.market.id:
                self._monitored_markets[tm.market.id] = tm.market
        logger.info(f"Now monitoring {len(self._monitored_markets)} markets")

    def set_tiered_markets(self, tiers: dict[str, list]) -> None:
        """
        Set markets with per-tier poll intervals.

        Stores poll_interval per market_id in _market_poll_intervals dict.
        """
        self._monitored_markets = {}
        self._market_poll_intervals: dict[str, int] = {}

        tier_intervals = {
            "tier1": self.settings.tier1_poll_interval,
            "tier2": self.settings.tier2_poll_interval,
            "tier3": self.settings.tier3_poll_interval,
        }

        for tier_name, markets in tiers.items():
            interval = tier_intervals.get(tier_name, self.settings.fetch_interval_seconds)
            for tm in markets:
                if tm.market.id:
                    self._monitored_markets[tm.market.id] = tm.market
                    self._market_poll_intervals[tm.market.id] = interval

        tier_counts = {k: len(v) for k, v in tiers.items()}
        logger.info(
            f"Tiered monitoring: {tier_counts} "
            f"(intervals: {tier_intervals}s), total={len(self._monitored_markets)}"
        )

    # ================================================================
    # Trade fetching
    # ================================================================

    _MAX_RETRIES = 4
    _RETRY_BACKOFF = [2, 5, 10, 20]  # seconds between retries (with jitter)

    # ================================================================
    # Official Polymarket data-api: fetch trades
    # ================================================================

    async def fetch_market_trades(self, market_id: str) -> List[TradeActivity]:
        """
        Fetch recent trades using the official Polymarket data-api /trades endpoint.

        The official API returns trades with fields:
        - id, taker_order_id, market, asset, side, size, price, status
        - match_time, transaction_hash, outcome, bucket_index, owner, type
        """
        try:
            market = self._monitored_markets.get(market_id)
            if not market:
                return []

            # The official /trades endpoint uses condition_id as the "market" param
            condition_id = market.condition_id
            if not condition_id:
                return []

            last_ts = self._market_last_ts.get(market_id)

            params: Dict[str, object] = {
                "market": condition_id,
                "limit": 50,
            }

            sem = self._api_sem or asyncio.Semaphore(20)
            last_err: Optional[Exception] = None
            async with sem:
                for attempt in range(self._MAX_RETRIES):
                    try:
                        async with self._api_lock:
                            now = _time.monotonic()
                            wait = self._api_global_interval - (now - self._api_last_request)
                            if wait > 0:
                                await asyncio.sleep(wait)
                            self._api_last_request = _time.monotonic()

                        response = await self._client.get(
                            f"{self.data_api_url}/trades", params=params,
                        )
                        response.raise_for_status()
                        break
                    except httpx.HTTPStatusError as e:
                        if e.response.status_code in (502, 503, 504) and attempt < self._MAX_RETRIES - 1:
                            delay = self._RETRY_BACKOFF[attempt]
                            logger.debug(
                                f"Official API {e.response.status_code} for {market_id} "
                                f"(attempt {attempt + 1}/{self._MAX_RETRIES}), "
                                f"retrying in {delay}s"
                            )
                            await asyncio.sleep(delay)
                            continue
                        raise
                    except httpx.HTTPError as e:
                        last_err = e
                        if attempt < self._MAX_RETRIES - 1:
                            delay = self._RETRY_BACKOFF[attempt] + random.uniform(0, 2)
                            logger.debug(
                                f"Official API retry for {market_id} "
                                f"(attempt {attempt + 1}/{self._MAX_RETRIES}): "
                                f"{type(e).__name__}, retrying in {delay:.1f}s"
                            )
                            await asyncio.sleep(delay)
                        else:
                            logger.warning(
                                f"Official API connection error for {market_id} "
                                f"(attempt {attempt + 1}/{self._MAX_RETRIES}, giving up): "
                                f"{type(e).__name__}: {e}"
                            )
                            return []
                else:
                    return []

            data = response.json()
            if not data:
                return []

            activities = []
            max_ts = last_ts or 0

            for item in data:
                try:
                    side = item.get("side", "").upper()

                    # Only track BUY trades (new positions)
                    if side != "BUY":
                        continue

                    size = float(item.get("size", 0) or 0)
                    price = float(item.get("price", 0) or 0)
                    usdc_size = size * price  # Official API: USDC value = tokens * price

                    outcome = item.get("outcome", "Yes")
                    outcome_index = int(item.get("outcomeIndex", 0 if outcome == "Yes" else 1))

                    # Timestamp is epoch seconds in the official API
                    ts = int(item.get("timestamp", 0) or 0)
                    if ts == 0:
                        ts = int(_time.time())

                    if ts > max_ts:
                        max_ts = ts

                    tx_hash = item.get("transactionHash", "")

                    activity = TradeActivity(
                        transaction_hash=tx_hash,
                        timestamp=ts,
                        condition_id=item.get("conditionId", condition_id),
                        asset=item.get("asset", ""),
                        side="BUY",
                        size=size,
                        usdc_size=usdc_size,
                        price=price,
                        outcome=outcome,
                        outcome_index=outcome_index,
                        title=item.get("title", ""),
                        slug=item.get("slug"),
                        event_slug=item.get("eventSlug"),
                        proxy_wallet=item.get("proxyWallet"),
                        name=item.get("name") or item.get("pseudonym"),
                    )
                    activities.append(activity)
                except Exception as e:
                    logger.debug(f"Failed to parse official API trade: {e}")
                    continue

            if max_ts > 0:
                self._market_last_ts[market_id] = max_ts

            return activities

        except httpx.HTTPStatusError as e:
            logger.warning(
                f"Official trades API HTTP {e.response.status_code} for {market_id}: "
                f"{e.response.text[:200]}"
            )
            return []
        except Exception as e:
            logger.warning(f"Error fetching official trades for {market_id}: {type(e).__name__}: {e}")
            return []

    # ================================================================
    # Official API: trader info (ranking + history)
    # ================================================================

    async def fetch_trader_ranking(self, wallet_address: str) -> Optional[TraderRanking]:
        """Fetch trader ranking from the leaderboard API."""
        if not wallet_address:
            return None

        if wallet_address in self._trader_ranking_cache:
            return self._trader_ranking_cache[wallet_address]

        try:
            params = {
                "user": wallet_address,
                "timePeriod": "ALL",
                "orderBy": "PNL",
            }
            response = await self._client.get(self.leaderboard_endpoint, params=params)
            response.raise_for_status()
            data = response.json()

            if data and len(data) > 0:
                user_data = data[0]
                ranking = TraderRanking(
                    rank=user_data.get("rank"),
                    pnl=float(user_data.get("pnl", 0) or 0),
                    volume=float(user_data.get("vol", 0) or 0),
                    user_name=user_data.get("userName"),
                    profile_image=user_data.get("profileImage"),
                    verified=bool(user_data.get("verifiedBadge")),
                    time_period="ALL",
                )
                self._trader_ranking_cache[wallet_address] = ranking
                logger.debug(f"Fetched ranking for {wallet_address}: #{ranking.rank}")
                return ranking

            return None

        except httpx.HTTPError as e:
            logger.debug(f"HTTP error fetching ranking for {wallet_address}: {e}")
            return None
        except Exception as e:
            logger.debug(f"Error fetching ranking for {wallet_address}: {e}")
            return None

    async def fetch_trader_history(self, wallet_address: str) -> Optional[TraderHistory]:
        """Fetch trader's recent trading history."""
        if not wallet_address:
            return None

        try:
            params = {
                "user": wallet_address,
                "limit": 100,
            }
            response = await self._client.get(self.trades_endpoint, params=params)
            response.raise_for_status()
            data = response.json()

            if not data:
                return None

            total_trades = len(data)
            total_volume = 0.0
            large_trades_count = 0
            recent_markets: Set[str] = set()
            recent_trades = []

            for trade in data:
                usdc_size = float(trade.get("usdcSize", 0) or 0)
                if usdc_size == 0:
                    size = float(trade.get("size", 0) or 0)
                    price = float(trade.get("price", 0) or 0)
                    usdc_size = size * price

                total_volume += usdc_size

                if usdc_size >= 5000:
                    large_trades_count += 1
                    recent_trades.append({
                        "side": trade.get("side", ""),
                        "usdc_size": usdc_size,
                        "price": float(trade.get("price", 0) or 0),
                        "title": trade.get("title", trade.get("marketTitle", "")),
                        "timestamp": trade.get("timestamp", 0),
                    })

                title = trade.get("title", trade.get("marketTitle", ""))
                if title:
                    recent_markets.add(title[:50])

            avg_trade_size = total_volume / total_trades if total_trades > 0 else 0
            recent_trades.sort(key=lambda x: x["usdc_size"], reverse=True)

            return TraderHistory(
                total_trades=total_trades,
                total_volume=total_volume,
                avg_trade_size=avg_trade_size,
                large_trades_count=large_trades_count,
                recent_markets=list(recent_markets)[:10],
                recent_trades=recent_trades[:10],
            )

        except httpx.HTTPError as e:
            logger.debug(f"HTTP error fetching history for {wallet_address}: {e}")
            return None
        except Exception as e:
            logger.debug(f"Error fetching history for {wallet_address}: {e}")
            return None

    # ================================================================
    # Event positions & market top traders
    # ================================================================

    async def fetch_whale_event_positions(
        self,
        wallet_address: str,
        event_slug: str,
        current_condition_id: str,
    ) -> List[EventPosition]:
        """
        Fetch the whale's current positions across all markets in the same event.

        Uses the Polymarket data API positions endpoint directly:
        GET https://data-api.polymarket.com/positions?user=<wallet>
        Then filters by event_slug to find related holdings.
        """
        if not wallet_address or not event_slug:
            return []

        try:
            response = await self._client.get(
                f"{self.data_api_url}/positions",
                params={"user": wallet_address},
            )
            response.raise_for_status()
            all_positions = response.json()
            if not all_positions:
                return []

            # Filter positions belonging to the same event, excluding current market
            result = []
            for pos in all_positions:
                pos_event_slug = pos.get("eventSlug", "")
                pos_condition_id = pos.get("conditionId", "")

                if pos_event_slug != event_slug:
                    continue
                if pos_condition_id == current_condition_id:
                    continue

                size = float(pos.get("size", 0) or 0)
                if size == 0:
                    continue  # skip empty positions

                outcome = pos.get("outcome", "Yes")
                avg_price = float(pos.get("avgPrice", 0) or 0)
                cur_price = float(pos.get("curPrice", 0) or 0)
                current_value = float(pos.get("currentValue", 0) or 0)
                initial_value = float(pos.get("initialValue", 0) or 0)
                cash_pnl = float(pos.get("cashPnl", 0) or 0)
                title = pos.get("title", "")

                # Build human-readable summary
                if outcome == "Yes":
                    side_summary = f"Holding Yes {size:,.0f} tokens @ avg {avg_price:.2%}, current {cur_price:.2%}"
                else:
                    side_summary = f"Holding No {size:,.0f} tokens @ avg {avg_price:.2%}, current {cur_price:.2%}"

                result.append(EventPosition(
                    market_question=title,
                    condition_id=pos_condition_id,
                    outcome=outcome,
                    size=size,
                    avg_price=avg_price,
                    current_price=cur_price,
                    current_value=current_value,
                    initial_value=initial_value,
                    pnl=cash_pnl,
                    side_summary=side_summary,
                ))

            # Sort by position value descending
            result.sort(key=lambda x: x.current_value, reverse=True)
            logger.debug(
                f"Found {len(result)} event positions for {wallet_address} "
                f"in event '{event_slug}'"
            )
            return result

        except Exception as e:
            logger.warning(f"Error fetching whale event positions: {e}")
            return []

    async def fetch_market_top_traders(
        self, market_id: str, condition_id: str = "",
        outcome_prices: Optional[List[float]] = None, top_n: int = 5,
    ) -> tuple[List[MarketTopTrader], List[MarketTopTrader]]:
        """
        Fetch top holders (bulls and bears) for a market.

        Uses the official Polymarket data-api /holders endpoint which returns
        the top position holders for each outcome token, sorted by amount.

        Returns:
            (top_buyers, top_sellers) — each up to top_n entries.
            top_buyers = top Yes token holders (bullish).
            top_sellers = top No token holders (bearish).
        """
        if not condition_id:
            return [], []

        # outcome_prices: [yes_price, no_price]
        yes_price = outcome_prices[0] if outcome_prices and len(outcome_prices) > 0 else 0.5
        no_price = outcome_prices[1] if outcome_prices and len(outcome_prices) > 1 else 0.5

        try:
            response = await self._client.get(
                f"{self.data_api_url}/holders",
                params={"market": condition_id, "limit": top_n},
            )
            response.raise_for_status()
            data = response.json()
            if not data:
                return [], []

            top_buyers = []
            top_sellers = []

            for token_group in data:
                holders = token_group.get("holders", [])
                if not holders:
                    continue

                # outcomeIndex: 0 = Yes (bulls), 1 = No (bears)
                outcome_index = holders[0].get("outcomeIndex", 0)
                token_price = yes_price if outcome_index == 0 else no_price

                for h in holders[:top_n]:
                    wallet = h.get("proxyWallet", "")
                    name = h.get("name") or h.get("pseudonym") or None
                    amount = float(h.get("amount", 0) or 0)
                    # Convert token amount to USD value
                    usd_value = amount * token_price

                    trader = MarketTopTrader(
                        wallet=wallet,
                        name=name,
                        net_volume_usd=usd_value,
                        trade_count=0,
                    )

                    if outcome_index == 0:
                        top_buyers.append(trader)
                    else:
                        top_sellers.append(trader)

            # Fetch rankings for top traders in parallel
            ranking_tasks = []
            trader_refs = []
            for t in top_buyers + top_sellers:
                ranking_tasks.append(self.fetch_trader_ranking(t.wallet))
                trader_refs.append(t)

            if ranking_tasks:
                rankings = await asyncio.gather(*ranking_tasks, return_exceptions=True)
                for trader, ranking in zip(trader_refs, rankings):
                    if isinstance(ranking, TraderRanking) and ranking:
                        trader.rank = ranking.rank
                        trader.pnl = ranking.pnl
                        if ranking.user_name:
                            trader.name = ranking.user_name

            logger.debug(
                f"Market {market_id}: {len(top_buyers)} top Yes holders, "
                f"{len(top_sellers)} top No holders"
            )
            return top_buyers, top_sellers

        except Exception as e:
            logger.warning(f"Error fetching top holders for {market_id}: {e}")
            return [], []

    # ================================================================
    # Whale detection
    # ================================================================

    def _is_whale_trade(self, activity: TradeActivity, market: Optional[Market] = None) -> bool:
        """
        Multi-layer pre-filter mirroring options flow SignalFilter._check_signal.

        Filter chain (early rejection, same order as options flow):
        1. Price range — like moneyness filter (OTM/ITM range)
        2. Direction — BUY only (like enabled direction_filters)
        3. Resolution window — like DTE filter (3-60 days sweet spot)
        4. Size — like premium filter ($250K+ minimum)
        5. Dynamic size — like dynamic_premium (base × √(vol / baseline))
        6. Signal strength — like ask_ratio filter (conviction check)
        """
        import math
        from datetime import datetime as _dt

        # --- 1. Price range (like moneyness: OTM 0-20%) ---
        # Price 0.2-0.8 = uncertain outcome = tradeable
        # Price < 0.2 or > 0.8 = near-consensus = no edge
        if not (self.settings.min_price <= activity.price <= self.settings.max_price):
            return False

        # --- 2. Direction: BUY only (like direction_filters.enabled) ---
        # Already enforced upstream (only BUY trades reach here)

        # --- 3. Resolution window (like DTE min=3, max=60) ---
        # Markets resolving < 6 hours = price already settled (like DTE < 3)
        # Markets resolving > 90 days = too far out, edge diluted (like DTE > 60)
        if market and market.end_date:
            try:
                end_dt = _dt.fromisoformat(market.end_date.replace("Z", "+00:00"))
                now_dt = _dt.utcnow().replace(tzinfo=end_dt.tzinfo) if end_dt.tzinfo else _dt.utcnow()
                hours_to_resolution = max(0, (end_dt - now_dt).total_seconds() / 3600)
                if hours_to_resolution < 3:
                    return False  # too close, like DTE < 3
                if hours_to_resolution > 180 * 24:
                    return False  # too far, like DTE > 60
            except (ValueError, TypeError):
                pass  # unknown end date, don't reject

        # --- 4. Size (like premium min=$250K) ---
        # Base minimum: $5,000 (Polymarket scale vs options $250K)
        if activity.usdc_size < 3_000:
            return False

        # --- 5. Dynamic size (like dynamic_premium = base × √(mcap / baseline)) ---
        # Larger markets require proportionally larger trades to be meaningful
        base_size = 5_000.0
        baseline_volume = 1_000_000.0

        if market and market.volume > 0:
            threshold = base_size * math.sqrt(market.volume / baseline_volume)
            threshold = max(3_000.0, min(threshold, 50_000.0))  # floor $3K, cap $50K
        else:
            threshold = base_size

        if activity.usdc_size < threshold:
            return False

        # --- 6. Signal strength (like ask_ratio > 70%) ---
        # In Polymarket: buyer paying above market mid = conviction
        # Reject trades at or below market mid (no conviction, possibly hedging)
        if market and market.outcome_prices:
            if activity.outcome == "Yes":
                market_mid = market.outcome_prices[0]
            elif len(market.outcome_prices) > 1:
                market_mid = market.outcome_prices[1]
            else:
                market_mid = 1.0 - market.outcome_prices[0]

            # Must pay above market mid (no discount buys = no conviction)
            if activity.price < market_mid + 0.01:
                return False

        return True

    async def _handle_whale(self, activity: TradeActivity, market_id: str, market: Market):
        """
        Handle a single whale trade:
        1. Fetch trader info (ranking + history) for anomaly scoring
        2. Compute multi-dimensional anomaly score as pre-filter
        3. If score passes threshold, fetch full enrichment data and fire LLM callback
        """
        try:
            # Phase 1: Quick fetch — only ranking + history (needed for anomaly scoring)
            trader_ranking, trader_history = await asyncio.gather(
                self.fetch_trader_ranking(activity.proxy_wallet),
                self.fetch_trader_history(activity.proxy_wallet),
            )

            # Phase 2: Multi-dimensional anomaly scoring (pre-filter before LLM)
            should_analyze, score, breakdown = self._anomaly_detector.should_analyze(
                activity, market=market, trader_history=trader_history,
                market_id=market_id,
            )

            rank_str = f"(Rank #{trader_ranking.rank})" if trader_ranking and trader_ranking.rank else "(Unranked)"
            breakdown_short = " | ".join(f"{k}={v:.2f}" for k, v in breakdown.items())

            if not should_analyze:
                logger.info(
                    f"⚪ Whale below threshold: ${activity.usdc_size:,.2f} "
                    f"BUY {activity.outcome} @ {activity.price:.4f} {rank_str} "
                    f"score={score:.2f} [{breakdown_short}] — skipped LLM"
                )
                return

            logger.info(
                f"🐋 Whale trade detected! ${activity.usdc_size:,.2f} "
                f"BUY {activity.outcome} @ {activity.price:.4f} {rank_str} "
                f"score={score:.2f} [{breakdown_short}] on '{market.question[:50]}...'"
            )

            # Phase 3: Full enrichment (only for trades that pass pre-filter)
            event_positions, (top_buyers, top_sellers) = await asyncio.gather(
                self.fetch_whale_event_positions(
                    activity.proxy_wallet,
                    activity.event_slug,
                    market.condition_id or "",
                ),
                self.fetch_market_top_traders(
                    market_id, condition_id=market.condition_id or "",
                    outcome_prices=market.outcome_prices,
                ),
            )

            whale_trade = WhaleTrade(
                id=f"{market_id}_{activity.transaction_hash}",
                trade=activity,
                market_id=market_id,
                market_question=market.question,
                market_description=market.description,
                market_outcomes=market.outcomes,
                market_outcome_prices=market.outcome_prices,
                trader_ranking=trader_ranking,
                trader_history=trader_history,
                whale_event_positions=event_positions,
                market_top_buyers=top_buyers,
                market_top_sellers=top_sellers,
            )

            # Fire callback (LLM report generation)
            if self._on_whale_detected:
                await self._on_whale_detected(whale_trade)

        except Exception as e:
            logger.error(f"Error handling whale trade in {market_id}: {e}")

    # ================================================================
    # Per-market independent loop
    # ================================================================

    async def _market_loop(self, market_id: str, initial_delay: float):
        """
        Independent polling loop for a single market.

        Each market runs this as its own asyncio.Task:
        1. Wait initial_delay (stagger startup to avoid request storm)
        2. First poll: record existing transactions (no alerts)
        3. Subsequent polls: detect whales, handle in parallel
        """
        if initial_delay > 0:
            await asyncio.sleep(initial_delay)

        market = self._monitored_markets.get(market_id)
        if not market:
            return

        # Per-market interval (from tiered monitoring) or global default
        poll_intervals = getattr(self, '_market_poll_intervals', {})
        poll_interval = poll_intervals.get(market_id, self.settings.fetch_interval_seconds)
        # If we already have a last_ts for this market, it means the loop was
        # restarted (e.g. after a market list refresh) — skip the silent
        # first-poll window to avoid missing trades.
        is_first_poll = market_id not in self._market_last_ts

        while self._running:
            try:
                # Check if market was removed during refresh
                market = self._monitored_markets.get(market_id)
                if not market:
                    logger.debug(f"Market {market_id} no longer monitored, stopping loop")
                    break

                activities = await self.fetch_market_trades(market_id)

                # Collect whale handling tasks for this poll cycle
                whale_tasks = []

                for activity in activities:
                    # Record every trade for cluster detection
                    self._anomaly_detector.record_trade(activity, market_id)

                    if activity.transaction_hash in self._processed_txns:
                        continue
                    self._processed_txns.add(activity.transaction_hash)

                    # First poll: only record, don't alert
                    if is_first_poll:
                        continue

                    if self._is_whale_trade(activity, market=market):
                        # Launch whale handling as a parallel task
                        whale_tasks.append(
                            asyncio.create_task(
                                self._handle_whale(activity, market_id, market)
                            )
                        )

                # Wait for all whale handlers in this cycle to complete
                if whale_tasks:
                    await asyncio.gather(*whale_tasks, return_exceptions=True)

                is_first_poll = False

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in market loop {market_id}: {e}")

            await asyncio.sleep(poll_interval)

    # ================================================================
    # Main run loop
    # ================================================================

    async def run(self):
        """
        Start the parallel monitoring loop.

        Architecture (modeled after paper_trading._poll_trades):
        - Each market gets its own asyncio.Task (_market_loop)
        - Startup is staggered to avoid request storms
        - Main loop handles: task lifecycle, persistence, new market spawning
        """
        self._running = True
        poll_interval = self.settings.fetch_interval_seconds

        # Create lock/semaphore inside event loop (avoids "attached to different loop" error)
        self._api_lock = asyncio.Lock()
        self._api_sem = asyncio.Semaphore(10)  # max 10 concurrent API requests

        logger.info(
            f"Starting parallel trade monitor "
            f"({len(self._monitored_markets)} markets, interval: {poll_interval}s)"
        )

        try:
            # Spawn per-market tasks with staggered start
            market_ids = list(self._monitored_markets.keys())
            n_markets = len(market_ids)
            stagger_window = max(poll_interval, n_markets * 1.0)  # ~1s per market

            for i, market_id in enumerate(market_ids):
                delay = (i / max(n_markets, 1)) * stagger_window
                task = asyncio.create_task(self._market_loop(market_id, initial_delay=delay))
                self._market_tasks[market_id] = task

            logger.info(f"Spawned {len(self._market_tasks)} parallel market tasks")

            # Main supervisory loop
            save_interval = 60  # save processed txns every 60 seconds
            last_save = asyncio.get_event_loop().time()

            while self._running:
                now = asyncio.get_event_loop().time()

                # Spawn tasks for newly added markets (from set_monitored_markets)
                for market_id in self._monitored_markets:
                    if market_id not in self._market_tasks or self._market_tasks[market_id].done():
                        task = asyncio.create_task(
                            self._market_loop(market_id, initial_delay=0)
                        )
                        self._market_tasks[market_id] = task
                        logger.info(f"Spawned new task for market {market_id}")

                # Clean up tasks for removed markets
                removed = [mid for mid in self._market_tasks if mid not in self._monitored_markets]
                for mid in removed:
                    self._market_tasks[mid].cancel()
                    del self._market_tasks[mid]

                # Periodic persistence
                if now - last_save >= save_interval:
                    self._save_processed_txns()
                    last_save = now

                await asyncio.sleep(5.0)

        finally:
            # Cancel all market tasks
            for task in self._market_tasks.values():
                task.cancel()
            await asyncio.gather(*self._market_tasks.values(), return_exceptions=True)
            self._market_tasks.clear()
            self._save_processed_txns()

    def stop(self):
        """Stop the monitoring loop."""
        self._running = False
        logger.info("Trade monitor stopping...")

    def clear_processed_transactions(self):
        """Clear the processed transactions cache."""
        count = len(self._processed_txns)
        self._processed_txns.clear()
        logger.info(f"Cleared {count} processed transactions from cache")
