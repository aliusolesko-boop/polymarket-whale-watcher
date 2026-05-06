"""
RTDS (Real-Time Data Socket) client for Polymarket.

Connects to wss://ws-live-data.polymarket.com and subscribes to
activity/trades for real-time trade data across ALL markets.

Replaces the per-market HTTP polling approach with a single persistent
WebSocket connection — zero missed trades, sub-second latency.
"""
import asyncio
import json
import logging
import time
from typing import Awaitable, Callable, Optional

import websockets
from websockets.asyncio.client import ClientConnection

from src.models.trade import TradeActivity

logger = logging.getLogger(__name__)

RTDS_URI = "wss://ws-live-data.polymarket.com"
HEARTBEAT_INTERVAL = 5  # seconds
RECONNECT_DELAYS = [1, 2, 5, 10, 30, 60]  # backoff schedule


class RTDSClient:
    """
    Persistent WebSocket client for Polymarket RTDS trade stream.

    Features:
    - Auto-reconnect with exponential backoff
    - Heartbeat (PING every 5s)
    - Parses raw messages into TradeActivity objects
    - Fires an async callback for each trade
    """

    def __init__(
        self,
        on_trade: Optional[Callable[[TradeActivity], Awaitable[None]]] = None,
    ):
        self._on_trade = on_trade
        self._running = False
        self._ws: Optional[ClientConnection] = None
        self._trade_count = 0
        self._connect_count = 0

    # ================================================================
    # Message parsing
    # ================================================================

    @staticmethod
    def _parse_trade(payload: dict) -> Optional[TradeActivity]:
        """Convert an RTDS trade payload into a TradeActivity."""
        try:
            side = (payload.get("side") or "").upper()
            size = float(payload.get("size", 0) or 0)
            price = float(payload.get("price", 0) or 0)
            usdc_size = size * price

            outcome = payload.get("outcome", "Yes")
            outcome_index = int(payload.get("outcomeIndex", 0 if outcome == "Yes" else 1))

            ts = int(payload.get("timestamp", 0) or 0)
            if ts == 0:
                ts = int(time.time())

            return TradeActivity(
                transaction_hash=payload.get("transactionHash", ""),
                timestamp=ts,
                condition_id=payload.get("conditionId", ""),
                asset=payload.get("asset", ""),
                side=side,
                size=size,
                usdc_size=usdc_size,
                price=price,
                outcome=outcome,
                outcome_index=outcome_index,
                title=payload.get("title", ""),
                slug=payload.get("slug"),
                event_slug=payload.get("eventSlug"),
                proxy_wallet=payload.get("proxyWallet"),
                name=payload.get("name") or payload.get("pseudonym"),
            )
        except Exception as e:
            logger.debug(f"Failed to parse RTDS trade: {e}")
            return None

    # ================================================================
    # Connection lifecycle
    # ================================================================

    async def _heartbeat(self, ws: ClientConnection) -> None:
        """Send PING every HEARTBEAT_INTERVAL seconds."""
        try:
            while True:
                await asyncio.sleep(HEARTBEAT_INTERVAL)
                await ws.send("PING")
        except (asyncio.CancelledError, websockets.ConnectionClosed):
            pass

    async def _subscribe(self, ws: ClientConnection) -> None:
        """Subscribe to the activity/trades stream."""
        msg = {
            "action": "subscribe",
            "subscriptions": [
                {"topic": "activity", "type": "trades", "filters": ""}
            ],
        }
        await ws.send(json.dumps(msg))
        logger.info("Subscribed to RTDS activity/trades")

    async def _consume(self, ws: ClientConnection) -> None:
        """Read messages from the WebSocket and dispatch trades."""
        async for raw in ws:
            if not self._running:
                break

            if raw == "PONG" or not raw.strip():
                continue

            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            if msg.get("topic") != "activity" or msg.get("type") != "trades":
                continue

            payload = msg.get("payload")
            if not payload:
                continue

            activity = self._parse_trade(payload)
            if not activity:
                continue

            self._trade_count += 1

            if self._on_trade:
                try:
                    await self._on_trade(activity)
                except Exception as e:
                    logger.error(f"Error in trade callback: {e}")

    async def _connect_and_run(self) -> None:
        """Single connection attempt: connect → subscribe → consume."""
        self._connect_count += 1
        logger.info(
            f"Connecting to RTDS ({self._connect_count})... "
            f"(total trades so far: {self._trade_count})"
        )

        async with websockets.connect(RTDS_URI, ping_interval=None) as ws:
            self._ws = ws
            logger.info("RTDS connected")

            await self._subscribe(ws)

            hb_task = asyncio.create_task(self._heartbeat(ws))
            try:
                await self._consume(ws)
            finally:
                hb_task.cancel()
                self._ws = None

    # ================================================================
    # Public API
    # ================================================================

    async def run(self) -> None:
        """
        Start the RTDS client with auto-reconnect.

        Runs forever until stop() is called.
        """
        self._running = True
        consecutive_failures = 0

        while self._running:
            try:
                await self._connect_and_run()
                # Clean disconnect (stop() called) — exit
                if not self._running:
                    break
                # Unexpected clean close — reconnect immediately
                consecutive_failures = 0
            except (
                websockets.ConnectionClosed,
                websockets.InvalidURI,
                websockets.InvalidHandshake,
                OSError,
                ConnectionError,
            ) as e:
                if not self._running:
                    break
                delay_idx = min(consecutive_failures, len(RECONNECT_DELAYS) - 1)
                delay = RECONNECT_DELAYS[delay_idx]
                consecutive_failures += 1
                logger.warning(
                    f"RTDS disconnected: {type(e).__name__}: {e}. "
                    f"Reconnecting in {delay}s (attempt {consecutive_failures})"
                )
                await asyncio.sleep(delay)
            except Exception as e:
                if not self._running:
                    break
                logger.error(f"Unexpected RTDS error: {e}. Reconnecting in 10s")
                await asyncio.sleep(10)

        logger.info(f"RTDS client stopped (total trades received: {self._trade_count})")

    def stop(self) -> None:
        """Stop the RTDS client."""
        self._running = False
        if self._ws:
            asyncio.ensure_future(self._ws.close())
        logger.info("RTDS client stopping...")

    @property
    def trade_count(self) -> int:
        """Total number of trades received since start."""
        return self._trade_count

    @property
    def is_connected(self) -> bool:
        """Whether the WebSocket is currently connected."""
        return self._ws is not None and self._ws.state.name == "OPEN"
