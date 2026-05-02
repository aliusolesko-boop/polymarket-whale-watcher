"""
Anomaly detection service — confidence scoring for whale trades.

Mirrors the options flow confidence scoring from llm-trading-agent:
  Base confidence: 0.50
  + Premium-to-threshold ratio: +0.20 (sqrt-scaled by market liquidity)
  + Signal cleanliness (ask ratio): +0.10
  + Volume/OI equivalent (depth ratio): +0.10
  + Alert rule / cluster tier: +0.10

Total max = 1.0. Pre-filter threshold = 0.60 (matches options flow pipeline).
"""
import logging
import math
import time
from collections import defaultdict, deque
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from src.config import get_settings
from src.models.trade import WhaleTrade, TradeActivity, TraderHistory
from src.models.market import Market
from src.services.trader_profiler import TraderProfiler

logger = logging.getLogger(__name__)


class AnomalyDetector:
    """
    Confidence scoring for whale trades, mirroring options flow pipeline.

    5-factor scoring (same structure as OptionsFlowSignalProvider._calculate_confidence):
    1. Base confidence: 0.50
    2. Premium-to-threshold ratio: +0.20 (trade_size vs dynamic threshold)
    3. Signal cleanliness: +0.10 (conviction / price displacement)
    4. Depth ratio: +0.10 (trade_size vs market liquidity, like Volume/OI)
    5. Cluster tier: +0.10 (repeated same-direction trades, like alert_rule)
    """

    # Cluster detection
    _CLUSTER_WINDOW_SECONDS = 300  # 5 minutes
    _CLUSTER_MIN_COUNT = 3

    def __init__(self):
        self.settings = get_settings()
        self.trader_profiler = TraderProfiler()
        self._recent_trades: Dict[str, deque] = defaultdict(
            lambda: deque(maxlen=50)
        )

    # ================================================================
    # Core scoring — mirrors OptionsFlowSignalProvider._calculate_confidence
    # ================================================================

    def get_anomaly_score(
        self,
        activity: TradeActivity,
        market: Optional[Market] = None,
        trader_history: Optional[TraderHistory] = None,
        market_id: str = "",
    ) -> Tuple[float, dict]:
        """
        Calculate confidence score using options-flow-style 5-factor model.

        Returns:
            (total_score, breakdown_dict) — total in [0.0, 1.0].
        """
        breakdown = {}

        # --- 1. Base confidence: 0.50 ---
        breakdown["base"] = 0.50

        # --- 2. Premium-to-threshold ratio: +0.20 max ---
        # Mirrors _premium_ratio_bonus: min(0.20, (sqrt(ratio) - 1) * 0.20)
        # where ratio = trade_size / dynamic_threshold
        # dynamic_threshold = base_size * sqrt(market_volume / baseline_volume)
        breakdown["premium_ratio"] = self._premium_ratio_bonus(activity, market)

        # --- 3. Signal cleanliness (conviction): +0.10 max ---
        # Mirrors _ask_ratio_bonus: measures taker aggressiveness.
        # In options: ASK-side ratio > 0.8 = full bonus.
        # In Polymarket: price displacement from market mid = conviction.
        breakdown["signal_clean"] = self._signal_cleanliness_bonus(activity, market)

        # --- 4. Depth ratio (like Volume/OI): +0.10 max ---
        # Mirrors _volume_oi_bonus: trade_size / market_liquidity.
        # High ratio = new significant positioning.
        breakdown["depth_ratio"] = self._depth_ratio_bonus(activity, market)

        # --- 5. Cluster tier (like alert_rule): +0.10 max ---
        # Mirrors _alert_rule_bonus: repeated same-direction activity.
        # In options: RepeatedHitsAscendingFill = 0.10, RepeatedHits = 0.07.
        # In Polymarket: multiple same-direction trades in 5-min window.
        breakdown["cluster_tier"] = self._cluster_tier_bonus(activity, market_id)

        # --- Total ---
        total = sum(breakdown.values())
        total = min(1.0, max(0.0, total))

        return total, breakdown

    @staticmethod
    def _premium_ratio_bonus(activity: TradeActivity, market: Optional[Market]) -> float:
        """
        Trade size vs dynamic threshold, sqrt-scaled.

        Mirrors OptionsFlowSignalProvider._premium_ratio_bonus:
          threshold = 100K * sqrt(market_cap / 40B)
          bonus = min(0.20, (sqrt(premium / threshold) - 1) * 0.20)

        Polymarket mapping:
          threshold = base_size * sqrt(market_volume / baseline_volume)
          base_size = $5,000, baseline_volume = $1,000,000
        """
        max_bonus = 0.20
        trade_size = activity.usdc_size

        if trade_size <= 0:
            return 0.0

        # Dynamic threshold based on market volume (like market-cap scaling)
        base_size = 10_000.0
        baseline_volume = 1_000_000.0

        if market and market.volume > 0:
            threshold = base_size * math.sqrt(market.volume / baseline_volume)
            threshold = max(5_000.0, threshold)  # floor $5K
        else:
            threshold = base_size

        ratio = trade_size / threshold
        if ratio <= 1.0:
            return 0.0

        bonus = (math.sqrt(ratio) - 1) * max_bonus
        return min(max_bonus, max(0.0, bonus))

    @staticmethod
    def _signal_cleanliness_bonus(activity: TradeActivity, market: Optional[Market]) -> float:
        """
        Taker conviction / price displacement from market mid.

        Mirrors _ask_ratio_bonus logic:
          ASK ratio > 0.8 → 0.10 (full), > 0.6 → 0.05 (half)

        Polymarket mapping:
          A buyer paying significantly above market mid = aggressive taker (like ASK-side).
          Displacement > 5% → 0.10, > 2% → 0.05.
        """
        if not market or not market.outcome_prices:
            return 0.0

        # Get market mid price for the outcome the trader bought
        if activity.outcome == "Yes":
            market_mid = market.outcome_prices[0]
        elif len(market.outcome_prices) > 1:
            market_mid = market.outcome_prices[1]
        else:
            market_mid = 1.0 - market.outcome_prices[0]

        displacement = activity.price - market_mid

        if displacement > 0.05:
            return 0.10  # strong conviction (like ASK ratio > 0.8)
        if displacement > 0.02:
            return 0.05  # moderate conviction (like ASK ratio > 0.6)
        return 0.0

    @staticmethod
    def _depth_ratio_bonus(activity: TradeActivity, market: Optional[Market]) -> float:
        """
        Trade size vs market liquidity (like Volume/OI).

        Mirrors _volume_oi_bonus:
          V/OI > 3.0 → 0.10, > 1.5 → 0.07, > 1.0 → 0.03

        Polymarket mapping:
          depth_ratio = trade_size / market_liquidity
          > 0.10 → 0.10, > 0.05 → 0.07, > 0.02 → 0.03
        """
        if not market or not market.liquidity or market.liquidity <= 0:
            return 0.0

        ratio = activity.usdc_size / market.liquidity

        if ratio > 0.10:
            return 0.10
        if ratio > 0.05:
            return 0.07
        if ratio > 0.02:
            return 0.03
        return 0.0

    def _cluster_tier_bonus(self, activity: TradeActivity, market_id: str = "") -> float:
        """
        Cluster of same-direction trades in short window (like alert_rule tiers).

        Mirrors _alert_rule_bonus tier structure:
          RepeatedHitsAscendingFill → 0.10
          RepeatedHits → 0.07
          SweepsFollowedByFloor → 0.05
          Single sweep → 0.03

        Polymarket mapping:
          5+ same-direction trades in 5min → 0.10
          3-4 trades → 0.07
          2 trades with large volume → 0.03
        """
        key = market_id or activity.condition_id
        recent = self._recent_trades.get(key)
        if not recent:
            return 0.0

        now = activity.timestamp
        cutoff = now - self._CLUSTER_WINDOW_SECONDS

        same_dir_count = 0
        same_dir_volume = 0.0
        for ts, side, size in recent:
            if ts >= cutoff and side == activity.side:
                same_dir_count += 1
                same_dir_volume += size

        if same_dir_count >= 5:
            return 0.10
        if same_dir_count >= self._CLUSTER_MIN_COUNT:
            return 0.07
        if same_dir_count >= 2 and same_dir_volume > 20_000:
            return 0.03
        return 0.0

    def record_trade(self, activity: TradeActivity, market_id: str):
        """Record a trade for cluster detection. Call for every trade, not just whales."""
        self._recent_trades[market_id].append((
            activity.timestamp,
            activity.side,
            activity.usdc_size,
        ))

    # ================================================================
    # Pre-filter (before LLM)
    # ================================================================

    def should_analyze(
        self,
        activity: TradeActivity,
        market: Optional[Market] = None,
        trader_history: Optional[TraderHistory] = None,
        market_id: str = "",
        min_score: float = 0.65,
    ) -> Tuple[bool, float, dict]:
        """
        Decide whether a whale trade warrants LLM analysis.

        Threshold 0.65: requires at least base (0.50) + one strong factor
        to trigger LLM analysis.
        """
        score, breakdown = self.get_anomaly_score(
            activity, market, trader_history, market_id=market_id,
        )
        return score >= min_score, score, breakdown

    # ================================================================
    # Legacy compatibility
    # ================================================================

    def is_anomalous_trade(self, activity: TradeActivity) -> bool:
        """Check if a trade is anomalous based on size and price."""
        if activity.usdc_size < self.settings.min_trade_size_usd:
            return False
        if not (self.settings.min_price <= activity.price <= self.settings.max_price):
            return False
        return True

    def filter_whale_trades(
        self,
        trades: List[WhaleTrade],
        min_score: float = 0.65,
    ) -> List[WhaleTrade]:
        """Filter whale trades by confidence score."""
        filtered = []
        for trade in trades:
            score, _ = self.get_anomaly_score(trade.trade)
            if score >= min_score:
                filtered.append(trade)
        return filtered

    # ================================================================
    # LLM context formatting
    # ================================================================

    def analyze_trade_context(self, whale_trade: WhaleTrade) -> dict:
        """Analyze the context of a whale trade for LLM input."""
        trade = whale_trade.trade

        if trade.outcome == "Yes":
            direction_meaning = f"Trader bought Yes Token @ {trade.price:.4f} — Bullish (believes event will occur)"
        else:
            direction_meaning = f"Trader bought No Token @ {trade.price:.4f} — Bearish (believes event will NOT occur)"

        implied_prob = trade.price

        market_state = "uncertain"
        if whale_trade.market_outcome_prices:
            max_price = max(whale_trade.market_outcome_prices)
            if max_price > 0.7:
                market_state = "leaning towards one outcome"
            elif max_price < 0.6:
                market_state = "highly uncertain"

        score, breakdown = self.get_anomaly_score(trade)

        return {
            "trade_size_usd": trade.usdc_size,
            "trade_side": trade.side,
            "trade_price": trade.price,
            "trade_outcome": trade.outcome,
            "direction_meaning": direction_meaning,
            "implied_probability": implied_prob,
            "market_state": market_state,
            "anomaly_score": score,
            "anomaly_breakdown": breakdown,
            "market_question": whale_trade.market_question,
            "market_outcomes": whale_trade.market_outcomes,
            "current_prices": whale_trade.market_outcome_prices,
        }

    def format_for_llm(self, whale_trade: WhaleTrade) -> str:
        """Format whale trade data for LLM analysis."""
        context = self.analyze_trade_context(whale_trade)
        trade = whale_trade.trade

        prices_str = ""
        for outcome, price in zip(context["market_outcomes"], context["current_prices"]):
            prices_str += f"  - {outcome}: {price:.2%}\n"

        trader_profile = self.trader_profiler.generate_profile(
            wallet_address=trade.proxy_wallet or "Unknown",
            ranking=whale_trade.trader_ranking,
            history=whale_trade.trader_history,
        )
        trader_profile_str = self.trader_profiler.format_profile_for_llm(trader_profile)

        bd = context["anomaly_breakdown"]
        breakdown_str = (
            f"  Base: {bd.get('base', 0):.2f} | "
            f"Premium ratio: {bd.get('premium_ratio', 0):.2f} | "
            f"Signal clean: {bd.get('signal_clean', 0):.2f} | "
            f"Depth ratio: {bd.get('depth_ratio', 0):.2f} | "
            f"Cluster tier: {bd.get('cluster_tier', 0):.2f}"
        )

        return f"""
## Whale Trade Anomaly Detection Report

### Trade Details
- **Trade amount**: ${context['trade_size_usd']:,.2f} USDC
- **Trade direction**: BUY {context['trade_outcome']} Token ({'Bullish' if context['trade_outcome'] == 'Yes' else 'Bearish'})
- **Buy price**: {context['trade_price']:.4f} (~{1/context['trade_price']:.1f}x odds)
- **Trade time**: {datetime.fromtimestamp(trade.timestamp).strftime('%Y-%m-%d %H:%M:%S UTC')}
- **Trader wallet**: {trade.proxy_wallet or 'Unknown'}

### Confidence Score
- **Overall score**: {context['anomaly_score']:.2f}/1.00
- **Score breakdown**:
{breakdown_str}

### Trade Interpretation
- **Direction**: {context['direction_meaning']}
{trader_profile_str}

### Market Information
- **Market question**: {context['market_question']}
- **Market description**: {whale_trade.market_description or 'N/A'}
- **Market state**: {context['market_state']}
- **Current odds**:
{prices_str}

{whale_trade.format_event_positions()}

{whale_trade.format_top_traders()}

### Analysis Points
1. This is a ${context['trade_size_usd']:,.2f} large trade, direction: **BUY {context['trade_outcome']} Token**
2. {context['direction_meaning']}
3. **Focus on the Trader Profile JSON above — assess trader credibility from ranking, PnL, trading behavior, and recent trades**
4. **Analyze the whale's positions in other markets under the same event** — opposing positions may indicate hedging
5. **Reference the market's Top long/short holders** — which side has the concentration of high-ranked traders

Please analyze the information asymmetry likelihood of this trade.
"""
