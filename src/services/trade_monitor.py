"""
Trade monitoring service — RTDS WebSocket architecture.

Single WebSocket connection receives ALL trades in real-time from
Polymarket RTDS (wss://ws-live-data.polymarket.com).

For each incoming trade:
1. Record for cluster detection (anomaly detector)
2. Dedup by transaction hash
3. Filter: whale pre-filter (price range, size, conviction)
4. Enrich: trader ranking + history → anomaly score
5. If score passes threshold → full enrichment + LLM callback

Replaces the previous per-market HTTP polling architecture.
"""
import asyncio
import json
import logging
import math
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
from src.services.rtds_client import RTDSClient

logger = logging.getLogger(__name__)

# Gamma API for fetching latest market prices
GAMMA_API_URL = "https://gamma-api.polymarket.com/markets"

# File to persist processed transaction hashes
PROCESSED_TXNS_FILE = Path(__file__).parent.parent.parent / "data" / "processed_transactions.json"


class TradeMonitor:
    """
    Monitors Polymarket markets for large trades via RTDS WebSocket.

    Architecture: single WebSocket connection → filter → enrich → callback.
    """

    def __init__(
        self,
        on_whale_detected: Optional[Callable[[WhaleTrade], Awaitable[None]]] = None,
    ):
        self.settings = get_settings()

        # RTDS WebSocket client (created in run())
        self._rtds: Optional[RTDSClient] = None

        # HTTP client for enrichment API calls (trader ranking, history, etc.)
        self.data_api_url = "https://data-api.polymarket.com"
        self.leaderboard_endpoint = f"{self.data_api_url}/v1/leaderboard"
        self.trades_endpoint = f"{self.data_api_url}/trades"
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, pool=120.0),
            limits=httpx.Limits(
                max_connections=50,
                max_keepalive_connections=20,
                keepalive_expiry=30,
            ),
        )

        # Cache for trader rankings to avoid repeated API calls
        self._trader_ranking_cache: Dict[str, TraderRanking] = {}

        # Markets being monitored: condition_id -> Market
        # Used for enrichment (market question, description, etc.)
        self._monitored_markets: Dict[str, Market] = {}
        # condition_id -> market_id mapping
        self._condition_to_market_id: Dict[str, str] = {}

        # Track processed transactions to avoid duplicates
        self._processed_txns: Set[str] = set()
        self._load_processed_txns()

        # Anomaly detector for multi-dimensional scoring
        self._anomaly_detector = AnomalyDetector()

        # Callback for whale detection
        self._on_whale_detected = on_whale_detected

        # Control flag
        self._running = False

        # Flag to suppress alerts during initial warmup
        self._warmup_complete = False
        self._warmup_seconds = 10  # seconds to collect baseline before alerting

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
        self._condition_to_market_id = {}
        for tm in markets:
            m = tm.market
            if m.id and m.condition_id:
                self._monitored_markets[m.condition_id] = m
                self._condition_to_market_id[m.condition_id] = m.id
        logger.info(f"Now monitoring {len(self._monitored_markets)} markets")

    def set_tiered_markets(self, tiers: dict[str, list]) -> None:
        """Set markets from tiered scan (same interface as before)."""
        self._monitored_markets = {}
        self._condition_to_market_id = {}

        for tier_name, markets in tiers.items():
            for tm in markets:
                m = tm.market
                if m.id and m.condition_id:
                    self._monitored_markets[m.condition_id] = m
                    self._condition_to_market_id[m.condition_id] = m.id

        total = len(self._monitored_markets)
        tier_counts = {k: len(v) for k, v in tiers.items()}
        logger.info(f"Tiered monitoring: {tier_counts}, total={total}")

    # ================================================================
    # RTDS trade handler (core of the new architecture)
    # ================================================================

    async def _on_rtds_trade(self, activity: TradeActivity) -> None:
        """
        Called for every trade received from RTDS WebSocket.

        This replaces the per-market polling loop.
        """
        condition_id = activity.condition_id

        # Look up market info (enrichment data)
        market = self._monitored_markets.get(condition_id)
        market_id = self._condition_to_market_id.get(condition_id, "")

        # Record every trade for cluster detection (even unmonitored markets)
        if market_id:
            self._anomaly_detector.record_trade(activity, market_id)

        # Dedup by transaction hash + outcome (same tx can have multiple fills)
        dedup_key = f"{activity.transaction_hash}_{activity.outcome}_{activity.size}"
        if dedup_key in self._processed_txns:
            return
        self._processed_txns.add(dedup_key)

        # Skip unmonitored markets
        if not market:
            return

        # Skip during warmup period (avoid alerting on historical trades)
        if not self._warmup_complete:
            return

        # Only track BUY trades (new positions)
        if activity.side != "BUY":
            return

        # Whale pre-filter
        if not self._is_whale_trade(activity, market=market):
            return

        # Handle whale (enrich + score + callback)
        asyncio.create_task(self._handle_whale(activity, market_id, market))

    # ================================================================
    # Whale detection (unchanged from original)
    # ================================================================

    def _is_whale_trade(self, activity: TradeActivity, market: Optional[Market] = None) -> bool:
        """
        Multi-layer pre-filter mirroring options flow SignalFilter._check_signal.

        Filter chain (early rejection):
        1. Price range — like moneyness filter (OTM/ITM range)
        2. Direction — BUY only (like enabled direction_filters)
        3. Resolution window — like DTE filter (3-60 days sweet spot)
        4. Size — like premium filter ($250K+ minimum)
        5. Dynamic size — like dynamic_premium (base × √(vol / baseline))
        6. Signal strength — like ask_ratio filter (conviction check)
        """
        # --- 1. Price range ---
        if not (self.settings.min_price <= activity.price <= self.settings.max_price):
            return False

        # --- 2. Direction: BUY only ---
        # Already enforced upstream

        # --- 3. Resolution window ---
        if market and market.end_date:
            try:
                end_dt = datetime.fromisoformat(market.end_date.replace("Z", "+00:00"))
                now_dt = datetime.utcnow().replace(tzinfo=end_dt.tzinfo) if end_dt.tzinfo else datetime.utcnow()
                hours_to_resolution = max(0, (end_dt - now_dt).total_seconds() / 3600)
                if hours_to_resolution < 3:
                    return False
                if hours_to_resolution > 180 * 24:
                    return False
            except (ValueError, TypeError):
                pass

        # --- 4. Size ---
        if activity.usdc_size < 3_000:
            return False

        # --- 5. Dynamic size ---
        base_size = 5_000.0
        baseline_volume = 1_000_000.0

        if market and market.volume > 0:
            threshold = base_size * math.sqrt(market.volume / baseline_volume)
            threshold = max(3_000.0, min(threshold, 50_000.0))
        else:
            threshold = base_size

        if activity.usdc_size < threshold:
            return False

        # --- 6. Signal strength ---
        if market and market.outcome_prices:
            if activity.outcome == "Yes":
                market_mid = market.outcome_prices[0]
            elif len(market.outcome_prices) > 1:
                market_mid = market.outcome_prices[1]
            else:
                market_mid = 1.0 - market.outcome_prices[0]

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
            # Phase 1: Quick fetch — ranking + history for anomaly scoring
            trader_ranking, trader_history = await asyncio.gather(
                self.fetch_trader_ranking(activity.proxy_wallet),
                self.fetch_trader_history(activity.proxy_wallet),
            )

            # Phase 2: Anomaly scoring
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

            # Phase 3: Full enrichment
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
    # Enrichment API calls (unchanged — still uses HTTP)
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

    async def fetch_whale_event_positions(
        self,
        wallet_address: str,
        event_slug: str,
        current_condition_id: str,
    ) -> List[EventPosition]:
        """Fetch the whale's positions across all markets in the same event."""
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
                    continue

                outcome = pos.get("outcome", "Yes")
                avg_price = float(pos.get("avgPrice", 0) or 0)
                cur_price = float(pos.get("curPrice", 0) or 0)
                current_value = float(pos.get("currentValue", 0) or 0)
                initial_value = float(pos.get("initialValue", 0) or 0)
                cash_pnl = float(pos.get("cashPnl", 0) or 0)
                title = pos.get("title", "")

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
        """Fetch top holders (bulls and bears) for a market."""
        if not condition_id:
            return [], []

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

                outcome_index = holders[0].get("outcomeIndex", 0)
                token_price = yes_price if outcome_index == 0 else no_price

                for h in holders[:top_n]:
                    wallet = h.get("proxyWallet", "")
                    name = h.get("name") or h.get("pseudonym") or None
                    amount = float(h.get("amount", 0) or 0)
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

            # Fetch rankings in parallel
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
    # Main run loop
    # ================================================================

    async def run(self):
        """
        Start the RTDS-based monitoring loop.

        Architecture:
        - Single RTDS WebSocket receives ALL trades in real-time
        - _on_rtds_trade filters and handles each trade
        - Periodic persistence of processed transactions
        """
        self._running = True

        # Create RTDS client with our trade handler
        self._rtds = RTDSClient(on_trade=self._on_rtds_trade)

        logger.info(
            f"Starting RTDS trade monitor "
            f"({len(self._monitored_markets)} monitored markets)"
        )

        # Start warmup timer — suppress alerts for first N seconds
        # to avoid firing on trades already in the RTDS pipeline
        async def warmup_timer():
            await asyncio.sleep(self._warmup_seconds)
            self._warmup_complete = True
            logger.info(
                f"Warmup complete ({self._warmup_seconds}s). "
                f"Now alerting on new whale trades."
            )

        warmup_task = asyncio.create_task(warmup_timer())

        # Periodic persistence task
        async def persistence_loop():
            while self._running:
                await asyncio.sleep(60)
                self._save_processed_txns()
                # Trim processed txns set to prevent unbounded growth
                if len(self._processed_txns) > 100_000:
                    # Keep only the most recent 50K (approximate — set is unordered,
                    # but old hashes won't repeat so trimming is safe)
                    excess = len(self._processed_txns) - 50_000
                    for _ in range(excess):
                        self._processed_txns.pop()
                    logger.info(f"Trimmed processed txns to {len(self._processed_txns)}")

        persistence_task = asyncio.create_task(persistence_loop())

        try:
            # Run RTDS client (blocks until stop)
            await self._rtds.run()
        finally:
            warmup_task.cancel()
            persistence_task.cancel()
            self._save_processed_txns()

    def stop(self):
        """Stop the monitoring loop."""
        self._running = False
        if self._rtds:
            self._rtds.stop()
        logger.info("Trade monitor stopping...")

    def clear_processed_transactions(self):
        """Clear the processed transactions cache."""
        count = len(self._processed_txns)
        self._processed_txns.clear()
        logger.info(f"Cleared {count} processed transactions from cache")
