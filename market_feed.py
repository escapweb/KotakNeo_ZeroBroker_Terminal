"""Async Kotak SFeed bridge for the synchronous, single-process Flask app.

One daemon thread owns the asyncio loop and the broker WebSocket. Flask routes
change the desired contract subscription and read a thread-safe quote cache.
"""

from __future__ import annotations

import asyncio
import queue
import threading
from datetime import datetime
from typing import Any

from neo_api_client.websocket.feed import SFeedScrip, WsToken


class MarketFeed:
    def __init__(self, client: Any):
        self._client = client
        self._lock = threading.RLock()
        self._quotes: dict[str, dict[str, Any]] = {}
        self._chain_tokens: set[WsToken] = set()
        self._position_tokens: set[WsToken] = set()
        self._desired_tokens: set[WsToken] = set()
        self._commands: queue.Queue[set[WsToken] | None] = queue.Queue(maxsize=1)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._status = "not_started"
        self._status_detail: str | None = None
        self._connected_at: str | None = None
        self._last_message_at: str | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._thread_main,
            name="kotak-sfeed",
            daemon=True,
        )
        self._thread.start()

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._run_forever())
        except Exception as exc:
            self._set_status("stopped", type(exc).__name__)

    async def _run_forever(self) -> None:
        delay = 1
        while not self._stop.is_set():
            try:
                self._set_status("connecting")
                async with self._client.create_websocket() as ws:
                    with self._lock:
                        initial_tokens = set(self._desired_tokens)
                    if initial_tokens:
                        await ws.subscribe_scrips(list(initial_tokens))

                    self._set_status("connected")
                    self._connected_at = self._now()
                    delay = 1

                    reader = asyncio.create_task(self._read_messages(ws))
                    controller = asyncio.create_task(
                        self._control_subscriptions(ws, initial_tokens)
                    )
                    done, pending = await asyncio.wait(
                        {reader, controller},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for task in pending:
                        task.cancel()
                    await asyncio.gather(*pending, return_exceptions=True)

                    for task in done:
                        error = task.exception()
                        if error is not None:
                            raise error

                if not self._stop.is_set():
                    raise ConnectionError("SFeed connection ended")

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self._stop.is_set():
                    break
                self._set_status("reconnecting", type(exc).__name__)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)

        self._set_status("stopped")

    async def _read_messages(self, ws: Any) -> None:
        async for message in ws:
            if not isinstance(message, SFeedScrip):
                continue

            segment = str(getattr(message, "exchange_segment", ""))
            token = str(getattr(message, "instrument_token", ""))
            if not segment or not token:
                continue

            key = f"{segment}|{token}"
            last_update_time = getattr(message, "last_update_time", None)
            is_live = last_update_time not in (None, 0, "0", "")
            quote = {
                "ltp": getattr(message, "last_traded_price", None),
                "last_update_time": str(last_update_time) if last_update_time is not None else None,
                "is_live": is_live,
                "trading_symbol": getattr(message, "trading_symbol", None),
                "received_at": self._now(),
            }
            with self._lock:
                self._quotes[key] = quote
                self._last_message_at = quote["received_at"]

    async def _control_subscriptions(
        self,
        ws: Any,
        current: set[WsToken],
    ) -> None:
        while not self._stop.is_set():
            try:
                desired = await asyncio.to_thread(self._commands.get, True, 1.0)
            except queue.Empty:
                continue
            if desired is None:
                return

            removed = current - desired
            added = desired - current
            if removed:
                await ws.unsubscribe_scrips(list(removed))
            if added:
                await ws.subscribe_scrips(list(added))
            current = desired

    def set_chain(self, chain: dict[str, Any]) -> None:
        """Subscribe to the visible option contracts and their underlying index."""
        tokens: set[WsToken] = set()
        spot_symbol = chain.get("spot_neo_symbol")
        if spot_symbol and "|" in str(spot_symbol):
            segment, token = str(spot_symbol).split("|", 1)
            tokens.add(WsToken(segment, token))
        for row in chain.get("rows", []):
            for side in ("call", "put"):
                contract = row.get(side) or {}
                neo_symbol = contract.get("neo_symbol")
                if not neo_symbol or "|" not in str(neo_symbol):
                    continue
                segment, token = str(neo_symbol).split("|", 1)
                tokens.add(WsToken(segment, token))
        self._chain_tokens = tokens
        self._apply_subscriptions()

    def set_positions(self, positions: list[dict[str, Any]]) -> None:
        """Keep open option-position contracts quoted alongside the visible chain."""
        tokens: set[WsToken] = set()
        for position in positions:
            neo_symbol = position.get("neo_symbol")
            if not neo_symbol or "|" not in str(neo_symbol):
                continue
            segment, token = str(neo_symbol).split("|", 1)
            tokens.add(WsToken(segment, token))
        self._position_tokens = tokens
        self._apply_subscriptions()

    def _apply_subscriptions(self) -> None:
        self.replace_tokens(set(self._chain_tokens) | set(self._position_tokens))

    def replace_tokens(self, tokens: set[WsToken]) -> None:
        with self._lock:
            self._desired_tokens = set(tokens)
            allowed_keys = {
                f"{token.exchange_segment}|{token.instrument_token}"
                for token in self._desired_tokens
            }
            self._quotes = {
                key: value for key, value in self._quotes.items()
                if key in allowed_keys
            }

        # Keep only the newest desired subscription if UI selections change
        # quickly while the broker feed is catching up.
        try:
            self._commands.put_nowait(set(tokens))
        except queue.Full:
            try:
                self._commands.get_nowait()
            except queue.Empty:
                pass
            try:
                self._commands.put_nowait(set(tokens))
            except queue.Full:
                pass

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "status": self._status,
                "status_detail": self._status_detail,
                "connected_at": self._connected_at,
                "last_message_at": self._last_message_at,
                "live_count": sum(1 for quote in self._quotes.values() if quote.get("is_live")),
                "prices": dict(self._quotes),
            }

    def close(self) -> None:
        self._stop.set()
        try:
            self._commands.put_nowait(None)
        except queue.Full:
            try:
                self._commands.get_nowait()
            except queue.Empty:
                pass
            try:
                self._commands.put_nowait(None)
            except queue.Full:
                pass

    def _set_status(self, status: str, detail: str | None = None) -> None:
        with self._lock:
            self._status = status
            self._status_detail = detail

    @staticmethod
    def _now() -> str:
        return datetime.now().astimezone().isoformat(timespec="seconds")
